# SPDX-License-Identifier: MIT

"""Real container steps pause at a socket gate, without timing-based overlap checks."""

import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from execution_core.worker import Worker


WORKFLOW = """on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          printf '%s:' "$(cat source.txt)"
          if test -f private-marker; then cat private-marker; fi
"""


class DockerGate:
    """Forward the real Docker CLI; a socket controls when job steps may execute."""

    def __init__(self, root, docker):
        self.path = root / "gate.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(16)
        self.lock = threading.Lock()
        self.held = {}
        self.names = set()
        self.started = queue.Queue()
        self.stopped = queue.Queue()
        self.open = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        self.bin = root / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "docker"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, re, socket, sys\n"
            "args = sys.argv[1:]\n"
            "step = args[:2] == ['exec', '--workdir']\n"
            "stop = args[:1] == ['rm'] and '-f' in args\n"
            "if step or stop:\n"
            "    name = next(a for a in args if re.fullmatch(r'rookrunner-[0-9a-f]{16}', a))\n"
            "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:\n"
            f"        sock.connect({str(self.path)!r})\n"
            "        sock.sendall((json.dumps({'kind': 'step' if step else 'stop', 'name': name}) + '\\n').encode())\n"
            "        if step:\n"
            "            if sock.makefile('rb').readline() != b'go\\n':\n"
            "                raise SystemExit(91)\n"
            f"os.execv({docker!r}, [{docker!r}, *args])\n"
        )
        wrapper.chmod(0o755)

    def _accept(self):
        while True:
            connection, _address = self.listener.accept()
            if self.done.is_set():
                connection.close()
                return
            connection.settimeout(10)
            with connection.makefile("rb") as stream:
                row = json.loads(stream.readline(4096))
            name = row["name"]
            if row["kind"] == "stop":
                self.stopped.put(name)
                connection.close()
                continue
            with self.lock:
                self.names.add(name)
                self.held[name] = connection
                if self.open:
                    self._release(name)
            self.started.put(name)

    def _release(self, name):
        connection = self.held.pop(name, None)
        if connection is not None:
            with contextlib.suppress(BrokenPipeError):
                connection.sendall(b"go\n")
            connection.close()

    def release(self, name):
        with self.lock:
            self._release(name)

    def release_all(self):
        with self.lock:
            self.open = True
            for name in list(self.held):
                self._release(name)

    def close(self):
        self.release_all()
        self.done.set()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.connect(str(self.path))
        self.thread.join(timeout=10)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("Docker gate did not stop")


class WorkerParallelismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docker = shutil.which("docker")
        if cls.docker is None:
            raise AssertionError("Docker is required")
        cls.tag = "rookrunner-parallel-" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="parallel-image-") as directory:
            (Path(directory) / "Dockerfile").write_text("FROM python:3.12-slim\n")
            subprocess.run(
                [cls.docker, "build", "--quiet", "-t", cls.tag, directory],
                capture_output=True,
                check=True,
            )
        cls.image = subprocess.check_output(
            [cls.docker, "image", "inspect", "--format", "{{.Id}}", cls.tag], text=True
        ).strip()

    @classmethod
    def tearDownClass(cls):
        subprocess.run([cls.docker, "rmi", cls.tag], capture_output=True, check=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.worker = None
        self.gate = DockerGate(self.root, self.docker)
        self.environment = patch.dict(
            os.environ, {"PATH": str(self.gate.bin) + os.pathsep + os.environ["PATH"]}
        )
        self.environment.start()
        self.addCleanup(self._cleanup)
        path = self.repo / ".github/workflows/test.yml"
        path.parent.mkdir(parents=True)
        path.write_text(WORKFLOW)
        (self.repo / "source.txt").write_text("initial")
        self._git("init", "--initial-branch=main")
        self._git("add", ".")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def _cleanup(self):
        self.gate.release_all()
        try:
            if self.worker is not None:
                self.worker.close()
        finally:
            self.gate.close()
            self.environment.stop()
            for name in self.gate.names:
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True)
            self.temp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True)

    def _start(self, cap=2):
        options = {} if cap is None else {"cap": cap}
        self.worker = Worker(self.repo, self.state, **options)
        self.worker.start()
        self.changed = threading.Event()
        self.worker.db.set_trace_callback(
            lambda sql: self.changed.set() if sql == "COMMIT" else None
        )

    def _rpc(self, method, params):
        reply = self.worker.response(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        )
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def _submit(self, key, group="separate"):
        (self.repo / "source.txt").write_text(key)
        return self._rpc(
            "run.submit",
            {
                "version": 1,
                "submission_key": key,
                "workflow": ".github/workflows/test.yml",
                "job_id": "build",
                "event": {"group": group},
                "image": self.image,
            },
        )["run_id"]

    def _get(self, run_id):
        return self._rpc("run.get", {"run_id": run_id})

    def _evidence(self, name, value):
        folder = os.environ.get("ROOKRUNNER_PARALLEL_PROOF")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{self._testMethodName}-{name}.json").write_text(
                json.dumps(value, indent=2) + "\n"
            )

    def _name(self, run_id):
        record = self._get(run_id)
        return (self.state / "ownership" / record["attempt_id"]).read_text().strip()

    def _terminal(self, run_id):
        while True:
            self.changed.clear()
            record = self._get(run_id)
            if record["state"] in {"succeeded", "failed", "lost", "cancelled"}:
                return record
            self.assertTrue(self.changed.wait(30), f"no terminal commit for {run_id}")

    def _succeeded(self, run_id, output):
        record = self._terminal(run_id)
        self.assertEqual(record["state"], "succeeded", record)
        self.assertEqual(record["exit_code"], 0)
        logs = self._rpc("run.logs", {"run_id": run_id})
        actual = base64.b64decode(logs["data_base64"]).decode()
        self.assertEqual(actual, output)
        self._evidence(
            run_id,
            {
                "run_id": run_id,
                "state": record["state"],
                "exit_code": record["exit_code"],
                "source_digest": record["input"]["digest"],
                "image_digest": record["input"]["image_digest"],
                "output": actual,
            },
        )

    def test_two_slots_overlap_isolate_snapshots_and_leave_the_third_queued(self):
        self._start()
        first = self._submit("first")
        second = self._submit("second")
        names = {self.gate.started.get(timeout=10), self.gate.started.get(timeout=10)}
        self.assertEqual(names, {self._name(first), self._name(second)})
        first_record, second_record = self._get(first), self._get(second)
        self.assertEqual([first_record["state"], second_record["state"]], ["running", "running"])
        self.assertNotEqual(first_record["attempt_id"], second_record["attempt_id"])
        first_dir = self.state / "attempts" / first_record["attempt_id"] / "workspace"
        second_dir = self.state / "attempts" / second_record["attempt_id"] / "workspace"
        (first_dir / "private-marker").write_text("private")
        self.assertFalse((second_dir / "private-marker").exists())
        inspected = []
        for name in names:
            running = subprocess.check_output(
                [self.docker, "inspect", "--format", "{{.State.Running}}", name], text=True
            ).strip()
            self.assertEqual(running, "true")
            inspected.append(running)
        third = self._submit("third")
        third_record = self._get(third)
        self.assertEqual(third_record["state"], "queued")
        self._evidence(
            "overlap",
            {
                "worker_cap": self.worker.cap,
                "running_containers": sum(value == "true" for value in inspected),
                "active_runs": [
                    {
                        "run_id": r["run_id"],
                        "state": r["state"],
                        "attempt_id": r["attempt_id"],
                        "source_digest": r["input"]["digest"],
                        "image_digest": r["input"]["image_digest"],
                    }
                    for r in [first_record, second_record]
                ],
                "waiting_run": {"run_id": third, "state": third_record["state"]},
            },
        )
        self.gate.release(self._name(first))
        third_name = self.gate.started.get(timeout=10)
        self.assertEqual(third_name, self._name(third))
        self.assertEqual(self._get(second)["state"], "running")
        self._succeeded(first, "first:private")
        self.gate.release_all()
        self._succeeded(second, "second:")
        self._succeeded(third, "third:")

    def test_default_capacity_keeps_one_run_active(self):
        self._start(cap=None)
        first = self._submit("first")
        self.assertEqual(self.gate.started.get(timeout=10), self._name(first))
        second = self._submit("second")
        self.assertEqual(self._get(second)["state"], "queued")
        self.gate.release(self._name(first))
        self.assertEqual(self.gate.started.get(timeout=10), self._name(second))
        self._succeeded(first, "first:")
        self.gate.release_all()
        self._succeeded(second, "second:")

    def test_slots_share_one_storage_budget_during_preparation(self):
        (self.repo / "payload.bin").write_bytes(b"z" * (1024 * 1024))
        self._git("add", "payload.bin")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "capture a larger input",
        )
        self._start()
        with self.worker.guard:
            first, second = self._submit("first"), self._submit("second")
            self.worker.disk_budget = self.worker.state_usage.refresh() + 1536 * 1024
        while True:
            self.changed.clear()
            records = [self._get(first), self._get(second)]
            failed = [record for record in records if record["state"] == "failed"]
            if failed:
                break
            self.assertTrue(self.changed.wait(30), "neither preparation recorded a storage refusal")
        self.assertEqual(failed[0]["error"]["kind"], "SETUP_FAILED")
        self.assertIn("storage", failed[0]["error"]["message"])
        self.gate.release_all()
        results = [self._terminal(first), self._terminal(second)]
        self.assertEqual(sorted(record["state"] for record in results), ["failed", "succeeded"])
        for record in results:
            if record["state"] == "succeeded":
                self.assertEqual(record["exit_code"], 0)
                expected = "first:" if record["run_id"] == first else "second:"
                logs = self._rpc("run.logs", {"run_id": record["run_id"]})
                self.assertEqual(base64.b64decode(logs["data_base64"]).decode(), expected)

    def test_a_group_waits_while_another_group_uses_the_spare_slot(self):
        path = self.repo / ".github/workflows/test.yml"
        path.write_text(
            "concurrency:\n  group: ${{ github.event.group }}\n  queue: max\n" + WORKFLOW
        )
        self._start()
        with self.worker.guard:
            first = self._submit("first", "shared")
            waiting = self._submit("waiting", "shared")
            other = self._submit("other", "different")
        names = {self.gate.started.get(timeout=10), self.gate.started.get(timeout=10)}
        self.assertEqual(names, {self._name(first), self._name(other)})
        self.assertEqual(self._get(waiting)["state"], "queued")
        self.gate.release(self._name(first))
        self.assertEqual(self.gate.started.get(timeout=10), self._name(waiting))
        self.assertEqual(self._get(other)["state"], "running")
        self.gate.release_all()
        for run_id, text in [(first, "first:"), (waiting, "waiting:"), (other, "other:")]:
            self._succeeded(run_id, text)

    def test_cancelling_one_run_keeps_the_other_slot_running(self):
        self._start()
        first, second = self._submit("first"), self._submit("second")
        self.gate.started.get(timeout=10)
        self.gate.started.get(timeout=10)
        first_name = self._name(first)
        results = queue.Queue()

        def cancel():
            results.put(self._rpc("run.cancel", {"version": 0, "run_id": first}))

        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            self.assertEqual(self.gate.stopped.get(timeout=10), first_name)
            self.gate.release(first_name)
            result = results.get(timeout=30)
            self.assertEqual(result["state"], "cancelled", result)
            self.assertEqual(self._get(second)["state"], "running")
            self.gate.release_all()
            self._succeeded(second, "second:")
            self.assertEqual(self._get(first)["state"], "cancelled")
        finally:
            self.gate.release_all()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())

    def test_shutdown_joins_both_slots_before_closing_the_database(self):
        self._start()
        first, second = self._submit("first"), self._submit("second")
        self.gate.started.get(timeout=10)
        self.gate.started.get(timeout=10)
        worker = self.worker
        thread = threading.Thread(target=worker.close)
        thread.start()
        self.assertTrue(worker.stop.wait(10))
        try:
            self.assertIsNotNone(worker.db)
            self.gate.release_all()
        finally:
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.worker = None
        with sqlite3.connect(self.state / "runs.sqlite3") as database:
            records = {
                json.loads(row[0])["run_id"]: json.loads(row[0])
                for row in database.execute("SELECT record FROM runs")
            }
        for run_id in (first, second):
            self.assertEqual(records[run_id]["state"], "succeeded")
            self.assertEqual(records[run_id]["exit_code"], 0)


