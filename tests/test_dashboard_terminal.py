# SPDX-License-Identifier: MIT

"""Exercise dashboard output and terminal restoration through a real PTY."""

from contextlib import contextmanager
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from execution_core.cli import call


class TerminalDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rr-pty-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        repo = root / "repo"
        repo.mkdir()
        self.state = root / "state"
        worker = subprocess.Popen(
            self.command("worker", "--repository", str(repo)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                call(self.state, "worker.describe", {})
                break
            except (OSError, ValueError):
                time.sleep(0.01)
        else:
            self.fail("worker did not become ready")
        reply = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "terminal-proof",
                "backend": "development",
                "fixture": {"exit_code": 0, "output": "terminal-proof"},
            },
        )
        self.run_id = reply["result"]["run_id"]
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": self.run_id})["result"]
            if record["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual((record["state"], record["exit_code"]), ("succeeded", 0))

    def command(self, *args):
        return [sys.executable, "-m", "execution_core", "--state", str(self.state), *args]

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()

    @contextmanager
    def terminal(self):
        master, slave = pty.openpty()
        try:
            termios.tcsetwinsize(slave, (30, 120))
            previous = termios.tcgetattr(slave)
            process = subprocess.Popen(
                self.command("dashboard"), stdin=slave, stdout=slave, stderr=subprocess.PIPE
            )
            try:
                yield process, master, slave, previous
            finally:
                self.stop(process)
        finally:
            os.close(master)
            os.close(slave)

    def read_until(self, master, marker):
        output = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
            if ready:
                output += os.read(master, 65536)
                if any(
                    item in output for item in ((marker,) if isinstance(marker, bytes) else marker)
                ):
                    return output
        self.fail(f"terminal did not produce {marker!r}: {output!r}")

    def frame(self, master):
        return self.read_until(
            master,
            (b"fixtures do not publish artifacts\n", b"fixtures do not publish artifacts\r\n"),
        )

    def assert_aligned(self, frame):
        body = frame.split(b"\x1b[2J\x1b[H", 1)[1]
        self.assertNotRegex(body, rb"(?<!\r)\n")
        rows = body.split(b"\r\n")
        self.assertEqual(
            rows[2:7],
            [
                b"[previous] k or up",
                b"[next] j or down",
                b"[open] enter",
                b"[cancel] c",
                b"[quit] q",
            ],
        )
        self.assertEqual(rows[7], f"> [run] {self.run_id} succeeded result succeeded".encode())

    def assert_restored(self, slave, previous):
        restored = termios.tcgetattr(slave)
        # PENDIN is a kernel-maintained request to retype pending input on macOS.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        previous[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, previous)

    def test_raw_rows_align_on_initial_frame_refresh_and_resize_then_restore_on_quit(self):
        with self.terminal() as (process, master, slave, previous):
            self.assert_aligned(self.frame(master))
            self.assertEqual(termios.tcgetattr(slave)[1] & termios.OPOST, 0)
            os.write(master, b"j")
            self.assert_aligned(self.frame(master))
            termios.tcsetwinsize(slave, (24, 96))
            process.send_signal(signal.SIGWINCH)
            os.write(master, b"k")
            self.assert_aligned(self.frame(master))
            os.write(master, b"q")
            output = self.read_until(master, b"\x1b[?1000l\x1b[?1006l")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, output)
            self.assert_restored(slave, previous)

    def test_interrupt_restores_terminal_and_disables_mouse_reporting(self):
        with self.terminal() as (process, master, slave, previous):
            self.frame(master)
            process.send_signal(signal.SIGINT)
            self.read_until(master, b"\x1b[?1000l\x1b[?1006l")
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assert_restored(slave, previous)

    def test_redirected_output_keeps_plain_newlines_and_the_same_run(self):
        output = subprocess.check_output(self.command("dashboard"))
        self.assertIn(b"[previous] k or up\n[next] j or down\n[open] enter\n", output)
        self.assertIn(f"> [run] {self.run_id} succeeded result succeeded\n".encode(), output)
        self.assertNotIn(b"\r", output)
        self.assertNotIn(b"\x1b", output)

    def test_stored_log_controls_are_data_on_a_raw_detail_frame(self):
        reply = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "stored-controls",
                "backend": "development",
                "fixture": {
                    "exit_code": 0,
                    "output": "keep-café\r\n\x1b[2Jbare\rline\x07\x1b[0m\n",
                },
            },
        )
        run_id = reply["result"]["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": run_id})["result"]
            if record["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual((record["state"], record["exit_code"]), ("succeeded", 0))
        with self.terminal() as (process, master, slave, previous):
            self.frame(master)
            os.write(master, b"j")
            self.frame(master)
            os.write(master, b"\r")
            detail = self.read_until(master, b"keep-caf")
            body = detail.split(b"\x1b[2J\x1b[H", 1)[1]
            self.assertNotRegex(body, rb"(?<!\r)\n")
            self.assertNotIn(b"\r\r", body)
            self.assertIn("keep-café\r\n".encode(), body)
            self.assertIn(b"^[[2Jbare\r\nline^G^[[0m\r\n", body)
            # Framework clear/home appear once per frame; stored ESC must not.
            self.assertEqual(body.count(b"\x1b"), 0)
            os.write(master, b"q")
            self.read_until(master, b"\x1b[?1000l\x1b[?1006l")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assert_restored(slave, previous)
