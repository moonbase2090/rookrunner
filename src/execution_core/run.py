"""Run one planned job as sequential Bash steps in a caller-pinned container.

This is the library later worker code can call. It is not a protocol method.
The caller supplies the image digest. This module does not select a default
image.

Shell behavior follows GitHub's workflow syntax for Linux runners, reviewed
2026-10-02. An omitted shell is `bash -e {0}`. `shell: bash` is
`bash --noprofile --norc -eo pipefail {0}`. `shell: sh` is `sh -e {0}`. If the
image has no bash, the omitted shell and `bash` fall back to `sh -e {0}`.
A custom shell is accepted only when the command is bash or sh and `{0}` is
its own argument. This is the Linux runner default, not the GitHub `container`
job default of sh. The `container` key remains unsupported.

Workflow env is overridden by job env, then by `GITHUB_ENV` written by an
earlier step of the same job, then by step env. `GITHUB_PATH` from earlier
steps is prepended to the step's `PATH` when that step sets `PATH`, and
otherwise to the container `PATH`. The runner then sets `GITHUB_WORKSPACE`,
`ROOKRUNNER_EVENT`, `GITHUB_ENV`, `GITHUB_OUTPUT`, and `GITHUB_PATH`, so
those names stay pointed at this attempt. `ROOKRUNNER_EVENT` is a read-only
file holding the caller event as canonical JSON. It is not a GitHub event
delivery. No other GitHub context is invented. `runs-on` does not select an
image.

`GITHUB_ENV`, `GITHUB_OUTPUT`, and `GITHUB_PATH` are per-step files. A write
applies to later steps in the same job, including a later step whose `if` is
true after a failed step. It does not apply when the step is skipped, fails
before exec, or times out, and it does not carry into the next job. Stdout
workflow commands can mask later log text in that job. `set-env` and
`add-path` are ignored. stderr is captured apart from stdout, so the whole
step stderr is masked with every mask registered while reading that step's
stdout. The workflow commands page says a masked value cannot be set as an
output, and its example writes that value to `GITHUB_OUTPUT` and reads it
back. This subset follows the example: the output is kept, and logs of that
value in the same job are masked. A later job does not inherit the mask.

The container is created with network `none`. The Docker socket and host
credential directories are not mounted. Step `if` and job `if` are evaluated.
Expressions in workflow `run`, workflow and step `env`, and `name` stay
literal text. `github.event` is the caller event. Other `github` properties
are not invented. `runner.os` is `Linux` because this subset runs in a Linux
container. Jobs in one plan share that container and the attempt workspace.
They run one at a time.

A step may name a local composite action with `uses` instead of `run`.
Planning a snapshot reads `action.yml` (or `action.yaml`) from that
snapshot and stores the inner `run` steps plus a digest of the parsed
action. `run_job` executes the stored steps in the same container and
workspace. It does not read the action file again and it does not fetch a
remote ref. `./path` and `$/path` are the accepted forms. JavaScript and
Docker actions are rejected. Nested `uses` is rejected. Composite `run`
text stays literal, so `${{ github.action_path }}` inside `run` is not
expanded. A whole-string expression is evaluated in a composite step's
`env`, an action output `value`, and the calling step's `with`. Mixed
`${{ }}` stays literal. `github.action_path` and `GITHUB_ACTION_PATH` exist
only while those inner steps run. Inner `GITHUB_OUTPUT` stays inside the
action and is published only through the action's outputs, on the calling
step id. Inner `GITHUB_ENV`, `GITHUB_PATH`, and masks apply to the rest of
the job. Each inner exec uses the calling step's `timeout-minutes` and the
job deadline; the step timeout is not a shared budget across inner steps.
This is not a GitHub-equivalence claim.
https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax

The job deadline is `timeout-minutes` on the plan (default 360). It starts
when `run_job` starts and covers setup and steps. Reaching it stops the owned
container and returns status `cancelled`. A step `timeout-minutes` fails that
step when it is shorter than the time left in the job. Stopping uses the
documented cancellation grace: SIGINT, 7500 ms, SIGTERM, 2500 ms, then the
container is removed. An optional owner reserves the container name before
create so a caller can stop that container with the same grace. The owner
can record that name before create and reject a name that belongs to an
unresolved attempt. A timed-out `docker exec` does not keep partial stdout
or stderr.
"""

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from .commands import ENV_NAME as _ENV_NAME
from .commands import mask_text, parse_env, parse_output, parse_path, process_stdout
from .expr import ExprError, evaluate, job_is_enabled, mentions_context, step_is_enabled
from .plan import DEFAULT_JOB_TIMEOUT_MINUTES, MAX_JOB_TIMEOUT_MINUTES, MAX_STEP_TIMEOUT_MINUTES
from .protocol import canonical
from .verify import VerifyError, verify_snapshot

# https://docs.github.com/en/actions/reference/workflow-cancellation-reference
# The runner sends SIGINT, waits 7500 ms, sends SIGTERM, waits 2500 ms, then
# kills the process tree. A job still marked cancelled after 5 minutes is
# forcibly terminated. These waits are maximums; polling returns earlier.
_CANCEL_SIGINT_SECONDS = 7.5
_CANCEL_SIGTERM_SECONDS = 2.5

