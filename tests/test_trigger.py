import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from execution_core.cli import call
from execution_core.protocol import Fault
from execution_core.trigger import submission_triggered
from schema_support import validate_response, validator


IMAGE = "sha256:" + "cd" * 32
PUSH = {"ref": "refs/heads/main"}


def triggered(on, event_name="push", event=None, **kwargs):
    if event is None:
        event = PUSH
    return submission_triggered(
        on,
        event_name,
        event,
        kwargs.get("activity_type"),
        kwargs.get("changed_files", []),
        kwargs.get("commit_count"),
        kwargs.get("diff_unavailable", False),
    )


def misses(on, **kwargs):
    try:
        submission_triggered(
            on,
            kwargs.get("event_name", "push"),
            kwargs.get("event", PUSH),
            kwargs.get("activity_type"),
            kwargs.get("changed_files", []),
            kwargs.get("commit_count"),
            kwargs.get("diff_unavailable", False),
        )
    except Fault as exc:
        return exc
    raise AssertionError("filter was accepted")


class PatternTests(unittest.TestCase):
    def test_cheat_sheet_shapes(self):
        self.assertTrue(
            triggered(
                {"push": {"branches": ["feature/*"]}}, event={"ref": "refs/heads/feature/my-branch"}
            )
        )
        self.assertFalse(
            triggered(
                {"push": {"branches": ["feature/*"]}}, event={"ref": "refs/heads/feature/a/b"}
            )
        )
        self.assertTrue(
            triggered(
                {"push": {"branches": ["feature/**"]}}, event={"ref": "refs/heads/feature/a/b"}
            )
        )
        self.assertTrue(triggered({"push": {"branches": ["*"]}}, event={"ref": "refs/heads/main"}))
        self.assertFalse(triggered({"push": {"branches": ["*"]}}, event={"ref": "refs/heads/a/b"}))
        self.assertTrue(triggered({"push": {"branches": ["**"]}}, event={"ref": "refs/heads/a/b"}))
        self.assertTrue(triggered({"push": {"paths": ["*.jsx?"]}}, changed_files=["page.js"]))
        self.assertTrue(triggered({"push": {"paths": ["*.jsx?"]}}, changed_files=["page.jsx"]))
        self.assertFalse(triggered({"push": {"paths": ["*.jsx?"]}}, changed_files=["dir/page.js"]))
        self.assertTrue(
            triggered(
                {"push": {"tags": ["v[12].[0-9]+.[0-9]+"]}}, event={"ref": "refs/tags/v1.10.1"}
            )
        )
        self.assertTrue(
            triggered(
                {"push": {"tags": ["v[12].[0-9]+.[0-9]+"]}}, event={"ref": "refs/tags/v2.0.0"}
            )
        )
        self.assertFalse(
            triggered(
                {"push": {"tags": ["v[12].[0-9]+.[0-9]+"]}}, event={"ref": "refs/tags/v3.0.0"}
            )
        )
        self.assertTrue(
            triggered({"push": {"paths": ["docs/**/*.md"]}}, changed_files=["docs/README.md"])
        )
        self.assertTrue(
            triggered(
                {"push": {"paths": ["docs/**/*.md"]}}, changed_files=["docs/mona/hello-world.md"]
            )
        )
        self.assertFalse(
            triggered({"push": {"paths": ["docs/*"]}}, changed_files=["docs/mona/file.txt"])
        )
        self.assertTrue(
            triggered({"push": {"paths": ["**/*src/**"]}}, changed_files=["my-src/code/js/app.js"])
        )

    def test_negation_order(self):
        patterns = ["*.md", "!README.md"]
        self.assertTrue(triggered({"push": {"paths": patterns}}, changed_files=["hello.md"]))
        self.assertFalse(triggered({"push": {"paths": patterns}}, changed_files=["README.md"]))
        self.assertFalse(triggered({"push": {"paths": patterns}}, changed_files=["docs/hello.md"]))
        reinclude = ["*.md", "!README.md", "README*"]
        self.assertTrue(triggered({"push": {"paths": reinclude}}, changed_files=["README.md"]))
        self.assertTrue(triggered({"push": {"paths": reinclude}}, changed_files=["README.doc"]))

    def test_push_branch_tag_and_ignore(self):
        branches = {"push": {"branches": ["main", "releases/**", "!releases/**-alpha"]}}
        self.assertTrue(triggered(branches, event={"ref": "refs/heads/main"}))
        self.assertTrue(triggered(branches, event={"ref": "refs/heads/releases/10"}))
        self.assertFalse(triggered(branches, event={"ref": "refs/heads/releases/10-alpha"}))
        self.assertFalse(triggered(branches, event={"ref": "refs/heads/dev"}))
        self.assertFalse(triggered(branches, event={"ref": "refs/tags/main"}))
        ignored = {"push": {"branches-ignore": ["dev", "releases/**-alpha"]}}
        self.assertTrue(triggered(ignored, event={"ref": "refs/heads/main"}))
        self.assertFalse(triggered(ignored, event={"ref": "refs/heads/dev"}))
        self.assertFalse(triggered(ignored, event={"ref": "refs/heads/releases/10-alpha"}))
        tags = {"push": {"tags": ["v2", "v1.*"]}}
        self.assertTrue(triggered(tags, event={"ref": "refs/tags/v1.9.1"}))
        self.assertFalse(triggered(tags, event={"ref": "refs/tags/v3"}))
        self.assertFalse(triggered(tags, event={"ref": "refs/heads/v2"}))
        tags_ignore = {"push": {"tags-ignore": ["v2", "v1.*"]}}
        self.assertFalse(triggered(tags_ignore, event={"ref": "refs/tags/v1.9"}))
        self.assertTrue(triggered(tags_ignore, event={"ref": "refs/tags/v3"}))
        self.assertFalse(triggered(tags_ignore, event={"ref": "refs/heads/main"}))
        self.assertTrue(triggered("push", event={"kind": "local"}))
        self.assertTrue(triggered({"push": None}, event={"kind": "local"}))
        self.assertFalse(triggered(["pull_request"]))
        self.assertFalse(triggered({"workflow_dispatch": None}))

    def test_push_paths_and_the_documented_limits(self):
        paths = {"push": {"branches": ["main"], "paths": ["**.js"]}}
        self.assertTrue(triggered(paths, changed_files=["src/app.js"]))
        self.assertFalse(triggered(paths, changed_files=["README.md"]))
        self.assertFalse(triggered(paths, changed_files=[]))
        ignored = {"push": {"paths-ignore": ["docs/**"]}}
        self.assertFalse(triggered(ignored, changed_files=["docs/readme.md"]))
        self.assertTrue(triggered(ignored, changed_files=["docs/readme.md", "src/app.py"]))
        self.assertFalse(triggered(ignored, changed_files=[]))
        window = ["note.txt"] * 3000 + ["app.js"]
        self.assertFalse(triggered(paths, changed_files=window))
        self.assertTrue(triggered(paths, changed_files=[*window[:2999], "app.js"]))
        self.assertFalse(triggered(paths, changed_files=["note.txt"], commit_count=1000))
        self.assertTrue(triggered(paths, changed_files=["note.txt"], commit_count=1001))
        self.assertTrue(triggered(paths, changed_files=["note.txt"], diff_unavailable=True))
        self.assertFalse(
            triggered(
                paths, event={"ref": "refs/heads/dev"}, changed_files=["app.js"], commit_count=1001
            )
        )
        tagged = {"push": {"tags": ["v1"], "paths": ["**.js"]}}
        self.assertTrue(triggered(tagged, event={"ref": "refs/tags/v1"}, changed_files=[]))

    def test_pull_request_types_branches_and_paths(self):
        event = {"pull_request": {"base": {"ref": "main"}}}
        self.assertTrue(
            triggered(
                "pull_request", event_name="pull_request", event=event, activity_type="opened"
            )
        )
        self.assertTrue(
            triggered(
                "pull_request", event_name="pull_request", event=event, activity_type="synchronize"
            )
        )
        self.assertTrue(
            triggered(
                "pull_request", event_name="pull_request", event=event, activity_type="reopened"
            )
        )
        self.assertFalse(
            triggered(
                "pull_request", event_name="pull_request", event=event, activity_type="closed"
            )
        )
        labeled = {"pull_request": {"types": ["labeled"]}}
        self.assertTrue(
            triggered(labeled, event_name="pull_request", event=event, activity_type="labeled")
        )
        self.assertFalse(
            triggered(labeled, event_name="pull_request", event=event, activity_type="opened")
        )
        branches = {"pull_request": {"branches": ["main", "releases/**"]}}
        self.assertTrue(
            triggered(branches, event_name="pull_request", event=event, activity_type="opened")
        )
        other = {"pull_request": {"base": {"ref": "dev"}}}
        self.assertFalse(
            triggered(branches, event_name="pull_request", event=other, activity_type="opened")
        )
        ignored = {"pull_request": {"branches-ignore": ["dev"]}}
        self.assertTrue(
            triggered(ignored, event_name="pull_request", event=event, activity_type="opened")
        )
        self.assertFalse(
            triggered(ignored, event_name="pull_request", event=other, activity_type="opened")
        )
        paths = {"pull_request": {"paths": ["**.js"]}}
        self.assertTrue(
            triggered(
                paths,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["app.js"],
            )
        )
        self.assertFalse(
            triggered(
                paths,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["README.md"],
            )
        )
        path_ignore = {"pull_request": {"paths-ignore": ["docs/**"]}}
        self.assertTrue(
            triggered(
                path_ignore,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["src/app.py"],
            )
        )
        self.assertFalse(
            triggered(
                path_ignore,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["docs/readme.md"],
            )
        )
        self.assertFalse(
            triggered(
                paths,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["note.txt"],
                commit_count=1001,
            )
        )
        self.assertTrue(
            triggered(
                paths,
                event_name="pull_request",
                event=event,
                activity_type="opened",
                changed_files=["note.txt"],
                diff_unavailable=True,
            )
        )

    def test_illegal_filters_are_rejected(self):
        both = misses({"push": {"branches": ["main"], "branches-ignore": ["dev"]}})
        self.assertEqual(both.kind, "INVALID_PARAMS")
        self.assertIn("both", str(both))
        only_negative = misses({"push": {"branches": ["!dev"]}})
        self.assertIn("without !", str(only_negative))
        missing_type = misses(
            "pull_request",
            event_name="pull_request",
            event={"pull_request": {"base": {"ref": "main"}}},
        )
        self.assertIn("activity_type", str(missing_type))


