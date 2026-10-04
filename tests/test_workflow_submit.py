import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from execution_core.cli import call
from execution_core.plan import MAX_WORKFLOW_BYTES
from execution_core.protocol import canonical
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
UNSUPPORTED = """\
on: push
jobs:
  build:
    steps:
      - uses: example/action@v1
"""
INVALID = "jobs: [\n"


class WorkflowSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workflow-submit-")
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
            process.communicate(timeout=5)
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

    def submit(self, key="test", **fixture):
        return self.rpc(
            "run.submit",
            {"version": 0, "submission_key": key, "backend": "development", "fixture": fixture},
        )

    def await_state(self, run_id, states):
        deadline = time.monotonic() + 20
        record = None
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": run_id})
            if record["state"] in states:
                return record
            time.sleep(0.01)
        self.fail(f"run did not reach {states}: {record}")

    def assert_fault(self, reply, kind, code=-32000):
        validator("ErrorResponse").validate(reply)
        self.assertEqual(reply["error"]["data"]["kind"], kind)
        self.assertEqual(reply["error"]["code"], code)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def write_workflows(self):
        (self.repo / "source.txt").write_text("original\n")
        for name, text in (
            (".github/workflows/test.yml", WORKFLOW),
            (".github/workflows/unsupported.yml", UNSUPPORTED),
            (".github/workflows/invalid.yml", INVALID),
        ):
            path = self.repo / name
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
            "submission_key": "wf-1",
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": EVENT,
            "image": IMAGE,
        }
        body.update(overrides)
        return body

    def test_registry_pin_stores_sha256_prefix(self):
        self.write_workflows()
        blocker = self.submit("hold-registry", delay_ms=3000)
        self.await_state(blocker["run_id"], {"running"})
        reference = "example.com/runner/app@sha256:" + "ab" * 32
        run = self.rpc("run.submit", self.params(submission_key="wf-ref", image=reference))
        self.assertEqual(run["input"]["image_reference"], reference)
        self.assertEqual(run["input"]["image_digest"], "sha256:" + "ab" * 32)
        cancelled = self.rpc("run.cancel", {"version": 0, "run_id": run["run_id"]})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["attempt_id"])

    def test_describe_advertises_workflow_job_only(self):
        described = self.rpc("worker.describe", {})
        self.assertEqual(described["protocol_versions"], [0, 1])
        self.assertEqual(
            described["capabilities"],
            ["development.fixture", "run.cancel", "run.logs", "workflow.job"],
        )
        for name in ("uses", "secrets", "matrix", "services", "actions", "checkout"):
            self.assertNotIn(name, described["capabilities"])

    def test_acceptance_binds_digests_and_ignores_later_checkout_edits(self):
        self.write_workflows()
        run = self.rpc("run.submit", self.params())
        self.assertEqual(run["state"], "queued")
        self.assertIsNone(run["exit_code"])
        self.assertEqual(run["backend"]["name"], "workflow")
        recorded = run["input"]
        self.assertEqual(recorded["kind"], "workflow_job")
        self.assertEqual(recorded["workflow"], ".github/workflows/test.yml")
        self.assertEqual(recorded["job_id"], "build")
        self.assertEqual(recorded["image_reference"], IMAGE)
        self.assertEqual(recorded["image_digest"], IMAGE)
        self.assertEqual(
            recorded["event_digest"],
            hashlib.sha256(canonical(EVENT).encode("ascii")).hexdigest(),
        )
        snapshot = self.state / "snapshots" / recorded["snapshot_id"] / "files" / "source.txt"
        self.assertEqual(snapshot.read_text(), "original\n")
        (self.repo / "source.txt").write_text("changed after acceptance\n")
        retry = self.rpc("run.submit", self.params())
        self.assertEqual(retry["run_id"], run["run_id"])
        self.assertEqual(retry["input"], recorded)
        self.assertEqual(snapshot.read_text(), "original\n")
        conflict = call(self.state, "run.submit", self.params(event={"kind": "other"}))
        self.assert_fault(conflict, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 1)
        failed = self.await_state(run["run_id"], {"failed"})
        self.assertIsNone(failed["exit_code"])
        self.assertEqual(failed["error"]["kind"], "SETUP_FAILED")
        self.assertEqual(failed["input"], recorded)
        development = self.submit("dev-after")
        self.assertEqual(
            self.await_state(development["run_id"], {"succeeded"})["state"], "succeeded"
        )

    def test_invalid_workflow_creates_no_run(self):
        self.write_workflows()
        cases = [
            (self.params(workflow=".github/workflows/invalid.yml"), "INVALID_PARAMS", -32602),
            (
                self.params(workflow=".github/workflows/unsupported.yml"),
                "CAPABILITY_UNSUPPORTED",
                -32000,
            ),
            (self.params(image="ubuntu:latest"), "INVALID_PARAMS", -32602),
            (self.params(job_id="missing"), "INVALID_PARAMS", -32602),
        ]
        for params, kind, code in cases:
            with self.subTest(kind=kind, workflow=params["workflow"], image=params["image"]):
                self.assert_fault(call(self.state, "run.submit", params), kind, code)
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_workflow_over_file_limit_creates_no_run(self):
        self.write_workflows()
        body = b"on: push\njobs:\n  build:\n    steps:\n      - run: echo ok\n"
        extra = MAX_WORKFLOW_BYTES + 1 - len(body)
        path = self.repo / ".github/workflows/large.yml"
        path.write_bytes(body + b"#" + b"x" * (extra - 1))
        self.assertGreater(path.stat().st_size, MAX_WORKFLOW_BYTES)
        self.git("add", ".github/workflows/large.yml")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "large",
        )
        reply = call(
            self.state,
            "run.submit",
            self.params(workflow=".github/workflows/large.yml", submission_key="large"),
        )
        self.assert_fault(reply, "CAPABILITY_UNSUPPORTED", -32000)
        self.assertIn("500 KB", reply["error"]["message"])
        self.assertIn("docs.github.com/en/actions/reference/limits", reply["error"]["message"])
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_write_permissions_create_no_run(self):
        self.write_workflows()
        path = self.repo / ".github/workflows/permissions.yml"
        path.write_text(
            "permissions: write-all\non: push\njobs:\n  build:\n    steps:\n      - run: echo ok\n"
        )
        self.git("add", ".github/workflows/permissions.yml")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "permissions",
        )
        reply = call(
            self.state,
            "run.submit",
            self.params(workflow=".github/workflows/permissions.yml", submission_key="permissions"),
        )
        self.assert_fault(reply, "CAPABILITY_UNSUPPORTED", -32000)
        self.assertIn("permissions", reply["error"]["message"])
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_step_timeout_above_360_creates_no_run(self):
        self.write_workflows()
        path = self.repo / ".github/workflows/step-timeout.yml"
        path.write_text(
            "on: push\njobs:\n  build:\n    steps:\n      - timeout-minutes: 361\n        run: echo ok\n"
        )
        self.git("add", ".github/workflows/step-timeout.yml")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "step timeout",
        )
        reply = call(
            self.state,
            "run.submit",
            self.params(
                workflow=".github/workflows/step-timeout.yml", submission_key="step-timeout"
            ),
        )
        self.assert_fault(reply, "CAPABILITY_UNSUPPORTED", -32000)
        self.assertIn("360", reply["error"]["message"])
        self.assertIn("workflow-syntax", reply["error"]["message"])
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_rejected_checkout_creates_no_run(self):
        self.write_workflows()
        rejected = {
            "clean.yml": (
                "clean: true",
                "jobs.build.steps.0.with.clean",
                "CAPABILITY_UNSUPPORTED",
            ),
            "persist.yml": (
                "persist-credentials: true",
                "jobs.build.steps.0.with.persist-credentials",
                "CAPABILITY_UNSUPPORTED",
            ),
            "token.yml": (
                "token: ignored",
                "jobs.build.steps.0.with.token",
                "CAPABILITY_UNSUPPORTED",
            ),
            "repository.yml": (
                "repository: example/name",
                "jobs.build.steps.0.with.repository",
                "CAPABILITY_UNSUPPORTED",
            ),
            "ref.yml": (
                "ref: main",
                "jobs.build.steps.0.with.ref",
                "CAPABILITY_UNSUPPORTED",
            ),
            "fetch-depth.yml": (
                "fetch-depth: 1",
                "jobs.build.steps.0.with.fetch-depth",
                "CAPABILITY_UNSUPPORTED",
            ),
            "ssh-key.yml": (
                "ssh-key: ignored",
                "jobs.build.steps.0.with.ssh-key",
                "CAPABILITY_UNSUPPORTED",
            ),
            "submodules.yml": (
                "submodules: true",
                "jobs.build.steps.0.with.submodules",
                "CAPABILITY_UNSUPPORTED",
            ),
            "text.yml": (
                'clean: "false"',
                "jobs.build.steps.0.with.clean",
                "INVALID_PARAMS",
            ),
        }
        uses = {
            "v7.yml": "actions/checkout@v7",
            "bare.yml": "actions/checkout",
            "short.yml": "actions/checkout@" + ("a" * 39),
            "main.yml": "actions/checkout@main",
            "v5.yml": "actions/checkout@v5",
        }
        for name, (body, field, kind) in rejected.items():
            path = self.repo / ".github" / "workflows" / name
            path.write_text(
                "on: push\njobs:\n  build:\n    steps:\n"
                f"      - uses: actions/checkout@v4\n        with:\n          {body}\n"
            )
        for name, reference in uses.items():
            path = self.repo / ".github" / "workflows" / name
            path.write_text(f"on: push\njobs:\n  build:\n    steps:\n      - uses: {reference}\n")
        self.git("add", ".github/workflows")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "checkout rejections",
        )
        for name, (_body, field, kind) in rejected.items():
            with self.subTest(name=name):
                reply = call(
                    self.state,
                    "run.submit",
                    self.params(
                        workflow=f".github/workflows/{name}",
                        submission_key=name,
                    ),
                )
                self.assert_fault(
                    reply, kind, -32000 if kind == "CAPABILITY_UNSUPPORTED" else -32602
                )
                self.assertIn(field, reply["error"]["message"])
        for name in uses:
            with self.subTest(name=name):
                reply = call(
                    self.state,
                    "run.submit",
                    self.params(
                        workflow=f".github/workflows/{name}",
                        submission_key=name,
                    ),
                )
                self.assert_fault(reply, "CAPABILITY_UNSUPPORTED", -32000)
                self.assertIn("jobs.build.steps.0.uses", reply["error"]["message"])
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        snaps = self.state / "snapshots"
        self.assertEqual([] if not snaps.exists() else list(snaps.iterdir()), [])

    def test_disconnect_before_reply_keeps_the_queued_run(self):
        self.write_workflows()
        params = self.params(submission_key="disconnect")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.state / "worker.sock"))
            connection.sendall(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 9, "method": "run.submit", "params": params}
                    )
                    + "\n"
                ).encode()
            )
        runs = self.rpc("run.list", {})["runs"]
        self.assertEqual(len(runs), 1)
        retry = self.rpc("run.submit", params)
        self.assertEqual(retry["run_id"], runs[0]["run_id"])
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 1)
        failed = self.await_state(runs[0]["run_id"], {"failed"})
        self.assertIsNone(failed["exit_code"])
        self.assertEqual(failed["error"]["kind"], "SETUP_FAILED")

    def test_cancel_queued_workflow_does_not_start_it(self):
        self.write_workflows()
        blocker = self.submit("hold-cancel", delay_ms=3000)
        self.await_state(blocker["run_id"], {"running"})
        run = self.rpc("run.submit", self.params())
        self.assertEqual(run["state"], "queued")
        cancelled = self.rpc("run.cancel", {"version": 0, "run_id": run["run_id"]})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["exit_code"])
        self.assertIsNone(cancelled["attempt_id"])
        self.assertEqual(self.await_state(blocker["run_id"], {"succeeded"})["state"], "succeeded")
        self.assertEqual(self.rpc("run.get", {"run_id": run["run_id"]})["state"], "cancelled")
        attempts = self.state / "attempts"
        self.assertEqual([] if not attempts.exists() else list(attempts.iterdir()), [])
