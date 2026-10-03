"""Manifest of files a workflow attempt writes into its workspace.

The manifest lists regular files under that workspace whose bytes differ from
the captured snapshot. It does not follow symlinks, and it does not list files
outside the workspace. Byte pages use the existing 65536-byte log page.
Manifest pages use the existing list page of 100. Neither number is a new
limit.

GitHub's artifact storage quota depends on the plan: 500 MB on GitHub Free,
1 GB on GitHub Pro, 500 MB on GitHub Free for organizations, 2 GB on GitHub
Team, and 50 GB on GitHub Enterprise Cloud. That page states no single
per-file or per-job count. This worker does not apply a second quota and does
not evict. The bytes stay in the attempt workspace, which is already covered
by the configured disk budget. This is not the upload-artifact API and it
does not build a zip.
https://docs.github.com/en/actions/reference/limits
"""

import hashlib
import os
from pathlib import PurePosixPath
import stat


class ArtifactError(Exception):
    def __init__(self, missing=False):
        super().__init__("artifact bytes are not available")
        self.missing = missing


def _parts(relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        return None
    if len(relative) > 1024:
        return None
    path = PurePosixPath(relative)
    if path.is_absolute():
        return None
    parts = path.parts
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return parts


def _open_regular(root, relative):
    parts = _parts(relative)
    if parts is None:
        raise ArtifactError()
    opened = []
    root_fd = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = root_fd
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            opened.append(current)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
    except FileNotFoundError as exc:
        raise ArtifactError(missing=True) from exc
    except OSError as exc:
        raise ArtifactError() from exc
    else:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise ArtifactError()
        return file_fd
    finally:
        for fd in opened:
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)


def file_identity(root, relative):
    """Return size and SHA-256 for one regular file, without following links."""

    fd = _open_regular(root, relative)
    try:
        digest = hashlib.sha256()
        size = 0
        while block := os.read(fd, 1024 * 1024):
            size += len(block)
            digest.update(block)
        return size, digest.hexdigest()
    finally:
        os.close(fd)


def read_bytes(root, relative, offset, limit):
    fd = _open_regular(root, relative)
    try:
        info = os.fstat(fd)
        if offset > info.st_size:
            raise ArtifactError()
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, limit)
        next_offset = offset + len(data)
        return data, next_offset, next_offset == info.st_size
    finally:
        os.close(fd)


def written_files(workspace, snapshot_dir):
    """Regular files the attempt wrote, compared with the snapshot copy.

    Unchanged snapshot bytes are omitted. Symlinks are omitted. A snapshot
    entry that cannot be read safely causes that workspace path to be omitted
    rather than followed.
    """

    snapshot_files = os.path.join(snapshot_dir, "files")
    found = []
    for dirpath, _dirnames, filenames in os.walk(workspace, followlinks=False):
        for name in filenames:
            relative = os.path.relpath(os.path.join(dirpath, name), workspace)
            relative = relative.replace(os.sep, "/")
            try:
                size, digest = file_identity(workspace, relative)
            except ArtifactError:
                continue
            try:
                snap_size, snap_digest = file_identity(snapshot_files, relative)
            except ArtifactError as exc:
                if not exc.missing:
                    continue
                snap_size, snap_digest = None, None
            if size == snap_size and digest == snap_digest:
                continue
            found.append({"path": relative, "size": size, "digest": digest})
    found.sort(key=lambda item: item["path"])
    return found
