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
delivery. `GITHUB_*` and `RUNNER_*` names cannot be overwritten. `CI` and
`HOME` can. When the Docker socket is mounted, `TMPDIR`, `TEMP`, and
`TMP` can too. `GITHUB_ACTIONS` stays unset. `runs-on` does not select an
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

The container is created on Docker network `bridge` by default, so the job
can reach the public internet. GitHub-hosted runners have that access by
default
(https://docs.github.com/en/actions/concepts/runners/private-networking).
Pass network `none` to turn it off. Other network names are rejected. The
Docker socket stays unmounted unless the caller sets `docker_socket`. That
mount gives the job the engine socket this process already uses, at
`/var/run/docker.sock` inside the container. The job keeps the caller uid.
It is added to the socket's group and to group 0 so it can open a mode
0660 socket, including one the engine presents as owned by root. The job
image must already contain the Docker client. This module does not install
one. When the socket is mounted, a private Docker volume is mounted
into the job at that volume's mountpoint. `TMPDIR`, `TEMP`, and `TMP`
default to it, so a nested client creates bind sources the host engine
can see. A later env layer can replace those three. The volume is
removed with the job container. A bind of the host's `/tmp` is not
used: on that shared folder, chmod of a Unix socket fails and Git does
not see a repository it just created. The host `/tmp` is not mounted.
GitHub requires Docker to be installed
and the service running for container-dependent jobs on a self-hosted
runner
(https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners).
Host credential directories are not mounted. The container is not
privileged. This is not a private-network or egress-policy implementation.
A job may declare service containers. Each image must be digest-pinned.
GitHub allows a registry name or a tag
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
This engine does not pull a floating tag. For that job only, the runner
creates a user-defined bridge network and the service label is the
hostname. Containers on that network reach each other without a published
host port. `credentials`, `volumes`, `options`, and `ports` are rejected.
GitHub warns that `--network` is not supported in service `options`; this
engine does not accept the key. The service container does not receive the
engine socket, the workspace, or privilege. It is ready when it is running,
or healthy when the image defines a health check. If it exits, or it is not
ready before the job deadline, setup fails and steps do not run. The wait
uses that deadline. There is no separate health timeout. Cancel and restart
remove the service containers and the network. `network none` does not
start them. `job.services` is not a context here, so a host port is not
recorded. This is not a GitHub-equivalence claim.
A job matrix runs in this same container, one combination at a time, on the
same attempt workspace. `strategy.fail-fast` defaults to true and skips
later combinations after one fails. `strategy.max-parallel` is recorded and
is not a second container count: this worker never runs two combinations at
once. Job `if` is evaluated before that expansion. A matrix job does not
publish outputs to `needs`. A job may call a local reusable workflow. The
called file was read from the snapshot while planning. Every job in that
workflow runs here, one at a time, on this workspace. Caller workflow env
is not copied in. `with` becomes the called workflow's `inputs` context
and is not exported as environment variables. An omitted optional input is
false, 0, or an empty string
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
Secrets are not passed, and `github.token` is not created. GitHub passes
`github.token` into a called workflow. This subset does not. Step `if` and
job `if` are evaluated.
Expressions in workflow `run`, workflow and step `env`, and `name` stay
literal text. `github.event` is the caller event. `github.workspace`,
`github.job`, `github.workflow`, `github.event_path`, and, when the caller
sends one, `github.event_name` are set. `github.sha` is the manifest
`base_commit` only when the capture is clean, `included` is empty, and
that value is a commit id. Other `github` properties stay unset. Nothing
is read from host Git configuration. `runner.os` is `Linux`. `runner.arch`
comes from the image platform. `runner.environment` is `self-hosted`.
`runner.temp` and `runner.tool_cache` are the attempt directories mounted
at `/github/runner-temp` and `/github/tool-cache`. `HOME` is `/github/home`.
`CI` is `true`. Jobs in one plan share that container and the attempt
workspace. They run one at a time. `RUNNER_TEMP` is emptied at the start
of each job. `HOME` and `RUNNER_TOOL_CACHE` are not.
An optional Node 24 directory is mounted read-only at `/opt/node24`.
It is not added to `PATH`. This module does not download Node.

A step may name a local composite action with `uses` instead of `run`.
Planning a snapshot reads `action.yml` (or `action.yaml`) from that
snapshot and stores the inner `run` steps plus a digest of the parsed
action. `run_job` executes the stored steps in the same container and
workspace. It does not read the action file again and it does not fetch a
remote ref. `./path` and `$/path` are the accepted forms. A remote
``node24`` action with ``main`` runs from a copy mounted at ``/actions``.
Its ``post`` runs after the job's main steps, in reverse order, when
that main ran. An omitted ``post-if`` is ``always()``. ``node20``,
``pre``, and Docker actions are rejected. A caller cancel or a job
deadline does not run ``post``. Nested
``uses`` is rejected. Composite `run`
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

`uses: actions/checkout@v4` and a full 40-character lowercase SHA pin of
`actions/checkout` are an owned checkout of the files already in the
workspace. The plan stores that `uses` string and `checkout` as
`captured`. The SHA is not fetched and is not verified. The step does not
start a process, does not modify the workspace, does not create `.git`,
does not delete one that materialize already wrote, does not read
`git.json` or the object store, and does not contact a
network. It succeeds with
exit code 0 and publishes no outputs. `clean: false` and
`persist-credentials: false` are the only accepted `with` values. Omitting
either key does not mean the upstream default of true
(https://github.com/actions/checkout). This is not a GitHub-equivalence
claim.

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

import contextvars
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
from .node24 import BINARY as _NODE24_BINARY
from .node24 import inspect_node24, node_version
from .commands import mask_text, parse_env, parse_output, parse_path, process_stdout
from .expr import ExprError, evaluate, job_is_enabled, mentions_context, step_is_enabled
from .plan import (
    CAPABILITY_VERSION,
    DEFAULT_JOB_TIMEOUT_MINUTES,
    MAX_JOB_TIMEOUT_MINUTES,
    MAX_STEP_TIMEOUT_MINUTES,
    owned_checkout_uses,
)
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
_INSPECT = (
    '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},'
    '"Os":{{json .Os}},"Architecture":{{json .Architecture}}}'
)
# Image platforms from the dogfood design. Any other pair fails setup.
# https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#runner-context
_RUNNER_ARCH = {
    ("linux", "amd64"): "X64",
    ("linux", "arm64"): "ARM64",
    ("linux", "386"): "X86",
    ("linux", "arm"): "ARM",
}
_EVENT_FILE = "/run/rookrunner/event.json"
_HOME = "/github/home"
_RUNNER_TEMP = "/github/runner-temp"
_TOOL_CACHE = "/github/tool-cache"
_ATTEMPT = contextvars.ContextVar("rookrunner_attempt", default=None)
_WORKFLOW_PATH = contextvars.ContextVar("rookrunner_workflow_path", default=None)
# GitHub-hosted runners have access to the public internet by default.
# https://docs.github.com/en/actions/concepts/runners/private-networking
# `bridge` is Docker's default outbound network. `none` turns that off.
# Other names, including `host`, are rejected.
DEFAULT_NETWORK = "bridge"
_CALLED_WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
_NETWORKS = {DEFAULT_NETWORK, "none"}
# Inside the job, the Docker client looks at this socket. GitHub's
# permission error names the same path.
# https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners
_CONTAINER_SOCKET = "/var/run/docker.sock"
# One DNS label. The service id is the hostname on the owned network.
_SERVICE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_NETWORK_NAME = re.compile(r"^rookrunner-net-[0-9a-f]{16}$")


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
        # GITHUB_STATE for one action instance. Later steps do not receive it.
        # The post entry of that same action receives it as STATE_<name>.
        self.action_state = {}
        self.javascript_inputs = {}
        self.javascript_ran = set()


def _reserved_env(script_name):
    """Names a step cannot replace. The three command files are per step."""

    reserved = {
        "GITHUB_WORKSPACE": "/workspace",
        "ROOKRUNNER_EVENT": _EVENT_FILE,
        "GITHUB_ENV": f"/run/rookrunner-cmd/{script_name}-env",
        "GITHUB_OUTPUT": f"/run/rookrunner-cmd/{script_name}-output",
        "GITHUB_PATH": f"/run/rookrunner-cmd/{script_name}-path",
    }
    reserved.update(_protected_defaults())
    return reserved


def _runner_arch(system, architecture):
    """Map an image OS and architecture to `runner.arch`.

    A platform outside the dogfood table is a setup failure. No architecture
    string is invented for it.
    """

    if not isinstance(system, str) or not isinstance(architecture, str):
        _setup("image platform is not accepted")
    arch = _RUNNER_ARCH.get((system, architecture))
    if arch is None:
        _setup("image platform is not accepted")
    return arch


def _commit_sha(manifest):
    """Return `github.sha` for one manifest, or None when it stays unset.

    The value is `base_commit` only for a clean capture with an empty
    `included` list. The synthesized commit id is not used.
    """

    if not isinstance(manifest, dict) or manifest.get("dirty") is not False:
        return None
    included = manifest.get("included")
    if not isinstance(included, list) or included:
        return None
    base = manifest.get("base_commit")
    if not isinstance(base, str) or len(base) not in (40, 64):
        return None
    if any(character not in "0123456789abcdef" for character in base):
        return None
    return base


def _workflow_label(workflow):
    """Return `github.workflow`: the plan name, or else the workflow path."""

    name = workflow.get("name") if isinstance(workflow, dict) else None
    if isinstance(name, str) and name != "":
        return name
    path = _WORKFLOW_PATH.get()
    if isinstance(path, str) and path != "":
        return path
    return None


def _protected_defaults(job=None, workflow=None):
    """`GITHUB_*` and `RUNNER_*` values a later env layer cannot replace."""

    attempt = _ATTEMPT.get()
    if attempt is None:
        return {}
    values = {
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": attempt["arch"],
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_TEMP": _RUNNER_TEMP,
        "RUNNER_TOOL_CACHE": _TOOL_CACHE,
        "GITHUB_WORKSPACE": "/workspace",
        "GITHUB_EVENT_PATH": _EVENT_FILE,
    }
    job_id = job.get("id") if isinstance(job, dict) else None
    if isinstance(job_id, str) and job_id != "":
        values["GITHUB_JOB"] = job_id
    label = _workflow_label(workflow)
    if label is not None:
        values["GITHUB_WORKFLOW"] = label
    if attempt.get("event_name") is not None:
        values["GITHUB_EVENT_NAME"] = attempt["event_name"]
    if attempt.get("sha") is not None:
        values["GITHUB_SHA"] = attempt["sha"]
    return values


def _soft_defaults():
    """Values a later env layer may replace.

    `CI` and `HOME` are set for an attempt. `TMPDIR`, `TEMP`, and `TMP`
    are set only when the job has the shared socket directory.
    """

    attempt = _ATTEMPT.get()
    if attempt is None:
        return {}
    values = {"CI": "true", "HOME": _HOME}
    socket_temp = attempt.get("socket_temp")
    if socket_temp:
        values["TMPDIR"] = socket_temp
        values["TEMP"] = socket_temp
        values["TMP"] = socket_temp
    return values


def _runner_dirs(workspace):
    """Create the attempt directories beside the workspace, mode 0700.

    They are `home`, `runner-temp`, and `tool-cache`. They sit outside the
    workspace, so they are not in the artifact manifest. `usage` counts them
    because they live under the state directory with the attempt.
    """

    root = Path(workspace).parent
    made = {}
    for name in ("home", "runner-temp", "tool-cache"):
        path = root / name
        if path.is_symlink():
            _setup("attempt directory is not accepted")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        if "," in os.fspath(path) or "\n" in os.fspath(path) or "\0" in os.fspath(path):
            _setup("workspace path is not accepted")
        made[name] = path
    return made


def _empty_directory(path):
    """Remove children of `path`. A child that cannot be deleted stays."""

    if path is None or not path.is_dir():
        return
    for child in path.iterdir():
        try:
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        except OSError:
            continue


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
    return digest, _runner_arch(info.get("Os"), info.get("Architecture"))


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


def _accept_jobs(jobs):
    if not isinstance(jobs, list) or not jobs:
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
        if "call" in job:
            _accept_call(job.get("call"))
        else:
            _accept_strategy(job.get("strategy"))
            _accept_services(job.get("services"))
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                _setup("plan is not accepted")
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or step.get("index") != index:
                    _setup("plan is not accepted")
                if step.get("checkout") == "captured":
                    _accept_checkout(step)
                elif step.get("javascript") == "node24":
                    _accept_javascript(step)
                elif "uses" in step:
                    _accept_composite(step)
                elif not isinstance(step.get("run"), str) or "\0" in step["run"]:
                    _setup("plan is not accepted")
        seen.add(job_id)


def _accept_call(call):
    if not isinstance(call, dict):
        _setup("plan is not accepted")
    path = call.get("path")
    if not isinstance(path, str) or not _CALLED_WORKFLOW.fullmatch(path):
        _setup("plan is not accepted")
    workflow = call.get("workflow")
    if not isinstance(workflow, dict):
        _setup("plan is not accepted")
    _env_layer(workflow.get("env"))
    _defaults(workflow)
    inputs = call.get("inputs")
    outputs = call.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, dict):
        _setup("plan is not accepted")
    for item in inputs:
        if not isinstance(item, dict):
            _setup("plan is not accepted")
        name = item.get("name")
        kind = item.get("type")
        if not isinstance(name, str) or name.strip() == "" or "\0" in name:
            _setup("plan is not accepted")
        if kind not in {"boolean", "number", "string"} or type(item.get("required")) is not bool:
            _setup("plan is not accepted")
        _accept_input_slot(item.get("default"))
        _accept_input_slot(item.get("passed"))
    for key, value in outputs.items():
        if not isinstance(key, str) or key.strip() == "" or "\0" in key:
            _setup("plan is not accepted")
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            _setup("plan is not accepted")
    _accept_jobs(call.get("jobs"))


def _accept_input_slot(slot):
    if slot is None:
        return
    if not isinstance(slot, dict) or len(slot) != 1:
        _setup("plan is not accepted")
    if "expression" in slot:
        text = slot["expression"]
        if not isinstance(text, str) or not text.strip() or "\0" in text:
            _setup("plan is not accepted")
        return
    if "literal" not in slot or not _input_literal(slot["literal"]):
        _setup("plan is not accepted")


def _input_literal(value):
    if value is None or type(value) is bool:
        return True
    if isinstance(value, str):
        return "\0" not in value
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _plan_parts(plan):
    # One accepted capability version. A plan from the previous version is
    # rejected here and is not migrated.
    if not isinstance(plan, dict) or plan.get("capability_version") != CAPABILITY_VERSION:
        _setup("plan is not accepted")
    workflow = plan.get("workflow")
    jobs = plan.get("jobs")
    selected = plan.get("job")
    if not isinstance(workflow, dict) or not isinstance(jobs, list) or not jobs:
        _setup("plan is not accepted")
    if not isinstance(selected, dict) or selected.get("id") != jobs[-1].get("id"):
        _setup("plan is not accepted")
    _accept_jobs(jobs)
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
    merged.update(_soft_defaults())
    merged.update(_env_layer(workflow.get("env")))
    merged.update(_env_layer(job.get("env")))
    if runtime is not None:
        merged.update(runtime.env)
    merged.update(_env_layer(step.get("env")))
    if path_value is not None:
        merged["PATH"] = path_value
    reserved = _reserved_env(script_name)
    reserved.update(_protected_defaults(job, workflow))
    if extra_reserved:
        reserved.update(extra_reserved)
    merged.update(reserved)
    return [(key, merged[key]) for key in sorted(merged)]


def _accept_checkout(step):
    """Accept an owned checkout. It has no action path and no inner steps."""

    if not owned_checkout_uses(step.get("uses")) or step.get("checkout") != "captured":
        _setup("plan is not accepted")
    for key in ("run", "action_path", "action_digest", "steps", "inputs", "outputs"):
        if key in step:
            _setup("plan is not accepted")
    if "with" in step:
        raw = step.get("with")
        if not isinstance(raw, dict):
            _setup("plan is not accepted")
        for key, value in raw.items():
            if key not in {"clean", "persist-credentials"} or value is not False:
                _setup("plan is not accepted")
    _env_layer(step.get("env"))
    for key in ("id", "name", "shell", "working_directory", "if"):
        value = step.get(key)
        if value is not None and (not isinstance(value, str) or "\0" in value):
            _setup("plan is not accepted")
    timeout = step.get("timeout_minutes")
    if timeout is not None and type(timeout) is not int:
        _setup("plan is not accepted")


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


def _accept_javascript(step):
    """Accept one remote node24 main. It has no inner steps."""

    if step.get("javascript") != "node24" or "steps" in step:
        _setup("plan is not accepted")
    uses = step.get("uses")
    action_path = step.get("action_path")
    main = step.get("main")
    if not isinstance(uses, str) or uses == "" or "\0" in uses:
        _setup("plan is not accepted")
    if not isinstance(action_path, str) or "\\" in action_path or "\0" in action_path:
        _setup("plan is not accepted")
    if not isinstance(main, str) or main == "" or "\\" in main or "\0" in main:
        _setup("plan is not accepted")
    relative = PurePosixPath(action_path)
    main_path = PurePosixPath(main)
    if (
        relative.is_absolute()
        or main_path.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or ".." in main_path.parts
        or "." in main_path.parts
        or "" in main_path.parts
    ):
        _setup("plan is not accepted")
    if action_path != "" and "" in relative.parts:
        _setup("plan is not accepted")
    for label in ("action_digest", "content_digest"):
        value = step.get(label)
        if not isinstance(value, str) or len(value) != 64:
            _setup("plan is not accepted")
        if any(char not in "0123456789abcdef" for char in value):
            _setup("plan is not accepted")
    commit = step.get("action_commit")
    owner = step.get("action_owner")
    repository = step.get("action_repository")
    if not isinstance(commit, str) or len(commit) != 40:
        _setup("plan is not accepted")
    if any(char not in "0123456789abcdef" for char in commit):
        _setup("plan is not accepted")
    if not isinstance(owner, str) or not isinstance(repository, str):
        _setup("plan is not accepted")
    if owner in {"", ".", ".."} or repository in {"", ".", ".."}:
        _setup("plan is not accepted")
    if any(char in owner + repository for char in "/\\\0"):
        _setup("plan is not accepted")
    for label in ("with", "inputs", "outputs", "env"):
        if not isinstance(step.get(label), dict):
            _setup("plan is not accepted")
    for key, item in step["inputs"].items():
        if not isinstance(key, str) or not isinstance(item, dict):
            _setup("plan is not accepted")
        if not isinstance(item.get("description"), str) or type(item.get("required")) is not bool:
            _setup("plan is not accepted")
        if "default" in item and not isinstance(item.get("default"), str):
            _setup("plan is not accepted")
    for key, item in step["outputs"].items():
        if not isinstance(key, str) or not isinstance(item, dict):
            _setup("plan is not accepted")
        if not isinstance(item.get("description"), str) or "value" in item:
            _setup("plan is not accepted")
    _env_layer(step.get("env"))
    for key in ("id", "name", "shell", "working_directory", "if"):
        value = step.get(key)
        if value is not None and (not isinstance(value, str) or "\0" in value):
            _setup("plan is not accepted")
    timeout = step.get("timeout_minutes")
    if timeout is not None and type(timeout) is not int:
        _setup("plan is not accepted")
    post = step.get("post")
    post_if = step.get("post_if")
    if post is None and post_if is not None:
        _setup("plan is not accepted")
    if post is not None and not isinstance(post, str):
        _setup("plan is not accepted")
    if post_if is not None and (not isinstance(post_if, str) or post_if == "" or "\0" in post_if):
        _setup("plan is not accepted")
    _node_main_path(step)
    if post is not None:
        _node_entry_path(step, post)


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
            # The owned directory is not a captured entry. Do not walk it,
            # and do not follow a symlink of that name.
            if child == ".git":
                continue
            path = current_path / name
            if path.is_symlink():
                found.add(child)
                _check_link(path, expected.get(child))
            else:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            child = f"{relative}/{name}" if relative else name
            if child == ".git":
                continue
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
    return reference, digest, workflow, jobs, encoded, workspace, manifest


def _discover_socket(docker):
    """Return the unix socket for the engine `docker` already uses."""

    host = os.environ.get("DOCKER_HOST") or ""
    if host.startswith("unix://"):
        return host.removeprefix("unix://")
    try:
        code, stdout, _stderr = _invoke(
            docker,
            ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            30,
        )
    except _Timeout:
        _setup("Docker socket is not available")
    if code == 0:
        text = stdout.decode("utf-8", "replace").strip()
        if text.startswith("unix://"):
            return text.removeprefix("unix://")
    return _CONTAINER_SOCKET


def _accept_node24(node24):
    """Return the inspected directory, or None when the caller omits it."""

    if node24 is None:
        return None
    if not isinstance(node24, tuple) or len(node24) != 2 or not isinstance(node24[1], str):
        _setup("node directory is not accepted")
    try:
        inspected = inspect_node24(node24[0])
    except ValueError as exc:
        _setup("node directory is not accepted", exc)
    if inspected["digest"] != node24[1]:
        _setup("node directory changed")
    return inspected


def _read_node_version(docker, name, deadline):
    """Return the version line from the mounted Node 24 binary."""

    try:
        code, stdout, _stderr = _invoke_within(
            docker,
            ["exec", name, _NODE24_BINARY, "--version"],
            30,
            deadline,
        )
    except _Timeout:
        _setup("Node 24 did not start")
    if code != 0:
        _setup("Node 24 did not start")
    try:
        text = stdout.decode("utf-8")
    except UnicodeError as exc:
        _setup("node is not Node 24", exc)
    try:
        return node_version(text)
    except ValueError as exc:
        _setup("node is not Node 24", exc)


def _job_socket(docker, docker_socket):
    """Return `(host path, gid)` or `(None, None)` when the job gets no socket."""

    if docker_socket is False or docker_socket is None:
        return None, None
    if docker_socket is True:
        path = _discover_socket(docker)
    elif isinstance(docker_socket, str) and docker_socket != "":
        path = docker_socket
    else:
        _setup("Docker socket is not available")
    if "," in path or "\n" in path or "\0" in path:
        _setup("Docker socket is not available")
    try:
        info = os.stat(path)
    except OSError:
        _setup("Docker socket is not available")
    if not stat.S_ISSOCK(info.st_mode):
        _setup("Docker socket is not available")
    return path, info.st_gid


def _socket_share(docker, image, deadline):
    """Return `(volume name, mountpoint)` for a socket job.

    A nested client sends host paths. A directory created only inside the
    job does not exist on the host. The volume is mounted at the
    mountpoint Docker already uses for it, and `TMPDIR` points there.
    A bind of host `/tmp` is not used: chmod of a socket fails on that
    shared folder, and Git does not see a repository it just created.
    """

    name = "rookrunner-socket-" + os.urandom(4).hex()
    try:
        code, _stdout, _stderr = _invoke_within(docker, ["volume", "create", name], 30, deadline)
        if code != 0:
            _setup("container setup failed")
        code, stdout, _stderr = _invoke_within(
            docker,
            ["volume", "inspect", "--format", "{{.Mountpoint}}", name],
            30,
            deadline,
        )
        if code != 0:
            _setup("container setup failed")
        try:
            text = stdout.decode("utf-8").strip()
        except UnicodeError as exc:
            _setup("workspace path is not accepted", exc)
        if not text.startswith("/") or "," in text or "\n" in text or "\0" in text or " " in text:
            _setup("workspace path is not accepted")
        owner = f"{os.getuid()}:{os.getgid()}"
        code, _stdout, _stderr = _invoke_within(
            docker,
            [
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                "--mount",
                f"type=volume,source={name},destination=/vol",
                image,
                "-c",
                f"chown {owner} /vol && chmod 700 /vol",
            ],
            60,
            deadline,
        )
        if code != 0:
            _setup("container setup failed")
    except Exception:
        try:
            _invoke(docker, ["volume", "rm", name], 30)
        except Exception:
            pass
        raise
    return name, text


def _plan_uses_node24(jobs):
    for job in jobs:
        for _job, step in _iter_concrete(job, ()):
            if step.get("javascript") == "node24":
                return True
    return False


def _accept_actions(actions, needed):
    """Return the attempt actions directory, or None when the plan has no node24."""

    if not needed:
        return None
    if actions is None:
        _setup("plan is not accepted")
    root = Path(actions)
    if root.is_symlink() or not root.is_dir():
        _setup("plan is not accepted")
    text = os.fspath(root)
    if "," in text or "\n" in text or "\0" in text:
        _setup("workspace path is not accepted")
    return root


def _create_args(
    name,
    workspace,
    private,
    commands,
    reference,
    network,
    socket_path,
    socket_gid,
    runner_dirs,
    node_root,
    actions_root,
    socket_temp=None,
    socket_volume=None,
):
    if network not in _NETWORKS:
        _setup("container network is not accepted")
    args = [
        "create",
        "--name",
        name,
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
    ]
    if socket_path is not None:
        # The engine socket is often mode 0660. The host gid opens a native
        # root:docker socket. Some engines present the same mount as
        # root:root, so group 0 is added as well. The job keeps the caller uid.
        if socket_gid != 0:
            args.extend(["--group-add", str(socket_gid)])
        args.extend(["--group-add", "0"])
    args.extend(
        [
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
            "--mount",
            f"type=bind,source={runner_dirs['home']},destination={_HOME}",
            "--mount",
            f"type=bind,source={runner_dirs['runner-temp']},destination={_RUNNER_TEMP}",
            "--mount",
            f"type=bind,source={runner_dirs['tool-cache']},destination={_TOOL_CACHE}",
        ]
    )
    if node_root is not None:
        args.extend(
            [
                "--mount",
                f"type=bind,source={node_root},destination=/opt/node24,readonly",
            ]
        )
    if actions_root is not None:
        args.extend(
            [
                "--mount",
                f"type=bind,source={actions_root},destination=/actions",
            ]
        )
    if socket_path is not None:
        args.extend(
            [
                "--mount",
                f"type=bind,source={socket_path},destination={_CONTAINER_SOCKET}",
            ]
        )
    if socket_volume is not None and socket_temp is not None:
        args.extend(
            [
                "--mount",
                f"type=volume,source={socket_volume},destination={socket_temp}",
            ]
        )
    args.extend(
        [
            reference,
            "-c",
            # PID 1 ignores SIGINT and SIGTERM unless it installs a handler.
            # `sleep` does not, so the cancellation grace would always wait out
            # both periods. This shell exits on those signals and `sleep` is a
            # child that only keeps the container alive across steps.
            "trap 'exit 130' INT TERM; sleep infinity & wait",
        ]
    )
    return args


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


def _owned_network_name(job_name):
    return "rookrunner-net-" + job_name[len("rookrunner-") :]


def _service_container_name(job_name, index):
    return f"rookrunner-svc-{job_name[len('rookrunner-') :]}-{index}"


def _labeled_service_ids(docker, job_name):
    """Return service container ids labeled for this job, or None on failure."""

    if not CONTAINER_NAME.fullmatch(job_name or ""):
        return []
    try:
        code, stdout, _stderr = _invoke(
            docker,
            [
                "ps",
                "-aq",
                "--filter",
                f"label=rookrunner.owner={job_name}",
                "--format",
                "{{.ID}}",
            ],
            30,
        )
    except (RunError, _Timeout):
        return None
    if code != 0:
        return None
    return [line.strip() for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def _network_exists(docker, network):
    """Return whether the owned network is present. A Docker error is present."""

    if not _NETWORK_NAME.fullmatch(network or ""):
        return False
    try:
        code, _stdout, _stderr = _invoke(
            docker, ["network", "inspect", "--format", "{{.Name}}", network], 30
        )
    except (RunError, _Timeout):
        return True
    return code == 0


def _drop_network(docker, network):
    """Remove the owned network. A network that is already gone is success."""

    if not _NETWORK_NAME.fullmatch(network or ""):
        return False
    try:
        if not _network_exists(docker, network):
            return True
        code, _stdout, _stderr = _invoke(docker, ["network", "rm", network], 60)
    except (RunError, _Timeout):
        return False
    if code == 0:
        return True
    try:
        return not _network_exists(docker, network)
    except (RunError, _Timeout):
        return False


def _retire_services(docker, job_name, service_names, network_name, connected):
    """Stop service containers and remove the owned network. Return 0 on success."""

    code = 0
    for service in service_names:
        if _stop_container(docker, service) != 0:
            code = 1
    labeled = _labeled_service_ids(docker, job_name)
    if labeled is None:
        code = 1
    else:
        for service in labeled:
            if _stop_container(docker, service) != 0:
                code = 1
    if network_name and _network_exists(docker, network_name):
        if connected:
            try:
                _invoke(
                    docker,
                    ["network", "disconnect", "--force", network_name, job_name],
                    60,
                )
            except (RunError, _Timeout):
                code = 1
        if not _drop_network(docker, network_name):
            code = 1
    return code


def _service_create_args(service_name, network_name, service, job_name):
    """Build `docker create` args. No socket, workspace, user, or privilege."""

    args = [
        "create",
        "--name",
        service_name,
        "--network",
        network_name,
        "--network-alias",
        service["id"],
        "--hostname",
        service["id"],
        "--label",
        f"rookrunner.owner={job_name}",
    ]
    for key in sorted(service.get("env") or {}):
        args.extend(["--env", f"{key}={service['env'][key]}"])
    if service.get("entrypoint"):
        args.extend(["--entrypoint", service["entrypoint"]])
    args.append(service["image"])
    if service.get("command"):
        args.extend(service["command"])
    return args


def _wait_until_service_ready(docker, service_name, deadline, owner):
    """Return True when the service is ready. An exit or the deadline fails setup.

    No health check means running is ready. A health check must report healthy.
    The wait stops at the job deadline. That deadline is the existing
    timeout-minutes bound. This is not a separate health timeout.
    """

    confirmed = False
    while True:
        if owner is not None and owner.cancelled():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _setup("service container did not become ready")
        try:
            code, stdout, _stderr = _invoke(
                docker,
                [
                    "inspect",
                    "--format",
                    "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    service_name,
                ],
                min(30, remaining),
            )
        except _Timeout:
            _setup("service container did not become ready")
        if code != 0:
            _setup("service container setup failed")
        parts = stdout.decode("utf-8", "replace").split()
        status = parts[0] if parts else ""
        health = parts[1] if len(parts) > 1 else "none"
        if status in {"exited", "dead"}:
            _setup("service container exited")
        if health == "unhealthy":
            _setup("service container did not become ready")
        if status == "running" and health == "healthy":
            return True
        if status == "running" and health == "none":
            # One extra look catches a process that exits as soon as it starts.
            # The interval matches the cancellation poll in this module.
            if confirmed:
                return True
            confirmed = True
        else:
            confirmed = False
        time.sleep(min(0.2, remaining))


def _start_one_service(docker, job_name, network_name, service, index, deadline, names, owner):
    reference, digest = _pinned(service.get("image"))
    _resolve_image(docker, reference, digest, deadline)
    service_name = _service_container_name(job_name, index)
    code, _stdout, _stderr = _invoke_within(
        docker,
        _service_create_args(service_name, network_name, service, job_name),
        60,
        deadline,
    )
    if code != 0:
        _setup("service container setup failed")
    names.append(service_name)
    if owner is not None:
        owner.note_service(service_name)
    code, _stdout, _stderr = _invoke_within(docker, ["start", service_name], 60, deadline)
    if code != 0:
        _setup("service container setup failed")
    return _wait_until_service_ready(docker, service_name, deadline, owner)


def _create_service_network(docker, network_name, deadline):
    code, _stdout, _stderr = _invoke_within(
        docker,
        ["network", "create", "--driver", "bridge", network_name],
        60,
        deadline,
    )
    if code != 0:
        _setup("service container setup failed")


def release_owned_container(name, docker="docker"):
    """Remove the job container, its service containers, and its network.

    Service containers carry the label `rookrunner.owner` set to the job
    container name. The network is `rookrunner-net-` plus that name's suffix.
    A missing container or network is already gone. A container with a
    different owner is left in place. Removal of a container uses the
    cancellation grace in `_stop_container`.
    """

    if not CONTAINER_NAME.fullmatch(name or ""):
        return False
    try:
        docker_bin = _docker_binary(docker)
        if _retire_services(docker_bin, name, [], _owned_network_name(name), True) != 0:
            return False
        if _stop_container(docker_bin, name) != 0:
            return False
        if _container_running(docker_bin, name) is not None:
            return False
        labeled = _labeled_service_ids(docker_bin, name)
        if labeled is None or labeled:
            return False
        if _network_exists(docker_bin, _owned_network_name(name)):
            return False
    except (RunError, _Timeout, OSError):
        return False
    return True


def owned_container_present(name, docker="docker"):
    """Return whether the job container, a service container, or its network remains.

    An invalid name is not inspected. A Docker failure is treated as present
    so a restart does not forget an unresolved container.
    """

    if not CONTAINER_NAME.fullmatch(name or ""):
        return False
    try:
        docker_bin = _docker_binary(docker)
    except RunError:
        return True
    try:
        if _container_running(docker_bin, name) is not None:
            return True
        labeled = _labeled_service_ids(docker_bin, name)
        if labeled is None or labeled:
            return True
        if _network_exists(docker_bin, _owned_network_name(name)):
            return True
    except (RunError, _Timeout, OSError):
        return True
    return False


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
        self.services = []
        self.network = None
        self._finished = threading.Event()

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
            self._finished.set()

    def note_service(self, name):
        with self._lock:
            self.services.append(name)

    def note_network(self, network):
        with self._lock:
            self.network = network

    def forget_services(self):
        with self._lock:
            self.services = []
            self.network = None

    def owned_extra(self):
        with self._lock:
            return list(self.services), self.network

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

    def wait_closed(self, seconds):
        """Wait until the run thread finishes cleanup, or until `seconds` elapses."""

        deadline = time.monotonic() + seconds
        while self.snapshot()[0] != "closed":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._finished.wait(min(0.2, remaining))

    def removed(self):
        _phase, name, docker, _cancel = self.snapshot()
        services, network = self.owned_extra()
        if not docker:
            return not services and network is None
        if name and _container_running(docker, name) is not None:
            return False
        for service in services:
            if _container_running(docker, service) is not None:
                return False
        if name:
            labeled = _labeled_service_ids(docker, name)
            if labeled is None or labeled:
                return False
            if _network_exists(docker, _owned_network_name(name)):
                return False
        if network and _network_exists(docker, network):
            return False
        return True

    def stop(self):
        _phase, name, docker, _cancel = self.snapshot()
        services, network = self.owned_extra()
        if not docker:
            return 0
        code = 0
        for service in services:
            if _stop_container(docker, service) != 0:
                code = 1
        if name:
            if _retire_services(docker, name, [], network or _owned_network_name(name), True) != 0:
                code = 1
            if _stop_container(docker, name) != 0:
                code = 1
        elif network and not _drop_network(docker, network):
            code = 1
        return code


def _write_script(private, script_name, text):
    script = private / script_name
    script.write_bytes(text.encode("utf-8"))
    os.chmod(script, 0o600)


def _accept_services(services):
    if not isinstance(services, list):
        _setup("plan is not accepted")
    seen = set()
    for item in services:
        if not isinstance(item, dict):
            _setup("plan is not accepted")
        service_id = item.get("id")
        image = item.get("image")
        if (
            not isinstance(service_id, str)
            or not _SERVICE_ID.fullmatch(service_id)
            or service_id in seen
        ):
            _setup("plan is not accepted")
        seen.add(service_id)
        if not isinstance(image, str) or not (
            _DIGEST.fullmatch(image) or _REFERENCE.fullmatch(image)
        ):
            _setup("plan is not accepted")
        _env_layer(item.get("env"))
        if "command" in item:
            command = item.get("command")
            if not isinstance(command, list) or not command:
                _setup("plan is not accepted")
            if any(not isinstance(part, str) or part == "" or "\0" in part for part in command):
                _setup("plan is not accepted")
        if "entrypoint" in item:
            entry = item.get("entrypoint")
            if not isinstance(entry, str) or entry.strip() == "" or "\0" in entry or "\n" in entry:
                _setup("plan is not accepted")


def _accept_strategy(strategy):
    if strategy is None:
        return
    if not isinstance(strategy, dict):
        _setup("plan is not accepted")
    if type(strategy.get("fail_fast")) is not bool:
        _setup("plan is not accepted")
    max_parallel = strategy.get("max_parallel")
    if max_parallel is not None and (type(max_parallel) is not int or max_parallel < 1):
        _setup("plan is not accepted")
    combinations = strategy.get("combinations")
    if combinations is None:
        return
    # Same documented matrix ceiling the planner enforces.
    # https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
    if not isinstance(combinations, list) or len(combinations) > 256:
        _setup("plan is not accepted")
    for combo in combinations:
        if not isinstance(combo, dict) or not _matrix_document(combo):
            _setup("plan is not accepted")


def _matrix_document(value):
    if value is None or type(value) is bool:
        return True
    if isinstance(value, str):
        return "\0" not in value
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_matrix_document(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and key != "" and "\0" not in key and _matrix_document(item)
            for key, item in value.items()
        )
    return False


def _combination_contexts(job):
    """Return `(matrix, strategy)` pairs. No matrix is one pair of empty contexts.

    `max-parallel` limits how many matrix jobs GitHub runs at once. This
    worker has one container, so combinations run one at a time and the
    declared value is only exposed on the strategy context.
    https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
    """

    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return [(None, None)]
    combinations = strategy.get("combinations")
    if combinations is None:
        return [(None, None)]
    total = len(combinations)
    pairs = []
    for index, combo in enumerate(combinations):
        context = {
            "fail-fast": strategy["fail_fast"],
            "job-index": index,
            "job-total": total,
        }
        if strategy.get("max_parallel") is not None:
            context["max-parallel"] = strategy["max_parallel"]
        pairs.append((combo, context))
    return pairs


# An omitted workflow_call input uses the type's documented default.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_callinputs
_TYPE_DEFAULTS = {"boolean": False, "number": 0, "string": ""}


class _CallInputError(Exception):
    pass


def _matches_input(kind, value):
    """Return whether `value` is the declared workflow_call input type.

    A boolean is not a number. A string is not coerced into either type.
    """

    if kind == "boolean":
        return type(value) is bool
    if kind == "number":
        if type(value) is int:
            return True
        return type(value) is float and math.isfinite(value)
    if kind == "string":
        return isinstance(value, str)
    return False


def _resolve_call_inputs(slots, passed_values, default_values_for):
    """Bind explicit `with` values, then defaults.

    A default expression sees those explicit values and does not see another
    omitted input's default. An omitted optional input with no default is
    false, 0, or "".
    """

    explicit = {}
    for item in slots:
        passed = item.get("passed")
        if passed is None:
            continue
        if "literal" in passed:
            value = passed["literal"]
        else:
            value = evaluate(passed["expression"], passed_values)
        if not _matches_input(item["type"], value):
            raise _CallInputError(f"{item['name']} must be a {item['type']}")
        explicit[item["name"]] = value
    default_values = default_values_for(explicit)
    bound = dict(explicit)
    for item in slots:
        if item["name"] in explicit:
            continue
        default = item.get("default")
        if default is None:
            value = _TYPE_DEFAULTS[item["type"]]
        elif "literal" in default:
            value = default["literal"]
        else:
            value = evaluate(default["expression"], default_values)
        if not _matches_input(item["type"], value):
            raise _CallInputError(f"{item['name']} must be a {item['type']}")
        bound[item["name"]] = value
    return bound


def _iter_concrete(job, parent_path):
    """Yield `(job, step)` for every concrete step under `job`."""

    if "call" in job:
        nested = parent_path + (job["id"],)
        for inner in job["call"]["jobs"]:
            yield from _iter_concrete(inner, nested)
        return
    for step in job["steps"]:
        yield job, step


def _write_job_scripts(job_list, path, private, scripts, counter=None):
    """Write step scripts. The key includes the job-id path.

    Two workflows can both use the job id `build`. The path keeps their
    scripts apart.
    """

    if counter is None:
        counter = [0]
    for planned in job_list:
        planned_path = path + (planned["id"],)
        if "call" in planned:
            _write_job_scripts(planned["call"]["jobs"], planned_path, private, scripts, counter)
            continue
        for step in planned["steps"]:
            if step.get("checkout") == "captured":
                scripts[(planned_path, step["index"])] = None
                continue
            if step.get("javascript") == "node24":
                script_name = f"node-{counter[0]}"
                counter[0] += 1
                scripts[(planned_path, step["index"])] = script_name
                continue
            if "uses" in step:
                names = []
                for inner in step["steps"]:
                    script_name = f"step-{counter[0]}"
                    counter[0] += 1
                    _write_script(private, script_name, inner["run"])
                    names.append(script_name)
                scripts[(planned_path, step["index"])] = names
            else:
                script_name = f"step-{counter[0]}"
                counter[0] += 1
                _write_script(private, script_name, step["run"])
                scripts[(planned_path, step["index"])] = script_name


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
    network=DEFAULT_NETWORK,
    docker_socket=False,
    event_name=None,
    node24=None,
    actions=None,
):
    """Run `plan` in one container identified by `image`.

    Setup failures raise RunError with kind SETUP_FAILED. They are not a step
    result and they are not exit code 0. A returned result has status
    succeeded and exit_code 0 only when every step that ran exited 0. A step
    whose `if` is false is skipped and does not run. A failed step does not
    run later steps whose condition is false, and it does not stop a later
    step whose condition is true. The result names the first failed step, its
    exit code, and the image digest.

    The first top-level concrete job's deadline is that job's
    `timeout_minutes` (default 360) measured from the start of this call.
    Each later top-level job, and each job inside a called workflow, gets
    its own deadline when it starts. A caller job has no `timeout-minutes`.
    Reaching a deadline returns status cancelled and does not start later
    jobs. A needed job that failed or was skipped skips a dependent job
    unless that job's `if` is true. A step timeout stops the container and
    does not start later jobs. `owner` reserves the name before
    create. If that owner is already cancelled, this does not start the
    container. `network` is `bridge` unless the caller passes `none`.
    `docker_socket` is off unless the caller passes true or a socket path.
    When the socket is mounted, a private Docker volume is mounted at
    that volume's mountpoint and `TMPDIR`, `TEMP`, and `TMP` default to it.
    `node24` is omitted, or a `(directory, digest)` pair. The directory is
    mounted read-only at `/opt/node24` and is not placed on `PATH`.
    `actions` is the attempt copy of node24 action files, mounted
    read-write at `/actions`. The content store is not mounted. A plan
    that runs node24 main without the Node directory fails setup before
    the container starts.
    Service containers for an enabled job start before its steps and are
    removed before the next job. They do not receive the engine socket
    or that volume.
    """

    if network not in _NETWORKS:
        _setup("container network is not accepted")
    node_mount = _accept_node24(node24)
    if event_name is not None and (
        not isinstance(event_name, str)
        or event_name == ""
        or "\0" in event_name
        or "\n" in event_name
        or "\r" in event_name
    ):
        _setup("event name is not accepted")
    started = time.monotonic()
    reference, digest, workflow, jobs, event_bytes, workspace, manifest = _prepare(
        snapshot_dir, snapshot_digest, workspace, plan, image, event
    )
    needs_node = _plan_uses_node24(jobs)
    if needs_node and node_mount is None:
        _setup("Node 24 is not configured")
    actions_root = _accept_actions(actions, needs_node)
    deadline = started + _job_seconds(jobs[0])
    resolved = digest
    docker_bin = _docker_binary(docker)
    socket_path, socket_gid = _job_socket(docker_bin, docker_socket)
    private = Path(tempfile.mkdtemp(prefix="rookrunner-run-"))
    os.chmod(private, 0o700)
    commands = Path(tempfile.mkdtemp(prefix="rookrunner-cmd-"))
    os.chmod(commands, 0o700)
    name = None
    created = False
    graceful = False
    outcome = None
    mounted = None
    failure = None
    records = []
    service_cleanup = 0
    attempt_token = None
    path_token = None
    socket_temp = None
    socket_volume = None
    try:
        resolved, arch = _resolve_image(docker_bin, reference, digest, deadline)
        runner_dirs = _runner_dirs(workspace)
        if socket_path is not None:
            socket_volume, socket_temp = _socket_share(docker_bin, reference, deadline)
        attempt_token = _ATTEMPT.set(
            {
                "arch": arch,
                "event_name": event_name,
                "sha": _commit_sha(manifest),
                "temp": runner_dirs["runner-temp"],
                "socket_temp": socket_temp,
            }
        )
        (private / "event.json").write_bytes(event_bytes)
        os.chmod(private / "event.json", 0o600)
        scripts = {}
        _write_job_scripts(jobs, (), private, scripts)
        for label in (private, commands):
            if "," in os.fspath(label) or "\n" in os.fspath(label):
                _setup("workspace path is not accepted")
        name = _container_name(owner)
        if owner is not None and not owner.begin(docker_bin, name):
            _setup("run was cancelled before the container existed")
        code, _stdout, _stderr = _invoke_within(
            docker_bin,
            _create_args(
                name,
                workspace,
                private,
                commands,
                reference,
                network,
                socket_path,
                socket_gid,
                runner_dirs,
                None if node_mount is None else node_mount["root"],
                actions_root,
                socket_temp,
                socket_volume,
            ),
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
        if node_mount is not None:
            mounted = {
                "digest": node_mount["digest"],
                "version": _read_node_version(docker_bin, name, deadline),
            }
        failed = None
        output_bytes = 0

        def _mark_call(job, parent_path, message):
            nonlocal failed
            concrete = list(_iter_concrete(job, parent_path))
            if message is not None and not concrete:
                _setup("plan is not accepted")
            for index, (inner, step) in enumerate(concrete):
                if message is not None and index == 0:
                    record = _step_result(step, "failed", None, "", "", message[:512], inner["id"])
                    records.append(record)
                    if failed is None:
                        failed = record
                else:
                    records.append(_step_result(step, "skipped", None, "", "", None, inner["id"]))

        def _execute_jobs(job_list, level_workflow, inputs, path, *, reset_first):
            nonlocal deadline, failed, output_bytes, graceful, outcome, service_cleanup
            results = {}
            jobs_by_id = {item["id"]: item for item in job_list}
            for job_index, job in enumerate(job_list):
                if "call" not in job and (reset_first or job_index):
                    deadline = time.monotonic() + _job_seconds(job)
                if owner is not None and owner.cancelled():
                    graceful = True
                    outcome = _cancelled(resolved, reference, records)
                    return None
                if "call" not in job and deadline - time.monotonic() <= 0:
                    raise _JobDeadline()
                needs = _needs_context(job, results)
                ancestor_failed = _ancestor_failed(job, jobs_by_id, results)
                cancelled = owner is not None and owner.cancelled()
                try:
                    enabled = job_is_enabled(
                        job.get("if"),
                        _expression_values(
                            event,
                            level_workflow,
                            job,
                            {"env": {}},
                            [],
                            cancelled,
                            needs,
                            inputs=inputs,
                        ),
                        [needs[item]["result"] for item in job["needs"]],
                        ancestor_failed,
                        cancelled,
                    )
                except ExprError as exc:
                    enabled = None
                    message = str(exc)[:512]
                if enabled is None:
                    if "call" in job:
                        _mark_call(job, path, message)
                    else:
                        record = _step_result(
                            job["steps"][0], "failed", None, "", "", message, job["id"]
                        )
                        records.append(record)
                        if failed is None:
                            failed = record
                        for step in job["steps"][1:]:
                            records.append(
                                _step_result(step, "skipped", None, "", "", None, job["id"])
                            )
                    results[job["id"]] = {"result": "failure", "outputs": {}}
                    continue
                if not enabled:
                    if "call" in job:
                        _mark_call(job, path, None)
                    else:
                        for step in job["steps"]:
                            records.append(
                                _step_result(step, "skipped", None, "", "", None, job["id"])
                            )
                    results[job["id"]] = {"result": "skipped", "outputs": {}}
                    continue
                if "call" in job:
                    try:
                        passed_values = _expression_values(
                            event,
                            level_workflow,
                            job,
                            {"env": {}},
                            [],
                            cancelled,
                            needs,
                            inputs=inputs,
                        )
                    except ExprError as exc:
                        _mark_call(job, path, str(exc))
                        results[job["id"]] = {"result": "failure", "outputs": {}}
                        continue
                    call_path = job["call"].get("path")
                    call_token = _WORKFLOW_PATH.set(
                        call_path if isinstance(call_path, str) and call_path != "" else None
                    )
                    try:
                        try:
                            resolved_inputs = _resolve_call_inputs(
                                job["call"]["inputs"],
                                passed_values,
                                lambda explicit, call_workflow=job["call"]["workflow"]: (
                                    _expression_values(
                                        event,
                                        call_workflow,
                                        {"env": {}},
                                        {"env": {}},
                                        [],
                                        cancelled,
                                        inputs=explicit,
                                    )
                                ),
                            )
                        except (ExprError, _CallInputError) as exc:
                            _mark_call(job, path, str(exc))
                            results[job["id"]] = {"result": "failure", "outputs": {}}
                            continue
                        inner_results = _execute_jobs(
                            job["call"]["jobs"],
                            job["call"]["workflow"],
                            resolved_inputs,
                            path + (job["id"],),
                            reset_first=True,
                        )
                        if inner_results is None:
                            return None
                        output_values = _expression_values(
                            event,
                            job["call"]["workflow"],
                            {"env": {}},
                            {"env": {}},
                            [],
                            cancelled,
                            inputs=resolved_inputs,
                        )
                        output_values["jobs"] = {
                            item_id: {
                                "result": item["result"],
                                "outputs": dict(item["outputs"]),
                            }
                            for item_id, item in inner_results.items()
                        }
                        produced, output_bytes = _job_outputs(
                            {"outputs": job["call"]["outputs"]},
                            output_values,
                            output_bytes,
                        )
                        call_failed = any(
                            item["result"] == "failure" for item in inner_results.values()
                        )
                        results[job["id"]] = {
                            "result": "failure" if call_failed else "success",
                            "outputs": produced,
                        }
                    finally:
                        _WORKFLOW_PATH.reset(call_token)
                    continue
                runs = _combination_contexts(job)
                if not runs:
                    results[job["id"]] = {"result": "skipped", "outputs": {}}
                    continue
                strategy = job.get("strategy") if isinstance(job.get("strategy"), dict) else None
                fail_fast = (
                    strategy is not None
                    and strategy.get("combinations") is not None
                    and strategy.get("fail_fast") is True
                )
                publish = strategy is None or strategy.get("combinations") is None
                job_failed = False
                produced = {}
                cancelled_run = False
                service_names = []
                service_network = None
                connected = False
                try:
                    services = job.get("services") or []
                    if services:
                        if deadline - time.monotonic() <= 0:
                            raise _JobDeadline()
                        if network == "none":
                            _setup("service containers are not started on network none")
                        service_network = _owned_network_name(name)
                        _create_service_network(docker_bin, service_network, deadline)
                        if owner is not None:
                            owner.note_network(service_network)
                        code, _stdout, _stderr = _invoke_within(
                            docker_bin,
                            ["network", "connect", service_network, name],
                            60,
                            deadline,
                        )
                        if code != 0:
                            _setup("service container setup failed")
                        connected = True
                        for index, service in enumerate(services):
                            if owner is not None and owner.cancelled():
                                graceful = True
                                outcome = _cancelled(resolved, reference, records)
                                cancelled_run = True
                                break
                            ready = _start_one_service(
                                docker_bin,
                                name,
                                service_network,
                                service,
                                index,
                                deadline,
                                service_names,
                                owner,
                            )
                            if not ready:
                                graceful = True
                                outcome = _cancelled(resolved, reference, records)
                                cancelled_run = True
                                break
                    if not cancelled_run:
                        attempt = _ATTEMPT.get()
                        if attempt is not None:
                            _empty_directory(attempt.get("temp"))
                        for matrix, strategy_context in runs:
                            if owner is not None and owner.cancelled():
                                graceful = True
                                outcome = _cancelled(resolved, reference, records)
                                cancelled_run = True
                                break
                            if deadline - time.monotonic() <= 0:
                                raise _JobDeadline()
                            prior = []
                            runtime = _JobRuntime()
                            for step in job["steps"]:
                                if deadline - time.monotonic() <= 0:
                                    raise _JobDeadline()
                                record = _consider_step(
                                    docker_bin,
                                    name,
                                    step,
                                    level_workflow,
                                    job,
                                    event,
                                    workspace,
                                    bash_ok,
                                    step_timeout,
                                    deadline,
                                    prior,
                                    owner is not None and owner.cancelled(),
                                    needs,
                                    scripts[(path + (job["id"],), step["index"])],
                                    runtime,
                                    commands,
                                    matrix=matrix,
                                    strategy=strategy_context,
                                    inputs=inputs,
                                )
                                records.append(record)
                                prior.append(record)
                                if record["status"] == "failed":
                                    job_failed = True
                                    if failed is None:
                                        failed = record
                            if owner is not None and owner.cancelled():
                                graceful = True
                                outcome = _cancelled(resolved, reference, records)
                                cancelled_run = True
                                break
                            posts = _node24_posts(job, runtime)
                            if posts and deadline - time.monotonic() <= 0:
                                raise _JobDeadline()
                            post_index = len(job["steps"])
                            for step in posts:
                                if owner is not None and owner.cancelled():
                                    graceful = True
                                    outcome = _cancelled(resolved, reference, records)
                                    cancelled_run = True
                                    break
                                if deadline - time.monotonic() <= 0:
                                    raise _JobDeadline()
                                record = _consider_post(
                                    docker_bin,
                                    name,
                                    step,
                                    level_workflow,
                                    job,
                                    event,
                                    workspace,
                                    bash_ok,
                                    step_timeout,
                                    deadline,
                                    prior,
                                    needs,
                                    scripts[(path + (job["id"],), step["index"])],
                                    runtime,
                                    commands,
                                    post_index,
                                    job.get("id"),
                                    matrix=matrix,
                                    strategy=strategy_context,
                                    inputs=inputs,
                                )
                                records.append(record)
                                post_index += 1
                                if record["status"] == "failed":
                                    job_failed = True
                                    if failed is None:
                                        failed = record
                            if cancelled_run:
                                break
                            if publish:
                                produced, output_bytes = _job_outputs(
                                    job,
                                    _expression_values(
                                        event,
                                        level_workflow,
                                        job,
                                        {"env": {}},
                                        prior,
                                        cancelled,
                                        needs,
                                        runtime,
                                        inputs=inputs,
                                        matrix=matrix,
                                        strategy=strategy_context,
                                    ),
                                    output_bytes,
                                )
                            if job_failed and fail_fast:
                                break
                finally:
                    if service_names or service_network is not None:
                        retired = _retire_services(
                            docker_bin, name, service_names, service_network, connected
                        )
                        if retired != 0:
                            service_cleanup = retired
                        elif owner is not None:
                            owner.forget_services()
                if cancelled_run:
                    return None
                result_name = "failure" if job_failed else "success"
                results[job["id"]] = {
                    "result": result_name,
                    "outputs": produced if publish else {},
                }
            return results

        workflow_path = manifest.get("workflow") if isinstance(manifest, dict) else None
        path_token = _WORKFLOW_PATH.set(
            workflow_path if isinstance(workflow_path, str) and workflow_path != "" else None
        )
        finished = _execute_jobs(jobs, workflow, {}, (), reset_first=False)
        if finished is not None:
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
        if path_token is not None:
            _WORKFLOW_PATH.reset(path_token)
        if attempt_token is not None:
            _ATTEMPT.reset(attempt_token)
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
            if service_cleanup != 0:
                retried = _retire_services(docker_bin, name, [], _owned_network_name(name), True)
                if retried == 0:
                    service_cleanup = 0
        shutil.rmtree(private, ignore_errors=True)
        shutil.rmtree(commands, ignore_errors=True)
        if socket_volume is not None:
            try:
                _invoke(docker_bin, ["volume", "rm", socket_volume], 30)
            except (RunError, _Timeout):
                pass
        if owner is not None:
            owner.closed()
    if failure is not None:
        raise failure
    if cleanup_code != 0 or service_cleanup != 0:
        _setup("container cleanup failed")
    if outcome is not None and mounted is not None:
        outcome["node24"] = mounted
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
    matrix=None,
    strategy=None,
):
    """Contexts for one step `if`. Missing properties stay missing.

    `env` starts with `CI` and `HOME` while an attempt is running, then the
    workflow env, the job env, `GITHUB_ENV` from earlier steps in this job,
    then this step's env. The default `GITHUB_*` and `RUNNER_*` variables
    are not copied into `env`. Values that contain `${{ }}` are not
    expanded. `steps.<id>.outputs` is what earlier steps wrote to
    `GITHUB_OUTPUT`. The command-file paths are not part of this map.
    `inputs` is empty unless the caller passes workflow inputs or composite
    action inputs. `github.action_path` stays empty unless the caller is
    inside a composite action. `output_map` supplies inner-step outputs so
    they are not stored as workflow step outputs. A caller job has no `env`
    key; a missing map is empty.
    """

    env = {}
    env.update(_soft_defaults())
    env.update(_env_layer(workflow.get("env", {})))
    env.update(_env_layer(job.get("env", {})))
    if runtime is not None:
        env.update(runtime.env)
    env.update(_env_layer(step.get("env", {})))
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
    runner = {"os": "Linux"}
    attempt = _ATTEMPT.get()
    if attempt is not None:
        github["workspace"] = "/workspace"
        github["event_path"] = _EVENT_FILE
        job_id = job.get("id") if isinstance(job, dict) else None
        if isinstance(job_id, str) and job_id != "":
            github["job"] = job_id
        label = _workflow_label(workflow)
        if label is not None:
            github["workflow"] = label
        if attempt.get("event_name") is not None:
            github["event_name"] = attempt["event_name"]
        if attempt.get("sha") is not None:
            github["sha"] = attempt["sha"]
        runner = {
            "os": "Linux",
            "arch": attempt["arch"],
            "environment": "self-hosted",
            "temp": _RUNNER_TEMP,
            "tool_cache": _TOOL_CACHE,
        }
    return {
        "github": github,
        "needs": needs if isinstance(needs, dict) else {},
        "strategy": strategy if isinstance(strategy, dict) else {},
        "matrix": matrix if isinstance(matrix, dict) else {},
        "job": {"status": job_status},
        "runner": runner,
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
    matrix=None,
    strategy=None,
    inputs=None,
):
    job_id = job.get("id")
    try:
        enabled = step_is_enabled(
            step.get("if"),
            _expression_values(
                event,
                workflow,
                job,
                step,
                prior,
                cancelled,
                needs,
                runtime,
                inputs=inputs,
                matrix=matrix,
                strategy=strategy,
            ),
            prior,
            cancelled,
        )
    except ExprError as exc:
        return _step_result(step, "failed", None, "", "", str(exc)[:512], job_id)
    if not enabled:
        return _step_result(step, "skipped", None, "", "", None, job_id)
    if step.get("checkout") == "captured":
        return _step_result(step, "succeeded", 0, "", "", None, job_id)
    if step.get("javascript") == "node24":
        return _run_javascript(
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
            matrix=matrix,
            strategy=strategy,
            inputs=inputs,
        )
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
            matrix=matrix,
            strategy=strategy,
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
    matrix=None,
    strategy=None,
):
    """Run the inlined composite steps as one workflow step.

    Each inner exec uses the calling step's timeout and the job deadline.
    That timeout is not divided across the inner steps.
    """

    if not isinstance(script_names, list) or len(script_names) != len(step["steps"]):
        _setup("plan is not accepted")
    try:
        caller_values = _expression_values(
            event,
            workflow,
            job,
            step,
            prior,
            cancelled,
            needs,
            runtime,
            matrix=matrix,
            strategy=strategy,
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
            matrix=matrix,
            strategy=strategy,
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
            matrix=matrix,
            strategy=strategy,
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
        matrix=matrix,
        strategy=strategy,
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
    matrix=None,
    strategy=None,
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
        matrix=matrix,
        strategy=strategy,
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


def _input_env_name(name):
    """INPUT_<NAME>, upper case, with spaces replaced by underscores."""

    return "INPUT_" + name.upper().replace(" ", "_")


def _node_main_path(step):
    """Container path of one node24 main file under /actions."""

    return _node_entry_path(step, step.get("main"))


def _node_entry_path(step, entry):
    """Container path of one node24 entry file under /actions."""

    owner = step.get("action_owner")
    repository = step.get("action_repository")
    commit = step.get("action_commit")
    action_path = step.get("action_path")
    if not all(isinstance(item, str) and item != "" for item in (owner, repository, commit, entry)):
        _setup("plan is not accepted")
    if not isinstance(action_path, str):
        _setup("plan is not accepted")
    parts = [owner, repository, commit]
    if action_path:
        parts.extend(action_path.split("/"))
    parts.extend(entry.split("/"))
    if any(part in {"", ".", ".."} or "\\" in part or "\0" in part for part in parts):
        _setup("plan is not accepted")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        _setup("plan is not accepted")
    return "/actions/" + "/".join(parts)


def _javascript_env(step, values):
    """Resolve node24 inputs. A with value wins. required does not fail a miss."""

    resolved = {}
    for key, raw in step.get("with", {}).items():
        resolved[key] = _render_expr(raw, values)
    spec = step.get("inputs")
    if not isinstance(spec, dict):
        _setup("plan is not accepted")
    for key in resolved:
        if key not in spec:
            _setup("plan is not accepted")
    env = {}
    for key, item in spec.items():
        if not isinstance(item, dict):
            _setup("plan is not accepted")
        if key in resolved:
            value = resolved[key]
        elif isinstance(item.get("default"), str):
            value = _render_expr(item["default"], values)
        else:
            value = ""
        if not isinstance(value, str) or "\0" in value:
            value = ""
        env[_input_env_name(key)] = value
    return env


def _run_javascript(
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
    matrix=None,
    strategy=None,
    inputs=None,
):
    """Run node24 main with /opt/node24/bin/node. The working directory is /workspace."""

    if not isinstance(script_name, str) or script_name == "":
        _setup("plan is not accepted")
    try:
        values = _expression_values(
            event,
            workflow,
            job,
            step,
            prior,
            cancelled,
            needs,
            runtime,
            inputs=inputs,
            matrix=matrix,
            strategy=strategy,
        )
        extra = _javascript_env(step, values)
    except ExprError as exc:
        return _step_result(step, "failed", None, "", "", str(exc)[:512], job_id)
    runtime.javascript_inputs[step["index"]] = {
        key: value for key, value in extra.items() if key.startswith("INPUT_")
    }
    extra["GITHUB_STATE"] = f"/run/rookrunner-cmd/{script_name}-state"
    extra["GITHUB_STEP_SUMMARY"] = f"/run/rookrunner-cmd/{script_name}-summary"
    result = _run_step(
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
        extra_reserved=extra,
        argv=[_NODE24_BINARY, _node_main_path(step)],
        workdir_override="/workspace",
        command_extra=("state", "summary"),
    )
    if isinstance(result.get("exit_code"), int):
        runtime.javascript_ran.add(step["index"])
    return result


def _node24_posts(job, runtime):
    """Node24 steps whose main ran and that declare post, last main first."""

    found = []
    for step in job.get("steps") or []:
        if step.get("javascript") != "node24" or "post" not in step:
            continue
        if step.get("index") not in runtime.javascript_ran:
            continue
        found.append(step)
    found.reverse()
    return found


def _post_state_env(runtime, index):
    """STATE_<name> for one action. Other actions do not receive it."""

    extra = {}
    stored = runtime.action_state.get(index, {})
    if not isinstance(stored, dict):
        return extra
    for key, value in stored.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in value:
            continue
        name = "STATE_" + key
        if not _ENV_NAME.fullmatch(name):
            continue
        extra[name] = value
    return extra


def _consider_post(
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
    needs,
    script_name,
    runtime,
    commands,
    index,
    job_id,
    matrix=None,
    strategy=None,
    inputs=None,
):
    """Run one node24 post from /workspace. An omitted post-if is always()."""

    if not isinstance(script_name, str) or script_name == "":
        _setup("plan is not accepted")
    saved = runtime.javascript_inputs.get(step["index"])
    if not isinstance(saved, dict):
        _setup("plan is not accepted")
    posted = dict(step)
    posted["index"] = index
    post_script = f"{script_name}-post"
    try:
        values = _expression_values(
            event,
            workflow,
            job,
            step,
            prior,
            False,
            needs,
            runtime,
            inputs=inputs,
            matrix=matrix,
            strategy=strategy,
        )
        source = step.get("post_if")
        enabled = True if source is None else step_is_enabled(source, values, prior, False)
    except ExprError as exc:
        return _step_result(posted, "failed", None, "", "", str(exc)[:512], job_id)
    if not enabled:
        return _step_result(posted, "skipped", None, "", "", None, job_id)
    extra = dict(saved)
    extra.update(_post_state_env(runtime, step["index"]))
    extra["GITHUB_STATE"] = f"/run/rookrunner-cmd/{post_script}-state"
    extra["GITHUB_STEP_SUMMARY"] = f"/run/rookrunner-cmd/{post_script}-summary"
    return _run_step(
        docker,
        name,
        posted,
        workflow,
        job,
        workspace,
        bash_ok,
        step_timeout,
        deadline,
        post_script,
        job_id,
        runtime,
        commands,
        extra_reserved=extra,
        argv=[_NODE24_BINARY, _node_entry_path(step, step.get("post"))],
        workdir_override="/workspace",
        command_extra=("state", "summary"),
    )


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
    argv=None,
    workdir_override=None,
    command_extra=(),
):
    if argv is None:
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
    else:
        command = list(argv)
        workdir = workdir_override
        if (
            not command
            or workdir is None
            or any(not isinstance(part, str) or "\0" in part for part in command)
        ):
            _setup("plan is not accepted")
    files = _command_files(commands, script_name, command_extra)
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
    if runtime is not None and "state" in files:
        state_text = _read_utf8(files["state"])
        if state_text is not None:
            runtime.action_state[step.get("index")] = parse_env(state_text)
    status = "succeeded" if code == 0 else "failed"
    result = _step_result(step, status, code, logged, logged_err, None, job_id)
    if "summary" in files:
        summary = _read_utf8(files["summary"])
        if summary:
            # Same character cap as step stdout in the v0 contract.
            result["summary"] = summary[:65536]
    return result


def _command_files(commands, script_name, extra=()):
    files = {
        "env": commands / f"{script_name}-env",
        "output": commands / f"{script_name}-output",
        "path": commands / f"{script_name}-path",
    }
    for name in extra:
        files[name] = commands / f"{script_name}-{name}"
    return files


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
