# SPDX-License-Identifier: MIT

from datetime import datetime, timedelta, UTC
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from execution_core.attempt import AttemptError
from execution_core.cli import call
from execution_core.disk import (
    DEFAULT_DISK_BUDGET,
    FINISHED_RUN_FOLDER_COUNT,
    FINISHED_RUN_FOLDER_SECONDS,
    usage,
)
from execution_core.protocol import canonical
from execution_core.run import ContainerLease
from execution_core.worker import Worker, _BudgetWatch, now
from schema_support import validate_response, validator


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
_STORAGE_FULL = "worker storage is full; free space before retrying"


def _submit(worker, key, output="ok\n"):
    params = {
        "version": 0,
        "submission_key": key,
        "backend": "development",
        "fixture": {"output": output},
    }
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params})
    reply = worker.response(request.encode())
    validate_response("run.submit", reply)
    return reply


class DiskUsageTests(unittest.TestCase):
    def test_default_is_ten_gibibytes_of_documented_cache_storage(self):
        self.assertEqual(DEFAULT_DISK_BUDGET, 10 * 1024 * 1024 * 1024)

    def test_usage_counts_entries_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            data = root / "data"
            data.write_bytes(b"abc")
            self.assertEqual(usage(root), root.lstat().st_size + data.lstat().st_size)

            linked = root / "linked"
            os.link(data, linked)
            self.assertEqual(
                usage(root),
                root.lstat().st_size + data.lstat().st_size + linked.lstat().st_size,
            )

            outside = Path(directory) / "outside"
            outside.write_bytes(b"x" * 50000)
            link = root / "escape"
            link.symlink_to(outside)
            outside_dir = Path(directory) / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "big").write_bytes(b"y" * 50000)
            dir_link = root / "dir-escape"
            dir_link.symlink_to(outside_dir)
            measured = usage(root)
            self.assertLess(measured, 50000)
            self.assertGreaterEqual(measured, link.lstat().st_size + dir_link.lstat().st_size)

    def test_budget_rejects_a_bool_and_a_negative_count(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            for value in (True, False, -1):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError) as caught:
                        Worker(directory, state, disk_budget=value)
                    self.assertEqual(
                        str(caught.exception), "disk budget must be a non-negative integer"
                    )
            worker = Worker(directory, state)
            self.assertEqual(worker.disk_budget, 10 * 1024 * 1024 * 1024)
            worker = Worker(directory, state, disk_budget=0)
            self.assertEqual(worker.disk_budget, 0)


