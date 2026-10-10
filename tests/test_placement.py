# SPDX-License-Identifier: MPL-2.0

"""Placement across configured workers.

Tests drive a real poll pass. A stand-in ssh is first on PATH for the
ssh place. No test contacts GitHub or opens a listen port.
"""

import json
import os
from argparse import Namespace
from http.server import ThreadingHTTPServer
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

from execution_core.cli import _places_from_args, _run_poll
from execution_core.poll import PollError, poll_once
from execution_core.protocol import canonical
from execution_core.worker import Worker
from test_poll import TOKEN, _Handler, _git

X86 = "sha256:" + "cd" * 32
ARM = "sha256:" + "ab" * 32
JOB = (".github/workflows/check.yml", "check")
LINT = (".github/workflows/check.yml", "lint")
WORKFLOW = """\
name: check
on:
  push:
  pull_request:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: echo lint
"""
SSH = """\
#!/usr/bin/env python3
import os
import sys

path = os.environ["SSH_ARGV_FILE"]
with open(path, "ab") as handle:
    handle.write(b"\\0".join(os.fsencode(arg) for arg in sys.argv[1:]))
    handle.write(b"\\0\\0")
if "--" not in sys.argv:
    sys.exit(2)
index = sys.argv.index("--")
args = sys.argv[index + 1 :]
remote = os.environ.get("SSH_REMOTE_PATH")
local = os.environ.get("SSH_LOCAL_PATH")
if remote and local:
    args = [local if arg == remote else arg for arg in args]
os.execvp(args[0], args)
"""


def _commands(path):
    raw = Path(path).read_bytes() if Path(path).exists() else b""
    records = []
    for chunk in raw.split(b"\0\0"):
        if chunk:
            records.append([part.decode() for part in chunk.split(b"\0")])
    return records


