# SPDX-License-Identifier: MIT

"""Bounded, restartable migration of legacy poll reporting references."""

VERSION = 1
DEFAULT_BATCH_SIZE = 100
UNREADABLE = "snapshot is not readable"
RETRY_FIELDS = (
    "run_id",
    "event",
    "status_sha",
    "host",
    "retry_base",
    "context",
    "tested_commit",
    "superseded",
    "abandoned",
    "reporting_disposition",
)


def reporting_ready(state, entry):
    migration = state.get("reporting_migration", {})
    return (
        entry.get("reporting_version") == VERSION
        or migration.get("complete") is True
        or (isinstance(migration.get("cursor"), str) and entry["key"] <= migration["cursor"])
    )


def tombstone(state, entry):
    completed = state.setdefault("completed", {})
    if isinstance(completed, list):
        completed = state["completed"] = {key: {} for key in completed}
    completed[entry["key"]] = {
        **{field: entry[field] for field in RETRY_FIELDS if field in entry},
        "reporting_disposition": "snapshot_unreadable",
        "reporting_version": VERSION,
    }
    state["submissions"] = [row for row in state["submissions"] if row is not entry]


def migrate_reporting(
    state, *, caller_for, save, batch_size=DEFAULT_BATCH_SIZE, dry_run=False, restart=False
):
    """Inspect at most one batch. Only proven terminal unreadable rows retire.

    The cursor and tombstones commit together. Worker disposal is idempotent,
    so a crash before that commit can safely repeat the same batch.
    """

    from .poll import PollError

    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise PollError("INVALID_PARAMS", "migration batch size must be from 1 to 1000")
    previous = state.get("reporting_migration")
    if previous is not None and (
        not isinstance(previous, dict)
        or type(previous.get("version")) is not int
        or previous["version"] != VERSION
        or type(previous.get("complete")) is not bool
        or type(previous.get("processed")) is not int
        or previous["processed"] < 0
        or (previous.get("cursor") is not None and not isinstance(previous["cursor"], str))
    ):
        raise PollError("STATE_INVALID", "poll reporting migration metadata is invalid")
    progress = dict(
        previous
        if previous and not restart
        else {"version": VERSION, "cursor": None, "complete": False, "processed": 0}
    )
    completed = state.get("completed", {})
    if not isinstance(completed, dict | list) or (
        isinstance(completed, list) and any(not isinstance(key, str) for key in completed)
    ):
        raise PollError("STATE_INVALID", "poll completed references are invalid")
    rows = state.get("submissions")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("key"), str) for row in rows
    ):
        raise PollError("STATE_INVALID", "poll submissions are invalid")
    if len({row["key"] for row in rows}) != len(rows):
        raise PollError("STATE_INVALID", "poll submission keys are not unique")
    pending = sorted(
        (row for row in rows if not reporting_ready({"reporting_migration": progress}, row)),
        key=lambda row: row["key"],
    )
    results = []
    for entry in pending[:batch_size]:
        disposition = "waiting"
        caller = caller_for(entry)
        if "run_id" in entry:
            disposition = "unreachable"
            if caller is not None:
                try:
                    record = caller("run.get", {"run_id": entry["run_id"]})
                    disposition = record["state"]
                    if disposition in {"succeeded", "failed", "cancelled"}:
                        params = {
                            field: entry[field]
                            for field in ("run_id", "tested_commit", "status_sha", "context")
                        }
                        try:
                            decision = caller("run.status", params)
                            if decision == {"action": "disposed", "state": "error"}:
                                disposition = "would_tombstone"
                                if not dry_run:
                                    tombstone(state, entry)
                                    disposition = "tombstoned"
                            else:
                                disposition = "reportable"
                        except PollError as error:
                            if error.kind != "STATUS_REFUSED" or str(error) != UNREADABLE:
                                raise
                            disposition = "would_tombstone"
                            if not dry_run:
                                disposed = caller(
                                    "run.status", {**params, "dispose": "snapshot_unreadable"}
                                )
                                if disposed != {"action": "disposed", "state": "error"}:
                                    raise PollError(
                                        "WORKER_ERROR", "worker did not record reporting disposal"
                                    ) from None
                                tombstone(state, entry)
                                disposition = "tombstoned"
                except (PollError, OSError, ValueError, KeyError, TypeError):
                    disposition = "unreachable"
        results.append(
            {"key": entry["key"], "run_id": entry.get("run_id"), "disposition": disposition}
        )
        progress["cursor"] = entry["key"]
    progress["processed"] += len(results)
    progress["complete"] = len(pending) <= batch_size
    if not dry_run:
        state["reporting_migration"] = progress
        save()
    return {
        "dry_run": dry_run,
        **progress,
        "scanned": len(results),
        "remaining": max(0, len(pending) - len(results)),
        "items": results,
    }
