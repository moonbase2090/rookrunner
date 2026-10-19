# SPDX-License-Identifier: MIT

"""Focused behavior tests for the low/info hardening cleanup batch.

Each class covers one batch item and fails on the pre-fix code:
1. SQLite state files are owner-only (0600) under umask 022.
2. A zero-byte or missing run database fails closed instead of resetting.
3. A Docker-unreachable engine reports not-ready honestly at run time.
4. ENOSPC during capture returns STORAGE_FULL, not INVALID_PARAMS.
5. Deeply nested YAML returns a structured input error naming the limit.
6. A lone surrogate in a workflow name is rejected early with a clear error.
7. One slow client cannot stall the status page past a bounded budget.
8. A registry refusal during pull reports image resolution failure, not an
   unreachable engine.
9. The worker service file bounds the start loop and delays restarts.
"""

import errno
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from execution_core import run as run_module
from execution_core import status_page as status_page_module
from execution_core.cli import call
from execution_core.plan import PlanError, plan_workflow
from execution_core.protocol import TERMINAL, Fault
from execution_core.snapshot import CaptureError, SourceCapture
from execution_core.worker import Worker

IMAGE = "sha256:" + "cd" * 32
EVENT = {"kind": "local", "n": 1}
WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _seed_repo(repo):
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text(WORKFLOW)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=322824348+mb2090@users.noreply.github.com",
        "commit",
        "-m",
        "fixture",
    )


class SqliteModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hardening-sqlite-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.old_umask = os.umask(0o022)
        self.worker = None

    def tearDown(self):
        os.umask(self.old_umask)
        if self.worker is not None:
            self.worker.close()
        self.tmp.cleanup()

    def test_db_and_sidecars_are_owner_only(self):
        self.worker = Worker(str(self.repo), str(self.state))
        self.worker.start()
        for name in ("runs.sqlite3", "runs.sqlite3-wal", "runs.sqlite3-shm"):
            with self.subTest(sidecar=name):
                mode = stat.S_IMODE(os.lstat(self.state / name).st_mode)
                self.assertEqual(mode, 0o600)


class MissingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hardening-history-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.worker = None

    def tearDown(self):
        if self.worker is not None:
            self.worker.close()
        self.tmp.cleanup()

    def test_zero_byte_database_refuses_to_start(self):
        (self.state / "runs.sqlite3").write_bytes(b"")
        self.worker = Worker(str(self.repo), str(self.state))
        with self.assertRaisesRegex(ValueError, "refusing to reset history"):
            self.worker.start()

    def test_missing_database_with_prior_state_refuses_to_start(self):
        snapshots = self.state / "snapshots"
        snapshots.mkdir(mode=0o700)
        (snapshots / "orphan").write_text("leftover")
        self.worker = Worker(str(self.repo), str(self.state))
        with self.assertRaisesRegex(ValueError, "refusing to reset history"):
            self.worker.start()

    def test_fresh_directory_still_starts(self):
        self.worker = Worker(str(self.repo), str(self.state))
        self.worker.start()
        self.assertTrue((self.state / "runs.sqlite3").exists())


class DockerUnreachableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hardening-docker-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.bin / "docker.marker"
        fake = self.bin / "docker"
        fake.write_text('#!/bin/sh\ncat "${0}.marker" >&2\nexit 1\n')
        fake.chmod(0o700)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.old_path
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _seed_repo(self.repo)
        self.state = self.root / "state"
        self.processes = []
        self.start_worker()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        os.environ["PATH"] = self.old_path
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            try:
                self.rpc("worker.describe", {})
                return
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("worker did not become ready")

    def rpc(self, method, params):
        reply = call(self.state, method, params)
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def await_state(self, run_id, states):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = self.rpc("run.get", {"run_id": run_id})
            if record["state"] in states:
                return record
            time.sleep(0.05)
        self.fail(f"run did not reach {states}: {run_id}")

    def test_unreachable_engine_reports_not_ready_honestly(self):
        cases = (
            (
                "docker-down-classic",
                "Cannot connect to the Docker daemon. Is the docker daemon running?",
            ),
            (
                "docker-down-api",
                "failed to connect to the docker API; check if the daemon is running",
            ),
        )
        for key, stderr in cases:
            with self.subTest(key=key):
                self.marker.write_text(stderr + "\n")
                submitted = self.rpc(
                    "run.submit",
                    {
                        "version": 1,
                        "submission_key": key,
                        "workflow": ".github/workflows/test.yml",
                        "job_id": "build",
                        "event": EVENT,
                        "image": IMAGE,
                    },
                )
                self.assertEqual(submitted["state"], "queued")
                record = self.await_state(submitted["run_id"], TERMINAL)
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["error"]["kind"], "SETUP_FAILED")
                self.assertIn("Docker is unreachable", record["error"]["message"])


