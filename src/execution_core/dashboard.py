"""Terminal dashboard.

The view reads the worker socket and binds no port. Each action has
a key and a mouse target. Cancel sends run.cancel with version 0
and renders the record the worker returned. dashboard-html is not
a command.
"""

import base64
from datetime import datetime, timezone
import os
import re
import sys

# screen, action, keys, mouse target. Every action has both.
BINDINGS = (
    ("queue", "previous", ("k", "up"), "[previous]"),
    ("queue", "next", ("j", "down"), "[next]"),
    ("queue", "open", ("enter",), "[open]"),
    ("queue", "cancel", ("c",), "[cancel]"),
    ("queue", "quit", ("q",), "[quit]"),
    ("detail", "previous", ("k", "up"), "[previous]"),
    ("detail", "next", ("j", "down"), "[next]"),
    ("detail", "log-prev", ("p",), "[log-prev]"),
    ("detail", "log-next", ("n",), "[log-next]"),
    ("detail", "cancel", ("c",), "[cancel]"),
    ("detail", "back", ("b", "escape"), "[back]"),
    ("detail", "quit", ("q",), "[quit]"),
)

_TARGET = re.compile(r"\[(log-next|log-prev|previous|next|open|quit|back|cancel|run|artifact)\]")
_KEY_LABEL = {
    "previous": "k or up",
    "next": "j or down",
    "open": "enter",
    "quit": "q",
    "log-prev": "p",
    "log-next": "n",
    "back": "b or escape",
    "cancel": "c",
}


def serve_terminal(state):
    board = Dashboard(state)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stdout.write(board.render())
        return
    _interact(board)


def decode_input(data, flush=False):
    """Turn key bytes and SGR mouse presses into key or click events."""

    events = []
    index = 0
    while index < len(data):
        if data[index : index + 1] != b"\x1b":
            piece = data[index : index + 1]
            if piece in (b"\r", b"\n"):
                events.append(("key", "enter"))
            else:
                try:
                    events.append(("key", piece.decode("ascii")))
                except UnicodeDecodeError:
                    pass
            index += 1
            continue
        if data.startswith(b"\x1b[<", index):
            taken = _take_sgr(data, index)
            if taken is None:
                return events, data[index:]
            kind, payload, index = taken
            if kind == "click":
                events.append(("click", payload))
            continue
        if data.startswith(b"\x1b[A", index):
            events.append(("key", "up"))
            index += 3
            continue
        if data.startswith(b"\x1b[B", index):
            events.append(("key", "down"))
            index += 3
            continue
        if data.startswith(b"\x1b[", index):
            final = _csi_end(data, index + 2)
            if final is None:
                return events, data[index:]
            index = final
            continue
        if index + 1 == len(data) and not flush:
            return events, data[index:]
        events.append(("key", "escape"))
        index += 1
    return events, b""


def apply_input(board, data):
    events, leftover = decode_input(data, flush=True)
    for event in events:
        if event[0] == "key":
            board.press(event[1])
        elif event[0] == "click":
            board.click(event[1])
    return leftover


