# SPDX-License-Identifier: MIT

"""Bounded diagnostic bundle for one worker state directory.

``diagnose`` reads ``skip-journal.jsonl`` and a count of runs accepted in
the window. It does not start a worker and it does not read secret files.
The worker appends a line when a submission is refused before a run is
stored, and when a workflow event does not match ``on``. The poll appends
a line for each skip. Journaling is always on.

Each line is one JSON object. The file mode is 0600, a symlink is refused,
and the file stays at most 1 MiB. ``--since`` is a duration (``30s``,
``5m``, ``2h``, ``7d``, ``1w``) or an aware ISO-8601 timestamp. The printed
object is at most 256 KiB. Secret-shaped text is replaced with ``***``.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from datetime import datetime, timedelta, UTC

from . import __version__
from .commands import mask_prefixes
from .protocol import canonical

BUNDLE_CAP_BYTES = 256 * 1024
MAX_BUNDLE_CAP_BYTES = 1024 * 1024
MIN_CAP_BYTES = 512
JOURNAL_CAP_BYTES = 1024 * 1024
MAX_LINE_BYTES = 2048
_MAX_DETAIL = 240
_MAX_FIELD = 200
JOURNAL_NAME = "skip-journal.jsonl"
LOCK_NAME = "skip-journal.lock"
_MASK = "***"
_DURATION = re.compile(r"^([0-9]{1,6})([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_REASON = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OUTCOMES = {"skipped", "not_admitted"}
_OPTIONAL = (
    "workflow",
    "job_id",
    "event",
    "ref",
    "host",
    "submission_key",
    "run_id",
    "sha",
)
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")
_ASSIGN = re.compile(
    r"(?i)(\b(?:token|secret|password|passwd|authorization|api[_-]?key)\b\s*[:=]\s*)(\S+)"
)
_AKIA = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_STATE = "state directory must be owned by you with mode 0700, not a symlink"

SKIP_DETAILS = {
    "retry_not_submitted": "failed-run retry was not submitted",
    "image": "worker place image did not match",
    "unreachable": "worker place could not be reached",
    "not_ready": "worker place was not ready",
    "repository": "worker place repository did not match",
    "host_full": "worker place was full",
    "host_unknown": "recorded host is not in this placement",
    "poll_stamp": "poll completion was not recorded",
    "signoff_absent": "signoff label was absent",
    "fork": "pull request was not admitted because it is a fork",
    "untrusted": "run was not admitted because the code is untrusted",
    "merge_commit_mismatch": "fetched merge commit did not match the listed head",
    "workflow_changed": "pull request that changes a workflow file was not admitted",
    "workflow_diff_unavailable": "pull request workflow diff could not be read",
    "merge_ref_absent": "pull request merge ref was absent",
    "workflow_absent": "workflow file is not in the tested commit",
    "job_absent": "configured job is not in the workflow",
    "workflow_invalid": "workflow rejected the configured job",
    "no_host": "run was not admitted because no worker place had room",
    "snapshot_unreadable": "snapshot could not be read for the final status",
    "not_triggered": "workflow on did not match this event",
}


class DiagnoseError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def parse_since(text, now):
    """Return the inclusive cutoff for a duration or an aware timestamp."""

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DiagnoseError("INVALID_PARAMS", "since must be a duration or an aware timestamp")
    if not isinstance(text, str):
        raise DiagnoseError("INVALID_PARAMS", "since must be a duration or an aware timestamp")
    match = _DURATION.fullmatch(text)
    if match:
        amount = int(match.group(1))
        if amount < 1:
            raise DiagnoseError("INVALID_PARAMS", "since duration must be at least 1")
        return now - timedelta(seconds=amount * _UNIT_SECONDS[match.group(2)])
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise DiagnoseError(
            "INVALID_PARAMS", "since must be a duration or an aware timestamp"
        ) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise DiagnoseError("INVALID_PARAMS", "since timestamp must include a timezone")
    return stamp


def redact_text(text):
    """Replace token-shaped and secret-shaped spans. Other text stays."""

    if not isinstance(text, str) or text == "":
        return text
    text = _PEM.sub(_MASK, text)
    text = _JWT.sub(_MASK, text)
    text = _BEARER.sub(_MASK, text)
    text = mask_prefixes(text)
    text = _AKIA.sub(_MASK, text)
    return _ASSIGN.sub(lambda match: match.group(1) + _MASK, text)


def redact(value):
    """Redact every string in a JSON-like tree. Keys are left as written."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def record_skip(at, row):
    """Build a journal record from one poll skip row."""

    reason = row.get("reason") if isinstance(row, dict) else None
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        reason = "skipped"
    record = {
        "at": at,
        "outcome": "skipped",
        "reason": reason,
        "detail": SKIP_DETAILS.get(reason, "run was skipped"),
    }
    if not isinstance(row, dict):
        return record
    for source, dest in (
        ("workflow", "workflow"),
        ("job_id", "job_id"),
        ("event", "event"),
        ("ref", "ref"),
        ("host", "host"),
        ("key", "submission_key"),
        ("run_id", "run_id"),
        ("sha", "sha"),
    ):
        value = row.get(source)
        if isinstance(value, str):
            record[dest] = value
    number = row.get("number")
    if type(number) is int:
        record["number"] = number
    return record


