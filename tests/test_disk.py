import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from execution_core.cli import call
from execution_core.disk import DEFAULT_DISK_BUDGET, usage
from execution_core.protocol import canonical
from execution_core.run import ContainerLease
from execution_core.worker import Worker, now
from schema_support import validate_response, validator


IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
_STORAGE_FULL = "worker storage is full; free space before retrying"


def _submit(worker, key, output="ok\n"):
    params = {
        "version": 0,
        "submission_key": key,
        "backend": "development",
        "fixture": {"output": output},
    }
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params})
    reply = worker.response(request.encode())
    validate_response("run.submit", reply)
    return reply


class DiskUsageTests(unittest.TestCase):
    def test_default_is_ten_gibibytes_of_documented_cache_storage(self):
        self.assertEqual(DEFAULT_DISK_BUDGET, 10 * 1024 * 1024 * 1024)

    def test_usage_counts_entries_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            data = root / "data"
            data.write_bytes(b"abc")
            self.assertEqual(usage(root), root.lstat().st_size + data.lstat().st_size)

            linked = root / "linked"
            os.link(data, linked)
            self.assertEqual(
                usage(root),
                root.lstat().st_size + data.lstat().st_size + linked.lstat().st_size,
            )

            outside = Path(directory) / "outside"
            outside.write_bytes(b"x" * 50000)
            link = root / "escape"
            link.symlink_to(outside)
            outside_dir = Path(directory) / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "big").write_bytes(b"y" * 50000)
            dir_link = root / "dir-escape"
            dir_link.symlink_to(outside_dir)
            measured = usage(root)
            self.assertLess(measured, 50000)
            self.assertGreaterEqual(measured, link.lstat().st_size + dir_link.lstat().st_size)

    def test_budget_rejects_a_bool_and_a_negative_count(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            for value in (True, False, -1):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError) as caught:
                        Worker(directory, state, disk_budget=value)
                    self.assertEqual(
                        str(caught.exception), "disk budget must be a non-negative integer"
                    )
            worker = Worker(directory, state)
            self.assertEqual(worker.disk_budget, DEFAULT_DISK_BUDGET)
            worker = Worker(directory, state, disk_budget=0)
            self.assertEqual(worker.disk_budget, 0)


