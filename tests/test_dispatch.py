"""One-shot workflow_dispatch against a local clone. No test contacts GitHub."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from execution_core.poll import PollError, allowlist_matches
from execution_core.protocol import canonical
from execution_core.worker import Worker


IMAGE = "sha256:" + "cd" * 32
WORKFLOW = ".github/workflows/check.yml"
DISPATCH = """\
name: dispatch
on:
  workflow_dispatch:
jobs:
  one:
    runs-on: ubuntu-latest
    steps:
      - run: echo one
  two:
    runs-on: ubuntu-latest
    steps:
      - run: echo two
"""
PUSH_ONLY = """\
name: push
on:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
INPUTS = """\
name: inputs
on:
  workflow_dispatch:
    inputs:
      name:
        description: who
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
TIMEZONE = """\
name: both
on:
  workflow_dispatch:
  schedule:
    - cron: "0 9 * * 1-5"
      timezone: UTC
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
STRATEGY = """\
name: grid
on: workflow_dispatch
jobs:
  plain:
    runs-on: ubuntu-latest
    steps:
      - run: echo plain
  grid:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        letter: [a]
    steps:
      - run: echo grid
"""
EMPTY = """\
name: empty
on: workflow_dispatch
"""


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
    )


def _request(method, params):
    return (
        canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    ).encode()


