"""File-backed secrets for one repository.

The store is ``~/Secrets/rookrunner-secrets/<owner>/<repo>/``. A file is
read only when ``--secrets`` is set and the allowlist matches. This module
does not mint a token and does not read the GitHub App key.
"""

import os
from pathlib import Path
import re
import stat

from .poll import allowlist_matches

# Same owner/name shape as worker --github-repository.
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_FILE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_SECRET_BYTES = 48 * 1024
_READ_LIMIT = MAX_SECRET_BYTES + 2


class SecretError(Exception):
    """A secret store or repository check failed. The message is public."""


class SecretAccess:
    """Repository and allowlist lists for one worker. This is not a path."""

    def __init__(self, repository, refs=(), pushers=()):
        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("secret repository is not accepted")
        owner, repo = repository.split("/", 1)
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("secret repository is not accepted")
        self.repository = repository
        self.refs = _tuple(refs)
        self.pushers = _tuple(pushers)


def secrets_allowed(access, event, event_name):
    """Return whether the allowlist allows file secrets for this event."""

    if not isinstance(access, SecretAccess):
        return False
    return allowlist_matches(event, access.refs, access.pushers, event_name)


def same_repository(access, event):
    """Return whether the event repository full name is the worker repository."""

    if not isinstance(access, SecretAccess) or not isinstance(event, dict):
        return False
    repository = event.get("repository")
    if not isinstance(repository, dict):
        return False
    return repository.get("full_name") == access.repository


def load_job_secrets(repository, names):
    """Return file values for `names`. Every directory entry is checked.

    Missing names are omitted. Unreferenced files are not read. The error
    does not include a path or file bytes.
    """

    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise SecretError("secret directory is not accepted")
    owner, repo = repository.split("/", 1)
    wanted = _wanted(names)
    home = Path.home()
    if not home.is_absolute():
        raise SecretError("secret directory is not accepted")
    fds = []
    try:
        fds.append(_open_dir(None, os.fspath(home), require_mode=False))
        parent = fds[-1]
        for part, require_mode in (
            ("Secrets", False),
            ("rookrunner-secrets", True),
            (owner, True),
            (repo, True),
        ):
            fds.append(_open_dir(parent, part, require_mode=require_mode))
            parent = fds[-1]
        return _read_repo(parent, wanted)
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _tuple(values):
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, list | tuple):
        raise ValueError("secret repository is not accepted")
    return tuple(values)


def _wanted(names):
    if isinstance(names, str) or not isinstance(names, list | tuple):
        raise SecretError("secret directory is not accepted")
    wanted = []
    for name in names:
        if (
            not isinstance(name, str)
            or _FILE_NAME.fullmatch(name) is None
            or name.startswith("GITHUB_")
        ):
            raise SecretError("secret directory is not accepted")
        if name not in wanted:
            wanted.append(name)
    return wanted


def _open_dir(parent, name, *, require_mode):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        if parent is None:
            fd = os.open(name, flags)
        else:
            fd = os.open(name, flags, dir_fd=parent)
    except OSError:
        raise SecretError("secret directory is not accepted") from None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise SecretError("secret directory is not accepted") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise SecretError("secret directory is not accepted")
    if require_mode and stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise SecretError("secret directory is not accepted")
    return fd


def _read_repo(repo_fd, wanted):
    try:
        entries = list(os.scandir(repo_fd))
    except OSError:
        raise SecretError("secret directory is not accepted") from None
    found = {}
    pending = set(wanted)
    for entry in entries:
        name = entry.name
        if name in {".", ".."}:
            continue
        if _FILE_NAME.fullmatch(name) is None or name.startswith("GITHUB_"):
            raise SecretError(f"secret file {name} is not accepted")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=repo_fd)
        except OSError:
            raise SecretError("secret directory is not accepted") from None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SecretError("secret directory is not accepted")
            if name in pending:
                found[name] = _read_secret(fd, name)
                pending.discard(name)
        finally:
            os.close(fd)
    return found


def _read_secret(fd, name):
    chunks = []
    remaining = _READ_LIMIT
    try:
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
    except OSError:
        raise SecretError(f"secret {name} is not accepted") from None
    raw = b"".join(chunks)
    if len(raw) > MAX_SECRET_BYTES + 1:
        raise SecretError(f"secret {name} is not accepted")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if len(raw) > MAX_SECRET_BYTES or b"\0" in raw:
        raise SecretError(f"secret {name} is not accepted")
    if raw == b"":
        raise SecretError(f"secret {name} is empty")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise SecretError(f"secret {name} is not accepted") from None