class DiskBudgetTests(unittest.TestCase):
    def test_fixture_over_budget_does_not_consume_the_key_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state, disk_budget=0)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                described = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
                validate_response("worker.describe", described)
                self.assertTrue(described["result"]["ready"])
                self.assertNotIn("disk_budget", described["result"]["limits"])
                self.assertEqual(
                    described["result"]["retention"],
                    "runs and submission keys retained indefinitely; pruning unsupported",
                )
                evidence = state / "snapshots" / "kept"
                evidence.parent.mkdir(mode=0o700)
                os.chmod(evidence.parent, 0o700)
                evidence.write_bytes(b"keep")
                attempt = state / "attempts" / "kept"
                attempt.parent.mkdir(mode=0o700)
                os.chmod(attempt.parent, 0o700)
                attempt.write_bytes(b"keep-attempt")
                refused = _submit(worker, "fresh")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(refused["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(refused))
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("fresh",)
                    ).fetchone()
                )
                self.assertEqual(evidence.read_bytes(), b"keep")
                self.assertEqual(attempt.read_bytes(), b"keep-attempt")
                worker.disk_budget = DEFAULT_DISK_BUDGET
                accepted = _submit(worker, "fresh")
                self.assertEqual(accepted["result"]["state"], "queued")
                worker.disk_budget = 0
                again = _submit(worker, "fresh")
                self.assertEqual(again["result"]["run_id"], accepted["result"]["run_id"])
                other = _submit(worker, "other")
                self.assertEqual(other["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
                self.assertEqual(evidence.read_bytes(), b"keep")
                self.assertEqual(attempt.read_bytes(), b"keep-attempt")
            finally:
                worker.close()

    def test_usage_error_refuses_the_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                with patch("execution_core.worker.usage", side_effect=OSError("stat failed")):
                    refused = _submit(worker, "stat")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertNotIn("stat failed", json.dumps(refused))
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            finally:
                worker.close()

    def test_equal_usage_is_allowed_and_one_more_byte_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                with worker.guard:
                    worker.disk_budget = usage(worker.state) + 10
                    self.assertFalse(worker._over_budget(10))
                    self.assertTrue(worker._over_budget(11))
            finally:
                worker.close()

    def test_workflow_over_budget_drops_only_the_uncommitted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                kept = state / "snapshots" / "kept"
                kept.parent.mkdir(mode=0o700)
                os.chmod(kept.parent, 0o700)
                kept.write_bytes(b"keep")
                accepted = self._workflow(worker, "wf-a")
                self.assertEqual(accepted["result"]["state"], "queued")
                snapshot_id = accepted["result"]["input"]["snapshot_id"]
                self.assertTrue((state / "snapshots" / snapshot_id).is_dir())
                worker.disk_budget = 0
                same = self._workflow(worker, "wf-a")
                self.assertEqual(same["result"]["run_id"], accepted["result"]["run_id"])
                refused = self._workflow(worker, "wf-b")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(refused["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(refused))
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("wf-b",)
                    ).fetchone()
                )
                names = sorted(path.name for path in (state / "snapshots").iterdir())
                self.assertEqual(names, sorted(["kept", snapshot_id]))
                self.assertEqual(kept.read_bytes(), b"keep")
                worker.disk_budget = DEFAULT_DISK_BUDGET
                retried = self._workflow(worker, "wf-b")
                self.assertEqual(retried["result"]["state"], "queued")
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
                self.assertTrue(
                    (state / "snapshots" / accepted["result"]["input"]["snapshot_id"]).is_dir()
                )
                self.assertEqual(kept.read_bytes(), b"keep")
            finally:
                worker.close()

    def test_materialize_stops_before_creating_a_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-run")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                evidence = state / "attempts" / "kept"
                evidence.parent.mkdir(mode=0o700, exist_ok=True)
                os.chmod(evidence.parent, 0o700)
                evidence.write_bytes(b"still-here")
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                manifest = (snapshot / "manifest.json").read_bytes()
                worker.disk_budget = 0
                with (
                    patch(
                        "execution_core.worker.materialize_attempt",
                        side_effect=AssertionError("materialized"),
                    ) as materialize,
                    patch(
                        "execution_core.worker.run_job",
                        side_effect=AssertionError("ran"),
                    ),
                ):
                    worker._execute_workflow(record, request, ContainerLease())
                materialize.assert_not_called()
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "failed")
                self.assertIsNone(finished["exit_code"])
                self.assertEqual(finished["error"]["kind"], "SETUP_FAILED")
                self.assertEqual(finished["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(finished))
                self.assertFalse((state / "attempts" / record["attempt_id"]).exists())
                self.assertEqual(evidence.read_bytes(), b"still-here")
                self.assertEqual((snapshot / "manifest.json").read_bytes(), manifest)
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
                worker.disk_budget = 0
                again = self._workflow(worker, "wf-run")
                self.assertEqual(again["result"]["run_id"], record["run_id"])
            finally:
                worker.close()

    def test_cli_flag_configures_the_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            refused = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    str(repo),
                    "--disk-budget-bytes",
                    "-1",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(refused.returncode, 1)
            payload = json.loads(refused.stderr)
            self.assertEqual(payload["error"]["kind"], "CLIENT_ERROR")
            self.assertEqual(
                payload["error"]["message"], "disk budget must be a non-negative integer"
            )
            self.assertNotIn(str(state), refused.stderr)
            self.assertNotIn(str(repo), refused.stderr)
            self.assertFalse(state.exists())

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    str(repo),
                    "--disk-budget-bytes",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode())
                    try:
                        call(state, "worker.describe", {})
                        break
                    except (OSError, ValueError):
                        time.sleep(0.01)
                else:
                    self.fail("worker did not become ready")
                params = {
                    "version": 0,
                    "submission_key": "cli",
                    "backend": "development",
                    "fixture": {},
                }
                validator("Request").validate(
                    {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
                )
                reply = call(state, "run.submit", params)
                validate_response("run.submit", reply)
                self.assertEqual(reply["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertNotIn(str(state), json.dumps(reply))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def _git_workflow(self, repo):
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        subprocess.run(
            ["git", "-C", repo, "init", "--initial-branch=main"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )

    def _workflow(self, worker, key):
        params = {
            "version": 1,
            "submission_key": key,
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": EVENT,
            "image": IMAGE,
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
        validator("Request").validate(body)
        reply = worker.response(canonical(body).encode())
        validate_response("run.submit", reply)
        return reply
