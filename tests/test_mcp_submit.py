"""Submit and cancel on the stdio MCP adapter.

submit sends version 1 with event {} and no event_name. A poll-
key does not dial. One dropped reply is sent again. cancel sends
version 0 and returns the worker record.
"""

import json
from pathlib import Path
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from execution_core.cli import call
from execution_core.poll import allowlist_matches
from execution_core.worker import Worker

IMAGE = "sha256:" + "cd" * 32
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
_ALLOWED = {"version", "submission_key", "workflow", "job_id", "event", "image"}


def _readline(stream, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise AssertionError("adapter did not respond")
    line = stream.readline()
    if not line:
        raise AssertionError("adapter closed stdout")
    return json.loads(line)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class Session:
    def __init__(self, state):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "execution_core", "--state", str(state), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def request(self, method, params=None, timeout=5):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        return _readline(self.process.stdout, timeout)

    def call(self, name, arguments=None, timeout=5):
        reply = self.request(
            "tools/call",
            {"name": name, "arguments": arguments if arguments is not None else {}},
            timeout,
        )
        return reply["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.terminate()
        self.process.stderr.read()
        self.process.wait(timeout=5)


class CountingProxy:
    """Counts dials, can drop one worker reply, and can hold connections."""

    def __init__(self, directory, upstream):
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / "worker.sock"
        self.upstream = str(upstream)
        self.accepted = 0
        self.requests = []
        self.hold_remaining = 0
        self.drop_next = False
        self.lock = threading.Lock()
        self._stop = False
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.path.chmod(0o600)
        self.server.listen(16)
        self.server.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self._stop = True
        self.server.close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)

    def _serve(self):
        while not self._stop:
            try:
                client, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self.lock:
                self.accepted += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        with client:
            raw = self._read_line(client)
            with self.lock:
                self.requests.append(raw)
                hold = self.hold_remaining > 0
                if hold:
                    self.hold_remaining -= 1
                drop = self.drop_next
                self.drop_next = False
            if hold:
                client.settimeout(None)
                try:
                    while client.recv(65536):
                        pass
                except OSError:
                    return
                return
            if drop:
                self._read_upstream(raw)
                return
            self._forward(client, raw)

    def _read_line(self, client):
        client.settimeout(5)
        raw = b""
        while b"\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
        return raw

    def _read_upstream(self, raw):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(5)
            upstream.connect(self.upstream)
            upstream.sendall(raw)
            reply = b""
            while b"\n" not in reply:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                reply += chunk

    def _forward(self, client, raw):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(5)
            upstream.connect(self.upstream)
            upstream.sendall(raw)
            reply = b""
            while b"\n" not in reply:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                reply += chunk
            if reply:
                client.sendall(reply)


class WorkflowSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-submit-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.second = self._checkout_second_commit()
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
        self.proxy = CountingProxy(self.root / "proxy", self.state / "worker.sock")
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.proxy.close()
        self.worker.stop.set()
        server = self.worker.server
        if server is not None:
            server.close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def session(self, state=None):
        opened = Session(self.proxy.path.parent if state is None else state)
        self.sessions.append(opened)
        return opened

    def _checkout_second_commit(self):
        workflow = self.repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        (self.repo / "source.txt").write_text("first\n")
        _git(self.repo, "init", "--initial-branch=main")
        _git(self.repo, "add", ".")
        self._commit("first")
        (self.repo / "source.txt").write_text("second\n")
        _git(self.repo, "add", "source.txt")
        self._commit("second")
        second = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(self.repo, "checkout", "--detach", "--force", second)
        return second

    def _commit(self, message):
        subprocess.run(
            [
                "git",
                "-C",
                self.repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                message,
            ],
            check=True,
            capture_output=True,
        )

    def _count(self, key):
        database = sqlite3.connect(self.state / "runs.sqlite3", timeout=5)
        try:
            return database.execute(
                "SELECT count(*) FROM runs WHERE submission_key=?", (key,)
            ).fetchone()[0]
        finally:
            database.close()

    def _submit(self, session, key, job_id="build", timeout=5):
        return session.call(
            "submit",
            {
                "submission_key": key,
                "workflow": ".github/workflows/test.yml",
                "job_id": job_id,
                "image": IMAGE,
            },
            timeout,
        )

    def test_submit_writes_version_1_and_the_force_checkout(self):
        head = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(head.stdout.strip(), "HEAD")
        shown = subprocess.run(
            ["git", "-C", self.repo, "show", f"{self.second}:source.txt"],
            check=True,
            capture_output=True,
        )
        session = self.session()
        listed = session.request("tools/list")["result"]["tools"]
        submit = next(tool for tool in listed if tool["name"] == "submit")
        self.assertIn("new submission key after each edit", submit["description"])
        self.assertIn("compare snapshot ids", submit["description"])
        self.assertNotIn("event", submit["inputSchema"]["properties"])
        self.assertNotIn("event_name", submit["inputSchema"]["properties"])
        self.assertNotIn("path", submit["inputSchema"]["properties"])
        before = self.proxy.accepted
        refused = [
            session.call("submit", {"submission_key": "poll-abc", "workflow": "w", "job_id": "j"}),
            session.call(
                "submit",
                {
                    "submission_key": "nope",
                    "workflow": ".github/workflows/test.yml",
                    "job_id": "build",
                    "event": {"ref": "refs/heads/main"},
                    "event_name": "push",
                    "fixture": {"exit_code": 0},
                    "backend": "development",
                },
            ),
        ]
        for result in refused:
            self.assertIs(result["isError"], True)
            self.assertEqual(result["structuredContent"]["kind"], "INVALID_PARAMS")
            self.assertIs(result["structuredContent"]["retryable"], False)
        self.assertEqual(self.proxy.accepted, before)
        accepted = self._submit(session, "checkout")
        self.assertIs(accepted["isError"], False)
        record = accepted["structuredContent"]
        self.assertEqual(record["state"], "queued")
        params = json.loads(self.proxy.requests[-1])["params"]
        self.assertEqual(params["version"], 1)
        self.assertEqual(params["event"], {})
        self.assertNotIn("event_name", params)
        self.assertNotIn("fixture", params)
        self.assertNotIn("backend", params)
        self.assertTrue(set(params) <= _ALLOWED)
        for value in params.values():
            if isinstance(value, str):
                self.assertNotIn("..", value)
                self.assertFalse(value.startswith("/"))
        snapshot = self.state / "snapshots" / record["input"]["snapshot_id"] / "files"
        self.assertEqual((snapshot / "source.txt").read_bytes(), shown.stdout)
        self.assertEqual(shown.stdout, b"second\n")

    def test_same_key_keeps_the_original_snapshot(self):
        session = self.session(self.state)
        first = self._submit(session, "same")
        self.assertIs(first["isError"], False)
        run_id = first["structuredContent"]["run_id"]
        snapshot_id = first["structuredContent"]["input"]["snapshot_id"]
        again = self._submit(session, "same")
        self.assertIs(again["isError"], False)
        self.assertEqual(again["structuredContent"]["run_id"], run_id)
        self.assertEqual(again["structuredContent"]["input"]["snapshot_id"], snapshot_id)
        (self.repo / "source.txt").write_text("edited\n")
        edited = self._submit(session, "same")
        self.assertIs(edited["isError"], False)
        self.assertEqual(edited["structuredContent"]["run_id"], run_id)
        self.assertEqual(edited["structuredContent"]["input"]["snapshot_id"], snapshot_id)
        stored = self.state / "snapshots" / snapshot_id / "files" / "source.txt"
        self.assertEqual(stored.read_bytes(), b"second\n")
        conflict = self._submit(session, "same", job_id="other")
        self.assertIs(conflict["isError"], True)
        self.assertEqual(conflict["structuredContent"]["kind"], "IDEMPOTENCY_CONFLICT")
        self.assertIs(conflict["structuredContent"]["retryable"], False)
        self.assertEqual(self._count("same"), 1)
        queued = session.call("get", {"run_id": run_id})
        self.assertEqual(queued["structuredContent"]["state"], "queued")
        proxied = self.session()
        cancelled = proxied.call("cancel", {"run_id": run_id})
        self.assertIs(cancelled["isError"], False)
        body = cancelled["structuredContent"]
        self.assertEqual(body["state"], "cancelled")
        self.assertFalse(body["state"] == "succeeded" and body["exit_code"] == 0)
        self.assertEqual(body["run_id"], run_id)
        cancel_params = [
            json.loads(raw)["params"] for raw in self.proxy.requests if b'"run.cancel"' in raw
        ]
        self.assertEqual(cancel_params, [{"run_id": run_id, "version": 0}])
        self.assertIs(type(cancel_params[0]["version"]), int)

    def test_a_dropped_submit_is_retried_once(self):
        session = self.session()
        self.proxy.drop_next = True
        accepted = self._submit(session, "retry")
        self.assertIs(accepted["isError"], False)
        run_id = accepted["structuredContent"]["run_id"]
        keys = [
            json.loads(raw)["params"]["submission_key"]
            for raw in self.proxy.requests
            if b"run.submit" in raw
        ]
        self.assertEqual(keys, ["retry", "retry"])
        self.assertEqual(self._count("retry"), 1)
        self.assertEqual(accepted["structuredContent"]["run_id"], run_id)

    def test_a_second_submit_timeout_is_not_retried_again(self):
        session = self.session()
        before = len(self.proxy.requests)
        self.proxy.hold_remaining = 2
        started = time.monotonic()
        result = self._submit(session, "slow", timeout=16)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 9)
        self.assertLess(elapsed, 14)
        self.assertEqual(len(self.proxy.requests), before + 2)
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"]["kind"], "WORKER_TIMEOUT")
        self.assertIs(result["structuredContent"]["retryable"], True)
        self.assertEqual(self._count("slow"), 0)


