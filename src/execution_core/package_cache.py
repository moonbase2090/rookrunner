# SPDX-License-Identifier: MIT

"""Package download caches that the jobs of one repository on one worker share.

The cache lives in the worker state directory, so it belongs to one host and
one repository. A job gets the download caches of the package managers its
workflows use: cargo's registry and git checkouts, pip, uv, npm, pnpm, yarn
and the Go module cache, plus install directories for tools that `cargo
install`, `pipx` and `uv tool` put on `PATH`. It never gets a build-output
cache: `target/`, `node_modules/` and the Go build cache stay in the job.

Cargo's two package cache lock files are shared on purpose. Cargo takes them
before it reads or writes the registry, so concurrent jobs wait for each
other instead of corrupting it.

Trust is decided by the worker from the stored event, never by the job. Two
namespaces never share a byte: pushes to the default branch, and everything
else that comes from a branch of this repository, pull requests included. A
fork, an untrusted override, a different repository or any other event gets
no cache at all. Inside a namespace the cache is also kept per runner image,
because an installed tool is only known to work on the image that built it.

apt is not cached. Its archive and list directories are written by root, so a
job could leave files the worker cannot age out, and the saving is small next
to a package index refresh.
"""

import contextlib
import errno
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

CACHE_DIR = "package-cache"
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_BRANCH_NAMESPACE = "default-branch"
BRANCH_NAMESPACE = "branches"
NAMESPACES = (DEFAULT_BRANCH_NAMESPACE, BRANCH_NAMESPACE)
CACHE_ROOT = "/github/cache"
CARGO_TOOLS_ROOT = f"{CACHE_ROOT}/cargo-tools"
TOOLS_BIN = f"{CACHE_ROOT}/tools/bin"
LOCK_FILES = (".package-cache", ".package-cache-mutate")
_STAMP = ".last-used"
_PUSH_EVENTS = ("push", "schedule", "workflow_dispatch")
_IMAGE = re.compile(r"[0-9a-f]{16}\Z")
# Directories under the one `/github/cache` mount. Each is a download cache or
# an install root. Build output and per-run state have no place here.
_SHARED_DIRS = (
    "pip",
    "uv",
    "npm",
    "pnpm-store",
    "yarn",
    "go-mod",
    "cargo-tools",
    "tools/bin",
    "tools/pipx",
    "tools/uv",
)
# Defaults a workflow may replace in its own `env`. The copy modes keep a job
# from changing a cache file through a hardlink.
_ENV_DEFAULTS = {
    "PIP_CACHE_DIR": f"{CACHE_ROOT}/pip",
    "UV_CACHE_DIR": f"{CACHE_ROOT}/uv",
    "UV_LINK_MODE": "copy",
    "UV_TOOL_DIR": f"{CACHE_ROOT}/tools/uv",
    "UV_TOOL_BIN_DIR": TOOLS_BIN,
    "PIPX_HOME": f"{CACHE_ROOT}/tools/pipx",
    "PIPX_BIN_DIR": TOOLS_BIN,
    "npm_config_cache": f"{CACHE_ROOT}/npm",
    "npm_config_store_dir": f"{CACHE_ROOT}/pnpm-store",
    "npm_config_package_import_method": "copy",
    "YARN_CACHE_FOLDER": f"{CACHE_ROOT}/yarn",
    "GOMODCACHE": f"{CACHE_ROOT}/go-mod",
    "CARGO_INSTALL_ROOT": CARGO_TOOLS_ROOT,
}


def _integer(value):
    return value if type(value) is int and value > 0 else None


def _pull_repository_id(pull, side):
    part = pull.get(side) if isinstance(pull, dict) else None
    repo = part.get("repo") if isinstance(part, dict) else None
    return _integer(repo.get("id")) if isinstance(repo, dict) else None