WORKFLOW = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


class SubmitTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trigger-submit-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.runs = []
        self.write_workflow(WORKFLOW)
        self.git("init", "--initial-branch=main")
        self.git("add", ".")
        self.commit()
        self.start_worker()

    def tearDown(self):
        for run_id in self.runs:
            with contextlib.suppress(OSError, ValueError):
                call(self.state, "run.cancel", {"version": 0, "run_id": run_id})
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def commit(self):
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def write_workflow(self, text):
        path = self.repo / ".github/workflows/test.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if (self.repo / ".git").exists():
            self.git("add", ".")
            self.commit()

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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            try:
                call(self.state, "worker.describe", {})
                return
            except (OSError, ValueError):
                time.sleep(0.01)
        self.fail("worker did not become ready")

    def params(self, **overrides):
        body = {
            "version": 1,
            "submission_key": "wf-1",
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": {"ref": "refs/heads/main"},
            "image": IMAGE,
        }
        body.update(overrides)
        return body

    def submit(self, **overrides):
        params = self.params(**overrides)
        validator("Request").validate(
            {"jsonrpc": "2.0", "id": 1, "method": "run.submit", "params": params}
        )
        reply = call(self.state, "run.submit", params)
        validate_response("run.submit", reply)
        if "result" in reply and "run_id" in reply["result"]:
            self.runs.append(reply["result"]["run_id"])
        return reply

    def rows(self, key):
        database = sqlite3.connect(self.state / "runs.sqlite3")
        try:
            return database.execute(
                "SELECT count(*) FROM runs WHERE submission_key=?", (key,)
            ).fetchone()[0]
        finally:
            database.close()

    def test_a_match_stores_one_run_and_reuses_the_key(self):
        self.write_workflow(
            "on:\n  push:\n    branches: [main]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        first = self.submit(submission_key="match", event_name="push")
        self.assertEqual(first["result"]["state"], "queued")
        self.assertEqual(self.rows("match"), 1)
        second = self.submit(submission_key="match", event_name="push")
        self.assertEqual(second["result"]["run_id"], first["result"]["run_id"])
        self.assertEqual(self.rows("match"), 1)

    def test_a_miss_creates_no_run_and_leaves_the_key_free(self):
        self.write_workflow(
            "on:\n  push:\n    branches: [main]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        before = (
            list((self.state / "snapshots").glob("*"))
            if (self.state / "snapshots").exists()
            else []
        )
        missed = self.submit(
            submission_key="later",
            event_name="push",
            event={"ref": "refs/heads/dev"},
        )
        self.assertEqual(missed["result"], {"triggered": False})
        self.assertEqual(self.rows("later"), 0)
        after = list((self.state / "snapshots").glob("*"))
        self.assertEqual(after, before)
        matched = self.submit(submission_key="later", event_name="push")
        self.assertIn("run_id", matched["result"])
        self.assertEqual(self.rows("later"), 1)

    def test_omitted_event_name_does_not_evaluate_on(self):
        self.write_workflow(
            "on:\n  push:\n    branches: [main]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        reply = self.submit(submission_key="plain", event={"kind": "local"})
        self.assertEqual(reply["result"]["state"], "queued")
        dispatch = self.submit(
            submission_key="dispatch",
            event_name="workflow_dispatch",
            event={"kind": "local"},
        )
        self.assertEqual(dispatch["result"]["state"], "queued")

    def test_pull_request_type_and_path_limits_reach_submission(self):
        self.write_workflow(
            "on:\n  pull_request:\n    paths: ['**.js']\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        event = {"pull_request": {"base": {"ref": "main"}}}
        closed = self.submit(
            submission_key="closed",
            event_name="pull_request",
            event=event,
            activity_type="closed",
            changed_files=["app.js"],
        )
        self.assertEqual(closed["result"], {"triggered": False})
        self.assertEqual(self.rows("closed"), 0)
        window = ["note.txt"] * 3000 + ["app.js"]
        past = self.submit(
            submission_key="past",
            event_name="pull_request",
            event=event,
            activity_type="opened",
            changed_files=window,
        )
        self.assertEqual(past["result"], {"triggered": False})
        self.assertEqual(self.rows("past"), 0)
        opened = self.submit(
            submission_key="opened",
            event_name="pull_request",
            event=event,
            activity_type="opened",
            changed_files=["app.js"],
        )
        self.assertEqual(opened["result"]["state"], "queued")
        push = self.write_and_submit_push_limit()
        self.assertEqual(push["result"], {"triggered": False})

    def write_and_submit_push_limit(self):
        self.write_workflow(
            "on:\n  push:\n    branches: [main]\n    paths: ['**.js']\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        blocked = self.submit(
            submission_key="thousand",
            event_name="push",
            changed_files=["note.txt"],
            commit_count=1000,
        )
        self.assertEqual(blocked["result"], {"triggered": False})
        self.assertEqual(self.rows("thousand"), 0)
        return self.submit(
            submission_key="branch",
            event_name="push",
            event={"ref": "refs/heads/dev"},
            changed_files=["note.txt"],
            commit_count=1001,
        )

    def test_more_than_1000_commits_skips_only_path_filters(self):
        self.write_workflow(
            "on:\n  push:\n    branches: [main]\n    paths: ['**.js']\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        skipped = self.submit(
            submission_key="over",
            event_name="push",
            changed_files=["note.txt"],
            commit_count=1001,
        )
        self.assertEqual(skipped["result"]["state"], "queued")
        unavailable = self.submit(
            submission_key="unavailable",
            event_name="push",
            changed_files=["note.txt"],
            diff_unavailable=True,
        )
        self.assertEqual(unavailable["result"]["state"], "queued")

    def test_schedule_submit_matches_only_the_listed_cron(self):
        self.write_workflow(
            "on:\n  schedule:\n    - cron: '*/5 * * * *'\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        missed = self.submit(
            submission_key="other-cron",
            event_name="schedule",
            event={"schedule": "0 0 * * *", "ref": "refs/heads/main"},
        )
        self.assertEqual(missed["result"], {"triggered": False})
        self.assertEqual(self.rows("other-cron"), 0)
        matched = self.submit(
            submission_key="listed-cron",
            event_name="schedule",
            event={"schedule": "*/5 * * * *", "ref": "refs/heads/main"},
        )
        self.assertEqual(matched["result"]["state"], "queued")
        self.assertEqual(self.rows("listed-cron"), 1)

    def test_a_bad_schedule_rejects_a_push_submit(self):
        self.write_workflow(
            "on:\n  push:\n  schedule:\n    - cron: '60 * * * *'\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        reply = self.submit(submission_key="bad-cron", event_name="push")
        self.assertEqual(reply["error"]["data"]["kind"], "INVALID_PARAMS")
        self.assertIn("on.schedule cron is not accepted", reply["error"]["message"])
        self.assertEqual(self.rows("bad-cron"), 0)

    def test_a_timezone_schedule_rejects_a_push_submit(self):
        self.write_workflow(
            "on:\n  push:\n  schedule:\n    - cron: '0 9 * * 1-5'\n"
            "      timezone: America/New_York\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        reply = self.submit(submission_key="timezone", event_name="push")
        self.assertEqual(reply["error"]["data"]["kind"], "INVALID_PARAMS")
        self.assertIn("on.schedule timezone is not accepted", reply["error"]["message"])
        self.assertEqual(self.rows("timezone"), 0)
