import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
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
LOGS = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: sleep 2
      - run: |
          echo out-two
          echo err-two >&2
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

    def spawn(self, env=None):
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
            env=env,
        )
        self.processes.append(process)
        return process

    def stop_workers(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.processes.clear()

    def start_worker(self, env=None, timeout=5):
        process = self.spawn(env)
        deadline = time.monotonic() + timeout
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

    def await_state(self, run_id, states, timeout=60):
        deadline = time.monotonic() + timeout
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

    def log_bytes(self, run_id, limit):
        data = bytearray()
        cursor = None
        ended = False
        for _ in range(10000):
            params = {"run_id": run_id, "limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.rpc("run.logs", params)
            chunk = base64.b64decode(page["data_base64"])
            self.assertLessEqual(len(chunk), limit)
            if not page["end_of_stream"]:
                self.assertEqual(len(chunk), limit)
            data.extend(chunk)
            cursor = page["next_cursor"]
            if page["end_of_stream"]:
                ended = True
                break
        self.assertTrue(ended)
        return bytes(data)

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
        self.assertEqual(self.log_bytes(done["run_id"], 3), b"before\n")

    def test_setup_failure_has_null_exit_and_structured_error(self):
        self.write_workflow(SUCCESS)
        image = "sha256:" + "ab" * 32
        submitted = self.rpc("run.submit", self.params(submission_key="setup-1", image=image))
        done = self.await_state(submitted["run_id"], {"failed"})
        self.assertIsNone(done["exit_code"])
        self.assertEqual(done["error"]["kind"], "SETUP_FAILED")
        self.assertIsNotNone(done["attempt_id"])
        self.assertNotIn("steps", done)
        page = self.rpc("run.logs", {"run_id": done["run_id"]})
        self.assertEqual(base64.b64decode(page["data_base64"]), b"")
        self.assertTrue(page["end_of_stream"])
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

    def test_logs_page_stdout_and_stderr_without_the_whole_output(self):
        self.write_workflow(LOGS)
        submitted = self.rpc("run.submit", self.params(submission_key="logs-1"))
        deadline = time.monotonic() + 30
        saw_active = False
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": submitted["run_id"]})
            if record["state"] == "running":
                active = self.rpc("run.logs", {"run_id": submitted["run_id"], "limit": 1})
                still = self.rpc("run.get", {"run_id": submitted["run_id"]})
                if still["state"] == "running":
                    self.assertFalse(active["end_of_stream"])
                    self.assertEqual(base64.b64decode(active["data_base64"]), b"")
                    saw_active = True
                    break
            elif record["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        self.assertTrue(saw_active)
        done = self.await_state(submitted["run_id"], {"succeeded"})
        expected = b"out-two\nerr-two\n"
        self.assertEqual(done["steps"][1]["stdout"], "out-two\n")
        self.assertEqual(done["steps"][1]["stderr"], "err-two\n")
        for limit in (1, 4, 65536):
            self.assertEqual(self.log_bytes(done["run_id"], limit), expected)

    def await_owned_container(self, run_id):
        deadline = time.monotonic() + 60
        record = None
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": run_id})
            names = subprocess.run(
                ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
                capture_output=True,
                text=True,
            )
            if record["state"] == "running" and names.stdout.strip():
                return record
            if record["state"] in {"succeeded", "failed", "cancelled", "lost"}:
                self.fail(f"run finished before its container could be cancelled: {record}")
            time.sleep(0.05)
        self.fail(f"owned container did not appear: {record}")

    def container_names(self):
        names = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return [line for line in names.stdout.splitlines() if line.startswith("rookrunner-")]

    def assert_no_containers(self):
        names = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertNotIn("rookrunner-", names.stdout)

    def test_explicit_timeout_allows_a_fast_job(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 1
    steps:
      - id: show
        run: echo ok
"""
        )
        started = time.monotonic()
        submitted = self.rpc("run.submit", self.params(submission_key="inside-timeout"))
        done = self.await_state(submitted["run_id"], {"succeeded", "failed", "cancelled"})
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["exit_code"], 0)
        self.assertIsNone(done["error"])
        self.assertFalse(done["cancel_requested"])
        self.assertEqual(done["steps"][0]["stdout"], "ok\n")
        self.assertLess(time.monotonic() - started, 30)
        self.assert_no_containers()

    def test_job_timeout_cancels_and_stops_the_container(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 1
    steps:
      - id: sleep
        run: sleep infinity
      - id: after
        run: echo after
"""
        )
        started = time.monotonic()
        submitted = self.rpc("run.submit", self.params(submission_key="job-timeout"))
        done = self.await_state(
            submitted["run_id"],
            {"cancelled", "failed", "succeeded", "lost"},
            timeout=120,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(done["state"], "cancelled")
        self.assertIsNone(done["exit_code"])
        self.assertIsNone(done["error"])
        self.assertFalse(done["cancel_requested"])
        self.assertNotEqual(done["state"], "succeeded")
        self.assertEqual([step["id"] for step in done["steps"]], ["sleep"])
        self.assertEqual(done["steps"][0]["error"], "job timed out")
        self.assertEqual(done["steps"][0]["exit_code"], None)
        # 60s is the minimum timeout-minutes. The stop grace is 7.5s + 2.5s.
        # https://docs.github.com/en/actions/reference/workflow-cancellation-reference
        # The remaining gap is scheduling slack for the test, not another limit.
        self.assertGreaterEqual(elapsed, 50)
        self.assertLess(elapsed, 90)
        self.assert_no_containers()
        page = self.rpc("run.logs", {"run_id": done["run_id"]})
        self.assertTrue(page["end_of_stream"])
        self.assertEqual(base64.b64decode(page["data_base64"]), b"")

    def test_step_timeout_fails_and_stops_before_later_steps(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: sleep
        timeout-minutes: 1
        run: sleep infinity
      - id: after
        run: echo after
"""
        )
        started = time.monotonic()
        submitted = self.rpc("run.submit", self.params(submission_key="step-timeout"))
        done = self.await_state(
            submitted["run_id"],
            {"cancelled", "failed", "succeeded", "lost"},
            timeout=120,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(done["state"], "failed")
        self.assertIsNone(done["exit_code"])
        self.assertEqual(done["error"]["kind"], "STEP_FAILED")
        self.assertIn("step timed out", done["error"]["message"])
        self.assertFalse(done["cancel_requested"])
        self.assertNotEqual(done["state"], "succeeded")
        self.assertEqual([step["id"] for step in done["steps"]], ["sleep"])
        self.assertEqual(done["steps"][0]["exit_code"], None)
        self.assertGreaterEqual(elapsed, 50)
        self.assertLess(elapsed, 90)
        self.assert_no_containers()

    def test_cancel_running_workflow_stops_the_container(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: sleep
        run: sleep infinity
"""
        )
        submitted = self.rpc("run.submit", self.params(submission_key="cancel-running"))
        self.await_owned_container(submitted["run_id"])
        started = time.monotonic()
        cancelled = self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]})
        # The stop grace is 7.5s + 2.5s. Fifteen seconds is test slack, not a limit.
        # https://docs.github.com/en/actions/reference/workflow-cancellation-reference
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])
        self.assertIsNone(cancelled["exit_code"])
        self.assertIsNone(cancelled["error"])
        self.assertEqual(cancelled["cleanup"], "confirmed_no_external_resources")
        self.assertNotEqual(cancelled["state"], "succeeded")
        self.assert_no_containers()
        again = self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]})
        self.assertEqual(again, cancelled)
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: show
        run: echo ok
