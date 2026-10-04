import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from execution_core.cli import call
from execution_core.protocol import canonical
from execution_core.worker import Worker
from schema_support import validate_response


IMAGE = "rust@sha256:" + "ab" * 32
WORKFLOW = """\
name: demo
on: push
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def run_cli(state, *args, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "execution_core", "--state", str(state), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class CliUsageTests(unittest.TestCase):
    def test_reproduction_flags_are_not_an_argument_error(self):
        result = run_cli(
            "unused",
            "submit",
            "--backend",
            "development",
            "--key",
            "dogfood",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["kind"], "CLIENT_ERROR")

    def test_partial_workflow_flags_exit_2(self):
        result = run_cli("unused", "submit", "--key", "k", "--workflow", "a.yml")
        self.assertEqual(result.returncode, 2)
        self.assertIn("workflow submit requires", result.stderr)

    def test_fixture_options_cannot_mix_with_a_workflow(self):
        result = run_cli(
            "unused",
            "submit",
            "--key",
            "k",
            "--exit-code",
            "1",
            "--workflow",
            "a.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("fixture options", result.stderr)

    def test_fixture_submit_still_requires_a_backend(self):
        result = run_cli("unused", "submit", "--key", "k")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fixture submit requires", result.stderr)

    def test_event_name_requires_a_workflow_submit(self):
        result = run_cli(
            "unused",
            "submit",
            "--backend",
            "development",
            "--key",
            "k",
            "--event-name",
            "push",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--event-name is only accepted with a workflow submit", result.stderr)

    def test_event_name_rejects_a_newline(self):
        result = run_cli(
            "unused",
            "submit",
            "--key",
            "k",
            "--workflow",
            "a.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
            "--event-name",
            "push\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--event-name must be 1 to 128 characters", result.stderr)

    def test_event_must_be_json(self):
        result = run_cli(
            "unused",
            "submit",
            "--key",
            "k",
            "--workflow",
            "a.yml",
            "--job-id",
            "ci",
            "--event",
            "{",
            "--image",
            IMAGE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--event must be one JSON value", result.stderr)

    def test_worker_network_none_is_recognized(self):
        result = run_cli(
            "unused-state", "worker", "--repository", "unused-repo", "--network", "none"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_worker_network_host_exits_2(self):
        result = run_cli(
            "unused-state", "worker", "--repository", "unused-repo", "--network", "host"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_worker_records_bridge_or_none(self):
        with tempfile.TemporaryDirectory(prefix="cli-network-") as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            state = Path(directory) / "state"
            self.assertEqual(Worker(repo, state).network, "bridge")
            self.assertEqual(Worker(repo, state, network="none").network, "none")
        with self.assertRaises(ValueError):
            Worker(".", ".", network="host")

    def test_worker_docker_socket_is_off_unless_requested(self):
        result = run_cli("unused-state", "worker", "--repository", "unused-repo", "--docker-socket")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)
        with tempfile.TemporaryDirectory(prefix="cli-socket-") as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            state = Path(directory) / "state"
            self.assertIs(Worker(repo, state).docker_socket, False)
            self.assertIs(Worker(repo, state, docker_socket=True).docker_socket, True)
        with self.assertRaises(ValueError):
            Worker(".", ".", docker_socket="on")


class CliWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-worker-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        workflow = self.repo / ".github" / "workflows" / "dogfood.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
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
        self.worker = self.start_worker()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            try:
                call(self.state, "worker.describe", {})
                return process
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def await_state(self, run_id, states):
        deadline = time.monotonic() + 20
        record = None
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": run_id})["result"]
            if record["state"] in states:
                return record
            time.sleep(0.01)
        self.fail(f"run did not reach {states}: {record}")

    def test_workflow_flags_submit_version_1_and_print_the_run_id(self):
        hold = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "hold",
                "backend": "development",
                "fixture": {"delay_ms": 5000},
            },
        )
        self.await_state(hold["result"]["run_id"], {"running"})
        submitted = run_cli(
            self.state,
            "submit",
            "--backend",
            "development",
            "--key",
            "dogfood",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        reply = json.loads(submitted.stdout)
        validate_response("run.submit", reply)
        run = reply["result"]
        self.assertEqual(run["state"], "queued")
        self.assertEqual(run["input"]["kind"], "workflow_job")
        self.assertEqual(run["input"]["job_id"], "ci")
        self.assertEqual(run["input"]["image_reference"], IMAGE)
        self.assertEqual(run["backend"]["name"], "workflow")
        self.assertEqual(
            run["input"]["event_digest"],
            hashlib.sha256(canonical({}).encode("ascii")).hexdigest(),
        )
        again = run_cli(
            self.state,
            "submit",
            "--key",
            "dogfood",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
        )
        self.assertEqual(json.loads(again.stdout)["result"]["run_id"], run["run_id"])
        cancelled = call(self.state, "run.cancel", {"version": 0, "run_id": run["run_id"]})
        self.assertEqual(cancelled["result"]["state"], "cancelled")

    def test_event_name_flag_is_part_of_the_submission(self):
        hold = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "hold-name",
                "backend": "development",
                "fixture": {"delay_ms": 5000},
            },
        )
        self.await_state(hold["result"]["run_id"], {"running"})
        submitted = run_cli(
            self.state,
            "submit",
            "--key",
            "named",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
            "--event-name",
            "push",
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        reply = json.loads(submitted.stdout)
        validate_response("run.submit", reply)
        run = reply["result"]
        self.assertEqual(run["state"], "queued")
        self.assertNotIn("event_name", run["input"])
        again = run_cli(
            self.state,
            "submit",
            "--key",
            "named",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
            "--event-name",
            "push",
        )
        self.assertEqual(json.loads(again.stdout)["result"]["run_id"], run["run_id"])
        conflict = run_cli(
            self.state,
            "submit",
            "--key",
            "named",
            "--workflow",
            ".github/workflows/dogfood.yml",
            "--job-id",
            "ci",
            "--event",
            "{}",
            "--image",
            IMAGE,
            "--event-name",
            "pull",
        )
        self.assertEqual(conflict.returncode, 1, conflict.stderr)
        self.assertEqual(
            json.loads(conflict.stdout)["error"]["data"]["kind"],
            "IDEMPOTENCY_CONFLICT",
        )
        call(self.state, "run.cancel", {"version": 0, "run_id": run["run_id"]})

    def test_follow_waits_for_status_and_logs(self):
        submitted = run_cli(
            self.state,
            "submit",
            "--backend",
            "development",
            "--key",
            "ok",
            "--output",
            "hello\n",
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        run_id = json.loads(submitted.stdout)["result"]["run_id"]
        followed = run_cli(self.state, "follow", run_id)
        self.assertEqual(followed.returncode, 0, followed.stderr)
        lines = [json.loads(line) for line in followed.stdout.splitlines()]
        self.assertTrue(any(line["result"].get("state") == "succeeded" for line in lines))
        logged = [
            base64.b64decode(line["result"]["data_base64"])
            for line in lines
            if "data_base64" in line["result"]
        ]
        self.assertIn(b"hello\n", logged)
        for line in lines:
            method = "run.logs" if "data_base64" in line["result"] else "run.get"
            validate_response(method, line)
        got = run_cli(self.state, "get", run_id)
        self.assertEqual(got.returncode, 0)

    def test_follow_exits_nonzero_when_the_run_fails(self):
        submitted = run_cli(
            self.state,
            "submit",
            "--backend",
            "development",
            "--key",
            "bad",
            "--exit-code",
            "1",
            "--output",
            "no\n",
        )
        run_id = json.loads(submitted.stdout)["result"]["run_id"]
        followed = run_cli(self.state, "follow", run_id)
        self.assertEqual(followed.returncode, 1, followed.stderr)
        lines = [json.loads(line) for line in followed.stdout.splitlines()]
        self.assertTrue(any(line["result"].get("state") == "failed" for line in lines))
        self.assertIn(
            b"no\n",
            [
                base64.b64decode(line["result"]["data_base64"])
                for line in lines
                if "data_base64" in line["result"]
            ],
        )
        got = run_cli(self.state, "get", run_id)
        self.assertEqual(got.returncode, 0)
        self.assertEqual(json.loads(got.stdout)["result"]["state"], "failed")

    def test_follow_reports_an_unknown_run(self):
        missing = run_cli(self.state, "follow", "missing")
        self.assertEqual(missing.returncode, 1)
        reply = json.loads(missing.stdout)
        validate_response("run.get", reply)
        self.assertEqual(reply["error"]["data"]["kind"], "RUN_NOT_FOUND")
