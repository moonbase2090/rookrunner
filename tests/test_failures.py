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
import uuid
from unittest.mock import patch

from execution_core.cli import call
from execution_core.protocol import canonical
from execution_core.run import (
    ContainerLease,
    _container_name,
    owned_container_present,
    release_owned_container,
)
from execution_core.worker import Worker, conflicts_with_unresolved, reuses_unresolved_identity
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

    def test_reuse_is_only_the_same_identity(self):
        owners = {"attempt-1": "rookrunner-0123456789abcdef"}
        self.assertFalse(reuses_unresolved_identity({}))
        self.assertFalse(
            reuses_unresolved_identity(
                owners, attempt_id="other", container_name="rookrunner-other"
            )
        )
        self.assertTrue(reuses_unresolved_identity(owners, attempt_id="attempt-1"))
        self.assertTrue(
            reuses_unresolved_identity(owners, container_name="rookrunner-0123456789abcdef")
        )

    def test_container_name_skips_a_retained_name(self):
        first = b"\x11" * 8
        second = b"\x22" * 8
        owner = ContainerLease()
        owner.blocked_names = frozenset({"rookrunner-" + first.hex()})
        with patch("execution_core.run.os.urandom", side_effect=[first, second]):
            self.assertEqual(_container_name(owner), "rookrunner-" + second.hex())

    def test_begin_records_the_name_before_create(self):
        seen = []
        lease = ContainerLease()
        lease.reserve = seen.append
        self.assertTrue(lease.begin("docker", "rookrunner-0123456789abcdef"))
        self.assertEqual(seen, ["rookrunner-0123456789abcdef"])
        self.assertEqual(lease.snapshot()[0], "creating")

    def test_begin_leaves_the_name_unreserved_when_recording_fails(self):
        lease = ContainerLease()

        def boom(_name):
            raise OSError("disk")

        lease.reserve = boom
        with self.assertRaises(OSError):
            lease.begin("docker", "rookrunner-0123456789abcdef")
        self.assertEqual(lease.snapshot()[:2], ("idle", None))

    def test_release_rejects_a_foreign_container_name(self):
        with patch("execution_core.run._stop_container") as stop:
            self.assertFalse(release_owned_container("other-container"))
            self.assertFalse(release_owned_container("rookrunner-short"))
            self.assertFalse(owned_container_present("other-container"))
            stop.assert_not_called()