class DiskBudgetTests(unittest.TestCase):
    def test_fixture_over_budget_does_not_consume_the_key_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state, disk_budget=0)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                described = worker.response(b'{"jsonrpc":"2.0","id":1,"method":"worker.describe"}')
                validate_response("worker.describe", described)
                self.assertTrue(described["result"]["ready"])
                self.assertNotIn("disk_budget", described["result"]["limits"])
                self.assertEqual(
                    described["result"]["retention"],
                    "runs and submission keys retained indefinitely; terminal attempt directories removed",
                )
                evidence = state / "snapshots" / "kept"
                evidence.parent.mkdir(mode=0o700)
                os.chmod(evidence.parent, 0o700)
                evidence.write_bytes(b"keep")
                attempt = state / "attempts" / "kept"
                attempt.parent.mkdir(mode=0o700)
                os.chmod(attempt.parent, 0o700)
                attempt.write_bytes(b"keep-attempt")
                refused = _submit(worker, "fresh")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(refused["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(refused))
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("fresh",)
                    ).fetchone()
                )
                self.assertEqual(evidence.read_bytes(), b"keep")
                self.assertEqual(attempt.read_bytes(), b"keep-attempt")
                worker.disk_budget = DEFAULT_DISK_BUDGET
                accepted = _submit(worker, "fresh")
                self.assertEqual(accepted["result"]["state"], "queued")
                worker.disk_budget = 0
                again = _submit(worker, "fresh")
                self.assertEqual(again["result"]["run_id"], accepted["result"]["run_id"])
                other = _submit(worker, "other")
                self.assertEqual(other["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
                self.assertEqual(evidence.read_bytes(), b"keep")
                self.assertEqual(attempt.read_bytes(), b"keep-attempt")
            finally:
                worker.close()

    def test_usage_error_refuses_the_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                with patch("execution_core.worker.usage", side_effect=OSError("stat failed")):
                    refused = _submit(worker, "stat")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertNotIn("stat failed", json.dumps(refused))
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            finally:
                worker.close()

    def test_equal_usage_is_allowed_and_one_more_byte_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, Path(directory) / "state")
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                with worker.guard:
                    worker.disk_budget = usage(worker.state) + 10
                    self.assertFalse(worker._over_budget(10))
                    self.assertTrue(worker._over_budget(11))
            finally:
                worker.close()

    def test_workflow_over_budget_drops_only_the_uncommitted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                kept = state / "snapshots" / "kept"
                kept.parent.mkdir(mode=0o700)
                os.chmod(kept.parent, 0o700)
                kept.write_bytes(b"keep")
                accepted = self._workflow(worker, "wf-a")
                self.assertEqual(accepted["result"]["state"], "queued")
                snapshot_id = accepted["result"]["input"]["snapshot_id"]
                self.assertTrue((state / "snapshots" / snapshot_id).is_dir())
                worker.disk_budget = 0
                same = self._workflow(worker, "wf-a")
                self.assertEqual(same["result"]["run_id"], accepted["result"]["run_id"])
                refused = self._workflow(worker, "wf-b")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(refused["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(refused))
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("wf-b",)
                    ).fetchone()
                )
                names = sorted(path.name for path in (state / "snapshots").iterdir())
                self.assertEqual(names, sorted(["kept", snapshot_id]))
                self.assertEqual(kept.read_bytes(), b"keep")
                worker.disk_budget = DEFAULT_DISK_BUDGET
                retried = self._workflow(worker, "wf-b")
                self.assertEqual(retried["result"]["state"], "queued")
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
                self.assertTrue(
                    (state / "snapshots" / accepted["result"]["input"]["snapshot_id"]).is_dir()
                )
                self.assertEqual(kept.read_bytes(), b"keep")
            finally:
                worker.close()

    def test_materialize_stops_before_creating_a_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-run")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                evidence = state / "attempts" / "kept"
                evidence.parent.mkdir(mode=0o700, exist_ok=True)
                os.chmod(evidence.parent, 0o700)
                evidence.write_bytes(b"still-here")
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                manifest = (snapshot / "manifest.json").read_bytes()
                worker.disk_budget = 0
                with (
                    patch(
                        "execution_core.worker.materialize_attempt",
                        side_effect=AssertionError("materialized"),
                    ) as materialize,
                    patch(
                        "execution_core.worker.run_job",
                        side_effect=AssertionError("ran"),
                    ),
                ):
                    worker._execute_workflow(record, request, ContainerLease())
                materialize.assert_not_called()
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "failed")
                self.assertIsNone(finished["exit_code"])
                self.assertEqual(finished["error"]["kind"], "SETUP_FAILED")
                self.assertEqual(finished["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(finished))
                self.assertFalse((state / "attempts" / record["attempt_id"]).exists())
                self.assertEqual(evidence.read_bytes(), b"still-here")
                self.assertEqual((snapshot / "manifest.json").read_bytes(), manifest)
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
                worker.disk_budget = 0
                again = self._workflow(worker, "wf-run")
                self.assertEqual(again["result"]["run_id"], record["run_id"])
            finally:
                worker.close()

    def test_materialize_failure_removes_the_attempt_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-run")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                with patch(
                    "execution_core.worker.materialize_attempt",
                    side_effect=AttemptError(
                        "ATTEMPT_FAILED",
                        "attempt workspace could not be materialized",
                    ),
                ):
                    worker._execute_workflow(record, request, ContainerLease())
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "failed")
                self.assertEqual(finished["error"]["kind"], "SETUP_FAILED")
                self.assertFalse((state / "attempts" / record["attempt_id"]).exists())
            finally:
                worker.close()

    def test_running_job_writing_past_its_budget_is_stopped_and_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-over")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                reserve = usage(snapshot)
                worker.disk_budget = usage(state) + 2 * reserve + 65536
                image_digest = record["input"]["image_digest"]
                lease = ContainerLease()
                # Publish a client the way begin() would, so the watch
                # has a container to stop; stops always fail here, so
                # the watch must keep retrying until the run ends.
                self.assertTrue(lease.begin(object(), "wf-over"))
                stop_calls = []
                lease.stop = lambda: stop_calls.append(1) or 1

                def fake_run_job(snapshot_dir, snapshot_digest, workspace, *args, **kwargs):
                    (workspace / "big").write_bytes(b"x" * (reserve + 256 * 1024))
                    deadline = time.monotonic() + 3
                    while not stop_calls and time.monotonic() < deadline:
                        time.sleep(0.05)
                    return {
                        "status": "succeeded",
                        "exit_code": 0,
                        "image_digest": image_digest,
                        "steps": [],
                    }

                with patch("execution_core.worker.run_job", side_effect=fake_run_job):
                    worker._execute_workflow(record, request, lease)
                self.assertTrue(stop_calls, "budget watch did not stop the over-budget job")
                self.assertTrue(lease.budget_breached(), "breach was not latched on the lease")
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "failed")
                self.assertIsNone(finished["exit_code"])
                self.assertEqual(finished["error"]["kind"], "SETUP_FAILED")
                self.assertEqual(finished["error"]["message"], _STORAGE_FULL)
                self.assertNotIn(str(state), json.dumps(finished))
                self.assertFalse((state / "attempts" / record["attempt_id"]).exists())
            finally:
                worker.close()

    def test_a_failed_stop_is_retried_until_the_container_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-retry-stop")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                reserve = usage(snapshot)
                worker.disk_budget = usage(state) + 2 * reserve + 65536
                image_digest = record["input"]["image_digest"]
                lease = ContainerLease()
                self.assertTrue(lease.begin(object(), "wf-retry-stop"))
                stop_calls = []

                def flaky_stop():
                    stop_calls.append(1)
                    if len(stop_calls) < 3:
                        return 1
                    return 0

                lease.stop = flaky_stop

                def fake_run_job(snapshot_dir, snapshot_digest, workspace, *args, **kwargs):
                    (workspace / "big").write_bytes(b"x" * (reserve + 256 * 1024))
                    deadline = time.monotonic() + 5
                    while len(stop_calls) < 3 and time.monotonic() < deadline:
                        time.sleep(0.05)
                    return {
                        "status": "succeeded",
                        "exit_code": 0,
                        "image_digest": image_digest,
                        "steps": [],
                    }

                with patch("execution_core.worker.run_job", side_effect=fake_run_job):
                    worker._execute_workflow(record, request, lease)
                self.assertGreaterEqual(
                    len(stop_calls), 3, "budget watch gave up after a failed stop"
                )
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "failed")
                self.assertEqual(finished["error"]["kind"], "SETUP_FAILED")
                self.assertFalse((state / "attempts" / record["attempt_id"]).exists())
            finally:
                worker.close()

    def test_a_breached_lease_refuses_begin(self):
        lease = ContainerLease()
        self.assertFalse(lease.budget_breached())
        self.assertTrue(lease.begin(object(), "wf-hold"))
        breached = ContainerLease()
        breached.note_budget_breach()
        self.assertTrue(breached.budget_breached())
        self.assertFalse(breached.begin(object(), "wf-held"))

    def test_a_pre_begin_breach_keeps_watching_until_close(self):
        from types import SimpleNamespace

        calls = []
        noted = []
        closed = []

        class FakeOwner:
            def snapshot(self):
                return ("closed" if closed else "idle", "n", None, False)

            def stop(self):
                calls.append(1)
                return 0

            def note_budget_breach(self):
                noted.append(1)

        worker = SimpleNamespace(_runtime_over_budget=lambda: True)
        watch = _BudgetWatch(worker, FakeOwner()).start()
        try:
            # Pre-begin there is nothing to stop, but the breach is
            # latched and the watch must stay alive: exiting here
            # would leave a later container unmonitored.
            deadline = time.monotonic() + 5
            while len(noted) < 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(noted, "breach was not latched on the lease")
            self.assertTrue(watch.exceeded)
            time.sleep(0.6)
            self.assertTrue(watch._thread.is_alive(), "watch exited before the lease closed")
            self.assertEqual(calls, [], "watch stopped a container that does not exist yet")
            closed.append(1)
            deadline = time.monotonic() + 5
            while watch._thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(watch._thread.is_alive(), "watch did not exit on lease close")
        finally:
            watch.join()

    def test_a_creating_phase_breach_keeps_watching_until_close(self):
        from types import SimpleNamespace

        calls = []
        closed = []

        class FakeOwner:
            def snapshot(self):
                return ("closed" if closed else "creating", "n", object(), False)

            def stop(self):
                calls.append(1)
                return 0

            def note_budget_breach(self):
                pass

        worker = SimpleNamespace(_runtime_over_budget=lambda: True)
        watch = _BudgetWatch(worker, FakeOwner()).start()
        try:
            # stop() succeeds while nothing exists yet, but the lease
            # is not terminal, so the watch must not exit: the
            # container may still be created (the latch refuses that).
            time.sleep(0.6)
            self.assertTrue(watch._thread.is_alive(), "watch exited during creating")
            self.assertTrue(calls, "watch never attempted a stop")
            closed.append(1)
            deadline = time.monotonic() + 5
            while watch._thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(watch._thread.is_alive(), "watch did not exit on lease close")
        finally:
            watch.join()
        self.assertTrue(watch.exceeded)

    def test_the_watch_latches_promptly_during_slow_admission(self):
        # Liveness regression test: the lease lock must never be held
        # across Docker I/O. While one thread performs the admission
        # checkpoint sequence with a slow step in the middle, the watch
        # must latch promptly instead of blocking behind admission. The
        # old design held admission_window across the slow step; take it
        # when present so this test pins the fix.
        from types import SimpleNamespace

        lease = ContainerLease()
        self.assertTrue(lease.begin(object(), "wf-slow"))
        stop_calls = []
        lease.stop = lambda: stop_calls.append(1) or 0
        release = threading.Event()
        confirmed = []

        def slow_admission():
            import contextlib

            window = getattr(lease, "admission_window", None)
            ctx = window() if window is not None else contextlib.nullcontext()
            with ctx:
                if lease.budget_breached():
                    return
                if not release.wait(3):
                    return
                # A breach landed mid-admission: the late confirm refuses.
                confirmed.append(lease.confirm_created())

        admission_thread = threading.Thread(target=slow_admission)
        start = time.monotonic()
        admission_thread.start()
        worker = SimpleNamespace(_runtime_over_budget=lambda: True)
        watch = _BudgetWatch(worker, lease).start()
        try:
            deadline = start + 5
            while not lease.budget_breached() and time.monotonic() < deadline:
                time.sleep(0.05)
            latch_delay = time.monotonic() - start
            self.assertTrue(lease.budget_breached(), "latch never landed during slow admission")
            self.assertLess(latch_delay, 1.5, "watch blocked behind admission I/O")
        finally:
            release.set()
            admission_thread.join(5)
            watch.join()
        self.assertEqual(confirmed, [False])

    def test_checkpoints_refuse_each_breach_interleaving(self):
        # Latch before begin refuses admission outright.
        refused = ContainerLease()
        refused.note_budget_breach()
        self.assertFalse(refused.begin(object(), "wf-never"))
        # Latch after begin but before the create checkpoint: the
        # just-created container must not start.
        created = ContainerLease()
        self.assertTrue(created.begin(object(), "wf-created"))
        created.note_budget_breach()
        self.assertFalse(created.confirm_created())
        # Latch after the create checkpoint but before start: the
        # started container must not run steps.
        started = ContainerLease()
        self.assertTrue(started.begin(object(), "wf-started"))
        self.assertTrue(started.confirm_created())
        started.note_budget_breach()
        self.assertFalse(started.confirm_started())
        # No breach anywhere: both confirms pass and phases advance.
        clean = ContainerLease()
        self.assertTrue(clean.begin(object(), "wf-clean"))
        self.assertTrue(clean.confirm_created())
        self.assertTrue(clean.confirm_started())
        self.assertEqual(clean.snapshot()[0], "running")

    def test_a_slow_reserve_fsync_does_not_delay_the_latch(self):
        # begin() persists ownership with an fsync; that write must not
        # hold the lease lock, or a blocked fsync delays the budget latch
        # and the stop. While begin sits in a slow reserve, the watch
        # must latch promptly, and begin must then roll back and refuse.
        from types import SimpleNamespace

        lease = ContainerLease()
        reserve_calls = []
        unreserve_calls = []
        release_fsync = threading.Event()

        def slow_reserve(name):
            reserve_calls.append(name)
            self.assertTrue(release_fsync.wait(5))

        lease.reserve = slow_reserve
        lease.unreserve = lambda: unreserve_calls.append(1)
        stop_calls = []
        lease.stop = lambda: stop_calls.append(1) or 0
        begun = []

        def begin_in_thread():
            begun.append(lease.begin(object(), "wf-slow-fsync"))

        begin_thread = threading.Thread(target=begin_in_thread)
        begin_thread.start()
        try:
            deadline = time.monotonic() + 5
            while not reserve_calls and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(reserve_calls, "begin never reached the reserve write")
            worker = SimpleNamespace(_runtime_over_budget=lambda: True)
            watch = _BudgetWatch(worker, lease).start()
            start = time.monotonic()
            try:
                deadline = start + 5
                while not lease.budget_breached() and time.monotonic() < deadline:
                    time.sleep(0.05)
                latch_delay = time.monotonic() - start
                self.assertTrue(lease.budget_breached(), "latch blocked on the slow fsync")
                self.assertLess(latch_delay, 1.5, "watch sat behind begin's fsync")
            finally:
                release_fsync.set()
                watch.join()
        finally:
            release_fsync.set()
            begin_thread.join(5)
        self.assertEqual(begun, [False], "begin kept a reservation made under breach")
        self.assertEqual(unreserve_calls, [1], "breached reservation was not rolled back")

    def test_running_job_within_its_budget_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-under")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                record.update(state="running", started_at=now(), attempt_id=str(uuid.uuid4()))
                with worker.db:
                    worker.save(record)
                snapshot = state / "snapshots" / record["input"]["snapshot_id"]
                reserve = usage(snapshot)
                worker.disk_budget = usage(state) + 2 * reserve + 1024 * 1024
                image_digest = record["input"]["image_digest"]
                lease = ContainerLease()
                stop_calls = []
                original_stop = lease.stop
                lease.stop = lambda: stop_calls.append(1) or original_stop()

                def fake_run_job(snapshot_dir, snapshot_digest, workspace, *args, **kwargs):
                    (workspace / "small").write_bytes(b"x" * 65536)
                    return {
                        "status": "succeeded",
                        "exit_code": 0,
                        "image_digest": image_digest,
                        "steps": [],
                    }

                with patch("execution_core.worker.run_job", side_effect=fake_run_job):
                    worker._execute_workflow(record, request, lease)
                self.assertEqual(stop_calls, [])
                finished = worker.get(record["run_id"])
                self.assertEqual(finished["state"], "succeeded")
                self.assertEqual(finished["exit_code"], 0)
            finally:
                worker.close()

    def test_home_file_bytes_count_toward_the_disk_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            self._git_workflow(repo)
            worker = Worker(repo, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = self._workflow(worker, "wf-a")
                self.assertEqual(accepted["result"]["state"], "queued")
                home = state / "attempts" / "11111111-1111-4111-8111-111111111111" / "home"
                home.mkdir(parents=True)
                before = usage(state)
                (home / "blob").write_bytes(b"h" * 8192)
                self.assertGreaterEqual(usage(state) - before, 8192)
                worker.disk_budget = before
                refused = self._workflow(worker, "wf-b")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("wf-b",)
                    ).fetchone()
                )
                worker._remove_workspace(home.parent)
                self.assertFalse(home.parent.exists())
            finally:
                worker.close()

    def test_finishing_a_run_removes_its_attempt_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = _submit(worker, "finished", output="kept-log\n")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                attempt_id = "22222222-2222-4222-8222-222222222222"
                attempt = state / "attempts" / attempt_id
                marker = attempt / "workspace" / "blob"
                marker.parent.mkdir(parents=True)
                marker.write_bytes(b"finished-bytes")
                record.update(state="running", started_at=now(), attempt_id=attempt_id)
                with worker.db:
                    worker.save(record)
                worker._finish_fixture(record, request)
                stored = worker.get(record["run_id"])
                self.assertEqual(stored["state"], "succeeded")
                self.assertEqual(stored["exit_code"], 0)
                self.assertEqual(
                    worker.db.execute(
                        "SELECT log FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0],
                    b"kept-log\n",
                )
                self.assertFalse(attempt.exists())
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            finally:
                worker.close()

    def test_full_budget_reclaims_a_finished_attempt_and_keeps_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = _submit(worker, "finished", output="kept-log\n")
                record = accepted["result"]
                request = json.loads(
                    worker.db.execute(
                        "SELECT request FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0]
                )
                attempt_id = "22222222-2222-4222-8222-222222222222"
                attempt = state / "attempts" / attempt_id
                marker = attempt / "workspace" / "blob"
                marker.parent.mkdir(parents=True)
                marker.write_bytes(b"q" * 65536)
                record.update(state="running", started_at=now(), attempt_id=attempt_id)
                with worker.db:
                    worker.save(record)
                worker._finish_fixture(record, request)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_bytes(b"q" * 65536)
                live = _submit(worker, "live")
                live_id = "44444444-4444-4444-8444-444444444444"
                live_dir = state / "attempts" / live_id
                live_file = live_dir / "workspace" / "live"
                live_file.parent.mkdir(parents=True)
                live_file.write_bytes(b"live-bytes")
                live["result"].update(state="running", started_at=now(), attempt_id=live_id)
                with worker.db:
                    worker.save(live["result"])
                evidence = state / "snapshots" / "kept-snap"
                evidence.parent.mkdir(mode=0o700, exist_ok=True)
                evidence.write_bytes(b"evidence")
                worker.disk_budget = usage(state) - 1
                nxt = _submit(worker, "next")
                self.assertNotIn("error", nxt)
                self.assertEqual(nxt["result"]["state"], "queued")
                self.assertFalse(attempt.exists())
                self.assertEqual(live_file.read_bytes(), b"live-bytes")
                self.assertEqual(evidence.read_bytes(), b"evidence")
                self.assertEqual(worker.get(record["run_id"])["state"], "succeeded")
                self.assertEqual(
                    worker.db.execute(
                        "SELECT log FROM runs WHERE id=?", (record["run_id"],)
                    ).fetchone()[0],
                    b"kept-log\n",
                )
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 3)
                self.assertIsNotNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("finished",)
                    ).fetchone()
                )
            finally:
                worker.close()

    def test_unresolved_attempt_is_kept_when_the_budget_is_full(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            worker = Worker(directory, state)
            worker.execute_queue = lambda: worker.stop.wait()
            worker.start()
            try:
                accepted = _submit(worker, "lost")
                record = accepted["result"]
                attempt_id = "33333333-3333-4333-8333-333333333333"
                attempt = state / "attempts" / attempt_id
                blob = attempt / "workspace" / "blob"
                blob.parent.mkdir(parents=True)
                blob.write_bytes(b"u" * 65536)
                record.update(
                    state="lost",
                    started_at=now(),
                    finished_at=now(),
                    attempt_id=attempt_id,
                    cleanup="unresolved",
                    error={
                        "kind": "WORKER_INTERRUPTED",
                        "message": "owned container cleanup was not confirmed",
                    },
                )
                with worker.db:
                    worker.save(record)
                worker.disk_budget = usage(state) - 1
                refused = _submit(worker, "next")
                self.assertEqual(refused["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertEqual(blob.read_bytes(), b"u" * 65536)
                self.assertEqual(worker.get(record["run_id"])["cleanup"], "unresolved")
                self.assertIsNone(
                    worker.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("next",)
                    ).fetchone()
                )
            finally:
                worker.close()

    def test_cli_flag_configures_the_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            refused = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    str(repo),
                    "--disk-budget-bytes",
                    "-1",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(refused.returncode, 1)
            payload = json.loads(refused.stderr)
            self.assertEqual(payload["error"]["kind"], "CLIENT_ERROR")
            self.assertEqual(
                payload["error"]["message"], "disk budget must be a non-negative integer"
            )
            self.assertNotIn(str(state), refused.stderr)
            self.assertNotIn(str(repo), refused.stderr)
            self.assertFalse(state.exists())

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
                    "--disk-budget-bytes",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
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
                params = {
                    "version": 0,
                    "submission_key": "cli",
                    "backend": "development",
                    "fixture": {},
                }
                validator("Request").validate(
                    {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
                )
                reply = call(state, "run.submit", params)
                validate_response("run.submit", reply)
                self.assertEqual(reply["error"]["data"]["kind"], "STORAGE_FULL")
                self.assertNotIn(str(state), json.dumps(reply))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def _git_workflow(self, repo):
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        subprocess.run(
            ["git", "-C", repo, "init", "--initial-branch=main"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )

    def _workflow(self, worker, key):
        params = {
            "version": 1,
            "submission_key": key,
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": EVENT,
            "image": IMAGE,
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
        validator("Request").validate(body)
        reply = worker.response(canonical(body).encode())
        validate_response("run.submit", reply)
        return reply


def _stamp(seconds_ago):
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _snapshot(state, payload):
    name = str(uuid.uuid4())
    path = state / "snapshots" / name
    path.mkdir(parents=True)
    (path / "marker").write_bytes(payload)
    return name, path


def _store(worker, key, state_name, snapshot_id, finished_at, cleanup, posted=True):
    run_id = str(uuid.uuid4())
    record = {
        "run_id": run_id,
        "submission_key": key,
        "state": state_name,
        "exit_code": 0 if state_name == "succeeded" else None,
        "input": {"kind": "workflow_job", "snapshot_id": snapshot_id},
        "finished_at": finished_at,
        "attempt_id": None,
        "cleanup": cleanup,
    }
    with worker.db:
        worker.db.execute(
            "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
            (run_id, key, "{}", canonical(record), b""),
        )
        if posted and state_name in {"succeeded", "failed", "lost", "cancelled"}:
            worker.db.execute(
                "INSERT INTO status_posts(run_id, context, sha, state) VALUES (?, ?, ?, ?)",
                (run_id, "rookrunner/check", "a" * 40, "success"),
            )
    return run_id


class FinishedFolderTests(unittest.TestCase):
    def _worker(self, directory):
        state = Path(directory) / "state"
        worker = Worker(directory, state)
        worker.execute_queue = lambda: worker.stop.wait()
        worker.start()
        return worker, state

    def test_a_finished_folder_past_ninety_days_is_removed_and_the_run_stays(self):
        self.assertEqual(FINISHED_RUN_FOLDER_SECONDS, 90 * 24 * 60 * 60)
        limit = 90 * 24 * 60 * 60
        with tempfile.TemporaryDirectory() as directory:
            worker, state = self._worker(directory)
            try:
                old_name, old_path = _snapshot(state, b"old")
                young_name, young_path = _snapshot(state, b"young")
                held_name, held_path = _snapshot(state, b"held")
                live_name, live_path = _snapshot(state, b"live")
                _store(
                    worker,
                    "old",
                    "succeeded",
                    old_name,
                    _stamp(limit + 1),
                    "confirmed_no_external_resources",
                )
                _store(
                    worker,
                    "young",
                    "succeeded",
                    young_name,
                    _stamp(limit - 1),
                    "confirmed_no_external_resources",
                )
                _store(
                    worker,
                    "held",
                    "lost",
                    held_name,
                    _stamp(limit + 1),
                    "unresolved",
                )
                _store(worker, "live", "queued", live_name, None, "not_started")
                worker.close()
                again = Worker(directory, state)
                again.execute_queue = lambda: again.stop.wait()
                again.start()
                try:
                    self.assertFalse(old_path.exists())
                    self.assertTrue(young_path.exists())
                    self.assertTrue(held_path.exists())
                    self.assertTrue(live_path.exists())
                    self.assertEqual(again.db.execute("SELECT count(*) FROM runs").fetchone()[0], 4)
                    kept = again.db.execute(
                        "SELECT 1 FROM runs WHERE submission_key=?", ("old",)
                    ).fetchone()
                    self.assertIsNotNone(kept)
                finally:
                    again.close()
            finally:
                if worker.db is not None:
                    worker.close()

    def test_an_unposted_workflow_folder_past_ninety_days_is_kept_until_its_status_is_posted(self):
        limit = 90 * 24 * 60 * 60
        with tempfile.TemporaryDirectory() as directory:
            worker, state = self._worker(directory)
            try:
                name, path = _snapshot(state, b"unposted")
                run_id = _store(
                    worker,
                    "unposted",
                    "succeeded",
                    name,
                    _stamp(limit + 1),
                    "confirmed_no_external_resources",
                    posted=False,
                )
                worker.close()
                again = Worker(directory, state)
                again.execute_queue = lambda: again.stop.wait()
                again.start()
                try:
                    self.assertTrue(path.exists())
                    with again.guard, again.db:
                        again.db.execute(
                            "INSERT INTO status_posts(run_id, context, sha, state) VALUES (?, ?, ?, ?)",
                            (run_id, "rookrunner/check", "a" * 40, "failure"),
                        )
                    again._prune_finished_run_folders(0)
                    self.assertFalse(path.exists())
                    self.assertIsNotNone(again.get(run_id))
                finally:
                    again.close()
            finally:
                if worker.db is not None:
                    worker.close()

    def test_the_oldest_finished_folder_past_one_hundred_is_removed(self):
        self.assertEqual(FINISHED_RUN_FOLDER_COUNT, 100)
        with tempfile.TemporaryDirectory() as directory:
            worker, state = self._worker(directory)
            try:
                paths = []
                for index in range(101):
                    name, path = _snapshot(state, b"f")
                    paths.append(path)
                    _store(
                        worker,
                        f"run-{index}",
                        "succeeded",
                        name,
                        _stamp(1000 - index),
                        "confirmed_no_external_resources",
                    )
                live_name, live_path = _snapshot(state, b"live")
                _store(worker, "live", "queued", live_name, None, "not_started")
                worker.close()
                again = Worker(directory, state)
                again.execute_queue = lambda: again.stop.wait()
                again.start()
                try:
                    self.assertFalse(paths[0].exists())
                    self.assertTrue(paths[1].exists())
                    self.assertTrue(paths[100].exists())
                    self.assertTrue(live_path.exists())
                    self.assertEqual(
                        again.db.execute("SELECT count(*) FROM runs").fetchone()[0], 102
                    )
                finally:
                    again.close()
            finally:
                if worker.db is not None:
                    worker.close()

    def test_disk_pressure_removes_the_oldest_finished_folder_first(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, state = self._worker(directory)
            try:
                old_name, old_path = _snapshot(state, b"o" * 4000)
                new_name, new_path = _snapshot(state, b"n" * 4000)
                run_name, run_path = _snapshot(state, b"r" * 4000)
                _store(
                    worker,
                    "old",
                    "succeeded",
                    old_name,
                    _stamp(1000),
                    "confirmed_no_external_resources",
                )
                _store(
                    worker,
                    "new",
                    "succeeded",
                    new_name,
                    _stamp(10),
                    "confirmed_no_external_resources",
                )
                _store(worker, "running", "running", run_name, None, "not_started")
                worker.disk_budget = usage(state) - 1000
                self.assertFalse(worker._over_budget(0))
                self.assertFalse(old_path.exists())
                self.assertTrue(new_path.exists())
                self.assertTrue(run_path.exists())
                self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 3)
            finally:
                worker.close()
