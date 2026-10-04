"""Materialize a private attempt workspace from a verified snapshot.

The workspace is a new directory. It is not the snapshot tree. Bytes are copied
from the snapshot, not from the original checkout. When the snapshot has an
object store, the workspace also receives one owned Git directory assembled
from that store and git.json. A failure removes the partial workspace.
"""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import zlib

from .verify import VerifyError, read_git_metadata, verify_snapshot

_FILE_MODES = {"100644": 0o644, "100755": 0o755}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class AttemptError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _invalid(message):
    raise AttemptError("ATTEMPT_INVALID", message)


def _failed(message):
    raise AttemptError("ATTEMPT_FAILED", message)


def _inside(path, root):
    try:
        Path(path).absolute().relative_to(Path(root).absolute())
    except ValueError:
        return False
    return True


def _reject_destination(snapshot_dir, workspace):
    snapshot = Path(snapshot_dir)
    target = Path(workspace)
    if target.name in ("", ".", ".."):
        _invalid("workspace must be a new directory outside the snapshot")
    parent = target.parent
    try:
        parent_real = parent.resolve(strict=True)
        snapshot_real = snapshot.resolve(strict=True)
    except OSError:
        _invalid("workspace must be a new directory outside the snapshot")
    if (
        _inside(target, snapshot)
        or _inside(target, snapshot_real)
        or _inside(parent_real, snapshot_real)
        or parent_real == snapshot_real
    ):
        _invalid("workspace must be a new directory outside the snapshot")
    try:
        os.lstat(target)
    except FileNotFoundError:
        return
    except OSError:
        _invalid("workspace must be a new directory outside the snapshot")
    _invalid("workspace must be a new directory outside the snapshot")


