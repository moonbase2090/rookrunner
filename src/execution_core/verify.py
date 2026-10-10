# SPDX-License-Identifier: MPL-2.0

"""Read-only check that a captured snapshot still matches its manifest.

Verification does not create an attempt directory, does not follow a symlink
out of the snapshot, and does not read the original checkout.
"""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat

from .protocol import canonical
from .snapshot import MAX_FILE_BYTES, MAX_FILES

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MANIFEST_KEYS = {
    "format_version",
    "base_commit",
    "dirty",
    "git_object_format",
    "workflow",
    "workflow_digest",
    "included",
    "excluded",
    "deleted",
    "entries",
}
_FILE_KEYS = {"path", "kind", "mode", "size", "sha256"}
_LINK_KEYS = _FILE_KEYS | {"target"}
_FILE_MODES = {"100644": 0o644, "100755": 0o755}


class VerifyError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _invalid(message):
    raise VerifyError("SNAPSHOT_INVALID", message)


def _mismatch(message):
    raise VerifyError("SNAPSHOT_MISMATCH", message)


def _follow_error(exc):
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
        return VerifyError("SNAPSHOT_INVALID", "refusing to follow a symlink")
    return VerifyError("SNAPSHOT_INVALID", "snapshot is not readable")


@contextmanager
def _opened(path, flags):
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _follow_error(exc) from exc
    try:
        yield fd
    finally:
        os.close(fd)


