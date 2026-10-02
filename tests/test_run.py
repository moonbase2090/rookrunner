import ast
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from execution_core.attempt import materialize_attempt
from execution_core.plan import plan_workflow
from execution_core.run import RunError, _exec_limit, run_job
from execution_core.snapshot import SourceCapture


SUCCESS = """\
name: demo
on: push
env:
  LEVEL: workflow
  TRACE: kept
defaults:
  run:
    working-directory: app
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      LEVEL: job
    steps:
      - name: first
        run: |
          printf '%s' "$LEVEL" > level.txt
          printf '%s' "$TRACE" > trace.txt
          printf '%s' "$PWD" > pwd.txt
          printf '%s' "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/workspace.txt"
          cp "$ROOKRUNNER_EVENT" "$GITHUB_WORKSPACE/event.json"
          if false | true; then printf '%s' ok > "$GITHUB_WORKSPACE/unspecified.txt"
          else printf '%s' bad > "$GITHUB_WORKSPACE/unspecified.txt"; fi
          printf '%s\\n' one > "$GITHUB_WORKSPACE/order.txt"
          cat "$GITHUB_WORKSPACE/link.txt" > "$GITHUB_WORKSPACE/linked.txt"
      - id: nested
        name: nested step
        working-directory: app/nested
        env:
          LEVEL: step
        run: |
          printf '%s' "$LEVEL" > level.txt
          printf '%s' "$TRACE" > trace.txt
          printf '%s' "$PWD" > pwd.txt
          printf '%s\\n' two >> "$GITHUB_WORKSPACE/order.txt"
      - shell: bash
        run: |
          if false | true; then printf '%s' bad > "$GITHUB_WORKSPACE/bash.txt"
          else printf '%s' ok > "$GITHUB_WORKSPACE/bash.txt"; fi
          printf '%s' '${{ github.sha }}' > "$GITHUB_WORKSPACE/expr.txt"
          printf '%s' "$ROOKRUNNER_TEST_MARKER" > "$GITHUB_WORKSPACE/hostenv.txt"
      - shell: sh
        run: |
          if false | true; then printf '%s' ok > "$GITHUB_WORKSPACE/sh.txt"
          else printf '%s' bad > "$GITHUB_WORKSPACE/sh.txt"; fi
          cat "$GITHUB_WORKSPACE/source.txt" > "$GITHUB_WORKSPACE/source-copy.txt"
"""

FAILURE = """\
on: push
jobs:
  build:
    steps:
      - id: first
        name: first
        run: printf '%s\\n' one > "$GITHUB_WORKSPACE/order.txt"
      - id: fail
        name: fail step
        run: exit 3
      - id: later
        name: later
        run: printf '%s\\n' later >> "$GITHUB_WORKSPACE/order.txt"
"""

EVENT = {"kind": "local", "n": 1}


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _capture(root, workflow_text):
    repo = root / "repo"
    repo.mkdir()
    workflow = repo / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(workflow_text)
    (repo / "source.txt").write_text("original\n")
    (repo / "link.txt").symlink_to("source.txt")
    app = repo / "app"
    app.mkdir()
    (app / "keep.txt").write_text("keep\n")
    nested = app / "nested"
    nested.mkdir()
    (nested / "keep.txt").write_text("nested\n")
    state = root / "state"
    capture = SourceCapture(repo, state)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    captured = capture.capture(".github/workflows/test.yml")
    snapshot = state / "snapshots" / captured["snapshot_id"]
    attempts = state / "attempts"
    attempts.mkdir(mode=0o700)
    workspace = attempts / "run-1"
    materialize_attempt(snapshot, captured["digest"], workspace)
    return repo, snapshot, captured["digest"], workspace


def _plan(workflow_text):
    return plan_workflow(workflow_text.encode(), "build")["plan"]


