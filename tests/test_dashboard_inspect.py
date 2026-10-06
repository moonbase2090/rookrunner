"""Terminal inspect view: snapshot, steps, log pages, and artifacts.

Every binding has a key and a mouse target. Reopening the view
shows the same run. dashboard-html is not part of this view.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from execution_core.cli import call
from execution_core.dashboard import BINDINGS, Dashboard, apply_input, decode_input
from execution_core.protocol import MAX_LOG_PAGE
from execution_core.worker import Worker, now

IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
SENTINEL = b"RR-SENTINEL-m3-inspect"
OTHER = b"RR-OTHER-m3-inspect"
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


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


def _git_workflow(repo):
    workflow = repo / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(WORKFLOW)
    (repo / "source.txt").write_text("original\n")
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


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dashboard-inspect-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        _git_workflow(self.repo)
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
                break
            except (OSError, ValueError):
                time.sleep(0.01)
        else:
            self.fail("worker did not become ready")
        self.queued = self._fixture("queued")
        self.workflow = self._workflow()

    def tearDown(self):
        self.worker.stop.set()
        server = self.worker.server
        if server is not None:
            server.close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _fixture(self, key):
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
        return reply["result"]

    def _workflow(self):
        reply = call(
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
        self.assertNotIn("error", reply)
        record = reply["result"]
        attempt_id = str(uuid.uuid4())
        snapshot = self.state / "snapshots" / record["input"]["snapshot_id"]
        workspace = self.state / "attempts" / attempt_id / "workspace"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(snapshot / "files", workspace, symlinks=True)
        (workspace / "out").mkdir()
        (workspace / "out" / "a-sentinel.txt").write_bytes(SENTINEL)
        (workspace / "out" / "b-other.txt").write_bytes(OTHER)
        head = "LOG-HEAD\n"
        tail = "LOG-TAIL\n"
        stdout = head + ("x" * (MAX_LOG_PAGE - len(head.encode()))) + tail
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
                    "steps": [
                        {
                            "index": 1,
                            "id": "step",
                            "name": "echo hi",
                            "status": "succeeded",
                            "exit_code": 0,
                            "stdout": stdout,
                            "stderr": "",
                        }
                    ],
                },
            )
        stored = call(self.state, "run.get", {"run_id": record["run_id"]})["result"]
        self.assertEqual(stored["state"], "succeeded")
        return stored

    def _open_workflow(self, board):
        self.assertEqual(board.runs[1]["run_id"], self.workflow["run_id"])
        board.press("j")
        board.press("enter")

    def _pair(self):
        return Dashboard(str(self.state)), Dashboard(str(self.state))

    def _same(self, left, right):
        self.assertEqual(_body(left.render()), _body(right.render()))

    def test_bindings_pair_every_key_with_a_mouse_target(self):
        seen = set()
        for screen, _action, keys, token in BINDINGS:
            self.assertTrue(keys)
            self.assertTrue(token.startswith("[") and token.endswith("]"))
            seen.add((screen, keys[0]))
        self.assertIn(("queue", "j"), seen)
        self.assertIn(("queue", "enter"), seen)
        self.assertIn(("detail", "n"), seen)
        self.assertIn(("detail", "b"), seen)
        queue, _other = self._pair()
        text = queue.render()
        for screen, _action, _keys, token in BINDINGS:
            if screen == "queue":
                self.assertIn(token, text)
        opened, _again = self._pair()
        self._open_workflow(opened)
        detail = opened.render()
        self.assertIn("[log-next]", detail)
        self.assertIn("[back]", detail)
        self.assertIn("[next]", detail)
        self.assertNotIn("[log-prev]", detail)
        opened.press("n")
        self.assertIn("[log-prev]", opened.render())

    def test_mouse_matches_keys_for_every_action(self):
        left, right = self._pair()
        left.press("j")
        right.click(_line(right, "[next]"))
        self._same(left, right)
        self.assertIn(f"> [run] {self.workflow['run_id']} succeeded", left.render())

        left.press("k")
        right.click(_line(right, "[previous]"))
        self._same(left, right)
        self.assertIn(f"> [run] {self.queued['run_id']} queued", left.render())

        left.press("j")
        right.click(_line(right, "[next]"))
        left.press("enter")
        right.click(_line(right, "[open]"))
        self._same(left, right)
        self._assert_workflow_detail(left.render(), first_page=True)

        left.press("n")
        right.click(_line(right, "[log-next]"))
        self._same(left, right)
        self._assert_workflow_detail(left.render(), first_page=False)

        left.press("p")
        right.click(_line(right, "[log-prev]"))
        self._same(left, right)
        self._assert_workflow_detail(left.render(), first_page=True)

        left.press("j")
        right.click(_line(right, "[next]"))
        self._same(left, right)
        self.assertIn(OTHER.decode(), left.render())
        self.assertNotIn(SENTINEL.decode(), left.render())

        left.press("k")
        right.click(_line(right, "[previous]"))
        self._same(left, right)
        self.assertIn(SENTINEL.decode(), left.render())

        left.press("down")
        right.press("j")
        self._same(left, right)

        left.press("b")
        right.click(_line(right, "[back]"))
        self._same(left, right)
        self.assertIn(f"> [run] {self.workflow['run_id']} succeeded", left.render())

        left.press("q")
        right.click(_line(right, "[quit]"))
        self.assertTrue(left.quit)
        self.assertTrue(right.quit)

    def test_key_bytes_and_sgr_clicks_drive_the_same_actions(self):
        left, right = self._pair()
        self.assertEqual(decode_input(b"j")[0], [("key", "j")])
        self.assertEqual(decode_input(b"\x1b[B")[0], [("key", "down")])
        self.assertEqual(decode_input(b"\r")[0], [("key", "enter")])
        apply_input(left, b"j")
        row = _line(right, "[next]") + 1
        encoded = f"\x1b[<0;1;{row}M".encode()
        self.assertEqual(decode_input(encoded)[0], [("click", row - 1)])
        apply_input(right, encoded)
        self._same(left, right)
        third = Dashboard(str(self.state))
        apply_input(third, b"\x1b[B")
        self._same(left, third)

    def test_reopening_shows_the_same_run(self):
        command = [
            sys.executable,
            "-m",
            "execution_core",
            "--state",
            str(self.state),
            "dashboard",
        ]
        first = subprocess.run(command, capture_output=True, text=True, check=False)
        second = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        marker = f"{self.workflow['run_id']} succeeded"
        self.assertIn(marker, first.stdout)
        self.assertIn(marker, second.stdout)
        self.assertNotIn("\x1b", first.stdout)

    def _assert_workflow_detail(self, text, first_page):
        self.assertIn(f"run {self.workflow['run_id']}", text)
        self.assertIn("state succeeded", text)
        self.assertIn(f"snapshot {self.workflow['input']['snapshot_id']}", text)
        self.assertIn("1 echo hi succeeded", text)
        self.assertIn("bytes are not masked", text)
        self.assertIn("out/a-sentinel.txt", text)
        if first_page:
            self.assertIn("LOG-HEAD", text)
            self.assertNotIn("LOG-TAIL", text)
            self.assertIn(SENTINEL.decode(), text)
        else:
            self.assertIn("LOG-TAIL", text)
            self.assertNotIn("LOG-HEAD", text)


if __name__ == "__main__":
    unittest.main()
