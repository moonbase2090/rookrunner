"""Local dashboard options.

Both commands read the worker socket and bind no port. Neither is
a chosen typeface, color, or layout.
"""

from datetime import datetime, timezone
from html import escape
from pathlib import Path


def render_terminal(state):
    return _text(collect(state))


def write_html(state, output):
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><title>Rookrunner runs</title></head>\n'
        "<body>\n<pre>" + escape(_text(collect(state))) + "</pre>\n</body>\n</html>\n"
    )
    Path(output).write_text(document, encoding="utf-8")


def collect(state):
    from .cli import call

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        runs, list_error = _list_runs(call, state)
    except OSError:
        return _view(timestamp, True, (), ())
    notes = []
    if list_error is not None:
        notes.append(list_error)
    for run in runs:
        try:
            reply = call(state, "run.artifacts", {"run_id": run["run_id"]})
        except OSError:
            return _view(timestamp, True, (), ())
        note = _error_line(run["run_id"], reply)
        if note is not None:
            notes.append(note)
    return _view(timestamp, False, tuple(runs), tuple(notes))


def _list_runs(call, state):
    runs = []
    params = {}
    seen = set()
    while True:
        reply = call(state, "run.list", params)
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
    if "error" not in reply:
        return None
    error = reply["error"] if isinstance(reply["error"], dict) else {}
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    kind = data.get("kind")
    if not isinstance(kind, str) or kind == "":
        kind = "INTERNAL_ERROR"
    message = error.get("message")
    if not isinstance(message, str):
        message = ""
    return _one_line(f"{label} {kind} {message}".rstrip())


def _view(timestamp, unavailable, runs, notes):
    return {"timestamp": timestamp, "unavailable": unavailable, "runs": runs, "notes": notes}


def _text(view):
    lines = [
        f"Run state as of {view['timestamp']}.",
        f"This snapshot is stale after {view['timestamp']}.",
    ]
    if view["unavailable"]:
        lines.append("worker socket is not available")
    else:
        for run in view["runs"]:
            lines.append(_one_line(f"{run['run_id']} {run['state']}"))
        lines.extend(view["notes"])
    return "\n".join(lines) + "\n"


def _one_line(value):
    return " ".join(str(value).split())
