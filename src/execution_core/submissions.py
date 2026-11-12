# SPDX-License-Identifier: MIT
"""Operator commands for poll submissions that have no final commit status.

``list`` and ``inspect`` read. ``repair`` posts the run's final status,
including ``error`` for a setup failure the worker still reports as
``retry``. ``abandon`` posts ``error`` and never ``success``. ``retry``
submits one successor under the derived poll key. ``--dry-run`` does not
post, submit, or write ``poll.json``.
"""

import contextlib
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import urllib.parse
from datetime import UTC, datetime

from .poll import PollError, _save_state, has_successor, is_capacity_wait, retry_submission_key
from .protocol import strict_json
from .status import TERMINAL_STATUS, StatusError, github_state, require_repository

_MAX_BODY = 1024 * 1024
_ACTIVE = frozenset({"queued", "running"})
_RETRY_EVENTS = frozenset({"push", "pull_request"})
_RETRY_STATES = frozenset({"failed", "cancelled"})
_CAPACITY_KEY = re.compile(r"poll-[0-9a-f]{64}\Z")
_KEPT = (
    "run_id",
    "event",
    "status_sha",
    "host",
    "retry_base",
    "context",
    "tested_commit",
    "superseded",
    "abandoned",
)


def load_poll_state(directory):
    """Return poll.json. A symlink, a wrong version, or a missing file is refused."""

    path = Path(directory) / "poll.json"
    if not path.exists():
        raise PollError("STATE_INVALID", "poll state is missing")
    if path.is_symlink():
        raise PollError("STATE_INVALID", "poll state must not be a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PollError("STATE_INVALID", "poll state could not be read") from None
    try:
        blob = os.read(fd, _MAX_BODY + 1)
    finally:
        os.close(fd)
    if len(blob) > _MAX_BODY:
        raise PollError("STATE_INVALID", "poll state could not be read")
    try:
        parsed = strict_json(blob)
    except (ValueError, UnicodeError, RecursionError):
        raise PollError("STATE_INVALID", "poll state could not be read") from None
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or not isinstance(parsed.get("submissions"), list)
    ):
        raise PollError("STATE_INVALID", "poll state could not be read")
    return parsed


def capacity_report(directory, *, alert_after_seconds=None, now=None):
    """Report persisted capacity waits for one repository without contacting a worker."""

    if alert_after_seconds is not None and (
        type(alert_after_seconds) is not int or alert_after_seconds <= 0
    ):
        raise PollError("INVALID_PARAMS", "capacity age threshold must be positive seconds")
    observed_at = datetime.now(UTC) if now is None else now
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise PollError("INVALID_PARAMS", "capacity observation time must include a time zone")
    observed_at = observed_at.astimezone(UTC)

    state = load_poll_state(directory)
    try:
        repository = require_repository(state.get("repository"))
    except StatusError:
        raise PollError("STATE_INVALID", "poll state repository is not usable") from None

    waits = []
    keys = set()
    for entry in state["submissions"]:
        if not is_capacity_wait(entry):
            continue
        waiting = entry["waiting"]
        request = waiting.get("request")
        key = entry.get("key")
        if not isinstance(key, str) or _CAPACITY_KEY.fullmatch(key) is None or key in keys:
            raise PollError("STATE_INVALID", "capacity wait identity is not usable")
        if not isinstance(request, dict):
            raise PollError("STATE_INVALID", "capacity wait request is not usable")
        try:
            since_value = waiting.get("since")
            if not isinstance(since_value, str):
                raise ValueError
            since = datetime.fromisoformat(since_value.replace("Z", "+00:00"))
            if since.tzinfo is None or since.utcoffset() is None:
                raise ValueError
            since = since.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            raise PollError("STATE_INVALID", "capacity wait time is not usable") from None
        if since > observed_at:
            raise PollError("STATE_INVALID", "capacity wait time is in the future")

        event = entry.get("event")
        ref = waiting.get("ref")
        workflow = request.get("workflow")
        job_id = request.get("job_id")
        if any(
            not isinstance(value, str) or value == "" for value in (event, ref, workflow, job_id)
        ):
            raise PollError("STATE_INVALID", "capacity wait details are not usable")
        keys.add(key)
        waits.append(
            (
                since,
                {
                    "key": key,
                    "since": since.isoformat(),
                    "age_seconds": int((observed_at - since).total_seconds()),
                    "event": event,
                    "ref": ref,
                    "workflow": workflow,
                    "job_id": job_id,
                },
            )
        )

    waits.sort(key=lambda item: (item[0], item[1]["key"]))
    rows = [row for _, row in waits]
    oldest_age = rows[0]["age_seconds"] if rows else None
    if alert_after_seconds is None:
        alert = "not_configured"
    else:
        alert = (
            "breached" if oldest_age is not None and oldest_age >= alert_after_seconds else "clear"
        )
    return {
        "repository": repository,
        "observed_at": observed_at.isoformat(),
        "queue_depth": len(rows),
        "oldest_since": rows[0]["since"] if rows else None,
        "oldest_age_seconds": oldest_age,
        "alert_after_seconds": alert_after_seconds,
        "alert": alert,
        "waits": rows,
    }


