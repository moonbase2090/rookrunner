# SPDX-License-Identifier: MPL-2.0

"""Artifact tools on the stdio MCP adapter.

`artifacts` and `artifact_read` forward one socket call each.
A development fixture stays unsupported. A finished workflow keeps
its manifest, and reading the removed attempt stays an internal error.
"""

import hashlib
import json
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from execution_core.cli import call
from execution_core.protocol import canonical
from execution_core.worker import Worker, now

IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
SENTINEL = b"RR-SENTINEL-m3-evidence"
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def _readline(stream, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise AssertionError("adapter did not respond")
    line = stream.readline()
    if not line:
        raise AssertionError("adapter closed stdout")
    return json.loads(line)


class Session:
    def __init__(self, state):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "execution_core", "--state", str(state), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def request(self, method, params=None, timeout=5):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        return _readline(self.process.stdout, timeout)

    def call(self, name, arguments=None, timeout=5):
        reply = self.request(
            "tools/call",
            {"name": name, "arguments": arguments if arguments is not None else {}},
            timeout,
        )
        return reply["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.terminate()
        self.process.stderr.read()
        self.process.wait(timeout=5)


def _git_workflow(repo):
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


class FixtureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-evidence-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.sessions = []
        self.worker = self.start_worker()

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def start_worker(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "worker",
                "--repository",
                str(self.repo),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            try:
                call(self.state, "worker.describe", {})
                return process
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def session(self):
        opened = Session(self.state)
        self.sessions.append(opened)
        return opened

    def test_fixture_artifacts_are_unsupported_and_the_session_stays_up(self):
        submitted = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "fixture",
                "backend": "development",
                "fixture": {"exit_code": 0, "output": "short"},
            },
        )
        run_id = submitted["result"]["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": run_id})["result"]
            if record["state"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail("fixture did not succeed")
        session = self.session()
        refused = session.call("artifacts", {"run_id": run_id})
        self.assertIs(refused["isError"], True)
        self.assertEqual(refused["structuredContent"]["kind"], "CAPABILITY_UNSUPPORTED")
        self.assertIs(refused["structuredContent"]["retryable"], False)
        self.assertEqual(
            refused["content"],
            [{"type": "text", "text": canonical(refused["structuredContent"])}],
        )
        again = session.call("get", {"run_id": run_id})
        self.assertIs(again["isError"], False)
        self.assertEqual(again["structuredContent"]["state"], "succeeded")

    def test_tool_text_has_no_path_and_names_the_shared_failure(self):
        session = self.session()
        listed = session.request("tools/list")["result"]["tools"]
        names = [tool["name"] for tool in listed]
        self.assertIn("artifacts", names)
        self.assertIn("artifact_read", names)
        self.assertFalse({"run.status", "status", "fixture"} & set(names))
        reader = next(tool for tool in listed if tool["name"] == "artifact_read")
        self.assertNotIn("path", reader["inputSchema"]["properties"])
        self.assertEqual(reader["inputSchema"]["required"], ["artifact_id"])
        self.assertIn("artifact bytes are not available", reader["description"])
        self.assertIn("other internal failures", reader["description"])
        artifacts = next(tool for tool in listed if tool["name"] == "artifacts")
        self.assertNotIn("path", artifacts["inputSchema"]["properties"])


class WorkflowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-evidence-wf-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        _git_workflow(self.repo)
        self.worker = Worker(self.repo, self.state)
        self.worker.execute_queue = lambda: self.worker.stop.wait()
        self.thread = threading.Thread(target=self.worker.serve, daemon=True)
        self.thread.start()
        self.sessions = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.thread.is_alive():
                self.fail("worker serve thread exited")
            try:
                call(self.state, "worker.describe", {})
                return
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.worker.stop.set()
        server = self.worker.server
        if server is not None:
            server.close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def session(self):
        opened = Session(self.state)
        self.sessions.append(opened)
        return opened

    def test_workflow_bytes_match_the_cli_and_a_deleted_file_stays_internal(self):
        submitted = call(
            self.state,
            "run.submit",
            {
                "version": 1,
                "submission_key": "wf",
                "workflow": ".github/workflows/test.yml",
                "job_id": "build",
                "event": EVENT,
                "image": IMAGE,
            },
        )
        record = submitted["result"]
        attempt_id = str(uuid.uuid4())
        snapshot = self.state / "snapshots" / record["input"]["snapshot_id"]
        workspace = self.state / "attempts" / attempt_id / "workspace"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(snapshot / "files", workspace, symlinks=True)
        (workspace / "out").mkdir()
        sentinel_path = workspace / "out" / "sentinel.txt"
        sentinel_path.write_bytes(SENTINEL)
        record.update(state="running", started_at=now(), attempt_id=attempt_id)
        with self.worker.db:
            self.worker.save(record)
            self.worker._save_workflow_outcome(
                record["run_id"],
                record,
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "image_digest": record["input"]["image_digest"],
                    "steps": [],
                },
            )
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "artifacts",
                record["run_id"],
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cli_page = json.loads(cli.stdout)["result"]
        session = self.session()
        listed = session.call("artifacts", {"run_id": record["run_id"]})
        self.assertIs(listed["isError"], False)
        self.assertEqual(listed["structuredContent"], cli_page)
        self.assertEqual(
            listed["content"],
            [{"type": "text", "text": canonical(cli_page)}],
        )
        item = next(
            artifact
            for artifact in listed["structuredContent"]["artifacts"]
            if artifact["path"] == "out/sentinel.txt"
        )
        self.assertEqual(item["size"], len(SENTINEL))
        self.assertEqual(item["digest"], hashlib.sha256(SENTINEL).hexdigest())
        self.assertFalse(sentinel_path.exists())
        missing = session.call("artifact_read", {"artifact_id": item["id"]})
        self.assertIs(missing["isError"], True)
        self.assertEqual(missing["structuredContent"]["kind"], "INTERNAL_ERROR")
        self.assertEqual(
            missing["structuredContent"]["message"], "artifact bytes are not available"
        )
        read_cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "artifact-read",
                item["id"],
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(read_cli.returncode, 1, read_cli.stderr)
        cli_error = json.loads(read_cli.stdout)["error"]
        self.assertEqual(cli_error["data"]["kind"], "INTERNAL_ERROR")
        self.assertEqual(cli_error["message"], "artifact bytes are not available")
        self.assertNotIn(str(self.state), read_cli.stdout)
        self.assertNotIn(SENTINEL.decode(), read_cli.stdout)
        follow = session.call("get", {"run_id": record["run_id"]})
        self.assertIs(follow["isError"], False)
        self.assertEqual(follow["structuredContent"]["run_id"], record["run_id"])
