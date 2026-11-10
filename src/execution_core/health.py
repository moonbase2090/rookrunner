# SPDX-License-Identifier: MIT

"""Worker presence recorded in the state directory.

A live worker rewrites ``worker-health.json`` about every five seconds from
a thread that does not take the request guard. ``observe`` reads that file
and does not rewrite it. A missing file is ``worker_unavailable`` with
reason ``absent``. A record written at shutdown is reason ``stopped``. An
``available`` record older than ``STALE_AFTER_SECONDS`` is reason
``heartbeat_stale``. That age does not prove a partitioned worker stopped
executing a job. A later ``available`` record clears ``worker_unavailable``.
"""

import errno
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

HEARTBEAT_NAME = "worker-health.json"
HEARTBEAT_SECONDS = 5
STALE_AFTER_SECONDS = 15
_MAX_BYTES = 4096
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def write_heartbeat(state, *, alive, now=None):
    """Replace the health file. ``alive`` false records reason ``stopped``."""

    root = Path(state)
    path = root / HEARTBEAT_NAME
    moment = _moment(now)
    record = {
        "last_seen": moment.isoformat(timespec="microseconds"),
        "state": "available" if alive else "worker_unavailable",
    }
    if not alive:
        record["reason"] = "stopped"
    payload = (json.dumps(record, sort_keys=True) + "\n").encode()
    if path.is_symlink():
        raise OSError(errno.ELOOP, "health file is a symlink", str(path))
    temp = root / (".%s.%d.tmp" % (HEARTBEAT_NAME, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.unlink(temp)
    except FileNotFoundError:
        pass
    fd = os.open(temp, flags, 0o600)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    return record


def observe(state, now=None):
    """Return ``state``, ``last_seen``, and ``reason`` for the health file."""

    moment = _moment(now)
    root = Path(state)
    try:
        info = root.lstat()
    except OSError:
        return _unavailable(None, "absent")
    if not stat.S_ISDIR(info.st_mode):
        return _unavailable(None, "absent")
    try:
        fd = os.open(root / HEARTBEAT_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return _unavailable(None, "absent")
    try:
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        return _unavailable(None, "absent")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _unavailable(None, "absent")
    if not isinstance(body, dict):
        return _unavailable(None, "absent")
    seen = body.get("last_seen")
    if not isinstance(seen, str) or _STAMP.fullmatch(seen) is None:
        return _unavailable(None, "absent")
    if body.get("state") == "worker_unavailable" and body.get("reason") == "stopped":
        return _unavailable(seen, "stopped")
    if body.get("state") != "available":
        return _unavailable(None, "absent")
    age = (moment - datetime.fromisoformat(seen)).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return _unavailable(seen, "heartbeat_stale")
    return {"state": "available", "last_seen": seen, "reason": None}


def _moment(now):
    moment = datetime.now(UTC) if now is None else now
    offset = moment.utcoffset() if moment.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("health time must be UTC")
    return moment


def _unavailable(last_seen, reason):
    return {"state": "worker_unavailable", "last_seen": last_seen, "reason": reason}