# 8 random bytes, hex-encoded. Only this name is removed for an attempt.
CONTAINER_NAME = re.compile(r"^rookrunner-[0-9a-f]{16}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_FILE_MODES = {"100644": 0o644, "100755": 0o755}
# jobs.<job_id>.outputs: 1 MB per job and 50 MB for the workflow run.
# The syntax page does not define MB as 1000 or 1024. These checks use
# 1024-based bytes of UTF-16-LE, the encoding named on that page.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
_OUTPUT_JOB_BYTES = 1024 * 1024
_OUTPUT_RUN_BYTES = 50 * 1024 * 1024
_INSPECT = '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}}}'


class _JobRuntime:
    """Env, PATH prefixes, step outputs, and masks for one job.

    The next job gets a new instance. The container and workspace are shared.
    """

    def __init__(self):
        self.env = {}
        self.paths = []
        self.outputs = {}
        self.masks = []
        self.base_path = None


def _reserved_env(script_name):
    """Names a step cannot replace. The three command files are per step."""

    return {
        "GITHUB_WORKSPACE": "/workspace",
        "ROOKRUNNER_EVENT": "/run/rookrunner/event.json",
        "GITHUB_ENV": f"/run/rookrunner-cmd/{script_name}-env",
        "GITHUB_OUTPUT": f"/run/rookrunner-cmd/{script_name}-output",
        "GITHUB_PATH": f"/run/rookrunner-cmd/{script_name}-path",
    }


class RunError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class _Timeout(Exception):
    pass


class _JobDeadline(Exception):
    def __init__(self, step=None):
        self.step = step


class _StepTimedOut(Exception):
    def __init__(self, step):
        self.step = step


def _setup(message, exc=None):
    error = RunError("SETUP_FAILED", message)
    if exc is None:
        raise error
    raise error from exc


def _invoke(docker, args, timeout):
    try:
        completed = subprocess.run(
            [docker, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        _setup("Docker is missing", exc)
    except subprocess.TimeoutExpired as exc:
        raise _Timeout from exc
    return completed.returncode, completed.stdout, completed.stderr


def _pinned(image):
    if not isinstance(image, str):
        _setup("image is not pinned by digest")
    if _DIGEST.fullmatch(image):
        return image, image
    if _REFERENCE.fullmatch(image):
        return image, image.rsplit("@", 1)[1]
    _setup("image is not pinned by digest")


def _invoke_within(docker, args, cap, deadline):
    """Run one Docker call. A timeout at the job deadline raises `_JobDeadline`.

    Equal remaining time and `cap` counts as the job bound. A timeout caused
    by `cap` alone stays `_Timeout`.
    """

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _JobDeadline()
    job_bound = remaining <= cap
    try:
        return _invoke(docker, args, remaining if job_bound else cap)
    except _Timeout:
        if job_bound:
            raise _JobDeadline() from None
        raise


def _resolve_image(docker, reference, digest, deadline):
    code, stdout = _inspect(docker, reference, deadline)
    if code != 0 and reference != digest:
        pull_code, _stdout, _stderr = _invoke_within(docker, ["pull", reference], 300, deadline)
        if pull_code != 0:
            _setup("image digest will not resolve")
        code, stdout = _inspect(docker, reference, deadline)
    if code != 0:
        _setup("image digest will not resolve")
    try:
        info = json.loads(stdout.decode("utf-8"))
        image_id = info["Id"]
        repo_digests = info["RepoDigests"] or []
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        _setup("image digest will not resolve")
    if not isinstance(image_id, str) or not isinstance(repo_digests, list):
        _setup("image digest will not resolve")
    matches_id = image_id == digest
    matches_repo = any(
        isinstance(item, str) and item.endswith("@" + digest) for item in repo_digests
    )
    if not matches_id and not matches_repo:
        _setup("image digest will not resolve")
    return digest


def _inspect(docker, reference, deadline):
    code, stdout, _stderr = _invoke_within(
        docker, ["image", "inspect", "--format", _INSPECT, reference], 60, deadline
    )
    return code, stdout


def _docker_binary(docker):
    if not isinstance(docker, str) or docker == "" or "\0" in docker:
        _setup("Docker is missing")
    candidate = docker if os.sep in docker else shutil.which(docker)
    if candidate is None:
        _setup("Docker is missing")
    path = Path(candidate)
    if not path.is_file() or not os.access(path, os.X_OK):
        _setup("Docker is missing")
    return str(path)


def _plan_parts(plan):
    if not isinstance(plan, dict) or plan.get("capability_version") != 5:
        _setup("plan is not accepted")
    workflow = plan.get("workflow")
    jobs = plan.get("jobs")
    selected = plan.get("job")
    if not isinstance(workflow, dict) or not isinstance(jobs, list) or not jobs:
        _setup("plan is not accepted")
    if not isinstance(selected, dict) or selected.get("id") != jobs[-1].get("id"):
        _setup("plan is not accepted")
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            _setup("plan is not accepted")
        job_id = job.get("id")
        needs = job.get("needs")
        outputs = job.get("outputs")
        if not isinstance(job_id, str) or job_id == "" or job_id in seen:
            _setup("plan is not accepted")
        if not isinstance(needs, list) or not isinstance(outputs, dict):
            _setup("plan is not accepted")
        if any(not isinstance(item, str) or item not in seen for item in needs):
            _setup("plan is not accepted")
        for key, value in outputs.items():
            if not isinstance(key, str) or key.strip() == "" or "\0" in key:
                _setup("plan is not accepted")
            if not isinstance(value, str) or value.strip() == "" or "\0" in value:
                _setup("plan is not accepted")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            _setup("plan is not accepted")
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("index") != index:
                _setup("plan is not accepted")
            if "uses" in step:
                _accept_composite(step)
            elif not isinstance(step.get("run"), str) or "\0" in step["run"]:
                _setup("plan is not accepted")
        seen.add(job_id)
    return workflow, jobs


def _env_layer(items):
    if not isinstance(items, dict):
        _setup("plan is not accepted")
    merged = {}
    for key, value in items.items():
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
            _setup("environment name is not accepted")
        if not isinstance(value, str) or "\0" in value:
            _setup("environment value is not accepted")
        merged[key] = value
    return merged


def _defaults(body):
    defaults = body.get("defaults")
    if not isinstance(defaults, dict):
        _setup("plan is not accepted")
    shell = defaults.get("shell")
    working = defaults.get("working_directory")
    if shell is not None and not isinstance(shell, str):
        _setup("plan is not accepted")
    if working is not None and not isinstance(working, str):
        _setup("plan is not accepted")
    return shell, working


def _merged_env(workflow, job, step, runtime, script_name, path_value, extra_reserved=None):
    merged = {}
    merged.update(_env_layer(workflow.get("env")))
    merged.update(_env_layer(job.get("env")))
    if runtime is not None:
        merged.update(runtime.env)
    merged.update(_env_layer(step.get("env")))
    if path_value is not None:
        merged["PATH"] = path_value
    reserved = _reserved_env(script_name)
    if extra_reserved:
        reserved.update(extra_reserved)
    merged.update(reserved)
    return [(key, merged[key]) for key in sorted(merged)]


def _accept_composite(step):
    uses = step.get("uses")
    action_path = step.get("action_path")
    digest = step.get("action_digest")
    if not isinstance(uses, str) or uses == "" or "\0" in uses:
        _setup("plan is not accepted")
    if not isinstance(action_path, str) or "\\" in action_path or "\0" in action_path:
        _setup("plan is not accepted")
    if action_path != "":
        relative = PurePosixPath(action_path)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            _setup("plan is not accepted")
    if not isinstance(digest, str) or len(digest) != 64:
        _setup("plan is not accepted")
    if any(char not in "0123456789abcdef" for char in digest):
        _setup("plan is not accepted")
    nested = step.get("steps")
    if not isinstance(nested, list) or not nested:
        _setup("plan is not accepted")
    for label in ("with", "inputs", "outputs", "env"):
        if not isinstance(step.get(label), dict):
            _setup("plan is not accepted")
    for index, inner in enumerate(nested):
        if not isinstance(inner, dict) or inner.get("index") != index:
            _setup("plan is not accepted")
        if not isinstance(inner.get("run"), str) or "\0" in inner["run"]:
            _setup("plan is not accepted")
        shell = inner.get("shell")
        if not isinstance(shell, str) or shell.strip() == "" or "\0" in shell:
            _setup("plan is not accepted")


def _shell_command(shell, bash_ok, script):
    """Return the container argv, or None when the shell is unsupported."""

    if shell is None:
        prefix = ["bash", "-e"] if bash_ok else ["sh", "-e"]
        return [*prefix, script]
    if shell == "bash":
        if bash_ok:
            prefix = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
        else:
            prefix = ["sh", "-e"]
        return [*prefix, script]
    if shell == "sh":
        return ["sh", "-e", script]
    if not isinstance(shell, str) or shell == "":
        return None
    parts = shell.split()
    if parts.count("{0}") != 1 or parts[0] not in {"bash", "sh"}:
        return None
    return [script if part == "{0}" else part for part in parts]


def _chosen_shell(step, job, workflow):
    if step.get("shell") is not None:
        return step.get("shell")
    job_shell, _job_dir = _defaults(job)
    if job_shell is not None:
        return job_shell
    workflow_shell, _workflow_dir = _defaults(workflow)
    return workflow_shell


def _chosen_directory(step, job, workflow):
    if step.get("working_directory") is not None:
        return step.get("working_directory")
    _job_shell, job_dir = _defaults(job)
    if job_dir is not None:
        return job_dir
    _workflow_shell, workflow_dir = _defaults(workflow)
    return workflow_dir


def _working_directory(workspace, relative):
    if relative is None:
        relative = ""
    if not isinstance(relative, str) or "\0" in relative or "\\" in relative:
        return None
    if relative.startswith("/"):
        return None
    parts = []
    for part in PurePosixPath(relative).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    host = Path(workspace).joinpath(*parts)
    try:
        real_host = host.resolve(strict=True)
        real_root = Path(workspace).resolve(strict=True)
        real_host.relative_to(real_root)
    except (OSError, ValueError):
        return None
    if not host.is_dir():
        return None
    if not parts:
        return "/workspace"
    return "/workspace/" + "/".join(parts)


def _sha256(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(fd)


def _verify_workspace(workspace, manifest):
    root = Path(workspace)
    try:
        real_root = root.resolve(strict=True)
    except OSError as exc:
        _setup("workspace failed verification", exc)
    if not root.is_dir() or root.is_symlink():
        _setup("workspace failed verification")
    expected = {}
    for entry in manifest["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            _setup("workspace failed verification")
        expected[entry["path"]] = entry
    found = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            current_path.resolve(strict=True).relative_to(real_root)
        except (OSError, ValueError):
            _setup("workspace failed verification")
        relative = current_path.relative_to(root).as_posix()
        if relative == ".":
            relative = ""
        kept = []
        for name in dirs:
            child = f"{relative}/{name}" if relative else name
            path = current_path / name
            if path.is_symlink():
                found.add(child)
                _check_link(path, expected.get(child))
            else:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            child = f"{relative}/{name}" if relative else name
            found.add(child)
            path = current_path / name
            if path.is_symlink():
                _check_link(path, expected.get(child))
            else:
                _check_file(path, expected.get(child))
    if found != set(expected):
        _setup("workspace failed verification")


def _check_link(path, entry):
    if entry is None or entry.get("kind") != "symlink":
        _setup("workspace failed verification")
    try:
        target = os.readlink(path)
    except OSError as exc:
        _setup("workspace failed verification", exc)
    if target != entry.get("target"):
        _setup("workspace failed verification")


def _check_file(path, entry):
    if entry is None or entry.get("kind") != "file":
        _setup("workspace failed verification")
    if path.is_symlink():
        _setup("workspace failed verification")
    try:
        info = path.lstat()
        digest = _sha256(path)
    except OSError as exc:
        _setup("workspace failed verification", exc)
    if stat.S_IMODE(info.st_mode) != _FILE_MODES.get(entry.get("mode")):
        _setup("workspace failed verification")
    if info.st_size != entry.get("size") or digest != entry.get("sha256"):
        _setup("workspace failed verification")


def _text(value):
    return value.decode("utf-8", "replace")


def _step_result(step, status, exit_code, stdout, stderr, error, job_id=None):
    record = {
        "index": step["index"],
        "id": step.get("id"),
        "name": step.get("name"),
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
    }
    if job_id is not None:
        record["job_id"] = job_id
    return record


def _prepare(snapshot_dir, snapshot_digest, workspace, plan, image, event):
    reference, digest = _pinned(image)
    workflow, jobs = _plan_parts(plan)
    try:
        encoded = (canonical(event) + "\n").encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        _setup("event input is not accepted", exc)
    try:
        manifest = verify_snapshot(snapshot_dir, snapshot_digest)
    except VerifyError as exc:
        _setup("snapshot failed verification", exc)
    workspace = Path(workspace)
    _verify_workspace(workspace, manifest)
    for label in (snapshot_dir, workspace):
        if "," in os.fspath(label) or "\n" in os.fspath(label):
            _setup("workspace path is not accepted")
    return reference, digest, workflow, jobs, encoded, workspace


def _create_args(name, workspace, private, commands, reference):
    return [
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--workdir",
        "/workspace",
        "--entrypoint",
        "sh",
        "--mount",
        f"type=bind,source={workspace},destination=/workspace",
        "--mount",
        f"type=bind,source={private},destination=/run/rookrunner,readonly",
        "--mount",
        f"type=bind,source={commands},destination=/run/rookrunner-cmd",
        reference,
        "-c",
        # PID 1 ignores SIGINT and SIGTERM unless it installs a handler.
        # `sleep` does not, so the cancellation grace would always wait out
        # both periods. This shell exits on those signals and `sleep` is a
        # child that only keeps the container alive across steps.
        "trap 'exit 130' INT TERM; sleep infinity & wait",
    ]


def _job_seconds(job):
    """Return one job's deadline in seconds. An omitted value is 360 minutes."""

    minutes = DEFAULT_JOB_TIMEOUT_MINUTES
    if isinstance(job, dict) and "timeout_minutes" in job:
        minutes = job["timeout_minutes"]
    if type(minutes) is not int or minutes < 1 or minutes > MAX_JOB_TIMEOUT_MINUTES:
        _setup("job timeout is not accepted")
    return minutes * 60


def _step_seconds(step, step_timeout):
    """Return the tighter step ceiling in seconds, or None when neither is set."""

    limits = []
    minutes = step.get("timeout_minutes")
    if minutes is not None:
        if type(minutes) is not int or minutes < 1 or minutes > MAX_STEP_TIMEOUT_MINUTES:
            _setup("step timeout is not accepted")
        limits.append(minutes * 60)
    if step_timeout is not None:
        if (
            type(step_timeout) not in (int, float)
            or not math.isfinite(step_timeout)
            or step_timeout <= 0
        ):
            _setup("step timeout is not accepted")
        limits.append(step_timeout)
    if not limits:
        return None
    return min(limits)


def _exec_limit(remaining, step_seconds):
    """Return `(seconds, job_bound)`.

    The job deadline wins when it is equal to or tighter than the step limit.
    """

    if step_seconds is not None and step_seconds < remaining:
        return step_seconds, False
    return remaining, True


def _container_running(docker, name):
    """Return True, False, or None when no container has that name."""

    try:
        code, stdout, _stderr = _invoke(
            docker, ["inspect", "--format", "{{.State.Running}}", name], 60
        )
    except (RunError, _Timeout):
        return True
    if code != 0:
        return None
    return stdout.strip() == b"true"


def _signal(docker, name, signal):
    try:
        _invoke(docker, ["kill", "--signal", signal, name], 60)
    except (RunError, _Timeout):
        return


def _wait_until_stopped(docker, name, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if _container_running(docker, name) is not True:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))


def _stop_container(docker, name):
    """Stop an owned container with the documented cancellation grace, then remove it.

    The runner sends SIGINT, waits 7500 ms, sends SIGTERM, waits 2500 ms,
    and then kills the process tree. A cancellation still in progress after
    5 minutes is forcibly terminated by the server.
    https://docs.github.com/en/actions/reference/workflow-cancellation-reference

    PID 1 is a shell that exits on SIGINT or SIGTERM. `sleep infinity` is its
    child and only keeps the container alive across steps. Signaling the shell
    stops the container, which ends the step process. This returns as soon as
    the container is stopped. A missing container is already gone. A timed-out
    `docker exec` does not keep the partial stdout or stderr from that call.
    """

    if _container_running(docker, name) is True:
        _signal(docker, name, "INT")
        if not _wait_until_stopped(docker, name, _CANCEL_SIGINT_SECONDS):
            _signal(docker, name, "TERM")
            _wait_until_stopped(docker, name, _CANCEL_SIGTERM_SECONDS)
    if _container_running(docker, name) is None:
        return 0
    try:
        code, _stdout, _stderr = _invoke(docker, ["rm", "-f", name], 60)
    except (RunError, _Timeout):
        return 1
    if code == 0 or _container_running(docker, name) is None:
        return 0
    return code


def release_owned_container(name, docker="docker"):
    """Remove one recorded container. Return True only when it is gone.

    This does not list or remove any other container. A missing container is
    already gone. Removal uses the cancellation grace in `_stop_container`.
    """

    if not CONTAINER_NAME.fullmatch(name or ""):
        return False
    try:
        docker_bin = _docker_binary(docker)
        stopped = _stop_container(docker_bin, name) == 0
    except (RunError, _Timeout, OSError):
        return False
    if not stopped:
        return False
    return _container_running(docker_bin, name) is None


def owned_container_present(name, docker="docker"):
    """Return whether the named owned container still exists.

    An invalid name is not inspected. A Docker failure is treated as present
    so a restart does not forget an unresolved container.
    """

    if not CONTAINER_NAME.fullmatch(name or ""):
        return False
    try:
        docker_bin = _docker_binary(docker)
    except RunError:
        return True
    return _container_running(docker_bin, name) is not None


def _container_name(owner):
    """Return a new container name, skipping one the owner still holds."""

    while True:
        name = "rookrunner-" + os.urandom(8).hex()
        if owner is None or not owner.taken(name):
            return name


class ContainerLease:
    """Publish one container name before `docker create`.

    `begin` reserves the name and may record it before create. `created` and
    `closed` wake a caller that is waiting to stop the container. Those
    methods do not take the worker lock. `blocked_names` must not be reused.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.phase = "idle"
        self.name = None
        self.docker = None
        self.cancel = False
        self.reserve = None
        self.blocked_names = frozenset()

    def taken(self, name):
        return name in self.blocked_names

    def begin(self, docker, name):
        with self._lock:
            if self.cancel or self.phase != "idle":
                return False
            if self.reserve is not None:
                self.reserve(name)
            self.docker = docker
            self.name = name
            self.phase = "creating"
            return True

    def created(self):
        with self._lock:
            if self.phase == "creating":
                self.phase = "live"
            self._ready.set()

    def closed(self):
        with self._lock:
            self.phase = "closed"
            self._ready.set()

    def cancelled(self):
        with self._lock:
            return self.cancel

    def snapshot(self):
        with self._lock:
            return self.phase, self.name, self.docker, self.cancel

    def request_cancel(self):
        with self._lock:
            self.cancel = True
            return self.phase, self.name, self.docker

    def wait_until_published(self, seconds):
        self._ready.wait(seconds)

    def removed(self):
        _phase, name, docker, _cancel = self.snapshot()
        if not name or not docker:
            return True
        return _container_running(docker, name) is None

    def stop(self):
        _phase, name, docker, _cancel = self.snapshot()
        if not name or not docker:
            return 0
        return _stop_container(docker, name)


def _write_script(private, script_name, text):
    script = private / script_name
    script.write_bytes(text.encode("utf-8"))
    os.chmod(script, 0o600)


def run_job(
    snapshot_dir,
    snapshot_digest,
    workspace,
    plan,
    image,
    event,
    *,
    docker="docker",
    step_timeout=None,
    owner=None,
):
    """Run `plan` in one container identified by `image`.

    Setup failures raise RunError with kind SETUP_FAILED. They are not a step
    result and they are not exit code 0. A returned result has status
    succeeded and exit_code 0 only when every step that ran exited 0. A step
    whose `if` is false is skipped and does not run. A failed step does not
    run later steps whose condition is false, and it does not stop a later
    step whose condition is true. The result names the first failed step, its
    exit code, and the image digest.

    The first job's deadline is that job's `timeout_minutes` (default 360)
    measured from the start of this call. Each later job gets its own
    deadline when it starts. Reaching a deadline returns status cancelled and
    does not start later jobs. A needed job that failed or was skipped skips
    a dependent job unless that job's `if` is true. A step timeout stops the
    container and does not start later jobs. `owner` reserves the name before
    create. If that owner is already cancelled, this does not start the
    container.
    """

    started = time.monotonic()
    reference, digest, workflow, jobs, event_bytes, workspace = _prepare(
        snapshot_dir, snapshot_digest, workspace, plan, image, event
    )
    deadline = started + _job_seconds(jobs[0])
    resolved = digest
    docker_bin = _docker_binary(docker)
    private = Path(tempfile.mkdtemp(prefix="rookrunner-run-"))
    os.chmod(private, 0o700)
    commands = Path(tempfile.mkdtemp(prefix="rookrunner-cmd-"))
    os.chmod(commands, 0o700)
    name = None
    created = False
    graceful = False
    outcome = None
    failure = None
    records = []
    try:
        resolved = _resolve_image(docker_bin, reference, digest, deadline)
        (private / "event.json").write_bytes(event_bytes)
        os.chmod(private / "event.json", 0o600)
        scripts = {}
        ordinal = 0
        for planned in jobs:
            for step in planned["steps"]:
                if "uses" in step:
                    names = []
                    for inner in step["steps"]:
                        script_name = f"step-{ordinal}"
                        ordinal += 1
                        _write_script(private, script_name, inner["run"])
                        names.append(script_name)
                    scripts[(planned["id"], step["index"])] = names
                else:
                    script_name = f"step-{ordinal}"
                    ordinal += 1
                    _write_script(private, script_name, step["run"])
                    scripts[(planned["id"], step["index"])] = script_name
        for label in (private, commands):
            if "," in os.fspath(label) or "\n" in os.fspath(label):
                _setup("workspace path is not accepted")
        name = _container_name(owner)
        if owner is not None and not owner.begin(docker_bin, name):
            _setup("run was cancelled before the container existed")
        code, _stdout, _stderr = _invoke_within(
            docker_bin,
            _create_args(name, workspace, private, commands, reference),
            60,
            deadline,
        )
        if code != 0:
            _setup("container setup failed")
        created = True
        if owner is not None:
            owner.created()
            if owner.cancelled():
                graceful = True
                _setup("run was cancelled before the container existed")
        code, _stdout, _stderr = _invoke_within(docker_bin, ["start", name], 60, deadline)
        if code != 0:
            _setup("container setup failed")
        probe, _stdout, _stderr = _invoke_within(
            docker_bin, ["exec", name, "bash", "-c", "exit 0"], 30, deadline
        )
        bash_ok = probe == 0
        failed = None
        results = {}
        output_bytes = 0
        jobs_by_id = {planned["id"]: planned for planned in jobs}
        for job_index, job in enumerate(jobs):
            if job_index:
                deadline = time.monotonic() + _job_seconds(job)
            if owner is not None and owner.cancelled():
                graceful = True
                outcome = _cancelled(resolved, reference, records)
                break
            if deadline - time.monotonic() <= 0:
                raise _JobDeadline()
            needs = _needs_context(job, results)
            ancestor_failed = _ancestor_failed(job, jobs_by_id, results)
            cancelled = owner is not None and owner.cancelled()
            try:
                enabled = job_is_enabled(
                    job.get("if"),
                    _expression_values(event, workflow, job, {"env": {}}, [], cancelled, needs),
                    [needs[item]["result"] for item in job["needs"]],
                    ancestor_failed,
                    cancelled,
                )
            except ExprError as exc:
                enabled = None
                message = str(exc)[:512]
            if enabled is None:
                record = _step_result(job["steps"][0], "failed", None, "", "", message, job["id"])
                records.append(record)
                if failed is None:
                    failed = record
                for step in job["steps"][1:]:
                    records.append(_step_result(step, "skipped", None, "", "", None, job["id"]))
                results[job["id"]] = {"result": "failure", "outputs": {}}
                continue
            if not enabled:
                for step in job["steps"]:
                    records.append(_step_result(step, "skipped", None, "", "", None, job["id"]))
                results[job["id"]] = {"result": "skipped", "outputs": {}}
                continue
            prior = []
            runtime = _JobRuntime()
            job_failed = False
            for step in job["steps"]:
                if deadline - time.monotonic() <= 0:
                    raise _JobDeadline()
                record = _consider_step(
                    docker_bin,
                    name,
                    step,
                    workflow,
                    job,
                    event,
                    workspace,
                    bash_ok,
                    step_timeout,
                    deadline,
                    prior,
                    owner is not None and owner.cancelled(),
                    needs,
                    scripts[(job["id"], step["index"])],
                    runtime,
                    commands,
                )
                records.append(record)
                prior.append(record)
                if record["status"] == "failed":
                    job_failed = True
                    if failed is None:
                        failed = record
            result_name = "failure" if job_failed else "success"
            produced, output_bytes = _job_outputs(
                job,
                _expression_values(
                    event, workflow, job, {"env": {}}, prior, cancelled, needs, runtime
                ),
                output_bytes,
            )
            results[job["id"]] = {"result": result_name, "outputs": produced}
        else:
            outcome = _outcome(resolved, reference, records, failed)
    except _JobDeadline as exc:
        if exc.step is not None:
            records.append(exc.step)
        graceful = True
        outcome = _cancelled(resolved, reference, records)
    except _StepTimedOut as exc:
        records.append(exc.step)
        graceful = True
        outcome = _outcome(resolved, reference, records, exc.step)
    except Exception as exc:
        failure = exc
    finally:
        if owner is not None:
            owner.closed()
        cleanup_code = 0
        if name is not None and (created or graceful):
            try:
                if graceful:
                    cleanup_code = _stop_container(docker_bin, name)
                else:
                    cleanup_code, _stdout, _stderr = _invoke(docker_bin, ["rm", "-f", name], 60)
                    if cleanup_code != 0 and _container_running(docker_bin, name) is None:
                        cleanup_code = 0
            except (RunError, _Timeout):
                cleanup_code = 1
        shutil.rmtree(private, ignore_errors=True)
        shutil.rmtree(commands, ignore_errors=True)
    if failure is not None:
        raise failure
    if cleanup_code != 0:
        _setup("container cleanup failed")
    return outcome


def _outcome(image_digest, reference, records, failed):
    if failed is None:
        return {
            "image_digest": image_digest,
            "image_reference": reference,
            "status": "succeeded",
            "exit_code": 0,
            "failed_step": None,
            "steps": records,
        }
    return {
        "image_digest": image_digest,
        "image_reference": reference,
        "status": "failed",
        "exit_code": failed["exit_code"],
        "failed_step": {
            "index": failed["index"],
            "id": failed["id"],
            "name": failed["name"],
        },
        "steps": records,
    }


def _cancelled(image_digest, reference, records):
    return {
        "image_digest": image_digest,
        "image_reference": reference,
        "status": "cancelled",
        "exit_code": None,
        "failed_step": None,
        "steps": records,
    }


def _expression_values(
    event,
    workflow,
    job,
    step,
    prior,
    cancelled,
    needs=None,
    runtime=None,
    *,
    inputs=None,
    action_path=None,
    output_map=None,
):
    """Contexts for one step `if`. Missing properties stay missing.

    `env` is the workflow env, the job env, `GITHUB_ENV` from earlier steps
    in this job, then this step's env. Values that contain `${{ }}` are not
    expanded. `steps.<id>.outputs` is what earlier steps wrote to
    `GITHUB_OUTPUT`. The command-file paths are not part of this map.
    `inputs` and `github.action_path` stay empty unless the caller is inside
    a composite action. `output_map` supplies inner-step outputs so they are
    not stored as workflow step outputs.
    """

    env = {}
    env.update(_env_layer(workflow.get("env")))
    env.update(_env_layer(job.get("env")))
    if runtime is not None:
        env.update(runtime.env)
    env.update(_env_layer(step.get("env")))
    steps = {}
    for record in prior:
        step_id = record.get("id")
        conclusion = _conclusion(record.get("status"))
        if not isinstance(step_id, str) or step_id == "" or conclusion is None:
            continue
        if output_map is not None:
            outputs = dict(output_map.get(step_id, {}))
        elif runtime is not None:
            outputs = dict(runtime.outputs.get(step_id, {}))
        else:
            outputs = {}
        steps[step_id] = {"outcome": conclusion, "conclusion": conclusion, "outputs": outputs}
    if cancelled or any(record.get("status") == "failed" for record in prior):
        job_status = "cancelled" if cancelled else "failure"
    else:
        job_status = "success"
    github = {"event": _event_value(event)}
    if isinstance(action_path, str):
        github["action_path"] = action_path
    return {
        "github": github,
        "needs": needs if isinstance(needs, dict) else {},
        "strategy": {},
        "matrix": {},
        "job": {"status": job_status},
        "runner": {"os": "Linux"},
        "env": env,
        "vars": {},
        "steps": steps,
        "inputs": inputs if isinstance(inputs, dict) else {},
    }


def _event_value(event):
    if isinstance(event, (dict, list, str, bool)) or event is None:
        return event
    if isinstance(event, int) and not isinstance(event, bool):
        return event
    if isinstance(event, float) and math.isfinite(event):
        return event
    return ""


def _conclusion(status):
    if status == "succeeded":
        return "success"
    if status == "failed":
        return "failure"
    if status == "skipped":
        return "skipped"
    return None


def _needs_context(job, results):
    context = {}
    for need in job["needs"]:
        item = results.get(need) or {"result": "skipped", "outputs": {}}
        context[need] = {"result": item["result"], "outputs": dict(item["outputs"])}
    return context


def _ancestor_failed(job, jobs_by_id, results):
    seen = set()
    stack = list(job["needs"])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        item = results.get(current)
        if item is not None and item["result"] == "failure":
            return True
        parent = jobs_by_id.get(current)
        if parent is not None:
            stack.extend(parent["needs"])
    return False


def _output_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    return None


def _job_outputs(job, values, used):
    """Copy job outputs. An expression that reads `secrets` is not copied.

    GitHub skips an output whose value contains a registered secret. This
    subset has no secret store, so an output expression that names `secrets`
    is omitted instead of evaluated. A value past the documented 1 MB job
    total or 50 MB run total is also omitted. Those sizes are 1024-based
    UTF-16-LE bytes. The syntax page says 1 MB and 50 MB and does not define
    MB.
    """

    produced = {}
    job_used = 0
    for _name, source in job.get("outputs", {}).items():
        if mentions_context(source, "secrets"):
            continue
        try:
            text = _output_text(evaluate(source, values))
        except ExprError:
            continue
        if text is None or "\0" in text:
            continue
        size = len(text.encode("utf-16-le"))
        if job_used + size > _OUTPUT_JOB_BYTES or used + size > _OUTPUT_RUN_BYTES:
            continue
        produced[_name] = text
        job_used += size
        used += size
    return produced, used


def _consider_step(
    docker,
    name,
    step,
    workflow,
    job,
    event,
    workspace,
    bash_ok,
    step_timeout,
    deadline,
    prior,
    cancelled,
    needs,
    script_name,
    runtime,
    commands,
):
    job_id = job.get("id")
    try:
        enabled = step_is_enabled(
            step.get("if"),
            _expression_values(event, workflow, job, step, prior, cancelled, needs, runtime),
            prior,
            cancelled,
        )
    except ExprError as exc:
        return _step_result(step, "failed", None, "", "", str(exc)[:512], job_id)
    if not enabled:
        return _step_result(step, "skipped", None, "", "", None, job_id)
    if "uses" in step:
        return _run_composite(
            docker,
            name,
            step,
            workflow,
            job,
            event,
            workspace,
            bash_ok,
            step_timeout,
            deadline,
            prior,
            cancelled,
            needs,
            script_name,
            runtime,
            commands,
            job_id,
        )
    return _run_step(
        docker,
        name,
        step,
        workflow,
        job,
        workspace,
        bash_ok,
        step_timeout,
        deadline,
        script_name,
        job_id,
        runtime,
        commands,
    )


def _is_whole_expression(text):
    stripped = text.strip()
    return stripped.startswith("${{") and stripped.endswith("}}") and "${{" not in stripped[3:-2]


def _render_expr(text, values):
    """Evaluate one whole-string expression. Any other string stays literal."""

    if not isinstance(text, str) or "\0" in text:
        _setup("plan is not accepted")
    if not _is_whole_expression(text):
        return text
    rendered = _output_text(evaluate(text, values))
    if rendered is None or "\0" in rendered:
        return ""
    return rendered


def _action_container_path(action_path):
    if action_path == "":
        return "/workspace"
    relative = PurePosixPath(action_path)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        _setup("plan is not accepted")
    return "/workspace/" + action_path


def _run_composite(
    docker,
    name,
    step,
    workflow,
    job,
    event,
    workspace,
    bash_ok,
    step_timeout,
    deadline,
    prior,
    cancelled,
    needs,
    script_names,
    runtime,
    commands,
    job_id,
):
    """Run the inlined composite steps as one workflow step.

    Each inner exec uses the calling step's timeout and the job deadline.
    That timeout is not divided across the inner steps.
    """

    if not isinstance(script_names, list) or len(script_names) != len(step["steps"]):
        _setup("plan is not accepted")
    try:
        caller_values = _expression_values(
            event, workflow, job, step, prior, cancelled, needs, runtime
        )
        resolved = {}
        for key, raw in step.get("with", {}).items():
            resolved[key] = _render_expr(raw, caller_values)
    except ExprError as exc:
        return _step_result(step, "failed", None, "", "", str(exc)[:512], job_id)
    spec = step.get("inputs")
    if not isinstance(spec, dict):
        _setup("plan is not accepted")
    inputs = {}
    deprecations = []
    for key, item in spec.items():
        if not isinstance(item, dict):
            _setup("plan is not accepted")
        if key in resolved:
            inputs[key] = resolved[key]
            message = item.get("deprecation_message")
            if isinstance(message, str) and message != "":
                deprecations.append(message)
        elif isinstance(item.get("default"), str):
            inputs[key] = item["default"]
        else:
            inputs[key] = ""
    for key in resolved:
        if key not in spec:
            _setup("plan is not accepted")
    action_path = _action_container_path(step.get("action_path", ""))
    calling_env = step.get("env") if isinstance(step.get("env"), dict) else {}
    local_outputs = {}
    inner_prior = []
    stdout_parts = []
    stderr_parts = []
    failed = None
    if deprecations:
        stdout_parts.append(
            mask_text("".join(f"{message}\n" for message in deprecations), runtime.masks)
        )
    for inner, script_name in zip(step["steps"], script_names):
        if deadline - time.monotonic() <= 0:
            raise _JobDeadline(_step_result(step, "failed", None, "", "", "job timed out", job_id))
        base_values = _expression_values(
            event,
            workflow,
            job,
            {"env": calling_env},
            inner_prior,
            cancelled,
            needs,
            runtime,
            inputs=inputs,
            action_path=action_path,
            output_map=local_outputs,
        )
        try:
            evaluated_env = {}
            for key, raw in (inner.get("env") or {}).items():
                evaluated_env[key] = _render_expr(raw, base_values)
        except ExprError as exc:
            record = _step_result(inner, "failed", None, "", "", str(exc)[:512], job_id)
            inner_prior.append(record)
            if failed is None:
                failed = record
            continue
        visible_env = {}
        visible_env.update(_env_layer(calling_env))
        visible_env.update(evaluated_env)
        if_values = _expression_values(
            event,
            workflow,
            job,
            {"env": visible_env},
            inner_prior,
            cancelled,
            needs,
            runtime,
            inputs=inputs,
            action_path=action_path,
            output_map=local_outputs,
        )
        try:
            enabled = step_is_enabled(inner.get("if"), if_values, inner_prior, cancelled)
        except ExprError as exc:
            record = _step_result(inner, "failed", None, "", "", str(exc)[:512], job_id)
            inner_prior.append(record)
            if failed is None:
                failed = record
            continue
        if not enabled:
            inner_prior.append(_step_result(inner, "skipped", None, "", "", None, job_id))
            continue
        runnable = dict(inner)
        runnable["env"] = visible_env
        try:
            record = _run_step(
                docker,
                name,
                runnable,
                workflow,
                job,
                workspace,
                bash_ok,
                step_timeout,
                deadline,
                script_name,
                job_id,
                runtime,
                commands,
                timeout_step=step,
                extra_reserved={"GITHUB_ACTION_PATH": action_path},
                output_map=local_outputs,
            )
        except _StepTimedOut as exc:
            message = "step timed out"
            if exc.step is not None and exc.step.get("error"):
                message = exc.step["error"]
            raise _StepTimedOut(
                _step_result(step, "failed", None, "", "", message, job_id)
            ) from None
        except _JobDeadline as exc:
            message = "job timed out"
            if exc.step is not None and exc.step.get("error"):
                message = exc.step["error"]
            raise _JobDeadline(
                _step_result(step, "failed", None, "", "", message, job_id)
            ) from None
        inner_prior.append(record)
        stdout_parts.append(record["stdout"])
        stderr_parts.append(record["stderr"])
        if record["status"] == "failed" and failed is None:
            failed = record
    produced = _action_outputs(
        step,
        event,
        workflow,
        job,
        calling_env,
        inner_prior,
        cancelled,
        needs,
        runtime,
        inputs,
        action_path,
        local_outputs,
    )
    calling_id = step.get("id")
    if isinstance(calling_id, str) and calling_id:
        runtime.outputs[calling_id] = produced
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if failed is None:
        return _step_result(step, "succeeded", 0, stdout, stderr, None, job_id)
    return _step_result(
        step,
        "failed",
        failed.get("exit_code"),
        stdout,
        stderr,
        failed.get("error"),
        job_id,
    )


def _action_outputs(
    step,
    event,
    workflow,
    job,
    calling_env,
    inner_prior,
    cancelled,
    needs,
    runtime,
    inputs,
    action_path,
    local_outputs,
):
    values = _expression_values(
        event,
        workflow,
        job,
        {"env": calling_env},
        inner_prior,
        cancelled,
        needs,
        runtime,
        inputs=inputs,
        action_path=action_path,
        output_map=local_outputs,
    )
    produced = {}
    outputs = step.get("outputs")
    if not isinstance(outputs, dict):
        _setup("plan is not accepted")
    for name, spec in outputs.items():
        if not isinstance(spec, dict):
            _setup("plan is not accepted")
        raw = spec.get("value", "")
        if not isinstance(raw, str):
            raw = ""
        if _is_whole_expression(raw):
            try:
                rendered = _output_text(evaluate(raw, values))
            except ExprError:
                rendered = ""
            if rendered is None or "\0" in rendered:
                rendered = ""
        else:
            rendered = raw
        produced[name] = rendered
    return produced


def _run_step(
    docker,
    name,
    step,
    workflow,
    job,
    workspace,
    bash_ok,
    step_timeout,
    deadline,
    script_name,
    job_id,
    runtime,
    commands,
    timeout_step=None,
    extra_reserved=None,
    output_map=None,
):
    shell = _chosen_shell(step, job, workflow)
    relative = _chosen_directory(step, job, workflow)
    container_script = f"/run/rookrunner/{script_name}"
    command = _shell_command(shell, bash_ok, container_script)
    workdir = _working_directory(workspace, relative)
    if command is None:
        return _step_result(step, "failed", None, "", "", "shell is unsupported", job_id)
    if workdir is None:
        return _step_result(
            step,
            "failed",
            None,
            "",
            "",
            "working-directory is not inside the workspace",
            job_id,
        )
    files = _command_files(commands, script_name)
    try:
        for path in files.values():
            path.write_bytes(b"")
            os.chmod(path, 0o644)
    except OSError as exc:
        _setup("container setup failed", exc)
    path_value = _path_overlay(docker, name, step, runtime, deadline)
    env_args = []
    for key, value in _merged_env(
        workflow, job, step, runtime, script_name, path_value, extra_reserved
    ):
        env_args.extend(["--env", f"{key}={value}"])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _JobDeadline()
    bounded = step if timeout_step is None else timeout_step
    limit, job_bound = _exec_limit(remaining, _step_seconds(bounded, step_timeout))
    try:
        code, stdout, stderr = _invoke(
            docker,
            ["exec", "--workdir", workdir, *env_args, name, *command],
            limit,
        )
    except _Timeout:
        # The docker client is gone. Partial stdout and stderr from that call
        # are discarded. Command files from this step are not applied.
        message = "job timed out" if job_bound else "step timed out"
        record = _step_result(step, "failed", None, "", "", message, job_id)
        if job_bound:
            raise _JobDeadline(record) from None
        raise _StepTimedOut(record) from None
    stdout_text = _text(stdout)
    stderr_text = _text(stderr)
    logged = process_stdout(stdout_text, runtime.masks)
    logged_err = mask_text(stderr_text, runtime.masks)
    _apply_command_files(runtime, step, files, output_map)
    status = "succeeded" if code == 0 else "failed"
    return _step_result(step, status, code, logged, logged_err, None, job_id)


def _command_files(commands, script_name):
    return {
        "env": commands / f"{script_name}-env",
        "output": commands / f"{script_name}-output",
        "path": commands / f"{script_name}-path",
    }


def _path_overlay(docker, name, step, runtime, deadline):
    """Prepend `GITHUB_PATH` entries. An empty file does not replace `PATH`."""

    if runtime is None or not runtime.paths:
        return None
    raw = step.get("env")
    if isinstance(raw, dict) and "PATH" in raw:
        base = raw["PATH"]
        if not isinstance(base, str) or "\0" in base:
            _setup("environment value is not accepted")
    else:
        if runtime.base_path is None:
            runtime.base_path = _probe_path(docker, name, deadline)
        base = runtime.base_path
    front = ":".join(reversed(runtime.paths))
    if base:
        return front + ":" + base
    return front


def _probe_path(docker, name, deadline):
    """Read the container `PATH`. A failed probe uses an empty base."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        code, stdout, _stderr = _invoke(
            docker,
            ["exec", name, "sh", "-c", 'printf %s "$PATH"'],
            min(30, remaining),
        )
    except _Timeout:
        return ""
    if code != 0:
        return ""
    text = _text(stdout)
    if "\0" in text:
        return ""
    return text


def _apply_command_files(runtime, step, files, output_map=None):
    """Apply command files after exec. A missing or non-UTF-8 file is ignored.

    `output_map` keeps a composite step's `GITHUB_OUTPUT` off the workflow
    step map. Env, PATH, and masks still update the job.
    """

    env_text = _read_utf8(files["env"])
    if env_text is not None:
        runtime.env.update(parse_env(env_text))
    output_text = _read_utf8(files["output"])
    if output_text is not None:
        step_id = step.get("id")
        if isinstance(step_id, str) and step_id:
            parsed = parse_output(output_text)
            if output_map is not None:
                output_map[step_id] = parsed
            else:
                runtime.outputs[step_id] = parsed
    path_text = _read_utf8(files["path"])
    if path_text is not None:
        runtime.paths.extend(parse_path(path_text))


def _read_utf8(path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None
