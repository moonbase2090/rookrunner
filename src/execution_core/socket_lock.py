"""Startup rules for the worker secret flags and the Docker socket.

``--secrets`` takes no path. ``--app-key`` accepts one path and does not
open it. ``--docker-socket`` combined with either flag refuses unless the
``~/Secrets`` probe reports that the directory is not shared. This module
does not inject a secret and does not mint a token.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess

_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?")
_APP_KEY_MESSAGE = (
    "worker --app-key accepts only ~/Secrets/github-app/rookrunner-app/private-key.pem"
)
_APP_KEY_PARTS = ("Secrets", "github-app", "rookrunner-app", "private-key.pem")
# The command prints nothing. Exit 42 means the mount is a usable directory.
_PROBE_SCRIPT = (
    "if [ -d /rr-secrets ] && [ -r /rr-secrets ] && [ -x /rr-secrets ]; "
    "then exit 42; else exit 0; fi"
)
_MOUNT_MARKERS = (
    "bind source path does not exist",
    "error while creating mount source path",
    "is not shared from the host",
    "mounts denied",
    "invalid mount config",
)
HOST_CONTROL_WARNING = (
    "docker socket exposes the host engine, including "
    "~/Secrets/github-app/rookrunner-app/ and "
    "~/Secrets/rookrunner-secrets/"
)


class SocketFlags:
    def __init__(self, secrets, github_repository, app_key, secret_refs, secret_pushers):
        self.secrets = secrets
        self.github_repository = github_repository
        self.app_key = app_key
        self.secret_refs = secret_refs
        self.secret_pushers = secret_pushers


def _combination(partners, detail=None):
    names = ["worker --docker-socket", *(f"worker {flag}" for flag in partners)]
    text = " and ".join(names) + " cannot be combined"
    if detail:
        return f"{text}: {detail}"
    return text


def _lexical(path):
    expanded = Path(path).expanduser()
    parts = []
    for part in expanded.parts:
        if part == expanded.anchor:
            parts = [part]
            continue
        if part in ("", "."):
            continue
        if part == ".." and len(parts) > 1:
            parts.pop()
            continue
        if part != "..":
            parts.append(part)
    return Path(*parts) if parts else Path(expanded.anchor or ".")


def _symlink_below_home(path, home):
    """Return whether `home` or a component under it is a symlink.

    Ancestors of the home directory are the operating system's layout.
    A symlink there is not a redirected key path.
    """

    try:
        if home.is_symlink():
            return True
    except OSError:
        return True
    try:
        relative = path.relative_to(home)
    except ValueError:
        return True
    current = home
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def accept_app_key(value):
    """Return the one accepted key path. Do not open the file."""

    if not isinstance(value, str) or value == "" or "\0" in value or "\n" in value:
        raise ValueError(_APP_KEY_MESSAGE)
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ValueError(_APP_KEY_MESSAGE)
    home = Path.home()
    allowed = _lexical(home.joinpath(*_APP_KEY_PARTS))
    candidate = _lexical(expanded)
    if candidate != allowed or _symlink_below_home(candidate, _lexical(home)):
        raise ValueError(_APP_KEY_MESSAGE)
    return str(allowed)


def _repository(value, required):
    if value is None:
        if required:
            raise ValueError("worker --secrets requires --github-repository")
        return None
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ValueError("worker --github-repository must be owner/name")
    return value


def _secret_ref(value):
    message = "worker --secret-ref must be a full ref"
    if (
        not isinstance(value, str)
        or not value.startswith("refs/")
        or value != value.strip()
        or "\n" in value
        or "\0" in value
    ):
        raise ValueError(message)
    try:
        result = subprocess.run(
            ["git", "check-ref-format", value],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(message) from None
    if result.returncode != 0:
        raise ValueError(message)
    return value


def _secret_pusher(value):
    if not isinstance(value, str) or _LOGIN.fullmatch(value) is None:
        raise ValueError("worker --secret-pusher must be a GitHub login")
    return value


def _sequence(values, accept, message):
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, list | tuple):
        raise ValueError(message)
    return tuple(accept(item) for item in values)


def _mount_source(secrets_dir, partners):
    text = os.fspath(secrets_dir)
    if (
        not isinstance(text, str)
        or not text.startswith("/")
        or any(character in text for character in (",", "\n", "\0"))
    ):
        raise ValueError(_combination(partners, "the ~/Secrets probe cannot run"))
    return text


def _rejected(stderr):
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", "replace")
    elif isinstance(stderr, str):
        text = stderr
    else:
        return False
    folded = text.casefold()
    return any(marker in folded for marker in _MOUNT_MARKERS)


def _docker_invoke(args, timeout):
    binary = shutil.which("docker")
    if binary is None or not isinstance(args, list | tuple):
        raise OSError("docker missing")
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("docker timed out") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _invoke(invoke, args, timeout, partners, detail):
    try:
        return invoke(args, timeout)
    except (OSError, TimeoutError):
        raise ValueError(_combination(partners, detail)) from None


def probe_secrets_unshared(image, partners, secrets_dir=None, invoke=None):
    """Return True when ``~/Secrets`` is not usable inside ``image``.

    The container's only host mount is that directory, read-only. It does
    not receive the Docker socket. Its command prints no file names and no
    file bytes. A rejected mount or a path that is not a usable directory
    returns True. A usable directory, a daemon that cannot be contacted, or
    an image failure other than the mount raises ValueError and names
    ``partners``. The container is removed before this function returns.
    This function does not create the directory and does not pull an image.
    """

    if secrets_dir is None:
        secrets_dir = Path.home() / "Secrets"
    if invoke is None:
        invoke = _docker_invoke
    source = _mount_source(secrets_dir, partners)
    code, _stdout, _stderr = _invoke(
        invoke,
        ["version", "--format", "{{.Server.APIVersion}}"],
        10,
        partners,
        "Docker daemon cannot be contacted",
    )
    if code != 0:
        raise ValueError(_combination(partners, "Docker daemon cannot be contacted"))
    code, _stdout, _stderr = _invoke(
        invoke,
        ["image", "inspect", "--format", "{{.Id}}", image],
        30,
        partners,
        "runner image failed for a reason other than the mount",
    )
    if code != 0:
        raise ValueError(
            _combination(partners, "runner image failed for a reason other than the mount")
        )
    name = "rookrunner-secrets-probe-" + os.urandom(4).hex()
    created = False
    image_failure = "runner image failed for a reason other than the mount"
    try:
        code, _stdout, stderr = _invoke(
            invoke,
            [
                "create",
                "--name",
                name,
                "--network",
                "none",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                "--mount",
                f"type=bind,source={source},target=/rr-secrets,readonly",
                image,
                "-c",
                _PROBE_SCRIPT,
            ],
            30,
            partners,
            image_failure,
        )
        if code != 0:
            if _rejected(stderr):
                return True
            raise ValueError(_combination(partners, image_failure))
        created = True
        code, _stdout, stderr = _invoke(invoke, ["start", name], 30, partners, image_failure)
        if code != 0:
            if _rejected(stderr):
                return True
            raise ValueError(_combination(partners, image_failure))
        code, stdout, _stderr = _invoke(invoke, ["wait", name], 30, partners, image_failure)
        if code != 0:
            raise ValueError(_combination(partners, image_failure))
        try:
            status = int(stdout.decode("ascii").strip())
        except (AttributeError, UnicodeError, ValueError):
            raise ValueError(_combination(partners, image_failure)) from None
        if status == 42:
            raise ValueError(_combination(partners, "~/Secrets is a usable directory"))
        if status != 0:
            raise ValueError(_combination(partners, image_failure))
        return True
    finally:
        try:
            remove_code, _stdout, _stderr = invoke(["rm", "-f", name], 30)
        except (OSError, TimeoutError):
            remove_code = 1
        if created and remove_code != 0:
            raise ValueError(_combination(partners, "the probe container could not be removed"))


def accept_socket_flags(
    *,
    docker_socket,
    secrets,
    github_repository,
    app_key,
    secret_refs,
    secret_pushers,
    runner_image,
):
    """Validate the worker flags. Probe only for a socket combination."""

    if type(secrets) is not bool:
        raise ValueError("worker --secrets takes no path")
    repository = _repository(github_repository, secrets)
    key = None if app_key is None else accept_app_key(app_key)
    refs = _sequence(secret_refs, _secret_ref, "worker --secret-ref must be a full ref")
    pushers = _sequence(
        secret_pushers, _secret_pusher, "worker --secret-pusher must be a GitHub login"
    )
    partners = []
    if key is not None:
        partners.append("--app-key")
    if secrets:
        partners.append("--secrets")
    if docker_socket and partners:
        if runner_image is None:
            raise ValueError(
                _combination(partners, "the ~/Secrets probe needs worker --runner-image")
            )
        unshared = probe_secrets_unshared(
            runner_image,
            partners,
            secrets_dir=Path.home() / "Secrets",
        )
        if unshared is False:
            raise ValueError(_combination(partners, "~/Secrets is a usable directory"))
    return SocketFlags(secrets, repository, key, refs, pushers)
