# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from execution_core.artifacts import written_files
from execution_core.cli import call
from execution_core.protocol import canonical
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


def _request(method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    validator("Request").validate(body)
    return canonical(body).encode()


class WrittenFileTests(unittest.TestCase):
    def test_only_changed_regular_files_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_files = root / "snapshot" / "files"
            workspace = root / "workspace"
            (snapshot_files / "keep").parent.mkdir(parents=True)
            (snapshot_files / "keep").write_bytes(b"same")
            (snapshot_files / "source.txt").write_bytes(b"original")
            shutil.copytree(snapshot_files, workspace, symlinks=True)
            (workspace / "source.txt").write_bytes(b"changed")
            (workspace / "out").mkdir()
            (workspace / "out" / "demo.txt").write_bytes(b"demo")
            outside = root / "outside"
            outside.write_bytes(b"secret-bytes")
            (workspace / "leak").symlink_to(outside)
            git = workspace / ".git"
            git.mkdir()
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            (git / "config").write_text("secret-token\n")
            secret_dir = root / "secret-dir"
            secret_dir.mkdir()
            (secret_dir / "token").write_text("secret-token")
            (workspace / "nested").mkdir()
            (workspace / "nested" / ".git").symlink_to(secret_dir, target_is_directory=True)
            listed = written_files(workspace, root / "snapshot")
            self.assertEqual([item["path"] for item in listed], ["out/demo.txt", "source.txt"])
            self.assertNotIn("secret-token", json.dumps(listed))
            demo = listed[0]
            self.assertEqual(demo["size"], 4)
            self.assertEqual(demo["digest"], hashlib.sha256(b"demo").hexdigest())
            self.assertNotIn("secret-bytes", json.dumps(listed))


class ArtifactManifestTests(unittest.TestCase):
    def test_fixture_is_unsupported_and_a_quiet_workflow_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                fixture = self._submit_fixture(worker, "fix")
                refused = worker.response(
                    _request("run.artifacts", {"run_id": fixture["result"]["run_id"]})
                )
                validate_response("run.artifacts", refused)
                self.assertEqual(refused["error"]["data"]["kind"], "CAPABILITY_UNSUPPORTED")
                queued = self._submit_workflow(worker, "wf")
                empty = worker.response(
                    _request("run.artifacts", {"run_id": queued["result"]["run_id"]})
                )
                validate_response("run.artifacts", empty)
                self.assertEqual(empty["result"], {"artifacts": [], "next_cursor": None})
                missing = worker.response(
                    _request(
                        "artifact.read",
                        {"artifact_id": "00000000-0000-4000-8000-000000000099"},
                    )
                )
                validate_response("artifact.read", missing)
                self.assertEqual(missing["error"]["data"]["kind"], "INVALID_PARAMS")
            finally:
                worker.close()

    def test_publish_lists_only_written_files_and_pages_their_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._submit_workflow(worker, "wf")
                record = accepted["result"]
                attempt_id = str(uuid.uuid4())
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                attempt = state / "attempts" / attempt_id
                attempt.mkdir(parents=True)
                workspace = attempt / "workspace"
                shutil.copytree(snapshot / "files", workspace, symlinks=True)
                (workspace / "source.txt").write_bytes(b"changed")
                home = attempt / "home"
                home.mkdir(mode=0o700)
                (home / "secret.txt").write_bytes(b"not-an-artifact")
                payload = b"x" * 50
                (workspace / "out").mkdir()
                (workspace / "out" / "demo.txt").write_bytes(payload)
                outside = root / "outside"
                outside.write_bytes(b"secret-bytes")
                (workspace / "leak").symlink_to(outside)
                record.update(state="running", started_at=now(), attempt_id=attempt_id)
                with worker.db:
                    worker.save(record)
                    worker._save_workflow_outcome(
                        record["run_id"],
                        record,
                        {
                            "status": "succeeded",
                            "exit_code": 0,
                            "image_digest": record["input"]["image_digest"],
                            "steps": [],
                        },
                    )
                first = worker.response(
                    _request("run.artifacts", {"run_id": record["run_id"], "limit": 1})
                )
                validate_response("run.artifacts", first)
                self.assertEqual(
                    [item["path"] for item in first["result"]["artifacts"]], ["out/demo.txt"]
                )
                self.assertIsNotNone(first["result"]["next_cursor"])
                second = worker.response(
                    _request(
                        "run.artifacts",
                        {
                            "run_id": record["run_id"],
                            "cursor": first["result"]["next_cursor"],
                            "limit": 1,
                        },
                    )
                )
                validate_response("run.artifacts", second)
                self.assertEqual(
                    [item["path"] for item in second["result"]["artifacts"]], ["source.txt"]
                )
                self.assertIsNone(second["result"]["next_cursor"])
                listed = first["result"]["artifacts"] + second["result"]["artifacts"]
                self.assertEqual(listed[0]["size"], 50)
                self.assertEqual(listed[0]["digest"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(listed[1]["digest"], hashlib.sha256(b"changed").hexdigest())
                self.assertNotIn("keep.txt", json.dumps(listed))
                self.assertNotIn("leak", json.dumps(listed))
                self.assertNotIn("secret-bytes", json.dumps(listed))
                self.assertNotIn("secret.txt", json.dumps(listed))
                self.assertNotIn("not-an-artifact", json.dumps(listed))
                self.assertFalse(attempt.exists())
                gone = worker.response(_request("artifact.read", {"artifact_id": listed[0]["id"]}))
                validate_response("artifact.read", gone)
                self.assertEqual(gone["error"]["data"]["kind"], "INTERNAL_ERROR")
                self.assertEqual(gone["error"]["message"], "artifact bytes are not available")
                self.assertNotIn(str(attempt), json.dumps(gone))
                self.assertNotIn("secret-bytes", json.dumps(gone))
                planted = str(uuid.uuid4())
                worker.db.execute(
                    "INSERT INTO artifacts(id, run_id, path, size, digest) VALUES (?, ?, ?, ?, ?)",
                    (planted, record["run_id"], "../outside", 12, "ab" * 32),
                )
                escaped = worker.response(_request("artifact.read", {"artifact_id": planted}))
                validate_response("artifact.read", escaped)
                self.assertEqual(escaped["error"]["data"]["kind"], "INTERNAL_ERROR")
                self.assertNotIn("secret-bytes", json.dumps(escaped))
                self.assertNotIn(str(outside), json.dumps(escaped))
                ids = [
                    row[0]
                    for row in worker.db.execute(
                        "SELECT id FROM artifacts WHERE run_id=? AND path!='../outside' ORDER BY path",
                        (record["run_id"],),
                    )
                ]
                worker._publish_artifacts(record)
                again = [
                    row[0]
                    for row in worker.db.execute(
                        "SELECT id FROM artifacts WHERE run_id=? AND path!='../outside' ORDER BY path",
                        (record["run_id"],),
                    )
                ]
                self.assertEqual(again, ids)
            finally:
                worker.close()

    def test_restart_publishes_files_from_the_attempt_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._submit_workflow(worker, "wf")
                record = accepted["result"]
                attempt_id = str(uuid.uuid4())
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                attempt = state / "attempts" / attempt_id
                attempt.mkdir(parents=True)
                workspace = attempt / "workspace"
                shutil.copytree(snapshot / "files", workspace, symlinks=True)
                (workspace / "out").mkdir()
                (workspace / "out" / "from-run.txt").write_bytes(b"kept")
                record.update(state="running", started_at=now(), attempt_id=attempt_id)
                with worker.db:
                    worker.save(record)
            finally:
                worker.close()
            again = Worker(repo, state)
            again.execute_queue = lambda: again.stop.wait()
            again.start()
            try:
                lost = again.get(record["run_id"])
                self.assertEqual(lost["state"], "lost")
                reply = again.response(_request("run.artifacts", {"run_id": record["run_id"]}))
                validate_response("run.artifacts", reply)
                listed = reply["result"]["artifacts"]
                self.assertEqual([item["path"] for item in listed], ["out/from-run.txt"])
                self.assertEqual(listed[0]["digest"], hashlib.sha256(b"kept").hexdigest())
                self.assertFalse(workspace.exists())
                self.assertEqual(lost["cleanup"], "confirmed_no_external_resources")
            finally:
                again.close()

    def test_cli_artifact_command_reaches_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
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
                submitted = call(
                    state,
                    "run.submit",
                    {
                        "version": 0,
                        "submission_key": "cli",
                        "backend": "development",
                        "fixture": {},
                    },
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "execution_core",
                        "--state",
                        str(state),
                        "artifacts",
                        submitted["result"]["run_id"],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 1)
                reply = json.loads(result.stdout)
                self.assertEqual(reply["error"]["data"]["kind"], "CAPABILITY_UNSUPPORTED")
                self.assertNotIn(str(state), result.stdout)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def _submit_fixture(self, worker, key):
        reply = worker.response(
            _request(
                "run.submit",
                {
                    "version": 0,
                    "submission_key": key,
                    "backend": "development",
                    "fixture": {"output": "ok\n"},
                },
            )
        )
        validate_response("run.submit", reply)
        return reply

    def _submit_workflow(self, worker, key):
        reply = worker.response(
            _request(
                "run.submit",
                {
                    "version": 1,
                    "submission_key": key,
                    "workflow": ".github/workflows/test.yml",
                    "job_id": "build",
                    "event": EVENT,
                    "image": IMAGE,
                },
            )
        )
        validate_response("run.submit", reply)
        return reply

    def _git_workflow(self, repo):
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        (repo / "source.txt").write_text("original\n")
        (repo / "keep.txt").write_text("keep\n")
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
