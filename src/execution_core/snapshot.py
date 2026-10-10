# SPDX-License-Identifier: MPL-2.0

"""Preparatory Git working-file capture for M2; never executes workflows.

Snapshots contain plain files, a canonical manifest, the loose trees and
blobs of the captured base commit, and one synthesized commit for that
tree. The default capture does not store the original commit. History,
when the caller asks for it before publish, also stores that commit and
every ancestor reachable from it. The synthesized commit then names that
commit as its parent. Snapshots do not contain Git configuration, hooks,
or a copied repository directory. A sibling git.json records only the
sanitized allow-list. The caller must use a private, trusted state root.
"""

from contextlib import contextmanager, suppress
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import uuid
import zlib

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


def _loose_object(kind, payload, algorithm, oid):
    raw = f"{kind} {len(payload)}\0".encode("ascii") + payload
    if hashlib.new(algorithm, raw).hexdigest() != oid:
        reject("SOURCE_INVALID", "Git could not inspect the selected repository")
    return zlib.compress(raw)


def _synthesized_commit(root_tree, algorithm, parent=None):
    """Id and payload of the fixed commit for this root tree.

    The name, email, timestamp, and message are not read from the original
    commit, Git configuration, or the environment. Unix time 0 keeps the id
    a pure function of the tree, plus the parent when history is stored.
    https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
    """

    payload = f"tree {root_tree}\n"
    if parent is not None:
        payload += f"parent {parent}\n"
    payload += (
        "author Rookrunner <rookrunner@example.invalid> 0 +0000\n"
        "committer Rookrunner <rookrunner@example.invalid> 0 +0000\n"
        "\n"
        "captured tree\n"
    )
    encoded = payload.encode("ascii")
    raw = f"commit {len(encoded)}\0".encode("ascii") + encoded
    return hashlib.new(algorithm, raw).hexdigest(), encoded


def _commit_parents(payload, algorithm):
    """Return parent ids named in a commit header. A bad id is rejected."""

    parents = []
    header = payload.split(b"\n\n", 1)[0]
    for line in header.split(b"\n"):
        if not line.startswith(b"parent "):
            continue
        try:
            parent = line[len(b"parent ") :].decode("ascii")
        except UnicodeError:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        _check_base(parent, algorithm)
        parents.append(parent)
    return parents


def _one_oid(raw, algorithm):
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        reject("SOURCE_INVALID", "Git could not inspect the selected repository")
    try:
        text = raw[:-1].decode("ascii")
    except UnicodeError:
        reject("SOURCE_INVALID", "Git could not inspect the selected repository")
    _check_base(text, algorithm)
    return text


def _classify_descriptor(fd):
    info = os.fstat(fd)
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if not stat.S_ISREG(info.st_mode):
        return "other"
    if info.st_size == 0:
        return "empty"
    return "nonempty"


def _read_regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, info.st_size)
    if len(data) != info.st_size:
        return None
    return data


def _open_at(parent, name, *, directory):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        return os.open(name, flags, dir_fd=parent), None
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "symlink"
        if exc.errno in (errno.ENOTDIR, errno.EISDIR):
            return None, "other"
        raise


def _walk(start, parts, *, final_directory):
    fd = os.dup(start)
    try:
        for index, part in enumerate(parts):
            last = index + 1 == len(parts)
            opened, kind = _open_at(fd, part, directory=final_directory if last else True)
            if opened is None:
                os.close(fd)
                return None, kind
            os.close(fd)
            fd = opened
        return fd, None
    except BaseException:
        os.close(fd)
        raise


def _directory_from_text(start, text):
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if text.startswith("/"):
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            return _walk(root, parts, final_directory=True)
        finally:
            os.close(root)
    return _walk(start, parts, final_directory=True)


def _gitdir_text(data):
    prefix = b"gitdir: "
    if not data.startswith(prefix) or data.count(b"\n") != 1 or not data.endswith(b"\n"):
        return None
    try:
        text = data[len(prefix) : -1].decode("utf-8")
    except UnicodeError:
        return None
    if not text or "\0" in text:
        return None
    return text


def _alternates_in(fd):
    found, kind = _walk(fd, ("objects", "info", "alternates"), final_directory=False)
    if found is None:
        return kind
    try:
        return _classify_descriptor(found)
    finally:
        os.close(found)