class Dashboard:
    def __init__(self, state):
        self.state = str(state)
        self.quit = False
        self.unavailable = False
        self.screen = "queue"
        self.selected = 0
        self.runs = []
        self.notes = []
        self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.detail = None
        self.log_pages = []
        self.log_index = 0
        self.log_cursor = None
        self.log_end = True
        self.artifacts = []
        self.artifact_index = 0
        self.artifact_text = ""
        self.artifact_error = None
        self._load_queue()

    def press(self, key):
        if self.quit:
            return
        if self.unavailable and not self._reconnect():
            return
        for screen, action, keys, _token in BINDINGS:
            if screen == self.screen and key in keys:
                self._act(action)
                return

    def click(self, line):
        if self.quit or type(line) is not int:
            return
        if self.unavailable and not self._reconnect():
            return
        rows = self.render().splitlines()
        if line < 0 or line >= len(rows):
            return
        found = _TARGET.search(rows[line])
        if not found:
            return
        name = found.group(1)
        if name == "run" and self.screen == "queue":
            self.selected = _nth(rows, line, "run")
            return
        if name == "artifact" and self.screen == "detail":
            self._select_artifact(_nth(rows, line, "artifact"))
            return
        token = f"[{name}]"
        for screen, action, _keys, bound in BINDINGS:
            if screen == self.screen and bound == token:
                self._act(action)
                return

    def render(self):
        if self.unavailable:
            lines = [
                f"Run state as of {self.timestamp}.",
                f"This snapshot is stale after {self.timestamp}.",
                "worker socket is not available",
            ]
            return "\n".join(lines) + "\n"
        if self.screen == "detail" and self.detail is not None:
            return "\n".join(self._detail_lines()) + "\n"
        return "\n".join(self._queue_lines()) + "\n"

    def _act(self, action):
        if self.screen == "queue":
            if action == "next":
                self._move_run(1)
            elif action == "previous":
                self._move_run(-1)
            elif action == "open":
                self._open()
            elif action == "cancel":
                self._cancel()
            elif action == "quit":
                self.quit = True
            return
        if action == "next":
            self._move_artifact(1)
        elif action == "previous":
            self._move_artifact(-1)
        elif action == "log-next":
            self._log_next()
        elif action == "log-prev":
            self._log_prev()
        elif action == "cancel":
            self._cancel()
        elif action == "back":
            self.screen = "queue"
        elif action == "quit":
            self.quit = True

    def _queue_lines(self):
        lines = [
            f"Run state as of {self.timestamp}.",
            f"This snapshot is stale after {self.timestamp}.",
        ]
        lines.extend(_controls("queue", ("previous", "next", "open", "cancel", "quit")))
        for index, run in enumerate(self.runs):
            mark = ">" if index == self.selected else " "
            lines.append(f"{mark} [run] {run['run_id']} {run['state']} result {_result_name(run)}")
        lines.extend(self.notes)
        return lines

    def _detail_lines(self):
        record = self.detail
        lines = _controls("detail", ("back", "quit", "previous", "next", "cancel"))
        if self.log_index > 0:
            lines.extend(_controls("detail", ("log-prev",)))
        if self._can_log_next():
            lines.extend(_controls("detail", ("log-next",)))
        lines.append(f"run {record['run_id']}")
        lines.append(f"state {record['state']}")
        lines.append(f"result {_result_name(record)}")
        lines.append(f"exit_code {_shown_exit(record)}")
        lines.append(f"cleanup {record.get('cleanup')}")
        error = record.get("error")
        if isinstance(error, dict) and isinstance(error.get("kind"), str) and error["kind"] != "":
            lines.append(f"error {error['kind']}")
        snapshot = (record.get("input") or {}).get("snapshot_id")
        lines.append(f"snapshot {snapshot}")
        lines.append("steps")
        steps = record.get("steps") or []
        if not steps:
            lines.append("(none)")
        for step in steps:
            name = step.get("name") or step.get("id") or "step"
            lines.append(f"{step.get('index')} {name} {step.get('status')}")
        lines.append("log")
        if self.log_pages:
            lines.append(self.log_pages[self.log_index])
        lines.append("artifacts")
        if self.artifact_error:
            lines.append(self.artifact_error)
            return lines
        lines.append("bytes are not masked")
        for index, item in enumerate(self.artifacts):
            mark = ">" if index == self.artifact_index else " "
            lines.append(f"{mark} [artifact] {item['id']} {item['path']}")
        if self.artifact_text:
            lines.append(self.artifact_text)
        return lines

    def _move_run(self, delta):
        if not self.runs:
            return
        self.selected = max(0, min(len(self.runs) - 1, self.selected + delta))

    def _move_artifact(self, delta):
        if not self.artifacts:
            return
        self._select_artifact(self.artifact_index + delta)

    def _select_artifact(self, index):
        if not self.artifacts:
            return
        self.artifact_index = max(0, min(len(self.artifacts) - 1, index))
        self._read_artifact()

    def _reconnect(self):
        self.unavailable = False
        self._load_queue()
        return not self.unavailable

    def _selected_id(self):
        if self.screen == "detail" and isinstance(self.detail, dict):
            run_id = self.detail.get("run_id")
            if isinstance(run_id, str) and run_id != "":
                return run_id
        if not self.runs:
            return None
        return self.runs[self.selected]["run_id"]

    def _cancel(self):
        run_id = self._selected_id()
        if run_id is None:
            return
        reply = self._call("run.cancel", {"version": 0, "run_id": run_id})
        if reply is None:
            return
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(reply, dict) or "error" in reply or not isinstance(result, dict):
            note = _error_line(run_id, reply) if isinstance(reply, dict) else None
            self.notes.append(note or "cancel worker response was not a protocol result")
            return
        self._show_returned(result)

    def _show_returned(self, record):
        self.detail = record
        self.screen = "detail"
        for index, run in enumerate(self.runs):
            if run.get("run_id") == record.get("run_id"):
                self.runs[index] = record
                self.selected = index
                break
        self.artifact_index = 0
        self._fetch_log(None, append=False)
        self._load_artifacts()

    def _open(self):
        if not self.runs:
            return
        run_id = self.runs[self.selected]["run_id"]
        reply = self._call("run.get", {"run_id": run_id})
        if reply is None or "error" in reply:
            return
        self.detail = reply["result"]
        self.screen = "detail"
        self.artifact_index = 0
        self._fetch_log(None, append=False)
        self._load_artifacts()

    def _log_next(self):
        if self.log_index + 1 < len(self.log_pages):
            self.log_index += 1
            return
        if not self._can_log_next():
            return
        self._fetch_log(self.log_cursor, append=True)

    def _log_prev(self):
        if self.log_index > 0:
            self.log_index -= 1

    def _can_log_next(self):
        return self.log_index + 1 < len(self.log_pages) or (
            not self.log_end and isinstance(self.log_cursor, str) and self.log_cursor != ""
        )

    def _fetch_log(self, cursor, append):
        params = {"run_id": self.detail["run_id"]}
        if cursor is not None:
            params["cursor"] = cursor
        reply = self._call("run.logs", params)
        if reply is None:
            return
        if "error" in reply:
            self.log_pages = [_error_line("log", reply) or "log"]
            self.log_index = 0
            self.log_end = True
            self.log_cursor = None
            return
        result = reply["result"]
        text = _decode_b64(result.get("data_base64"))
        ended = result.get("end_of_stream") is True or text == ""
        cursor_value = result.get("next_cursor")
        self.log_end = ended or not isinstance(cursor_value, str) or cursor_value == ""
        self.log_cursor = None if self.log_end else cursor_value
        if append:
            self.log_pages.append(text)
            self.log_index = len(self.log_pages) - 1
        else:
            self.log_pages = [text]
            self.log_index = 0

    def _load_artifacts(self):
        artifacts = []
        params = {}
        seen = set()
        while True:
            params["run_id"] = self.detail["run_id"]
            reply = self._call("run.artifacts", params)
            if reply is None:
                return
            if "error" in reply:
                self.artifact_error = _error_line(self.detail["run_id"], reply)
                self.artifacts = []
                self.artifact_text = ""
                return
            result = reply["result"]
            page = result.get("artifacts") if isinstance(result, dict) else None
            if not isinstance(page, list):
                self.artifact_error = "artifacts worker response was not a protocol result"
                self.artifacts = []
                self.artifact_text = ""
                return
            artifacts.extend(page)
            cursor = result.get("next_cursor")
            if not isinstance(cursor, str) or cursor == "" or cursor in seen:
                break
            seen.add(cursor)
            params = {"cursor": cursor}
        self.artifact_error = None
        self.artifacts = artifacts
        self.artifact_index = 0
        self._read_artifact()

    def _read_artifact(self):
        if not self.artifacts:
            self.artifact_text = ""
            return
        item = self.artifacts[self.artifact_index]
        reply = self._call("artifact.read", {"artifact_id": item["id"]})
        if reply is None:
            return
        if "error" in reply:
            self.artifact_text = _error_line(item["id"], reply) or ""
            return
        self.artifact_text = _decode_b64(reply["result"].get("data_base64"))

    def _load_queue(self):
        runs, error = _list_runs(self._call)
        if self.unavailable:
            return
        self.runs = list(runs)
        self.notes = []
        if error is not None:
            self.notes.append(error)
        for run in self.runs:
            reply = self._call("run.artifacts", {"run_id": run["run_id"]})
            if reply is None:
                return
            note = _error_line(run["run_id"], reply)
            if note is not None:
                self.notes.append(note)
        if self.selected >= len(self.runs):
            self.selected = 0

    def _call(self, method, params):
        from .cli import call

        try:
            return call(self.state, method, params)
        except OSError:
            self.unavailable = True
            self.screen = "queue"
            return None


