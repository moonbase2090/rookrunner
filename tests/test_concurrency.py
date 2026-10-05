import subprocess
import uuid
from pathlib import Path
import tempfile
import unittest

from execution_core.concurrency import (
    MAX_PENDING,
    decide,
    eligible,
    resolve_groups,
)
from execution_core.expr import ExprError
from execution_core.worker import Worker, now
from schema_support import validate_response


IMAGE = "sha256:" + "cd" * 32


def _run(run_id, state, group, queue="single", cancel=False):
    return {
        "run_id": run_id,
        "state": state,
        "concurrency": [{"group": group, "cancel_in_progress": cancel, "queue": queue}],
    }


class ConcurrencyPolicyTests(unittest.TestCase):
    def test_single_queue_replaces_a_pending_run_and_keeps_another_group(self):
        older = _run("older", "queued", "Deploy")
        other = _run("other", "queued", "docs")
        new = _run("new", "queued", "deploy")
        cancel, reject = decide([older, other], new)
        self.assertFalse(reject)
        self.assertEqual(cancel, {"older"})

    def test_cancel_in_progress_selects_the_running_run(self):
        running = _run("running", "running", "deploy")
        pending = _run("pending", "queued", "deploy")
        new = _run("new", "queued", "deploy", cancel=True)
        cancel, reject = decide([running, pending], new)
        self.assertFalse(reject)
        self.assertEqual(cancel, {"running", "pending"})

    def test_queue_max_keeps_one_hundred_pending_runs(self):
        pending = [
            _run(f"p{index}", "queued", "deploy", queue="max") for index in range(MAX_PENDING)
        ]
        new = _run("new", "queued", "deploy", queue="max")
        cancel, reject = decide(pending, new)
        self.assertTrue(reject)
        self.assertEqual(cancel, set())
        room = pending[:-1]
        cancel, reject = decide(room, new)
        self.assertFalse(reject)
        self.assertEqual(cancel, set())

    def test_a_running_group_blocks_only_that_group(self):
        running = _run("running", "running", "deploy")
        waiting = _run("waiting", "queued", "deploy")
        other = _run("other", "queued", "docs")
        self.assertEqual(eligible([waiting, other], [running])["run_id"], "other")
        self.assertIsNone(eligible([waiting], [running]))

    def test_group_text_uses_the_event_ref(self):
        plan = {
            "workflow": {
                "name": "demo",
                "concurrency": {
                    "group": "ci-${{ github.workflow }}-${{ github.ref }}",
                    "cancel_in_progress": "${{ github.event_name == 'pull_request' }}",
                    "queue": "single",
                },
            },
            "job": {},
        }
        groups = resolve_groups(
            plan,
            {"ref": "refs/heads/main"},
            "push",
            ".github/workflows/ci.yml",
        )
        self.assertEqual(
            groups,
            [
                {
                    "group": "ci-demo-refs/heads/main",
                    "cancel_in_progress": False,
                    "queue": "single",
                }
            ],
        )
        pull = resolve_groups(
            plan,
            {"ref": "refs/pull/7/merge"},
            "pull_request",
            ".github/workflows/ci.yml",
        )
        self.assertTrue(pull[0]["cancel_in_progress"])
        self.assertEqual(pull[0]["group"], "ci-demo-refs/pull/7/merge")

    def test_queue_max_with_a_true_flag_is_rejected(self):
        plan = {
            "workflow": {
                "concurrency": {
                    "group": "deploy",
                    "cancel_in_progress": "${{ github.event_name == 'push' }}",
                    "queue": "max",
                }
            },
            "job": {},
        }
        with self.assertRaises(ExprError) as raised:
            resolve_groups(plan, {"ref": "refs/heads/main"}, "push", "ci.yml")
        self.assertIn("queue max", str(raised.exception))


class ConcurrencySubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="concurrency-submit-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self._write(
            "name: demo\n"
            "on: push\n"
            "concurrency:\n"
            "  group: ci-${{ github.ref }}\n"
            "  cancel-in-progress: false\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo hi\n"
        )
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
        self.worker = Worker(self.repo, self.state)
        self.worker.start()

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def _write(self, text):
        path = self.repo / ".github" / "workflows" / "test.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _params(self, key, ref):
        return {
            "version": 1,
            "submission_key": key,
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": {"ref": ref},
            "image": IMAGE,
        }

    def _cancel(self, run_id):
        self.worker.dispatch("run.cancel", {"version": 0, "run_id": run_id})

    def test_a_newer_run_in_the_group_cancels_the_queued_one(self):
        with self.worker.guard:
            try:
                first = self.worker.dispatch("run.submit", self._params("one", "refs/heads/main"))
                second = self.worker.dispatch("run.submit", self._params("two", "refs/heads/main"))
                other = self.worker.dispatch("run.submit", self._params("three", "refs/heads/dev"))
                self.assertEqual(self.worker.get(first["run_id"])["state"], "cancelled")
                self.assertEqual(second["state"], "queued")
                self.assertEqual(
                    second["concurrency"],
                    [
                        {
                            "group": "ci-refs/heads/main",
                            "cancel_in_progress": False,
                            "queue": "single",
                        }
                    ],
                )
                self.assertEqual(other["state"], "queued")
                self.assertEqual(other["concurrency"][0]["group"], "ci-refs/heads/dev")
                validate_response(
                    "run.submit",
                    {"jsonrpc": "2.0", "id": 1, "result": second},
                )
            finally:
                for run_id in (
                    first.get("run_id") if "first" in locals() else None,
                    second.get("run_id") if "second" in locals() else None,
                    other.get("run_id") if "other" in locals() else None,
                ):
                    if run_id:
                        self._cancel(run_id)

    def test_cancel_in_progress_names_the_running_run(self):
        self._write(
            "name: demo\n"
            "on: push\n"
            "concurrency:\n"
            "  group: ci-${{ github.ref }}\n"
            "  cancel-in-progress: true\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo hi\n"
        )
        self._git("add", ".")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "cancel",
        )
        with self.worker.guard:
            first = self.worker.dispatch("run.submit", self._params("run-a", "refs/heads/main"))
            current = self.worker.get(first["run_id"])
            current.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
            with self.worker.db:
                self.worker.save(current)
            try:
                second = self.worker.dispatch(
                    "run.submit", self._params("run-b", "refs/heads/main")
                )
                self.assertEqual(self.worker._pending_concurrency_cancels, [first["run_id"]])
                self.assertEqual(second["state"], "queued")
                self.assertTrue(second["concurrency"][0]["cancel_in_progress"])
            finally:
                self.worker._pending_concurrency_cancels.clear()
                current = self.worker.get(first["run_id"])
                current.update(
                    state="cancelled",
                    cancel_requested=True,
                    finished_at=now(),
                    cleanup="confirmed_no_external_resources",
                )
                with self.worker.db:
                    self.worker.save(current)
                if "second" in locals():
                    self._cancel(second["run_id"])

    def test_queue_max_keeps_both_queued_runs(self):
        self._write(
            "name: demo\n"
            "on: push\n"
            "concurrency:\n"
            "  group: deploy\n"
            "  queue: max\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo hi\n"
        )
        self._git("add", ".")
        self._git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "queue",
        )
        with self.worker.guard:
            try:
                first = self.worker.dispatch("run.submit", self._params("max-a", "refs/heads/main"))
                second = self.worker.dispatch(
                    "run.submit", self._params("max-b", "refs/heads/main")
                )
                self.assertEqual(first["state"], "queued")
                self.assertEqual(self.worker.get(first["run_id"])["state"], "queued")
                self.assertEqual(second["state"], "queued")
                self.assertEqual(second["concurrency"][0]["queue"], "max")
            finally:
                for run_id in (
                    first.get("run_id") if "first" in locals() else None,
                    second.get("run_id") if "second" in locals() else None,
                ):
                    if run_id:
                        self._cancel(run_id)
