import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from execution_core.cli import call
from execution_core.protocol import canonical
from schema_support import validate_response, validator


EVENT = {"kind": "local", "n": 1}
SUCCESS = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: show
        name: show source
        run: cat source.txt
      - id: event
        run: cat "$ROOKRUNNER_EVENT"
"""
FAILURE = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: one
        run: echo before
      - id: fail
        name: fail step
        run: exit 3
      - id: after
        run: echo after
"""
SLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: sleep 1
      - id: done
        run: echo finished
"""


class WorkflowRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.image_root = tempfile.TemporaryDirectory(prefix="workflow-image-")
        root = Path(cls.image_root.name)
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n")
        cls.tag = "rookrunner-ns6-fixture"
        subprocess.run(
            ["docker", "build", "--quiet", "-t", cls.tag, str(root)],
            check=True,
            capture_output=True,
        )
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", cls.tag],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.image = inspected.stdout.strip()

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rmi", "-f", cls.tag], capture_output=True)
        cls.image_root.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workflow-run-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.worker = self.start_worker()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            capture_output=True,
            text=True,
        )
        for container in names.stdout.split():
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        self.tmp.cleanup()

    def spawn(self):
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    def start_worker(self):
        process = self.spawn()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            try:
                self.rpc("worker.describe", {})
                return process
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def rpc(self, method, params):
        validator("Request").validate(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        reply = call(self.state, method, params)
        validate_response(method, reply)
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def await_state(self, run_id, states):
        deadline = time.monotonic() + 60
        record = None
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": run_id})
            if record["state"] in states:
                return record
            time.sleep(0.05)
        self.fail(f"run did not reach {states}: {record}")

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def write_workflow(self, text):
        (self.repo / "source.txt").write_text("original\n")
        path = self.repo / ".github/workflows/test.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git("init", "--initial-branch=main")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def params(self, **overrides):
        body = {
            "version": 1,
            "submission_key": "run-1",
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": EVENT,
            "image": self.image,
        }
        body.update(overrides)
        return body

    def test_success_uses_captured_bytes_and_records_digests(self):
        self.write_workflow(SUCCESS)
        submitted = self.rpc("run.submit", self.params())
        self.assertEqual(submitted["state"], "queued")
        (self.repo / "source.txt").write_text("changed after acceptance\n")
        done = self.await_state(submitted["run_id"], {"succeeded"})
        self.assertEqual(done["exit_code"], 0)
        self.assertIsNone(done["error"])
        self.assertEqual(done["input"], submitted["input"])
        self.assertEqual(done["input"]["image_digest"], self.image)
        self.assertEqual([step["exit_code"] for step in done["steps"]], [0, 0])
        self.assertEqual(done["steps"][0]["stdout"], "original\n")
        self.assertEqual(done["steps"][1]["stdout"], canonical(EVENT) + "\n")
        workspace = self.state / "attempts" / done["attempt_id"] / "source.txt"
        self.assertEqual(workspace.read_text(), "original\n")
        names = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertNotIn("rookrunner-", names.stdout)
        development = self.rpc(
            "run.submit",
            {
                "version": 0,
                "submission_key": "dev-after",
                "backend": "development",
                "fixture": {},
            },
        )
        self.assertEqual(
            self.await_state(development["run_id"], {"succeeded"})["state"], "succeeded"
        )

    def test_nonzero_step_fails_and_stops(self):
        self.write_workflow(FAILURE)
        submitted = self.rpc("run.submit", self.params(submission_key="fail-1"))
        done = self.await_state(submitted["run_id"], {"failed"})
        self.assertEqual(done["exit_code"], 3)
        self.assertIsNone(done["error"])
        self.assertEqual(done["input"]["image_digest"], self.image)
        self.assertEqual(done["input"]["digest"], submitted["input"]["digest"])
        self.assertEqual([step["id"] for step in done["steps"]], ["one", "fail"])
        self.assertEqual(done["steps"][1]["name"], "fail step")
        self.assertEqual(done["steps"][1]["exit_code"], 3)

    def test_setup_failure_has_null_exit_and_structured_error(self):
        self.write_workflow(SUCCESS)
        image = "sha256:" + "ab" * 32
        submitted = self.rpc("run.submit", self.params(submission_key="setup-1", image=image))
        done = self.await_state(submitted["run_id"], {"failed"})
        self.assertIsNone(done["exit_code"])
        self.assertEqual(done["error"]["kind"], "SETUP_FAILED")
        self.assertIsNotNone(done["attempt_id"])
        self.assertNotIn("steps", done)
        self.assertEqual(done["input"]["image_digest"], image)
        self.assertEqual(done["input"]["digest"], submitted["input"]["digest"])

    def test_client_disconnect_does_not_stop_the_container(self):
        self.write_workflow(SLOW)
        params = self.params(submission_key="disconnect-run")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.state / "worker.sock"))
            connection.sendall(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 4, "method": "run.submit", "params": params}
                    )
                    + "\n"
                ).encode()
            )
        runs = self.rpc("run.list", {})["runs"]
        self.assertEqual(len(runs), 1)
        done = self.await_state(runs[0]["run_id"], {"succeeded"})
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["steps"][1]["stdout"], "finished\n")
        self.assertEqual(done["input"]["image_digest"], self.image)
