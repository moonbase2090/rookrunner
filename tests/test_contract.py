import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from execution_core.cli import call
from execution_core.worker import MAX_MESSAGE
from schema_support import validate_response, validator


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.worker = self.start_worker()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def spawn(self, repository=None):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "worker",
                "--repository",
                str(repository or self.repo),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    def start_worker(self):
        process = self.spawn()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            try:
                self.rpc("worker.describe", {})
                return process
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def rpc(self, method, params):
        validator("Request").validate(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        reply = call(self.state, method, params)
        validate_response(method, reply)
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def submit(self, key="test", **fixture):
        return self.rpc("run.submit", self.submission(key, **fixture))

    @staticmethod
    def submission(key="test", **fixture):
        return {"version": 0, "submission_key": key, "backend": "development", "fixture": fixture}

    def await_state(self, run_id, states):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": run_id})
            if record["state"] in states:
                return record
            time.sleep(0.01)
        self.fail(f"run did not reach {states}: {record}")

    def test_terminal_result_and_immutable_inputs(self):
        run = self.submit(output="hello\x00🌙", delay_ms=100)
        (self.repo / "changed.txt").write_text("edited after submission")
        result = self.await_state(run["run_id"], {"succeeded"})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["input"], run["input"])
        self.assertEqual(result["input"]["kind"], "development_fixture")
        self.assertIsNotNone(result["attempt_id"])
        data, cursor = bytearray(), None
        while True:
            params = {"run_id": run["run_id"], "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = self.rpc("run.logs", params)
            data.extend(base64.b64decode(page["data_base64"]))
            cursor = page["next_cursor"]
            if page["end_of_stream"]:
                break
        self.assertEqual(bytes(data), "hello\x00🌙".encode())

    def test_nonzero_is_failed(self):
        run = self.submit(exit_code=7)
        result = self.await_state(run["run_id"], {"failed"})
        self.assertEqual(result["exit_code"], 7)

    def test_duplicate_normalization_and_conflict(self):
        run = self.submit()
        retry = self.submit(exit_code=0, delay_ms=0, output="development fixture completed\n")
        self.assertEqual(run["run_id"], retry["run_id"])
        reply = call(self.state, "run.submit", self.submission(output="different"))
        self.assertEqual(reply["error"]["data"]["kind"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 1)

    def test_disconnect_after_send_and_reconnect(self):
        params = self.submission(delay_ms=100)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.state / "worker.sock"))
            connection.sendall(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 9, "method": "run.submit", "params": params}
                    )
                    + "\n"
                ).encode()
            )
        # Observe durable acceptance without resubmitting, so this proves the
        # disconnected first client created work before its retry.
        runs = self.rpc("run.list", {})["runs"]
        self.assertEqual(len(runs), 1)
        run = self.rpc("run.submit", params)
        self.assertEqual(run["run_id"], runs[0]["run_id"])
        self.await_state(run["run_id"], {"succeeded"})
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 1)

    def test_invalid_requests_never_create_work(self):
        requests = [
            self.submission(delay_ms=-1),
            self.submission(exit_code=True),
            self.submission(output=42),
            self.submission(command="touch arbitrary"),
            {**self.submission(), "version": 1},
            {**self.submission(), "backend": "shell"},
            {**self.submission(), "extra": "ignored?"},
        ]
        expected = (
            ("INVALID_PARAMS", -32602),
            ("INVALID_PARAMS", -32602),
            ("INVALID_PARAMS", -32602),
            ("INVALID_PARAMS", -32602),
            ("INVALID_PARAMS", -32602),
            ("CAPABILITY_UNSUPPORTED", -32000),
            ("INVALID_PARAMS", -32602),
        )
        for params, (kind, code) in zip(requests, expected, strict=True):
            self.assert_fault(call(self.state, "run.submit", params), kind, code)
        self.assertEqual(self.rpc("run.list", {})["runs"], [])
        self.assert_fault(call(self.state, "run.list", {"limit": True}), "INVALID_PARAMS", -32602)
        self.assert_fault(call(self.state, "run.list", {"state": []}), "INVALID_PARAMS", -32602)

    def test_socket_permissions_and_single_owner(self):
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "worker.sock").stat().st_mode), 0o600)
        worker_id = self.rpc("worker.describe", {})["worker_id"]
        other = self.spawn()
        other.communicate(timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(self.rpc("worker.describe", {})["worker_id"], worker_id)

    def test_restart_marks_active_lost_and_preserves_queue_and_keys(self):
        running = self.submit("active", delay_ms=5000)
        self.await_state(running["run_id"], {"running"})
        queued = self.submit("waiting")
        self.assertEqual(queued["state"], "queued")
        self.worker.kill()
        self.worker.communicate(timeout=5)
        # The old socket remains after SIGKILL; startup must reconcile it under lock.
        self.worker = self.start_worker()
        lost = self.rpc("run.get", {"run_id": running["run_id"]})
        self.assertEqual(lost["state"], "lost")
        self.assertIsNone(lost["exit_code"])
        self.assertEqual(self.submit("active", delay_ms=5000)["run_id"], running["run_id"])
        self.await_state(queued["run_id"], {"succeeded"})
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 2)

    def test_cancel_queued_and_running_and_terminal_authority(self):
        running = self.submit("active", delay_ms=400)
        self.await_state(running["run_id"], {"running"})
        queued = self.submit("waiting")
        for run in (queued, running):
            result = self.rpc("run.cancel", {"version": 0, "run_id": run["run_id"]})
            self.assertEqual(result["state"], "cancelled")
            self.assertIsNone(result["exit_code"])
        time.sleep(0.5)
        self.assertEqual(self.rpc("run.get", {"run_id": running["run_id"]})["state"], "cancelled")
        finished = self.submit("finished")
        final = self.await_state(finished["run_id"], {"succeeded"})
        self.assertEqual(
            self.rpc("run.cancel", {"version": 0, "run_id": finished["run_id"]}), final
        )

    def test_log_end_and_cursor_scope(self):
        first = self.submit("first", delay_ms=100)
        page = self.rpc("run.logs", {"run_id": first["run_id"]})
        self.assertFalse(page["end_of_stream"])
        other = self.submit("other")
        reply = call(
            self.state, "run.logs", {"run_id": other["run_id"], "cursor": page["next_cursor"]}
        )
        self.assertEqual(reply["error"]["data"]["kind"], "CURSOR_EXPIRED")
        self.assert_fault(
            call(self.state, "run.logs", {"run_id": first["run_id"], "limit": 65537}),
            "INVALID_PARAMS",
            -32602,
        )

    def test_pagination(self):
        ids = [self.submit(str(i))["run_id"] for i in range(3)]
        page = self.rpc("run.list", {"limit": 2})
        self.assertEqual([r["run_id"] for r in page["runs"]], ids[:2])
        next_page = self.rpc("run.list", {"limit": 2, "cursor": page["next_cursor"]})
        self.assertEqual([r["run_id"] for r in next_page["runs"]], ids[2:])
        self.assertIsNone(next_page["next_cursor"])

    def test_binding_and_unsafe_state_directory(self):
        self.worker.terminate()
        self.worker.communicate(timeout=5)
        other_repo = self.root / "other"
        other_repo.mkdir()
        other = self.spawn(other_repo)
        self.assertIn(b"ROOT_MISMATCH", other.communicate(timeout=5)[1])
        self.assertNotEqual(other.returncode, 0)
        self.state.chmod(0o755)
        unsafe = self.spawn()
        self.assertIn(b"0700", unsafe.communicate(timeout=5)[1])
        self.assertNotEqual(unsafe.returncode, 0)

    def test_malformed_and_oversized_transport(self):
        self.assertEqual(MAX_MESSAGE, 1024 * 1024)
        cases = (
            (b"{broken\n", "PARSE_ERROR", -32700),
            (b"[]\n", "INVALID_REQUEST", -32600),
            (b"x" * (1024 * 1024) + b"\n", "INVALID_REQUEST", -32600),
        )
        for raw, kind, code in cases:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(5)
                connection.connect(str(self.state / "worker.sock"))
                connection.sendall(raw)
                with connection.makefile("rb") as stream:
                    reply = json.loads(stream.readline())
            self.assert_fault(reply, kind, code)
        self.assertEqual(self.rpc("run.list", {})["runs"], [])

    def test_cli_json_and_failure_exit(self):
        command = [sys.executable, "-m", "execution_core", "--state", str(self.state)]
        result = subprocess.run(
            [*command, "submit", "--backend", "development", "--key", "cli"], capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = json.loads(result.stdout)["result"]["run_id"]
        self.await_state(run_id, {"succeeded"})
        result = subprocess.run([*command, "get", run_id], capture_output=True)
        self.assertEqual(json.loads(result.stdout)["result"]["exit_code"], 0)
        result = subprocess.run([*command, "get", "nonexistent"], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["data"]["kind"], "RUN_NOT_FOUND")

    def raw_rpc(self, raw):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            connection.connect(str(self.state / "worker.sock"))
            connection.sendall(raw)
            connection.shutdown(socket.SHUT_WR)
            with connection.makefile("rb") as stream:
                wire = stream.readline(1024 * 1024 + 1)
        self.assertLessEqual(len(wire), 1024 * 1024)
        reply = json.loads(wire)
        validator("Response").validate(reply)
        return reply

    def assert_fault(self, reply, kind, code=-32000):
        validator("ErrorResponse").validate(reply)
        self.assertEqual(reply["error"]["data"]["kind"], kind)
        self.assertEqual(reply["error"]["code"], code)

    def test_wire_rejections_have_stable_errors_and_no_work(self):
        for raw in [
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"worker.describe"}\n',
            b'{"jsonrpc":"2.0","id":NaN,"method":"worker.describe"}\n',
            b'{"jsonrpc":"2.0","id":1e999,"method":"worker.describe"}\n',
            '{"jsonrpc":"2.0"}\n'.encode("utf-16"),
            b'"\xff"\n',
            b"[" * 2000 + b"0" + b"]" * 2000 + b"\n",
        ]:
            self.assert_fault(self.raw_rpc(raw), "PARSE_ERROR", -32700)
        for envelope in [
            [],
            {},
            {"jsonrpc": "2.0", "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": None, "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": True, "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": "x" * 129, "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": 2**53, "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": "\ud800", "method": "worker.describe"},
            {"jsonrpc": "2.0", "id": 1, "method": "worker.describe", "extra": 0},
        ]:
            self.assert_fault(
                self.raw_rpc((json.dumps(envelope) + "\n").encode()), "INVALID_REQUEST", -32600
            )
        for method, params, kind, code in [
            ("worker.describe", [], "INVALID_PARAMS", -32602),
            ("worker.describe", None, "INVALID_PARAMS", -32602),
            ("unknown", {}, "METHOD_NOT_FOUND", -32601),
            ("run.artifacts", {}, "INVALID_PARAMS", -32602),
            ("artifact.read", {}, "INVALID_PARAMS", -32602),
            ("run.submit", self.submission(output="\ud800"), "INVALID_PARAMS", -32602),
            ("run.submit", self.submission(key="\ud800"), "INVALID_PARAMS", -32602),
            ("run.submit", {**self.submission(), "version": True}, "VERSION_UNSUPPORTED", -32000),
            ("run.get", {"run_id": "\ud800"}, "INVALID_PARAMS", -32602),
        ]:
            with self.subTest(method=method, params=params):
                self.assert_fault(call(self.state, method, params), kind, code)
        self.assertEqual(self.rpc("run.list", {})["runs"], [])

    def test_exact_frame_boundary_and_missing_newline(self):
        self.assertEqual(MAX_MESSAGE, 1024 * 1024)
        request = b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}'
        boundary = request + b" " * (1024 * 1024 - len(request) - 1) + b"\n"
        self.assertIn("result", self.raw_rpc(boundary))
        self.assert_fault(self.raw_rpc(boundary[:-1] + b" \n"), "INVALID_REQUEST", -32600)
        self.assert_fault(self.raw_rpc(request), "INVALID_REQUEST", -32600)
        self.assertTrue(self.rpc("worker.describe", {})["ready"])

    def test_utf8_byte_boundary_and_full_log_page(self):
        output = "🌙" * (65536 // 4)
        run = self.submit(output=output)
        self.await_state(run["run_id"], {"succeeded"})
        page = self.rpc("run.logs", {"run_id": run["run_id"], "limit": 65536})
        self.assertEqual(base64.b64decode(page["data_base64"]), output.encode())
        self.assertTrue(page["end_of_stream"])
        params = self.submission("oversized", output=output + "a")
        self.assertFalse(
            validator("Request").is_valid(
                {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
            )
        )
        self.assert_fault(call(self.state, "run.submit", params), "INVALID_PARAMS", -32602)

    def test_integral_numbers_normalize_for_idempotency(self):
        params = self.submission(exit_code=0.0, delay_ms=0.0)
        params["version"] = 0.0
        run = self.rpc("run.submit", params)
        self.assertEqual(self.submit()["run_id"], run["run_id"])
        reply = self.raw_rpc(b'{"jsonrpc":"2.0","id":1.0,"method":"worker.describe"}\n')
        self.assertEqual(reply["id"], 1.0)
        self.assert_fault(
            call(self.state, "run.submit", self.submission("fraction", exit_code=0.5)),
            "INVALID_PARAMS",
            -32602,
        )

    def test_concurrent_retries_remain_one_run(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(
                pool.map(
                    lambda _: call(self.state, "run.submit", self.submission(delay_ms=200)),
                    range(16),
                )
            )
        for reply in replies:
            validate_response("run.submit", reply)
            self.assertNotIn("error", reply)
        ids = {reply["result"]["run_id"] for reply in replies}
        self.assertEqual(len(ids), 1)
        self.await_state(ids.pop(), {"succeeded"})
        self.assertEqual(len(self.rpc("run.list", {})["runs"]), 1)

    def test_queue_capacity_and_idempotency_at_capacity(self):
        active = self.submit("active", delay_ms=5000)
        self.await_state(active["run_id"], {"running"})
        for i in range(100):
            self.assertEqual(self.submit(f"queued-{i}")["state"], "queued")
        self.assertEqual(self.submit("queued-0")["submission_key"], "queued-0")
        self.assert_fault(call(self.state, "run.submit", self.submission("overflow")), "QUEUE_FULL")
        listing = self.rpc("run.list", {"state": "queued", "limit": 100})
        self.assertEqual(len(listing["runs"]), 100)
        victim = listing["runs"][0]
        self.rpc("run.cancel", {"version": 0, "run_id": victim["run_id"]})
        self.assertEqual(self.submit("overflow")["state"], "queued")

    def test_cursor_overflow_and_query_scope(self):
        for i in range(3):
            self.submit(str(i))
        page = self.rpc("run.list", {"limit": 1})
        self.assert_fault(
            call(self.state, "run.list", {"cursor": page["next_cursor"], "state": "failed"}),
            "CURSOR_EXPIRED",
        )
        huge = base64.urlsafe_b64encode(
            json.dumps(
                ["runs", [self.rpc("worker.describe", {})["worker_id"], None], 2**100]
            ).encode()
        ).decode()
        self.assert_fault(call(self.state, "run.list", {"cursor": huge}), "CURSOR_EXPIRED")
        for value in ("x" * 1025, None, []):
            self.assert_fault(
                call(self.state, "run.list", {"cursor": value}), "INVALID_PARAMS", -32602
            )
        self.assert_fault(call(self.state, "run.list", {"cursor": "%%%"}), "CURSOR_EXPIRED")

    def test_completion_cancellation_race_preserves_first_terminal_result(self):
        for i in range(10):
            run = self.submit(str(i), delay_ms=1)
            result = self.rpc("run.cancel", {"version": 0, "run_id": run["run_id"]})
            self.assertIn(result["state"], {"succeeded", "cancelled"})
            self.assertEqual(self.rpc("run.get", {"run_id": run["run_id"]}), result)
            self.assertEqual(
                self.rpc("run.cancel", {"version": 0, "run_id": run["run_id"]}), result
            )

    def test_clean_restart_retains_terminal_result_and_worker_identity(self):
        run = self.submit()
        final = self.await_state(run["run_id"], {"succeeded"})
        self.worker.terminate()
        self.assertEqual(self.worker.wait(timeout=5), 0)
        self.worker = self.start_worker()
        self.assertEqual(self.rpc("run.get", {"run_id": run["run_id"]}), final)
        self.assertEqual(self.submit(), final)
        self.assertEqual(self.rpc("worker.describe", {})["worker_id"], final["worker_id"])

    def test_state_symlink_and_non_socket_are_not_replaced(self):
        self.worker.terminate()
        self.worker.communicate(timeout=5)
        original = self.state.with_name("original")
        self.state.rename(original)
        self.state.symlink_to(original, target_is_directory=True)
        other = self.spawn()
        self.assertNotEqual(other.wait(timeout=5), 0)
        self.assertTrue(self.state.is_symlink())
        self.state.unlink()
        original.rename(self.state)
        sock = self.state / "worker.sock"
        sock.write_text("preserve me")
        other = self.spawn()
        self.assertNotEqual(other.wait(timeout=5), 0)
        self.assertEqual(sock.read_text(), "preserve me")


if __name__ == "__main__":
    unittest.main()