class CancelAndKeyTests(unittest.TestCase):
    def test_cancel_of_a_succeeded_run_reports_exit_code_0(self):
        tmp = tempfile.TemporaryDirectory(prefix="mcp-submit-ok-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / "repo"
        repo.mkdir()
        state = root / "state"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(state),
                "worker",
                "--repository",
                str(repo),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(process.communicate, timeout=5)
        self.addCleanup(process.terminate)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            try:
                call(state, "worker.describe", {})
                break
            except (OSError, ValueError):
                time.sleep(0.01)
        else:
            self.fail("worker did not become ready")
        submitted = call(
            state,
            "run.submit",
            {
                "version": 0,
                "submission_key": "done",
                "backend": "development",
                "fixture": {"exit_code": 0, "output": "ok"},
            },
        )
        run_id = submitted["result"]["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = call(state, "run.get", {"run_id": run_id})["result"]
            if record["state"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail(record)
        session = Session(state)
        self.addCleanup(session.close)
        cancelled = session.call("cancel", {"run_id": run_id})
        self.assertIs(cancelled["isError"], False)
        body = cancelled["structuredContent"]
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(body["exit_code"], 0)
        self.assertTrue(body["state"] == "succeeded" and body["exit_code"] == 0)

    def test_allowlist_misses_an_empty_event_and_mcp_takes_no_key(self):
        self.assertIs(allowlist_matches({}, (), (), None), False)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                "unused",
                "mcp",
                "--app-key",
                "unused",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments", completed.stderr)
