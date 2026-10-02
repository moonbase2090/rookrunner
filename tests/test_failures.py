import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from execution_core.cli import call
from execution_core.worker import Worker
from schema_support import validate_response


class FailureTests(unittest.TestCase):
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
