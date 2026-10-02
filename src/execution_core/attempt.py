"""Materialize a private attempt workspace from a verified snapshot.

The workspace is a new directory. It is not the snapshot tree. Bytes are copied
from the snapshot, not from the original checkout. A failure removes the
partial workspace.
"""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from .verify import verify_snapshot

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

    Raises VerifyError when the snapshot does not match `digest`, before
    creating `workspace`. Raises AttemptError for a rejected path or a failed
    copy. The original checkout is not read.
    """

    manifest = verify_snapshot(snapshot_dir, digest)
    _reject_destination(snapshot_dir, workspace)
    created = False
    try:
        _create_private(workspace)
        created = True
        _fill(snapshot_dir, workspace, manifest["entries"])
    except Exception as exc:
        if created:
            _remove_tree(workspace)
            if not isinstance(exc, AttemptError):
                raise AttemptError(
                    "ATTEMPT_FAILED", "attempt workspace could not be materialized"
                ) from exc
        raise
    return manifest