def _commit(repo):
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-")
        self.root = Path(self.tmp.name)
        self.branch = "main"
        self.remote = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", self.branch, str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.seed = self.root / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-b", self.branch)
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        self.write(DISPATCH)
        self.push()
        self.clone = self.root / "clone"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.clone)],
            check=True,
            capture_output=True,
        )
        self.state = self.root / "state"
        self.worker = Worker(self.clone, self.state)
        self.worker.start()
        self.worker.stop.set()
        self.worker.scheduler.join(timeout=5)
        if self.worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        self.worker.stop.clear()
        self.calls = []

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def write(self, text, path=WORKFLOW):
        target = self.seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(self.seed, "add", ".")
        _commit(self.seed)

    def push(self):
        _git(self.seed, "push", "origin", f"HEAD:refs/heads/{self.branch}")

    def fetch(self):
        _git(self.clone, "fetch", "origin")

    def head(self):
        return _git(self.clone, "rev-parse", "HEAD").stdout.decode().strip()

    def origin_tip(self):
        return _git(self.clone, "rev-parse", "origin/HEAD").stdout.decode().strip()

    def caller(self, method, params):
        self.calls.append(method)
        reply = self.worker.response(_request(method, params))
        if "error" in reply:
            data = reply["error"].get("data") or {}
            message = reply["error"].get("message") or "worker refused the request"
            raise PollError(data.get("kind") or "WORKER_ERROR", message)
        return reply["result"]

    def dispatch(self, workflow=WORKFLOW, repository="acme/demo", image=IMAGE, caller=None):
        from execution_core.poll import dispatch_once

        return dispatch_once(
            repository=repository,
            clone=self.clone,
            workflow=workflow,
            caller=caller or self.caller,
            image=image,
        )

    def rows(self):
        database = sqlite3.connect(self.state / "runs.sqlite3")
        try:
            return database.execute("SELECT count(*) FROM runs").fetchone()[0]
        finally:
            database.close()

    def request_for(self, run_id):
        database = sqlite3.connect(self.state / "runs.sqlite3")
        try:
            row = database.execute("SELECT request FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            database.close()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_a_listed_workflow_submits_every_job_on_the_origin_head_tip(self):
        self.write("later\n", "README.md")
        self.push()
        self.fetch()
        parked = self.head()
        self.assertNotEqual(parked, self.origin_tip())
        result = self.dispatch()
        self.assertTrue(result["triggered"])
        self.assertEqual(result["ref"], "refs/heads/main")
        self.assertEqual(result["sha"], self.origin_tip())
        self.assertEqual(self.head(), result["sha"])
        self.assertNotEqual(self.head(), parked)
        self.assertEqual([item["job_id"] for item in result["submitted"]], ["one", "two"])
        self.assertEqual(len({item["run_id"] for item in result["submitted"]}), 2)
        self.assertNotIn("run.status", self.calls)
        self.assertFalse((self.state / "poll.json").exists())
        for item in result["submitted"]:
            request = self.request_for(item["run_id"])
            self.assertEqual(request["event_name"], "workflow_dispatch")
            self.assertEqual(request["job_id"], item["job_id"])
            self.assertEqual(
                request["event"],
                {
                    "ref": "refs/heads/main",
                    "repository": {"full_name": "acme/demo", "default_branch": "main"},
                },
            )
            self.assertTrue(allowlist_matches(request["event"], [], [], "workflow_dispatch"))
            self.assertTrue(
                allowlist_matches(
                    request["event"], ["refs/heads/dev"], ["nobody"], "workflow_dispatch"
                )
            )

    def test_a_repeat_of_the_same_tip_returns_the_same_runs(self):
        first = self.dispatch()
        second = self.dispatch()
        self.assertEqual(
            [item["run_id"] for item in first["submitted"]],
            [item["run_id"] for item in second["submitted"]],
        )
        self.assertEqual(self.rows(), 2)

    def test_a_later_tip_is_a_new_run(self):
        first = self.dispatch()
        self.write("later\n", "LATER.md")
        self.push()
        self.fetch()
        second = self.dispatch()
        self.assertNotEqual(first["sha"], second["sha"])
        self.assertNotEqual(first["submitted"][0]["run_id"], second["submitted"][0]["run_id"])
        self.assertEqual(self.rows(), 4)

    def test_the_command_reads_the_tip_blob_and_does_not_fetch(self):
        recorded = self.origin_tip()
        dirty = self.clone / WORKFLOW
        dirty.write_text(PUSH_ONLY)
        self.write("remote-only\n", "REMOTE.md")
        self.push()
        result = self.dispatch()
        remote = _git(self.remote, "rev-parse", f"refs/heads/{self.branch}").stdout.decode().strip()
        self.assertNotEqual(remote, recorded)
        self.assertEqual(result["sha"], recorded)
        self.assertTrue(result["triggered"])
        self.assertIn("workflow_dispatch", (self.clone / WORKFLOW).read_text())

    def test_a_push_only_workflow_does_not_check_out_or_submit(self):
        self.write(PUSH_ONLY)
        self.push()
        self.fetch()
        parked = self.head()
        self.assertNotEqual(parked, self.origin_tip())
        extra = self.clone / "untracked.txt"
        extra.write_text("keep\n")
        result = self.dispatch()
        self.assertEqual(
            result, {"triggered": False, "ref": "refs/heads/main", "sha": self.origin_tip()}
        )
        self.assertEqual(self.head(), parked)
        self.assertEqual(extra.read_text(), "keep\n")
        self.assertNotIn("run.submit", self.calls)
        self.assertEqual(self.rows(), 0)
        self.assertFalse((self.state / "poll.json").exists())

    def test_a_match_cleans_the_work_tree(self):
        extra = self.clone / "untracked.txt"
        extra.write_text("gone\n")
        result = self.dispatch()
        self.assertTrue(result["triggered"])
        self.assertFalse(extra.exists())

    def test_a_missing_workflow_file_does_not_check_out(self):
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch(workflow=".github/workflows/missing.yml")
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("workflow", str(raised.exception))
        self.assertEqual(self.head(), parked)
        self.assertNotIn("run.submit", self.calls)

    def test_a_parent_path_does_not_call_the_worker(self):
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch(workflow="../check.yml")
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.head(), parked)

    def test_inputs_are_rejected_and_the_clone_stays(self):
        self.write(INPUTS)
        self.push()
        self.fetch()
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch()
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("inputs", str(raised.exception))
        self.assertEqual(self.head(), parked)
        self.assertNotIn("run.submit", self.calls)
        self.assertEqual(self.rows(), 0)

    def test_a_timezone_on_schedule_rejects_the_dispatch(self):
        self.write(TIMEZONE)
        self.push()
        self.fetch()
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch()
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("timezone", str(raised.exception))
        self.assertEqual(self.head(), parked)
        self.assertEqual(self.rows(), 0)

    def test_a_workflow_without_jobs_does_not_check_out(self):
        self.write(EMPTY)
        self.push()
        self.fetch()
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch()
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(self.head(), parked)
        self.assertNotIn("run.submit", self.calls)

    def test_a_missing_origin_head_is_git_failed(self):
        _git(self.clone, "remote", "set-head", "origin", "-d")
        parked = self.head()
        with self.assertRaises(PollError) as raised:
            self.dispatch()
        self.assertEqual(raised.exception.kind, "GIT_FAILED")
        self.assertEqual(self.head(), parked)
        self.assertNotIn("run.submit", self.calls)

    def test_a_clone_mismatch_does_not_submit(self):
        parked = self.head()

        def caller(method, params):
            self.calls.append(method)
            if method == "worker.describe":
                return {"repository": str(self.root / "other")}
            raise AssertionError(method)

        with self.assertRaises(PollError) as raised:
            self.dispatch(caller=caller)
        self.assertEqual(raised.exception.kind, "CLONE_MISMATCH")
        self.assertEqual(self.calls, ["worker.describe"])
        self.assertEqual(self.head(), parked)

    def test_the_repository_must_be_owner_name(self):
        with self.assertRaises(PollError) as raised:
            self.dispatch(repository="acme")
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(self.calls, [])

    def test_a_strategy_job_is_still_submitted(self):
        self.write(STRATEGY)
        self.push()
        self.fetch()
        seen = []

        def caller(method, params):
            if method == "worker.describe":
                return {"repository": str(self.clone.resolve())}
            seen.append(params)
            return {"run_id": "run-" + params["job_id"], "state": "queued"}

        result = self.dispatch(caller=caller)
        self.assertEqual([item["job_id"] for item in seen], ["plain", "grid"])
        self.assertTrue(all(item["event_name"] == "workflow_dispatch" for item in seen))
        self.assertEqual(
            [item["run_id"] for item in result["submitted"]], ["run-plain", "run-grid"]
        )

    def test_job_ids_keep_a_strategy_job(self):
        from execution_core.plan import workflow_job_ids

        self.assertEqual(workflow_job_ids(STRATEGY.encode()), ["plain", "grid"])

    def test_the_default_branch_comes_from_origin_head(self):
        root = self.root / "trunk"
        root.mkdir()
        remote = root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "trunk", str(remote)],
            check=True,
            capture_output=True,
        )
        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "trunk")
        _git(seed, "remote", "add", "origin", str(remote))
        target = seed / WORKFLOW
        target.parent.mkdir(parents=True)
        target.write_text(DISPATCH)
        _git(seed, "add", ".")
        _commit(seed)
        _git(seed, "push", "origin", "HEAD:refs/heads/trunk")
        clone = root / "clone"
        subprocess.run(
            ["git", "clone", str(remote), str(clone)],
            check=True,
            capture_output=True,
        )
        state = root / "state"
        worker = Worker(clone, state)
        worker.start()
        worker.stop.set()
        worker.scheduler.join(timeout=5)
        worker.stop.clear()

        def caller(method, params):
            reply = worker.response(_request(method, params))
            if "error" in reply:
                data = reply["error"].get("data") or {}
                raise PollError(data.get("kind") or "WORKER_ERROR", reply["error"]["message"])
            return reply["result"]

        try:
            from execution_core.poll import dispatch_once

            result = dispatch_once(
                repository="acme/demo",
                clone=clone,
                workflow=WORKFLOW,
                caller=caller,
                image=IMAGE,
            )
        finally:
            worker.close()
        self.assertEqual(result["ref"], "refs/heads/trunk")
        request = json.loads(
            sqlite3.connect(state / "runs.sqlite3")
            .execute("SELECT request FROM runs WHERE id=?", (result["submitted"][0]["run_id"],))
            .fetchone()[0]
        )
        self.assertEqual(request["event"]["repository"]["default_branch"], "trunk")
        self.assertEqual(request["event"]["ref"], "refs/heads/trunk")
        self.assertTrue(
            allowlist_matches(request["event"], ["refs/heads/dev"], ["nobody"], "workflow_dispatch")
        )
        self.assertFalse(allowlist_matches(request["event"], [], [], "pull_request"))

    def cli(self, *args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "dispatch",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_the_command_prints_the_runs_and_exits(self):
        result = self.cli(
            "--repository",
            "acme/demo",
            "--clone",
            str(self.clone),
            "--workflow",
            WORKFLOW,
            "--image",
            IMAGE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["triggered"])
        self.assertEqual([item["job_id"] for item in payload["submitted"]], ["one", "two"])
        again = self.cli(
            "--repository",
            "acme/demo",
            "--clone",
            str(self.clone),
            "--workflow",
            WORKFLOW,
            "--image",
            IMAGE,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        repeated = json.loads(again.stdout)
        self.assertEqual(
            [item["run_id"] for item in payload["submitted"]],
            [item["run_id"] for item in repeated["submitted"]],
        )
        self.assertFalse((self.state / "poll.json").exists())

    def test_the_command_reports_a_miss_and_exits(self):
        self.write(PUSH_ONLY)
        self.push()
        self.fetch()
        parked = self.head()
        result = self.cli(
            "--repository",
            "acme/demo",
            "--clone",
            str(self.clone),
            "--workflow",
            WORKFLOW,
            "--image",
            IMAGE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["triggered"], False)
        self.assertNotIn("submitted", payload)
        self.assertEqual(self.head(), parked)

    def test_the_command_has_no_ref_flag(self):
        result = self.cli("--ref", "refs/heads/dev")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized", result.stderr)