def namespace_for(event, event_name, repository, untrusted_override=False, secret_refs=()):
    """Return the namespace a run may use, or None when it may use no cache.

    `repository` is the worker's verified `owner/name`. Without it, or without
    the repository id (and, for a push, the default branch) in the stored
    event, the worker cannot tell which namespace applies and the run gets no
    cache. A ref the operator lists for file-backed secrets also gets none, so
    a credentialed run never reads what an uncredentialed one wrote.
    The answer reads only the stored event the poller built, the override
    flag and the secret refs the operator set.
    """

    if untrusted_override is not False or not isinstance(repository, str) or repository == "":
        return None
    if not isinstance(event, dict):
        return None
    stored = event.get("repository")
    ref = event.get("ref")
    if (
        not isinstance(stored, dict)
        or stored.get("full_name") != repository
        or _integer(stored.get("id")) is None
        or not isinstance(ref, str)
    ):
        return None
    if ref in tuple(secret_refs or ()):
        return None
    if event_name == "pull_request":
        pull = event.get("pull_request")
        head = _pull_repository_id(pull, "head")
        base = _pull_repository_id(pull, "base")
        if head is None or head != base or head != _integer(stored.get("id")):
            return None
        return BRANCH_NAMESPACE
    default = stored.get("default_branch")
    if (
        event_name not in _PUSH_EVENTS
        or not ref.startswith("refs/heads/")
        or not isinstance(default, str)
        or default == ""
    ):
        return None
    if ref == "refs/heads/" + default:
        return DEFAULT_BRANCH_NAMESPACE
    if event_name == "push":
        return BRANCH_NAMESPACE
    return None


_ORIGIN = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://[^/]+/|[^@/\s]+@[^:/\s]+:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


def repository_id(event):
    """Return the repository id the stored event carries, or None."""

    stored = event.get("repository") if isinstance(event, dict) else None
    return _integer(stored.get("id")) if isinstance(stored, dict) else None


