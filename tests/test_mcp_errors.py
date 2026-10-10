# SPDX-License-Identifier: MPL-2.0

"""Error results on the stdio MCP adapter.

A bad tool call is a tool error and the session stays open. An
unknown tool, a missing run id, and a cursor that is not 1 to 1024
characters are rejected before the adapter dials. Worker faults keep
the worker's kind.
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
from execution_core.protocol import canonical
from execution_core.worker import cursor


def _readline(stream, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise AssertionError("adapter did not respond")
    line = stream.readline()
    if not line:
        raise AssertionError("adapter closed stdout")
    return json.loads(line)


class Session:
    """One stdio MCP process."""

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
    """Unix socket that counts dials and forwards them to the worker.

    `hold_next` accepts one connection and writes nothing, so the
    client hits its own timeout. `rewrite_next` changes one request's
    method before the worker sees it.
    """

    def __init__(self, directory, upstream):
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / "worker.sock"
        self.upstream = str(upstream)
        self.accepted = 0
        self.requests = []
        self.hold_next = False
        self.rewrite_next = None
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
                hold = self.hold_next
                self.hold_next = False
                rewrite = self.rewrite_next
                self.rewrite_next = None
            if hold:
                client.settimeout(None)
                try:
                    while client.recv(65536):
                        pass
                except OSError:
                    return
                return
            if rewrite is not None:
                message = json.loads(raw)
                message["method"] = rewrite
                raw = (canonical(message) + "\n").encode()
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


class McpErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-err-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.sessions = []
        self.worker = self.start_worker()
        self.proxies = []
        self.proxy = self.proxy_to(self.root / "proxy")

    def proxy_to(self, directory):
        opened = CountingProxy(directory, self.state / "worker.sock")
        self.proxies.append(opened)
        return opened

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for proxy in self.proxies:
            proxy.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def start_worker(self):
        process = subprocess.Popen(
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            try:
                call(self.state, "worker.describe", {})
                return process
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def session(self, state=None):
        opened = Session(self.proxy.path.parent if state is None else state)
        self.sessions.append(opened)
        return opened

    def submit_fixture(self, key, exit_code=0, output="short"):
        reply = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": key,
                "backend": "development",
                "fixture": {"exit_code": exit_code, "output": output},
            },
        )
        run_id = reply["result"]["run_id"]
        want = "succeeded" if exit_code == 0 else "failed"
        deadline = time.monotonic() + 5
        record = None
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": run_id})["result"]
            if record["state"] == want:
                return record
            time.sleep(0.01)
        self.fail(f"fixture did not reach {want}: {record}")

    def store_lost(self, record):
        database = sqlite3.connect(self.state / "runs.sqlite3", timeout=5)
        try:
            row = database.execute(
                "SELECT record FROM runs WHERE id=?", (record["run_id"],)
            ).fetchone()
            stored = json.loads(row[0])
            stored["state"] = "lost"
            stored["exit_code"] = None
            stored["error"] = {
                "kind": "WORKER_INTERRUPTED",
                "message": "worker stopped during execution",
            }
            stored["cleanup"] = "confirmed_no_external_resources"
            database.execute(
                "UPDATE runs SET record=? WHERE id=?",
                (canonical(stored), record["run_id"]),
            )
            database.commit()
        finally:
            database.close()

    def assert_later_get(self, session, run_id, state):
        again = session.call("get", {"run_id": run_id})
        self.assertIs(again["isError"], False)
        self.assertEqual(again["structuredContent"]["state"], state)
        self.assertEqual(again["structuredContent"]["run_id"], run_id)

    def test_malformed_tool_call_keeps_the_session(self):
        record = self.submit_fixture("alive")
        session = self.session()
        before = self.proxy.accepted
        reply = session.request("tools/call", ["get"])
        result = reply["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"]["kind"], "INVALID_PARAMS")
        self.assertIs(result["structuredContent"]["retryable"], False)
        self.assertEqual(self.proxy.accepted, before)
        self.assert_later_get(session, record["run_id"], "succeeded")

    def test_local_rejections_do_not_dial(self):
        record = self.submit_fixture("local")
        session = self.session()
        before = self.proxy.accepted
        rejected = [
            session.call("submit", {"run_id": record["run_id"]}),
            session.call("get", {}),
            session.call("logs", {"run_id": record["run_id"], "cursor": ""}),
            session.call("logs", {"run_id": record["run_id"], "cursor": "x" * 1025}),
            session.call("logs", {"run_id": record["run_id"], "cursor": 1}),
        ]
        for result in rejected:
            self.assertIs(result["isError"], True)
            self.assertEqual(result["structuredContent"]["kind"], "INVALID_PARAMS")
            self.assertIs(result["structuredContent"]["retryable"], False)
            self.assertEqual(
                result["content"],
                [{"type": "text", "text": canonical(result["structuredContent"])}],
            )
        self.assertEqual(self.proxy.accepted, before)
        self.assert_later_get(session, record["run_id"], "succeeded")

    def test_worker_answered_outcomes(self):
        succeeded = self.submit_fixture("ok")
        failed = self.submit_fixture("bad", exit_code=1, output="nope")
        lost = self.submit_fixture("gone")
        self.store_lost(lost)
        absent = self.root / "absent"
        absent.mkdir(mode=0o700)
        absent_session = self.session(absent)
        missing = absent_session.call("get", {"run_id": succeeded["run_id"]})
        self.assertIs(missing["isError"], True)
        self.assertEqual(missing["structuredContent"]["kind"], "WORKER_UNAVAILABLE")
        self.assertNotEqual(missing["structuredContent"]["kind"], "RUN_NOT_FOUND")
        self.assertNotIn("/", missing["structuredContent"]["message"])
        self.proxy_to(absent)
        self.assert_later_get(absent_session, succeeded["run_id"], "succeeded")

        # One read is shown to the worker as run.artifacts. A development
        # fixture answers CAPABILITY_UNSUPPORTED, and the adapter copies it.
        refused = call(self.state, "run.artifacts", {"run_id": succeeded["run_id"]})
        self.assertEqual(refused["error"]["data"]["kind"], "CAPABILITY_UNSUPPORTED")
        self.proxy.rewrite_next = "run.artifacts"
        session = self.session()
        copied = session.call("get", {"run_id": succeeded["run_id"]})
        self.assertIs(copied["isError"], True)
        self.assertEqual(copied["structuredContent"]["kind"], "CAPABILITY_UNSUPPORTED")
        self.assertEqual(copied["structuredContent"]["message"], refused["error"]["message"])
        self.assertIs(
            copied["structuredContent"]["retryable"], refused["error"]["data"]["retryable"]
        )
        self.assert_later_get(session, succeeded["run_id"], "succeeded")

        failed_get = session.call("get", {"run_id": failed["run_id"]})
        self.assertIs(failed_get["isError"], False)
        self.assertEqual(failed_get["structuredContent"]["state"], "failed")
        self.assertNotEqual(failed_get["structuredContent"]["state"], "lost")
        self.assertEqual(failed_get["structuredContent"]["exit_code"], 1)

        lost_get = session.call("get", {"run_id": lost["run_id"]})
        self.assertIs(lost_get["isError"], False)
        self.assertEqual(lost_get["structuredContent"]["state"], "lost")
        self.assertNotEqual(lost_get["structuredContent"]["state"], "succeeded")
        self.assertIsNone(lost_get["structuredContent"]["exit_code"])

    def test_foreign_and_past_cursors_expire(self):
        first = self.submit_fixture("one", output="aaaa")
        second = self.submit_fixture("two", output="bbbb")
        stolen = call(self.state, "run.logs", {"run_id": first["run_id"], "limit": 64})["result"][
            "next_cursor"
        ]
        session = self.session()
        foreign = session.call("logs", {"run_id": second["run_id"], "cursor": stolen})
        socket_foreign = call(
            self.state, "run.logs", {"run_id": second["run_id"], "cursor": stolen}
        )
        self.assertIs(foreign["isError"], True)
        self.assertEqual(foreign["structuredContent"]["kind"], "CURSOR_EXPIRED")
        self.assertEqual(
            foreign["structuredContent"]["kind"], socket_foreign["error"]["data"]["kind"]
        )
        self.assertEqual(
            foreign["structuredContent"]["message"], socket_foreign["error"]["message"]
        )
        self.assertIs(foreign["structuredContent"]["retryable"], False)
        self.assert_later_get(session, second["run_id"], "succeeded")

        late = cursor("logs", second["run_id"], len(b"bbbb") + 1)
        past = session.call("logs", {"run_id": second["run_id"], "cursor": late})
        socket_past = call(self.state, "run.logs", {"run_id": second["run_id"], "cursor": late})
        self.assertIs(past["isError"], True)
        self.assertEqual(past["structuredContent"]["kind"], "CURSOR_EXPIRED")
        self.assertEqual(past["structuredContent"]["kind"], socket_past["error"]["data"]["kind"])
        self.assertIs(past["structuredContent"]["retryable"], False)
        self.assert_later_get(session, second["run_id"], "succeeded")

    def test_a_timed_out_read_is_not_sent_again(self):
        record = self.submit_fixture("slow")
        session = self.session()
        before = len(self.proxy.requests)
        self.proxy.hold_next = True
        started = time.monotonic()
        result = session.call("logs", {"run_id": record["run_id"], "limit": 64}, timeout=12)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 8)
        self.assertEqual(len(self.proxy.requests), before + 1)
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"]["kind"], "WORKER_TIMEOUT")
        self.assertIs(result["structuredContent"]["retryable"], True)
        self.assertNotIn("/", result["structuredContent"]["message"])
        self.assert_later_get(session, record["run_id"], "succeeded")