"""
        )
        followed = self.rpc("run.submit", self.params(submission_key="after-cancel"))
        done = self.await_state(followed["run_id"], {"succeeded", "failed", "cancelled", "lost"})
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["exit_code"], 0)
        self.assert_no_containers()

    def test_unconfirmed_container_cleanup_is_lost_and_blocks_a_new_workflow(self):
        real = shutil.which("docker")
        self.assertIsNotNone(real)
        wrapper = self.root / "bin"
        wrapper.mkdir()
        script = wrapper / "docker"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "rm" ] && [ "$ROOKRUNNER_FAIL_RM" = "1" ]; then\n'
            "  exit 1\n"
            "fi\n"
            f'exec {shlex.quote(real)} "$@"\n'
        )
        script.chmod(0o755)
        self.stop_workers()
        env = os.environ.copy()
        env["PATH"] = str(wrapper) + os.pathsep + env.get("PATH", "")
        env["ROOKRUNNER_FAIL_RM"] = "1"
        self.worker = self.start_worker(env)
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: sleep
        run: sleep infinity
"""
        )
        submitted = self.rpc("run.submit", self.params(submission_key="fail-rm"))
        self.await_owned_container(submitted["run_id"])
        queued = self.rpc("run.submit", self.params(submission_key="stay-queued"))
        self.assertEqual(queued["state"], "queued")
        lost = self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]})
        self.assertEqual(lost["state"], "lost")
        self.assertTrue(lost["cancel_requested"])
        self.assertEqual(lost["cleanup"], "unresolved")
        self.assertIsNone(lost["exit_code"])
        self.assertEqual(lost["error"]["kind"], "WORKER_INTERRUPTED")
        self.assertNotIn("/", lost["error"]["message"])
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(names.stdout.strip())
        self.assertEqual(
            self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]}), lost
        )
        self.assertEqual(
            self.rpc("run.submit", self.params(submission_key="fail-rm"))["run_id"],
            lost["run_id"],
        )
        blocked = call(self.state, "run.submit", self.params(submission_key="blocked"))
        validate_response("run.submit", blocked)
        self.assertEqual(blocked["error"]["data"]["kind"], "WORKER_NOT_READY")
        self.assertIn("unresolved", blocked["error"]["message"])
        self.assertTrue(self.rpc("worker.describe", {})["ready"])
        still = self.rpc("run.get", {"run_id": queued["run_id"]})
        self.assertEqual(still["state"], "queued")
        self.assertIsNone(still["attempt_id"])
        development = self.rpc(
            "run.submit",
            {
                "version": 0,
                "submission_key": "fixture-during-unresolved",
                "backend": "development",
                "fixture": {},
            },
        )
        self.assertEqual(
            self.await_state(development["run_id"], {"succeeded"})["state"], "succeeded"
        )
        self.assertEqual(self.rpc("run.get", {"run_id": queued["run_id"]})["state"], "queued")
        runs = self.rpc("run.list", {})["runs"]
        self.assertEqual(
            sorted(run["submission_key"] for run in runs),
            ["fail-rm", "fixture-during-unresolved", "stay-queued"],
        )

    def test_workflow_completion_cancel_race_keeps_the_first_terminal_result(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: show
        run: echo ok
"""
        )
        submitted = self.rpc("run.submit", self.params(submission_key="race-1"))
        done = self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]})
        self.assertIn(done["state"], {"cancelled", "succeeded"})
        if done["state"] == "cancelled":
            self.assertTrue(done["cancel_requested"])
            self.assertIsNone(done["exit_code"])
            self.assertIsNone(done["error"])
        else:
            self.assertFalse(done["cancel_requested"])
            self.assertEqual(done["exit_code"], 0)
        again = self.rpc("run.cancel", {"version": 0, "run_id": submitted["run_id"]})
        self.assertEqual(again, done)
        self.assert_no_containers()

    def test_restart_marks_a_workflow_lost_and_starts_the_queue(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: sleep
        run: sleep infinity
"""
        )
        submitted = self.rpc("run.submit", self.params(submission_key="active-workflow"))
        running = self.await_owned_container(submitted["run_id"])
        attempt_id = running["attempt_id"]
        ownership = self.state / "ownership" / attempt_id
        name = ownership.read_text().strip()
        self.assertRegex(name, r"^rookrunner-[0-9a-f]{16}$")
        self.assertEqual(stat.S_IMODE(ownership.stat().st_mode), 0o600)
        self.assertEqual(self.container_names(), [name])
        self.assertNotIn(name, json.dumps(running))
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: show
        run: echo queued-ok
