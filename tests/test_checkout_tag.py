import hashlib
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import zlib
from unittest.mock import patch

from execution_core.attempt import AttemptError, materialize_attempt
from execution_core.plan import (
    CAPABILITY_VERSION,
    PlanError,
    plan_needs_history,
    plan_snapshot,
    plan_workflow,
)
from execution_core.run import _accept_jobs, _consider_step
from execution_core.snapshot import CaptureError, SourceCapture
from execution_core.worker import Worker


IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
CHECK_SHA = "11d5960a326750d5838078e36cf38b85af677262"


def _git(repo, *args, input=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        check=True,
        capture_output=True,
    )


def _commit(repo, message):
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        message,
    )


def _init(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")


def _workflow(uses, with_text=""):
    body = f"on: push\njobs:\n  build:\n    steps:\n      - uses: {uses}\n"
    if with_text:
        body += f"        with:\n{with_text}"
    return body


def _write(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _loose_commits(store):
    found = []
    for bucket in store.iterdir():
        for leaf in bucket.iterdir():
            raw = zlib.decompress(leaf.read_bytes())
            header, payload = raw.split(b"\0", 1)
            kind, _size = header.split(b" ", 1)
            if kind == b"commit":
                found.append((bucket.name + leaf.name, payload))
    return found


def _git_log(workspace):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(workspace),
            "log",
            "--format=%s",
        ],
        check=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    return result.stdout.decode("utf-8").splitlines()


class _NoFetch:
    def __init__(self):
        self.calls = []
        self._real = subprocess.run

    def __call__(self, args, **kwargs):
        argv = list(args) if isinstance(args, list | tuple) else [args]
        self.calls.append(argv)
        if any(part in {"fetch", "clone", "unshallow"} for part in argv):
            raise AssertionError(argv)
        return self._real(args, **kwargs)

    @property
    def commands(self):
        return [call[call.index("-C") + 2] if "-C" in call else call[0] for call in self.calls]


class CheckoutTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkout-tag-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def reject(self, workflow, job_id="build"):
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), job_id)
        return raised.exception

    def test_major_tags_plan_as_owned_checkout_and_the_step_starts_nothing(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        for uses in (
            "actions/checkout@v4",
            "actions/checkout@v5",
            "actions/checkout@v3",
            "actions/checkout@v9",
            "actions/checkout@v10",
        ):
            workflow = _workflow(uses)
            plan = plan_workflow(workflow.encode(), "build")["plan"]
            step = plan["job"]["steps"][0]
            with self.subTest(uses=uses):
                self.assertEqual(plan["capability_version"], 12)
                self.assertEqual(step["uses"], uses)
                self.assertEqual(step["checkout"], "captured")
                self.assertNotIn("with", step)
                self.assertNotIn("action_path", step)
        repo = self.root / "dirty"
        state = self.root / "dirty-state"
        _init(repo)
        _write(repo, ".github/workflows/test.yml", _workflow("actions/checkout@v5"))
        _write(repo, "source.txt", "original\n")
        _git(repo, "add", ".")
        _commit(repo, "fixture")
        (repo / "source.txt").write_text("dirty-bytes\n")
        captured = SourceCapture(repo, state).capture(".github/workflows/test.yml")
        snapshot = state / "snapshots" / captured["snapshot_id"]
        workspace = self.root / "dirty-workspace"
        materialize_attempt(snapshot, captured["digest"], workspace)
        planned = plan_snapshot(snapshot, "build")["plan"]
        step = planned["job"]["steps"][0]
        self.assertEqual(step["uses"], "actions/checkout@v5")
        self.assertEqual(step["checkout"], "captured")

        def refuse_socket(*_args, **_kwargs):
            raise AssertionError("socket")

        def refuse_process(*_args, **_kwargs):
            raise AssertionError("process")

        with (
            patch("socket.socket", refuse_socket),
            patch("execution_core.run.subprocess.run", refuse_process),
        ):
            result = _consider_step(
                "docker",
                "unused",
                step,
                planned["workflow"],
                planned["job"],
                EVENT,
                workspace,
                True,
                None,
                time.monotonic() + 60,
                [],
                False,
                {},
                None,
                None,
                None,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "source.txt").read_text(), "dirty-bytes\n")
        self.assertEqual((repo / "source.txt").read_text(), "dirty-bytes\n")
        self.assertFalse(hasattr(socket, "_checkout_tag_connected"))

    def test_v4_without_fetch_depth_keeps_one_parentless_commit(self):
        repo = self.root / "plain"
        state = self.root / "plain-state"
        _init(repo)
        workflow = _workflow("actions/checkout@v4")
        _write(repo, ".github/workflows/test.yml", workflow)
        _write(repo, "source.txt", "original\n")
        _git(repo, "add", ".")
        _commit(repo, "fixture")
        capture = SourceCapture(repo, state)
        direct = capture.capture(".github/workflows/test.yml")
        with capture.prepare(".github/workflows/test.yml") as prepared:
            planned = plan_snapshot(prepared.path, "build")
            self.assertFalse(plan_needs_history(planned["plan"]))
            staged = prepared.publish()
        self.assertEqual(staged["git_objects_digest"], direct["git_objects_digest"])
        self.assertEqual(planned["plan"]["capability_version"], 12)
        step = planned["plan"]["job"]["steps"][0]
        self.assertEqual(step["uses"], "actions/checkout@v4")
        self.assertNotIn("with", step)
        for result in (direct, staged):
            store = state / "snapshots" / result["snapshot_id"] / "objects"
            commits = _loose_commits(store)
            self.assertEqual(len(commits), 1)
            _oid, payload = commits[0]
            self.assertNotIn(b"parent ", payload)
            self.assertIn(b"captured tree\n", payload)
            self.assertNotIn(b"Fixture", payload)
        direct_capture = SourceCapture(repo, self.root / "direct-state")
        _write(
            repo,
            ".github/workflows/test.yml",
            _workflow("actions/checkout@v4", "          fetch-depth: 0\n"),
        )
        _git(repo, "add", ".github/workflows/test.yml")
        _commit(repo, "ask")
        asked = direct_capture.capture(".github/workflows/test.yml")
        asked_commits = _loose_commits(
            self.root / "direct-state" / "snapshots" / asked["snapshot_id"] / "objects"
        )
        self.assertEqual(len(asked_commits), 1)
        self.assertNotIn(b"parent ", asked_commits[0][1])

    def test_dotted_uppercase_and_bare_checkout_name_the_uses_field(self):
        for uses in (
            "actions/checkout@v4.2.2",
            "actions/checkout@V5",
            "actions/checkout",
            "actions/checkout@v",
        ):
            error = self.reject(_workflow(uses))
            with self.subTest(uses=uses):
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, "jobs.build.steps.0.uses")

    def test_checkout_inside_a_local_composite_names_the_uses_field(self):
        workflow = "on: push\njobs:\n  build:\n    steps:\n      - uses: ./.github/actions/hello\n"
        action = (
            "name: Hello\n"
            "description: Checkout\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - uses: actions/checkout@v5\n"
        )
        root = self.root / "composite"
        files = root / "files"
        action_path = files / ".github" / "actions" / "hello" / "action.yml"
        action_path.parent.mkdir(parents=True)
        action_path.write_text(action)
        workflow_path = files / ".github" / "workflows" / "test.yml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(workflow)
        (root / "manifest.json").write_text('{"workflow": ".github/workflows/test.yml"}')
        with self.assertRaises(PlanError) as raised:
            plan_snapshot(root, "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.steps.0.uses.runs.steps.0.uses")

    def test_fetch_depth_zero_stores_ancestors_and_log_starts_at_the_synthesized_commit(self):
        repo = self.root / "history"
        state = self.root / "history-state"
        _init(repo)
        _write(repo, "source.txt", "one\n")
        _write(
            repo,
            ".github/workflows/test.yml",
            _workflow(
                "actions/checkout@v4",
                "          fetch-depth: 0\n          clean: false\n          persist-credentials: false\n",
            ),
        )
        _git(repo, "add", ".")
        _commit(repo, "first")
        first = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        _write(repo, "source.txt", "two\n")
        _git(repo, "add", "source.txt")
        _commit(repo, "second")
        second = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        (repo / "source.txt").write_text("dirty-bytes\n")
        capture = SourceCapture(repo, state)
        guard = _NoFetch()
        with patch("execution_core.snapshot.subprocess.run", guard):
            with capture.prepare(".github/workflows/test.yml") as prepared:
                planned = plan_snapshot(prepared.path, "build")
                self.assertTrue(plan_needs_history(planned["plan"]))
                self.assertEqual(planned["plan"]["capability_version"], 12)
                step = planned["plan"]["job"]["steps"][0]
                self.assertEqual(
                    step["with"],
                    {"fetch-depth": 0, "clean": False, "persist-credentials": False},
                )
                _accept_jobs(planned["plan"]["jobs"])
                prepared.add_history()
                captured = prepared.publish()
        self.assertIn("rev-list", guard.commands)
        self.assertIn("cat-file", guard.commands)
        self.assertNotIn("fetch", guard.commands)
        snapshot = state / "snapshots" / captured["snapshot_id"]
        commits = dict(_loose_commits(snapshot / "objects"))
        self.assertIn(second, commits)
        self.assertIn(first, commits)
        self.assertIn(b"Fixture", commits[second])
        self.assertIn(b"fixture@example.invalid", commits[second])
        synthesized = [
            oid
            for oid, payload in commits.items()
            if payload.startswith(b"tree ") and b"\ncaptured tree\n" in payload
        ]
        self.assertEqual(len(synthesized), 1)
        self.assertIn(f"parent {second}\n".encode(), commits[synthesized[0]])
        self.assertNotIn(b"author Rookrunner", commits[second])
        for bucket in (snapshot / "objects").iterdir():
            self.assertEqual(stat.S_IMODE(bucket.stat().st_mode), 0o700)
            for leaf in bucket.iterdir():
                self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o600)
        self.assertFalse((snapshot / "objects" / "pack").exists())
        workspace = self.root / "history-workspace"
        materialize_attempt(snapshot, captured["digest"], workspace)
        self.assertEqual((workspace / "source.txt").read_text(), "dirty-bytes\n")
        head = (workspace / ".git" / "refs" / "heads" / "main").read_text().strip()
        self.assertEqual(head, synthesized[0])
        self.assertNotEqual(head, second)
        self.assertEqual(_git_log(workspace), ["captured tree", "second", "first"])
        self.assertFalse((workspace / ".git" / "refs" / "tags").exists())
        self.assertNotIn("remote", (workspace / ".git" / "config").read_text())
        copy = self.root / "extra-snapshot"
        shutil.copytree(snapshot, copy)
        extra = (
            b"tree "
            + commits[synthesized[0]].split(b"\n", 1)[0][len(b"tree ") :]
            + (
                b"\nauthor Extra <extra@example.invalid> 0 +0000\n"
                b"committer Extra <extra@example.invalid> 0 +0000\n"
                b"\nextra\n"
            )
        )
        algorithm = "sha1" if len(second) == 40 else "sha256"
        raw = f"commit {len(extra)}\0".encode("ascii") + extra
        oid = hashlib.new(algorithm, raw).hexdigest()
        bucket = copy / "objects" / oid[:2]
        bucket.mkdir(mode=0o700, exist_ok=True)
        (bucket / oid[2:]).write_bytes(zlib.compress(raw))
        rejected = self.root / "extra-workspace"
        with self.assertRaises(AttemptError) as raised:
            materialize_attempt(copy, captured["digest"], rejected)
        self.assertEqual(raised.exception.kind, "ATTEMPT_FAILED")
        self.assertFalse(rejected.exists())

    def test_a_merge_stores_every_parent(self):
        repo = self.root / "merge"
        state = self.root / "merge-state"
        _init(repo)
        _write(
            repo,
            ".github/workflows/test.yml",
            _workflow("actions/checkout@v5", "          fetch-depth: 0\n"),
        )
        _write(repo, "source.txt", "base\n")
        _git(repo, "add", ".")
        _commit(repo, "base")
        _git(repo, "branch", "side")
        _write(repo, "source.txt", "main\n")
        _git(repo, "add", "source.txt")
        _commit(repo, "main-side")
        _git(repo, "checkout", "side")
        _write(repo, "side.txt", "side\n")
        _git(repo, "add", "side.txt")
        _commit(repo, "topic")
        _git(repo, "checkout", "main")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "merge",
            "--no-ff",
            "-m",
            "merge",
            "side",
        )
        left = _git(repo, "rev-parse", "HEAD^1").stdout.decode().strip()
        right = _git(repo, "rev-parse", "HEAD^2").stdout.decode().strip()
        tip = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        capture = SourceCapture(repo, state)
        with capture.prepare(".github/workflows/test.yml") as prepared:
            self.assertTrue(plan_needs_history(plan_snapshot(prepared.path, "build")["plan"]))
            prepared.add_history()
            captured = prepared.publish()
        commits = dict(_loose_commits(state / "snapshots" / captured["snapshot_id"] / "objects"))
        self.assertIn(tip, commits)
        self.assertIn(left, commits)
        self.assertIn(right, commits)
        self.assertIn(f"parent {left}\n".encode(), commits[tip])
        self.assertIn(f"parent {right}\n".encode(), commits[tip])

    def test_history_follows_a_needed_job_a_false_if_and_a_called_workflow(self):
        needed = """\
on: push
jobs:
  first:
    steps:
      - if: false
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
  build:
    needs: first
    steps:
      - run: echo hi
"""
        self.assertTrue(plan_needs_history(plan_workflow(needed.encode(), "build")["plan"]))
        absent = "on: push\njobs:\n  build:\n    steps:\n      - uses: actions/checkout@v5\n"
        self.assertFalse(plan_needs_history(plan_workflow(absent.encode(), "build")["plan"]))
        caller = "on: push\njobs:\n  build:\n    uses: ./.github/workflows/called.yml\n"
        called = """\
on: workflow_call
jobs:
  inner:
    steps:
      - uses: actions/checkout@v10
        with:
          fetch-depth: 0
"""
        root = self.root / "called"
        files = root / "files"
        called_path = files / ".github" / "workflows" / "called.yml"
        called_path.parent.mkdir(parents=True)
        called_path.write_text(called)
        workflow_path = files / ".github" / "workflows" / "test.yml"
        workflow_path.write_text(caller)
        (root / "manifest.json").write_text('{"workflow": ".github/workflows/test.yml"}')
        self.assertTrue(plan_needs_history(plan_snapshot(root, "build")["plan"]))

    def test_fetch_depth_one_is_unsupported_and_a_string_zero_is_invalid(self):
        unsupported = self.reject(_workflow("actions/checkout@v4", "          fetch-depth: 1\n"))
        self.assertEqual(unsupported.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(unsupported.field, "jobs.build.steps.0.with.fetch-depth")
        for value in ('"0"', "'0'", "false", "0.0", "${{ 0 }}"):
            error = self.reject(
                _workflow("actions/checkout@v5", f"          fetch-depth: {value}\n")
            )
            with self.subTest(value=value):
                self.assertEqual(error.kind, "WORKFLOW_INVALID")
                self.assertEqual(error.field, "jobs.build.steps.0.with.fetch-depth")
                self.assertIn("must be an integer", str(error))

    def test_unborn_history_is_source_invalid_and_a_plan_error_wins(self):
        repo = self.root / "unborn"
        state = self.root / "unborn-state"
        _init(repo)
        _write(repo, "workflow.yml", _workflow("actions/checkout@v4", "          fetch-depth: 0\n"))
        _git(repo, "add", "workflow.yml")
        capture = SourceCapture(repo, state)
        with capture.prepare("workflow.yml") as prepared:
            planned = plan_snapshot(prepared.path, "build")
            self.assertTrue(plan_needs_history(planned["plan"]))
            with self.assertRaises(CaptureError) as raised:
                prepared.add_history()
        self.assertEqual(raised.exception.kind, "SOURCE_INVALID")
        snaps = state / "snapshots"
        self.assertEqual(list(snaps.iterdir()), [])
        _write(repo, "workflow.yml", _workflow("actions/checkout@v4.2.2"))
        _git(repo, "add", "workflow.yml")
        with self.assertRaises(PlanError) as planned_error:
            with capture.prepare("workflow.yml") as prepared:
                plan_snapshot(prepared.path, "build")
        self.assertEqual(planned_error.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(list(snaps.iterdir()), [])

    def test_a_missing_parent_is_source_invalid_and_is_not_fetched(self):
        source = self.root / "source"
        _init(source)
        _write(
            source,
            ".github/workflows/test.yml",
            _workflow("actions/checkout@v4", "          fetch-depth: 0\n"),
        )
        _write(source, "source.txt", "one\n")
        _git(source, "add", ".")
        _commit(source, "first")
        _write(source, "source.txt", "two\n")
        _git(source, "add", "source.txt")
        _commit(source, "second")
        cloned = self.root / "shallow"
        _git(
            source,
            "clone",
            "--depth",
            "1",
            "--no-local",
            str(source),
            str(cloned),
        )
        self.assertEqual(
            _git(cloned, "rev-parse", "--is-shallow-repository").stdout.decode().strip(),
            "true",
        )
        state = self.root / "shallow-state"
        capture = SourceCapture(cloned, state)
        guard = _NoFetch()
        with patch("execution_core.snapshot.subprocess.run", guard):
            with self.assertRaises(CaptureError) as raised:
                with capture.prepare(".github/workflows/test.yml") as prepared:
                    self.assertTrue(
                        plan_needs_history(plan_snapshot(prepared.path, "build")["plan"])
                    )
                    prepared.add_history()
        self.assertEqual(raised.exception.kind, "SOURCE_INVALID")
        self.assertNotIn("fetch", guard.commands)
        self.assertNotIn("unshallow", guard.commands)
        snaps = state / "snapshots"
        self.assertEqual(list(snaps.iterdir()), [])

    def test_worker_publish_includes_history_and_permissions_still_fail(self):
        repo = self.root / "worker"
        state = self.root / "worker-state"
        _init(repo)
        _write(repo, "source.txt", "one\n")
        _write(
            repo,
            ".github/workflows/test.yml",
            _workflow("actions/checkout@v5", "          fetch-depth: 0\n"),
        )
        _git(repo, "add", ".")
        _commit(repo, "first")
        _write(repo, "source.txt", "two\n")
        _git(repo, "add", "source.txt")
        _commit(repo, "second")
        worker = Worker(repo, state)
        worker.execute_queue = lambda: worker.stop.wait()
        worker.start()
        try:
            guard = _NoFetch()
            with patch("execution_core.snapshot.subprocess.run", guard):
                record = worker.submit_workflow(
                    {
                        "version": 1,
                        "submission_key": "history",
                        "workflow": ".github/workflows/test.yml",
                        "job_id": "build",
                        "event": EVENT,
                        "image": IMAGE,
                    }
                )
            self.assertEqual(record["state"], "queued")
            self.assertNotIn("fetch", guard.commands)
            snapshot = state / "snapshots" / record["input"]["snapshot_id"]
            self.assertGreater(len(_loose_commits(snapshot / "objects")), 1)
            _write(
                repo,
                ".github/workflows/plain.yml",
                _workflow("actions/checkout@v4"),
            )
            _git(repo, "add", ".github/workflows/plain.yml")
            _commit(repo, "plain")
            plain = worker.submit_workflow(
                {
                    "version": 1,
                    "submission_key": "plain",
                    "workflow": ".github/workflows/plain.yml",
                    "job_id": "build",
                    "event": EVENT,
                    "image": IMAGE,
                }
            )
            plain_store = state / "snapshots" / plain["input"]["snapshot_id"] / "objects"
            self.assertEqual(len(_loose_commits(plain_store)), 1)
        finally:
            worker.close()
        for scope in ("security-events", "actions"):
            error = self.reject(
                "permissions:\n"
                f"  {scope}: write\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v5\n"
            )
            with self.subTest(scope=scope):
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, f"permissions.{scope}")
        check = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "check.yml"
        text = check.read_text()
        self.assertIn(f"uses: actions/checkout@{CHECK_SHA}", text)
        self.assertNotIn("fetch-depth", text)

    def test_rejected_fetch_depth_publishes_no_snapshot(self):
        repo = self.root / "reject"
        state = self.root / "reject-state"
        _init(repo)
        _write(
            repo,
            ".github/workflows/test.yml",
            _workflow("actions/checkout@v4", "          fetch-depth: 1\n"),
        )
        _git(repo, "add", ".")
        _commit(repo, "fixture")
        capture = SourceCapture(repo, state)
        with self.assertRaises(PlanError) as raised:
            with capture.prepare(".github/workflows/test.yml") as prepared:
                plan_snapshot(prepared.path, "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(list((state / "snapshots").iterdir()), [])
