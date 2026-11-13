# SPDX-License-Identifier: MIT

import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from execution_core import cli, poll
from execution_core.checks import post_commit_status
from execution_core.poll import PollError
from execution_core.submissions import (
    abandon_submission,
    capacity_report,
    list_submissions,
    recorded_status_states,
    repair_submission,
    retry_submission,
    stored_request,
)

KEY = "poll-" + "ab" * 32
SUCCESSOR = "poll-62a441d9f31c6d7aa0b6edc5d12d0c00c9ea3a235b56fae6d9bc82db962bd777"
SHA = "cd" * 20
TESTED = "ef" * 20
CONTEXT = "rookrunner/check.yml/test"


def _entry(key=KEY, run_id="run-setup", event="push"):
    return {
        "key": key,
        "run_id": run_id,
        "event": event,
        "tested_commit": TESTED,
        "status_sha": SHA,
        "context": CONTEXT,
        "pending": True,
    }


def _capacity_entry(key, since, workflow, job_id, ref="refs/heads/main"):
    return {
        "key": key,
        "event": "push",
        "waiting": {
            "since": since,
            "reason": "capacity",
            "ref": ref,
            "request": {"workflow": workflow, "job_id": job_id},
        },
    }


def _write_poll(directory, submissions, completed=None):
    payload = {
        "version": 1,
        "repository": "acme/demo",
        "submissions": submissions,
        "completed": {} if completed is None else completed,
    }
    path = Path(directory) / "poll.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _setup_record():
    return {
        "state": "failed",
        "exit_code": None,
        "error": {"kind": "SETUP_FAILED", "message": "image digest will not resolve"},
    }


def _none(*_args):
    return set()