class RegistryRefusalTests(unittest.TestCase):
    def test_registry_refusal_during_pull_reports_image_failure(self):
        reference = "demo:latest@sha256:" + "ab" * 32
        digest = "sha256:" + "cd" * 32

        def fake_invoke(docker, args, cap, deadline):
            if args[:1] == ["pull"]:
                return (
                    1,
                    b"",
                    b"Error response from daemon: Get registry: dial tcp: connection refused\n",
                )
            return 1, b"", b"Error: No such image\n"

        with (
            mock.patch.object(run_module, "_invoke_within", side_effect=fake_invoke),
            self.assertRaises(run_module.RunError) as caught,
        ):
            run_module._resolve_image("docker", reference, digest, time.monotonic() + 60)
        self.assertEqual(caught.exception.kind, "SETUP_FAILED")
        self.assertIn("image digest will not resolve", str(caught.exception))
        self.assertNotIn("Docker is unreachable", str(caught.exception))


class WorkerServiceTests(unittest.TestCase):
    def test_crash_loop_is_bounded(self):
        text = (
            Path(__file__).resolve().parents[1] / "deploy" / "rookrunner-worker.service"
        ).read_text()
        self.assertIn("StartLimitIntervalSec=300", text)
        self.assertIn("StartLimitBurst=5", text)
        self.assertIn("RestartSec=5", text)


class CaptureEnospcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hardening-enospc-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_enospc_during_capture_is_storage_full(self):
        capture = SourceCapture(str(self.repo), str(self.state))
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            mock.patch("tempfile.mkdtemp", side_effect=full),
            self.assertRaises(CaptureError) as caught,
            capture.prepare(".github/workflows/test.yml"),
        ):
            pass
        self.assertEqual(caught.exception.kind, "STORAGE_FULL")

    def test_storage_full_capture_maps_to_storage_full_fault(self):
        with self.assertRaises(Fault) as caught:
            Worker._capture_fault(CaptureError("STORAGE_FULL", "disk is full"))
        self.assertEqual(caught.exception.kind, "STORAGE_FULL")


class NestedYamlTests(unittest.TestCase):
    def test_deep_nesting_returns_a_structured_input_error(self):
        depth = 10000
        workflow = (
            b"name: deep\non: push\njobs:\n"
            b"  build:\n    runs-on: ubuntu-latest\n"
            b"    steps:\n      - run: echo hi\n"
            b"env:\n  X: " + b"[" * depth + b"1" + b"]" * depth
        )
        with self.assertRaises(PlanError) as caught:
            plan_workflow(workflow, "build")
        self.assertEqual(caught.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("64", str(caught.exception))
        fault = Worker._plan_fault(caught.exception)
        self.assertEqual(fault.kind, "INVALID_PARAMS")


class SurrogateNameTests(unittest.TestCase):
    def test_lone_surrogate_in_name_is_rejected_early(self):
        workflow = (
            b'name: "bad\\ud800name"\non: push\njobs:\n'
            b"  build:\n    runs-on: ubuntu-latest\n"
            b"    steps:\n      - run: echo hi\n"
        )
        with self.assertRaises(PlanError) as caught:
            plan_workflow(workflow, "build")
        self.assertEqual(caught.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("UTF-8", str(caught.exception))


class StatusPageStallTests(unittest.TestCase):
    def test_one_slow_client_cannot_stall_the_page(self):
        server, trickle = socket.socketpair()
        stop = threading.Event()

        def drip():
            try:
                trickle.sendall(b"GET / HTTP/1.1\r\n")
                while not stop.is_set():
                    time.sleep(0.2)
                    trickle.sendall(b"X-Pad: a\r\n")
            except OSError:
                pass

        needle = threading.Thread(target=drip, daemon=True)
        needle.start()
        handled = threading.Thread(
            target=status_page_module.handle, args=(server, self._dud_state()), daemon=True
        )
        handled.start()
        started = time.monotonic()
        try:
            handled.join(timeout=30)
            elapsed = time.monotonic() - started
            self.assertFalse(handled.is_alive(), "a trickling client held the page past its budget")
            # The request budget is 10s: bound completion near it (with
            # scheduling tolerance) so a regression past the budget fails.
            self.assertLess(
                elapsed, 13, f"the page held a trickling client for {elapsed:.1f}s, past its budget"
            )
        finally:
            stop.set()
            trickle.close()
            server.close()
            needle.join(timeout=5)
            handled.join(timeout=5)
        host = f"Host: {status_page_module.ADDRESS}:{status_page_module.PORT}\r\n".encode()
        head, _body = self._exchange(b"GET / HTTP/1.1\r\n" + host + b"\r\n", self._dud_state())
        self.assertIn("200 OK", head)

    def _dud_state(self):
        dud = Path(tempfile.mkdtemp(prefix="hardening-dud-"))
        self.addCleanup(lambda: _rmtree(dud))
        return dud

    def _exchange(self, request, state):
        left, right = socket.socketpair()
        left.settimeout(10)
        right.settimeout(10)
        try:
            right.sendall(request)
            right.shutdown(socket.SHUT_WR)
            status_page_module.handle(left, state)
            return _read_http(right)
        finally:
            left.close()
            right.close()


def _read_http(sock):
    rest = b""
    while b"\r\n\r\n" not in rest:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    head, _, rest = rest.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head.decode(), rest[:length].decode()


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
