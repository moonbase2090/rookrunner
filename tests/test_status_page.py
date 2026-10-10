import contextlib
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from execution_core.protocol import canonical
from execution_core.worker import Worker


TIP = "cd" * 20
STARTED = "2026-10-09T00:00:00+00:00"
FINISHED = "2026-10-09T00:00:12+00:00"
CHECK_HREF = "https://github.com/moonbase2090/lightwell/runs/4"
GET = b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n"


def _page():
    from execution_core import status_page

    return status_page


def _record(run_id, *, state, started_at, finished_at, exit_code, concurrency=None):
    return {
        "run_id": run_id,
        "worker_id": "00000000-0000-4000-8000-0000000000aa",
        "submission_key": run_id,
        "state": state,
        "exit_code": exit_code,
        "input": {
            "kind": "workflow_job",
            "digest": "11" * 32,
            "snapshot_id": "22" * 32,
            "workflow": ".github/workflows/rookrunner.yml",
            "workflow_digest": "33" * 32,
            "plan_digest": "44" * 32,
            "job_id": "checks",
            "event_digest": "55" * 32,
            "image_digest": "sha256:" + "66" * 32,
            "image_reference": "example@sha256:" + "66" * 32,
        },
        "accepted_at": STARTED,
        "started_at": started_at,
        "finished_at": finished_at,
        "concurrency": [] if concurrency is None else concurrency,
    }


def _request(method, target, host="127.0.0.1:8765"):
    return f"{method} {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()


class _FakeSocket:
    """Records worker methods. The page must not call it for a rejected request."""

    def __init__(self, error=None):
        self.methods = []
        self._error = error

    def __call__(self, state, method, params):
        self.methods.append(method)
        if self._error is not None:
            raise self._error
        return {"jsonrpc": "2.0", "id": 1, "result": {}}


def _read_http(sock):
    sock.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    length = None
    for line in head.decode().split("\r\n")[1:]:
        name, value = line.split(":", 1)
        if name.lower() == "content-length":
            length = int(value.strip())
    if length is None:
        raise AssertionError("response has no Content-Length")
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head.decode(), rest[:length].decode()


class StatusPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.worker = None
        self.listener = None
        self._accept_thread = None

    def tearDown(self):
        if self.worker is not None:
            self.worker.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        if self.listener is not None:
            self.listener.close()
        self.tmp.cleanup()

    def start_worker(self):
        self.worker = Worker(self.repo, self.state)
        self.worker.start()
        self.worker.stop.set()
        self.worker.scheduler.join(timeout=5)
        if self.worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        self.worker.stop.clear()

        def loop():
            while not self.worker.stop.is_set():
                try:
                    client, _addr = self.worker.server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with client:
                    client.settimeout(1)
                    try:
                        with client.makefile("rb") as stream:
                            raw = stream.readline(1024 * 1024 + 1)
                        reply = self.worker.response(raw)
                        client.sendall((canonical(reply) + "\n").encode())
                    except (OSError, ValueError):
                        pass

        self._accept_thread = threading.Thread(target=loop, daemon=True)
        self._accept_thread.start()

    def insert(self, record, request, checks=()):
        with self.worker.db:
            self.worker.db.execute(
                "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                (
                    record["run_id"],
                    record["submission_key"],
                    canonical(request),
                    canonical(record),
                    b"",
                ),
            )
            for sha, check_id in checks:
                self.worker.db.execute(
                    "INSERT INTO check_posts(run_id, context, sha, check_run_id, status, conclusion) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record["run_id"],
                        "rookrunner/rookrunner.yml/checks",
                        sha,
                        check_id,
                        "completed",
                        "success",
                    ),
                )
        return record["run_id"]

    def exchange(self, request, state=None):
        left, right = socket.socketpair()
        left.settimeout(5)
        right.settimeout(5)
        try:
            right.sendall(request)
            right.shutdown(socket.SHUT_WR)
            _page().handle(left, self.state if state is None else state)
            return _read_http(right)
        finally:
            left.close()
            right.close()

    def test_the_bound_socket_is_loopback_port_8765(self):
        sock = _page().bind_listener()
        self.listener = sock
        address, port = sock.getsockname()[:2]
        self.assertEqual(address, "127.0.0.1")
        self.assertEqual(port, 8765)
        self.assertEqual(sock.family, socket.AF_INET)
        self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)

    def test_a_second_bind_exits_without_a_path(self):
        self.listener = _page().bind_listener()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            _page().bind_listener()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(err.getvalue(), "status page could not bind 127.0.0.1:8765\n")

    def test_get_renders_a_finished_twelve_second_run(self):
        run_id = str(uuid.uuid4())
        self.start_worker()
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {
                "event_name": "push",
                "event": {"after": TIP, "repository": {"full_name": "moonbase2090/lightwell"}},
            },
            checks=((TIP, 4),),
        )
        seen = []
        real = __import__("execution_core.cli", fromlist=["call"]).call

        def wrapped(state, method, params):
            seen.append(method)
            return real(state, method, params)

        with mock.patch("execution_core.cli.call", wrapped):
            head, body = self.exchange(GET)
        self.assertEqual(seen, ["status.view"])
        self.assertIn("0.0.1", body)
        self.assertIn("12s", body)
        self.assertIn("succeeded", body)
        self.assertIn("<td>succeeded</td><td>0</td><td>12s</td>", body)
        self.assertIn(f'href="{CHECK_HREF}"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn('content="5"', body)
        self.assertIn("<dt>Last poll</dt><dd>not recorded</dd>", body)
        self.assertIn("<dt>Poll repository</dt><dd>not recorded</dd>", body)
        self.assertIn("<dt>Runner image</dt><dd>not set</dd>", body)
        self.assertIn("<dt>Ready</dt><dd>true</dd>", body)
        self.assertNotIn("<form", body)
        self.assertNotIn("<button", body)
        self.assertNotIn("run.cancel", body)
        self.assertNotIn(str(self.worker.state), body)
        self.assertNotIn(str(self.worker.repository), body)
        self.assertNotIn("Access-Control-Allow-Origin", head)
        self.assertNotIn("Set-Cookie", head)
        self.assertNotRegex(head, r"(?im)^server:")
        self.assertIn("Content-Type: text/html; charset=utf-8", head)
        self.assertIn("Cache-Control: no-store", head)
        self.assertIn("Connection: close", head)

    def test_post_is_405_and_does_not_call_the_socket(self):
        fake = _FakeSocket()
        with mock.patch("execution_core.cli.call", fake):
            head, body = self.exchange(_request("POST", "/"))
        self.assertIn("405", head.split("\r\n", 1)[0])
        self.assertEqual(body, "")
        self.assertEqual(fake.methods, [])
        self.assertNotIn("Content-Type", head)
        self.assertNotIn("Access-Control-Allow-Origin", head)

    def test_a_query_string_is_400(self):
        fake = _FakeSocket()
        with mock.patch("execution_core.cli.call", fake):
            head, body = self.exchange(b"GET /?x HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n")
        self.assertIn("400", head.split("\r\n", 1)[0])
        self.assertEqual(body, "")
        self.assertEqual(fake.methods, [])

    def test_a_localhost_host_is_400(self):
        fake = _FakeSocket()
        with mock.patch("execution_core.cli.call", fake):
            head, body = self.exchange(_request("GET", "/", host="localhost:8765"))
        self.assertIn("400", head.split("\r\n", 1)[0])
        self.assertEqual(body, "")
        self.assertEqual(fake.methods, [])

    def test_a_missing_socket_is_down_and_ignores_a_planted_database(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        self.state.mkdir(mode=0o700)
        database = self.state / "runs.sqlite3"
        db = sqlite3.connect(database)
        db.execute("CREATE TABLE runs (id TEXT, record TEXT)")
        db.execute("INSERT INTO runs VALUES (?, ?)", (run_id, run_id))
        db.commit()
        db.close()
        head, body = self.exchange(GET)
        self.assertIn("200", head.split("\r\n", 1)[0])
        self.assertIn("<dt>Worker</dt><dd>down</dd>", body)
        self.assertIn("not available", body)
        self.assertNotIn(run_id, body)
        self.assertNotIn(str(self.state), body)

    def test_a_timeout_is_busy_and_not_down(self):
        fake = _FakeSocket(error=TimeoutError("slow"))
        with mock.patch("execution_core.cli.call", fake):
            head, body = self.exchange(GET)
        self.assertIn("200", head.split("\r\n", 1)[0])
        self.assertEqual(fake.methods, ["status.view"])
        self.assertIn("<dt>Worker</dt><dd>busy</dd>", body)
        self.assertNotIn("<dt>Worker</dt><dd>down</dd>", body)
        self.assertIn("not available", body)
        self.assertNotIn("0.0.1", body)
        self.assertIn('content="5"', head + body)
        self.assertNotIn("Access-Control-Allow-Origin", head)

    def test_a_group_named_main_is_escaped(self):
        self.start_worker()
        self.insert(
            _record(
                str(uuid.uuid4()),
                state="queued",
                started_at=None,
                finished_at=None,
                exit_code=None,
                concurrency=[{"group": "<main>", "queue": "max", "cancel_in_progress": False}],
            ),
            {},
        )
        _head, body = self.exchange(GET)
        self.assertIn("&lt;main&gt;", body)
        self.assertNotIn("<main>", body)
        self.assertNotIn("<form", body)
        self.assertNotIn("<button", body)

    def test_an_absolute_group_is_not_shown(self):
        self.start_worker()
        state_path = str(self.worker.state)
        self.insert(
            _record(
                str(uuid.uuid4()),
                state="queued",
                started_at=None,
                finished_at=None,
                exit_code=None,
                concurrency=[{"group": state_path, "queue": "single", "cancel_in_progress": False}],
            ),
            {},
        )
        _head, body = self.exchange(GET)
        self.assertIn("not shown", body)
        self.assertNotIn(state_path, body)

    def test_a_relative_ref_group_is_shown(self):
        self.start_worker()
        self.insert(
            _record(
                str(uuid.uuid4()),
                state="queued",
                started_at=None,
                finished_at=None,
                exit_code=None,
                concurrency=[
                    {"group": "refs/heads/main", "queue": "single", "cancel_in_progress": False}
                ],
            ),
            {},
        )
        _head, body = self.exchange(GET)
        self.assertIn("refs/heads/main", body)

    def test_the_parser_has_no_host_or_port_argument(self):
        env = os.environ.copy()
        root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(root / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "status-page",
                "--help",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--host", proc.stdout)
        self.assertNotIn("--port", proc.stdout)
        self.assertNotIn("--repository", proc.stdout)


class StatusPageSourceTests(unittest.TestCase):
    def test_the_module_does_not_name_the_database_or_poll_file(self):
        text = Path(_page().__file__).read_text()
        self.assertNotIn("sqlite3", text)
        self.assertNotIn("runs.sqlite3", text)
        self.assertNotIn("poll.json", text)
