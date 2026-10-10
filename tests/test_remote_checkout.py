# SPDX-License-Identifier: MPL-2.0

"""Remote checkout and the host claim.

The stand-in ssh is first on PATH. It records argv and runs the remote
command locally. No test contacts GitHub or opens a listen port.
"""

import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from execution_core.cli import _run_poll
from execution_core.poll import PollError, poll_once
from execution_core.protocol import canonical
from execution_core.worker import Worker
from test_poll import IMAGE, JOB, TOKEN, WORKFLOW, _Handler, _git

HOST = "worker-host"
SSH = """\
#!/usr/bin/env python3
import os
import sys

path = os.environ["SSH_ARGV_FILE"]
with open(path, "ab") as handle:
    handle.write(b"\\0".join(os.fsencode(arg) for arg in sys.argv[1:]))
    handle.write(b"\\0\\0")
if os.environ.get("SSH_FORCE_FAIL") == "1":
    sys.stderr.buffer.write(b"SECRET-STDERR-PATH\\n")
    sys.exit(1)
if "--" not in sys.argv:
    sys.exit(2)
index = sys.argv.index("--")
os.execvp(sys.argv[index + 1], sys.argv[index + 1:])
"""


def _commands(path):
    raw = Path(path).read_bytes() if Path(path).exists() else b""
    records = []
    for chunk in raw.split(b"\0\0"):
        if chunk:
            records.append([part.decode() for part in chunk.split(b"\0")])
    return records


class RemoteCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.old_path = os.environ.get("PATH", "")
        self.tmp = tempfile.TemporaryDirectory(prefix="remote-checkout-")
        self.root = Path(self.tmp.name)
        self.argv_path = self.root / "ssh-argv"
        os.environ["SSH_ARGV_FILE"] = str(self.argv_path)
        os.environ.pop("SSH_FORCE_FAIL", None)
        bindir = self.root / "bin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text(SSH)
        ssh.chmod(0o755)
        os.environ["PATH"] = str(bindir) + os.pathsep + self.old_path
        self.remote = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.seed = self.root / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-b", "main")
        workflow = self.seed / ".github/workflows/check.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-u", "origin", "main")
        self.clone = self.root / "clone"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.clone)],
            check=True,
            capture_output=True,
        )
        self.worker_repo = self.root / "worker"
        self.worker_repo.mkdir()
        _git(self.worker_repo, "init", "-b", "main")
        (self.worker_repo / "README").write_text("local only\n")
        _git(self.worker_repo, "add", "README")
        _git(
            self.worker_repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "worker",
        )
        _git(self.worker_repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
        self.state = self.root / "state"
        self.credential = self.root / "credential"
        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.branches = []
        self.server.pulls = []
        self.server.remaining = 40
        self.server.remaining_by_kind = {}
        self.server.repository = {
            "id": 5150,
            "full_name": "ignored/name",
            "default_branch": "main",
        }
        self.server.authors = {}
        self.server.etags = {"branches": "branches-1", "pulls": "pulls-1"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.worker = Worker(self.worker_repo, self.state)
        self.worker.start()
        self.worker.stop.set()
        self.worker.scheduler.join(timeout=5)
        if self.worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        self.worker.stop.clear()
        self.submits = []
        self.claims_at_submit = []
        self.head_at_submit = None

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("SSH_FORCE_FAIL", None)
        os.environ.pop("SSH_ARGV_FILE", None)
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.worker.close()
        self.tmp.cleanup()

    def _tip(self):
        return _git(self.remote, "rev-parse", "refs/heads/main").stdout.strip()

    def _request(self, method, params):
        return (
            canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
        ).encode()

    def caller(self, method, params):
        if method == "run.submit":
            self.submits.append(params)
            saved = json.loads((self.state / "poll.json").read_text())
            claim = next(
                item for item in saved["submissions"] if item["key"] == params["submission_key"]
            )
            self.claims_at_submit.append(dict(claim))
            self.head_at_submit = _git(self.worker_repo, "rev-parse", "HEAD").stdout.strip()
        reply = self.worker.response(self._request(method, params))
        if "error" in reply:
            raise PollError(reply["error"]["data"]["kind"], reply["error"]["message"])
        return reply["result"]

    def poll(self, ssh_host=HOST):
        def mint(*_args):
            self.fail("list mint is not used")

        def revoke(*_args):
            self.fail("list revoke is not used")

        args = type("Args", (), {})()
        args.repository = "acme/demo"
        args.clone = self.clone
        args.job = [JOB]
        args.image = IMAGE
        args.credential_file = self.credential
        args.app_key = None
        args.api_base = self.base
        args.state = self.state
        args.ssh_host = ssh_host
        return _run_poll(args, self.caller, mint, revoke)

    def commands(self):
        return _commands(self.argv_path)

    def runs(self):
        return self.worker.db.execute("SELECT count(*) FROM runs").fetchone()[0]

    def test_remote_checkout_claims_the_host_before_submit(self):
        tip = self._tip()
        missing = _git(self.worker_repo, "cat-file", "-e", tip + "^{commit}", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        result = self.poll()
        self.assertFalse(result["stopped"])
        self.assertEqual(self.runs(), 1)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.claims_at_submit[0]["host"], HOST)
        self.assertNotIn("run_id", self.claims_at_submit[0])
        self.assertEqual(self.head_at_submit, tip)
        self.assertEqual(_git(self.worker_repo, "rev-parse", "HEAD").stdout.strip(), tip)
        self.assertEqual((self.worker_repo / ".github/workflows/check.yml").read_text(), WORKFLOW)
        self.assertFalse((self.worker_repo / "README").exists())
        present = _git(self.worker_repo, "cat-file", "-e", tip + "^{commit}", check=False)
        self.assertEqual(present.returncode, 0)
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(saved["submissions"][0]["host"], HOST)
        self.assertEqual(saved["submissions"][0]["run_id"], result["submitted"][0]["run_id"])
        flat = [arg for command in self.commands() for arg in command]
        self.assertIn("unpack-objects", flat)
        self.assertNotIn("fetch", flat)
        for command in self.commands():
            self.assertEqual(command[:2], ["-o", "BatchMode=yes"])
            self.assertNotIn("--docker-socket", command)
            self.assertNotIn("--app-key", command)
            for flag in ("-L", "-R", "-D", "-W", "-N"):
                self.assertNotIn(flag, command)
        self.assertNotEqual(self.clone.resolve(), self.worker_repo.resolve())

    def test_a_recorded_host_is_not_sent_a_second_time(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.poll()
        saved = json.loads((self.state / "poll.json").read_text())
        saved["submissions"][0]["host"] = "other-host"
        (self.state / "poll.json").write_text(json.dumps(saved) + "\n")
        self.server.requests.clear()
        second = self.poll()
        self.assertEqual(second["submitted"], [])
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.runs(), 1)
        again = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(again["submissions"][0]["host"], "other-host")

    def test_a_lost_run_stays_on_its_host(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        first = self.poll()
        run_id = first["submitted"][0]["run_id"]
        record = self.worker.get(run_id)
        record["state"] = "lost"
        record["exit_code"] = None
        with self.worker.guard, self.worker.db:
            self.worker.save(record)
        self.server.requests.clear()
        second = self.poll()
        self.assertEqual(second["submitted"], [])
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.runs(), 1)
        posts = [item for item in self.server.requests if item["method"] == "POST"]
        self.assertEqual(json.loads(posts[0]["body"])["state"], "error")
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(saved["submissions"][0]["host"], HOST)
        self.assertEqual(saved["submissions"][0]["run_id"], run_id)

    def test_origin_name_mismatch_submits_nothing(self):
        _git(self.worker_repo, "remote", "set-url", "origin", "https://github.com/other/name.git")
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        with self.assertRaises(PollError) as raised:
            self.poll()
        self.assertEqual(raised.exception.kind, "CLONE_MISMATCH")
        self.assertEqual(str(raised.exception), "worker repository must be the polled repository")
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.submits, [])
        flat = [arg for command in self.commands() for arg in command]
        self.assertIn("get-url", flat)
        self.assertNotIn("unpack-objects", flat)

    def test_a_local_pass_still_requires_the_clone_path(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        with self.assertRaises(PollError) as raised:
            poll_once(
                repository="acme/demo",
                clone=self.clone,
                jobs=[JOB],
                image=IMAGE,
                credential_file=self.credential,
                api_base=self.base,
                state=self.state,
                caller=self.caller,
            )
        self.assertEqual(raised.exception.kind, "CLONE_MISMATCH")
        self.assertEqual(str(raised.exception), "dedicated clone must be the worker repository")
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.runs(), 0)

    def test_a_rejected_host_does_not_run_ssh(self):
        with self.assertRaises(PollError) as raised:
            self.poll(ssh_host="-L")
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(str(raised.exception), "ssh host is not accepted")
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.submits, [])

    def test_ssh_stderr_is_not_returned(self):
        os.environ["SSH_FORCE_FAIL"] = "1"
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        with self.assertRaises(PollError) as raised:
            self.poll()
        self.assertEqual(raised.exception.kind, "GIT_FAILED")
        self.assertEqual(str(raised.exception), "git command failed")
        self.assertNotIn("SECRET-STDERR-PATH", str(raised.exception))
        self.assertEqual(self.runs(), 0)
        self.assertGreaterEqual(len(self.commands()), 1)
