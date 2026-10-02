"""Single-user Unix worker.

Version 0 submits a synthetic development fixture. It never launches commands
or reads a workflow. Version 1 captures the repository, verifies that
snapshot, plans one selected job, and commits a queued run. The scheduler
then materializes an attempt, records it, and runs that plan in one
caller-pinned container.
"""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import threading
import uuid
from datetime import datetime, timezone

from . import __version__
from .attempt import AttemptError, materialize_attempt
from .plan import PlanError, plan_snapshot
from .run import RunError, run_job
from .protocol import (
    MAX_CURSOR,
    MAX_JSON_DEPTH,
    MAX_LIST_PAGE,
    MAX_LOG_PAGE,
    MAX_MESSAGE,
    MAX_OFFSET,
    MAX_QUEUE,
    METHODS,
    TERMINAL,
    Fault,
    canonical,
    error_response,
    fields,
    integer,
    invalid,
    is_integer,
    strict_json,
    utf8_string,
    valid_id,
)
from .snapshot import CaptureError, SourceCapture
from .verify import VerifyError, verify_snapshot

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_STEP_TEXT = 65536
_QUEUED = (
    "SELECT record, request FROM runs "
    "WHERE json_extract(record, '$.state')='queued' "
    "ORDER BY sequence LIMIT 1"
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def cursor(kind, identity, offset):
    return base64.urlsafe_b64encode(canonical([kind, identity, offset]).encode()).decode()


def offset_from(value, kind, identity):
    if not utf8_string(value, 1, MAX_CURSOR):
        invalid("cursor must be a nonempty UTF-8 string of at most 1024 characters")
    try:
        decoded = strict_json(base64.b64decode(value, altchars=b"-_", validate=True))
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or decoded[:2] != [kind, identity]
            or type(decoded[2]) is not int
            or not 0 <= decoded[2] <= MAX_OFFSET
        ):
            raise ValueError()
        return decoded[2]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Fault("CURSOR_EXPIRED", "invalid cursor or cursor belongs to another query") from None


class _Abandoned(Exception):
    pass


def _error_text(exc):
    if isinstance(exc, OSError):
        return "workflow attempt could not be prepared"
    text = "".join(ch for ch in " ".join(str(exc).split()) if not 0xD800 <= ord(ch) <= 0xDFFF)
    return text[:512] or "workflow attempt could not be prepared"


def _public_steps(steps):
    stored = []
    for step in steps:
        item = {
            "index": step["index"],
            "id": step.get("id"),
            "name": step.get("name"),
            "status": step["status"],
            "exit_code": step["exit_code"],
            "stdout": step.get("stdout") or "",
            "stderr": step.get("stderr") or "",
            "error": step.get("error"),
        }
        for key in ("id", "name"):
            if item[key] == "":
                item[key] = None
        for key in ("stdout", "stderr"):
            if len(item[key]) > _STEP_TEXT:
                item[key] = item[key][:_STEP_TEXT]
        if isinstance(item["error"], str):
            text = "".join(ch for ch in item["error"] if not 0xD800 <= ord(ch) <= 0xDFFF)[:512]
            item["error"] = text or None
        elif item["error"] is not None:
            item["error"] = None
        stored.append(item)
    return stored


def _workflow_log(steps):
    """UTF-8 bytes of each executed step's stdout, then its stderr.

    `run.logs` pages this blob with the existing protocol page size. The
    captured output is not cut to a separate log-size cap.
    """

    chunks = []
    for step in steps:
        for key in ("stdout", "stderr"):
            text = step.get(key) or ""
            if isinstance(text, str) and text:
                chunks.append(text.encode("utf-8"))
    return b"".join(chunks)


def _step_failure_message(outcome, image_ok):
    if not image_ok:
        return "image digest does not match the accepted pin"
    for step in outcome["steps"]:
        if step.get("status") == "failed" and step.get("error"):
            label = step.get("name") or step.get("id") or f"step {step.get('index')}"
            return f"{label}: {step['error']}"
    return "step failed without an exit code"


