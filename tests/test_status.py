import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from execution_core.protocol import canonical
from execution_core.status import (
    github_state,
    post_status,
    read_credential,
    status_url,
    StatusError,
)
from execution_core.worker import Worker
from schema_support import validate_response


COMMIT = "ab" * 20
HEAD = "cd" * 20
TOKEN = "token-ns40-secret-value"
CONTEXT = "rookrunner/check.yml/check"


def _request(method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return canonical(body).encode()


def _snapshot(state, *, dirty=False, included=None, base=COMMIT):
    snapshot_id = str(uuid.uuid4())
    root = Path(state) / "snapshots" / snapshot_id
    files = root / "files"
    files.mkdir(parents=True, mode=0o700)
    payload = b"echo hi\n"
    target = files / "workflow.yml"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.fchmod(fd, 0o644)
    os.write(fd, payload)
    os.close(fd)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "format_version": 1,
        "base_commit": base,
        "dirty": dirty,
        "git_object_format": "sha1",
        "workflow": "workflow.yml",
        "workflow_digest": digest,
        "included": [] if included is None else included,
        "excluded": [],
        "deleted": [],
        "entries": [
            {
                "path": "workflow.yml",
                "kind": "file",
                "mode": "100644",
                "size": len(payload),
                "sha256": digest,
            }
        ],
    }
    encoded = canonical(manifest).encode()
    (root / "manifest.json").write_bytes(encoded)
    return snapshot_id, hashlib.sha256(encoded).hexdigest()


def _record(worker, snapshot_id, digest, *, state="succeeded", exit_code=0, kind="workflow_job"):
    run_id = str(uuid.uuid4())
    record = {
        "run_id": run_id,
        "worker_id": worker.worker_id,
        "submission_key": run_id,
        "state": state,
        "exit_code": exit_code,
        "input": {
            "kind": kind,
            "digest": digest,
            "snapshot_id": snapshot_id,
            "workflow": "workflow.yml",
            "workflow_digest": "ef" * 32,
            "plan_digest": "11" * 32,
            "job_id": "check",
            "event_digest": "22" * 32,
            "image_digest": "sha256:" + "33" * 32,
            "image_reference": "example@sha256:" + "33" * 32,
        },
        "backend": {
            "name": "workflow" if kind == "workflow_job" else "development",
            "version": "0.0.1",
        },
        "compatibility_notes": [],
        "accepted_at": "2026-10-04T00:00:00.000000+00:00",
        "started_at": "2026-10-04T00:00:01.000000+00:00",
        "finished_at": "2026-10-04T00:00:02.000000+00:00",
        "attempt_id": str(uuid.uuid4()),
        "cancel_requested": False,
        "error": None,
        "cleanup": "confirmed_no_external_resources",
    }
    with worker.guard, worker.db:
        worker.db.execute(
            "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
            (run_id, run_id, "{}", canonical(record), b""),
        )
    return record


def _params(run_id, *, tested=COMMIT, sha=HEAD, context=CONTEXT, record=None):
    params = {
        "run_id": run_id,
        "tested_commit": tested,
        "status_sha": sha,
        "context": context,
    }
    if record is not None:
        params["record"] = record
    return params


class MappingTests(unittest.TestCase):
    def test_only_a_zero_exit_is_success(self):
        self.assertEqual(github_state("queued", None), "pending")
        self.assertEqual(github_state("running", None), "pending")
        self.assertEqual(github_state("succeeded", 0), "success")
        self.assertEqual(github_state("succeeded", None), "error")
        self.assertEqual(github_state("succeeded", 1), "error")
        self.assertEqual(github_state("failed", 1), "failure")
        self.assertEqual(github_state("failed", None), "failure")
        self.assertEqual(github_state("cancelled", None), "error")
        self.assertEqual(github_state("lost", None), "error")
        self.assertEqual(github_state("unknown", 0), "error")

    def test_url_uses_the_status_sha(self):
        url = status_url("https://api.github.com", "moonbase2090/rookrunner", HEAD)
        self.assertEqual(
            url, f"https://api.github.com/repos/moonbase2090/rookrunner/statuses/{HEAD}"
        )

    def test_url_rejects_a_credential_or_a_path(self):
        with self.assertRaises(StatusError):
            status_url("https://user:secret@api.github.com", "moonbase2090/rookrunner", HEAD)
        with self.assertRaises(StatusError):
            status_url("https://api.github.com/extra", "moonbase2090/rookrunner", HEAD)
        with self.assertRaises(StatusError):
            status_url("file:///tmp/api", "moonbase2090/rookrunner", HEAD)
        with self.assertRaises(StatusError):
            status_url("https://api.github.com", "owner/name/extra", HEAD)


class WorkerStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.worker = Worker(self.repo, self.state)
        self.worker.start()

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def _reply(self, params):
        reply = self.worker.response(_request("run.status", params))
        validate_response("run.status", reply)
        return reply

    def test_a_clean_run_asks_for_one_post_on_the_status_sha(self):
        snapshot_id, digest = _snapshot(self.state)
        run = _record(self.worker, snapshot_id, digest)
        reply = self._reply(_params(run["run_id"]))
        self.assertEqual(reply["result"], {"action": "post", "state": "success"})
        recorded = self._reply(_params(run["run_id"], record="success"))
        self.assertEqual(recorded["result"], {"action": "recorded", "state": "success"})
        again = self._reply(_params(run["run_id"]))
        self.assertEqual(again["result"], {"action": "skip", "state": "success"})

    def test_pending_is_not_a_terminal_skip(self):
        snapshot_id, digest = _snapshot(self.state)
        run = _record(self.worker, snapshot_id, digest, state="queued", exit_code=None)
        first = self._reply(_params(run["run_id"]))
        self.assertEqual(first["result"]["state"], "pending")
        self._reply(_params(run["run_id"], record="pending"))
        second = self._reply(_params(run["run_id"]))
        self.assertEqual(second["result"], {"action": "post", "state": "pending"})

    def test_refusals_are_structured_and_do_not_record(self):
        snapshot_id, digest = _snapshot(self.state, dirty=True)
        dirty = _record(self.worker, snapshot_id, digest, state="failed", exit_code=1)
        refused = self._reply(_params(dirty["run_id"]))
        self.assertEqual(refused["error"]["data"]["kind"], "STATUS_REFUSED")
        self.assertIn("dirty", refused["error"]["message"])
        included_id, included_digest = _snapshot(self.state, included=["note.txt"])
        included = _record(self.worker, included_id, included_digest)
        refused = self._reply(_params(included["run_id"]))
        self.assertIn("includes", refused["error"]["message"])
        clean_id, clean_digest = _snapshot(self.state)
        mismatched = _record(self.worker, clean_id, clean_digest)
        refused = self._reply(_params(mismatched["run_id"], tested="12" * 20))
        self.assertIn("tested commit", refused["error"]["message"])
        fixture = _record(self.worker, clean_id, clean_digest, kind="development_fixture")
        refused = self._reply(_params(fixture["run_id"]))
        self.assertIn("workflow", refused["error"]["message"])
        self.assertEqual(
            self.worker.db.execute("SELECT count(*) FROM status_posts").fetchone()[0], 0
        )

    def test_a_failed_run_maps_to_failure_and_a_nonzero_success_maps_to_error(self):
        snapshot_id, digest = _snapshot(self.state)
        failed = _record(self.worker, snapshot_id, digest, state="failed", exit_code=1)
        self.assertEqual(self._reply(_params(failed["run_id"]))["result"]["state"], "failure")
        other_id, other_digest = _snapshot(self.state)
        nonzero = _record(self.worker, other_id, other_digest, state="succeeded", exit_code=2)
        self.assertEqual(self._reply(_params(nonzero["run_id"]))["result"]["state"], "error")
        cancelled_id, cancelled_digest = _snapshot(self.state)
        cancelled = _record(
            self.worker, cancelled_id, cancelled_digest, state="cancelled", exit_code=None
        )
        self.assertEqual(self._reply(_params(cancelled["run_id"]))["result"]["state"], "error")

    def test_recorded_state_must_match_the_run(self):
        snapshot_id, digest = _snapshot(self.state)
        run = _record(self.worker, snapshot_id, digest, state="failed", exit_code=1)
        reply = self._reply(_params(run["run_id"], record="success"))
        self.assertEqual(reply["error"]["data"]["kind"], "INVALID_PARAMS")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        code, extra = self.server.reply
        self.send_response(code)
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class CliStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.credential = self.root / "credential"
        self.credential.write_text(TOKEN + "\n")
        os.chmod(self.credential, 0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.reply = (201, [])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.worker = subprocess.Popen(
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.worker.poll() is not None:
                self.fail(self.worker.stderr.read().decode())
            if (self.state / "worker.sock").exists() and (self.state / "runs.sqlite3").exists():
                break
            time.sleep(0.01)
        else:
            self.fail("worker did not become ready")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.communicate(timeout=5)
        self.tmp.cleanup()

    def _insert(self, **record_kwargs):
        snapshot_kwargs = {}
        for key in ("dirty", "included", "base"):
            if key in record_kwargs:
                snapshot_kwargs[key] = record_kwargs.pop(key)
        snapshot_id, digest = _snapshot(self.state, **snapshot_kwargs)
        with sqlite3.connect(self.state / "runs.sqlite3", timeout=5) as connection:
            worker_id = connection.execute(
                "SELECT value FROM metadata WHERE key='worker_id'"
            ).fetchone()[0]
        record = {
            "run_id": str(uuid.uuid4()),
            "worker_id": worker_id,
            "submission_key": None,
            "state": record_kwargs.get("state", "succeeded"),
            "exit_code": record_kwargs.get("exit_code", 0),
            "input": {
                "kind": "workflow_job",
                "digest": digest,
                "snapshot_id": snapshot_id,
            },
            "backend": {"name": "workflow", "version": "0.0.1"},
            "compatibility_notes": [],
            "accepted_at": "2026-10-04T00:00:00.000000+00:00",
            "started_at": "2026-10-04T00:00:01.000000+00:00",
            "finished_at": "2026-10-04T00:00:02.000000+00:00",
            "attempt_id": str(uuid.uuid4()),
            "cancel_requested": False,
            "error": None,
            "cleanup": "confirmed_no_external_resources",
        }
        record["submission_key"] = record["run_id"]
        record["input"]["workflow"] = "workflow.yml"
        record["input"]["workflow_digest"] = "ef" * 32
        record["input"]["plan_digest"] = "11" * 32
        record["input"]["job_id"] = "check"
        record["input"]["event_digest"] = "22" * 32
        record["input"]["image_digest"] = "sha256:" + ("33" * 32)
        record["input"]["image_reference"] = "example@sha256:" + ("33" * 32)
        with sqlite3.connect(self.state / "runs.sqlite3", timeout=5) as connection:
            connection.execute(
                "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                (record["run_id"], record["run_id"], "{}", canonical(record), b""),
            )
        return record

    def _cli(self, run_id, *, credential=None, tested=COMMIT, sha=HEAD):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "status",
                run_id,
                "--repository",
                "moonbase2090/rookrunner",
                "--status-sha",
                sha,
                "--tested-commit",
                tested,
                "--context",
                CONTEXT,
                "--credential-file",
                str(credential or self.credential),
                "--api-base",
                f"http://127.0.0.1:{self.server.server_address[1]}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_posts_the_mapped_state_and_skips_the_same_terminal_state(self):
        run = self._insert()
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout)
        self.assertNotIn(TOKEN, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        posted = self.server.requests[0]
        self.assertEqual(posted["path"], f"/repos/moonbase2090/rookrunner/statuses/{HEAD}")
        self.assertEqual(posted["authorization"], "Bearer " + TOKEN)
        self.assertEqual(json.loads(posted["body"]), {"context": CONTEXT, "state": "success"})
        stored = b"".join(path.read_bytes() for path in self.state.rglob("*") if path.is_file())
        self.assertNotIn(TOKEN.encode(), stored)
        again = self._cli(run["run_id"])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(json.loads(again.stdout)["result"]["action"], "skip")

    def test_a_dirty_run_and_a_sha_mismatch_send_nothing(self):
        dirty = self._insert(dirty=True, state="failed", exit_code=1)
        result = self._cli(dirty["run_id"], credential=self.root / "missing-credential")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["data"]["kind"], "STATUS_REFUSED")
        mismatch = self._insert()
        result = self._cli(mismatch["run_id"], tested="12" * 20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.server.requests), 0)
        self.assertFalse(self.credential.read_text().strip() == "")

    def test_rate_limit_is_not_retried(self):
        self.server.reply = (429, [])
        run = self._insert(state="running", exit_code=None)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["kind"], "RATE_LIMITED")
        self.assertIs(payload["error"]["retryable"], True)
        self.assertNotIn(TOKEN, result.stderr)
        with sqlite3.connect(self.state / "runs.sqlite3") as conn:
            count = conn.execute("SELECT count(*) FROM status_posts").fetchone()[0]
        self.assertEqual(count, 0)

    def test_credential_inside_state_is_not_sent(self):
        planted = self.state / "token"
        planted.write_text(TOKEN + "\n")
        run = self._insert()
        result = self._cli(run["run_id"], credential=planted)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.server.requests), 0)
        self.assertEqual(json.loads(result.stderr)["error"]["kind"], "CREDENTIAL_UNREADABLE")
        self.assertNotIn(TOKEN, result.stderr)


class PostTests(unittest.TestCase):
    def test_a_forbidden_rate_limit_is_retryable_and_a_redirect_is_not_followed(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.requests = []
        server.reply = (403, [("x-ratelimit-remaining", "0")])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(StatusError) as caught:
                post_status(base, "moonbase2090/rookrunner", HEAD, "pending", CONTEXT, TOKEN)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(len(server.requests), 1)
            server.requests.clear()
            server.reply = (302, [("Location", base + "/elsewhere")])
            with self.assertRaises(StatusError) as caught:
                post_status(base, "moonbase2090/rookrunner", HEAD, "pending", CONTEXT, TOKEN)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn(TOKEN, str(caught.exception))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_credential_must_be_one_line_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            state.mkdir()
            secret = root / "secret"
            secret.write_text("one-line-token\n")
            self.assertEqual(read_credential(secret, state, repo), "one-line-token")
            secret.write_text("one\ntwo\n")
            with self.assertRaises(StatusError):
                read_credential(secret, state, repo)
            inside = repo / "secret"
            inside.write_text("one-line-token\n")
            with self.assertRaises(StatusError) as caught:
                read_credential(inside, state, repo)
            self.assertNotIn("one-line-token", str(caught.exception))