def repository_from_clone(clone):
    """Return the `owner/name` the clone's `origin` remote names, or None.

    The poller builds each event for the repository it polls, and the worker's
    clone is that repository, so the remote is the identity the worker can
    check an event against when no name was configured.
    """

    try:
        done = subprocess.run(
            ["git", "-C", os.fspath(clone), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    found = _ORIGIN.match(done.stdout.strip())
    return found.group(1) if found else None


def image_key(digest):
    """Return the directory name for a `sha256:` image digest, or None."""

    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    key = digest[len("sha256:") :][:16]
    return key if _IMAGE.fullmatch(key) else None


class CacheMount:
    """Host paths of one cache and where they appear in the job."""

    def __init__(self, namespace, image, root):
        self.namespace = namespace
        self.image = image
        self.root = root
        self.shared = root / "shared"
        self.registry = root / "registry"
        self.git = root / "git"
        self.locks = tuple(root / name for name in LOCK_FILES)

    def mount_args(self, cargo_home=None):
        """`docker create` mount options. Cargo's parts need the image's cargo home."""

        pairs = [(self.shared, CACHE_ROOT)]
        if cargo_home is not None:
            pairs += [
                (self.registry, f"{cargo_home}/registry"),
                (self.git, f"{cargo_home}/git"),
                *((lock, f"{cargo_home}/{lock.name}") for lock in self.locks),
            ]
        args = []
        for source, destination in pairs:
            args.extend(["--mount", f"type=bind,source={source},destination={destination}"])
        return args

    @staticmethod
    def env_defaults():
        return dict(_ENV_DEFAULTS)

    @staticmethod
    def path_prefixes():
        return [f"{CARGO_TOOLS_ROOT}/bin", TOOLS_BIN]

    def host_paths(self):
        return (self.root, self.shared, self.registry, self.git)


_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_dir(name, dir_fd=None):
    """Open a directory and refuse a link, on every platform.

    Some systems let ``O_NOFOLLOW`` through when ``O_DIRECTORY`` is also set,
    so the entry is checked with ``lstat`` first and compared with what was
    opened, which also catches a swap between the two calls.
    """

    before = os.lstat(name, dir_fd=dir_fd)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError(errno.ELOOP, "package cache entry is not a directory")
    descriptor = os.open(name, _OPEN, dir_fd=dir_fd)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        raise OSError(errno.ELOOP, "package cache entry changed while it was opened")
    return descriptor


_MARKER = "repository"


class PackageCache:
    """The caches under one state directory, and who is using them.

    Every step walks down from the state directory through directory
    descriptors opened without following a link, so a link planted anywhere
    in the tree is refused or unlinked and never followed, whether the step
    prepares, measures or removes a cache. The cache also remembers which
    repository id it was filled for and empties itself when a different one
    reuses the state directory.
    """

    def __init__(
        self,
        state,
        *,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        max_bytes=DEFAULT_MAX_BYTES,
        clock=time.time,
    ):
        if type(max_age_seconds) is not int or max_age_seconds < 1:
            raise ValueError("package cache age must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("package cache size must be a positive integer")
        self.state = Path(state)
        self.root = self.state / CACHE_DIR
        self.max_age_seconds = max_age_seconds
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._users = {}

    def unit_path(self, namespace, image):
        if namespace not in NAMESPACES or not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ValueError("package cache name is not accepted")
        return self.root / namespace / image

    def acquire(self, namespace, image, repository):
        """Prepare one cache for a job and count the job as its user."""

        path = self.unit_path(namespace, image)
        if type(repository) is not int or repository < 1:
            raise ValueError("package cache repository id is not accepted")
        with self._lock:
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            state = self._open_state()
            try:
                root = self._open_child(state, CACHE_DIR, create=True)
                try:
                    self._bind_repository(root, repository)
                    folder = self._open_child(root, namespace, create=True)
                    try:
                        if self._users.get(path, 0) == 0:
                            self._discard_if_over(folder, image)
                        unit = self._open_child(folder, image, create=True)
                        try:
                            self._prepare(unit)
                        finally:
                            os.close(unit)
                    finally:
                        os.close(folder)
                finally:
                    os.close(root)
            finally:
                os.close(state)
            self._users[path] = self._users.get(path, 0) + 1
        return CacheMount(namespace, image, path)

    def release(self, mount):
        with self._lock:
            path = mount.root
            self._users[path] = max(0, self._users.get(path, 0) - 1)
            with contextlib.suppress(OSError):
                state = self._open_state()
                try:
                    root = self._open_child(state, CACHE_DIR)
                    try:
                        folder = self._open_child(root, mount.namespace)
                        try:
                            unit = self._open_child(folder, mount.image)
                            try:
                                self._touch(unit)
                            finally:
                                os.close(unit)
                        finally:
                            os.close(folder)
                    finally:
                        os.close(root)
                finally:
                    os.close(state)

    def prune(self):
        """Remove idle caches nobody used for the maximum age. Return their paths."""

        removed = []
        now = self._clock()
        with self._lock:
            try:
                for namespace, image, stamp, folder in self._idle():
                    if now - stamp >= self.max_age_seconds:
                        _remove_at(folder, image)
                        removed.append(self.unit_path(namespace, image))
            finally:
                self._close_idle()
        return removed

    def reclaim(self):
        """Remove the idle cache used longest ago. Return its path, or None."""

        with self._lock:
            idle = sorted(self._idle(), key=lambda item: (item[2], item[0], item[1]))
            try:
                if not idle:
                    return None
                namespace, image, _stamp, folder = idle[0]
                _remove_at(folder, image)
                return self.unit_path(namespace, image)
            finally:
                self._close_idle()

    def _idle(self):
        """List idle caches as ``(namespace, image, stamp, folder descriptor)``.

        The folder descriptors stay open until ``_close_idle`` so the caller
        removes through the descriptor it measured.
        """

        self._held = []
        found = []
        try:
            state = self._open_state()
        except OSError:
            return found
        self._held.append(state)
        try:
            root = self._open_child(state, CACHE_DIR)
        except OSError:
            return found
        self._held.append(root)
        for namespace in NAMESPACES:
            try:
                folder = self._open_child(root, namespace)
            except OSError:
                continue
            self._held.append(folder)
            for image in sorted(os.listdir(folder)):
                path = self.unit_path(namespace, image)
                if not _IMAGE.fullmatch(image) or self._users.get(path, 0):
                    continue
                stamp = self._stamp(folder, image)
                if stamp is not None:
                    found.append((namespace, image, stamp, folder))
        return found

    def _close_idle(self):
        for descriptor in getattr(self, "_held", []):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._held = []

    def _open_state(self):
        return _open_dir(self.state)

    @staticmethod
    def _open_child(parent, name, create=False):
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = _open_dir(name, parent)
        if create:
            os.fchmod(descriptor, 0o700)
        return descriptor

    def _bind_repository(self, root, repository):
        """Keep the cache for one repository id. A different id empties it first."""

        try:
            descriptor = os.open(_MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            recorded = None
        else:
            try:
                recorded = os.read(descriptor, 64).decode("ascii", "replace").strip()
            finally:
                os.close(descriptor)
        if recorded == str(repository):
            return
        if recorded is not None or os.listdir(root):
            if any(self._users.values()):
                raise OSError("package cache belongs to another repository and is in use")
            for namespace in os.listdir(root):
                if namespace == _MARKER:
                    continue
                info = os.lstat(namespace, dir_fd=root)
                if stat.S_ISDIR(info.st_mode):
                    _remove_at(root, namespace)
                else:
                    os.unlink(namespace, dir_fd=root)
        descriptor = os.open(
            _MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=root
        )
        try:
            os.write(descriptor, f"{repository}\n".encode("ascii"))
        finally:
            os.close(descriptor)

    def _prepare(self, unit):
        for name in ("registry", "git"):
            os.close(self._open_child(unit, name, create=True))
        shared = self._open_child(unit, "shared", create=True)
        try:
            for name in _SHARED_DIRS:
                parent = os.dup(shared)
                try:
                    for part in name.split("/"):
                        child = self._open_child(parent, part, create=True)
                        os.close(parent)
                        parent = child
                finally:
                    os.close(parent)
        finally:
            os.close(shared)
        for name in LOCK_FILES:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=unit
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("package cache lock is not accepted")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        self._touch(unit)

    def _touch(self, unit):
        try:
            descriptor = os.open(
                _STAMP, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=unit
            )
        except OSError:
            return
        try:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                now = self._clock()
                os.utime(descriptor, (now, now))
        finally:
            os.close(descriptor)

    @staticmethod
    def _stamp(folder, image):
        try:
            unit = _open_dir(image, folder)
        except OSError:
            return None
        try:
            return os.lstat(_STAMP, dir_fd=unit).st_mtime
        except OSError:
            return None
        finally:
            os.close(unit)

    def _discard_if_over(self, folder, image):
        try:
            unit = _open_dir(image, folder)
        except FileNotFoundError:
            return
        except OSError:
            unit = None
        size = None
        if unit is not None:
            try:
                size = _size_at(unit)
            except OSError:
                size = None
            finally:
                os.close(unit)
        if size is None or size > self.max_bytes:
            _remove_at(folder, image)


def _size_at(descriptor):
    """Bytes of a tree measured through descriptors; a link counts by its own size."""

    total = os.fstat(descriptor).st_size
    for name in os.listdir(descriptor):
        info = os.lstat(name, dir_fd=descriptor)
        if stat.S_ISDIR(info.st_mode):
            child = _open_dir(name, descriptor)
            try:
                total += _size_at(child)
            finally:
                os.close(child)
        else:
            total += info.st_size
    return total


def _remove_at(parent, name):
    """Remove ``name`` under ``parent`` without following a link anywhere in it.

    A directory is made writable only through a descriptor opened without
    following links, which a Go module cache needs because it is read-only.
    Anything that is not a directory is unlinked itself, never its target.
    """

    try:
        info = os.lstat(name, dir_fd=parent)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    descriptor = _open_dir(name, parent)
    try:
        os.fchmod(descriptor, 0o700)
        for child in os.listdir(descriptor):
            _remove_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent)
