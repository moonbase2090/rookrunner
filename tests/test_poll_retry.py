# SPDX-License-Identifier: MIT

"""Explicit retries keep failed-run history and recheck current trust."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
import unittest

from execution_core.poll import PollError
import test_poll as fixtures


class PollRetryTests(unittest.TestCase):
    def setUp(self):
        self.host = fixtures.PollTests("setUp")
        self.host.setUp()

    def tearDown(self):
        self.host.tearDown()

    def _saved(self):
        return json.loads((self.host.state / "poll.json").read_text())

    def _finish(self, run_id, state="failed", exit_code=1):
        worker = self.host.worker
        record = worker.get(run_id)
        record.update(state=state, exit_code=exit_code)
        with worker.guard, worker.db:
            worker.save(record)

    def _push(self):
        tip = self.host._tip()
        self.host.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        result = self.host.poll()
        run_id = result["submitted"][0]["run_id"]
        self._finish(run_id)
        return tip, run_id, self._saved()["submissions"][0]["key"]

    def test_a_failed_push_gets_one_successor_and_keeps_its_original_record(self):
        _tip, old_id, key = self._push()
        result = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        new_id = result["submitted"][0]["run_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.host.worker.get(old_id)["state"], "failed")
        self.assertEqual(self.host.worker.get(new_id)["state"], "queued")
        self.assertEqual(json.loads(self.host.posts()[-1]["body"])["state"], "pending")
        rows = self._saved()["submissions"]
        self.assertEqual(self._saved()["completed"][key]["run_id"], old_id)
        self.assertEqual(rows[0]["retry_of"], key)
        self.assertNotEqual(rows[0]["key"], key)
        self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        self._finish(new_id, "succeeded", 0)
        self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)

    def test_a_storage_refusal_on_submit_is_retried_once_through_the_real_submit_path(self):
        # The worker answers the first submit with the earlier storage-full failure
        # for the same key; every other call reaches the real worker.
        host = self.host
        host.server.branches = [{"name": "main", "commit": {"sha": host._tip()}}]
        original = host.caller
        keys = []
        refusal = {
            "state": "failed",
            "run_id": "earlier-failure",
            "error": {
                "kind": "SETUP_FAILED",
                "message": "worker storage is full; free space before retrying",
            },
        }

        def wrapped(method, params):
            if method == "run.submit":
                keys.append(params["submission_key"])
                if len(keys) == 1:
                    return dict(refusal)
            return original(method, params)

        host.caller = wrapped
        try:
            result = host.poll()
        finally:
            host.caller = original
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertTrue(keys[1].startswith("poll-"))
        self.assertEqual(len(result["submitted"]), 1)
        self.assertNotEqual(result["submitted"][0]["run_id"], "earlier-failure")
        self.assertEqual(host.runs(), 1)

    def test_a_pruned_failure_can_be_retried_after_an_ordinary_pass(self):
        _tip, old_id, key = self._push()
        self.host.poll()
        saved = self._saved()
        self.assertEqual(saved["submissions"], [])
        self.host.poll()
        self.assertEqual(self.host.runs(), 1)
        result = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        self.assertNotEqual(result["submitted"][0]["run_id"], old_id)
        self.assertEqual(self.host.worker.get(old_id)["state"], "failed")
        self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)

    def test_only_a_failed_run_can_be_retried(self):
        _tip, run_id, key = self._push()
        for state, code in [
            ("succeeded", 0),
            ("queued", None),
            ("running", None),
            ("lost", None),
            ("cancelled", None),
        ]:
            with self.subTest(state=state):
                self._finish(run_id, state, code)
                with self.assertRaises(PollError) as raised:
                    self.host.poll(retry_failed=key)
                self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
                self.assertEqual(self.host.runs(), 1)

    def test_retry_rechecks_push_author_permission(self):
        tip, _run_id, key = self._push()
        self.host.server.authors[tip] = "outsider"
        self.host.server.permissions = {"outsider": "read"}
        result = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 1)
        self.assertEqual(result["submitted"], [])
        self.assertIn("commit author cannot write", result["warnings"][0]["message"])

    def test_retry_rechecks_an_already_trusted_pull_request(self):
        def mutate(seed):
            (seed / "feature-9.txt").write_text("feature\n")

        _base, head = self.host._open_feature(9, mutate)
        result = self.host.poll()
        run_id = result["submitted"][0]["run_id"]
        self._finish(run_id)
        key = self._saved()["submissions"][0]["key"]
        self.host.server.authors[head] = "outsider"
        self.host.server.permissions = {"outsider": "read"}
        result = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 1)
        self.assertEqual(result["submitted"], [])
        trust_posts = [
            json.loads(row["body"])
            for row in self.host.posts()
            if json.loads(row["body"])["context"] == "rookrunner/trust"
        ]
        self.assertEqual(
            trust_posts,
            [
                {
                    "context": "rookrunner/trust",
                    "state": "failure",
                    "description": "refused: untrusted code: commit author cannot write to the repository",
                }
            ],
        )

    def test_an_unknown_key_creates_no_run(self):
        self._push()
        with self.assertRaises(PollError) as raised:
            self.host.poll(retry_failed="poll-" + "0" * 64)
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertEqual(self.host.runs(), 1)

    def test_a_lost_retry_reply_reuses_the_same_successor(self):
        _tip, _old_id, key = self._push()
        original = self.host.caller

        def lose_reply(method, params):
            result = original(method, params)
            if method == "run.submit":
                raise PollError("WORKER_ERROR", "retry reply lost")
            return result

        self.host.caller = lose_reply
        with self.assertRaisesRegex(PollError, "retry reply lost"):
            self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        self.host.caller = original
        recovered = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        self.assertEqual(len(recovered["submitted"]), 1)
        self.assertEqual(
            self._saved()["submissions"][0]["run_id"], recovered["submitted"][0]["run_id"]
        )

    def test_a_trusted_failed_pull_can_run_again(self):
        def mutate(seed):
            (seed / "feature-9.txt").write_text("feature\n")

        self.host._open_feature(9, mutate)
        first = self.host.poll()
        old_id = first["submitted"][0]["run_id"]
        self._finish(old_id)
        key = self._saved()["submissions"][0]["key"]
        second = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        new_id = second["submitted"][0]["run_id"]
        self.assertNotEqual(old_id, new_id)
        old = self.host.worker.get(old_id)
        new = self.host.worker.get(new_id)
        self.assertEqual(old["input"]["digest"], new["input"]["digest"])
        self.assertEqual(new["state"], "queued")

    def test_a_failed_successor_can_be_retried_explicitly(self):
        _tip, _old_id, key = self._push()
        second = self.host.poll(retry_failed=key)
        self._finish(second["submitted"][0]["run_id"])
        next_key = self._saved()["submissions"][0]["key"]
        third = self.host.poll(retry_failed=next_key)
        self.assertEqual(self.host.runs(), 3)
        self.assertEqual(len(third["submitted"]), 1)
        self.host.poll(retry_failed=next_key)
        self.assertEqual(self.host.runs(), 3)

    def test_a_placed_failure_uses_a_new_key_on_the_reached_worker(self):
        tip = self.host._tip()
        self.host.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        worker = self.host._place_worker("fixture")
        places = [self.host._place("fixture", worker, self.host._bound(worker, []))]
        first = self.host.poll(places=places)
        old_id = first["submitted"][0]["run_id"]
        record = worker.get(old_id)
        record.update(state="failed", exit_code=1)
        with worker.guard, worker.db:
            worker.save(record)
        key = self._saved()["submissions"][0]["key"]
        retried = self.host.poll(places=places, retry_failed=key)
        self.assertEqual(retried["submitted"][0]["host"], "fixture")
        self.assertNotEqual(retried["submitted"][0]["run_id"], old_id)
        self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.host.poll(places=places, retry_failed=key)
        self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)

    def test_the_cli_retries_the_selected_failed_key(self):
        _tip, old_id, key = self._push()
        done = threading.Event()
        worker = self.host.worker

        def serve():
            while not done.is_set():
                try:
                    client, _ = worker.server.accept()
                except TimeoutError:
                    continue
                with client, client.makefile("rb") as stream:
                    raw = stream.readline(1024 * 1024 + 1)
                    client.sendall((json.dumps(worker.response(raw)) + "\n").encode())

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            command = [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.host.state),
                "poll",
                "--repository",
                "acme/demo",
                "--clone",
                str(self.host.clone),
                "--job",
                ".github/workflows/check.yml",
                "check",
                "--image",
                fixtures.IMAGE,
                "--credential-file",
                str(self.host.credential),
                "--api-base",
                self.host.base,
                "--retry-failed",
                key,
            ]
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            retry = json.loads(result.stdout)["submitted"]
            self.assertEqual(len(retry), 1)
            self.assertNotEqual(retry[0]["run_id"], old_id)
            self.assertEqual(self.host.runs(), 2)
            again = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["submitted"], [])
            self.assertEqual(self.host.runs(), 2)
        finally:
            done.set()
            thread.join(timeout=2)

    def test_a_push_retry_does_not_disappear_behind_an_empty_diff(self):
        self.host._write(
            fixtures.WORKFLOW.replace("  push:\n", "  push:\n    paths: [source.txt]\n")
        )
        fixtures._git(self.host.seed, "push", "origin", "main")
        _tip, old_id, key = self._push()
        retried = self.host.poll(retry_failed=key)
        self.assertEqual(self.host.runs(), 2)
        self.assertNotEqual(retried["submitted"][0]["run_id"], old_id)

    def test_a_retry_does_not_inherit_workflow_edit_permission(self):
        def mutate(seed):
            path = seed / ".github/workflows/check.yml"
            path.write_text(path.read_text().replace("echo hi", "echo changed"))

        self.host._open_feature(9, mutate)
        first = self.host.poll(allow_pr_workflow=["COLLABORATOR"])
        old_id = first["submitted"][0]["run_id"]
        self._finish(old_id)
        key = self._saved()["submissions"][0]["key"]
        refused = self.host.poll(retry_failed=key)
        self.assertEqual(refused["submitted"], [])
        self.assertEqual(self.host.runs(), 1)
        self.assertIn({"reason": "workflow_changed", "number": 9}, refused["skipped"])
