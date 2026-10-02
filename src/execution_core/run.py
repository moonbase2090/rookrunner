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

Workflow env is overridden by job env, then by step env. The runner then sets
`GITHUB_WORKSPACE` and `ROOKRUNNER_EVENT`, so those two names stay pointed at
this attempt. `ROOKRUNNER_EVENT` is a read-only file holding the caller event
as canonical JSON. It is not a GitHub event delivery. No other GitHub context
is invented. `runs-on` does not select an image.

The container is created with network `none`. The Docker socket and host
credential directories are not mounted. Expressions in `run` are not evaluated.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

from .protocol import canonical
from .verify import VerifyError, verify_snapshot

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILE_MODES = {"100644": 0o644, "100755": 0o755}
_INSPECT = '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}}}'
_RESERVED_ENV = {
    "GITHUB_WORKSPACE": "/workspace",
    "ROOKRUNNER_EVENT": "/run/rookrunner/event.json",
}


class RunError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class _Timeout(Exception):
    pass


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


def _resolve_image(docker, reference, digest):
    code, stdout = _inspect(docker, reference)
    if code != 0 and reference != digest:
        pull_code, _stdout, _stderr = _invoke(docker, ["pull", reference], 300)
        if pull_code != 0:
            _setup("image digest will not resolve")
        code, stdout = _inspect(docker, reference)
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


def _inspect(docker, reference):
    code, stdout, _stderr = _invoke(
        docker, ["image", "inspect", "--format", _INSPECT, reference], 60
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
    if not isinstance(plan, dict) or plan.get("capability_version") != 1:
        _setup("plan is not accepted")
    workflow = plan.get("workflow")
    job = plan.get("job")
    if not isinstance(workflow, dict) or not isinstance(job, dict):
        _setup("plan is not accepted")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        _setup("plan is not accepted")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("index") != index:
            _setup("plan is not accepted")
        if not isinstance(step.get("run"), str) or "\0" in step["run"]:
            _setup("plan is not accepted")
    return workflow, job, steps


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


def _merged_env(workflow, job, step):
    merged = {}
    merged.update(_env_layer(workflow.get("env")))
    merged.update(_env_layer(job.get("env")))
    merged.update(_env_layer(step.get("env")))
    merged.update(_RESERVED_ENV)
    return [(key, merged[key]) for key in sorted(merged)]


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


def _step_result(step, status, exit_code, stdout, stderr, error):
    return {
        "index": step["index"],
        "id": step.get("id"),
        "name": step.get("name"),
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
    }


def _prepare(snapshot_dir, snapshot_digest, workspace, plan, image, event):
    reference, digest = _pinned(image)
    workflow, job, steps = _plan_parts(plan)
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
    return reference, digest, workflow, job, steps, encoded, workspace


def _create_args(name, workspace, private, reference):
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
        "sleep",
        "--mount",
        f"type=bind,source={workspace},destination=/workspace",
        "--mount",
        f"type=bind,source={private},destination=/run/rookrunner,readonly",
        reference,
        "infinity",
    ]


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
):
    """Run `plan` in one container identified by `image`.

    Setup failures raise RunError with kind SETUP_FAILED. They are not a step
    result and they are not exit code 0. A returned result has status
    succeeded and exit_code 0 only when every step exited 0. A nonzero step
    stops the sequence. The result names that step, its exit code, and the
    image digest.
    """

    reference, digest, workflow, job, steps, event_bytes, workspace = _prepare(
        snapshot_dir, snapshot_digest, workspace, plan, image, event
    )
    docker_bin = _docker_binary(docker)
    image_digest = _resolve_image(docker_bin, reference, digest)
    private = Path(tempfile.mkdtemp(prefix="rookrunner-run-"))
    os.chmod(private, 0o700)
    name = "rookrunner-" + os.urandom(8).hex()
    created = False
    outcome = None
    failure = None
    try:
        (private / "event.json").write_bytes(event_bytes)
        os.chmod(private / "event.json", 0o600)
        for step in steps:
            script = private / f"step-{step['index']}"
            script.write_bytes(step["run"].encode("utf-8"))
            os.chmod(script, 0o600)
        if "," in os.fspath(private):
            _setup("workspace path is not accepted")
        code, _stdout, _stderr = _invoke(
            docker_bin, _create_args(name, workspace, private, reference), 60
        )
        if code != 0:
            _setup("container setup failed")
        created = True
        code, _stdout, _stderr = _invoke(docker_bin, ["start", name], 60)
        if code != 0:
            _setup("container setup failed")
        probe, _stdout, _stderr = _invoke(docker_bin, ["exec", name, "bash", "-c", "exit 0"], 30)
        bash_ok = probe == 0
        records = []
        for step in steps:
            record = _run_step(
                docker_bin,
                name,
                step,
                workflow,
                job,
                workspace,
                bash_ok,
                step_timeout,
            )
            records.append(record)
            if record["status"] != "succeeded":
                outcome = _outcome(image_digest, reference, records, record)
                break
        else:
            outcome = _outcome(image_digest, reference, records, None)
    except Exception as exc:
        failure = exc
    finally:
        cleanup_code = 0
        if created:
            try:
                cleanup_code, _stdout, _stderr = _invoke(docker_bin, ["rm", "-f", name], 60)
            except (RunError, _Timeout):
                cleanup_code = 1
        shutil.rmtree(private, ignore_errors=True)
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


def _run_step(docker, name, step, workflow, job, workspace, bash_ok, step_timeout):
    shell = _chosen_shell(step, job, workflow)
    relative = _chosen_directory(step, job, workflow)
    container_script = f"/run/rookrunner/step-{step['index']}"
    command = _shell_command(shell, bash_ok, container_script)
    workdir = _working_directory(workspace, relative)
    if command is None:
        return _step_result(step, "failed", None, "", "", "shell is unsupported")
    if workdir is None:
        return _step_result(
            step, "failed", None, "", "", "working-directory is not inside the workspace"
        )
    env_args = []
    for key, value in _merged_env(workflow, job, step):
        env_args.extend(["--env", f"{key}={value}"])
    try:
        code, stdout, stderr = _invoke(
            docker,
            ["exec", "--workdir", workdir, *env_args, name, *command],
            step_timeout,
        )
    except _Timeout:
        return _step_result(step, "failed", None, "", "", "step timed out")
    status = "succeeded" if code == 0 else "failed"
    return _step_result(step, status, code, _text(stdout), _text(stderr), None)