class OutcomeTests(unittest.TestCase):
    def test_commit_status_is_posted_for_a_neutral_setup_check(self):
        decision = {
            "state": "error",
            "check_conclusion": "neutral",
            "check_status": "completed",
            "check_summary": "setup error",
        }
        self.assertTrue(post_commit_status(decision))
        self.assertFalse(post_commit_status({**decision, "status_recorded": True}))
        self.assertTrue(post_commit_status({"state": "failure", "check_conclusion": "failure"}))
        self.assertFalse(post_commit_status({"state": "success", "check_conclusion": "neutral"}))

    def test_poll_posts_error_for_a_setup_failure_and_retires_it(self):
        entry = _entry()
        passed = poll.Pass.__new__(poll.Pass)
        passed.caller = lambda method, params: (
            _setup_record() if method == "run.get" else {"action": "retry", "state": "error"}
        )
        passed.app_key = None
        passed.state = {"submissions": [entry], "completed": {}}
        passed.result = {"statuses": []}
        passed.sleep = lambda _seconds: None
        posted = []

        def fake_post(_entry, state, *_args, **_kwargs):
            posted.append(state)
            return True

        passed._post = fake_post
        self.assertTrue(passed._post_record(entry))
        self.assertEqual(posted, ["error"])
        self.assertEqual(passed.result["statuses"][0]["action"], "posted")
        self.assertEqual(passed.result["statuses"][0]["state"], "error")

    def test_poll_does_not_post_when_a_successor_owns_the_context(self):
        entry = _entry()
        passed = poll.Pass.__new__(poll.Pass)
        passed.caller = lambda method, params: (
            _setup_record() if method == "run.get" else {"action": "retry", "state": "error"}
        )
        passed.app_key = None
        passed.state = {
            "submissions": [entry, {"key": SUCCESSOR, "retry_of": KEY}],
            "completed": {},
        }
        passed.result = {"statuses": []}
        passed.sleep = lambda _seconds: None
        posted = []
        passed._post = lambda *_args, **_kwargs: posted.append("posted") or True
        self.assertTrue(passed._post_record(entry))
        self.assertEqual(posted, [])
        self.assertEqual(passed.result["statuses"][0]["action"], "superseded")
        self.assertTrue(entry["superseded"])

    def test_poll_refuses_a_retry_decision_that_is_not_error(self):
        entry = _entry()
        passed = poll.Pass.__new__(poll.Pass)
        passed.caller = lambda method, params: (
            {"state": "failed", "exit_code": 1, "error": None}
            if method == "run.get"
            else {"action": "retry", "state": "failure"}
        )
        passed.app_key = None
        passed.state = {"submissions": [entry], "completed": {}}
        passed.result = {"statuses": []}
        passed.sleep = lambda _seconds: None
        posted = []
        passed._post = lambda *_args, **_kwargs: posted.append("posted") or True
        with self.assertRaises(PollError) as raised:
            passed._post_record(entry)
        self.assertEqual(str(raised.exception), "setup failure did not map to error")
        self.assertEqual(posted, [])

    def test_poll_neutral_setup_check_requests_an_error_commit_status(self):
        passed = poll.Pass.__new__(poll.Pass)
        passed.app_key = "/keys/private-key.pem"
        passed.key_dir = None
        passed.api_base = "https://example.invalid"
        passed.repository = "acme/demo"
        passed.state_dir = Path("/tmp/submission-outcome-state")
        passed.ssh_host = None
        passed.budget = SimpleNamespace(stopped=False, observe=lambda _remaining: None)

        def caller(method, _params):
            if method == "worker.describe":
                return {"repository": "/work/repo"}
            if method == "run.status":
                return {"action": "recorded", "state": "error"}
            raise AssertionError(method)

        passed.caller = caller
        decision = {
            "action": "post",
            "state": "error",
            "check_status": "completed",
            "check_conclusion": "neutral",
            "check_summary": "setup error",
        }
        with mock.patch("execution_core.poll.post_check_flow") as flow:
            flow.return_value = SimpleNamespace(
                error=None, check_id=9, status_posted=True, status_remaining=4
            )
            self.assertTrue(passed._post(_entry(), "error", decision))
        self.assertEqual(flow.call_args.kwargs["status_state"], "error")
        self.assertTrue(flow.call_args.kwargs["post_status_request"])
        decision["status_recorded"] = True
        with mock.patch("execution_core.poll.post_check_flow") as flow:
            flow.return_value = SimpleNamespace(
                error=None, check_id=9, status_posted=False, status_remaining=None
            )
            passed._post(_entry(), "error", decision)
        self.assertFalse(flow.call_args.kwargs["post_status_request"])

    def test_list_keeps_only_submissions_without_a_final_status(self):
        with tempfile.TemporaryDirectory() as directory:
            stuck = _entry()
            done = _entry(key="poll-" + "cd" * 32, run_id="run-ok")
            _write_poll(directory, [stuck, done, {"key": "poll-" + "11" * 32}])

            def caller(method, params):
                if params.get("run_id") == "run-ok":
                    if method == "run.get":
                        return {"state": "succeeded", "exit_code": 0, "error": None}
                    return {"action": "skip", "state": "success"}
                if method == "run.get":
                    return _setup_record()
                return {"action": "retry", "state": "error"}

            listed = list_submissions(directory, caller=caller, recorded_states=_none, dry_run=True)
            self.assertEqual(
                [row["key"] for row in listed["submissions"]], [KEY, "poll-" + "11" * 32]
            )
            self.assertTrue(all(row["stuck"] for row in listed["submissions"]))
            self.assertEqual(listed["submissions"][0]["error_kind"], "SETUP_FAILED")
            self.assertEqual(listed["submissions"][0]["final"], "error")
            before = Path(directory, "poll.json").read_bytes()
            again = list_submissions(directory, caller=caller, recorded_states=_none, dry_run=True)
            self.assertEqual(Path(directory, "poll.json").read_bytes(), before)
            self.assertEqual(
                [row["run_id"] for row in again["submissions"] if row.get("run_id")], ["run-setup"]
            )

    def test_repair_dry_run_leaves_a_setup_failure_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_poll(directory, [_entry()])
            before = path.read_bytes()
            posted = []

            def caller(method, _params):
                if method == "run.get":
                    return _setup_record()
                return {"action": "retry", "state": "error"}

            result = repair_submission(
                directory,
                KEY,
                caller=caller,
                recorded_states=_none,
                post=lambda *_args: posted.append("posted"),
                dry_run=True,
            )
            self.assertEqual(result["action"], "would_post")
            self.assertEqual(result["state"], "error")
            self.assertTrue(result["dry_run"])
            self.assertEqual(posted, [])
            self.assertEqual(path.read_bytes(), before)

    def test_repair_posts_error_for_a_setup_failure_and_a_second_call_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            posted = []
            calls = []

            def caller(method, params):
                calls.append((method, params.get("record")))
                if method == "run.get":
                    return _setup_record()
                if params.get("record") == "error":
                    return {"action": "recorded", "state": "error"}
                return {"action": "retry", "state": "error"}

            result = repair_submission(
                directory,
                KEY,
                caller=caller,
                recorded_states=_none,
                post=lambda sha, state, context: posted.append((sha, state, context)),
            )
            self.assertEqual(result["action"], "posted")
            self.assertEqual(posted, [(SHA, "error", CONTEXT)])
            self.assertEqual(calls[-1], ("run.status", "error"))
            saved = json.loads(Path(directory, "poll.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["submissions"], [])
            self.assertEqual(saved["completed"][KEY]["run_id"], "run-setup")
            again = repair_submission(
                directory,
                KEY,
                caller=caller,
                recorded_states=lambda *_args: {"error"},
                post=lambda *_args: posted.append("again"),
            )
            self.assertEqual(again["action"], "already_final")
            self.assertEqual(posted, [(SHA, "error", CONTEXT)])

    def test_repair_posts_success_only_when_the_run_succeeded(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(run_id="run-ok")])
            posted = []

            def caller(method, params):
                if method == "run.get":
                    return {"state": "succeeded", "exit_code": 0, "error": None}
                if params.get("record") == "success":
                    return {"action": "recorded", "state": "success"}
                return {"action": "post", "state": "success"}

            result = repair_submission(
                directory,
                KEY,
                caller=caller,
                recorded_states=_none,
                post=lambda _sha, state, _context: posted.append(state),
            )
            self.assertEqual((result["action"], posted), ("posted", ["success"]))

    def test_repair_refuses_a_decision_that_disagrees_with_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_poll(directory, [_entry()])
            before = path.read_bytes()
            posted = []

            def caller(method, _params):
                if method == "run.get":
                    return _setup_record()
                return {"action": "post", "state": "success"}

            with self.assertRaises(PollError) as raised:
                repair_submission(
                    directory,
                    KEY,
                    caller=caller,
                    recorded_states=_none,
                    post=lambda *_args: posted.append("posted"),
                )
            self.assertEqual(str(raised.exception), "worker final state does not match the run")
            self.assertEqual(posted, [])
            self.assertEqual(path.read_bytes(), before)

    def test_repair_does_not_post_when_a_completed_list_names_the_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()], completed=[SUCCESSOR])
            posted = []
            result = repair_submission(
                directory,
                KEY,
                caller=lambda *_args: self.fail("worker was called"),
                recorded_states=_none,
                post=lambda *_args: posted.append("posted"),
            )
            self.assertEqual(result["action"], "superseded")
            self.assertEqual(posted, [])

    def test_repair_does_not_post_over_a_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(), {"key": SUCCESSOR, "retry_of": KEY, "run_id": "new"}])
            posted = []
            result = repair_submission(
                directory,
                KEY,
                caller=lambda *_args: self.fail("worker was called"),
                recorded_states=_none,
                post=lambda *_args: posted.append("posted"),
            )
            self.assertEqual(result["action"], "superseded")
            self.assertEqual(posted, [])
            saved = json.loads(Path(directory, "poll.json").read_text(encoding="utf-8"))
            self.assertNotIn(KEY, [item.get("key") for item in saved["submissions"]])
            self.assertTrue(saved["completed"][KEY]["superseded"])

    def test_abandon_posts_error_and_refuses_success(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            posted = []
            recorded = []

            def caller(method, params):
                if method == "run.get":
                    return _setup_record()
                recorded.append(params.get("record"))
                return {"action": "recorded", "state": "error"}

            result = abandon_submission(
                directory,
                KEY,
                caller=caller,
                post=lambda _sha, state, _context: posted.append(state),
            )
            self.assertEqual(result["action"], "abandoned")
            self.assertEqual(posted, ["error"])
            self.assertEqual(recorded, ["error"])
            saved = json.loads(Path(directory, "poll.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["completed"][KEY]["abandoned"])
            self.assertEqual(saved["submissions"], [])

        with tempfile.TemporaryDirectory() as directory:
            path = _write_poll(directory, [_entry(run_id="run-ok")])
            before = path.read_bytes()
            posted = []

            def caller(method, _params):
                if method == "run.get":
                    return {"state": "succeeded", "exit_code": 0, "error": None}
                raise AssertionError(method)

            with self.assertRaises(PollError) as raised:
                abandon_submission(
                    directory,
                    KEY,
                    caller=caller,
                    post=lambda *_args: posted.append("posted"),
                )
            self.assertEqual(str(raised.exception), "abandon does not post success")
            self.assertEqual(posted, [])
            self.assertEqual(path.read_bytes(), before)

    def test_abandon_of_a_workflow_failure_records_failure_and_posts_error(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            posted = []

            def caller(method, params):
                if method == "run.get":
                    return {"state": "failed", "exit_code": 1, "error": None}
                if params.get("record") != "failure":
                    raise PollError("INVALID_PARAMS", "recorded state does not match the run")
                return {"action": "recorded", "state": "failure"}

            abandon_submission(
                directory,
                KEY,
                caller=caller,
                post=lambda _sha, state, _context: posted.append(state),
            )
            self.assertEqual(posted, ["error"])

    def test_abandon_dry_run_does_not_cancel_a_queued_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_poll(directory, [_entry()])
            before = path.read_bytes()
            calls = []

            def caller(method, _params):
                calls.append(method)
                return {"state": "queued", "exit_code": None, "error": None}

            result = abandon_submission(
                directory,
                KEY,
                caller=caller,
                post=lambda *_args: self.fail("posted"),
                dry_run=True,
            )
            self.assertEqual(result["action"], "would_cancel")
            self.assertEqual(calls, ["run.get"])
            self.assertEqual(path.read_bytes(), before)

    def test_retry_submits_one_derived_successor_and_repeats_as_the_same_one(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            submits = []
            request = {
                "version": 1,
                "workflow": "workflows/operator-retry.yml",
                "job_id": "test",
                "event": {"after": TESTED},
                "event_name": "push",
            }

            def caller(method, params):
                if method == "run.get":
                    return _setup_record()
                if method == "run.submit":
                    submits.append(params)
                    return {"run_id": "run-next"}
                raise AssertionError(method)

            dry = retry_submission(
                directory,
                KEY,
                caller=caller,
                request_for=lambda _run_id: request,
                dry_run=True,
            )
            self.assertEqual(dry["action"], "would_submit")
            self.assertEqual(dry["submission_key"], SUCCESSOR)
            self.assertEqual(dry["workflow"], "workflows/operator-retry.yml")
            self.assertEqual(submits, [])
            self.assertEqual(
                len(json.loads(Path(directory, "poll.json").read_text())["submissions"]), 1
            )
            live = retry_submission(
                directory, KEY, caller=caller, request_for=lambda _run_id: request
            )
            self.assertEqual(live["action"], "submitted")
            self.assertEqual(live["run_id"], "run-next")
            self.assertEqual(submits[0]["submission_key"], SUCCESSOR)
            self.assertEqual(submits[0]["workflow"], "workflows/operator-retry.yml")
            self.assertEqual(submits[0]["event"], {"after": TESTED})
            again = retry_submission(
                directory,
                KEY,
                caller=caller,
                request_for=lambda _run_id: (_ for _ in ()).throw(AssertionError("request read")),
            )
            self.assertEqual(again["action"], "already_retried")
            self.assertEqual(again["submission_key"], SUCCESSOR)
            self.assertEqual(len(submits), 1)

    def test_retry_refuses_a_succeeded_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(run_id="run-ok")])
            submits = []

            def caller(method, params):
                if method == "run.get":
                    return {"state": "succeeded", "exit_code": 0, "error": None}
                submits.append(params)
                return {"run_id": "nope"}

            with self.assertRaises(PollError) as raised:
                retry_submission(
                    directory,
                    KEY,
                    caller=caller,
                    request_for=lambda _run_id: {"version": 1, "workflow": "w.yml"},
                )
            self.assertEqual(str(raised.exception), "only a failed or cancelled run can be retried")
            self.assertEqual(submits, [])

    def test_retry_submits_a_successor_of_a_cancelled_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(run_id="run-cancelled")])
            submits = []

            def caller(method, params):
                if method == "run.get":
                    return {"state": "cancelled", "exit_code": None, "error": None}
                submits.append(params)
                return {"run_id": "run-next"}

            live = retry_submission(
                directory,
                KEY,
                caller=caller,
                request_for=lambda _run_id: {"version": 1, "workflow": "w.yml", "job_id": "gui"},
            )
            self.assertEqual(live["action"], "submitted")
            self.assertEqual(submits[0]["submission_key"], SUCCESSOR)
            saved = json.loads(Path(directory, "poll.json").read_text())["submissions"]
            self.assertEqual(saved[-1]["retry_of"], KEY)
            self.assertEqual(saved[-1]["run_id"], "run-next")

    def test_retry_of_a_pull_request_run_carries_the_activity_type_of_its_event(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(event="pull_request")])
            submits = []
            request = {
                "version": 1,
                "workflow": "w.yml",
                "job_id": "test",
                "event_name": "pull_request",
                "event": {"action": "synchronize", "number": 158},
            }

            def caller(method, params):
                if method == "run.get":
                    return _setup_record()
                submits.append(params)
                return {"run_id": "run-next"}

            retry_submission(directory, KEY, caller=caller, request_for=lambda _run_id: request)
            self.assertEqual(submits[0]["activity_type"], "synchronize")

    def test_retry_of_a_push_run_has_no_activity_type(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            submits = []
            request = {
                "version": 1,
                "workflow": "w.yml",
                "job_id": "test",
                "event_name": "push",
                "event": {"after": TESTED, "action": "created"},
            }

            def caller(method, params):
                if method == "run.get":
                    return _setup_record()
                submits.append(params)
                return {"run_id": "run-next"}

            retry_submission(directory, KEY, caller=caller, request_for=lambda _run_id: request)
            self.assertNotIn("activity_type", submits[0])

    def test_retry_refuses_a_running_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry(run_id="run-live")])
            submits = []

            def caller(method, params):
                if method == "run.get":
                    return {"state": "running", "exit_code": None, "error": None}
                submits.append(params)
                return {"run_id": "nope"}

            with self.assertRaises(PollError):
                retry_submission(
                    directory,
                    KEY,
                    caller=caller,
                    request_for=lambda _run_id: {"version": 1, "workflow": "w.yml"},
                )
            self.assertEqual(submits, [])

    def test_stored_request_returns_the_saved_workflow_request(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "runs.sqlite3"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE runs (id TEXT, request TEXT, record TEXT, submission_key TEXT, log BLOB)"
            )
            request = {"version": 1, "workflow": "workflows/operator-retry.yml", "job_id": "test"}
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                ("run-setup", json.dumps(request), "{}", "poll-old", b""),
            )
            db.commit()
            db.close()
            self.assertEqual(stored_request(db_path, "run-setup")["workflow"], request["workflow"])
            self.assertEqual(
                recorded_status_states(db_path, "run-setup", CONTEXT, SHA),
                set(),
            )


