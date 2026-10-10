# SPDX-License-Identifier: MPL-2.0

import json
from pathlib import Path
import tempfile
import unittest
import uuid

from execution_core.protocol import canonical
from execution_core.worker import Worker
from schema_support import SCHEMA, validator


MERGE = "ab" * 20
TIP = "cd" * 20
LOGIN = "page-hidden-login"
KEY = "page-hidden-submission-key"
STARTED = "2026-10-09T00:00:00+00:00"
FINISHED = "2026-10-09T00:00:12+00:00"
STAMP = "2026-10-09T00:00:12+00:00"


def _request(method, params):
    return canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()


def _record(run_id, *, state, started_at, finished_at, exit_code, concurrency=None, key=None):
    record = {
        "run_id": run_id,
        "worker_id": "00000000-0000-4000-8000-0000000000aa",
        "submission_key": run_id if key is None else key,
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
    return record


class StatusViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.worker = Worker(self.repo, self.state)
        self.worker.start()
        self.worker.stop.set()
        self.worker.scheduler.join(timeout=5)
        if self.worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        self.worker.stop.clear()

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def call(self, method, params):
        return self.worker.response(_request(method, params))

    def view(self):
        reply = self.call("status.view", {})
        self.assertNotIn("error", reply)
        return reply["result"]

    def insert(self, record, request, checks=(), statuses=()):
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
            for sha in statuses:
                self.worker.db.execute(
                    "INSERT INTO status_posts(run_id, context, sha, state) VALUES (?, ?, ?, ?)",
                    (record["run_id"], "rookrunner/rookrunner.yml/checks", sha, "success"),
                )
        return record["run_id"]

    def runs(self):
        return self.worker.db.execute("SELECT count(*) FROM runs").fetchone()[0]

    def test_a_twelve_second_span_is_finished(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {
                "event_name": "push",
                "event": {"after": TIP, "repository": {"full_name": "acme/demo"}},
            },
        )
        view = self.view()
        row = view["runs"][0]
        self.assertEqual(row["run_id"], run_id)
        self.assertEqual(row["repository"], "acme/demo")
        self.assertEqual(row["duration"], "finished")
        self.assertEqual(row["duration_seconds"], 12)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(view["poll"], {"completed_at": None, "repository": None})
        self.assertEqual(view["worker"]["version"], "0.1.0")
        self.assertIs(view["worker"]["ready"], True)
        self.assertIsNone(view["worker"]["readiness_error"])
        self.assertIsNone(view["worker"]["runner_image"])
        self.assertEqual(view["ungrouped"], 0)

    def test_a_null_start_is_unstarted(self):
        queued = str(uuid.uuid4())
        cancelled = str(uuid.uuid4())
        self.insert(
            _record(queued, state="queued", started_at=None, finished_at=None, exit_code=None),
            {},
        )
        self.insert(
            _record(
                cancelled,
                state="cancelled",
                started_at=None,
                finished_at=FINISHED,
                exit_code=None,
            ),
            {},
        )
        found = {row["run_id"]: row for row in self.view()["runs"]}
        for run_id in (queued, cancelled):
            self.assertEqual(found[run_id]["duration"], "unstarted")
            self.assertIsNone(found[run_id]["duration_seconds"])

    def test_a_naive_start_is_not_twelve_seconds(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id,
                state="succeeded",
                started_at="2026-10-09T00:00:00",
                finished_at="2026-10-09T00:00:12",
                exit_code=0,
            ),
            {},
        )
        row = self.view()["runs"][0]
        self.assertEqual(row["duration"], "none")
        self.assertIsNone(row["duration_seconds"])
        self.assertNotEqual(row["duration_seconds"], 12)

    def test_a_pull_request_number_hides_the_merge_sha(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {
                "event_name": "pull_request",
                "event": {
                    "number": 12,
                    "after": MERGE,
                    "repository": {"full_name": "moonbase2090/lightwell"},
                },
            },
        )
        row = self.view()["runs"][0]
        self.assertEqual(row["subject"], {"kind": "pull_request", "number": 12})
        self.assertNotIn(MERGE, canonical(self.view()))

    def test_a_push_after_is_the_commit(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {
                "event_name": "push",
                "event": {"after": TIP, "repository": {"full_name": "acme/demo"}},
            },
        )
        self.assertEqual(self.view()["runs"][0]["subject"], {"kind": "commit", "sha": TIP})

    def test_two_check_rows_have_no_url(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {"event_name": "push", "event": {"repository": {"full_name": "acme/demo"}}},
            checks=((TIP, 4), (MERGE, 5)),
        )
        self.assertIsNone(self.view()["runs"][0]["check_url"])

    def test_one_check_row_builds_the_runs_url(self):
        run_id = str(uuid.uuid4())
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
        self.assertEqual(
            self.view()["runs"][0]["check_url"],
            "https://github.com/moonbase2090/lightwell/runs/4",
        )

    def test_the_view_omits_paths_the_login_and_the_submission_key(self):
        state_path = str(self.worker.state)
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id,
                state="queued",
                started_at=None,
                finished_at=None,
                exit_code=None,
                key=KEY,
                concurrency=[
                    {"group": "refs/heads/main", "queue": "max", "cancel_in_progress": False},
                    {"group": state_path, "queue": "single", "cancel_in_progress": False},
                    {"group": "foo/../../etc", "queue": "single", "cancel_in_progress": False},
                ],
            ),
            {
                "event_name": "push",
                "event": {
                    "after": TIP,
                    "repository": {"full_name": "acme/demo"},
                    "commits": [{"author": {"login": LOGIN}}],
                },
            },
        )
        result = self.view()
        text = canonical(result)
        self.assertNotIn(state_path, text)
        self.assertNotIn(self.worker.repository, text)
        self.assertNotIn(LOGIN, text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("foo/../../etc", text)
        shown = [row for row in result["groups"] if row["key"] == "not shown"]
        self.assertEqual(
            shown,
            [
                {
                    "key": "not shown",
                    "names": ["not shown"],
                    "queue": "single",
                    "cancel_in_progress": False,
                    "queued": 1,
                    "running": 0,
                    "pending_limit": None,
                }
            ],
        )
        kept = [row for row in result["groups"] if row["key"] == "refs/heads/main"]
        self.assertEqual(kept[0]["names"], ["refs/heads/main"])
        self.assertEqual(kept[0]["queue"], "max")
        self.assertEqual(kept[0]["pending_limit"], 100)
        self.assertIn("refs/heads/main", result["queue"][0]["groups"])
        self.assertNotIn(state_path, result["queue"][0]["groups"])

    def test_a_rejected_repository_keeps_the_stamp_and_adds_no_run(self):
        before = self.runs()
        recorded = self.call(
            "poll.record",
            {"completed_at": STAMP, "repository": "acme/demo"},
        )
        self.assertNotIn("error", recorded)
        self.assertEqual(
            self.view()["poll"],
            {"completed_at": STAMP, "repository": "acme/demo"},
        )
        rejected = self.call(
            "poll.record",
            {"completed_at": "2026-10-09T00:00:13+00:00", "repository": "acme"},
        )
        self.assertEqual(rejected["error"]["data"]["kind"], "INVALID_PARAMS")
        self.assertEqual(self.view()["poll"]["completed_at"], STAMP)
        self.assertEqual(self.view()["poll"]["repository"], "acme/demo")
        self.assertEqual(self.runs(), before)

    def test_a_naive_completed_at_leaves_the_stamp(self):
        self.call("poll.record", {"completed_at": STAMP, "repository": "acme/demo"})
        rejected = self.call(
            "poll.record",
            {"completed_at": "2026-10-09T00:00:00", "repository": "acme/demo"},
        )
        self.assertEqual(rejected["error"]["data"]["kind"], "INVALID_PARAMS")
        self.assertEqual(self.view()["poll"]["completed_at"], STAMP)

    def test_two_views_of_a_finished_run_share_duration_seconds(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {},
        )
        first = self.view()["runs"][0]["duration_seconds"]
        second = self.view()["runs"][0]["duration_seconds"]
        self.assertEqual(first, 12)
        self.assertEqual(second, 12)

    def test_a_running_peer_marks_the_queued_run_waiting(self):
        group = [{"group": "refs/heads/main", "queue": "max", "cancel_in_progress": False}]
        queued = str(uuid.uuid4())
        running = str(uuid.uuid4())
        self.insert(
            _record(
                queued,
                state="queued",
                started_at=None,
                finished_at=None,
                exit_code=None,
                concurrency=group,
            ),
            {
                "event_name": "push",
                "event": {"after": TIP, "repository": {"full_name": "acme/demo"}},
            },
        )
        self.insert(
            _record(
                running,
                state="running",
                started_at=STARTED,
                finished_at=None,
                exit_code=None,
                concurrency=group,
            ),
            {
                "event_name": "push",
                "event": {"after": TIP, "repository": {"full_name": "acme/demo"}},
            },
        )
        result = self.view()
        queued_row = next(row for row in result["queue"] if row["run_id"] == queued)
        self.assertTrue(queued_row["waiting"])
        self.assertNotIn(running, [row["run_id"] for row in result["queue"]])
        group_row = next(row for row in result["groups"] if row["key"] == "refs/heads/main")
        self.assertEqual(group_row["queued"], 1)
        self.assertEqual(group_row["running"], 1)

    def test_describe_methods_match_the_schema(self):
        const = SCHEMA["$defs"]["DescribeResult"]["properties"]["methods"]["const"]
        described = self.call("worker.describe", {})
        self.assertNotIn("error", described)
        self.assertEqual(described["result"]["methods"], const)
        self.assertIn("status.view", const)
        self.assertIn("poll.record", const)

    def test_the_new_requests_validate(self):
        examples = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/v0/examples.json").read_text()
        )
        for method in ("status.view", "poll.record"):
            match = [
                example["value"]
                for example in examples
                if example["valid"]
                and example["definition"] == "Request"
                and example["value"]["method"] == method
            ]
            self.assertEqual(len(match), 1)
            validator("Request").validate(match[0])

    def test_run_get_has_no_check_run_id(self):
        run_id = str(uuid.uuid4())
        self.insert(
            _record(
                run_id, state="succeeded", started_at=STARTED, finished_at=FINISHED, exit_code=0
            ),
            {"event_name": "push", "event": {"after": TIP}},
            checks=((TIP, 4),),
        )
        reply = self.call("run.get", {"run_id": run_id})
        self.assertNotIn("error", reply)
        self.assertNotIn("check_run_id", reply["result"])
        self.assertNotIn("check_url", reply["result"])
