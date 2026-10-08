"""One poll pass against a local Git remote and a local HTTP stub.

No test contacts GitHub. The scheduler is stopped so a submitted run stays
queued until the test changes it or the 24-hour rule cancels it.
"""

import json
import os
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from execution_core.checks import list_token_body
from execution_core.poll import (
    PollError,
    allowlist_matches,
    poll_once,
    queue_expired,
    submission_key,
)
from execution_core.protocol import canonical
from execution_core.status import StatusError
from execution_core.worker import Worker


IMAGE = "sha256:" + "cd" * 32
TOKEN = "token-ns42-secret-value"
WORKFLOW = """\
name: check
on:
  push:
  pull_request:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
JOB = (".github/workflows/check.yml", "check")
SCHEDULE = """\
name: check
on:
  schedule:
    - cron: "*/5 * * * *"
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def _request(method, params):
    return (
        canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    ).encode()


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        forced = getattr(self.server, "forced_status", None)
        if forced is not None:
            self.server.requests.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "if_none_match": self.headers.get("If-None-Match"),
                }
            )
            self._send(forced, getattr(self.server, "forced_body", b""), None)
            return
        path = self.path.split("?", 1)[0]
        if path.endswith("/branches"):
            kind = "branches"
        elif path.endswith("/pulls"):
            kind = "pulls"
        elif "/commits/" in path:
            kind = "commit"
        elif path.startswith("/repos/") and path.count("/") == 3:
            kind = "repository"
        else:
            kind = None
        self.server.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "if_none_match": self.headers.get("If-None-Match"),
            }
        )
        remaining = getattr(self.server, "remaining_by_kind", {}).get(kind, self.server.remaining)
        if kind in ("branches", "pulls"):
            etag = self.server.etags[kind]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("x-ratelimit-remaining", str(remaining))
                self.end_headers()
                return
            body = json.dumps(
                self.server.branches if kind == "branches" else self.server.pulls
            ).encode()
            self._send(200, body, etag, remaining)
            return
        if kind == "commit":
            sha = path.rsplit("/", 1)[-1]
            login = self.server.authors.get(sha)
            author = None
            if isinstance(login, str):
                author = {"login": login, "id": 1, "email": "hidden@example.invalid"}
            body = json.dumps(
                {"sha": sha, "author": author, "commit": {"message": "hidden-message"}}
            ).encode()
            self._send(200, body, None, remaining)
            return
        if kind == "repository":
            self._send(200, json.dumps(self.server.repository).encode(), None, remaining)
            return
        self._send(404, b"", None, remaining)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        self._send(201, b"{}", None)

    def _send(self, code, body, etag, remaining=None):
        if remaining is None:
            remaining = self.server.remaining
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-ratelimit-remaining", str(remaining))
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class QueueTests(unittest.TestCase):
    def test_twenty_four_hours_cancels_and_one_microsecond_less_does_not(self):
        clock = datetime(2026, 10, 5, tzinfo=UTC)
        self.assertTrue(queue_expired((clock - timedelta(hours=24)).isoformat(), clock))
        self.assertFalse(
            queue_expired(
                (clock - timedelta(hours=24) + timedelta(microseconds=1)).isoformat(),
                clock,
            )
        )
        self.assertFalse(queue_expired("not-a-time", clock))

    def test_the_submission_key_is_built_from_the_five_fields(self):
        first = submission_key("acme/demo", "push", "ab" * 20, JOB[0], JOB[1])
        self.assertEqual(first, submission_key("acme/demo", "push", "ab" * 20, JOB[0], JOB[1]))
        self.assertNotEqual(
            first, submission_key("acme/demo", "pull_request", "ab" * 20, JOB[0], JOB[1])
        )
        self.assertTrue(first.startswith("poll-"))
        self.assertLessEqual(len(first), 128)
        self.assertEqual(
            first, submission_key("acme/demo", "push", "ab" * 20, JOB[0], JOB[1], None)
        )
        self.assertNotEqual(
            first,
            submission_key(
                "acme/demo",
                "push",
                "ab" * 20,
                JOB[0],
                JOB[1],
                "2026-10-08T12:15:00+00:00",
            ),
        )


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="poll-")
        self.root = Path(self.tmp.name)
        self.remote = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.seed = self.root / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-b", "main")
        self._write(WORKFLOW)
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-u", "origin", "main")
        self.clone = self.root / "clone"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.clone)],
            check=True,
            capture_output=True,
        )
        self.state = self.root / "state"
        self.credential = self.root / "credential"
        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.branches = []
        self.server.pulls = []
        self.server.remaining = 40
        self.server.remaining_by_kind = {}
        self.server.repository = {
            "id": 5150,
            "full_name": "ignored/name",
            "default_branch": "main",
        }
        self.server.authors = {}
        self.server.etags = {"branches": "branches-1", "pulls": "pulls-1"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.worker = Worker(self.clone, self.state)
        self.worker.start()
        self.worker.stop.set()
        self.worker.scheduler.join(timeout=5)
        if self.worker.scheduler.is_alive():
            self.fail("scheduler did not stop")
        self.worker.stop.clear()
        self.submits = []

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.worker.close()
        self.tmp.cleanup()

    def _write(self, text):
        path = self.seed / ".github/workflows/check.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def _tip(self, ref="refs/heads/main"):
        return _git(self.remote, "rev-parse", ref).stdout.strip()

    def caller(self, method, params):
        if method == "run.submit":
            self.submits.append(params)
        reply = self.worker.response(_request(method, params))
        if "error" in reply:
            raise PollError(reply["error"]["data"]["kind"], reply["error"]["message"])
        return reply["result"]

    def poll(self, clock=None):
        return poll_once(
            repository="acme/demo",
            clone=self.clone,
            jobs=[JOB],
            image=IMAGE,
            credential_file=self.credential,
            api_base=self.base,
            state=self.state,
            caller=self.caller,
            clock=clock,
        )

    def posts(self):
        return [item for item in self.server.requests if item["method"] == "POST"]

    def runs(self):
        return self.worker.db.execute("SELECT count(*) FROM runs").fetchone()[0]

    def state_bytes(self):
        chunks = []
        for path in self.state.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            chunks.append(path.read_bytes())
        return b"".join(chunks)

    def test_a_push_and_a_pull_request_post_pending_then_one_final_status(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        first = self.poll()
        self.assertFalse(first["stopped"])
        self.assertEqual(self.runs(), 1)
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertNotIn("host", saved["submissions"][0])
        self.assertEqual(self.submits[0]["event"]["before"], "0" * 40)
        self.assertIs(self.submits[0]["diff_unavailable"], True)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0]["path"], f"/repos/acme/demo/statuses/{tip}")
        self.assertEqual(
            json.loads(self.posts()[0]["body"]),
            {
                "context": "rookrunner/check.yml/check",
                "state": "pending",
            },
        )
        self.assertEqual(self.posts()[0]["authorization"], "Bearer " + TOKEN)
        self.assertTrue(
            all(
                item["authorization"] is None
                for item in self.server.requests
                if item["method"] == "GET"
            )
        )
        self.assertNotIn(TOKEN.encode(), self.state_bytes())
        run_id = first["submitted"][0]["run_id"]

        self.credential.unlink()
        self.server.requests.clear()
        second = self.poll()
        self.assertEqual(second["submitted"], [])
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.runs(), 1)
        self.assertTrue(any(item["if_none_match"] == "branches-1" for item in self.server.requests))

        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        record = self.worker.get(run_id)
        record["state"] = "succeeded"
        record["exit_code"] = 0
        with self.worker.guard, self.worker.db:
            self.worker.save(record)
        self.server.requests.clear()
        third = self.poll()
        self.assertEqual(third["submitted"], [])
        self.assertEqual(json.loads(self.posts()[0]["body"])["state"], "success")
        self.server.requests.clear()
        fourth = self.poll()
        self.assertEqual(self.posts(), [])
        self.assertEqual(fourth["statuses"][0]["action"], "skip")

        feature = self.seed / "feature.txt"
        feature.write_text("feature\n")
        _git(self.seed, "checkout", "-b", "feature")
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "feature",
        )
        _git(self.seed, "push", "origin", "feature")
        head = _git(self.seed, "rev-parse", "feature").stdout.strip()
        _git(self.seed, "checkout", "main")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "merge",
            "--no-ff",
            "feature",
            "-m",
            "merge",
        )
        merge = _git(self.seed, "rev-parse", "HEAD").stdout.strip()
        _git(self.seed, "push", "origin", "HEAD:refs/pull/7/merge")
        self.server.pulls = [
            {
                "number": 7,
                "head": {
                    "sha": head,
                    "ref": "feature",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
                "base": {"sha": tip, "ref": "main", "repo": {"id": 5150, "full_name": "acme/demo"}},
            }
        ]
        self.server.etags["pulls"] = "pulls-2"
        self.server.requests.clear()
        self.submits.clear()
        opened = self.poll()
        pull = [item for item in opened["submitted"] if item["event"] == "pull_request"]
        self.assertEqual(len(pull), 1)
        self.assertEqual(self.runs(), 2)
        self.assertEqual(self.submits[0]["activity_type"], "opened")
        posted = json.loads(self.posts()[0]["body"])
        self.assertEqual(posted["state"], "pending")
        self.assertTrue(self.posts()[0]["path"].endswith("/" + head))
        manifest = json.loads(
            (
                self.state
                / "snapshots"
                / self.worker.get(pull[0]["run_id"])["input"]["snapshot_id"]
                / "manifest.json"
            ).read_text()
        )
        self.assertEqual(manifest["base_commit"], merge)
        self.assertIs(manifest["dirty"], False)
        self.assertEqual(manifest["included"], [])
        self.credential.unlink()
        self.server.requests.clear()
        repeated = self.poll()
        self.assertEqual(
            [item for item in repeated["submitted"] if item.get("event") == "pull_request"],
            [],
        )
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.runs(), 2)

    def test_a_newer_push_keeps_the_older_run_and_supplies_the_diff(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        first = self.poll()
        older = first["submitted"][0]["run_id"]
        note = self.seed / "note.txt"
        note.write_text("note\n")
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "note",
        )
        _git(self.seed, "push", "origin", "main")
        new = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": new}}]
        self.server.etags["branches"] = "branches-2"
        self.submits.clear()
        second = self.poll()
        self.assertEqual(len(second["submitted"]), 1)
        self.assertEqual(self.runs(), 2)
        self.assertEqual(self.worker.get(older)["state"], "queued")
        self.assertEqual(self.worker.get(second["submitted"][0]["run_id"])["state"], "queued")
        self.assertEqual(self.submits[0]["changed_files"], ["note.txt"])
        self.assertEqual(self.submits[0]["commit_count"], 1)
        self.assertNotIn("diff_unavailable", self.submits[0])
        self.assertEqual(self.submits[0]["event"]["before"], tip)
        self.assertEqual(self.submits[0]["event"]["after"], new)

    def test_a_fork_and_a_missing_merge_ref_run_nothing(self):
        self.server.pulls = [
            {
                "number": 4,
                "head": {
                    "sha": "ab" * 20,
                    "ref": "feature",
                    "repo": {"id": 9, "full_name": "other/demo"},
                },
                "base": {"sha": "cd" * 20, "ref": "main", "repo": {"id": 5150}},
            },
            {
                "number": 8,
                "head": {
                    "sha": "ef" * 20,
                    "ref": "feature",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
                "base": {"sha": "12" * 20, "ref": "main", "repo": {"id": 5150}},
            },
        ]
        result = self.poll()
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(
            [item["reason"] for item in result["skipped"]],
            ["fork", "merge_ref_absent"],
        )
        self.server.requests.clear()
        self.credential.unlink()
        again = self.poll()
        self.assertEqual(again["skipped"], [])
        self.assertEqual(self.runs(), 0)
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(len(saved["forks"]), 1)
        self.assertEqual(len(saved["merge_absent"]), 1)
        self.assertNotIn(TOKEN.encode(), self.state_bytes())

    def test_a_rate_limit_stops_the_pass_and_the_next_pass_resumes(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.server.remaining = 0
        stopped = self.poll()
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.posts(), [])
        self.assertFalse(any("/pulls" in item["path"] for item in self.server.requests))
        self.server.remaining = 40
        self.server.requests.clear()
        resumed = self.poll()
        self.assertFalse(resumed["stopped"])
        self.assertEqual(self.runs(), 1)
        self.assertEqual(json.loads(self.posts()[0]["body"])["state"], "pending")

    def test_a_run_queued_for_twenty_four_hours_is_cancelled_and_posted_as_error(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        created = self.poll()
        run_id = created["submitted"][0]["run_id"]
        clock = datetime(2026, 10, 5, 12, tzinfo=UTC)
        record = self.worker.get(run_id)
        record["accepted_at"] = (clock - timedelta(hours=24) + timedelta(seconds=1)).isoformat()
        with self.worker.guard, self.worker.db:
            self.worker.save(record)
        self.server.requests.clear()
        young = self.poll(clock=clock)
        self.assertEqual(self.worker.get(run_id)["state"], "queued")
        self.assertEqual(self.posts(), [])
        self.assertEqual(young["statuses"], [])
        record = self.worker.get(run_id)
        record["accepted_at"] = (clock - timedelta(hours=24)).isoformat()
        with self.worker.guard, self.worker.db:
            self.worker.save(record)
        self.server.requests.clear()
        expired = self.poll(clock=clock)
        self.assertEqual(self.worker.get(run_id)["state"], "cancelled")
        self.assertEqual(json.loads(self.posts()[0]["body"])["state"], "error")
        self.assertEqual(expired["statuses"][0]["action"], "posted")

    def test_the_credential_is_read_only_when_a_status_is_posted(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.credential.unlink()
        with self.assertRaises(StatusError) as caught:
            self.poll()
        self.assertEqual(caught.exception.kind, "CREDENTIAL_UNREADABLE")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.posts(), [])
        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        self.server.requests.clear()
        resumed = self.poll()
        self.assertEqual(self.runs(), 1)
        self.assertEqual(json.loads(self.posts()[0]["body"])["state"], "pending")
        self.assertIn("run_id", resumed["submitted"][0])

    def test_a_null_head_and_a_different_repository_id_run_nothing(self):
        self.server.pulls = [
            {
                "number": 4,
                "head": {"sha": "ab" * 20, "ref": "feature", "repo": None},
                "base": {"sha": "cd" * 20, "ref": "main", "repo": {"id": 5150}},
            },
            {
                "number": 5,
                "head": {
                    "sha": "ef" * 20,
                    "ref": "feature",
                    "repo": {"id": 9, "full_name": "acme/demo"},
                },
                "base": {
                    "sha": "12" * 20,
                    "ref": "main",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
            },
            {
                "number": 6,
                "head": {
                    "sha": "34" * 20,
                    "ref": "feature",
                    "repo": {"id": True, "full_name": "acme/demo"},
                },
                "base": {"sha": "56" * 20, "ref": "main", "repo": {"id": 5150}},
            },
            {
                "number": 11,
                "head": {
                    "sha": "78" * 20,
                    "ref": "feature",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
                "base": {"sha": "90" * 20, "ref": "main"},
            },
        ]
        result = self.poll()
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(
            [item["reason"] for item in result["skipped"]],
            ["fork", "fork", "fork", "fork"],
        )
        self.assertFalse(any("/commits/" in item["path"] for item in self.server.requests))
        self.assertFalse(
            any(
                item["path"].split("?", 1)[0] == "/repos/acme/demo" for item in self.server.requests
            )
        )
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual([row["number"] for row in saved["forks"]], [4, 5, 6, 11])
        self.assertIsNone(saved["forks"][0]["repository"])
        self.assertEqual(saved["forks"][1]["repository"], "acme/demo")
        self.server.requests.clear()
        self.credential.unlink()
        again = self.poll()
        self.assertEqual(again["skipped"], [])
        self.assertEqual(self.runs(), 0)
        self.assertNotIn(TOKEN.encode(), self.state_bytes())

    def test_equal_repository_ids_run_when_the_full_name_differs(self):
        tip = self._tip()
        feature = self.seed / "feature.txt"
        feature.write_text("feature\n")
        _git(self.seed, "checkout", "-b", "feature")
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "feature",
        )
        _git(self.seed, "push", "origin", "feature")
        head = _git(self.seed, "rev-parse", "feature").stdout.strip()
        _git(self.seed, "checkout", "main")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "merge",
            "--no-ff",
            "feature",
            "-m",
            "merge",
        )
        _git(self.seed, "push", "origin", "HEAD:refs/pull/9/merge")
        self.server.pulls = [
            {
                "number": 9,
                "user": {"login": "Mona", "id": 3, "email": "hidden@example.invalid"},
                "head": {
                    "sha": head,
                    "ref": "feature",
                    "repo": {"id": 5150, "full_name": "other/demo", "private": True},
                },
                "base": {
                    "sha": tip,
                    "ref": "main",
                    "repo": {"id": 5150, "full_name": "acme/demo", "private": True},
                },
            }
        ]
        self.server.etags["pulls"] = "pulls-2"
        opened = self.poll()
        self.assertEqual(self.runs(), 1)
        self.assertEqual(opened["skipped"], [{"reason": "signoff_absent"}])
        event = self.submits[0]["event"]
        self.assertEqual(
            event["repository"],
            {"full_name": "acme/demo", "id": 5150, "default_branch": "main"},
        )
        self.assertIs(type(event["repository"]["id"]), int)
        self.assertEqual(event["pull_request"]["user"], {"login": "Mona"})
        self.assertEqual(
            event["pull_request"]["head"]["repo"],
            {"id": 5150, "full_name": "other/demo"},
        )
        self.assertIs(type(event["pull_request"]["head"]["repo"]["id"]), int)
        self.assertEqual(
            event["pull_request"]["base"]["repo"],
            {"id": 5150, "full_name": "acme/demo"},
        )
        encoded = json.dumps(event)
        self.assertNotIn("hidden@example.invalid", encoded)
        self.assertNotIn("private", encoded)
        self.assertFalse(allowlist_matches(event, [], [], "pull_request"))
        self.assertTrue(allowlist_matches(event, [], ["mona"], "pull_request"))
        self.assertTrue(allowlist_matches(event, ["refs/pull/9/merge"], [], "pull_request"))
        self.assertTrue(
            all(
                item["authorization"] is None
                for item in self.server.requests
                if item["method"] == "GET"
            )
        )

    def test_a_push_copies_the_repository_and_the_commit_login(self):
        tip = self._tip()
        self.server.authors[tip] = "octocat"
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.poll()
        event = self.submits[0]["event"]
        self.assertEqual(event["repository"]["full_name"], "acme/demo")
        self.assertEqual(event["repository"]["id"], 5150)
        self.assertIs(type(event["repository"]["id"]), int)
        self.assertEqual(event["repository"]["default_branch"], "main")
        self.assertEqual(event["commits"], [{"author": {"login": "octocat"}}])
        encoded = json.dumps(event)
        self.assertNotIn("hidden@example.invalid", encoded)
        self.assertNotIn("hidden-message", encoded)
        self.assertNotIn("ignored/name", encoded)
        self.assertTrue(allowlist_matches(event, [], [], "push"))
        self.assertTrue(allowlist_matches(event, ["refs/heads/other"], ["nobody"], "push"))

        note = self.seed / "dev.txt"
        note.write_text("dev\n")
        _git(self.seed, "checkout", "-b", "dev")
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "dev",
        )
        _git(self.seed, "push", "origin", "dev")
        dev = self._tip("refs/heads/dev")
        self.server.branches = [{"name": "dev", "commit": {"sha": dev}}]
        self.server.etags["branches"] = "branches-2"
        self.submits.clear()
        self.poll()
        dev_event = self.submits[0]["event"]
        self.assertEqual(dev_event["ref"], "refs/heads/dev")
        self.assertNotIn("commits", dev_event)
        self.assertEqual(dev_event["repository"]["default_branch"], "main")
        self.assertFalse(allowlist_matches(dev_event, [], [], "push"))
        self.assertFalse(allowlist_matches(dev_event, [], ["octocat"], "push"))
        self.assertTrue(allowlist_matches(dev_event, ["refs/heads/dev"], [], "push"))
        self.assertFalse(allowlist_matches(dev_event, ["refs/heads/Dev"], [], "push"))

    def test_an_exhausted_repository_response_submits_nothing(self):
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        self.server.authors[tip] = "octocat"
        self.server.remaining_by_kind = {"repository": 0}
        stopped = self.poll()
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.posts(), [])
        self.assertFalse(any("/commits/" in item["path"] for item in self.server.requests))
        self.server.remaining_by_kind = {}
        self.server.requests.clear()
        self.submits.clear()
        resumed = self.poll()
        self.assertFalse(resumed["stopped"])
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.submits[0]["event"]["commits"], [{"author": {"login": "octocat"}}])

    def _commit_all(self, message):
        _git(self.seed, "add", "-A")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            message,
        )

    def test_a_missing_workflow_file_is_skipped_and_later_branches_still_submit(self):
        main = self._tip()
        _git(self.seed, "checkout", "-b", "bare")
        (self.seed / ".github/workflows/check.yml").unlink()
        self._commit_all("drop workflow")
        _git(self.seed, "push", "origin", "bare")
        _git(self.seed, "checkout", "main")
        bare = self._tip("refs/heads/bare")
        self.server.branches = [
            {"name": "bare", "commit": {"sha": bare}},
            {"name": "main", "commit": {"sha": main}},
        ]
        result = self.poll()
        self.assertEqual(
            result["skipped"],
            [
                {
                    "reason": "workflow_absent",
                    "event": "push",
                    "ref": "refs/heads/bare",
                    "workflow": JOB[0],
                    "job_id": JOB[1],
                }
            ],
        )
        self.assertEqual([item["ref"] for item in result["submitted"]], ["refs/heads/main"])
        self.assertIn("run_id", result["submitted"][0])
        self.assertEqual(self.runs(), 1)
        self.server.etags["branches"] = "branches-2"
        self.submits.clear()
        again = self.poll()
        self.assertEqual(again["skipped"], [])
        self.assertEqual(again["submitted"], [])
        self.assertEqual(self.runs(), 1)

    def test_a_workflow_file_that_is_not_regular_still_stops_the_pass(self):
        main = self._tip()
        _git(self.seed, "checkout", "-b", "linked")
        _git(self.seed, "config", "core.symlinks", "true")
        workflow = self.seed / ".github/workflows/check.yml"
        workflow.unlink()
        (self.seed / ".github/workflows/target.txt").write_text("not a workflow\n")
        workflow.symlink_to("target.txt")
        self._commit_all("link workflow")
        mode = _git(self.seed, "ls-files", "-s", ".github/workflows/check.yml").stdout.split()[0]
        self.assertEqual(mode, "120000")
        _git(self.seed, "push", "origin", "linked")
        _git(self.seed, "checkout", "main")
        linked = self._tip("refs/heads/linked")
        self.server.branches = [
            {"name": "linked", "commit": {"sha": linked}},
            {"name": "main", "commit": {"sha": main}},
        ]
        with self.assertRaises(PollError) as raised:
            self.poll()
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(str(raised.exception), "workflow must be a captured regular file")
        self.assertEqual(self.runs(), 0)

    def test_a_list_token_is_sent_on_gets_and_is_absent_from_state(self):
        token = "list-token-distinct-from-status"
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        result = poll_once(
            repository="acme/demo",
            clone=self.clone,
            jobs=[JOB],
            image=IMAGE,
            credential_file=self.credential,
            api_base=self.base,
            state=self.state,
            caller=self.caller,
            list_token=token,
        )
        gets = [item for item in self.server.requests if item["method"] == "GET"]
        self.assertGreater(len(gets), 0)
        self.assertTrue(all(item["authorization"] == "Bearer " + token for item in gets))
        self.assertNotIn(token.encode(), self.state_bytes())
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(self.posts()[0]["authorization"], "Bearer " + TOKEN)
        self.assertEqual(self.runs(), 1)

    def test_a_refused_list_does_not_echo_the_list_token(self):
        token = "list-token-must-stay-unprinted"
        self.server.forced_status = 403
        self.server.forced_body = token.encode()
        with self.assertRaises(PollError) as raised:
            poll_once(
                repository="acme/demo",
                clone=self.clone,
                jobs=[JOB],
                image=IMAGE,
                credential_file=self.credential,
                api_base=self.base,
                state=self.state,
                caller=self.caller,
                list_token=token,
            )
        self.assertEqual(raised.exception.kind, "API_REJECTED")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(self.runs(), 0)

    def test_the_poll_command_sends_a_minted_list_token_and_revokes_it(self):
        from execution_core.cli import _run_poll

        token = "minted-list-token"
        revoked = []

        def mint(*_args):
            return token

        def revoke(_api_base, value):
            revoked.append(value)
            return True

        args = SimpleNamespace(
            repository="acme/demo",
            clone=self.clone,
            job=[JOB],
            image=IMAGE,
            credential_file=None,
            app_key="unused-key-path",
            api_base=self.base,
            state=self.state,
        )
        result = _run_poll(args, self.caller, mint, revoke)
        gets = [item for item in self.server.requests if item["method"] == "GET"]
        self.assertGreater(len(gets), 0)
        self.assertTrue(all(item["authorization"] == "Bearer " + token for item in gets))
        self.assertEqual(revoked, [token])
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn(token.encode(), self.state_bytes())
        self.assertEqual(self.runs(), 0)

    def test_a_schedule_baselines_then_fires_once_on_the_default_tip(self):
        self._write(SCHEDULE)
        _git(self.seed, "push", "origin", "main")
        tip = self._tip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        baseline = self.poll(clock=datetime(2026, 10, 8, 12, 2, tzinfo=UTC))
        self.assertEqual(self.runs(), 0)
        self.assertEqual(
            [item for item in baseline["submitted"] if item.get("event") == "schedule"],
            [],
        )
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(saved["schedules"][JOB[0]], "2026-10-08T12:00:00+00:00")
        self.assertEqual(saved["tips"]["refs/heads/main"], tip)

        feature = self.seed / "feature.txt"
        feature.write_text("feature\n")
        _git(self.seed, "checkout", "-b", "feature")
        _git(self.seed, "add", ".")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "feature",
        )
        _git(self.seed, "push", "origin", "feature")
        head = _git(self.seed, "rev-parse", "feature").stdout.strip()
        _git(self.seed, "checkout", "main")
        _git(
            self.seed,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "merge",
            "--no-ff",
            "feature",
            "-m",
            "merge",
        )
        _git(self.seed, "push", "origin", "HEAD:refs/pull/7/merge")
        self.server.pulls = [
            {
                "number": 7,
                "head": {
                    "sha": head,
                    "ref": "feature",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
                "base": {
                    "sha": tip,
                    "ref": "main",
                    "repo": {"id": 5150, "full_name": "acme/demo"},
                },
            }
        ]
        self.server.etags["pulls"] = "pulls-2"
        self.server.requests.clear()
        self.submits.clear()
        fired = self.poll(clock=datetime(2026, 10, 8, 12, 17, tzinfo=UTC))
        self.assertTrue(
            any(item.get("if_none_match") == "branches-1" for item in self.server.requests)
        )
        self.assertEqual(self.runs(), 1)
        schedule = [item for item in self.submits if item.get("event_name") == "schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["event"]["schedule"], "*/5 * * * *")
        self.assertEqual(schedule[0]["event"]["ref"], "refs/heads/main")
        self.assertNotIn("commits", schedule[0]["event"])
        self.assertEqual(
            [json.loads(item["body"]) for item in self.posts()],
            [{"context": "rookrunner/check.yml/check/schedule", "state": "pending"}],
        )
        self.assertTrue(self.posts()[0]["path"].endswith("/" + tip))
        run_id = next(
            item["run_id"] for item in fired["submitted"] if item.get("event") == "schedule"
        )
        manifest = json.loads(
            (
                self.state
                / "snapshots"
                / self.worker.get(run_id)["input"]["snapshot_id"]
                / "manifest.json"
            ).read_text()
        )
        self.assertEqual(manifest["base_commit"], tip)
        saved = json.loads((self.state / "poll.json").read_text())
        self.assertEqual(saved["schedules"][JOB[0]], "2026-10-08T12:15:00+00:00")
        tick = "2026-10-08T12:15:00+00:00\0*/5 * * * *"
        keyed = submission_key("acme/demo", "schedule", tip, JOB[0], JOB[1], tick)
        self.assertEqual(saved["submissions"][0]["key"], keyed)
        self.assertNotEqual(keyed, submission_key("acme/demo", "schedule", tip, JOB[0], JOB[1]))
        self.assertEqual(saved["submissions"][0]["context"], "rookrunner/check.yml/check/schedule")

        self.submits.clear()
        again = self.poll(clock=datetime(2026, 10, 8, 12, 17, tzinfo=UTC))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.submits, [])
        self.assertEqual(
            [item for item in again["submitted"] if item.get("event") == "schedule"],
            [],
        )


class AllowlistTests(unittest.TestCase):
    def test_only_the_default_push_or_a_listed_ref_or_login_matches(self):
        default = {
            "ref": "refs/heads/main",
            "repository": {"id": 5150, "full_name": "acme/demo", "default_branch": "main"},
            "commits": [{"author": {"login": "octocat"}}],
        }
        other = {
            "ref": "refs/heads/dev",
            "repository": {"id": 5150, "full_name": "acme/demo", "default_branch": "main"},
            "commits": [{"author": {"login": "OctoCat"}}],
        }
        local = {"ref": "refs/heads/main", "repository": {"full_name": "acme/demo"}}
        pull = {
            "ref": "refs/pull/9/merge",
            "repository": {"id": 5150, "default_branch": "main"},
            "pull_request": {"user": {"login": "Mona"}},
        }
        bare_pull = {
            "ref": "refs/pull/4/merge",
            "repository": {"id": 5150, "default_branch": "main"},
            "pull_request": {"head": {"repo": {"id": 5150}}},
        }

        self.assertTrue(allowlist_matches(default, [], [], "push"))
        self.assertTrue(allowlist_matches(default, ["refs/heads/dev"], ["nobody"], "push"))
        self.assertFalse(allowlist_matches(other, [], [], "push"))
        self.assertFalse(allowlist_matches(local, [], [], "push"))
        self.assertFalse(allowlist_matches(default, [], [], None))
        self.assertFalse(allowlist_matches(default, [], [], "pull_request"))
        self.assertTrue(allowlist_matches(other, ["refs/heads/dev"], [], "push"))
        self.assertFalse(allowlist_matches(other, ["refs/heads/Dev"], [], "push"))
        self.assertTrue(allowlist_matches(other, [], ["octocat"], "push"))
        self.assertFalse(allowlist_matches(other, [], ["octocat "], "push"))
        self.assertFalse(
            allowlist_matches(
                {
                    "ref": "refs/heads/dev",
                    "repository": {"default_branch": "main"},
                    "commits": [{"author": {"login": "İ"}}],
                },
                [],
                ["i"],
                "push",
            )
        )
        self.assertFalse(allowlist_matches(other, [], ["octocat"], "workflow_dispatch"))
        self.assertFalse(allowlist_matches({"ref": "refs/heads/dev"}, [], ["octocat"], "push"))
        self.assertFalse(allowlist_matches(pull, [], [], "pull_request"))
        self.assertTrue(allowlist_matches(pull, [], ["mona"], "pull_request"))
        self.assertTrue(allowlist_matches(pull, ["refs/pull/9/merge"], [], "pull_request"))
        self.assertFalse(allowlist_matches(bare_pull, [], ["mona"], "pull_request"))
        self.assertFalse(allowlist_matches(bare_pull, [], [""], "pull_request"))


class CliPollTests(unittest.TestCase):
    def test_the_command_records_a_fork_and_does_not_read_the_credential(self):
        with tempfile.TemporaryDirectory(prefix="poll-cli-") as directory:
            root = Path(directory)
            clone = root / "clone"
            clone.mkdir()
            _git(clone, "init", "-b", "main")
            state = root / "state"
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            server.requests = []
            server.branches = []
            server.pulls = [
                {
                    "number": 3,
                    "head": {
                        "sha": "ab" * 20,
                        "ref": "feature",
                        "repo": {"id": 9, "full_name": "other/demo"},
                    },
                    "base": {"sha": "cd" * 20, "ref": "main", "repo": {"id": 5150}},
                }
            ]
            server.remaining = 20
            server.etags = {"branches": "branches-1", "pulls": "pulls-1"}
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    str(clone),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        self.fail(worker.stderr.read().decode())
                    if (state / "worker.sock").exists():
                        break
                    time.sleep(0.01)
                else:
                    self.fail("worker did not become ready")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "execution_core",
                        "--state",
                        str(state),
                        "poll",
                        "--repository",
                        "acme/demo",
                        "--clone",
                        str(clone),
                        "--job",
                        JOB[0],
                        JOB[1],
                        "--image",
                        IMAGE,
                        "--credential-file",
                        str(root / "missing-credential"),
                        "--api-base",
                        f"http://127.0.0.1:{server.server_address[1]}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["skipped"], [{"number": 3, "reason": "fork"}]
                )
                self.assertNotIn(TOKEN, result.stdout)
                self.assertFalse(any(item["method"] == "POST" for item in server.requests))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=5)


class ListTokenBodyTests(unittest.TestCase):
    def test_the_body_names_the_repository_and_omits_permissions(self):
        self.assertEqual(list_token_body("moonbase2090/lunatui"), {"repositories": ["lunatui"]})


if __name__ == "__main__":
    unittest.main()
