"""Read-only HTML page on 127.0.0.1:8765.

The process calls status.view and nothing else. It does not open
worker storage and it does not signal the worker.
"""

import html
from pathlib import PurePosixPath
import re
import signal
import socket
import sys
import threading

ADDRESS = "127.0.0.1"
PORT = 8765
_MAX_HEADERS = 8192
_BIND_ERROR = "status page could not bind 127.0.0.1:8765\n"
_HOST = "127.0.0.1:8765"
_VERSION = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_CHECK = re.compile(
    r"https://github.com/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/"
    r"runs/([1-9][0-9]*)"
)
_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
}
_UNAVAILABLE = "<p>not available</p>"


def bind_listener():
    """Bind the loopback port. A mismatch or a second process exits 1."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((ADDRESS, PORT))
        bound = sock.getsockname()
        if bound[0] != ADDRESS or bound[1] != PORT:
            raise OSError
        sock.listen(1)
    except OSError:
        sock.close()
        sys.stderr.write(_BIND_ERROR)
        raise SystemExit(1) from None
    return sock


def serve(state):
    """Accept one request at a time until the listen socket closes."""

    listener = bind_listener()
    _arm(listener)
    try:
        while True:
            try:
                client, _addr = listener.accept()
            except OSError:
                return
            with client:
                try:
                    handle(client, state)
                except OSError:
                    continue
    finally:
        listener.close()


def handle(client, state):
    """Answer one request. A dropped request writes nothing."""

    text = _read_request(client)
    if text is None:
        return
    status, body, head_only = _answer(text, state)
    _send(client, status, body, head_only)


def _arm(listener):
    if threading.current_thread() is not threading.main_thread():
        return

    def stop(_signum, _frame):
        listener.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _read_request(client):
    client.settimeout(5)
    data = bytearray()
    limit = _MAX_HEADERS + 4
    while b"\r\n\r\n" not in data and len(data) < limit:
        try:
            chunk = client.recv(limit - len(data))
        except (TimeoutError, OSError):
            return None
        if not chunk:
            break
        data.extend(chunk)
    if b"\r\n\r\n" not in data:
        return None
    head, rest = bytes(data).split(b"\r\n\r\n", 1)
    if len(head) > _MAX_HEADERS or rest:
        return None
    lowered = head.lower()
    if b"transfer-encoding:" in lowered or b"content-length:" in lowered:
        return None
    return head.decode("ascii", errors="replace")


def _answer(text, state):
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return 400, b"", False
    method, target, version = parts
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        return 400, b"", False
    if method not in {"GET", "HEAD"}:
        return 405, b"", False
    if not target.startswith("/") or "?" in target:
        return 400, b"", False
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            return 400, b"", False
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if key == "" or key in headers:
            return 400, b"", False
        headers[key] = value.strip()
    if target != "/":
        return 404, b"", False
    if headers.get("host") != _HOST:
        return 400, b"", False
    mode, view = _fetch(state)
    body = _document(mode, view).encode()
    return 200, body, method == "HEAD"


def _fetch(state):
    from . import cli

    try:
        reply = cli.call(state, "status.view", {})
    except TimeoutError:
        return "busy", None
    except (OSError, ValueError):
        return "down", None
    if not isinstance(reply, dict) or not isinstance(reply.get("result"), dict):
        return "down", None
    return "up", reply["result"]


def _send(client, status, body, head_only):
    lines = [
        f"HTTP/1.1 {status} {_REASONS[status]}",
        "Connection: close",
        f"Content-Length: {len(body)}",
    ]
    if status == 200:
        lines.append("Content-Type: text/html; charset=utf-8")
    lines.extend(
        [
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            f"Content-Security-Policy: {_CSP}",
        ]
    )
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode()
    if not head_only:
        payload += body
    client.sendall(payload)


def _esc(value):
    return html.escape(value, quote=True)


def _td(value):
    return f"<td>{_esc(value)}</td>"


def _pair(label, value):
    return f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>"


def _plain_name(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        return "not shown"
    if "\n" in value or "\0" in value:
        return "not shown"
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        return "not shown"
    return value


def _names(values):
    if not isinstance(values, list):
        return "not shown"
    shown = [_plain_name(item) for item in values if isinstance(item, str)]
    if not shown:
        return "not shown"
    return ", ".join(shown)


def _version(value):
    if isinstance(value, str) and _VERSION.fullmatch(value):
        return value
    return "not shown"


def _readiness(value):
    if value is None:
        return "none"
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and "/" not in value
        and "\\" not in value
        and "\n" not in value
        and "\0" not in value
    ):
        return value
    return "not shown"


def _image(value):
    if value is None:
        return "not set"
    if isinstance(value, str) and _IMAGE.fullmatch(value):
        return value
    return "not shown"


def _stamp(value):
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and "/" not in value
        and "\\" not in value
        and "\n" not in value
        and "\0" not in value
    ):
        return value
    return "not recorded"


def _owner(value):
    if isinstance(value, str) and _OWNER.fullmatch(value):
        return value
    return "not recorded"


def _worker_block(mode, view):
    if mode != "up" or not isinstance(view, dict):
        label = mode if mode in {"busy", "down"} else "down"
        rows = (
            ("Worker", label),
            ("Version", "not available"),
            ("Ready", "not available"),
            ("Readiness", "not available"),
            ("Runner image", "not available"),
            ("Last poll", "not available"),
            ("Poll repository", "not available"),
        )
    else:
        worker = view.get("worker") if isinstance(view.get("worker"), dict) else {}
        poll = view.get("poll") if isinstance(view.get("poll"), dict) else {}
        ready = "true" if worker.get("ready") is True else "false"
        rows = (
            ("Worker", "up"),
            ("Version", _version(worker.get("version"))),
            ("Ready", ready),
            ("Readiness", _readiness(worker.get("readiness_error"))),
            ("Runner image", _image(worker.get("runner_image"))),
            ("Last poll", _stamp(poll.get("completed_at"))),
            ("Poll repository", _owner(poll.get("repository"))),
        )
    return "<dl>" + "".join(_pair(label, value) for label, value in rows) + "</dl>"


def _text(value):
    if (
        isinstance(value, str)
        and value
        and len(value) <= 128
        and "\n" not in value
        and "\0" not in value
    ):
        return _td(value)
    return _td("not recorded")


def _repo_cell(value):
    if isinstance(value, str) and _OWNER.fullmatch(value):
        return _td(value)
    return _td("not recorded")


def _subject_cell(subject):
    if isinstance(subject, dict) and subject.get("kind") == "pull_request":
        number = subject.get("number")
        if type(number) is int and number >= 1:
            return _td(f"#{number}")
    if isinstance(subject, dict) and subject.get("kind") == "commit":
        sha = subject.get("sha")
        if isinstance(sha, str) and _SHA.fullmatch(sha):
            return _td(sha)
    return _td("not recorded")


def _field_cell(value):
    if isinstance(value, str) and _plain_name(value) == value:
        return _td(value)
    return _td("not recorded")


def _exit_cell(value):
    if type(value) is int:
        return _td(str(value))
    return _td("")


def _duration_cell(row):
    kind = row.get("duration")
    if kind == "unstarted":
        return _td("not started")
    seconds = row.get("duration_seconds")
    if kind in {"running", "finished"} and type(seconds) is int and seconds >= 0:
        return _td(f"{seconds}s")
    return _td("not recorded")


def _check_cell(row):
    url = row.get("check_url")
    matched = _CHECK.fullmatch(url) if isinstance(url, str) else None
    if matched is None:
        return _td("not posted")
    if int(matched.group(1)) > 2**63 - 1:
        return _td("not posted")
    href = _esc(url)
    return f'<td><a href="{href}" rel="noopener noreferrer">check</a></td>'


def _runs(rows):
    if not isinstance(rows, list):
        return _UNAVAILABLE
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = row.get("state")
        state_cell = state if isinstance(state, str) and _TOKEN.fullmatch(state) else "unknown"
        cells = (
            _text(row.get("run_id")),
            _repo_cell(row.get("repository")),
            _subject_cell(row.get("subject")),
            _field_cell(row.get("workflow")),
            _field_cell(row.get("job_id")),
            _td(state_cell),
            _exit_cell(row.get("exit_code")),
            _duration_cell(row),
            _check_cell(row),
        )
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<table><thead><tr>"
        "<th>Run</th><th>Repository</th><th>Pull request or commit</th>"
        "<th>Workflow</th><th>Job</th><th>Status</th><th>Exit</th>"
        "<th>Duration</th><th>Check</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _queue(rows):
    if not isinstance(rows, list):
        return _UNAVAILABLE
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        waiting = "true" if row.get("waiting") is True else "false"
        cells = (
            _text(row.get("run_id")),
            _repo_cell(row.get("repository")),
            _subject_cell(row.get("subject")),
            _field_cell(row.get("workflow")),
            _field_cell(row.get("job_id")),
            _td(waiting),
            _td(_names(row.get("groups"))),
        )
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<p>A waiting flag is the group skip only.</p>"
        "<table><thead><tr>"
        "<th>Run</th><th>Repository</th><th>Pull request or commit</th>"
        "<th>Workflow</th><th>Job</th><th>Waiting</th><th>Groups</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _count(value):
    if type(value) is int and value >= 0:
        return str(value)
    return "0"


def _groups(rows, ungrouped):
    if not isinstance(rows, list):
        return _UNAVAILABLE
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        queue = row.get("queue")
        queue_cell = queue if queue in {"single", "max"} else "not shown"
        cancel = "true" if row.get("cancel_in_progress") is True else "false"
        pending = row.get("pending_limit")
        limit = str(pending) if queue == "max" and type(pending) is int else ""
        cells = (
            _td(_names(row.get("names"))),
            _td(queue_cell),
            _td(cancel),
            _td(limit),
            _td(_count(row.get("queued"))),
            _td(_count(row.get("running"))),
        )
        body.append("<tr>" + "".join(cells) + "</tr>")
    count = ungrouped if type(ungrouped) is int and ungrouped >= 0 else 0
    return (
        "<table><thead><tr>"
        "<th>Names</th><th>Queue</th><th>Cancel in progress</th><th>Limit</th>"
        "<th>Queued</th><th>Running</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>" + f"<p>no group {count}</p>"
    )


def _document(mode, view):
    if mode == "up" and isinstance(view, dict):
        runs = _runs(view.get("runs"))
        queue = _queue(view.get("queue"))
        groups = _groups(view.get("groups"), view.get("ungrouped"))
    else:
        runs = queue = groups = _UNAVAILABLE
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="refresh" content="5">\n'
        "<title>Status</title>\n</head>\n<body>\n"
        "<h1>Worker</h1>\n"
        f"{_worker_block(mode, view)}\n"
        "<h1>Recent runs</h1>\n"
        f"{runs}\n"
        "<h1>Queue</h1>\n"
        f"{queue}\n"
        "<h1>Concurrency groups</h1>\n"
        f"{groups}\n"
        "</body>\n</html>\n"
    )