def _result_name(record):
    """Succeeded is a result only when the record also exited 0."""

    if record.get("state") == "succeeded" and record.get("exit_code") == 0:
        return "succeeded"
    if record.get("state") == "succeeded":
        return "not succeeded"
    state = record.get("state")
    if isinstance(state, str) and state != "":
        return state
    return "unknown"


def _shown_exit(record):
    code = record.get("exit_code")
    if type(code) is int:
        return str(code)
    return "none"


def _controls(screen, names):
    lines = []
    for name in names:
        for bound_screen, action, _keys, token in BINDINGS:
            if bound_screen == screen and action == name:
                lines.append(f"{token} {_KEY_LABEL[action]}")
                break
    return lines


def _list_runs(call):
    runs = []
    params = {}
    seen = set()
    while True:
        reply = call("run.list", params)
        if reply is None:
            return (), None
        if "error" in reply:
            return (), _error_line("run.list", reply)
        result = reply["result"]
        page = result.get("runs") if isinstance(result, dict) else None
        if not isinstance(page, list):
            return (), "run.list worker response was not a protocol result"
        runs.extend(page)
        cursor = result.get("next_cursor") if isinstance(result, dict) else None
        if not isinstance(cursor, str) or cursor == "" or cursor in seen:
            return tuple(runs), None
        seen.add(cursor)
        params = {"cursor": cursor}


