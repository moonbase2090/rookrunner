"""Preparatory Git working-file capture for M2; never executes workflows.

Snapshots contain plain files and a canonical manifest, never Git configuration,
credentials, hooks, or objects. A sibling git.json records only the sanitized
allow-list. The caller must use a private, trusted state root.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import uuid

from .protocol import canonical

MAX_FILES = 10000
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
EXCLUDED_PARTS = {
    ".git",
    ".execution-state",
    ".cache",
    ".aws",
    ".ssh",
    ".gnupg",
    ".docker",
    ".secrets",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}


class CaptureError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def reject(kind, message):
    raise CaptureError(kind, message)


def _check_base(base, algorithm):
    if base is None:
        return
    length = {"sha1": 40, "sha256": 64}[algorithm]
    if len(base) != length or any(character not in "0123456789abcdef" for character in base):
        reject("SOURCE_INVALID", "Git could not inspect the selected repository")


def _write_private(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        output = os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise
    with output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def relative_path(value):
    if not isinstance(value, str):
        reject("SOURCE_INVALID", "paths must be UTF-8 strings")
    try:
        value.encode("utf-8")
    except UnicodeError:
        reject("SOURCE_INVALID", "paths must be valid UTF-8")
    path = PurePosixPath(value)
    if (
        not value
        or "\0" in value
        or path.is_absolute()
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        reject("SOURCE_INVALID", "paths must be canonical repository-relative paths")
    return value


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        reject(
            "SOURCE_INVALID",
            "snapshot state directories must be owner-only mode 0700, not symlinks",
        )


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class SourceCapture:
    def __init__(self, repository, state):
        self.repository = Path(repository).resolve(strict=True)
        self.state = Path(state).absolute()
        self.excluded_state = None
        try:
            self.excluded_state = self.state.resolve().relative_to(self.repository).as_posix()
        except ValueError:
            pass
        if self.excluded_state == ".":
            reject("SOURCE_INVALID", "state directory cannot be the repository root")

    def git(self, *arguments, input=None, allow_failure=False):
        # Discard inherited Git redirection/config variables and avoid hooks,
        # filters, global configuration, lazy network fetches, and index writes.
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
        }
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(self.repository),
                *arguments,
            ],
            input=input,
            capture_output=True,
            env=env,
            timeout=30,
        )
        if result.returncode and not allow_failure:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        return result

    def excluded(self, path):
        if self.excluded_state and (
            path == self.excluded_state or path.startswith(self.excluded_state + "/")
        ):
            return True
        return any(
            part in EXCLUDED_PARTS
            or part == ".env"
            or part.startswith(".env.")
            or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            for part in PurePosixPath(path).parts
        )

    def inventory(self):
        root = self.git("rev-parse", "--show-toplevel").stdout.decode().rstrip("\n")
        if Path(root).resolve() != self.repository:
            reject("SOURCE_INVALID", "repository must identify the Git working-tree root")
        if (
            self.git("config", "--bool", "core.sparseCheckout", allow_failure=True).stdout.strip()
            == b"true"
        ):
            reject("CAPABILITY_UNSUPPORTED", "sparse working trees are not supported")
        index = self.git("ls-files", "--stage", "-z").stdout
        tracked = {}
        for row in index.split(b"\0"):
            if not row:
                continue
            meta, encoded = row.split(b"\t", 1)
            mode, _, stage = meta.decode("ascii").split()
            path = relative_path(encoded.decode("utf-8"))
            if stage != "0":
                reject("CAPABILITY_UNSUPPORTED", "resolve index conflicts before capture")
            if mode == "160000":
                reject("CAPABILITY_UNSUPPORTED", "submodule capture is not supported")
            tracked[path] = mode
        if len(tracked) > MAX_FILES:
            reject("SOURCE_LIMIT", "tracked file count exceeds capture limit")
        head = self.git("rev-parse", "--verify", "HEAD", allow_failure=True)
        base = head.stdout.decode("ascii").strip() if head.returncode == 0 else None
        tree = {}
        if base:
            for row in self.git("ls-tree", "-r", "-z", base).stdout.split(b"\0"):
                if not row:
                    continue
                meta, encoded = row.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
                path = relative_path(encoded.decode("utf-8"))
                if kind == "commit":
                    reject("CAPABILITY_UNSUPPORTED", "submodule history at HEAD is not supported")
                if not self.excluded(path):
                    tree[path] = [mode, oid]
        algorithm = self.git("rev-parse", "--show-object-format").stdout.decode("ascii").strip()
        if algorithm not in ("sha1", "sha256"):
            reject("CAPABILITY_UNSUPPORTED", "unsupported Git object format")
        return index, tracked, base, tree, algorithm, self.symbolic_head()

    def symbolic_head(self):
        """Return a local branch name, or None when the name is not copied.

        An unborn repository still has a symbolic ref. The caller stores null
        when the base commit is null. A detached HEAD exits 1 with no output.
        """

        result = self.git("symbolic-ref", "--quiet", "HEAD", allow_failure=True)
        if result.returncode == 1 and result.stdout == b"":
            return None
        if (
            result.returncode != 0
            or result.stdout.count(b"\n") != 1
            or not result.stdout.endswith(b"\n")
        ):
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        try:
            text = result.stdout[:-1].decode("utf-8")
        except UnicodeError:
            reject("SOURCE_INVALID", "repository metadata or paths are not supported UTF-8")
        if (
            "@" in text
            or not text.startswith("refs/heads/")
            or self.git("check-ref-format", text, allow_failure=True).returncode != 0
        ):
            return None
        return text

    @contextmanager
    def parent(self, path):
        # Open every ancestor without following links, not just the leaf.
        fd = os.open(self.repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = PurePosixPath(path).parts
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read_entry(self, path, destination, algorithm):
        with self.parent(path) as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            before = signature(info)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(leaf, dir_fd=parent)
                data = target.encode("utf-8")
                entry = {
                    "path": path,
                    "kind": "symlink",
                    "mode": "120000",
                    "target": target,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                if destination:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.symlink_to(target)
                oid = hashlib.new(algorithm, f"blob {len(data)}\0".encode() + data).hexdigest()
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_FILE_BYTES:
                    reject("SOURCE_LIMIT", "file exceeds capture byte limit")
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as source:
                    if signature(os.fstat(source.fileno())) != before:
                        reject("SOURCE_UNSTABLE", "file changed while opening")
                    digest = hashlib.sha256()
                    git_hash = hashlib.new(algorithm, f"blob {info.st_size}\0".encode())
                    size, prefix = 0, b""
                    target = None
                    if destination:
                        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target = destination.open("xb")
                    try:
                        while block := source.read(1024 * 1024):
                            size += len(block)
                            if size > MAX_FILE_BYTES:
                                reject("SOURCE_LIMIT", "file grew beyond capture byte limit")
                            if not prefix:
                                prefix = block[:256]
                            digest.update(block)
                            git_hash.update(block)
                            if target:
                                target.write(block)
                        if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
                            reject("CAPABILITY_UNSUPPORTED", "Git LFS pointers are not supported")
                        if signature(os.fstat(source.fileno())) != before:
                            reject("SOURCE_UNSTABLE", "file changed while reading")
                        mode = "100755" if info.st_mode & 0o111 else "100644"
                        if target:
                            target.flush()
                            os.fchmod(target.fileno(), 0o755 if mode == "100755" else 0o644)
                            os.fsync(target.fileno())
                    finally:
                        if target:
                            target.close()
                entry = {
                    "path": path,
                    "kind": "file",
                    "mode": mode,
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
                oid = git_hash.hexdigest()
            else:
                reject(
                    "CAPABILITY_UNSUPPORTED",
                    "only regular files and internal file symlinks are supported",
                )
            if signature(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != before:
                reject("SOURCE_UNSTABLE", "source path changed during capture")
        return entry, before, [entry["mode"], oid]

    @staticmethod
    def validate_links(entries):
        by_path = {entry["path"]: entry for entry in entries}
        for entry in entries:
            seen = set()
            while entry["kind"] == "symlink":
                if entry["path"] in seen:
                    reject("CAPABILITY_UNSUPPORTED", "cyclic symlinks are not supported")
                seen.add(entry["path"])
                target = entry["target"]
                if PurePosixPath(target).is_absolute():
                    reject("CAPABILITY_UNSUPPORTED", "absolute symlinks are not supported")
                parts = list(PurePosixPath(entry["path"]).parent.parts)
                named_component = False
                for part in target.split("/"):
                    if part in ("", "."):
                        reject(
                            "CAPABILITY_UNSUPPORTED",
                            "symlink targets must use canonical relative paths",
                        )
                    if part == "..":
                        if named_component:
                            reject(
                                "CAPABILITY_UNSUPPORTED",
                                "symlink parent traversal must precede named components",
                            )
                        if not parts:
                            reject("CAPABILITY_UNSUPPORTED", "symlink escapes the snapshot")
                        parts.pop()
                    else:
                        named_component = True
                        parts.append(part)
                entry = by_path.get("/".join(parts))
                if entry is None:
                    reject(
                        "CAPABILITY_UNSUPPORTED", "symlink must resolve to a captured regular file"
                    )

    def scan(self, paths, tracked, algorithm, destination=None):
        entries, stamps, objects, deleted = [], {}, {}, []
        total = 0
        for path in paths:
            try:
                entry, stamp, oid = self.read_entry(
                    path, destination / path if destination else None, algorithm
                )
            except FileNotFoundError:
                if destination and os.path.lexists(destination / path):
                    reject("SOURCE_UNSTABLE", "source disappeared after capture started")
                if path not in tracked:
                    reject("SOURCE_UNSTABLE", "explicit input is missing")
                deleted.append(path)
                continue
            entries.append(entry)
            stamps[path] = stamp
            objects[path] = oid
            total += entry["size"]
            if total > MAX_BYTES:
                reject("SOURCE_LIMIT", "snapshot exceeds capture byte limit")
        self.validate_links(entries)
        return entries, stamps, objects, deleted

    def filters(self, paths):
        return self.git(
            "check-attr",
            "-z",
            "--stdin",
            "filter",
            input=b"\0".join(p.encode() for p in paths) + b"\0",
        ).stdout

    def capture(self, workflow, include=()):
        workflow = relative_path(workflow)
        include = sorted({relative_path(path) for path in include})
        private_directory(self.state)
        snapshots = self.state / "snapshots"
        private_directory(snapshots)
        staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=snapshots))
        try:
            initial = self.inventory()
            _, tracked, base, tree, algorithm, head_name = initial
            if any(self.excluded(path) for path in [workflow, *include]):
                reject(
                    "SOURCE_EXCLUDED", "workflow or explicit input is excluded by capture policy"
                )
            paths = sorted((set(tracked) | set(include)) - {p for p in tracked if self.excluded(p)})
            if len(paths) > MAX_FILES:
                reject("SOURCE_LIMIT", "selected file count exceeds capture limit")
            if workflow not in paths:
                reject("SOURCE_INVALID", "workflow must be tracked or explicitly included")
            attrs = self.filters(paths)
            if any(value == b"lfs" for value in attrs.split(b"\0")[2::3]):
                reject("CAPABILITY_UNSUPPORTED", "Git LFS files are not supported")
            files = staging / "files"
            files.mkdir(mode=0o700)
            first = self.scan(paths, tracked, algorithm, files)
            second = self.scan(paths, tracked, algorithm)
            if first != second or self.inventory() != initial or self.filters(paths) != attrs:
                reject(
                    "SOURCE_UNSTABLE",
                    "source or Git selection changed during capture; retry explicitly",
                )
            entries, _, objects, deleted = first
            deleted = sorted(set(deleted) | (set(tree) - set(objects)))
            selected = next((e for e in entries if e["path"] == workflow), None)
            if not selected or selected["kind"] != "file":
                reject("SOURCE_INVALID", "workflow must be a captured regular file")
            _check_base(base, algorithm)
            manifest = {
                "format_version": 1,
                "base_commit": base,
                "dirty": objects != tree,
                "git_object_format": algorithm,
                "workflow": workflow,
                "workflow_digest": selected["sha256"],
                "included": include,
                "excluded": sorted(p for p in tracked if self.excluded(p)),
                "deleted": deleted,
                "entries": entries,
            }
            encoded = canonical(manifest).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            with (staging / "manifest.json").open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            metadata = {
                "format_version": 1,
                "base_commit": base,
                "dirty": manifest["dirty"],
                "git_object_format": algorithm,
                "head": None if base is None else head_name,
            }
            git_encoded = canonical(metadata).encode()
            _write_private(staging / "git.json", git_encoded)
            for directory, _, _ in os.walk(staging, topdown=False, followlinks=False):
                sync_directory(directory)
            snapshot_id = str(uuid.uuid4())
            staging.rename(snapshots / snapshot_id)
            sync_directory(snapshots)
            return {
                "snapshot_id": snapshot_id,
                "digest": digest,
                "workflow_digest": selected["sha256"],
                "base_commit": base,
                "dirty": manifest["dirty"],
                "file_count": len(entries),
                "total_bytes": sum(e["size"] for e in entries),
                "git_metadata_digest": hashlib.sha256(git_encoded).hexdigest(),
            }
        except CaptureError:
            raise
        except (UnicodeError, ValueError):
            reject("SOURCE_INVALID", "repository metadata or paths are not supported UTF-8")
        except subprocess.TimeoutExpired:
            reject("SOURCE_UNSTABLE", "Git inspection timed out")
        except OSError:
            reject("SOURCE_IO_ERROR", "source capture could not read or persist a safe snapshot")
        finally:
            if staging.exists():
                shutil.rmtree(staging)