def _create_private(workspace):
    parent = Path(workspace).parent
    try:
        parent_fd = os.open(parent, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
            _invalid("workspace must be a new directory outside the snapshot")
        _failed("attempt workspace could not be materialized")
    made = False
    try:
        try:
            try:
                os.mkdir(workspace.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                _invalid("workspace must be a new directory outside the snapshot")
            made = True
            child = os.open(workspace.name, _DIR_FLAGS, dir_fd=parent_fd)
            try:
                os.fchmod(child, 0o700)
                info = os.fstat(child)
                private = stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                if not private or info.st_uid != os.getuid():
                    _failed("attempt workspace could not be materialized")
            finally:
                os.close(child)
        except Exception:
            if made:
                _remove_tree(workspace)
            raise
    finally:
        os.close(parent_fd)


def _remove_tree(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    fd = os.open(path, _DIR_FLAGS)
    try:
        names = os.listdir(fd)
    finally:
        os.close(fd)
    for name in names:
        _remove_tree(Path(path) / name)
    os.rmdir(path)


def _open_dir(path):
    try:
        return os.open(path, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
            _failed("refusing to follow a symlink")
        _failed("attempt workspace could not be materialized")


@contextmanager
def _parent(root_fd, relative, create):
    """Yield the parent directory fd for relative's final component."""

    parts = PurePosixPath(relative).parts
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.lstat(part, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            if create:
                os.fchmod(child, 0o700)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _write_all(fd, block):
    view = memoryview(block)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            _failed("attempt workspace could not be materialized")
        view = view[written:]


def _copy_file(src_parent, dst_parent, leaf, entry):
    try:
        src = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
            _failed("refusing to follow a symlink")
        _failed("attempt workspace could not be materialized")
    try:
        info = os.fstat(src)
        mode = _FILE_MODES[entry["mode"]]
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
            _failed("snapshot changed during materialization")
        try:
            dst = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=dst_parent,
            )
        except OSError:
            _failed("attempt workspace could not be materialized")
        try:
            digest = hashlib.sha256()
            size = 0
            while block := os.read(src, 1024 * 1024):
                size += len(block)
                if size > entry["size"]:
                    _failed("snapshot changed during materialization")
                _write_all(dst, block)
                digest.update(block)
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                _failed("snapshot changed during materialization")
            os.fchmod(dst, mode)
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _copy_link(src_parent, dst_parent, leaf, entry):
    try:
        target = os.readlink(leaf, dir_fd=src_parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
            _failed("refusing to follow a symlink")
        _failed("attempt workspace could not be materialized")
    data = target.encode("utf-8")
    if target != entry["target"] or len(data) != entry["size"]:
        _failed("snapshot changed during materialization")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        _failed("snapshot changed during materialization")
    if PurePosixPath(target).is_absolute():
        _failed("refusing to follow a symlink")
    try:
        os.symlink(target, leaf, dir_fd=dst_parent)
    except OSError:
        _failed("attempt workspace could not be materialized")


def _config(algorithm):
    version = "0" if algorithm == "sha1" else "1"
    text = (
        "[core]\n"
        f"\trepositoryformatversion = {version}\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = false\n"
        "\tignorecase = false\n"
    )
    if algorithm == "sha256":
        text += "[extensions]\n\tobjectformat = sha256\n"
    return text.encode("ascii")


def _safe_head(head):
    if head is None:
        return True
    path = PurePosixPath(".git") / head
    if any(part in ("", ".", "..") for part in path.parts):
        return False
    try:
        path.relative_to(PurePosixPath(".git/refs/heads"))
    except ValueError:
        return False
    return len(path.parts) >= 4


def _prepare_git(snapshot_dir):
    """Read the owned Git directory, or return None when the store is absent.

    Raises VerifyError or AttemptError before a workspace exists.
    """

    objects = Path(snapshot_dir) / "objects"
    try:
        info = os.lstat(objects)
    except FileNotFoundError:
        return None
    except OSError:
        _failed("object store is not accepted")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _failed("object store is not accepted")
    metadata = read_git_metadata(snapshot_dir)
    if metadata is None or metadata["base_commit"] is None or not _safe_head(metadata["head"]):
        raise VerifyError("SNAPSHOT_INVALID", "git metadata is not accepted")
    return metadata, *_read_store(objects, metadata["git_object_format"])


def _read_store(objects, algorithm):
    loose = {}
    kinds = {}
    commits = []
    width = {"sha1": 40, "sha256": 64}[algorithm]
    try:
        buckets = list(os.scandir(objects))
    except OSError:
        _failed("object store is not accepted")
    for bucket in buckets:
        try:
            bucket_info = bucket.stat(follow_symlinks=False)
        except OSError:
            _failed("object store is not accepted")
        name = bucket.name
        if (
            stat.S_ISLNK(bucket_info.st_mode)
            or not stat.S_ISDIR(bucket_info.st_mode)
            or len(name) != 2
            or any(character not in "0123456789abcdef" for character in name)
        ):
            _failed("object store is not accepted")
        try:
            leaves = list(os.scandir(bucket.path))
        except OSError:
            _failed("object store is not accepted")
        for leaf in leaves:
            try:
                leaf_info = leaf.stat(follow_symlinks=False)
            except OSError:
                _failed("object store is not accepted")
            if stat.S_ISLNK(leaf_info.st_mode) or not stat.S_ISREG(leaf_info.st_mode):
                _failed("object store is not accepted")
            oid = name + leaf.name
            if len(oid) != width or any(character not in "0123456789abcdef" for character in oid):
                _failed("object store is not accepted")
            try:
                descriptor = os.open(leaf.path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                _failed("object store is not accepted")
            try:
                chunks = []
                while block := os.read(descriptor, 1024 * 1024):
                    chunks.append(block)
            finally:
                os.close(descriptor)
            stored = b"".join(chunks)
            try:
                raw = zlib.decompress(stored)
            except zlib.error:
                _failed("object store is not accepted")
            if hashlib.new(algorithm, raw).hexdigest() != oid:
                _failed("object store is not accepted")
            header, payload = raw.split(b"\0", 1)
            kind, separator, size = header.partition(b" ")
            if separator != b" " or not size.isdigit() or int(size) != len(payload):
                _failed("object store is not accepted")
            if kind not in (b"blob", b"tree", b"commit"):
                _failed("object store is not accepted")
            loose[oid] = stored
            kinds[oid] = kind
            if kind == b"commit":
                commits.append((oid, payload))
    if len(commits) != 1:
        _failed("object store is not accepted")
    commit_id, payload = commits[0]
    if not payload.startswith(b"tree ") or b"\n" not in payload:
        _failed("object store is not accepted")
    try:
        tree_id = payload.split(b"\n", 1)[0][len(b"tree ") :].decode("ascii")
    except UnicodeError:
        _failed("object store is not accepted")
    expected = (
        f"tree {tree_id}\n"
        "author Rookrunner <rookrunner@example.invalid> 0 +0000\n"
        "committer Rookrunner <rookrunner@example.invalid> 0 +0000\n"
        "\n"
        "captured tree\n"
    ).encode("ascii")
    if payload != expected or kinds.get(tree_id) != b"tree":
        _failed("object store is not accepted")
    return commit_id, loose


def _ensure_dir(parent_fd, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    os.fchmod(descriptor, 0o700)
    return descriptor


def _write_private(parent_fd, name, payload):
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_objects(git_fd, loose):
    objects = _ensure_dir(git_fd, "objects")
    try:
        buckets = {}
        for oid, payload in loose.items():
            buckets.setdefault(oid[:2], []).append((oid[2:], payload))
        for name, files in buckets.items():
            bucket = _ensure_dir(objects, name)
            try:
                for leaf, payload in files:
                    _write_private(bucket, leaf, payload)
                os.fsync(bucket)
            finally:
                os.close(bucket)
        os.fsync(objects)
    finally:
        os.close(objects)


def _write_ref(git_fd, head, payload):
    descriptor = os.dup(git_fd)
    try:
        parts = head.split("/")
        for part in parts[:-1]:
            child = _ensure_dir(descriptor, part)
            os.close(descriptor)
            descriptor = child
        _write_private(descriptor, parts[-1], payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_git(workspace, metadata, commit_id, loose):
    workspace_fd = _open_dir(workspace)
    try:
        git_fd = _ensure_dir(workspace_fd, ".git")
        try:
            _write_objects(git_fd, loose)
            _write_private(git_fd, "config", _config(metadata["git_object_format"]))
            identity = (commit_id + "\n").encode("ascii")
            head = metadata["head"]
            if head is None:
                _write_private(git_fd, "HEAD", identity)
                refs = _ensure_dir(git_fd, "refs")
                os.close(refs)
            else:
                _write_private(git_fd, "HEAD", f"ref: {head}\n".encode("ascii"))
                _write_ref(git_fd, head, identity)
            os.fsync(git_fd)
        finally:
            os.close(git_fd)
        os.fsync(workspace_fd)
    finally:
        os.close(workspace_fd)


def _read_tree(workspace):
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
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(workspace),
                "read-tree",
                "HEAD",
            ],
            env=env,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        _failed("attempt workspace could not be materialized")
    if result.returncode != 0:
        _failed("attempt workspace could not be materialized")


def _fill(snapshot_dir, workspace, entries):
    files_fd = _open_dir(Path(snapshot_dir) / "files")
    workspace_fd = _open_dir(workspace)
    try:
        for entry in entries:
            with _parent(files_fd, entry["path"], create=False) as (src_parent, leaf):
                with _parent(workspace_fd, entry["path"], create=True) as (dst_parent, dst_leaf):
                    if entry["kind"] == "symlink":
                        _copy_link(src_parent, dst_parent, leaf, entry)
                    else:
                        _copy_file(src_parent, dst_parent, dst_leaf, entry)
        os.fsync(workspace_fd)
    finally:
        os.close(files_fd)
        os.close(workspace_fd)


def materialize_attempt(snapshot_dir, digest, workspace):
    """Copy a verified snapshot into a new private workspace directory.

    Raises VerifyError when the snapshot does not match `digest`, or when an
    object store has no acceptable sibling, before creating `workspace`.
    Raises AttemptError for a rejected path, a rejected store, or a failed
    copy. The original checkout is not read.
    """

    manifest = verify_snapshot(snapshot_dir, digest)
    _reject_destination(snapshot_dir, workspace)
    prepared = _prepare_git(snapshot_dir)
    created = False
    try:
        _create_private(workspace)
        created = True
        _fill(snapshot_dir, workspace, manifest["entries"])
        if prepared is not None:
            _write_git(workspace, *prepared)
            _read_tree(workspace)
    except Exception as exc:
        if created:
            _remove_tree(workspace)
            if not isinstance(exc, AttemptError):
                raise AttemptError(
                    "ATTEMPT_FAILED", "attempt workspace could not be materialized"
                ) from exc
        raise
    return manifest