class CapacityReportTests(unittest.TestCase):
    def test_report_orders_waits_and_marks_threshold_equality_breached(self):
        with tempfile.TemporaryDirectory() as directory:
            oldest = _capacity_entry(
                "poll-" + "aa" * 32,
                "2026-11-11T11:59:30Z",
                "workflows/build.yml",
                "build",
            )
            newer = _capacity_entry(
                "poll-" + "bb" * 32,
                "2026-11-11T12:00:00+00:00",
                "workflows/test.yml",
                "test",
                "refs/heads/release",
            )
            path = _write_poll(directory, [newer, oldest, _entry()])
            before = path.read_bytes()

            result = capacity_report(
                directory,
                alert_after_seconds=60,
                now=datetime(2026, 11, 11, 12, 0, 30, tzinfo=UTC),
            )

            self.assertEqual(result["repository"], "acme/demo")
            self.assertEqual(result["queue_depth"], 2)
            self.assertEqual(result["oldest_since"], "2026-11-11T11:59:30+00:00")
            self.assertEqual(result["oldest_age_seconds"], 60)
            self.assertEqual(result["alert_after_seconds"], 60)
            self.assertEqual(result["alert"], "breached")
            self.assertEqual(
                result["waits"],
                [
                    {
                        "key": oldest["key"],
                        "since": "2026-11-11T11:59:30+00:00",
                        "age_seconds": 60,
                        "event": "push",
                        "ref": "refs/heads/main",
                        "workflow": "workflows/build.yml",
                        "job_id": "build",
                    },
                    {
                        "key": newer["key"],
                        "since": "2026-11-11T12:00:00+00:00",
                        "age_seconds": 30,
                        "event": "push",
                        "ref": "refs/heads/release",
                        "workflow": "workflows/test.yml",
                        "job_id": "test",
                    },
                ],
            )
            clear = capacity_report(
                directory,
                alert_after_seconds=61,
                now=datetime(2026, 11, 11, 12, 0, 30, tzinfo=UTC),
            )
            self.assertEqual(clear["alert"], "clear")
            self.assertEqual(path.read_bytes(), before)

    def test_empty_queue_has_no_oldest_age_and_no_enabled_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [])

            result = capacity_report(
                directory,
                now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
            )

            self.assertEqual(result["queue_depth"], 0)
            self.assertIsNone(result["oldest_since"])
            self.assertIsNone(result["oldest_age_seconds"])
            self.assertIsNone(result["alert_after_seconds"])
            self.assertEqual(result["alert"], "not_configured")
            self.assertEqual(result["waits"], [])

    def test_future_wait_time_is_invalid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(
                directory,
                [_capacity_entry("poll-" + "aa" * 32, "2026-11-11T12:00:01Z", "w.yml", "job")],
            )

            with self.assertRaises(PollError) as raised:
                capacity_report(
                    directory,
                    now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
                )

            self.assertEqual(raised.exception.kind, "STATE_INVALID")