def recorded_status_states(db_path, run_id, context, sha):
    """Return commit-status states stored for one run, context, and SHA."""

    path = Path(db_path)
    if not path.is_file():
        return set()
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = db.execute(
            "SELECT state FROM status_posts WHERE run_id=? AND context=? AND sha=?",
            (run_id, context, sha),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        db.close()
    return {row[0] for row in rows if isinstance(row[0], str)}


def stored_request(db_path, run_id):
    """Return the workflow request stored for ``run_id``."""

    path = Path(db_path)
    if not path.is_file():
        raise PollError("RUN_NOT_FOUND", "run does not exist")
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise PollError("STATE_INVALID", "stored request is not usable") from None
    try:
        row = db.execute("SELECT request FROM runs WHERE id=?", (run_id,)).fetchone()
    except sqlite3.Error:
        raise PollError("STATE_INVALID", "stored request is not usable") from None
    finally:
        db.close()
    if row is None or not isinstance(row[0], str):
        raise PollError("RUN_NOT_FOUND", "run does not exist")
    try:
        request = strict_json(row[0])
    except (ValueError, UnicodeError, RecursionError):
        raise PollError("STATE_INVALID", "stored request is not usable") from None
    if not isinstance(request, dict):
        raise PollError("STATE_INVALID", "stored request is not usable")
    return request


def list_submissions(directory, *, caller, recorded_states, stuck_only=True, dry_run=False):
    """Describe active poll submissions. Stuck ones have no final commit status."""

    state = load_poll_state(directory)
    rows = [
        _describe(entry, caller=caller, recorded_states=recorded_states, retired=False)
        for entry in state["submissions"]
        if isinstance(entry, dict)
    ]
    if stuck_only:
        rows = [row for row in rows if row["stuck"]]
    return {"dry_run": dry_run, "submissions": rows}


def inspect_submission(directory, key, *, caller, recorded_states, dry_run=False):
    """Describe one submission, active or completed."""

    _require_key(key)
    state = load_poll_state(directory)
    entry, retired = _find(state, key)
    row = _describe(entry, caller=caller, recorded_states=recorded_states, retired=retired)
    row["dry_run"] = dry_run
    return row


def repair_submission(directory, key, *, caller, recorded_states, post, dry_run=False):
    """Post the missing final commit status and retire the submission.

    A setup failure posts ``error``. A successor already recorded for the
    key is retired without a post. ``success`` is posted only when that is
    the worker's final state.
    """

    _require_key(key)
    with _locked(directory, dry_run):
        state = load_poll_state(directory)
        entry, retired = _find(state, key)
        if has_successor(state, entry):
            if not dry_run and not retired:
                _retire(directory, state, entry, superseded=True)
            return _outcome(entry, "would_supersede" if dry_run else "superseded", dry_run)
        record = _run(caller, entry)
        final = _final_state(record)
        if final == "pending":
            raise PollError("INVALID_PARAMS", "run is not final")
        decision = caller("run.status", _status_params(entry))
        if not isinstance(decision, dict) or decision.get("state") != final:
            raise PollError("WORKER_ERROR", "worker final state does not match the run")
        recorded = recorded_states(entry["run_id"], entry["context"], entry["status_sha"])
        if decision.get("action") == "skip" or recorded & TERMINAL_STATUS:
            if not dry_run and not retired:
                _retire(directory, state, entry)
            return _outcome(entry, "already_final", dry_run, state=final)
        if decision.get("action") == "retry":
            if final != "error":
                raise PollError("WORKER_ERROR", "setup failure did not map to error")
            posting = "error"
        elif decision.get("action") == "post":
            posting = final
        else:
            raise PollError("WORKER_ERROR", "worker status decision was not post, skip, or retry")
        if dry_run:
            return _outcome(entry, "would_post", True, state=posting)
        _require_post(post)
        post(entry["status_sha"], posting, entry["context"])
        caller("run.status", {**_status_params(entry), "record": posting})
        if not retired:
            _retire(directory, state, entry)
        return _outcome(entry, "posted", False, state=posting)


def abandon_submission(directory, key, *, caller, post, dry_run=False):
    """Post ``error`` for one submission and retire it. Never post ``success``."""

    _require_key(key)
    with _locked(directory, dry_run):
        state = load_poll_state(directory)
        entry, retired = _find(state, key)
        if entry.get("abandoned") is True:
            return _outcome(entry, "already_abandoned", dry_run)
        if has_successor(state, entry):
            raise PollError("INVALID_PARAMS", "a successor owns this status context")
        if "run_id" not in entry:
            if dry_run:
                return _outcome(entry, "would_abandon", True)
            if not retired:
                _retire(directory, state, entry, abandoned=True)
            return _outcome(entry, "abandoned", False)
        record = _run(caller, entry)
        final = _final_state(record)
        if final == "success":
            raise PollError("INVALID_PARAMS", "abandon does not post success")
        if final == "pending":
            if dry_run:
                return _outcome(entry, "would_cancel", True)
            caller("run.cancel", {"version": 0, "run_id": entry["run_id"]})
            record = _run(caller, entry)
            final = _final_state(record)
            if final == "success":
                raise PollError("INVALID_PARAMS", "abandon does not post success")
            if final == "pending":
                raise PollError("INVALID_PARAMS", "run is still active")
        _require_context(entry)
        if dry_run:
            return _outcome(entry, "would_post", True, state="error")
        _require_post(post)
        post(entry["status_sha"], "error", entry["context"])
        if final in TERMINAL_STATUS:
            caller("run.status", {**_status_params(entry), "record": final})
        if not retired:
            _retire(directory, state, entry, abandoned=True)
        return _outcome(entry, "abandoned", False, state="error")


def retry_submission(directory, key, *, caller, request_for, dry_run=False):
    """Submit one successor of a failed or cancelled push or pull request. Repeat is a no-op."""

    _require_key(key)
    with _locked(directory, dry_run):
        state = load_poll_state(directory)
        entry, _retired = _find(state, key)
        if entry.get("event") not in _RETRY_EVENTS or "run_id" not in entry:
            raise PollError("INVALID_PARAMS", "retry requires a recorded push or pull-request run")
        successor = retry_submission_key(entry["key"])
        if _successor_recorded(state, entry["key"], successor):
            return _outcome(entry, "already_retried", dry_run, submission_key=successor)
        record = _run(caller, entry)
        if record.get("state") not in _RETRY_STATES:
            raise PollError("INVALID_PARAMS", "only a failed or cancelled run can be retried")
        request = request_for(entry["run_id"])
        if not isinstance(request, dict) or request.get("version") != 1:
            raise PollError("INVALID_PARAMS", "retry requires a workflow submission")
        params = {field: request[field] for field in request if field != "submission_key"}
        params["submission_key"] = successor
        if dry_run:
            return _outcome(
                entry,
                "would_submit",
                True,
                submission_key=successor,
                workflow=request.get("workflow"),
            )
        submitted = caller("run.submit", params)
        if not isinstance(submitted, dict) or not isinstance(submitted.get("run_id"), str):
            raise PollError("WORKER_ERROR", "worker submit did not return a run")
        claimed = {
            "key": successor,
            "retry_of": entry["key"],
            "retry_base": entry.get("retry_base", entry["key"]),
            "run_id": submitted["run_id"],
            "event": entry["event"],
            "tested_commit": entry["tested_commit"],
            "status_sha": entry["status_sha"],
            "context": entry["context"],
            "pending": False,
        }
        if "host" in entry:
            claimed["host"] = entry["host"]
        state["submissions"].append(claimed)
        _save_state(Path(directory) / "poll.json", state)
        return _outcome(
            entry,
            "submitted",
            False,
            submission_key=successor,
            run_id=submitted["run_id"],
            workflow=request.get("workflow"),
        )


def _describe(entry, *, caller, recorded_states, retired):
    row = {
        "key": entry.get("key"),
        "retired": retired,
        "abandoned": entry.get("abandoned") is True,
        "stuck": False,
    }
    for field in ("run_id", "event", "context", "status_sha", "tested_commit"):
        if field in entry:
            row[field] = entry[field]
    if "run_id" not in entry or not _has_context(entry):
        row["stuck"] = True
        return row
    record = caller("run.get", {"run_id": entry["run_id"]})
    if not isinstance(record, dict):
        raise PollError("WORKER_ERROR", "worker run was not readable")
    row["run_state"] = record.get("state")
    error = record.get("error")
    if isinstance(error, dict) and isinstance(error.get("kind"), str):
        row["error_kind"] = error["kind"]
    if record.get("state") in _ACTIVE:
        return row
    recorded = recorded_states(entry["run_id"], entry["context"], entry["status_sha"])
    if recorded & TERMINAL_STATUS:
        row["final"] = _final_state(record)
        return row
    decision = caller("run.status", _status_params(entry))
    if not isinstance(decision, dict):
        raise PollError("WORKER_ERROR", "worker status decision was not post, skip, or retry")
    row["status_action"] = decision.get("action")
    if isinstance(decision.get("state"), str):
        row["final"] = decision["state"]
    row["stuck"] = decision.get("action") != "skip"
    return row


def _run(caller, entry):
    _require_context(entry)
    record = caller("run.get", {"run_id": entry["run_id"]})
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        raise PollError("WORKER_ERROR", "worker run was not readable")
    return record


def _final_state(record):
    final = github_state(record.get("state"), record.get("exit_code"), record.get("error"))
    if final not in {"pending", "success", "failure", "error"}:
        raise PollError("WORKER_ERROR", "worker final state does not match the run")
    return final


def _find(state, key):
    for entry in state["submissions"]:
        if isinstance(entry, dict) and (entry.get("key") == key or entry.get("run_id") == key):
            return entry, False
    completed = state.get("completed")
    if isinstance(completed, dict):
        for completed_key, meta in completed.items():
            if not isinstance(meta, dict):
                continue
            if completed_key == key or meta.get("run_id") == key:
                return {"key": completed_key, **meta}, True
    raise PollError("INVALID_PARAMS", "submission was not found")


def _successor_recorded(state, key, successor):
    for other in state["submissions"]:
        if isinstance(other, dict) and (
            other.get("key") == successor or other.get("retry_of") == key
        ):
            return True
    completed = state.get("completed")
    if isinstance(completed, list):
        return successor in completed
    if isinstance(completed, dict):
        if successor in completed:
            return True
        return any(
            isinstance(meta, dict) and meta.get("retry_of") == key for meta in completed.values()
        )
    return False


def _retire(directory, state, entry, **extra):
    completed = state.get("completed")
    if isinstance(completed, list):
        completed = {item: {} for item in completed if isinstance(item, str)}
    if not isinstance(completed, dict):
        completed = {}
    kept = {field: entry[field] for field in _KEPT if field in entry}
    kept.update(extra)
    completed[entry["key"]] = kept
    state["completed"] = completed
    state["submissions"] = [
        item
        for item in state["submissions"]
        if not isinstance(item, dict) or item.get("key") != entry["key"]
    ]
    _save_state(Path(directory) / "poll.json", state)


def _outcome(entry, action, dry_run, **extra):
    result = {"action": action, "key": entry.get("key"), "dry_run": dry_run}
    if "run_id" in entry and "run_id" not in extra:
        result["run_id"] = entry["run_id"]
    result.update(extra)
    return result


def _status_params(entry):
    _require_context(entry)
    return {
        "run_id": entry["run_id"],
        "tested_commit": entry["tested_commit"],
        "status_sha": entry["status_sha"],
        "context": entry["context"],
    }


def _has_context(entry):
    return (
        isinstance(entry.get("run_id"), str)
        and isinstance(entry.get("tested_commit"), str)
        and isinstance(entry.get("status_sha"), str)
        and isinstance(entry.get("context"), str)
    )


def _require_context(entry):
    if "run_id" not in entry:
        raise PollError("INVALID_PARAMS", "submission has no run")
    if not _has_context(entry):
        raise PollError("INVALID_PARAMS", "submission has no status context")


def _require_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(c in key for c in "\0\r\n"):
        raise PollError("INVALID_PARAMS", "submission key is not accepted")


def _require_post(post):
    if post is None:
        raise PollError("INVALID_PARAMS", "a live submission command posts a commit status")


@contextlib.contextmanager
def _locked(directory, dry_run):
    if dry_run:
        yield
        return
    path = Path(directory) / "poll.lock"
    if path.is_symlink():
        raise PollError("STATE_INVALID", "poll lock must not be a symlink")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise PollError("POLL_BUSY", "another poll pass holds this state directory") from None
    except OSError:
        raise PollError("STATE_INVALID", "poll state could not be locked") from None
    try:
        yield
    finally:
        os.close(fd)


def bound_repository(bound, repository):
    """Return the repository a live post uses. A mismatch is refused."""

    if repository and bound and repository != bound:
        raise PollError("STATE_INVALID", "poll state is bound to another repository")
    chosen = repository or bound
    if not isinstance(chosen, str) or chosen == "":
        raise StatusError("INVALID_PARAMS", "repository must be owner/name")
    return chosen