class Worker:
    def __init__(self, repository, state):
        self.repository = str(Path(repository).resolve(strict=True))
        if not Path(self.repository).is_dir():
            raise ValueError("repository must be a directory")
        self.state = Path(state).absolute()
        self.stop = threading.Event()
        self.guard = threading.RLock()
        self.db = None
        self.lock = None
        self.server = None
        self.scheduler = None
        self.execution_error = None
        self.socket_path = self.state / "worker.sock"

    def start(self):
        # Never change permissions on a pre-existing shared or foreign directory.
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("state directory must be owned by you with mode 0700, not a symlink")
        self.lock = os.open(
            self.state / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.state / "runs.sqlite3", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    submission_key TEXT NOT NULL UNIQUE,
                    request TEXT NOT NULL,
                    record TEXT NOT NULL,
                    log BLOB NOT NULL
                );
            """)
            with self.db:
                root = self.db.execute(
                    "SELECT value FROM metadata WHERE key='repository'"
                ).fetchone()
                if root and root[0] != self.repository:
                    raise ValueError(
                        "ROOT_MISMATCH: state directory is bound to another repository"
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('repository', ?)", (self.repository,)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('worker_id', ?)", (str(uuid.uuid4()),)
                )
            self.worker_id = self.db.execute(
                "SELECT value FROM metadata WHERE key='worker_id'"
            ).fetchone()[0]
            # The development backend has no external processes or containers.
            with self.db:
                for row in self.db.execute("SELECT record FROM runs").fetchall():
                    record = json.loads(row[0])
                    if record["state"] == "running":
                        record.update(
                            state="lost",
                            finished_at=now(),
                            error={
                                "kind": "WORKER_INTERRUPTED",
                                "message": "worker stopped during execution",
                            },
                            cleanup="confirmed_no_external_resources",
                        )
                        self.save(record)
            if os.path.lexists(self.socket_path):
                if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                    raise ValueError("refusing to replace a non-socket worker.sock")
                self.socket_path.unlink()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(self.socket_path))
            self.owns_socket = True
            os.chmod(self.socket_path, 0o600)
            self.server.listen(16)
            self.server.settimeout(0.2)
            self.scheduler = threading.Thread(target=self.schedule, daemon=True)
            self.scheduler.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join()
            self.scheduler = None
        if self.server:
            self.server.close()
            self.server = None
        if getattr(self, "owns_socket", False):
            self.socket_path.unlink(missing_ok=True)
            self.owns_socket = False
        if self.db:
            self.db.close()
            self.db = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def save(self, record):
        self.db.execute(
            "UPDATE runs SET record=? WHERE id=?", (canonical(record), record["run_id"])
        )

    def get(self, run_id):
        if not utf8_string(run_id, 1, 128):
            invalid("run_id must be a nonempty UTF-8 string of at most 128 characters")
        row = self.db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise Fault("RUN_NOT_FOUND", "run does not exist")
        return json.loads(row[0])

    def schedule(self):
        try:
            self.execute_queue()
        except Exception:
            # Fail closed if storage or execution breaks; never accept work into
            # a dead scheduler. Restart reconciles a still-running record as lost.
            with self.guard:
                self.execution_error = "scheduler stopped; restart worker to reconcile runs"

    def execute_queue(self):
        while not self.stop.is_set():
            with self.guard:
                row = self.db.execute(_QUEUED).fetchone()
                if row:
                    record, request = json.loads(row[0]), json.loads(row[1])
                    kind = record["input"]["kind"]
                    if kind in {"development_fixture", "workflow_job"}:
                        record.update(
                            state="running", started_at=now(), attempt_id=str(uuid.uuid4())
                        )
                        with self.db:
                            self.save(record)
                    else:
                        self._fail_unclaimed(record)
                        row = None
            if not row:
                self.stop.wait(0.02)
                continue
            if kind == "development_fixture":
                self._finish_fixture(record, request)
            else:
                self._execute_workflow(record, request)

    def _finish_fixture(self, record, request):
        interrupted = self.stop.wait(request["fixture"]["delay_ms"] / 1000)
        with self.guard, self.db:
            record = self.get(record["run_id"])
            if record["state"] in TERMINAL:
                return
            if interrupted:
                record.update(
                    state="lost",
                    error={
                        "kind": "WORKER_INTERRUPTED",
                        "message": "worker stopped during execution",
                    },
                )
            else:
                code = request["fixture"]["exit_code"]
                record.update(state="succeeded" if code == 0 else "failed", exit_code=code)
                self.db.execute(
                    "UPDATE runs SET log=? WHERE id=?",
                    (request["fixture"]["output"].encode(), record["run_id"]),
                )
            record.update(finished_at=now(), cleanup="confirmed_no_external_resources")
            self.save(record)

    def _fail_unclaimed(self, record):
        record.update(
            state="failed",
            exit_code=None,
            started_at=now(),
            finished_at=now(),
            attempt_id=str(uuid.uuid4()),
            error={"kind": "SETUP_FAILED", "message": "queued run is not executable"},
            cleanup="confirmed_no_external_resources",
        )
        with self.db:
            self.save(record)

    def _execute_workflow(self, record, request):
        workspace = Path(self.state) / "attempts" / record["attempt_id"]
        try:
            outcome = self._run_accepted(record, request)
        except _Abandoned:
            self._remove_workspace(workspace)
            return
        except Exception as exc:
            message = (
                _error_text(exc)
                if isinstance(
                    exc,
                    (
                        RunError,
                        AttemptError,
                        VerifyError,
                        PlanError,
                        OSError,
                        UnicodeError,
                        ValueError,
                    ),
                )
                else "workflow attempt could not be prepared"
            )
            self._finish_setup_failure(record["run_id"], message)
            return
        self._finish_workflow(record["run_id"], outcome)

    def _run_accepted(self, record, request):
        pinned = record["input"]
        event = request["event"]
        event_digest = hashlib.sha256(canonical(event).encode("ascii")).hexdigest()
        if event_digest != pinned["event_digest"]:
            raise RunError("SETUP_FAILED", "event digest does not match the accepted event")
        image = pinned["image_reference"]
        if not isinstance(image, str) or not (
            _IMAGE_ID.fullmatch(image) or _IMAGE_REF.fullmatch(image)
        ):
            raise RunError("SETUP_FAILED", "image is not pinned by digest")
        if "sha256:" + image.rsplit("sha256:", 1)[1] != pinned["image_digest"]:
            raise RunError("SETUP_FAILED", "image digest does not match the accepted pin")
        snapshot = Path(self.state) / "snapshots" / pinned["snapshot_id"]
        manifest = verify_snapshot(snapshot, pinned["digest"])
        if (
            manifest.get("workflow") != pinned["workflow"]
            or manifest.get("workflow_digest") != pinned["workflow_digest"]
        ):
            raise RunError("SETUP_FAILED", "snapshot workflow does not match the accepted digest")
        planned = plan_snapshot(snapshot, pinned["job_id"])
        if planned["digest"] != pinned["plan_digest"]:
            raise RunError("SETUP_FAILED", "planned digest does not match the accepted plan")
        if self._abandoned(record["run_id"]):
            raise _Abandoned()
        attempts = Path(self.state) / "attempts"
        if attempts.is_symlink():
            raise RunError("SETUP_FAILED", "attempt directory is not accepted")
        attempts.mkdir(mode=0o700, exist_ok=True)
        os.chmod(attempts, 0o700)
        materialize_attempt(snapshot, pinned["digest"], attempts / record["attempt_id"])
        if self._abandoned(record["run_id"]):
            raise _Abandoned()
        return run_job(
            snapshot,
            pinned["digest"],
            attempts / record["attempt_id"],
            planned["plan"],
            image,
            event,
        )

    def _abandoned(self, run_id):
        with self.guard:
            return self.get(run_id)["state"] in TERMINAL

    def _finish_setup_failure(self, run_id, message):
        with self.guard, self.db:
            current = self.get(run_id)
            if current["state"] in TERMINAL:
                return
            current.update(
                state="failed",
                exit_code=None,
                finished_at=now(),
                error={"kind": "SETUP_FAILED", "message": message[:512]},
                cleanup="confirmed_no_external_resources",
            )
            self.save(current)

    def _finish_workflow(self, run_id, outcome):
        with self.guard, self.db:
            current = self.get(run_id)
            if current["state"] in TERMINAL:
                return
            steps = _public_steps(outcome["steps"])
            exit_code = outcome["exit_code"]
            image_ok = outcome["image_digest"] == current["input"]["image_digest"]
            if outcome["status"] == "succeeded" and exit_code == 0 and image_ok:
                current.update(state="succeeded", exit_code=0, error=None, steps=steps)
            elif type(exit_code) is int and 1 <= exit_code <= 255 and image_ok:
                current.update(state="failed", exit_code=exit_code, error=None, steps=steps)
            else:
                current.update(
                    state="failed",
                    exit_code=None,
                    error={
                        "kind": "SETUP_FAILED" if not image_ok else "STEP_FAILED",
                        "message": _step_failure_message(outcome, image_ok)[:512],
                    },
                    steps=steps,
                )
            current.update(finished_at=now(), cleanup="confirmed_no_external_resources")
            self.db.execute(
                "UPDATE runs SET log=? WHERE id=?",
                (_workflow_log(outcome["steps"]), run_id),
            )
            self.save(current)

    def _remove_workspace(self, workspace):
        attempts = Path(self.state) / "attempts"
        try:
            if workspace.is_symlink() or workspace.parent != attempts or not workspace.is_dir():
                return
            shutil.rmtree(workspace)
        except OSError:
            return

    def submit_workflow(self, p):
        fields(p, ("version", "submission_key", "workflow", "job_id", "event", "image"))
        key = p["submission_key"]
        if not utf8_string(key, 1, 128):
            invalid("submission_key must be a UTF-8 string of 1 to 128 characters")
        if not utf8_string(p["workflow"], 1, 1024) or not utf8_string(p["job_id"], 1, 128):
            invalid("workflow and job_id must be UTF-8 strings within their limits")
        if not isinstance(p["image"], str) or not (
            _IMAGE_ID.fullmatch(p["image"]) or _IMAGE_REF.fullmatch(p["image"])
        ):
            invalid("image is not pinned by digest")
        try:
            event_text = canonical(p["event"])
        except (TypeError, ValueError, UnicodeError):
            invalid("event input is not accepted")
        normalized = canonical(
            {
                "version": 1,
                "workflow": p["workflow"],
                "job_id": p["job_id"],
                "event": p["event"],
                "image": p["image"],
            }
        )
        existing = self.db.execute(
            "SELECT request, record FROM runs WHERE submission_key=?", (key,)
        ).fetchone()
        if existing:
            if existing[0] != normalized:
                raise Fault(
                    "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
                )
            return json.loads(existing[1])
        if self.stop.is_set() or self.execution_error is not None:
            raise Fault(
                "WORKER_NOT_READY",
                "worker cannot accept new execution; inspect worker.describe",
            )
        queued = self.db.execute(
            "SELECT count(*) FROM runs WHERE json_extract(record, '$.state')='queued'"
        ).fetchone()[0]
        if queued >= MAX_QUEUE:
            raise Fault("QUEUE_FULL", "queued run limit reached")
        try:
            captured = SourceCapture(self.repository, self.state).capture(p["workflow"])
        except CaptureError as exc:
            self._capture_fault(exc)
        snapshot = Path(self.state) / "snapshots" / captured["snapshot_id"]
        try:
            manifest = verify_snapshot(snapshot, captured["digest"])
            planned = plan_snapshot(snapshot, p["job_id"])
        except VerifyError as exc:
            self._drop_snapshot(captured["snapshot_id"])
            raise Fault("INTERNAL_ERROR", "captured snapshot failed verification") from exc
        except PlanError as exc:
            self._drop_snapshot(captured["snapshot_id"])
            kind = (
                "CAPABILITY_UNSUPPORTED"
                if exc.kind == "CAPABILITY_UNSUPPORTED"
                else "INVALID_PARAMS"
            )
            raise Fault(kind, str(exc)[:512]) from exc
        except (OSError, UnicodeError, ValueError) as exc:
            self._drop_snapshot(captured["snapshot_id"])
            raise Fault("INVALID_PARAMS", "workflow snapshot could not be planned") from exc
        image_digest = "sha256:" + p["image"].rsplit("sha256:", 1)[1]
        record = {
            "run_id": str(uuid.uuid4()),
            "worker_id": self.worker_id,
            "submission_key": key,
            "state": "queued",
            "exit_code": None,
            "input": {
                "kind": "workflow_job",
                "digest": captured["digest"],
                "snapshot_id": captured["snapshot_id"],
                "workflow": manifest["workflow"],
                "workflow_digest": captured["workflow_digest"],
                "plan_digest": planned["digest"],
                "job_id": p["job_id"],
                "event_digest": hashlib.sha256(event_text.encode("ascii")).hexdigest(),
                "image_digest": image_digest,
                "image_reference": p["image"],
            },
            "backend": {"name": "workflow", "version": __version__},
            "compatibility_notes": [
                "One sequential run job in a caller-pinned container.",
                "Expressions, actions, needs, secrets, matrices, and services are not claimed.",
            ],
            "accepted_at": now(),
            "started_at": None,
            "finished_at": None,
            "attempt_id": None,
            "cancel_requested": False,
            "error": None,
            "cleanup": "not_started",
        }
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                    (record["run_id"], key, normalized, canonical(record), b""),
                )
        except sqlite3.IntegrityError:
            self._drop_snapshot(captured["snapshot_id"])
            existing = self.db.execute(
                "SELECT request, record FROM runs WHERE submission_key=?", (key,)
            ).fetchone()
            if existing and existing[0] == normalized:
                return json.loads(existing[1])
            raise Fault(
                "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
            ) from None
        return record

    @staticmethod
    def _capture_fault(exc):
        message = str(exc)[:512]
        if exc.kind == "CAPABILITY_UNSUPPORTED":
            raise Fault("CAPABILITY_UNSUPPORTED", message) from exc
        if exc.kind == "SOURCE_UNSTABLE":
            raise Fault("SOURCE_UNSTABLE", message) from exc
        raise Fault("INVALID_PARAMS", message) from exc

    def _drop_snapshot(self, snapshot_id):
        target = Path(self.state) / "snapshots" / snapshot_id
        if target.is_symlink() or not target.is_dir():
            return
        shutil.rmtree(target)

    def dispatch(self, method, p):
        if method == "worker.describe":
            fields(p)
            return {
                "protocol_versions": [0, 1],
                "worker_id": self.worker_id,
                "repository": self.repository,
                "version": __version__,
                "ready": not self.stop.is_set() and self.execution_error is None,
                "readiness_error": self.execution_error,
                "methods": METHODS,
                "capabilities": [
                    "development.fixture",
                    "run.cancel",
                    "run.logs",
                    "workflow.job",
                ],
                "limits": {
                    "message_bytes": MAX_MESSAGE,
                    "log_page_bytes": MAX_LOG_PAGE,
                    "queued_runs": MAX_QUEUE,
                    "list_page": MAX_LIST_PAGE,
                    "cursor_characters": MAX_CURSOR,
                    "request_id_characters": 128,
                    "fixture_output_bytes": MAX_LOG_PAGE,
                    "fixture_delay_ms": 5000,
                    "submission_key_characters": 128,
                    "json_depth": MAX_JSON_DEPTH,
                },
                "retention": "runs and submission keys retained indefinitely; pruning unsupported",
            }
        if method == "run.submit":
            if isinstance(p, dict) and is_integer(p.get("version")) and int(p["version"]) == 1:
                return self.submit_workflow(p)
            fields(p, ("version", "submission_key", "backend", "fixture"))
            self.version(p)
            if p["backend"] != "development":
                raise Fault(
                    "CAPABILITY_UNSUPPORTED", "only explicit development fixtures are supported"
                )
            key = p["submission_key"]
            if not utf8_string(key, 1, 128):
                invalid("submission_key must be a UTF-8 string of 1 to 128 characters")
            fixture = p["fixture"]
            fields(fixture, (), ("exit_code", "delay_ms", "output"))
            fixture = {
                "exit_code": integer(fixture.get("exit_code", 0), 0, 255, "exit_code"),
                "delay_ms": integer(fixture.get("delay_ms", 0), 0, 5000, "delay_ms"),
                "output": fixture.get("output", "development fixture completed\n"),
            }
            if (
                not utf8_string(fixture["output"], 0, MAX_LOG_PAGE)
                or len(fixture["output"].encode()) > MAX_LOG_PAGE
            ):
                invalid("output must be UTF-8 text of at most 65536 bytes")
            normalized = canonical({"backend": "development", "fixture": fixture})
            existing = self.db.execute(
                "SELECT request, record FROM runs WHERE submission_key=?", (key,)
            ).fetchone()
            if existing:
                if existing[0] != normalized:
                    raise Fault(
                        "IDEMPOTENCY_CONFLICT", "submission key already identifies different inputs"
                    )
                return json.loads(existing[1])
            if self.stop.is_set() or self.execution_error is not None:
                raise Fault(
                    "WORKER_NOT_READY",
                    "worker cannot accept new execution; inspect worker.describe",
                )
            queued = self.db.execute(
                "SELECT count(*) FROM runs WHERE json_extract(record, '$.state')='queued'"
            ).fetchone()[0]
            if queued >= MAX_QUEUE:
                raise Fault("QUEUE_FULL", "queued run limit reached")
            digest = hashlib.sha256(normalized.encode()).hexdigest()
            record = {
                "run_id": str(uuid.uuid4()),
                "worker_id": self.worker_id,
                "submission_key": key,
                "state": "queued",
                "exit_code": None,
                "input": {"kind": "development_fixture", "digest": digest, "snapshot_id": digest},
                "backend": {"name": "development", "version": __version__},
                "compatibility_notes": [
                    "Synthetic fixture only; no repository source or workflow execution."
                ],
                "accepted_at": now(),
                "started_at": None,
                "finished_at": None,
                "attempt_id": None,
                "cancel_requested": False,
                "error": None,
                "cleanup": "not_started",
            }
            with self.db:
                self.db.execute(
                    "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                    (record["run_id"], key, normalized, canonical(record), b""),
                )
            return record
        if method == "run.get":
            fields(p, ("run_id",))
            return self.get(p["run_id"])
        if method == "run.cancel":
            fields(p, ("version", "run_id"))
            self.version(p)
            record = self.get(p["run_id"])
            if record["state"] not in TERMINAL:
                record.update(
                    state="cancelled",
                    cancel_requested=True,
                    finished_at=now(),
                    cleanup="confirmed_no_external_resources",
                )
                with self.db:
                    self.save(record)
            return record
        if method == "run.list":
            fields(p, (), ("cursor", "limit", "state"))
            limit = integer(p.get("limit", 20), 1, MAX_LIST_PAGE, "limit")
            state_filter = p.get("state")
            if state_filter is not None and (
                not isinstance(state_filter, str)
                or state_filter not in TERMINAL | {"queued", "running"}
            ):
                invalid("unknown state filter")
            identity = [self.worker_id, state_filter]
            offset = offset_from(p["cursor"], "runs", identity) if "cursor" in p else 0
            rows = self.db.execute(
                "SELECT sequence, record FROM runs WHERE sequence>? AND (? IS NULL OR json_extract(record, '$.state')=?) ORDER BY sequence LIMIT ?",
                (offset, state_filter, state_filter, limit + 1),
            ).fetchall()
            return {
                "runs": [json.loads(r[1]) for r in rows[:limit]],
                "next_cursor": cursor("runs", identity, rows[limit - 1][0])
                if len(rows) > limit
                else None,
            }
        if method == "run.logs":
            fields(p, ("run_id",), ("cursor", "limit"))
            record = self.get(p["run_id"])
            limit = integer(p.get("limit", MAX_LOG_PAGE), 1, MAX_LOG_PAGE, "limit")
            offset = offset_from(p["cursor"], "logs", record["run_id"]) if "cursor" in p else 0
            size = self.db.execute(
                "SELECT length(log) FROM runs WHERE id=?", (record["run_id"],)
            ).fetchone()[0]
            if offset > size:
                raise Fault("CURSOR_EXPIRED", "cursor exceeds available output")
            data = (
                self.db.execute(
                    "SELECT substr(log, ?, ?) FROM runs WHERE id=?",
                    (offset + 1, limit, record["run_id"]),
                ).fetchone()[0]
                or b""
            )
            return {
                "data_base64": base64.b64encode(data).decode(),
                "next_cursor": cursor("logs", record["run_id"], offset + len(data)),
                "end_of_stream": record["state"] in TERMINAL and offset + len(data) == size,
            }
        if method in {"run.artifacts", "artifact.read"}:
            raise Fault("CAPABILITY_UNSUPPORTED", "artifact storage is not implemented")
        raise Fault("METHOD_NOT_FOUND", "unknown method")

    @staticmethod
    def version(p):
        if not is_integer(p["version"]) or p["version"] != 0:
            raise Fault("VERSION_UNSUPPORTED", "only protocol version 0 is supported")

    def response(self, raw):
        request_id = None
        try:
            try:
                request = strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise Fault(
                    "PARSE_ERROR", "invalid UTF-8 JSON, duplicate keys, or excessive nesting"
                ) from None
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not utf8_string(request.get("method"), 1, 128)
                or "id" not in request
                or not valid_id(request["id"])
                or set(request) - {"jsonrpc", "id", "method", "params"}
            ):
                raise Fault(
                    "INVALID_REQUEST", "expected bounded JSON-RPC request with string or integer id"
                )
            request_id = request["id"]
            with self.guard:
                result = self.dispatch(request["method"], request.get("params", {}))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Fault as error:
            return error_response(request_id, error)
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                return error_response(
                    request_id,
                    Fault("STORAGE_FULL", "worker storage is full; free space before retrying"),
                )
            return error_response(
                request_id, Fault("INTERNAL_ERROR", "worker storage operation failed")
            )
        except Exception:
            # Never include database paths, query text, or submitted contents in errors.
            return error_response(
                request_id, Fault("INTERNAL_ERROR", "worker could not process request")
            )

    def serve(self):
        self.start()
        try:
            while not self.stop.is_set():
                try:
                    client, _ = self.server.accept()
                except TimeoutError:
                    continue
                with client:
                    client.settimeout(1)
                    try:
                        with client.makefile("rb") as stream:
                            raw = stream.readline(MAX_MESSAGE + 1)
                        if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
                            reply = error_response(
                                None,
                                Fault(
                                    "INVALID_REQUEST",
                                    "expected newline-delimited request within 1 MiB",
                                ),
                            )
                        else:
                            reply = self.response(raw)
                        client.sendall((canonical(reply) + "\n").encode())
                    except (OSError, ValueError):
                        # A lost reply does not roll back an accepted submission.
                        pass
        finally:
            self.close()


def serve(repository, state):
    worker = Worker(repository, state)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.serve()