def _error_line(label, reply):
    if not isinstance(reply, dict) or "error" not in reply:
        return None
    error = reply["error"] if isinstance(reply["error"], dict) else {}
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    kind = data.get("kind")
    if not isinstance(kind, str) or kind == "":
        kind = "INTERNAL_ERROR"
    message = error.get("message")
    if not isinstance(message, str):
        message = ""
    return " ".join(f"{label} {kind} {message}".split()).rstrip()


def _decode_b64(value):
    if not isinstance(value, str):
        return ""
    try:
        raw = base64.b64decode(value)
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", "replace")


def _nth(rows, line, name):
    seen = 0
    token = f"[{name}]"
    for index, row in enumerate(rows):
        if token in row:
            if index == line:
                return seen
            seen += 1
    return 0


def _take_sgr(data, index):
    end = None
    for cursor in range(index + 3, len(data)):
        if data[cursor] in b"Mm":
            end = cursor
            break
    if end is None:
        return None
    parts = data[index + 3 : end].split(b";")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return ("skip", None, end + 1)
    button = int(parts[0])
    row = int(parts[2])
    if data[end : end + 1] == b"M" and button == 0 and row >= 1:
        return ("click", row - 1, end + 1)
    return ("skip", None, end + 1)


def _csi_end(data, index):
    while index < len(data):
        if 0x40 <= data[index] <= 0x7E:
            return index + 1
        index += 1
    return None


def _interact(board):
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()
        pending = b""
        while not board.quit:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write(board.render())
            sys.stdout.flush()
            chunk = os.read(fd, 64)
            if chunk == b"":
                return
            pending += chunk
            events, pending = decode_input(pending)
            if pending == b"\x1b":
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    events, pending = decode_input(pending, flush=True)
            for event in events:
                if event[0] == "key":
                    board.press(event[1])
                elif event[0] == "click":
                    board.click(event[1])
    finally:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
