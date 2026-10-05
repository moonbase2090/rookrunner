"""Select workspace files for an owned upload step.

The step records an artifact name and workspace-relative paths. It does not
copy bytes, zip them, or open a network connection. The finish scan reads
the bytes. Symlinks are not followed.
"""

import json
import os
from pathlib import Path
import stat
import tempfile

from .artifacts import _parts

SARIF_NAME = "codeql-sarif"
NO_FILES_LINE = "No files were found for the artifact.\n"
# The upload-artifact README at the inventory pin limits one job to 500
# named artifacts. The cap counts names created by that job, not files.
# https://github.com/actions/upload-artifact/blob/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/README.md
MAX_UPLOADS_PER_JOB = 500


class UploadError(Exception):
    def __init__(self, message):
        super().__init__(message)


def _name_ok(name):
    return (
        isinstance(name, str)
        and name != ""
        and "\0" not in name
        and "\n" not in name
        and "\r" not in name
    )


def _hidden(relative):
    return any(part.startswith(".") for part in relative.split("/"))


def _compile_line(line):
    if line == "" or any(char in line for char in "\\?[~"):
        raise UploadError("path is not accepted")
    if line.startswith("/"):
        raise UploadError("path is not accepted")
    stripped = False
    if line.startswith("./"):
        line = line[2:]
        stripped = True
    directory = line.endswith("/")
    if directory:
        line = line[:-1]
    if line == "." or (line == "" and stripped):
        return ("all",)
    if line == "":
        raise UploadError("path is not accepted")
    parts = line.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UploadError("path is not accepted")
    glob = False
    for part in parts:
        if part in {"*", "**"}:
            glob = True
            continue
        if "*" in part:
            raise UploadError("path is not accepted")
    if not glob:
        if _parts(line) is None:
            raise UploadError("path is not accepted")
        return ("literal", line, directory)
    return ("glob", tuple(parts), directory)


def _glob_match(segments, parts):
    """Match `segments` against `parts`. `**` may match zero segments."""

    pattern_count = len(segments)
    part_count = len(parts)
    ok = [[False] * (part_count + 1) for _ in range(pattern_count + 1)]
    ok[0][0] = True
    for index in range(1, pattern_count + 1):
        if segments[index - 1] != "**":
            break
        ok[index][0] = True
    for index in range(1, pattern_count + 1):
        segment = segments[index - 1]
        for part_index in range(1, part_count + 1):
            if segment == "**":
                ok[index][part_index] = ok[index - 1][part_index] or ok[index][part_index - 1]
            elif segment == "*":
                ok[index][part_index] = ok[index - 1][part_index - 1]
            elif segment == parts[part_index - 1]:
                ok[index][part_index] = ok[index - 1][part_index - 1]
    return ok[pattern_count][part_count]


def _regular_files(workspace, *, include_hidden):
    root = os.fspath(workspace)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            if not include_hidden and name.startswith("."):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            if not include_hidden and name.startswith("."):
                continue
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if _hidden(relative) and not include_hidden:
                continue
            found.append(relative)
    return found


def _apply_literal(workspace, relative, directory, files, include_hidden):
    if _hidden(relative) and not include_hidden:
        return []
    target = os.path.join(os.fspath(workspace), *relative.split("/"))
    try:
        info = os.lstat(target)
    except OSError:
        return []
    if stat.S_ISLNK(info.st_mode):
        return []
    if stat.S_ISREG(info.st_mode):
        if directory:
            return []
        return [relative]
    if stat.S_ISDIR(info.st_mode):
        prefix = relative + "/"
        return [item for item in files if item.startswith(prefix)]
    return []


def _apply_glob(segments, directory, files):
    matched = []
    for relative in files:
        parts = relative.split("/")
        if directory:
            if any(_glob_match(segments, parts[:length]) for length in range(1, len(parts))):
                matched.append(relative)
        elif _glob_match(segments, parts):
            matched.append(relative)
    return matched


def _apply(workspace, pattern, files, include_hidden):
    if pattern[0] == "all":
        return list(files)
    if pattern[0] == "literal":
        return _apply_literal(workspace, pattern[1], pattern[2], files, include_hidden)
    return _apply_glob(pattern[1], pattern[2], files)


