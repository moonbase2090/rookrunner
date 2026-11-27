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

import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from .disk import usage

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


def namespace_for(event, event_name, repository, untrusted_override=False):
    """Return the namespace a run may use, or None when it may use no cache.

    `repository` is the worker's `owner/name` when it is configured, and
    None when it is not. The answer reads only the stored event the poller
    built and the override flag the operator set.
    """

    if untrusted_override is not False:
        return None
    if not isinstance(event, dict):
        return None
    stored = event.get("repository")
    ref = event.get("ref")
    if (
        not isinstance(stored, dict)
        or (repository is not None and stored.get("full_name") != repository)
        or not isinstance(ref, str)
    ):
        return None
    if event_name == "pull_request":
        pull = event.get("pull_request")
        head = _pull_repository_id(pull, "head")
        base = _pull_repository_id(pull, "base")
        if head is None or head != base or head != _integer(stored.get("id")):
            return None
        return BRANCH_NAMESPACE
    if event_name not in _PUSH_EVENTS or not ref.startswith("refs/heads/"):
        return None
    default = stored.get("default_branch")
    if isinstance(default, str) and default != "" and ref == "refs/heads/" + default:
        return DEFAULT_BRANCH_NAMESPACE
    if event_name == "push":
        return BRANCH_NAMESPACE
    return None


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


class PackageCache:
    """The caches under one state directory, and who is using them."""

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
        self.root = Path(state) / CACHE_DIR
        self.max_age_seconds = max_age_seconds
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._users = {}

    def unit_path(self, namespace, image):
        if namespace not in NAMESPACES or not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ValueError("package cache name is not accepted")
        return self.root / namespace / image

    def acquire(self, namespace, image):
        """Prepare one cache for a job and count the job as its user."""

        path = self.unit_path(namespace, image)
        with self._lock:
            if self._users.get(path, 0) == 0:
                self._discard_if_over(path)
            self._prepare(path)
            self._users[path] = self._users.get(path, 0) + 1
        return CacheMount(namespace, image, path)

    def release(self, mount):
        with self._lock:
            path = mount.root
            self._users[path] = max(0, self._users.get(path, 0) - 1)
            self._touch(path)

    def prune(self):
        """Remove idle caches nobody used for the maximum age. Return their paths."""

        removed = []
        now = self._clock()
        with self._lock:
            for path, stamp in self._idle():
                if now - stamp >= self.max_age_seconds:
                    self._remove(path)
                    removed.append(path)
        return removed

    def reclaim(self):
        """Remove the idle cache used longest ago. Return its path, or None."""

        with self._lock:
            idle = sorted(self._idle(), key=lambda item: (item[1], str(item[0])))
            if not idle:
                return None
            self._remove(idle[0][0])
            return idle[0][0]

    def _idle(self):
        found = []
        for namespace in NAMESPACES:
            folder = self.root / namespace
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if not _IMAGE.fullmatch(path.name) or self._users.get(path, 0):
                    continue
                stamp = self._stamp(path)
                if stamp is not None:
                    found.append((path, stamp))
        return found

    def _prepare(self, path):
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.root, path.parent, path):
            self._directory(directory)
        for name in ("registry", "git", "shared"):
            self._directory(path / name)
        for name in _SHARED_DIRS:
            parent = path / "shared"
            for part in name.split("/"):
                parent = parent / part
                self._directory(parent)
        for name in LOCK_FILES:
            lock = path / name
            if lock.is_symlink() or (lock.exists() and not lock.is_file()):
                raise OSError("package cache lock is not accepted")
            lock.touch(mode=0o600, exist_ok=True)
        self._touch(path)

    @staticmethod
    def _directory(path):
        if path.is_symlink():
            raise OSError("package cache directory is not accepted")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)

    def _touch(self, path):
        if path.is_dir() and not path.is_symlink():
            stamp = path / _STAMP
            stamp.touch(mode=0o600, exist_ok=True)
            now = self._clock()
            os.utime(stamp, (now, now))

    @staticmethod
    def _stamp(path):
        try:
            return (path / _STAMP).lstat().st_mtime
        except OSError:
            return None

    def _discard_if_over(self, path):
        try:
            size = usage(path)
        except FileNotFoundError:
            return
        except OSError:
            size = None
        if size is None or size > self.max_bytes:
            self._remove(path)

    @staticmethod
    def _remove(path):
        if path.is_symlink():
            path.unlink()
            return

        def writable(function, name, _error):
            # A Go module cache is read-only on purpose. Make the entry
            # removable, then try that one removal again.
            os.chmod(os.path.dirname(name), 0o700)
            os.chmod(name, 0o700)
            function(name)

        if path.exists():
            handler = {"onexc" if sys.version_info >= (3, 12) else "onerror": writable}
            shutil.rmtree(path, **handler)
