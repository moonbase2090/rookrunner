# SPDX-License-Identifier: MIT

import json
import os
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from execution_core.health import observe, write_heartbeat

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

    def _plant(self, *, state, last_seen, reason=None):
        body = {"last_seen": last_seen, "state": state}
        if reason is not None:
            body["reason"] = reason
        path = self.state / "worker-health.json"
        path.write_text(json.dumps(body, sort_keys=True) + "\n")
        os.chmod(path, 0o600)


if __name__ == "__main__":
    unittest.main()
