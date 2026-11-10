# SPDX-License-Identifier: MIT

import contextlib
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from execution_core.cli import main
from execution_core.health import observe, write_heartbeat
from execution_core.worker import Worker

SEEN = "2026-11-10T02:00:00.000000+00:00"
LATER = "2026-11-10T02:00:05.000000+00:00"
AT = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
FIVE = datetime(2026, 11, 10, 2, 0, 5, tzinfo=UTC)
EDGE = datetime(2026, 11, 10, 2, 0, 15, tzinfo=UTC)
PAST = datetime(2026, 11, 10, 2, 0, 15, 1, tzinfo=UTC)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.state = Path(self.tmp.name)
        os.chmod(self.state, 0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_missing_file_is_worker_unavailable_and_absent(self):
        report = observe(self.state, now=AT)
        self.assertEqual(
            report,
            {"state": "worker_unavailable", "last_seen": None, "reason": "absent"},
        )

    def test_a_fresh_available_record_stays_available(self):
        self._plant(state="available", last_seen=SEEN)
        report = observe(self.state, now=FIVE)
        self.assertEqual(report["state"], "available")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["last_seen"], SEEN)

    def test_a_heartbeat_fifteen_seconds_old_stays_available(self):
        self._plant(state="available", last_seen=SEEN)
        report = observe(self.state, now=EDGE)
        self.assertEqual(report["state"], "available")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["last_seen"], SEEN)

    def test_a_heartbeat_older_than_fifteen_seconds_is_stale(self):
        self._plant(state="available", last_seen=SEEN)
        before = (self.state / "worker-health.json").read_bytes()
        report = observe(self.state, now=PAST)
        self.assertEqual(report["state"], "worker_unavailable")
        self.assertEqual(report["reason"], "heartbeat_stale")
        self.assertEqual(report["last_seen"], SEEN)
        self.assertEqual((self.state / "worker-health.json").read_bytes(), before)

    def test_a_stopped_record_stays_worker_unavailable(self):
        self._plant(state="worker_unavailable", last_seen=SEEN, reason="stopped")
        report = observe(self.state, now=FIVE)
        self.assertEqual(report["state"], "worker_unavailable")
        self.assertEqual(report["reason"], "stopped")
        self.assertEqual(report["last_seen"], SEEN)

    def test_a_stopped_record_is_not_relabeled_stale(self):
        self._plant(state="worker_unavailable", last_seen=SEEN, reason="stopped")
        report = observe(self.state, now=PAST)
        self.assertEqual(report["reason"], "stopped")
        self.assertEqual(report["last_seen"], SEEN)

    def test_an_available_heartbeat_clears_a_stopped_worker(self):
        write_heartbeat(self.state, alive=False, now=AT)
        write_heartbeat(self.state, alive=True, now=FIVE)
        report = observe(self.state, now=FIVE)
        self.assertEqual(report["state"], "available")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["last_seen"], LATER)
        self.assertEqual(
            (self.state / "worker-health.json").read_text(),
            '{"last_seen": "2026-11-10T02:00:05.000000+00:00", "state": "available"}\n',
        )

    def test_a_heartbeat_file_is_owner_readable_only(self):
        write_heartbeat(self.state, alive=True, now=AT)
        mode = (self.state / "worker-health.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_symlink_health_file_is_absent(self):
        real = self.state / "other.json"
        real.write_text(
            json.dumps({"last_seen": SEEN, "state": "available"}, sort_keys=True) + "\n"
        )
        (self.state / "worker-health.json").symlink_to(real.name)
        report = observe(self.state, now=AT)
        self.assertEqual(report["state"], "worker_unavailable")
        self.assertEqual(report["reason"], "absent")
        self.assertIsNone(report["last_seen"])

    def test_write_refuses_a_symlink(self):
        real = self.state / "other.json"
        real.write_text("kept\n")
        (self.state / "worker-health.json").symlink_to(real.name)
        with self.assertRaises(OSError):
            write_heartbeat(self.state, alive=True, now=AT)
        self.assertEqual(real.read_text(), "kept\n")

    def test_a_failed_write_keeps_the_previous_record_whole(self):
        write_heartbeat(self.state, alive=True, now=AT)
        before = (self.state / "worker-health.json").read_bytes()
        with mock.patch("execution_core.health.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                write_heartbeat(self.state, alive=False, now=FIVE)
        self.assertEqual((self.state / "worker-health.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["worker-health.json"])

    def _plant(self, *, state, last_seen, reason=None):
        body = {"last_seen": last_seen, "state": state}
        if reason is not None:
            body["reason"] = reason
        path = self.state / "worker-health.json"
        path.write_text(json.dumps(body, sort_keys=True) + "\n")
        os.chmod(path, 0o600)


class WorkerPresenceTests(unittest.TestCase):
    def test_shutdown_records_stopped_and_the_next_process_clears_it(self):
        with tempfile.TemporaryDirectory(prefix="execution-test-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            first = Worker(repo, state)
            try:
                first.start()
                first.stop.set()
                first.scheduler.join(timeout=5)
                if first.scheduler.is_alive():
                    self.fail("scheduler did not stop")
                first.stop.clear()
                live = observe(state)
            finally:
                first.close()
            self.assertEqual(live["state"], "available")
            self.assertIsNone(live["reason"])
            stopped = observe(state)
            self.assertEqual(stopped["state"], "worker_unavailable")
            self.assertEqual(stopped["reason"], "stopped")
            self.assertRegex(
                stopped["last_seen"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$",
            )
            second = Worker(repo, state)
            try:
                second.start()
                second.stop.set()
                second.scheduler.join(timeout=5)
                if second.scheduler.is_alive():
                    self.fail("scheduler did not stop")
                second.stop.clear()
                cleared = observe(state)
                text = (state / "worker-health.json").read_text()
            finally:
                second.close()
            self.assertEqual(cleared["state"], "available")
            self.assertIsNone(cleared["reason"])
            self.assertNotIn("stopped", text)

    def test_a_duplicate_start_that_fails_leaves_the_live_worker_available(self):
        with tempfile.TemporaryDirectory(prefix="execution-test-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            live = Worker(repo, state)
            try:
                live.start()
                duplicate = Worker(repo, state)
                with self.assertRaises(Exception):
                    duplicate.start()
                after = observe(state)
            finally:
                live.close()
            self.assertEqual(after["state"], "available")
            self.assertIsNone(after["reason"])

    def test_closing_a_worker_that_never_started_writes_no_health_file(self):
        with tempfile.TemporaryDirectory(prefix="execution-test-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            state.mkdir(mode=0o700)
            Worker(repo, state).close()
            self.assertFalse((state / "worker-health.json").exists())


class HealthCommandTests(unittest.TestCase):
    def test_health_prints_a_stale_heartbeat(self):
        with tempfile.TemporaryDirectory(prefix="execution-test-") as directory:
            state = Path(directory)
            os.chmod(state, 0o700)
            (state / "worker-health.json").write_text(
                json.dumps(
                    {
                        "last_seen": "2020-01-01T00:00:00.000000+00:00",
                        "state": "available",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            out = io.StringIO()
            argv = ["execution_core", "--state", str(state), "health"]
            with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", argv):
                main()
            report = json.loads(out.getvalue())
            self.assertEqual(report["state"], "worker_unavailable")
            self.assertEqual(report["reason"], "heartbeat_stale")
            self.assertEqual(report["last_seen"], "2020-01-01T00:00:00.000000+00:00")


if __name__ == "__main__":
    unittest.main()