class WorkerCapacityValidationTests(unittest.TestCase):
    def test_a_replaced_ownership_directory_is_not_chmodded_or_written(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            outside = Path(directory) / "outside"
            outside.mkdir(mode=0o755)
            outside.chmod(0o755)
            worker = Worker(directory, state)
            mkdir = Path.mkdir

            def replace_directory(path, *args, **kwargs):
                if path == state / "ownership":
                    path.symlink_to(outside, target_is_directory=True)
                return mkdir(path, *args, **kwargs)

            with (
                patch.object(Path, "mkdir", replace_directory),
                self.assertRaisesRegex(OSError, "attempt identity"),
            ):
                worker._write_ownership(str(uuid.uuid4()), "rookrunner-" + "a" * 16)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(outside.iterdir()), [])

    def test_a_failed_claim_stops_other_slots_without_closing_inspection(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        worker = Worker(directory, Path(directory) / "state", cap=2)
        worker.start()
        self.addCleanup(worker.close)
        denied = False

        def refuse_one_update(action, table, _column, _database, _trigger):
            nonlocal denied
            if action == sqlite3.SQLITE_UPDATE and table == "runs" and not denied:
                denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        with worker.guard:
            run_ids = []
            for key in ("first", "second"):
                reply = worker.response(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "run.submit",
                            "params": {
                                "version": 0,
                                "submission_key": key,
                                "backend": "development",
                                "fixture": {"output": key},
                            },
                        }
                    ).encode()
                )
                self.assertNotIn("error", reply, reply)
                run_ids.append(reply["result"]["run_id"])
            worker.db.set_authorizer(refuse_one_update)
        worker.scheduler.join(timeout=5)
        self.assertFalse(worker.scheduler.is_alive())
        worker.db.set_authorizer(None)
        reply = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
        self.assertIs(reply["result"]["ready"], False)
        self.assertIn("scheduler stopped", reply["result"]["readiness_error"])
        with worker.guard:
            self.assertEqual(
                [worker.get(run_id)["state"] for run_id in run_ids], ["queued", "queued"]
            )
        refused = worker.response(
            b'{"jsonrpc":"2.0","id":1,"method":"run.submit","params":{"version":0,"submission_key":"later","backend":"development","fixture":{}}}'
        )
        self.assertEqual(refused["error"]["data"]["kind"], "WORKER_NOT_READY")

    def test_a_slot_start_failure_drains_started_slots_and_keeps_inspection(self):
        start = threading.Thread.start

        def fail_second_slot(thread):
            if thread.name == "worker-slot-2":
                raise RuntimeError("thread creation refused")
            return start(thread)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        worker = Worker(directory, Path(directory) / "state", cap=2)
        with patch.object(threading.Thread, "start", fail_second_slot):
            worker.start()
        self.addCleanup(worker.close)
        worker.scheduler.join(timeout=5)
        self.assertFalse(worker.scheduler.is_alive())
        reply = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
        self.assertIs(reply["result"]["ready"], False)
        self.assertIn("scheduler stopped", reply["result"]["readiness_error"])
        self.assertTrue((worker.state / "worker.sock").exists())

    def test_invalid_worker_capacities_are_refused_before_state_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            for cap in [None, True, False, 0, -1, 1.5, "2"]:
                with self.subTest(cap=cap):
                    with self.assertRaisesRegex(ValueError, "cap.*positive integer"):
                        Worker(directory, state, cap=cap)
                    self.assertFalse(state.exists())

    def test_the_cli_refuses_zero_capacity_before_starting_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
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
                    "--cap",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("cap must be a positive integer", result.stderr)
            self.assertFalse(state.exists())

    def test_two_first_attempts_can_publish_their_ownership_records(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            worker = Worker(directory, state)
            entries = [(str(uuid.uuid4()), "rookrunner-" + char * 16) for char in "ab"]
            barrier = threading.Barrier(2)
            mkdir = Path.mkdir

            def together(path, *args, **kwargs):
                if path == state / "ownership":
                    barrier.wait(timeout=5)
                return mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", together), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(worker._write_ownership, attempt, name) for attempt, name in entries
                ]
                for future in futures:
                    future.result(timeout=5)
            for attempt, name in entries:
                path = state / "ownership" / attempt
                self.assertEqual(path.read_text(), name + "\n")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