def append_skip(state, record, *, journal_cap=JOURNAL_CAP_BYTES):
    """Append one redacted journal line and drop oldest lines past the cap."""

    if type(journal_cap) is not int or not 256 <= journal_cap <= JOURNAL_CAP_BYTES:
        raise DiagnoseError("INVALID_PARAMS", "journal cap is not accepted")
    directory = _state_dir(state)
    line = _encode(record)
    lock = _lock(directory, exclusive=True)
    if lock is None:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be locked")
    try:
        path = directory / JOURNAL_NAME
        _append_line(path, line)
        _trim(path, journal_cap)
    finally:
        os.close(lock)


def bundle(state, since, *, now=None, cap_bytes=BUNDLE_CAP_BYTES):
    """Return the diagnostic object whose canonical JSON fits in ``cap_bytes``."""

    if type(cap_bytes) is not int or not MIN_CAP_BYTES <= cap_bytes <= MAX_BUNDLE_CAP_BYTES:
        raise DiagnoseError("INVALID_PARAMS", "cap must be an integer from 512 to 1048576")
    moment = datetime.now(UTC) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise DiagnoseError("INVALID_PARAMS", "since must be a duration or an aware timestamp")
    cutoff = since if isinstance(since, datetime) else parse_since(since, moment)
    if isinstance(since, datetime) and (since.tzinfo is None or since.utcoffset() is None):
        raise DiagnoseError("INVALID_PARAMS", "since timestamp must include a timezone")
    directory = _state_dir(state)
    matched = [entry for stamp, entry in _load(directory) if stamp >= cutoff]
    worker, runs = _summary(directory, cutoff)
    full = {
        "since": cutoff.isoformat(),
        "until": moment.isoformat(),
        "cap_bytes": cap_bytes,
        "worker": worker,
        "runs": runs,
    }
    bare = {
        "since": cutoff.isoformat(),
        "until": moment.isoformat(),
        "cap_bytes": cap_bytes,
    }
    selected = _pack(matched, cap_bytes, full)
    if selected is None:
        selected = _pack(matched, cap_bytes, bare)
    if selected is None:
        raise DiagnoseError("INVALID_PARAMS", "diagnostic bundle does not fit the cap")
    return selected


def _pack(entries, cap, base):
    best = None
    low = 0
    high = len(entries)
    while low <= high:
        count = (low + high) // 2
        kept = entries[len(entries) - count :] if count else []
        rendered, size = _render(base, entries, kept, cap)
        if size <= cap:
            best = rendered
            low = count + 1
        else:
            high = count - 1
    return best


def _render(base, entries, kept, cap):
    omitted = len(entries) - len(kept)
    body = dict(base)
    body["cap_bytes"] = cap
    body["truncated"] = omitted > 0
    body["omitted"] = omitted
    body["skips"] = kept
    if omitted:
        counts = {}
        for entry in entries[:omitted]:
            reason = entry["reason"]
            counts[reason] = counts.get(reason, 0) + 1
        body["omitted_reasons"] = counts
    rendered = redact(body)
    return rendered, len(canonical(rendered).encode())


def _summary(state, cutoff):
    worker = {
        "package_version": __version__,
        "worker_id": None,
        "repository": None,
        "storage": "absent",
    }
    path = state / "runs.sqlite3"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return worker, {"accepted": 0}
    if not stat.S_ISREG(info.st_mode):
        worker["storage"] = "unreadable"
        return worker, {"accepted": None}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        worker["storage"] = "unreadable"
        return worker, {"accepted": None}
    try:
        worker_id, repository, accepted = _read_storage(connection, cutoff)
    except sqlite3.Error:
        worker["storage"] = "unreadable"
        return worker, {"accepted": None}
    finally:
        connection.close()
    worker["storage"] = "ok"
    worker["worker_id"] = worker_id
    worker["repository"] = repository
    return worker, {"accepted": accepted}


def _read_storage(connection, cutoff):
    meta = {}
    for key, value in connection.execute("SELECT key, value FROM metadata"):
        if key in {"worker_id", "repository"} and isinstance(value, str) and _plain(value):
            meta[key] = value
    accepted = 0
    for (text,) in connection.execute("SELECT record FROM runs"):
        if not isinstance(text, str):
            continue
        try:
            record = json.loads(text)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        stamp = _aware(record.get("accepted_at"))
        if stamp is not None and stamp >= cutoff:
            accepted += 1
    return meta.get("worker_id"), meta.get("repository"), accepted


