# SPDX-License-Identifier: MIT

"""Docker-backed canary for an installed engine artifact and legacy state."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from execution_core.poll import PollError, poll_once
from execution_core.protocol import canonical
from execution_core.verify import verify_snapshot
from execution_core.engine_canary import run_poller_canary
from schema_support import validate_response
from execution_core.worker import Worker
from test_poll import TOKEN, WORKFLOW, _Handler, _git

LEGACY_STATE = Path(__file__).parent / "fixtures/pre-d8-state.sql"
CANARY_IMAGE = os.environ.get("ROOKRUNNER_CANARY_IMAGE")


@unittest.skipUnless(CANARY_IMAGE, "CI supplies a pinned Docker image for the canary")
class EngineReleaseCanaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="engine-release-canary-")
        self.root = Path(self.tmp.name)
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.seed.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
        )
        _git(self.seed, "init", "-b", "main")
        _git(self.seed, "config", "user.name", "Fixture")
        _git(self.seed, "config", "user.email", "fixture@example.invalid")
        workflow = self.seed / ".github/workflows/check.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW.replace("echo hi", "printf 'release canary\\n'"))
        _git(self.seed, "add", ".")
        _git(self.seed, "commit", "-m", "canary input")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-u", "origin", "main")
        self.clone = self.root / "clone"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.clone)],
            check=True,
            capture_output=True,
        )
        legacy_state = self.root / "pre-d8-state"
        legacy_state.mkdir(mode=0o700)
        database = sqlite3.connect(legacy_state / "runs.sqlite3")
        database.executescript(LEGACY_STATE.read_text(encoding="utf-8"))
        old_record = {"run_id": "pre-d8-history", "state": "succeeded", "exit_code": 0}
        database.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [("repository", str(self.clone.resolve())), ("worker_id", "legacy-worker")],
        )
        database.execute(
            "INSERT INTO runs (id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
            ("pre-d8-history", "old-key", "{}", canonical(old_record), b""),
        )
        database.commit()
        self.assertIsNone(
            database.execute(
                "SELECT value FROM metadata WHERE key='state_format_version'"
            ).fetchone()
        )
        database.close()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        shutil.copyfile(legacy_state / "runs.sqlite3", self.state / "runs.sqlite3")
        os.chmod(self.state / "runs.sqlite3", 0o600)

        self.credential = self.root / "credential"
        self.credential.write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.credential, 0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.branches = []
        self.server.pulls = []
        self.server.remaining = 40
        self.server.remaining_by_kind = {}
        self.server.repository = {"id": 5150, "full_name": "ignored/name", "default_branch": "main"}
        self.server.authors = {}
        self.server.committers = {}
        self.server.verified = set()
        self.server.permissions = {}
        self.server.pull_commits = {}
        self.server.remote = self.remote
        self.server.etags = {"branches": "branches-1", "pulls": "pulls-1"}
        self.server.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server.thread.start()
        self.api_base = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.worker = Worker(self.clone, self.state, runner_image=CANARY_IMAGE)
        self.worker.start()

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.thread.join(timeout=5)
            self.server.server_close()
        if getattr(self, "worker", None) is not None:
            self.worker.close()
        self.tmp.cleanup()

    def caller(self, method, params):
        reply = self.worker.response(
            (
                canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
            ).encode()
        )
        if "error" in reply:
            raise PollError(reply["error"]["data"]["kind"], reply["error"]["message"])
        return reply["result"]

    def poll(self):
        return poll_once(
            repository="acme/demo",
            clone=self.clone,
            jobs=[(".github/workflows/check.yml", "check")],
            image=CANARY_IMAGE,
            credential_file=self.credential,
            api_base=self.api_base,
            state=self.state,
            caller=self.caller,
        )

    def test_installed_release_migrates_legacy_state_and_passes_canary_gate(self):
        manifest = self.worker.engine_manifest
        self.assertIsInstance(manifest, dict)
        validate_response(
            "worker.describe",
            {"jsonrpc": "2.0", "id": 1, "result": self.caller("worker.describe", {})},
        )
        expected_commit = os.environ.get("ROOKRUNNER_EXPECTED_COMMIT")
        if expected_commit:
            self.assertEqual(expected_commit, manifest["source_commit"])
        with self.worker.db:
            self.assertEqual(
                "1",
                self.worker.db.execute(
                    "SELECT value FROM metadata WHERE key='state_format_version'"
                ).fetchone()[0],
            )
            self.assertIsNotNone(
                self.worker.db.execute("SELECT 1 FROM runs WHERE id='pre-d8-history'").fetchone()
            )

        tip = _git(self.remote, "rev-parse", "refs/heads/main").stdout.strip()
        self.server.branches = [{"name": "main", "commit": {"sha": tip}}]
        evidence = run_poller_canary(
            self.poll,
            self.caller,
            expected_engine_commit=manifest["source_commit"],
            timeout=180,
        )
        self.assertEqual("succeeded", evidence["run_state"])
        self.assertEqual(0, evidence["exit_code"])
        self.assertEqual(tip, evidence["commit_sha"])
        self.assertTrue(evidence["input_digest_verified"])
        self.assertEqual("posted", evidence["status_disposition"])
        self.assertEqual("clean", evidence["second_poll"])

        record = self.worker.get(evidence["run_id"])
        snapshot = self.state / "snapshots" / record["input"]["snapshot_id"]
        verified = verify_snapshot(snapshot, evidence["input_digest"])
        self.assertEqual(tip, verified["base_commit"])
        status_posts = [
            request
            for request in self.server.requests
            if request["method"] == "POST" and request["path"].endswith("/statuses/" + tip)
        ]
        self.assertEqual("success", json.loads(status_posts[-1]["body"])["state"])
        posted_states = [json.loads(request["body"])["state"] for request in status_posts]
        self.assertEqual(1, posted_states.count("success"))


if __name__ == "__main__":
    unittest.main()
