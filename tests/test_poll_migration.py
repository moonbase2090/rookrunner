# SPDX-License-Identifier: MIT

import copy
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid

from execution_core.poll import PollError
from execution_core.poll_migration import migrate_reporting
from execution_core.protocol import canonical
from execution_core.submissions import migrate_submissions
from execution_core.worker import Worker, storage_plan
from schema_support import validate_response
import test_poll


class ReportingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_poll.PollTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f.server.remaining = 10000
        tip = f._tip()
        f.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.run_id = f.poll()["submitted"][0]["run_id"]
        self.state_path = f.state / "poll.json"
        self.payload = json.loads(self.state_path.read_text())
        self.entry = self.payload["submissions"][0]
        self.params = {
            field: self.entry[field]
            for field in ("run_id", "tested_commit", "status_sha", "context")
        }
        f.server.requests.clear()

    def restart(self):
        f = self.fixture
        f.worker.close()
        f.worker = Worker(f.clone, f.state)
        f.worker.execute_queue = lambda: f.worker.stop.wait()
        f.worker.start()

    def legacy_missing(self):
        f = self.fixture
        record = f.worker.get(self.run_id)
        record.update(state="failed", exit_code=None)
        with f.worker.db:
            f.worker.save(record)
        shutil.rmtree(f.state / "snapshots" / record["input"]["snapshot_id"])
        self.entry.pop("reporting_version", None)
        self.payload.pop("reporting_migration")
        self.state_path.write_text(canonical(self.payload))

    def test_hundreds_of_legacy_rows_migrate_across_retention_and_restart_once(self):
        f = self.fixture
        original = f.worker.get(self.run_id)
        source = f.state / "snapshots" / original["input"]["snapshot_id"]
        shared_id = str(uuid.uuid4())
        shared = f.state / "snapshots" / shared_id
        shutil.copytree(source, shared)
        missing_id = str(uuid.uuid4())
        missing = f.state / "snapshots" / missing_id
        shutil.copytree(source, missing)
        shutil.rmtree(missing)
        request = json.loads(
            f.worker.db.execute("SELECT request FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]
        )
        entries = []
        finished = (datetime.now(UTC) - timedelta(days=91)).isoformat()
        with f.worker.guard, f.worker.db:
            for index in range(240):
                key = f"poll-{index:064x}"
                record = copy.deepcopy(original)
                record.update(
                    run_id=str(uuid.uuid4()),
                    submission_key=key,
                    state="succeeded",
                    exit_code=0,
                    finished_at=finished,
                    cleanup="confirmed_no_external_resources",
                )
                record["input"]["snapshot_id"] = missing_id if index % 2 == 0 else shared_id
                stored_request = {**request, "submission_key": key}
                f.worker.db.execute(
                    "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                    (record["run_id"], key, canonical(stored_request), canonical(record), b""),
                )
                entries.append({**self.entry, "key": key, "run_id": record["run_id"]})
        live = dict(self.entry)
        for entry in [*entries, live]:
            entry.pop("reporting_version", None)
        self.payload["submissions"] = [*entries, live]
        self.payload.pop("reporting_migration")
        self.payload.pop("completed")
        self.state_path.write_text(canonical(self.payload))
        before = self.state_path.read_bytes()
        changes = f.worker.db.total_changes
        report = migrate_submissions(f.state, caller=f.caller, dry_run=True)
        self.assertEqual(100, report["scanned"])
        self.assertEqual(
            50, sum(row["disposition"] == "would_tombstone" for row in report["items"])
        )
        self.assertEqual(before, self.state_path.read_bytes())
        self.assertEqual(changes, f.worker.db.total_changes)

        f.worker.state_usage.refresh()
        f.worker._prune_finished_run_folders(0)
        self.assertTrue(shared.is_dir(), "unreported terminal evidence was pruned")
        statuses, skips = [], []
        for expected in (100, 200, 241):
            result = f.poll()
            statuses.extend(result["statuses"])
            skips.extend(result["skipped"])
            saved = json.loads(self.state_path.read_text())
            self.assertEqual(expected, saved["reporting_migration"]["processed"])
            self.assertEqual(1, saved["reporting_migration"]["version"])
            self.assertIsInstance(saved["reporting_migration"]["cursor"], str)
            f.worker._prune_finished_run_folders(0)
            self.assertTrue(source.is_dir(), "live evidence was pruned")
            if expected < 241:
                self.assertTrue(shared.is_dir(), "a still-reportable shared snapshot was pruned")
            if expected == 100:
                self.restart()
        saved = json.loads(self.state_path.read_text())
        self.assertTrue(saved["reporting_migration"]["complete"])
        self.assertEqual([live], saved["submissions"])
        self.assertEqual(240, len(saved["completed"]))
        tombstones = [
            row
            for row in saved["completed"].values()
            if row.get("reporting_disposition") == "snapshot_unreadable"
        ]
        self.assertEqual(120, len(tombstones))
        self.assertEqual(120, len(statuses))
        self.assertTrue(
            all(row["state"] == "success" and row["action"] == "posted" for row in statuses)
        )
        self.assertEqual(
            120, len({row["run_id"] for row in skips if row["reason"] == "snapshot_unreadable"})
        )
        self.assertEqual(
            120, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )
        self.assertEqual(
            120,
            f.worker.db.execute(
                "SELECT count(*) FROM status_posts WHERE state='success'"
            ).fetchone()[0],
        )
        self.assertFalse(shared.exists(), "reported expired evidence was not reclaimed")
        repeated = f.poll()
        self.assertEqual([], repeated["statuses"])
        self.assertEqual([], repeated["skipped"])
        self.assertEqual(0, repeated["migration"]["scanned"])

    def test_disposal_requires_a_terminal_run_missing_evidence_and_matching_commit(self):
        f = self.fixture
        params = {**self.params, "dispose": "snapshot_unreadable"}
        for state in ("queued", "running", "lost"):
            record = f.worker.get(self.run_id)
            record["state"] = state
            with f.worker.db:
                f.worker.save(record)
            with self.subTest(state=state), self.assertRaisesRegex(PollError, "safely terminal"):
                f.caller("run.status", params)
        record.update(state="failed", exit_code=None)
        with f.worker.db:
            f.worker.save(record)
        with self.assertRaisesRegex(PollError, "still readable"):
            f.caller("run.status", params)
        shutil.rmtree(f.state / "snapshots" / record["input"]["snapshot_id"])
        with self.assertRaisesRegex(PollError, "submitted commit"):
            f.caller("run.status", {**params, "tested_commit": "f" * 40})
        result = f.caller("run.status", params)
        self.assertEqual({"action": "disposed", "state": "error"}, result)
        validate_response("run.status", {"jsonrpc": "2.0", "id": 1, "result": result})
        self.restart()
        self.assertEqual(result, f.caller("run.status", params))
        self.assertEqual(
            1, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )
        self.assertEqual(
            0,
            f.worker.db.execute("SELECT count(*) FROM status_posts WHERE state='error'").fetchone()[
                0
            ],
        )
        with self.assertRaisesRegex(PollError, "already disposed"):
            f.caller("run.status", {**self.params, "record": "success"})

    def test_disposition_is_authoritative_if_snapshot_bytes_return(self):
        f = self.fixture
        record = f.worker.get(self.run_id)
        record.update(state="succeeded", exit_code=0)
        with f.worker.db:
            f.worker.save(record)
        snapshot = f.state / "snapshots" / record["input"]["snapshot_id"]
        saved_snapshot = f.root / "saved-snapshot"
        shutil.move(snapshot, saved_snapshot)
        f.caller("run.status", {**self.params, "dispose": "snapshot_unreadable"})
        shutil.move(saved_snapshot, snapshot)
        self.assertEqual(
            {"action": "disposed", "state": "error"},
            f.caller("run.status", self.params),
        )
        with self.assertRaisesRegex(PollError, "already disposed"):
            f.caller("run.status", {**self.params, "record": "success"})
        result = f.poll()
        self.assertEqual([], result["statuses"])
        self.assertEqual([], f.posts())
        self.assertEqual([], json.loads(self.state_path.read_text())["submissions"])

    def test_recorded_final_status_survives_pruning_before_poll_checkpoint(self):
        f = self.fixture
        record = f.worker.get(self.run_id)
        record.update(
            state="succeeded",
            exit_code=0,
            cleanup="confirmed_no_external_resources",
            finished_at=(datetime.now(UTC) - timedelta(days=91)).isoformat(),
        )
        with f.worker.db:
            f.worker.save(record)
        f.caller("run.status", {**self.params, "record": "success"})
        f.worker._prune_finished_run_folders(0)
        self.assertFalse((f.state / "snapshots" / record["input"]["snapshot_id"]).exists())
        with self.assertRaisesRegex(PollError, "snapshot is not readable"):
            f.caller("run.status", {**self.params, "tested_commit": "f" * 40})
        self.entry.pop("reporting_version", None)
        self.payload.pop("reporting_migration")
        self.state_path.write_text(canonical(self.payload))
        result = f.poll()
        self.assertEqual(
            [{"run_id": self.run_id, "state": "success", "action": "skip"}], result["statuses"]
        )
        self.assertEqual([], result["skipped"])
        self.assertEqual([], f.posts())
        self.assertEqual(
            0, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )
        saved = json.loads(self.state_path.read_text())
        self.assertEqual([], saved["submissions"])
        self.assertNotIn("reporting_disposition", saved["completed"][self.entry["key"]])
        self.assertEqual([], f.poll()["statuses"])

    def test_unknown_migration_version_refuses_before_state_write(self):
        self.payload["reporting_migration"]["version"] = 99
        self.state_path.write_text(canonical(self.payload))
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(PollError, "migration metadata"):
            migrate_submissions(self.fixture.state, caller=self.fixture.caller)
        with self.assertRaisesRegex(PollError, "migration metadata"):
            migrate_submissions(self.fixture.state, caller=self.fixture.caller, restart=True)
        self.assertEqual(before, self.state_path.read_bytes())

    def test_corrupt_disposed_evidence_is_reclaimable_in_plan_and_prune(self):
        f = self.fixture
        record = f.worker.get(self.run_id)
        record.update(
            state="failed",
            exit_code=None,
            cleanup="confirmed_no_external_resources",
            finished_at=(datetime.now(UTC) - timedelta(days=91)).isoformat(),
        )
        with f.worker.db:
            f.worker.save(record)
        snapshot = f.state / "snapshots" / record["input"]["snapshot_id"]
        (snapshot / "manifest.json").write_text("broken evidence")
        self.assertEqual([], storage_plan(f.state)["folders"])
        f.caller("run.status", {**self.params, "dispose": "snapshot_unreadable"})
        report = storage_plan(f.state)
        self.assertEqual([record["input"]["snapshot_id"]], [row["id"] for row in report["folders"]])
        self.assertGreater(report["freed_bytes"], 0)
        f.worker.state_usage.refresh()
        f.worker._prune_finished_run_folders(0)
        self.assertFalse(snapshot.exists())
        self.assertEqual("failed", f.worker.get(self.run_id)["state"])

    def test_lost_evidence_stays_even_after_an_error_status_was_recorded(self):
        f = self.fixture
        record = f.worker.get(self.run_id)
        record.update(
            state="lost",
            exit_code=None,
            cleanup="confirmed_no_external_resources",
            finished_at=(datetime.now(UTC) - timedelta(days=91)).isoformat(),
        )
        with f.worker.db:
            f.worker.save(record)
        f.caller("run.status", {**self.params, "record": "error"})
        self.assertEqual([], storage_plan(f.state)["folders"])
        f.worker._prune_finished_run_folders(0)
        self.assertTrue((f.state / "snapshots" / record["input"]["snapshot_id"]).is_dir())

    def test_setup_failure_on_an_old_head_still_gets_final_error(self):
        f = self.fixture
        old_tip = f._tip()
        record = f.worker.get(self.run_id)
        record.update(
            state="failed",
            exit_code=None,
            error={"kind": "SETUP_FAILED", "message": "fixture setup failure"},
        )
        with f.worker.db:
            f.worker.save(record)
        tip = f._push_main("newer.txt")
        f.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        f.server.etags["branches"] = "branches-2"
        result = f.poll()
        old_posts = [row for row in f.posts() if row["path"].endswith("/" + old_tip)]
        self.assertEqual(["error"], [json.loads(row["body"])["state"] for row in old_posts])
        self.assertIn(
            {"run_id": self.run_id, "state": "error", "action": "posted"}, result["statuses"]
        )
        f.server.requests.clear()
        f.poll()
        self.assertEqual([], [row for row in f.posts() if row["path"].endswith("/" + old_tip)])

    def test_crash_after_worker_disposal_repeats_safely_before_cursor_commit(self):
        self.legacy_missing()
        f = self.fixture
        before = self.state_path.read_bytes()

        def interrupted_save():
            raise RuntimeError("interrupted before poll-state checkpoint")

        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            migrate_reporting(
                json.loads(before), caller_for=lambda _entry: f.caller, save=interrupted_save
            )
        self.assertEqual(before, self.state_path.read_bytes())
        self.assertEqual(
            1, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )
        self.restart()
        result = migrate_submissions(f.state, caller=f.caller)
        self.assertEqual("tombstoned", result["items"][0]["disposition"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual([], saved["submissions"])
        self.assertEqual(
            "snapshot_unreadable", saved["completed"][self.entry["key"]]["reporting_disposition"]
        )
        self.assertEqual(
            1, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )
        self.assertEqual(0, migrate_submissions(f.state, caller=f.caller)["scanned"])

    def test_queued_running_lost_and_unreachable_references_are_unchanged(self):
        f = self.fixture
        original = f.worker.get(self.run_id)
        request = f.worker.db.execute(
            "SELECT request FROM runs WHERE id=?", (self.run_id,)
        ).fetchone()[0]
        entries = []
        with f.worker.db:
            for index, state in enumerate(("queued", "running", "lost")):
                record = copy.deepcopy(original)
                key = f"poll-{index:064x}"
                record.update(run_id=str(uuid.uuid4()), submission_key=key, state=state)
                f.worker.db.execute(
                    "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                    (record["run_id"], key, request, canonical(record), b""),
                )
                entries.append({**self.entry, "key": key, "run_id": record["run_id"]})
        entries.append({**self.entry, "key": "poll-" + "f" * 64, "host": "unreachable"})
        for row in entries:
            row.pop("reporting_version", None)
        self.payload["submissions"] = entries
        self.payload.pop("reporting_migration")
        self.state_path.write_text(canonical(self.payload))
        result = migrate_submissions(f.state, caller=f.caller)
        self.assertEqual(
            ["queued", "running", "lost", "unreachable"],
            [row["disposition"] for row in result["items"]],
        )
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(entries, saved["submissions"])
        self.assertEqual({}, saved["completed"])
        self.assertEqual(
            0, f.worker.db.execute("SELECT count(*) FROM reporting_dispositions").fetchone()[0]
        )

    def test_operator_cli_reports_before_applying_without_github_requests(self):
        self.legacy_missing()
        f = self.fixture
        f.worker.close()
        f.worker = Worker(f.clone, f.state)
        f.worker.execute_queue = lambda: f.worker.stop.wait()
        server = threading.Thread(target=f.worker.serve, daemon=True)
        server.start()

        def stop_server():
            f.worker.stop.set()
            server.join(timeout=5)

        self.addCleanup(stop_server)
        deadline = time.monotonic() + 5
        while f.worker.scheduler is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(f.worker.scheduler)
        before = self.state_path.read_bytes()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        command = [
            sys.executable,
            "-m",
            "execution_core",
            "--state",
            str(f.state),
            "submissions",
            "migrate",
        ]
        preview = subprocess.run(
            [*command, "--dry-run"], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(0, preview.returncode, preview.stderr)
        self.assertEqual("would_tombstone", json.loads(preview.stdout)["items"][0]["disposition"])
        self.assertEqual(before, self.state_path.read_bytes())
        applied = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual("tombstoned", json.loads(applied.stdout)["items"][0]["disposition"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual([], saved["submissions"])
        self.assertEqual(1, len(saved["completed"]))
        self.assertEqual([], f.server.requests)


if __name__ == "__main__":
    unittest.main()