def _load(state):
    path = state / JOURNAL_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode):
        raise DiagnoseError("STATE_INVALID", "skip journal must not be a symlink")
    lock = _lock(state, exclusive=False)
    try:
        data = _journal_bytes(path, info.st_size)
    finally:
        if lock is not None:
            os.close(lock)
    loaded = []
    for line in data.splitlines():
        parsed = _parse_line(line)
        if parsed is not None:
            loaded.append(parsed)
    return loaded


def _journal_bytes(path, size):
    with path.open("rb") as handle:
        if size > JOURNAL_CAP_BYTES:
            handle.seek(size - JOURNAL_CAP_BYTES)
            handle.readline()
        return handle.read(JOURNAL_CAP_BYTES + MAX_LINE_BYTES)


def _parse_line(line):
    if len(line) > MAX_LINE_BYTES or line.strip() == b"":
        return None
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    stamp = _aware(value.get("at"))
    outcome = value.get("outcome")
    reason = value.get("reason")
    if stamp is None or outcome not in _OUTCOMES or not isinstance(reason, str):
        return None
    if _REASON.fullmatch(reason) is None:
        return None
    detail = value.get("detail")
    entry = {
        "at": stamp.isoformat(),
        "outcome": outcome,
        "reason": reason,
        "detail": detail if isinstance(detail, str) else reason,
    }
    for name in _OPTIONAL:
        item = value.get(name)
        if isinstance(item, str) and _plain(item):
            entry[name] = item[:_MAX_FIELD]
    number = value.get("number")
    if type(number) is int and 0 <= number <= 1_000_000_000:
        entry["number"] = number
    return stamp, entry


def _encode(record):
    if not isinstance(record, dict):
        raise DiagnoseError("INVALID_PARAMS", "skip record is not usable")
    stamp = _aware(record.get("at"))
    outcome = record.get("outcome")
    reason = record.get("reason")
    if stamp is None or outcome not in _OUTCOMES:
        raise DiagnoseError("INVALID_PARAMS", "skip record is not usable")
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        reason = "skipped"
    detail = record.get("detail")
    if not isinstance(detail, str):
        detail = SKIP_DETAILS.get(reason, "run was skipped")
    body = {
        "at": stamp.isoformat(),
        "outcome": outcome,
        "reason": reason,
        "detail": _clip(redact_text(detail), _MAX_DETAIL),
    }
    for name in _OPTIONAL:
        item = record.get(name)
        if isinstance(item, str) and _plain(item):
            body[name] = _clip(redact_text(item), _MAX_FIELD)
    number = record.get("number")
    if type(number) is int and 0 <= number <= 1_000_000_000:
        body["number"] = number
    while True:
        encoded = (canonical(body) + "\n").encode()
        if len(encoded) <= MAX_LINE_BYTES:
            return encoded
        dropped = False
        for name in reversed(_OPTIONAL):
            if name in body:
                del body[name]
                dropped = True
                break
        if dropped:
            continue
        if len(body["detail"]) > 32:
            body["detail"] = body["detail"][:32]
            continue
        raise DiagnoseError("INVALID_PARAMS", "skip record is not usable")


def _clip(text, limit):
    text = text.replace("\r", " ").replace("\n", " ").replace("\0", "")
    return text[:limit]


def _plain(text):
    return text != "" and "\n" not in text and "\r" not in text and "\0" not in text


def _aware(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return stamp


def _state_dir(state):
    path = Path(state)
    try:
        info = path.lstat()
    except OSError:
        raise DiagnoseError("STATE_INVALID", _STATE) from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DiagnoseError("STATE_INVALID", _STATE)
    return path


def _lock(state, *, exclusive):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if exclusive:
        flags |= os.O_CREAT
    try:
        fd = os.open(state / LOCK_NAME, flags, 0o600)
    except FileNotFoundError:
        return None
    except OSError:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be locked") from None
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError:
        os.close(fd)
        raise DiagnoseError("STATE_INVALID", "skip journal could not be locked") from None
    return fd


def _append_line(path, line):
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be written") from None
    try:
        os.write(fd, line)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except OSError:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be written") from None
    finally:
        os.close(fd)


def _trim(path, cap):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise DiagnoseError("STATE_INVALID", "skip journal must not be a symlink")
    if info.st_size <= cap:
        return
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    total = sum(len(line) for line in lines)
    while lines and total > cap:
        total -= len(lines.pop(0))
    temporary = path.with_name(".skip-journal.preparing")
    if temporary.is_symlink():
        raise DiagnoseError("STATE_INVALID", "skip journal must not be a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(temporary, flags, 0o600)
    except OSError:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be written") from None
    try:
        os.write(fd, b"".join(lines))
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except OSError:
        raise DiagnoseError("STATE_INVALID", "skip journal could not be written") from None
    finally:
        os.close(fd)
    os.replace(temporary, path)
