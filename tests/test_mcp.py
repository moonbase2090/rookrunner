# SPDX-License-Identifier: MPL-2.0

"""Read tools on the stdio MCP adapter.

The adapter is a client of one worker socket. These tests cover
describe, get, list, and logs. Submit, status, and fixtures stay
on the CLI.
"""

import base64
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from execution_core.cli import call
from execution_core.protocol import MAX_LOG_PAGE, canonical


def _readline(stream, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise AssertionError("adapter did not respond")
    line = stream.readline()
    if not line:
        raise AssertionError("adapter closed stdout")
    return json.loads(line)


class Session:
    """One stdio MCP process. Each request reads one response line."""

    def __init__(self, state):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "execution_core", "--state", str(state), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.request_id = 0

    def request(self, method, params=None):
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        return _readline(self.process.stdout, 5)

    def call(self, name, arguments=None):
        reply = self.request(
            "tools/call", {"name": name, "arguments": arguments if arguments is not None else {}}
        )
        return reply["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.terminate()
        stderr = self.process.stderr.read()
        self.process.wait(timeout=5)
        return stderr


class McpReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-read-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.sessions = []
        self.worker = self.start_worker()

    def tearDown(self):
        for session in self.sessions:
            session.close()
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
        opened = Session(self.state if state is None else state)
        self.sessions.append(opened)
        return opened

    def submit_fixture(self, key="fixture"):
        reply = call(
            self.state,
            "run.submit",
            {
                "version": 0,
                "submission_key": key,
                "backend": "development",
                "fixture": {"exit_code": 0, "output": "short"},
            },
        )
        run_id = reply["result"]["run_id"]
        deadline = time.monotonic() + 5
        record = None
        while time.monotonic() < deadline:
            record = call(self.state, "run.get", {"run_id": run_id})["result"]
            if record["state"] == "succeeded":
                return record
            time.sleep(0.01)
        self.fail(f"fixture did not succeed: {record}")

    def test_get_matches_the_socket_and_a_second_call_succeeds(self):
        record = self.submit_fixture()
        socket_get = call(self.state, "run.get", {"run_id": record["run_id"]})
        socket_logs = call(self.state, "run.logs", {"run_id": record["run_id"], "limit": 64})
        session = self.session()
        got = session.call("get", {"run_id": record["run_id"]})
        self.assertIs(got["isError"], False)
        self.assertEqual(got["structuredContent"], socket_get["result"])
        self.assertEqual(
            got["content"], [{"type": "text", "text": canonical(socket_get["result"])}]
        )
        logged = session.call("logs", {"run_id": record["run_id"], "limit": 64})
        self.assertIs(logged["isError"], False)
        self.assertEqual(logged["structuredContent"], socket_logs["result"])
        self.assertEqual(
            logged["content"], [{"type": "text", "text": canonical(socket_logs["result"])}]
        )

    def test_exiting_the_adapter_leaves_the_run_state(self):
        record = self.submit_fixture("stay")
        session = self.session()
        session.call("get", {"run_id": record["run_id"]})
        session.close()
        self.sessions.remove(session)
        again = call(self.state, "run.get", {"run_id": record["run_id"]})["result"]
        self.assertEqual(again["state"], record["state"])
        self.assertEqual(again["run_id"], record["run_id"])
        self.assertEqual(again["exit_code"], record["exit_code"])

    def test_missing_socket_is_not_run_not_found(self):
        record = self.submit_fixture("known")
        absent = self.root / "absent"
        absent.mkdir()
        session = self.session(absent)
        missing = session.call("get", {"run_id": record["run_id"]})
        self.assertIs(missing["isError"], True)
        self.assertEqual(missing["structuredContent"]["kind"], "WORKER_UNAVAILABLE")
        self.assertNotEqual(missing["structuredContent"]["kind"], "RUN_NOT_FOUND")
        present = self.session()
        unknown = present.call("get", {"run_id": "missing"})
        self.assertIs(unknown["isError"], True)
        self.assertEqual(unknown["structuredContent"]["kind"], "RUN_NOT_FOUND")

    def test_a_long_log_pages_match_the_cli_across_connections(self):
        page_limit = 64 * 1024
        self.assertEqual(MAX_LOG_PAGE, page_limit)
        record = self.submit_fixture("paged")
        payload = b"L" * (page_limit + 100)
        database = sqlite3.connect(self.state / "runs.sqlite3")
        try:
            database.execute("UPDATE runs SET log=? WHERE id=?", (payload, record["run_id"]))
            database.commit()
        finally:
            database.close()
        first = self.session()
        opened = first.call("logs", {"run_id": record["run_id"], "limit": page_limit})
        page = opened["structuredContent"]
        self.assertIs(opened["isError"], False)
        self.assertEqual(len(base64.b64decode(page["data_base64"])), page_limit)
        cursor = page["next_cursor"]
        self.assertIsInstance(cursor, str)
        self.assertFalse(page["end_of_stream"])
        socket_page = call(
            self.state,
            "run.logs",
            {"run_id": record["run_id"], "limit": page_limit},
        )
        self.assertEqual(page, socket_page["result"])
        second = self.session()
        followed = second.call(
            "logs",
            {"run_id": record["run_id"], "cursor": cursor, "limit": page_limit},
        )
        self.assertIs(followed["isError"], False)
        chunks = [
            base64.b64decode(page["data_base64"]),
            base64.b64decode(followed["structuredContent"]["data_base64"]),
        ]
        self.assertEqual(b"".join(chunks), payload)
        self.assertTrue(followed["structuredContent"]["end_of_stream"])
        cli_chunks = []
        cli_cursor = None
        while True:
            command = [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "logs",
                record["run_id"],
                "--limit",
                str(page_limit),
            ]
            if cli_cursor is not None:
                command.extend(["--cursor", cli_cursor])
            completed = subprocess.run(command, check=True, capture_output=True)
            body = json.loads(completed.stdout)["result"]
            self.assertLessEqual(len(base64.b64decode(body["data_base64"])), page_limit)
            cli_chunks.append(base64.b64decode(body["data_base64"]))
            cli_cursor = body["next_cursor"]
            if body["end_of_stream"]:
                break
        self.assertEqual(b"".join(cli_chunks), payload)

    def test_tool_table_lists_submit_and_omits_status_and_fixtures(self):
        session = self.session()
        listed = session.request("tools/list")
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "describe",
                "get",
                "list",
                "logs",
                "artifacts",
                "artifact_read",
                "submit",
                "cancel",
            ],
        )
        self.assertFalse({"run.status", "status", "fixture"} & set(names))
        described = session.call("describe")
        self.assertIs(described["isError"], False)
        self.assertIn("run.status", described["structuredContent"]["methods"])
        self.assertIn("development.fixture", described["structuredContent"]["capabilities"])
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["dependencies"], ["pyyaml==6.0.3"])
        self.assertEqual(project["project"]["requires-python"], ">=3.11")

    def test_mcp_command_takes_no_app_key(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "mcp",
                "--app-key",
                "unused",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments", completed.stderr)


if __name__ == "__main__":
    unittest.main()