def _common_alternates(git_fd):
    opened, kind = _open_at(git_fd, "commondir", directory=False)
    if opened is None:
        if kind == "absent":
            return _alternates_in(git_fd)
        return kind
    try:
        data = _read_regular(opened)
    finally:
        os.close(opened)
    if data is None or data.count(b"\n") != 1 or not data.endswith(b"\n"):
        return "other"
    try:
        text = data[:-1].decode("utf-8")
    except UnicodeError:
        return "other"
    if not text or "\0" in text:
        return "other"
    common, kind = _directory_from_text(git_fd, text)
    if common is None:
        return kind
    try:
        return _alternates_in(common)
    finally:
        os.close(common)


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
        with suppress(ValueError):
            self.excluded_state = self.state.resolve().relative_to(self.repository).as_posix()
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
        algorithm = self.git("rev-parse", "--show-object-format").stdout.decode("ascii").strip()
        if algorithm not in ("sha1", "sha256"):
            reject("CAPABILITY_UNSUPPORTED", "unsupported Git object format")
        tree = {}
        root_tree = None
        object_ids = None
        if base:
            _check_base(base, algorithm)
            root_tree = _one_oid(
                self.git("rev-parse", "--verify", f"{base}^{{tree}}").stdout, algorithm
            )
            # -t lists intermediate trees. The root id comes from rev-parse.
            # https://git-scm.com/docs/git-ls-tree
            excluded_in_tree = False
            oids = [root_tree]
            for row in self.git("ls-tree", "-r", "-t", "-z", base).stdout.split(b"\0"):
                if not row:
                    continue
                meta, encoded = row.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
                path = relative_path(encoded.decode("utf-8"))
                if kind == "commit":
                    reject("CAPABILITY_UNSUPPORTED", "submodule history at HEAD is not supported")
                if kind not in ("blob", "tree"):
                    reject("SOURCE_INVALID", "Git could not inspect the selected repository")
                _check_base(oid, algorithm)
                if self.excluded(path):
                    excluded_in_tree = True
                elif kind == "blob":
                    tree[path] = [mode, oid]
                oids.append(oid)
            if not excluded_in_tree:
                object_ids = tuple(sorted(set(oids)))
        return (
            index,
            tracked,
            base,
            tree,
            algorithm,
            self.symbolic_head(),
            root_tree,
            object_ids,
            self._alternates(),
        )

    def _alternates(self):
        """Classify the object alternates file without following a symlink.

        A linked worktree stores objects in the common Git directory. The
        file is not read and is not copied.
        https://git-scm.com/docs/git
        """

        root = os.open(self.repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            git_fd, kind = _open_at(root, ".git", directory=False)
            if git_fd is None:
                return kind
            try:
                info = os.fstat(git_fd)
                if stat.S_ISDIR(info.st_mode):
                    return _alternates_in(git_fd)
                if stat.S_ISLNK(info.st_mode):
                    return "symlink"
                if not stat.S_ISREG(info.st_mode):
                    return "other"
                data = _read_regular(git_fd)
                if data is None:
                    return "other"
                gitdir = _gitdir_text(data)
                if gitdir is None:
                    return "other"
                admin, kind = _directory_from_text(root, gitdir)
                if admin is None:
                    return kind
                try:
                    return _common_alternates(admin)
                finally:
                    os.close(admin)
            finally:
                os.close(git_fd)
        finally:
            os.close(root)

    def _objects(self, oids, algorithm, payloads=None, allow_commits=False):
        """Read object payloads. A missing object is not fetched.

        The default batch accepts trees and blobs. History also accepts
        commits and applies the blob size limit to every new payload.
        https://git-scm.com/docs/git-cat-file
        """

        requested = list(oids)
        if not requested:
            return {}
        allowed = (b"blob", b"tree", b"commit") if allow_commits else (b"blob", b"tree")
        result = self.git(
            "cat-file",
            "--batch",
            input="".join(f"{oid}\n" for oid in requested).encode("ascii"),
        )
        data = result.stdout
        found = {}
        offset = 0
        for oid in requested:
            newline = data.find(b"\n", offset)
            if newline < 0:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            header = data[offset:newline]
            offset = newline + 1
            parts = header.split(b" ")
            if len(parts) == 2 and parts[1] == b"missing":
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            if len(parts) != 3:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            try:
                reported = parts[0].decode("ascii")
            except UnicodeError:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            if reported != oid or parts[1] not in allowed:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            try:
                size = int(parts[2])
            except ValueError:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            if (
                size < 0
                or offset + size >= len(data)
                or data[offset + size : offset + size + 1] != b"\n"
            ):
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            if size > MAX_FILE_BYTES and (parts[1] == b"blob" or allow_commits):
                reject("SOURCE_LIMIT", "blob exceeds capture byte limit")
            payload = data[offset : offset + size]
            offset += size + 1
            if payloads is not None:
                payloads[oid] = (parts[1].decode("ascii"), payload)
            found[oid] = _loose_object(parts[1].decode("ascii"), payload, algorithm, oid)
        if offset != len(data):
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        return found

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
        """Stage and publish a parentless snapshot. History is not copied."""

        with self.prepare(workflow, include) as prepared:
            return prepared.publish()

    @contextmanager
    def prepare(self, workflow, include=()):
        """Stage a snapshot and publish it only when the caller says so.

        The caller can plan the staged tree and add history before publish.
        Leaving the block without publishing removes the staging directory.
        Exceptions from the caller are not turned into capture errors.
        """

        workflow = relative_path(workflow)
        include = sorted({relative_path(path) for path in include})
        private_directory(self.state)
        snapshots = self.state / "snapshots"
        private_directory(snapshots)
        staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=snapshots))
        prepared = None
        try:
            try:
                prepared = self._stage(workflow, include, staging, snapshots)
            except CaptureError:
                raise
            except (UnicodeError, ValueError):
                reject("SOURCE_INVALID", "repository metadata or paths are not supported UTF-8")
            except subprocess.TimeoutExpired:
                reject("SOURCE_UNSTABLE", "Git inspection timed out")
            except OSError:
                reject(
                    "SOURCE_IO_ERROR", "source capture could not read or persist a safe snapshot"
                )
            yield prepared
        finally:
            if (prepared is None or not prepared.published) and staging.exists():
                shutil.rmtree(staging)

    def _stage(self, workflow, include, staging, snapshots):
        initial = self.inventory()
        (
            _,
            tracked,
            base,
            tree,
            algorithm,
            head_name,
            root_tree,
            object_ids,
            alternates,
        ) = initial
        if any(self.excluded(path) for path in [workflow, *include]):
            reject("SOURCE_EXCLUDED", "workflow or explicit input is excluded by capture policy")
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
        if alternates == "nonempty":
            reject("CAPABILITY_UNSUPPORTED", "object alternates are not supported")
        if alternates not in ("absent", "empty"):
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
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
        objects_root = None
        commit_id = None
        objects_canonical = None
        if object_ids is not None:
            # Computed after the inventory reads agree. The original commit
            # is not read, and this id is not added to the cat-file batch.
            commit_id, commit_payload = _synthesized_commit(root_tree, algorithm)
            commit_loose = _loose_object("commit", commit_payload, algorithm, commit_id)
            stored_ids = tuple(sorted((*object_ids, commit_id)))
            objects_root = staging / "objects"
            objects_root.mkdir(mode=0o700)
            for oid, payload in self._objects(object_ids, algorithm).items():
                _store_loose(objects_root, oid, payload)
            _store_loose(objects_root, commit_id, commit_loose)
            objects_canonical = canonical(list(stored_ids)).encode()
        return _PreparedCapture(
            self,
            staging,
            snapshots,
            base=base,
            algorithm=algorithm,
            root_tree=root_tree,
            object_ids=object_ids,
            objects_root=objects_root,
            commit_id=commit_id,
            objects_canonical=objects_canonical,
            digest=digest,
            selected=selected,
            entries=entries,
            git_encoded=git_encoded,
            dirty=manifest["dirty"],
        )

    def _add_history(self, prepared):
        try:
            self._copy_history(prepared)
        except CaptureError:
            raise
        except (UnicodeError, ValueError):
            reject("SOURCE_INVALID", "repository metadata or paths are not supported UTF-8")
        except subprocess.TimeoutExpired:
            reject("SOURCE_UNSTABLE", "Git inspection timed out")
        except OSError:
            reject("SOURCE_IO_ERROR", "source capture could not read or persist a safe snapshot")

    def _copy_history(self, prepared):
        """Copy the ancestor closure into the staging store before publish.

        A missing parent, including one absent from a shallow repository, is
        not fetched. An unborn repository has no base commit. Either failure
        leaves the staging directory unpublished.
        """

        if (
            prepared.base is None
            or prepared.object_ids is None
            or prepared.root_tree is None
            or prepared.objects_root is None
            or prepared.commit_id is None
        ):
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        listed = self._history_oids(prepared.base, prepared.algorithm)
        if prepared.base not in listed:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        already = set(prepared.object_ids)
        needed = [oid for oid in listed if oid not in already]
        payloads = {}
        found = self._objects(needed, prepared.algorithm, payloads=payloads, allow_commits=True)
        commits = {oid for oid, (kind, _payload) in payloads.items() if kind == "commit"}
        if prepared.base not in commits:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        for oid in commits:
            for parent in _commit_parents(payloads[oid][1], prepared.algorithm):
                if parent not in commits:
                    reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        parented_id, parented_payload = _synthesized_commit(
            prepared.root_tree, prepared.algorithm, prepared.base
        )
        if parented_id in already or parented_id in found or parented_id == prepared.commit_id:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        _remove_loose(prepared.objects_root, prepared.commit_id)
        for oid, loose in found.items():
            _store_loose(prepared.objects_root, oid, loose)
        loose = _loose_object("commit", parented_payload, prepared.algorithm, parented_id)
        _store_loose(prepared.objects_root, parented_id, loose)
        stored = tuple(sorted((*already, *found, parented_id)))
        prepared.objects_canonical = canonical(list(stored)).encode()
        prepared.commit_id = parented_id

    def _history_oids(self, base, algorithm):
        raw = self.git("rev-list", "--objects", base).stdout
        found = []
        seen = set()
        for line in raw.splitlines():
            if not line:
                continue
            try:
                oid = line.split(b" ", 1)[0].decode("ascii")
            except UnicodeError:
                reject("SOURCE_INVALID", "Git could not inspect the selected repository")
            _check_base(oid, algorithm)
            if oid in seen:
                continue
            seen.add(oid)
            found.append(oid)
        return found

    def _publish(self, prepared):
        if prepared.published:
            reject("SOURCE_INVALID", "Git could not inspect the selected repository")
        try:
            for directory, _, _ in os.walk(prepared.staging, topdown=False, followlinks=False):
                sync_directory(directory)
            snapshot_id = str(uuid.uuid4())
            prepared.staging.rename(prepared.snapshots / snapshot_id)
            prepared.published = True
            sync_directory(prepared.snapshots)
        except CaptureError:
            raise
        except OSError:
            reject("SOURCE_IO_ERROR", "source capture could not read or persist a safe snapshot")
        objects_canonical = prepared.objects_canonical
        return {
            "snapshot_id": snapshot_id,
            "digest": prepared.digest,
            "workflow_digest": prepared.selected["sha256"],
            "base_commit": prepared.base,
            "dirty": prepared.dirty,
            "file_count": len(prepared.entries),
            "total_bytes": sum(entry["size"] for entry in prepared.entries),
            "git_metadata_digest": hashlib.sha256(prepared.git_encoded).hexdigest(),
            "git_objects_digest": (
                None if objects_canonical is None else hashlib.sha256(objects_canonical).hexdigest()
            ),
        }


class _PreparedCapture:
    """Staged snapshot. publish() renames it. History is optional before that."""

    def __init__(self, owner, staging, snapshots, **fields):
        self.owner = owner
        self.staging = staging
        self.snapshots = snapshots
        self.published = False
        self.__dict__.update(fields)

    @property
    def path(self):
        return self.staging

    def add_history(self):
        self.owner._add_history(self)

    def publish(self):
        return self.owner._publish(self)


def _store_loose(root, oid, payload):
    bucket = root / oid[:2]
    bucket.mkdir(mode=0o700, exist_ok=True)
    _write_private(bucket / oid[2:], payload)


def _remove_loose(root, oid):
    leaf = root / oid[:2] / oid[2:]
    leaf.unlink()
    bucket = leaf.parent
    if not any(bucket.iterdir()):
        bucket.rmdir()
