"""Terminal cancel: version 0, then the record the worker returned.

A queued cancel renders cancelled. A finished run stays succeeded
only with exit code 0. A lost record shows its cleanup. Stopping
the worker while the view is open shows the socket is absent, and
starting it again shows the same run. Every action has a key and a
mouse target. The view does not submit.
"""

from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import time
import unittest

from execution_core.cli import call
from execution_core.dashboard import BINDINGS, Dashboard
from execution_core.protocol import canonical
from execution_core.worker import Worker


def _body(text):
    return "\n".join(
        line
        for line in text.splitlines()
        if "Run state as of " not in line and "This snapshot is stale after " not in line
    )


def _line(board, token):
    for index, line in enumerate(board.render().splitlines()):
        if token in line:
            return index
    raise AssertionError(f"{token} is not on the screen")


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dashboard-cancel-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.worker = None
        self.thread = None
        self._start_worker()
        self.queued = self._submit("queued")
        self.done = self._rewrite(
            self._submit("done"),
            state="succeeded",
            exit_code=0,
            cleanup="confirmed_no_external_resources",
        )
        self.bad = self._rewrite(
            self._submit("bad"),
            state="succeeded",
            exit_code=1,
            cleanup="confirmed_no_external_resources",
        )
        self.lost = self._rewrite(
            self._submit("lost"),
            state="lost",
            exit_code=None,
            cleanup="unresolved",
            error={
                "kind": "WORKER_INTERRUPTED",
                "message": "owned container cleanup was not confirmed",
            },
        )

    def tearDown(self):
        self._stop_worker()
        self.tmp.cleanup()

    def _start_worker(self):
        self.worker = Worker(self.repo, self.state)
        self.worker.execute_queue = lambda: self.worker.stop.wait()
        self.thread = threading.Thread(target=self.worker.serve, daemon=True)
        self.thread.start()
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

    def _stop_worker(self):
        if self.worker is None:
            return
        self.worker.stop.set()
        server = self.worker.server
        if server is not None:
            server.close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.worker = None
        self.thread = None

    def _submit(self, key):
        reply = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": key,
                "backend": "development",
                "fixture": {"exit_code": 0, "output": key},
            },
        )
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["state"], "queued")
        return reply["result"]

    def _rewrite(self, record, **fields):
        database = sqlite3.connect(self.state / "runs.sqlite3", timeout=5)
        try:
            row = database.execute(
                "SELECT record FROM runs WHERE id=?", (record["run_id"],)
            ).fetchone()
            stored = json.loads(row[0])
            stored.update(fields)
            database.execute(
                "UPDATE runs SET record=? WHERE id=?",
                (canonical(stored), record["run_id"]),
            )
            database.commit()
        finally:
            database.close()
        stored = call(self.state, "run.get", {"run_id": record["run_id"]})["result"]
        self.assertEqual(stored["state"], fields["state"])
        return stored

    def _select(self, board, run_id):
        for _ in range(len(board.runs)):
            if board.runs[board.selected]["run_id"] == run_id:
                return
            board.press("j")
        self.fail(f"{run_id} is not on the queue")

    def test_queued_cancel_sends_version_0_and_matches_the_mouse(self):
        import execution_core.cli as cli

        seen = []
        original = cli.call

        def wrapped(state, method, params):
            seen.append((method, dict(params)))
            return original(state, method, params)

        cli.call = wrapped
        try:
            left = Dashboard(str(self.state))
            right = Dashboard(str(self.state))
            left.press("c")
            right.click(_line(right, "[cancel]"))
        finally:
            cli.call = original
        cancels = [params for method, params in seen if method == "run.cancel"]
        self.assertEqual(
            cancels,
            [
                {"version": 0, "run_id": self.queued["run_id"]},
                {"version": 0, "run_id": self.queued["run_id"]},
            ],
        )
        self.assertNotIn("run.submit", [method for method, _params in seen])
        self.assertEqual(_body(left.render()), _body(right.render()))
        text = left.render()
        self.assertIn("state cancelled", text)
        self.assertIn("result cancelled", text)
        self.assertNotIn("result succeeded", text)
        stored = call(self.state, "run.get", {"run_id": self.queued["run_id"]})["result"]
        self.assertEqual(stored["state"], "cancelled")

    def test_finished_cancel_stays_succeeded_only_with_exit_code_0(self):
        bad = Dashboard(str(self.state))
        self._select(bad, self.bad["run_id"])
        bad.press("enter")
        bad_text = bad.render()
        self.assertIn("state succeeded", bad_text)
        self.assertIn("exit_code 1", bad_text)
        self.assertIn("result not succeeded", bad_text)
        self.assertNotIn("result succeeded", bad_text)

        left = Dashboard(str(self.state))
        right = Dashboard(str(self.state))
        self._select(left, self.done["run_id"])
        self._select(right, self.done["run_id"])
        left.press("enter")
        right.press("enter")
        left.press("c")
        right.click(_line(right, "[cancel]"))
        self.assertEqual(_body(left.render()), _body(right.render()))
        text = left.render()
        self.assertIn("state succeeded", text)
        self.assertIn("exit_code 0", text)
        self.assertIn("result succeeded", text)
        stored = call(self.state, "run.get", {"run_id": self.done["run_id"]})["result"]
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["exit_code"], 0)

    def test_lost_cancel_shows_cleanup_unresolved(self):
        board = Dashboard(str(self.state))
        self._select(board, self.lost["run_id"])
        board.press("c")
        text = board.render()
        self.assertIn("state lost", text)
        self.assertIn("cleanup unresolved", text)
        self.assertIn("error WORKER_INTERRUPTED", text)
        self.assertNotIn("result succeeded", text)
        stored = call(self.state, "run.get", {"run_id": self.lost["run_id"]})["result"]
        self.assertEqual(stored["state"], "lost")
        self.assertEqual(stored["cleanup"], "unresolved")

    def test_restart_shows_the_same_run(self):
        board = Dashboard(str(self.state))
        self.assertIn(f"{self.queued['run_id']} queued", board.render())
        self._stop_worker()
        board.press("enter")
        absent = board.render()
        self.assertIn("worker socket is not available", absent)
        self.assertNotIn("worker.sock", absent)
        self.assertNotIn(str(self.state), absent)
        self._start_worker()
        board.press("j")
        text = board.render()
        self.assertIn(f"{self.queued['run_id']} queued", text)
        self.assertNotIn("worker socket is not available", text)
        stored = call(self.state, "run.get", {"run_id": self.queued["run_id"]})["result"]
        self.assertEqual(stored["state"], "queued")
        self.assertEqual(stored["run_id"], self.queued["run_id"])

    def test_every_action_has_a_key_and_the_view_does_not_submit(self):
        import execution_core.dashboard as dashboard

        source = Path(dashboard.__file__).read_text()
        self.assertNotIn("run.submit", source)
        self.assertNotIn("run.status", source)
        actions = set()
        for screen, action, keys, token in BINDINGS:
            self.assertTrue(keys)
            self.assertTrue(token.startswith("[") and token.endswith("]"))
            actions.add((screen, action, keys[0], token))
        self.assertIn(("queue", "cancel", "c", "[cancel]"), actions)
        self.assertIn(("detail", "cancel", "c", "[cancel]"), actions)
        self.assertNotIn("submit", {action for _screen, action, _key, _token in actions})
        board = Dashboard(str(self.state))
        self.assertIn("[cancel] c", board.render())
        self.assertNotIn("[submit]", board.render())


if __name__ == "__main__":
    unittest.main()
