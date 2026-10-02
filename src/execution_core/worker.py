"""Single-user Unix worker with durable deterministic development runs.

The development backend interprets bounded fixture data. It never launches
commands, reads a workflow, or claims to have captured repository source.
"""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import threading
import uuid
from datetime import datetime, timezone

from . import __version__
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
                row = self.db.execute(
                    "SELECT record, request FROM runs WHERE json_extract(record, '$.state')='queued' ORDER BY sequence LIMIT 1"
                ).fetchone()
                if row:
                    record, request = json.loads(row[0]), json.loads(row[1])
                    record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                    with self.db:
                        self.save(record)
            if not row:
                self.stop.wait(0.02)
                continue
            interrupted = self.stop.wait(request["fixture"]["delay_ms"] / 1000)
            with self.guard, self.db:
                record = self.get(record["run_id"])
                if record["state"] in TERMINAL:
                    continue
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

    def dispatch(self, method, p):
        if method == "worker.describe":
            fields(p)
            return {
                "protocol_versions": [0],
                "worker_id": self.worker_id,
                "repository": self.repository,
                "version": __version__,
                "ready": not self.stop.is_set() and self.execution_error is None,
                "readiness_error": self.execution_error,
                "methods": METHODS,
                "capabilities": ["development.fixture", "run.cancel", "run.logs"],
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
