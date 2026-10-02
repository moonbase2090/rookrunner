import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from execution_core.cli import call
from execution_core.protocol import canonical
from execution_core.worker import Worker, conflicts_with_unresolved
from schema_support import validate_response


class UnresolvedAttemptTests(unittest.TestCase):
    def test_any_unresolved_container_conflicts(self):
        self.assertFalse(conflicts_with_unresolved({}))
        pending = {"attempt-1": "rookrunner-abc"}
        self.assertTrue(conflicts_with_unresolved(pending))
        self.assertTrue(conflicts_with_unresolved(pending, attempt_id="attempt-1"))
        self.assertTrue(conflicts_with_unresolved(pending, container_name="rookrunner-abc"))
        self.assertTrue(
            conflicts_with_unresolved(pending, attempt_id="other", container_name="other")
        )
        self.assertTrue(conflicts_with_unresolved({"attempt-1": None}, container_name=None))


class FailureTests(unittest.TestCase):
    def test_cancel_commit_keeps_a_completion_that_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.start()
            try:
                self._assert_commit_keeps_completion(worker, worker._commit_cancelled)
                self._assert_commit_keeps_completion(worker, worker._commit_lost)
                plain = self._insert_running(worker, "plain")
                cancelled = worker._commit_cancelled(plain)
                self.assertEqual(cancelled["state"], "cancelled")
                self.assertTrue(cancelled["cancel_requested"])
                self.assertEqual(cancelled["cleanup"], "confirmed_no_external_resources")
                self.assertFalse(worker.unresolved)
                lost_run = self._insert_running(worker, "lost-run")
                lost = worker._commit_lost(lost_run, "rookrunner-left")
                self.assertEqual(lost["state"], "lost")
                self.assertEqual(lost["cleanup"], "unresolved")
                self.assertEqual(lost["error"]["kind"], "WORKER_INTERRUPTED")
                self.assertEqual(worker.unresolved.get(lost["attempt_id"]), "rookrunner-left")
            finally:
                worker.close()

    def _insert_running(self, worker, key):
        run_id = f"run-{key}"
        record = {
            "run_id": run_id,
            "worker_id": worker.worker_id,
            "submission_key": key,
            "state": "running",
            "exit_code": None,
            "input": {"kind": "workflow_job"},
            "backend": {"name": "workflow", "version": "0.0.1"},
            "compatibility_notes": [],
            "accepted_at": "2026-10-02T00:00:00.000000+00:00",
            "started_at": "2026-10-02T00:00:00.000001+00:00",
            "finished_at": None,
            "attempt_id": f"attempt-{key}",
            "cancel_requested": False,
            "error": None,
            "cleanup": "not_started",
        }
        with worker.db:
            worker.db.execute(
                "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                (run_id, key, "{}", canonical(record), b""),
            )
        return record

    def _assert_commit_keeps_completion(self, worker, commit):
        record = self._insert_running(worker, "done-" + commit.__name__)
        ready = threading.Event()
        started = threading.Event()
        write_now = threading.Event()
        result = {}

        def completion():
            with worker.guard:
                ready.set()
                self.assertTrue(write_now.wait(5))
                current = worker.get(record["run_id"])
                current.update(
                    state="succeeded",
                    exit_code=0,
                    error=None,
                    finished_at="2026-10-02T00:00:01.000000+00:00",
                    cleanup="confirmed_no_external_resources",
                    steps=[
                        {
                            "index": 0,
                            "id": "show",
                            "name": None,
                            "status": "succeeded",
                            "exit_code": 0,
                            "stdout": "ok\n",
                            "stderr": "",
                            "error": None,
                        }
                    ],
                )
                with worker.db:
                    worker.db.execute(
                        "UPDATE runs SET log=? WHERE id=?", (b"ok\n", record["run_id"])
                    )
                    worker.save(current)

        def cancel():
            started.set()
            if commit.__func__ is Worker._commit_lost:
                result["record"] = commit(record, None)
            else:
                result["record"] = commit(record)

        holder = threading.Thread(target=completion)
        holder.start()
        self.assertTrue(ready.wait(5))
        canceller = threading.Thread(target=cancel)
        canceller.start()
        self.assertTrue(started.wait(5))
        time.sleep(0.05)
        write_now.set()
        self.assertTrue(holder.join(5) is None and not holder.is_alive())
        self.assertTrue(canceller.join(5) is None and not canceller.is_alive())
        kept = result["record"]
        self.assertEqual(kept["state"], "succeeded")
        self.assertEqual(kept["exit_code"], 0)
        self.assertEqual(kept["steps"][0]["stdout"], "ok\n")
        self.assertNotIn(record["attempt_id"], worker.unresolved)
        stored = worker.get(record["run_id"])
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["steps"][0]["id"], "show")

    def test_corrupt_database_startup_returns_json_without_replacing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            database = state / "runs.sqlite3"
            database.write_bytes(b"not a SQLite database")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    directory,
                ],
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["error"]["kind"], "CLIENT_ERROR")
            self.assertEqual(database.read_bytes(), b"not a SQLite database")
            self.assertFalse((state / "worker.sock").exists())

    def test_scheduler_failure_is_not_ready_and_cannot_accept_work(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            with patch.object(worker, "execute_queue", side_effect=RuntimeError("injected")):
                worker.start()
                try:
                    worker.scheduler.join(timeout=5)
                    reply = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
                    validate_response("worker.describe", reply)
                    self.assertFalse(reply["result"]["ready"])
                    self.assertIsNotNone(reply["result"]["readiness_error"])
                    result = worker.response(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "run.submit",
                                "params": {
                                    "version": 0,
                                    "submission_key": "test",
                                    "backend": "development",
                                    "fixture": {},
                                },
                            }
                        )
                    )
                    validate_response("run.submit", result)
                    self.assertEqual(result["error"]["data"]["kind"], "WORKER_NOT_READY")
                    self.assertEqual(
                        worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0
                    )
                finally:
                    worker.close()

    def test_sqlite_full_does_not_accept_or_consume_submission_key(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.start()
            try:
                with worker.guard:
                    pages = worker.db.execute("PRAGMA page_count").fetchone()[0]
                    worker.db.execute(f"PRAGMA max_page_count={pages}")
                    params = {
                        "version": 0,
                        "submission_key": "retry",
                        "backend": "development",
                        "fixture": {"output": "x" * 65536},
                    }
                    request = json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
                    )
                    reply = worker.response(request)
                    validate_response("run.submit", reply)
                    self.assertEqual(reply["error"]["data"]["kind"], "STORAGE_FULL")
                    self.assertEqual(
                        worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0
                    )
                    worker.db.execute("PRAGMA max_page_count=10000")
                    retried = worker.response(request)
                    validate_response("run.submit", retried)
                    self.assertEqual(retried["result"]["state"], "queued")
                    self.assertEqual(
                        worker.response(request)["result"]["run_id"], retried["result"]["run_id"]
                    )
            finally:
                worker.close()

    def test_storage_error_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            with patch.object(
                worker, "dispatch", side_effect=sqlite3.OperationalError("private database path")
            ):
                reply = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
            validate_response("worker.describe", reply)
            self.assertEqual(reply["error"]["data"]["kind"], "INTERNAL_ERROR")
            self.assertNotIn("private", json.dumps(reply))

    def test_client_rejects_malformed_response_envelopes(self):
        replies = [
            b"[]\n",
            b'{"jsonrpc":"2.0","id":1}\n',
            b'{"jsonrpc":"2.0","id":1,"result":{},"error":{}}\n',
            b'{"jsonrpc":"2.0","id":true,"result":{}}\n',
            b'{"jsonrpc":"2.0","id":1,"result":null}\n',
            b'{"jsonrpc":"2.0","id":1,"result":{},"id":2}\n',
        ]
        for reply in replies:
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as directory:
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(Path(directory) / "worker.sock"))
                    server.listen(1)
                    server.settimeout(5)

                    def respond():
                        client, _ = server.accept()
                        with client:
                            client.recv(65536)
                            client.sendall(reply)

                    thread = threading.Thread(target=respond)
                    thread.start()
                    try:
                        with self.assertRaises(ValueError):
                            call(directory, "worker.describe", {})
                    finally:
                        thread.join(timeout=5)