def select_files(workspace, pattern_text, *, include_hidden):
    """Return sorted relative paths selected by newline-separated patterns.

    An empty line is ignored. A line that starts with `!` excludes matches.
    A bad pattern fails the whole call. Hidden paths are skipped unless
    `include_hidden` is true, including when the pattern names them.
    """

    if (
        not isinstance(pattern_text, str)
        or "\0" in pattern_text
        or type(include_hidden) is not bool
    ):
        raise UploadError("path is not accepted")
    includes = []
    excludes = []
    for raw in pattern_text.split("\n"):
        if raw == "":
            continue
        exclude = raw.startswith("!")
        body = raw[1:] if exclude else raw
        if exclude and body == "":
            raise UploadError("path is not accepted")
        compiled = _compile_line(body)
        if exclude:
            excludes.append(compiled)
        else:
            includes.append(compiled)
    files = _regular_files(workspace, include_hidden=include_hidden)
    chosen = []
    seen = set()
    for pattern in includes:
        for relative in _apply(workspace, pattern, files, include_hidden):
            if relative in seen:
                continue
            if _parts(relative) is None:
                raise UploadError("path is not accepted")
            seen.add(relative)
            chosen.append(relative)
    if excludes:
        blocked = set()
        for pattern in excludes:
            blocked.update(_apply(workspace, pattern, files, include_hidden))
        chosen = [item for item in chosen if item not in blocked]
    chosen.sort()
    return chosen


def select_sarif(workspace, text):
    """Return one relative regular file. A missing file is a step failure."""

    if (
        not isinstance(text, str)
        or text == ""
        or "\n" in text
        or "\0" in text
        or any(char in text for char in "\\?[*~")
        or text.startswith("/")
    ):
        raise UploadError("path is not accepted")
    relative = text[2:] if text.startswith("./") else text
    if relative == "" or relative.endswith("/") or _parts(relative) is None:
        raise UploadError("path is not accepted")
    target = os.path.join(os.fspath(workspace), *_parts(relative))
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        raise UploadError("sarif file is missing") from None
    except OSError:
        raise UploadError("path is not accepted") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UploadError("path is not accepted")
    return relative


class UploadBook:
    """Names and paths selected during one attempt.

    Names are unique across the attempt. The 500 cap is per job id.
    `record_path` is `uploads.json` beside the workspace, not inside it.
    """

    def __init__(self, record_path=None):
        self.record_path = None if record_path is None else Path(record_path)
        self.records = []
        self._names = set()
        self._paths = set()
        self._counts = {}

    def add(self, job_id, name, paths):
        label = job_id if isinstance(job_id, str) else ""
        if not _name_ok(name):
            raise UploadError("artifact name is not accepted")
        if name in self._names:
            raise UploadError("artifact name is already used")
        if not isinstance(paths, list) or not paths:
            raise UploadError("path is not accepted")
        fresh = []
        for relative in paths:
            if not isinstance(relative, str) or relative in self._paths or relative in fresh:
                raise UploadError("path is already selected")
            fresh.append(relative)
        count = self._counts.get(label, 0)
        if count >= MAX_UPLOADS_PER_JOB:
            raise UploadError("job artifact limit is 500")
        self._names.add(name)
        self._paths.update(fresh)
        self._counts[label] = count + 1
        self.records.append({"name": name, "paths": list(fresh)})
        try:
            self._persist()
        except UploadError:
            self.records.pop()
            self._names.remove(name)
            self._paths.difference_update(fresh)
            self._counts[label] = count
            raise

    def _persist(self):
        path = self.record_path
        if path is None:
            return
        if path.is_symlink():
            raise UploadError("path is not accepted")
        payload = json.dumps({"uploads": self.records}, separators=(",", ":")).encode("utf-8")
        fd = None
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".uploads-", suffix=".json", dir=path.parent)
            os.write(fd, payload)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            os.close(fd)
            fd = None
            os.replace(temporary, path)
            temporary = None
            os.chmod(path, 0o600)
        except OSError as exc:
            raise UploadError("path is not accepted") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def load_uploads(path):
    """Read `uploads.json`. A missing or unusable file means no selections."""

    try:
        info = os.lstat(path)
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode):
        return []
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(body, dict) or not isinstance(body.get("uploads"), list):
        return []
    cleaned = []
    for item in body["uploads"]:
        if not isinstance(item, dict):
            return []
        name = item.get("name")
        paths = item.get("paths")
        if not _name_ok(name) or not isinstance(paths, list) or not paths:
            return []
        if any(not isinstance(entry, str) or _parts(entry) is None for entry in paths):
            return []
        cleaned.append({"name": name, "paths": list(paths)})
    return cleaned