def _request(method, params):
    return (
        canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    ).encode()


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.old_path = os.environ.get("PATH", "")
        self.tmp = tempfile.TemporaryDirectory(prefix="placement-")
        self.root = Path(self.tmp.name)
        self.argv_path = self.root / "ssh-argv"
        os.environ["SSH_ARGV_FILE"] = str(self.argv_path)
        os.environ.pop("SSH_REMOTE_PATH", None)
        os.environ.pop("SSH_LOCAL_PATH", None)
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
        self.poll_state = self.root / "poll-state"
        self.poll_state.mkdir()
        self.credential = self.root / "credential"
        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.branches = [{"name": "main", "commit": {"sha": self._tip()}}]
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
        self.workers = []
        self.sockets = []

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("SSH_ARGV_FILE", None)
        os.environ.pop("SSH_REMOTE_PATH", None)
        os.environ.pop("SSH_LOCAL_PATH", None)
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        for worker in self.workers:
            worker.close()
        for thread in self.sockets:
            thread.join(timeout=5)
        self.tmp.cleanup()

    def _tip(self):
        return _git(self.remote, "rev-parse", "refs/heads/main").stdout.strip()

    def _advance(self):
        name = f"next-{len(list(self.seed.glob('next-*.txt')))}.txt"
        (self.seed / name).write_text(name + "\n")
        _git(self.seed, "add", name)
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            name,
        )
        _git(self.seed, "push", "origin", "main")
        tip = self._tip()
        self.server.etags["branches"] = "branches-" + tip[:12]
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        return tip

    def _serve_socket(self, worker):
        while not worker.stop.is_set():
            try:
                client, _ = worker.server.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(1)
                try:
                    with client.makefile("rb") as stream:
                        raw = stream.readline(1024 * 1024 + 1)
                    if raw:
                        reply = worker.response(raw)
                        client.sendall((canonical(reply) + "\n").encode())
                except (OSError, ValueError):
                    continue

    def _worker(
        self, name, origin="https://github.com/acme/demo.git", runner_image=None, accept=False
    ):
        repo = self.root / name
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "README").write_text("local only\n")
        _git(repo, "add", "README")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "worker",
        )
        _git(repo, "remote", "add", "origin", origin)
        worker = Worker(repo, self.root / f"state-{name}", runner_image=runner_image)
        worker.start()
        worker.stop.set()
        worker.scheduler.join(timeout=5)
        if worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        worker.stop.clear()
        if accept:
            thread = threading.Thread(target=self._serve_socket, args=(worker,), daemon=True)
            thread.start()
            self.sockets.append(thread)
        self.workers.append(worker)
        return worker

    def _caller(self, worker, repository=None):
        def caller(method, params):
            reply = worker.response(_request(method, params))
            if "error" in reply:
                data = reply["error"].get("data") or {}
                message = reply["error"].get("message") or "worker refused the poll request"
                raise PollError(data.get("kind") or "WORKER_ERROR", message)
            result = reply["result"]
            if method == "worker.describe" and repository is not None:
                result = dict(result)
                result["repository"] = repository
            return result

        return caller

    def _place(self, name, worker, cap=1, image=X86, ssh=None, caller=None):
        place = {
            "name": name,
            "state": str(worker.state),
            "cap": cap,
            "image": image,
            "caller": caller or self._caller(worker),
        }
        if ssh is not None:
            place["ssh"] = ssh
        return place

    def _poll(self, places, image=X86, jobs=None):
        return poll_once(
            repository="acme/demo",
            clone=self.clone,
            jobs=jobs or [JOB],
            image=image,
            credential_file=self.credential,
            api_base=self.base,
            state=self.poll_state,
            caller=None,
            places=places,
        )

    def _runs(self, worker):
        return worker.db.execute("SELECT count(*) FROM runs").fetchone()[0]

    def _saved(self):
        return json.loads((self.poll_state / "poll.json").read_text())

    def commands(self):
        return _commands(self.argv_path)

    def test_json_place_uses_the_local_socket(self):
        worker = self._worker("mac", accept=True)
        text = json.dumps({"name": "mac", "state": str(worker.state), "cap": 1, "image": X86})
        args = Namespace(
            repository="acme/demo",
            clone=self.clone,
            job=[JOB],
            image=X86,
            credential_file=self.credential,
            app_key=None,
            api_base=self.base,
            state=self.poll_state,
            place=[text],
            ssh_host=None,
            remote_state=None,
        )

        def mint(*_args):
            self.fail("list mint is not used")

        def revoke(*_args):
            self.fail("list revoke is not used")

        places = _places_from_args(args)
        result = _run_poll(args, None, mint, revoke, places)
        self.assertEqual(result["submitted"][0]["host"], "mac")
        self.assertEqual(self._runs(worker), 1)
        self.assertEqual(self._saved()["submissions"][0]["host"], "mac")
        self.assertEqual(_git(worker.repository, "rev-parse", "HEAD").stdout.strip(), self._tip())
        self.assertEqual(self.commands(), [])

    def test_order_skips_a_stopped_worker_and_a_full_cap(self):
        alpha = self._worker("alpha")
        beta = self._worker("beta")
        gamma = self._worker("gamma")
        alpha.stop.set()
        result = self._poll(
            [
                self._place("alpha", alpha),
                self._place("beta", beta),
                self._place("gamma", gamma),
            ],
            jobs=[JOB, LINT],
        )
        self.assertEqual([item["host"] for item in result["submitted"]], ["beta", "gamma"])
        self.assertEqual(self._runs(alpha), 0)
        self.assertEqual(self._runs(beta), 1)
        self.assertEqual(self._runs(gamma), 1)
        self.assertEqual(
            result["skipped"],
            [
                {"reason": "not_ready", "host": "alpha"},
                {"reason": "host_full", "host": "beta"},
            ],
        )
        self.assertEqual([item["host"] for item in self._saved()["submissions"]], ["beta", "gamma"])

    def test_a_queued_run_fills_the_cap_on_the_next_pass(self):
        alpha = self._worker("alpha")
        beta = self._worker("beta")
        places = [self._place("alpha", alpha), self._place("beta", beta)]
        first = self._poll(places)
        self.assertEqual(first["submitted"][0]["host"], "alpha")
        self._advance()
        second = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        self.assertEqual(second["submitted"][0]["host"], "beta")
        self.assertIn({"reason": "host_full", "host": "alpha"}, second["skipped"])
        self.assertEqual(self._runs(alpha), 1)
        self.assertEqual(self._runs(beta), 1)
        self.assertEqual([item["host"] for item in self._saved()["submissions"]], ["alpha", "beta"])

    def test_an_image_mismatch_does_not_connect(self):
        calls = []

        def boom(_method, _params):
            calls.append(_method)
            raise PollError("WORKER_ERROR", "worker ssh call failed")

        x86 = self._worker("x86")
        result = self._poll(
            [
                {
                    "name": "arm",
                    "state": "/arm-state",
                    "cap": 1,
                    "image": ARM,
                    "ssh": "arm-host",
                    "caller": boom,
                },
                self._place("x86", x86),
            ]
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.commands(), [])
        self.assertEqual(result["submitted"][0]["host"], "x86")
        self.assertIn({"reason": "image", "host": "arm"}, result["skipped"])
        self.assertEqual(self._runs(x86), 1)

    def test_an_omitted_image_matches_the_worker_runner_image(self):
        arm = self._worker("arm", runner_image=X86)
        x86 = self._worker("x86", runner_image=X86)
        result = self._poll(
            [self._place("arm", arm, image=ARM), self._place("x86", x86, image=X86)],
            image=None,
        )
        self.assertEqual(result["submitted"][0]["host"], "x86")
        self.assertEqual(self._runs(arm), 0)
        self.assertEqual(self._runs(x86), 1)
        self.assertIn({"reason": "image", "host": "arm"}, result["skipped"])

    def test_a_name_at_digest_matches_the_place_image(self):
        worker = self._worker("mac")
        result = self._poll([self._place("mac", worker)], image="example.invalid/runner@" + X86)
        self.assertEqual(result["submitted"][0]["host"], "mac")
        self.assertEqual(self._runs(worker), 1)

    def test_no_ready_place_records_nothing(self):
        alpha = self._worker("alpha")
        alpha.stop.set()
        result = self._poll([self._place("alpha", alpha)])
        self.assertEqual(result["submitted"], [])
        self.assertEqual(self._runs(alpha), 0)
        self.assertEqual(self._saved()["submissions"], [])
        self.assertEqual([item["reason"] for item in result["skipped"]], ["not_ready", "no_host"])
        self.assertEqual(result["skipped"][1]["workflow"], JOB[0])
        self.assertEqual(result["skipped"][1]["job_id"], JOB[1])
        self.assertEqual(result["skipped"][1]["event"], "push")

    def test_a_recorded_place_is_not_moved(self):
        alpha = self._worker("alpha")
        beta = self._worker("beta")
        first = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        run_id = first["submitted"][0]["run_id"]
        alpha.stop.set()
        self._advance()
        second = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        self.assertEqual(second["submitted"][0]["host"], "beta")
        saved = self._saved()
        old = next(item for item in saved["submissions"] if item["run_id"] == run_id)
        self.assertEqual(old["host"], "alpha")
        self.assertEqual(self._runs(alpha), 1)
        self.assertEqual(self._runs(beta), 1)

    def test_a_lost_run_is_posted_on_its_place(self):
        alpha = self._worker("alpha")
        beta = self._worker("beta")
        first = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        run_id = first["submitted"][0]["run_id"]
        record = alpha.get(run_id)
        record["state"] = "lost"
        record["exit_code"] = None
        with alpha.guard, alpha.db:
            alpha.save(record)
        self.server.requests.clear()
        second = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        self.assertEqual(second["submitted"], [])
        self.assertEqual(self._runs(alpha), 1)
        self.assertEqual(self._runs(beta), 0)
        posts = [item for item in self.server.requests if item["method"] == "POST"]
        self.assertEqual(json.loads(posts[0]["body"])["state"], "error")
        self.assertEqual(self._saved()["submissions"][0]["host"], "alpha")
        self.assertEqual(self._saved()["submissions"][0]["run_id"], run_id)

    def test_an_unknown_host_is_left(self):
        alpha = self._worker("alpha")
        first = self._poll([self._place("alpha", alpha, cap=2)])
        run_id = first["submitted"][0]["run_id"]
        saved = self._saved()
        saved["submissions"][0]["host"] = "retired"
        (self.poll_state / "poll.json").write_text(json.dumps(saved) + "\n")
        self._advance()
        second = self._poll([self._place("alpha", alpha, cap=2)])
        self.assertEqual(second["submitted"][0]["host"], "alpha")
        again = self._saved()
        retired = next(item for item in again["submissions"] if item["host"] == "retired")
        self.assertEqual(retired["run_id"], run_id)
        self.assertIn({"reason": "host_unknown", "host": "retired"}, second["skipped"])
        self.assertEqual(self._runs(alpha), 2)

    def test_an_unreachable_claim_is_not_moved(self):
        alpha = self._worker("alpha")
        beta = self._worker("beta")
        failing = {"on": False}

        def caller(method, params):
            if failing["on"]:
                raise PollError("WORKER_ERROR", "worker ssh call failed")
            return self._caller(alpha)(method, params)

        self._poll([self._place("alpha", alpha, caller=caller), self._place("beta", beta)])
        saved = self._saved()
        self.assertEqual(saved["submissions"][0]["host"], "alpha")
        saved["submissions"][0].pop("run_id")
        saved["tips"] = {}
        (self.poll_state / "poll.json").write_text(json.dumps(saved) + "\n")
        failing["on"] = True
        self.server.etags["branches"] = "branches-again"
        result = self._poll([self._place("alpha", alpha, caller=caller), self._place("beta", beta)])
        self.assertEqual(result["submitted"], [])
        self.assertEqual(self._runs(beta), 0)
        self.assertEqual(self._runs(alpha), 1)
        again = self._saved()
        self.assertEqual(again["submissions"][0]["host"], "alpha")
        self.assertNotIn("run_id", again["submissions"][0])
        self.assertIn({"reason": "unreachable", "host": "alpha"}, result["skipped"])

    def test_a_repository_mismatch_uses_the_next_place(self):
        alpha = self._worker("alpha", origin="https://github.com/other/name.git")
        beta = self._worker("beta")
        result = self._poll([self._place("alpha", alpha), self._place("beta", beta)])
        self.assertEqual(result["submitted"][0]["host"], "beta")
        self.assertEqual(self._runs(alpha), 0)
        self.assertEqual(self._runs(beta), 1)
        self.assertIn({"reason": "repository", "host": "alpha"}, result["skipped"])

    def test_ssh_place_records_the_place_name(self):
        worker = self._worker("remote")
        os.environ["SSH_REMOTE_PATH"] = "/remote/worker"
        os.environ["SSH_LOCAL_PATH"] = str(worker.repository)
        result = self._poll(
            [
                self._place(
                    "nexus",
                    worker,
                    ssh="worker-host",
                    caller=self._caller(worker, repository="/remote/worker"),
                )
            ]
        )
        self.assertEqual(result["submitted"][0]["host"], "nexus")
        saved = self._saved()
        self.assertEqual(saved["submissions"][0]["host"], "nexus")
        self.assertNotIn("worker-host", json.dumps(saved["submissions"]))
        self.assertNotIn("/remote/worker", json.dumps(saved))
        tip = self._tip()
        repo = Path(worker.repository)
        self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), tip)
        self.assertEqual((repo / ".github/workflows/check.yml").read_text(), WORKFLOW)
        self.assertFalse((repo / "README").exists())
        posts = [item for item in self.server.requests if item["method"] == "POST"]
        self.assertEqual(json.loads(posts[0]["body"])["state"], "pending")
        flat = [arg for command in self.commands() for arg in command]
        self.assertIn("BatchMode=yes", flat)
        self.assertIn("worker-host", flat)
        self.assertIn("/remote/worker", flat)
        self.assertIn("unpack-objects", flat)
        self.assertNotIn("fetch", flat)
        for command in self.commands():
            self.assertEqual(command[:2], ["-o", "BatchMode=yes"])
            self.assertNotIn("--docker-socket", command)
            self.assertNotIn("--app-key", command)
            for flag in ("-L", "-R", "-D", "-W", "-N"):
                self.assertNotIn(flag, command)