class TimeoutLimitTests(unittest.TestCase):
    def test_job_deadline_wins_an_equal_step_limit(self):
        self.assertEqual(_exec_limit(10, None), (10, True))
        self.assertEqual(_exec_limit(10, 10), (10, True))
        self.assertEqual(_exec_limit(10, 11), (10, True))
        self.assertEqual(_exec_limit(10, 9), (9, False))
        self.assertEqual(_exec_limit(0, 5), (0, True))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.snapshot, self.digest, self.workspace = _capture(self.root, SUCCESS)
        self.plan = _plan(SUCCESS)
        self.marker = self.root / "docker-called"
        docker = self.root / "docker"
        docker.write_text(f"#!/bin/sh\nprintf '%s\\n' called >> '{self.marker}'\nexit 99\n")
        docker.chmod(0o755)
        self.docker = docker

    def test_unpinned_image_does_not_run(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "ubuntu:latest",
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("not pinned", str(raised.exception))
        self.assertFalse(self.marker.exists())
        self.assertFalse(hasattr(raised.exception, "exit_code"))

    def test_missing_docker_is_setup_failure(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.root / "missing-docker"),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("Docker is missing", str(raised.exception))

    def test_changed_workspace_is_not_a_step_exit(self):
        (self.workspace / "source.txt").write_text("changed\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("workspace failed verification", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_changed_snapshot_is_not_a_step_exit(self):
        (self.snapshot / "files" / "source.txt").write_text("changed\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("snapshot failed verification", str(raised.exception))
        self.assertFalse(self.marker.exists())


class DockerRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.image_root = tempfile.TemporaryDirectory(prefix="run-image-")
        root = Path(cls.image_root.name)
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n")
        tag = "rookrunner-ns4-fixture"
        subprocess.run(
            ["docker", "build", "--quiet", "-t", tag, root],
            check=True,
            capture_output=True,
        )
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.image = inspected.stdout.strip()
        cls.tag = tag

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rmi", "-f", cls.tag], capture_output=True)
        cls.image_root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run-docker-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.snapshot, self.digest, self.workspace = _capture(self.root, SUCCESS)
        self.log = self.root / "docker-args"
        wrapper = self.root / "docker"
        real = shutil.which("docker")
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"path = {str(self.log)!r}\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(repr(sys.argv[1:]) + '\\n')\n"
            f"raise SystemExit(subprocess.call([{real!r}, *sys.argv[1:]]))\n"
        )
        wrapper.chmod(0o755)
        self.docker = wrapper
        self.previous = os.environ.get("ROOKRUNNER_TEST_MARKER")
        os.environ["ROOKRUNNER_TEST_MARKER"] = "fixture-secret-value"

    def tearDown(self):
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            capture_output=True,
            text=True,
        )
        for container in names.stdout.split():
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        if self.previous is None:
            os.environ.pop("ROOKRUNNER_TEST_MARKER", None)
        else:
            os.environ["ROOKRUNNER_TEST_MARKER"] = self.previous

    def _calls(self):
        return [ast.literal_eval(line) for line in self.log.read_text().splitlines() if line]

    def test_steps_run_in_one_pinned_container(self):
        (self.repo / "source.txt").write_text("mutated checkout\n")
        result = run_job(
            self.snapshot,
            self.digest,
            self.workspace,
            _plan(SUCCESS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["failed_step"])
        self.assertEqual(result["image_digest"], self.image)
        self.assertEqual(result["image_reference"], self.image)
        self.assertEqual([step["exit_code"] for step in result["steps"]], [0, 0, 0, 0])
        self.assertEqual((self.workspace / "order.txt").read_text(), "one\ntwo\n")
        self.assertEqual((self.workspace / "app" / "level.txt").read_text(), "job")
        self.assertEqual((self.workspace / "app" / "trace.txt").read_text(), "kept")
        self.assertEqual((self.workspace / "app" / "pwd.txt").read_text(), "/workspace/app")
        self.assertEqual((self.workspace / "workspace.txt").read_text(), "/workspace")
        self.assertEqual((self.workspace / "event.json").read_text(), '{"kind":"local","n":1}\n')
        self.assertEqual((self.workspace / "unspecified.txt").read_text(), "ok")
        self.assertEqual((self.workspace / "app" / "nested" / "level.txt").read_text(), "step")
        self.assertEqual((self.workspace / "app" / "nested" / "trace.txt").read_text(), "kept")
        self.assertEqual(
            (self.workspace / "app" / "nested" / "pwd.txt").read_text(), "/workspace/app/nested"
        )
        self.assertEqual((self.workspace / "bash.txt").read_text(), "ok")
        self.assertEqual((self.workspace / "sh.txt").read_text(), "ok")
        self.assertEqual((self.workspace / "expr.txt").read_text(), "${{ github.sha }}")
        self.assertEqual((self.workspace / "hostenv.txt").read_text(), "")
        self.assertEqual((self.workspace / "source-copy.txt").read_text(), "original\n")
        self.assertEqual((self.workspace / "linked.txt").read_text(), "original\n")
        self.assertEqual((self.repo / "source.txt").read_text(), "mutated checkout\n")
        calls = self._calls()
        creates = [call for call in calls if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)
        create = creates[0]
        self.assertIn("--network", create)
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertNotIn("--privileged", create)
        rendered = "\n".join(repr(call) for call in calls)
        self.assertNotIn("docker.sock", rendered)
        self.assertNotIn(".ssh", rendered)
        self.assertNotIn("fixture-secret-value", rendered)
        self.assertNotIn(self.image.split(":", 1)[0] + ":latest", rendered)
        name = create[create.index("--name") + 1]
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.stdout.strip(), "")
        self.assertEqual(stat.S_IMODE((self.workspace / "order.txt").stat().st_mode) & 0o777, 0o644)

    def test_nonzero_step_stops_and_names_the_digest(self):
        fail_root = self.root / "fail"
        fail_root.mkdir()
        _root, snapshot, digest, workspace = _capture(fail_root, FAILURE)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(FAILURE),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["image_digest"], self.image)
        self.assertEqual(result["failed_step"]["index"], 1)
        self.assertEqual(result["failed_step"]["id"], "fail")
        self.assertEqual(result["failed_step"]["name"], "fail step")
        self.assertEqual([step["index"] for step in result["steps"]], [0, 1])
        self.assertEqual(result["steps"][1]["exit_code"], 3)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "order.txt").read_text(), "one\n")
        self.assertFalse((workspace / "order.txt").read_text().endswith("later\n"))

    def test_unresolvable_digest_is_setup_failure(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                _plan(SUCCESS),
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("will not resolve", str(raised.exception))
        self.assertFalse(any(call and call[0] == "create" for call in self._calls()))

    def test_step_timeout_argument_stops_before_the_job_deadline(self):
        workflow = """\
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
        root = self.root / "step-timeout"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        started = time.monotonic()
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker="docker",
            step_timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["steps"][0]["error"], "step timed out")
        self.assertEqual([step["id"] for step in result["steps"]], ["sleep"])
        self.assertNotEqual(result["status"], "succeeded")
        # 2s step ceiling plus the 7.5s + 2.5s cancellation grace, with slack.
        self.assertLess(elapsed, 20)
        self.assertGreater(elapsed, 1)
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.stdout.strip(), "")
