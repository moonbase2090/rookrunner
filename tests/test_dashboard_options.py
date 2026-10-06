"""Two local dashboard options, both socket clients.

Each shows a queued run, a failed run, a lost run, a missing
socket, and a CAPABILITY_UNSUPPORTED error. The HTML command
writes one file and exits. Neither option binds a listener.
"""

from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from execution_core.cli import call
from execution_core.protocol import canonical
from execution_core.worker import Worker

_STAMP = re.compile(r"Run state as of (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\.")
_CONTROLS = ("<form", "<button", "<script", "<input", "<a ", "onclick", "worker.sock")


def _command(state, *args):
    return subprocess.run(
        [sys.executable, "-m", "execution_core", "--state", str(state), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _guarded(state, *args):
    code = """
import socket
import sys

class Guard(socket.socket):
    def bind(self, *args, **kwargs):
        raise SystemExit("bind")

    def listen(self, *args, **kwargs):
        raise SystemExit("listen")

socket.socket = Guard
from execution_core.cli import main
sys.argv = ["execution_core", *sys.argv[1:]]
main()
"""
    return subprocess.run(
        [sys.executable, "-c", code, "--state", str(state), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class DashboardOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dashboard-options-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
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
        self.queued = self._submit("queued")
        self.failed = self._rewrite(self._submit("failed"), state="failed", exit_code=1)
        self.lost = self._rewrite(
            self._submit("lost"),
            state="lost",
            exit_code=None,
            cleanup="unresolved",
        )

    def tearDown(self):
        self.worker.stop.set()
        server = self.worker.server
        if server is not None:
            server.close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

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

    def _assert_records(self, text):
        self.assertIn(f"{self.queued['run_id']} queued", text)
        self.assertIn(f"{self.failed['run_id']} failed", text)
        self.assertIn(f"{self.lost['run_id']} lost", text)
        self.assertIn(
            f"{self.queued['run_id']} CAPABILITY_UNSUPPORTED",
            text,
        )
        self.assertNotIn("worker.sock", text)
        stamp = _STAMP.search(text)
        self.assertIsNotNone(stamp)
        written = datetime.strptime(stamp.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLess(abs((datetime.now(timezone.utc) - written).total_seconds()), 60)
        self.assertIn(f"This snapshot is stale after {stamp.group(1)}.", text)

    def _assert_closed_socket(self, *args):
        absent = self.root / "absent"
        absent.mkdir(mode=0o700)
        completed = _command(absent, *args)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        text = completed.stdout
        if args and args[0] == "dashboard-html":
            text = Path(args[args.index("--output") + 1]).read_text()
        self.assertIn("worker socket is not available", text)
        self.assertNotIn(str(absent), text)
        self.assertNotIn("worker.sock", text)
        self.assertNotIn(self.queued["run_id"], text)

    def test_terminal_option_shows_fixture_states_and_a_closed_socket(self):
        completed = _command(self.state, "dashboard")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self._assert_records(completed.stdout)
        self._assert_closed_socket("dashboard")

    def test_html_option_writes_one_stale_file_and_exits(self):
        output = self.root / "runs.html"
        completed = _command(self.state, "dashboard-html", "--output", str(output))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        text = output.read_text()
        self._assert_records(text)
        lowered = text.lower()
        for token in _CONTROLS:
            self.assertNotIn(token, lowered)
        self._assert_closed_socket("dashboard-html", "--output", str(self.root / "closed.html"))
        closed = (self.root / "closed.html").read_text().lower()
        for token in _CONTROLS:
            self.assertNotIn(token, closed)

    def test_options_do_not_bind_a_socket(self):
        output = self.root / "guard.html"
        for args in (("dashboard",), ("dashboard-html", "--output", str(output))):
            completed = _guarded(self.state, *args)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("bind", completed.stderr)
            self.assertNotIn("listen", completed.stderr)

    def test_options_take_no_app_key(self):
        for args in (
            ("dashboard", "--app-key", "unused"),
            ("dashboard-html", "--output", str(self.root / "unused.html"), "--app-key", "unused"),
        ):
            completed = _command(self.state, *args)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("unrecognized arguments", completed.stderr)
            self.assertFalse((self.root / "unused.html").exists())


if __name__ == "__main__":
    unittest.main()