def _relative(value):
    if not isinstance(value, str) or not value or "\0" in value:
        _invalid("snapshot path is not accepted")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        _invalid("snapshot path is not accepted")
    return value


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _manifest_bytes(root):
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise _follow_error(exc) from exc
    if not stat.S_ISDIR(info.st_mode):
        _invalid("snapshot is not a directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with _opened(root, flags) as root_fd:
        try:
            manifest_fd = os.open(
                "manifest.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise _follow_error(exc) from exc
        try:
            chunks = []
            size = 0
            while block := os.read(manifest_fd, 1024 * 1024):
                size += len(block)
                if size > MAX_MANIFEST_BYTES:
                    _invalid("manifest exceeds size limit")
                chunks.append(block)
        finally:
            os.close(manifest_fd)
        try:
            files_fd = os.open("files", flags, dir_fd=root_fd)
        except OSError as exc:
            raise _follow_error(exc) from exc
    return b"".join(chunks), files_fd


def _load_manifest(raw, digest):
    if not _sha256(digest) or hashlib.sha256(raw).hexdigest() != digest:
        _mismatch("manifest digest does not match")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _invalid("manifest is not accepted")
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        _mismatch("manifest field is not accepted")
    if canonical(manifest).encode() != raw:
        _mismatch("manifest field is not accepted")
    if manifest["format_version"] != 1 or manifest["git_object_format"] not in ("sha1", "sha256"):
        _mismatch("manifest field is not accepted")
    if not isinstance(manifest["dirty"], bool):
        _mismatch("manifest field is not accepted")
    base = manifest["base_commit"]
    if base is not None and not isinstance(base, str):
        _mismatch("manifest field is not accepted")
    if not _sha256(manifest["workflow_digest"]):
        _mismatch("manifest field is not accepted")
    for key in ("included", "excluded", "deleted"):
        values = manifest[key]
        if not isinstance(values, list):
            _mismatch("manifest field is not accepted")
        for value in values:
            _relative(value)
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        _mismatch("manifest field is not accepted")
    _relative(manifest["workflow"])
    return manifest


def _check_entry_shape(entry):
    if not isinstance(entry, dict):
        _mismatch("manifest field is not accepted")
    kind = entry.get("kind")
    expected = _LINK_KEYS if kind == "symlink" else _FILE_KEYS
    if kind not in ("file", "symlink") or set(entry) != expected:
        _mismatch("manifest field is not accepted")
    _relative(entry["path"])
    if kind == "file" and entry["mode"] not in _FILE_MODES:
        _mismatch("entry mode does not match")
    if kind == "symlink" and entry["mode"] != "120000":
        _mismatch("entry mode does not match")
    if not isinstance(entry["size"], int) or entry["size"] < 0 or entry["size"] > MAX_FILE_BYTES:
        _mismatch("entry bytes do not match")
    if not _sha256(entry["sha256"]):
        _mismatch("entry bytes do not match")
    if kind == "symlink" and (not isinstance(entry["target"], str) or "\0" in entry["target"]):
        _mismatch("manifest field is not accepted")


def _collect(fd, prefix, found):
    try:
        names = os.listdir(fd)
    except OSError as exc:
        raise _follow_error(exc) from exc
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            info = os.lstat(name, dir_fd=fd)
        except OSError as exc:
            raise _follow_error(exc) from exc
        if stat.S_ISLNK(info.st_mode):
            found.add(relative)
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise _follow_error(exc) from exc
            try:
                _collect(child, relative, found)
            finally:
                os.close(child)
            continue
        if stat.S_ISREG(info.st_mode):
            found.add(relative)
            continue
        _invalid("snapshot contains an unsupported file")


def _hash_regular(parent, leaf, limit):
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError as exc:
        raise _follow_error(exc) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        while block := os.read(fd, 1024 * 1024):
            size += len(block)
            if size > limit:
                _mismatch("entry bytes do not match")
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest(), size


@contextmanager
def _parent(files_fd, relative):
    parts = PurePosixPath(relative).parts
    fd = os.dup(files_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise _follow_error(exc) from exc
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _verify_tree_entry(files_fd, entry):
    with _parent(files_fd, entry["path"]) as (parent, leaf):
        try:
            info = os.lstat(leaf, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                _mismatch("entry is missing")
            raise _follow_error(exc) from exc
        if entry["kind"] == "symlink":
            if not stat.S_ISLNK(info.st_mode):
                _mismatch("entry mode does not match")
            try:
                target = os.readlink(leaf, dir_fd=parent)
            except OSError as exc:
                raise _follow_error(exc) from exc
            data = target.encode("utf-8")
            if target != entry["target"]:
                _mismatch("symlink target does not match")
            if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["size"]:
                _mismatch("entry bytes do not match")
            return
        regular = stat.S_ISREG(info.st_mode)
        mode_matches = stat.S_IMODE(info.st_mode) == _FILE_MODES[entry["mode"]]
        if not regular or not mode_matches:
            _mismatch("entry mode does not match")
        digest, size = _hash_regular(parent, leaf, entry["size"])
        if digest != entry["sha256"] or size != entry["size"]:
            _mismatch("entry bytes do not match")


def _links_stay_inside(entries):
    by_path = {entry["path"]: entry for entry in entries}
    for start in entries:
        entry = start
        seen = set()
        while entry["kind"] == "symlink":
            if entry["path"] in seen:
                _invalid("symlink escapes the snapshot")
            seen.add(entry["path"])
            target = entry["target"]
            if PurePosixPath(target).is_absolute():
                _invalid("symlink escapes the snapshot")
            parts = list(PurePosixPath(entry["path"]).parent.parts)
            named = False
            for part in target.split("/"):
                if part in ("", "."):
                    _invalid("symlink escapes the snapshot")
                if part == "..":
                    if named or not parts:
                        _invalid("symlink escapes the snapshot")
                    parts.pop()
                else:
                    named = True
                    parts.append(part)
            entry = by_path.get("/".join(parts))
            if entry is None:
                _invalid("symlink escapes the snapshot")


def verify_snapshot(snapshot_dir, digest):
    """Return the manifest when its digest and every entry still match.

    `digest` is the SHA-256 of the captured manifest bytes. A changed byte,
    mode, or manifest field raises VerifyError. Nothing is written. A sibling
    git.json is not read.
    """

    raw, files_fd = _manifest_bytes(snapshot_dir)
    try:
        manifest = _load_manifest(raw, digest)
        entries = manifest["entries"]
        paths = []
        for entry in entries:
            _check_entry_shape(entry)
            paths.append(entry["path"])
        if len(paths) != len(set(paths)):
            _mismatch("manifest field is not accepted")
        found = set()
        _collect(files_fd, "", found)
        if found != set(paths):
            _mismatch("snapshot tree does not match")
        for entry in entries:
            _verify_tree_entry(files_fd, entry)
        workflow = next(
            (entry for entry in entries if entry["path"] == manifest["workflow"]),
            None,
        )
        if workflow is None or workflow["kind"] != "file":
            _mismatch("manifest field is not accepted")
        if workflow["sha256"] != manifest["workflow_digest"]:
            _mismatch("manifest field is not accepted")
        _links_stay_inside(entries)
        return manifest
    finally:
        os.close(files_fd)


_GIT_KEYS = {
    "format_version",
    "base_commit",
    "dirty",
    "git_object_format",
    "head",
}


def _object_id(value, algorithm):
    if value is None:
        return True
    length = {"sha1": 40, "sha256": 64}.get(algorithm, 0)
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _head_name(value):
    if value is None:
        return True
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        return False
    if any(character in value for character in "@: \t\n"):
        return False
    return len(value) > len("refs/heads/")


def _read_named(snapshot_dir, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root = os.open(snapshot_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _follow_error(exc) from exc
    try:
        try:
            descriptor = os.open(name, flags, dir_fd=root)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise _follow_error(exc) from exc
        try:
            chunks = []
            size = 0
            while block := os.read(descriptor, 1024 * 1024):
                size += len(block)
                if size > MAX_MANIFEST_BYTES:
                    _invalid("git metadata is not accepted")
                chunks.append(block)
        finally:
            os.close(descriptor)
    finally:
        os.close(root)
    return b"".join(chunks)


def git_metadata_digest(snapshot_dir):
    """Return the SHA-256 of git.json, or None when that sibling is absent."""

    raw = _read_named(snapshot_dir, "git.json")
    if raw is None:
        return None
    return hashlib.sha256(raw).hexdigest()


def read_git_metadata(snapshot_dir):
    """Return the sanitized sibling, or None when git.json is absent.

    A present sibling must be the canonical five-field object and must agree
    with the manifest's base commit, dirty flag, and object format. This
    reader is not used by verify_snapshot or by a run.
    """

    raw = _read_named(snapshot_dir, "git.json")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _invalid("git metadata is not accepted")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != _GIT_KEYS
        or canonical(parsed).encode() != raw
    ):
        _invalid("git metadata is not accepted")
    if type(parsed["format_version"]) is not int or parsed["format_version"] != 1:
        _invalid("git metadata is not accepted")
    if type(parsed["dirty"]) is not bool or parsed["git_object_format"] not in ("sha1", "sha256"):
        _invalid("git metadata is not accepted")
    if not _object_id(parsed["base_commit"], parsed["git_object_format"]) or not _head_name(
        parsed["head"]
    ):
        _invalid("git metadata is not accepted")
    manifest_raw = _read_named(snapshot_dir, "manifest.json")
    if manifest_raw is None:
        _invalid("git metadata is not accepted")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _invalid("git metadata is not accepted")
    if not isinstance(manifest, dict) or any(
        parsed[key] != manifest.get(key) for key in ("base_commit", "dirty", "git_object_format")
    ):
        _invalid("git metadata is not accepted")
    return parsed
