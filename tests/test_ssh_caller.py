# SPDX-License-Identifier: MPL-2.0

"""The poller reaches a worker through ssh -o BatchMode=yes.

The remote command is the local client. Tests put a stand-in ssh on PATH.
No listen port is opened.
"""

from argparse import Namespace
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from execution_core.cli import _poll_caller, ssh_caller
from execution_core.poll import PollError


def _write_executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


class SshCallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.argv_path = self.root / "argv"
        self.marker = self.root / "ssh-ran"
        _write_executable(
            self.bin / "python3",
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n',
        )
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.old_path
        os.environ["SSH_ARGV_FILE"] = str(self.argv_path)
        self.worker = subprocess.Popen(
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.worker.poll() is not None:
                self.fail(self.worker.stderr.read().decode())
            if (self.state / "worker.sock").exists():
                break
            time.sleep(0.01)
        else:
            self.fail("worker did not become ready")

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("SSH_ARGV_FILE", None)
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.communicate(timeout=5)
        self.tmp.cleanup()

    def _ssh(self, text):
        _write_executable(self.bin / "ssh", text)

    def test_batch_mode_reaches_the_local_client(self):
        self._ssh(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['SSH_ARGV_FILE']).write_text('\\n'.join(sys.argv))\n"
            "index = sys.argv.index('--')\n"
            "os.execvp(sys.argv[index + 1], sys.argv[index + 1:])\n"
        )
        described = ssh_caller("worker-host", str(self.state))("worker.describe", {})
        self.assertTrue(described["ready"])
        self.assertEqual(Path(described["repository"]).resolve(), self.repo.resolve())
        recorded = self.argv_path.read_text().splitlines()
        self.assertEqual(Path(recorded[0]).name, "ssh")
        self.assertEqual(
            recorded[1:],
            [
                "-o",
                "BatchMode=yes",
                "worker-host",
                "--",
                "python3",
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "call",
            ],
        )
        for flag in ("-L", "-R", "-D", "-W", "-N", "--docker-socket", "--app-key"):
            self.assertNotIn(flag, recorded)
        with self.assertRaises(PollError) as caught:
            ssh_caller("worker-host", str(self.state))("run.get", {"run_id": "missing-run"})
        self.assertEqual(caught.exception.kind, "RUN_NOT_FOUND")
        self.assertNotIn(str(self.state), str(caught.exception))

    def test_a_hung_ssh_times_out_without_a_listen_port(self):
        self._ssh("#!/bin/sh\nexec sleep 60\n")
        started = time.monotonic()
        with self.assertRaises(PollError) as caught:
            ssh_caller("worker-host", str(self.state))("worker.describe", {})
        self.assertEqual(caught.exception.kind, "WORKER_ERROR")
        self.assertEqual(str(caught.exception), "worker ssh call timed out")
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(self.marker.exists())

    def test_ssh_stderr_is_not_returned(self):
        self._ssh(
            "#!/bin/sh\n"
            "echo SECRET-STDERR-PATH >&2\n"
            "echo marker > " + shlex.quote(str(self.marker)) + "\n"
            "exit 2\n"
        )
        with self.assertRaises(PollError) as caught:
            ssh_caller("worker-host", str(self.state))("worker.describe", {})
        self.assertEqual(str(caught.exception), "worker ssh call failed")
        self.assertNotIn("SECRET-STDERR-PATH", str(caught.exception))
        self.assertTrue(self.marker.is_file())

    def test_a_bad_host_does_not_run_ssh(self):
        self._ssh("#!/bin/sh\necho ran > " + shlex.quote(str(self.marker)) + "\nexit 1\n")
        for host in ("-L", "worker host", "user@-host", ""):
            with self.subTest(host=host):
                with self.assertRaises(PollError) as caught:
                    ssh_caller(host, str(self.state))
                self.assertEqual(caught.exception.kind, "INVALID_PARAMS")
        with self.assertRaises(PollError) as caught:
            ssh_caller("worker-host", "remote\nstate")
        self.assertEqual(caught.exception.kind, "INVALID_PARAMS")
        self.assertFalse(self.marker.exists())

    def test_poll_uses_ssh_only_when_both_flags_are_set(self):
        self._ssh("#!/bin/sh\necho ran > " + shlex.quote(str(self.marker)) + "\nexit 1\n")
        local = _poll_caller(Namespace(state=str(self.state), ssh_host=None, remote_state=None))
        described = local("worker.describe", {})
        self.assertTrue(described["ready"])
        self.assertFalse(self.marker.exists())
        for kwargs in (
            {"ssh_host": "worker-host", "remote_state": None},
            {"ssh_host": None, "remote_state": str(self.state)},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(PollError) as caught:
                    _poll_caller(Namespace(state=str(self.state), **kwargs))
                self.assertEqual(
                    str(caught.exception), "ssh host and remote state are set together"
                )
        self.assertFalse(self.marker.exists())

    def test_cli_refuses_one_ssh_flag_before_spawning_ssh(self):
        self._ssh("#!/bin/sh\necho ran > " + shlex.quote(str(self.marker)) + "\nexit 9\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "poll",
                "--repository",
                "acme/demo",
                "--clone",
                str(self.repo),
                "--job",
                ".github/workflows/ci.yml",
                "check",
                "--credential-file",
                str(self.root / "missing-token"),
                "--ssh-host",
                "worker-host",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=os.environ.copy(),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ssh host and remote state are set together", result.stderr)
        self.assertNotIn(str(self.state), result.stderr)
        self.assertNotIn(str(self.state), result.stdout)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