class CliTests(unittest.TestCase):
    def _argv(self, directory, *extra):
        return [
            "execution_core",
            "--state",
            directory,
            "submissions",
            *extra,
            "--poll-state",
            directory,
        ]

    def test_dry_run_repair_command_posts_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_poll(directory, [_entry()])
            before = path.read_bytes()

            def caller(method, _params):
                if method == "run.get":
                    return _setup_record()
                if method == "run.status":
                    return {"action": "retry", "state": "error"}
                raise AssertionError(method)

            out = io.StringIO()
            with (
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "post_status") as post_status,
                mock.patch.object(sys, "argv", self._argv(directory, "repair", KEY, "--dry-run")),
                redirect_stdout(out),
            ):
                cli.main()
            result = json.loads(out.getvalue())
            self.assertEqual(result["action"], "would_post")
            self.assertEqual(result["state"], "error")
            post_status.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_capacity_command_reports_breach_without_worker_or_state_write(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as worker_state,
        ):
            path = _write_poll(
                directory,
                [_capacity_entry(KEY, "2026-11-11T11:59:00Z", "workflows/ci.yml", "check")],
            )
            before = path.read_bytes()
            out = io.StringIO()
            argv = [
                "execution_core",
                "--state",
                worker_state,
                "submissions",
                "capacity",
                "--alert-after-seconds",
                "60",
                "--poll-state",
                directory,
            ]
            with (
                mock.patch.object(cli, "_worker_caller", side_effect=AssertionError("worker call")),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cli, "datetime") as clock,
                redirect_stdout(out),
            ):
                clock.now.return_value = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
                with self.assertRaises(SystemExit) as exited:
                    cli.main()
            result = json.loads(out.getvalue())
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(result["alert"], "breached")
            self.assertEqual(result["queue_depth"], 1)
            self.assertEqual(result["oldest_age_seconds"], 60)
            self.assertEqual(path.read_bytes(), before)

    def test_capacity_command_reports_unknown_for_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            err = io.StringIO()
            with (
                mock.patch.object(cli, "_worker_caller", side_effect=AssertionError("worker call")),
                mock.patch.object(sys, "argv", self._argv(directory, "capacity")),
                redirect_stdout(out),
                redirect_stderr(err),
                self.assertRaises(SystemExit) as exited,
            ):
                cli.main()

            result = json.loads(err.getvalue())
            self.assertEqual(exited.exception.code, 2)
            self.assertEqual(result["alert"], "unknown")
            self.assertEqual(result["error"]["kind"], "STATE_INVALID")
            self.assertEqual(out.getvalue(), "")

    def test_capacity_command_exits_zero_when_threshold_is_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(
                directory,
                [_capacity_entry(KEY, "2026-11-11T11:59:30Z", "workflows/ci.yml", "check")],
            )
            out = io.StringIO()
            with (
                mock.patch.object(cli, "_worker_caller", side_effect=AssertionError("worker call")),
                mock.patch.object(
                    sys, "argv", self._argv(directory, "capacity", "--alert-after-seconds", "60")
                ),
                mock.patch.object(cli, "datetime") as clock,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as exited,
            ):
                clock.now.return_value = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
                cli.main()

            result = json.loads(out.getvalue())
            self.assertEqual(exited.exception.code, 0)
            self.assertEqual(result["alert"], "clear")
            self.assertEqual(result["oldest_age_seconds"], 30)

    def test_live_repair_command_posts_the_error_status(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            calls = []

            def caller(method, params):
                calls.append((method, params.get("record")))
                if method == "worker.describe":
                    return {"repository": "/work/repo"}
                if method == "run.get":
                    return _setup_record()
                if method == "run.status" and params.get("record") == "error":
                    return {"action": "recorded", "state": "error"}
                if method == "run.status":
                    return {"action": "retry", "state": "error"}
                raise AssertionError(method)

            out = io.StringIO()
            argv = self._argv(
                directory,
                "repair",
                KEY,
                "--repository",
                "acme/demo",
                "--credential-file",
                "/credential",
            )
            with (
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token"),
                mock.patch.object(cli, "post_status") as post_status,
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(out),
            ):
                cli.main()
            self.assertEqual(json.loads(out.getvalue())["action"], "posted")
            post_status.assert_called_once_with(
                "https://api.github.com", "acme/demo", SHA, "error", CONTEXT, "token"
            )
            self.assertIn(("run.status", "error"), calls)
            saved = json.loads(Path(directory, "poll.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["submissions"], [])

    def test_each_command_accepts_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_poll(directory, [_entry()])
            db_path = Path(directory) / "runs.sqlite3"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE runs (id TEXT, request TEXT)")
            db.execute(
                "INSERT INTO runs VALUES (?, ?)",
                (
                    "run-setup",
                    json.dumps(
                        {
                            "version": 1,
                            "workflow": "workflows/operator-retry.yml",
                            "job_id": "test",
                            "event": {"after": TESTED},
                            "event_name": "push",
                        }
                    ),
                ),
            )
            db.commit()
            db.close()

            def caller(method, _params):
                if method == "run.get":
                    return _setup_record()
                if method == "run.status":
                    return {"action": "retry", "state": "error"}
                raise AssertionError(method)

            before = Path(directory, "poll.json").read_bytes()
            for command in ("list", "inspect", "abandon", "retry", "repair"):
                extra = [command, "--dry-run"]
                if command != "list":
                    extra.append(KEY)
                out = io.StringIO()
                with (
                    mock.patch.object(cli, "_worker_caller", return_value=caller),
                    mock.patch.object(cli, "post_status") as post_status,
                    mock.patch.object(sys, "argv", self._argv(directory, *extra)),
                    redirect_stdout(out),
                ):
                    cli.main()
                payload = json.loads(out.getvalue())
                self.assertTrue(payload["dry_run"], command)
                post_status.assert_not_called()
            self.assertEqual(Path(directory, "poll.json").read_bytes(), before)