"""
        )
        queued = self.rpc("run.submit", self.params(submission_key="queued-workflow"))
        self.assertEqual(queued["state"], "queued")
        self.worker.kill()
        self.worker.communicate(timeout=5)
        self.worker = self.start_worker(timeout=30)
        lost = self.rpc("run.get", {"run_id": submitted["run_id"]})
        self.assertEqual(lost["state"], "lost")
        self.assertIsNone(lost["exit_code"])
        self.assertFalse(lost["cancel_requested"])
        self.assertEqual(lost["cleanup"], "confirmed_no_external_resources")
        self.assertEqual(lost["error"]["kind"], "WORKER_INTERRUPTED")
        self.assertEqual(lost["attempt_id"], attempt_id)
        self.assertNotIn(name, json.dumps(lost))
        self.assertNotIn("/", lost["error"]["message"])
        self.assertEqual(
            self.rpc("run.submit", self.params(submission_key="active-workflow"))["run_id"],
            submitted["run_id"],
        )
        self.assertEqual(self.container_names(), [])
        self.assertFalse(ownership.exists())
        other = self.spawn()
        other.communicate(timeout=5)
        self.assertNotEqual(other.returncode, 0)
        done = self.await_state(queued["run_id"], {"succeeded", "failed", "cancelled", "lost"})
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["exit_code"], 0)
        self.assertNotEqual(done["attempt_id"], attempt_id)
        still = self.rpc("run.get", {"run_id": submitted["run_id"]})
        self.assertEqual(still["state"], "lost")
        self.assertEqual(still["attempt_id"], attempt_id)
        self.assert_no_containers()

    def test_unremoved_leftover_blocks_reuse_and_still_starts_the_queue(self):
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: sleep
        run: sleep infinity
"""
        )
        submitted = self.rpc("run.submit", self.params(submission_key="keep-workflow"))
        running = self.await_owned_container(submitted["run_id"])
        attempt_id = running["attempt_id"]
        name = (self.state / "ownership" / attempt_id).read_text().strip()
        self.write_workflow(
            """\
name: demo
on: push
jobs:
  build:
    timeout-minutes: 30
    steps:
      - id: show
        run: echo queued-ok
"""
        )
        queued = self.rpc("run.submit", self.params(submission_key="queued-after-kill"))
        self.assertEqual(queued["state"], "queued")
        self.worker.kill()
        self.worker.communicate(timeout=5)
        real = shutil.which("docker")
        wrapper = self.root / "bin"
        wrapper.mkdir()
        script = wrapper / "docker"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "rm" ] && [ -n "$ROOKRUNNER_KEEP_CONTAINER" ]; then\n'
            '  for arg in "$@"; do\n'
            '    if [ "$arg" = "$ROOKRUNNER_KEEP_CONTAINER" ]; then\n'
            "      exit 1\n"
            "    fi\n"
            "  done\n"
            "fi\n"
            f'exec {shlex.quote(real)} "$@"\n'
        )
        script.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(wrapper) + os.pathsep + env.get("PATH", "")
        env["ROOKRUNNER_KEEP_CONTAINER"] = name
        self.worker = self.start_worker(env=env, timeout=30)
        lost = self.rpc("run.get", {"run_id": submitted["run_id"]})
        self.assertEqual(lost["state"], "lost")
        self.assertEqual(lost["cleanup"], "unresolved")
        self.assertFalse(lost["cancel_requested"])
        self.assertIsNone(lost["exit_code"])
        self.assertEqual(lost["error"]["kind"], "WORKER_INTERRUPTED")
        self.assertEqual(lost["attempt_id"], attempt_id)
        self.assertNotIn(name, json.dumps(lost))
        self.assertNotIn("/", lost["error"]["message"])
        self.assertEqual((self.state / "ownership" / attempt_id).read_text().strip(), name)
        self.assertIn(name, self.container_names())
        self.assertEqual(
            self.rpc("run.submit", self.params(submission_key="keep-workflow"))["run_id"],
            submitted["run_id"],
        )
        self.assertTrue(self.rpc("worker.describe", {})["ready"])
        done = self.await_state(
            queued["run_id"], {"succeeded", "failed", "cancelled", "lost"}, timeout=90
        )
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["exit_code"], 0)
        self.assertNotEqual(done["attempt_id"], attempt_id)
        self.assertEqual(self.container_names(), [name])
        still = self.rpc("run.get", {"run_id": submitted["run_id"]})
        self.assertEqual(still["state"], "lost")
        self.assertEqual(still["attempt_id"], attempt_id)
        self.assertEqual(still["cleanup"], "unresolved")