class PlaceFlagTests(unittest.TestCase):
    def test_a_bad_place_is_refused(self):
        base = {"name": "mac", "state": "/state", "cap": 1, "image": X86}
        samples = [
            "-",
            "{",
            "[]",
            json.dumps({**base, "name": "-mac"}),
            json.dumps({**base, "cap": 0}),
            json.dumps({**base, "cap": True}),
            json.dumps({**base, "cap": 101}),
            json.dumps({**base, "image": "ubuntu:latest"}),
            json.dumps({**base, "extra": 1}),
            json.dumps({**base, "state": "-state"}),
            json.dumps({**base, "ssh": "-host"}),
            json.dumps({**base, "state": "remote\nstate"}),
        ]
        for text in samples:
            with self.subTest(text=text):
                args = Namespace(place=[text], ssh_host=None, remote_state=None)
                with self.assertRaises(PollError) as raised:
                    _places_from_args(args)
                self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
                self.assertEqual(str(raised.exception), "place is not accepted")

    def test_a_repeated_place_name_is_refused(self):
        text = json.dumps({"name": "mac", "state": "/state", "cap": 1, "image": X86})
        args = Namespace(place=[text, text], ssh_host=None, remote_state=None)
        with self.assertRaises(PollError) as raised:
            _places_from_args(args)
        self.assertEqual(str(raised.exception), "place is not accepted")

    def test_place_and_ssh_flags_are_refused_before_ssh_runs(self):
        tmp = tempfile.TemporaryDirectory(prefix="place-flag-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        marker = root / "ssh-ran"
        bindir = root / "bin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text("#!/bin/sh\necho ran > " + shlex.quote(str(marker)) + "\nexit 9\n")
        ssh.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        place = json.dumps({"name": "mac", "state": "/state", "cap": 1, "image": X86})
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(root),
                "poll",
                "--repository",
                "acme/demo",
                "--clone",
                str(root),
                "--job",
                ".github/workflows/check.yml",
                "check",
                "--credential-file",
                str(root / "token"),
                "--place",
                place,
                "--ssh-host",
                "worker-host",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("place and ssh host are set separately", result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