class RestartReconciliationTests(unittest.TestCase):
    def test_restart_removes_only_the_recorded_container(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.start()
            try:
                fixture = self._insert(worker, "fixture", "development_fixture")
                bare = self._insert(worker, "bare", "workflow_job")
                owned = self._insert(worker, "owned", "workflow_job")
                name = "rookrunner-0123456789abcdef"
                worker._write_ownership(owned["attempt_id"], name)
                self.assertEqual(
                    (state / "ownership" / owned["attempt_id"]).read_text(), name + "\n"
                )
            finally:
                worker.close()
            with (
                patch(
                    "execution_core.worker.release_owned_container", return_value=True
                ) as release,
                patch("execution_core.worker.owned_container_present", return_value=True),
            ):
                again = Worker(directory, state)
                again.start()
            try:
                release.assert_called_once_with(name)
                for run_id in (fixture["run_id"], bare["run_id"], owned["run_id"]):
                    lost = again.get(run_id)
                    self.assertEqual(lost["state"], "lost")
                    self.assertIsNone(lost["exit_code"])
                    self.assertFalse(lost["cancel_requested"])
                    self.assertEqual(lost["cleanup"], "confirmed_no_external_resources")
                    self.assertEqual(lost["error"]["message"], "worker stopped during execution")
                    self.assertNotIn(name, json.dumps(lost))
                self.assertFalse((state / "ownership" / owned["attempt_id"]).exists())
                self.assertEqual(again.retained, {})
                self.assertFalse(again.unresolved)
            finally:
                again.close()

    def test_restart_keeps_an_unremoved_container_and_blocks_its_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.start()
            try:
                owned = self._insert(worker, "owned", "workflow_job")
                name = "rookrunner-fedcba9876543210"
                worker._write_ownership(owned["attempt_id"], name)
            finally:
                worker.close()
            with patch(
                "execution_core.worker.release_owned_container", return_value=False
            ) as release:
                again = Worker(directory, state)
                again.start()
            try:
                release.assert_called_once_with(name)
                lost = again.get(owned["run_id"])
                self.assertEqual(lost["state"], "lost")
                self.assertEqual(lost["cleanup"], "unresolved")
                self.assertFalse(lost["cancel_requested"])
                self.assertEqual(lost["error"]["kind"], "WORKER_INTERRUPTED")
                self.assertNotIn(name, json.dumps(lost))
                self.assertNotIn("/", lost["error"]["message"])
                self.assertEqual(again.retained[owned["attempt_id"]], name)
                self.assertFalse(again.unresolved)
                self.assertTrue(
                    reuses_unresolved_identity(again.retained, attempt_id=owned["attempt_id"])
                )
                self.assertTrue(reuses_unresolved_identity(again.retained, container_name=name))
                self.assertFalse(
                    reuses_unresolved_identity(
                        again.retained,
                        attempt_id="other",
                        container_name="rookrunner-0000000000000000",
                    )
                )
                reused = self._insert(again, "reused", "workflow_job")
                reused["attempt_id"] = owned["attempt_id"]
                with again.guard, again.db:
                    again.save(reused)
                lease = ContainerLease()
                again.live[reused["run_id"]] = lease
                with patch.object(again, "_run_accepted", side_effect=AssertionError("launched")):
                    again._execute_workflow(reused, {}, lease)
                refused = again.get(reused["run_id"])
                self.assertEqual(refused["state"], "lost")
                self.assertEqual(refused["cleanup"], "unresolved")
                self.assertFalse(refused["cancel_requested"])
                self.assertNotIn(name, json.dumps(refused))
                self.assertEqual(again.get(owned["run_id"])["state"], "lost")
                self.assertEqual(
                    (state / "ownership" / owned["attempt_id"]).read_text(), name + "\n"
                )
            finally:
                again.close()
            with (
                patch("execution_core.worker.release_owned_container") as release,
                patch("execution_core.worker.owned_container_present", return_value=False),
            ):
                third = Worker(directory, state)
                third.start()
            try:
                release.assert_not_called()
                self.assertEqual(third.retained, {})
                self.assertFalse((state / "ownership" / owned["attempt_id"]).exists())
                stored = third.get(owned["run_id"])
                self.assertEqual(stored["state"], "lost")
                self.assertEqual(stored["cleanup"], "unresolved")
            finally:
                third.close()

    def test_invalid_ownership_is_not_a_container_to_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.start()
            try:
                owned = self._insert(worker, "owned", "workflow_job")
                directory_path = state / "ownership"
                directory_path.mkdir(mode=0o700)
                outside = Path(directory) / "outside"
                outside.write_text("rookrunner-0123456789abcdef\n")
                link = directory_path / owned["attempt_id"]
                link.symlink_to(outside)
            finally:
                worker.close()
            with patch("execution_core.worker.release_owned_container") as release:
                again = Worker(directory, state)
                again.start()
            try:
                release.assert_not_called()
                lost = again.get(owned["run_id"])
                self.assertEqual(lost["cleanup"], "unresolved")
                self.assertFalse(lost["cancel_requested"])
                self.assertIsNone(again.retained[owned["attempt_id"]])
                self.assertEqual(outside.read_text(), "rookrunner-0123456789abcdef\n")
                self.assertTrue(link.is_symlink())
            finally:
                again.close()

    def _insert(self, worker, key, kind):
        attempt_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        record = {
            "run_id": run_id,
            "worker_id": worker.worker_id,
            "submission_key": key,
            "state": "running",
            "exit_code": None,
            "input": {"kind": kind},
            "backend": {"name": "workflow", "version": "0.0.1"},
            "compatibility_notes": [],
            "accepted_at": "2026-10-02T00:00:00.000000+00:00",
            "started_at": "2026-10-02T00:00:00.000001+00:00",
            "finished_at": None,
            "attempt_id": attempt_id,
            "cancel_requested": False,
            "error": None,
            "cleanup": "not_started",
        }
        with worker.guard, worker.db:
            worker.db.execute(
                "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                (run_id, key, "{}", canonical(record), b""),
            )
        return record


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
