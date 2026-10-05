"""Check runs through a local HTTP stub and a fixture key under a temporary HOME.

No test reads ~/Secrets/github-app/rookrunner-app/ and no test contacts api.github.com.
"""

import base64
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from execution_core.checks import check_mapping, check_summary, post_check_flow
from execution_core.plan import CAPABILITY_VERSION, PlanError, plan_workflow
from execution_core.poll import PollError, poll_once
from execution_core.protocol import canonical
from execution_core.run import _ATTEMPT, _protected_defaults, _reserved_env, _soft_defaults
from schema_support import validate_response
from test_status import COMMIT, CONTEXT, HEAD, _snapshot


CLIENT = "Iv1.fixtureclient"
INSTALL = "424242"
TOKEN = "ghs_fixture_installation_token"
CHECK_ID = 901
MARKER = "response-body-marker-not-stored"
ROOT = Path(__file__).resolve().parents[1]


def _b64decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._accept("POST")

    def do_PATCH(self):
        self._accept("PATCH")

    def _accept(self, method):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path.endswith("/access_tokens"):
            kind = "token"
        elif self.path.endswith("/check-runs"):
            kind = "check"
        elif "/check-runs/" in self.path:
            kind = "check"
        else:
            kind = "status"
        self.server.requests.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "version": self.headers.get("X-GitHub-Api-Version"),
                "agent": self.headers.get("User-Agent"),
            }
        )
        code, extra, payload = self.server.routes.get(kind, (201, [], b"{}"))
        self.send_response(code)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return


def _routes(token_code=201, check_code=201, status_code=201, token_headers=(), check_headers=()):
    token_body = json.dumps({"token": TOKEN, "expires_at": "2026-10-05T00:00:00Z"}).encode()
    check_body = json.dumps({"id": CHECK_ID, "output": {"summary": MARKER}}).encode()
    if token_code != 201:
        token_body = b""
    if check_code not in {200, 201}:
        check_body = b""
    return {
        "token": (token_code, list(token_headers), token_body),
        "check": (check_code, list(check_headers), check_body),
        "status": (status_code, [], b"{}"),
    }


class MappingTests(unittest.TestCase):
    def test_only_a_zero_exit_is_success_and_there_is_no_error_conclusion(self):
        self.assertEqual(check_mapping("queued", None), ("queued", None))
        self.assertEqual(check_mapping("running", None), ("in_progress", None))
        self.assertEqual(check_mapping("succeeded", 0), ("completed", "success"))
        self.assertEqual(check_mapping("succeeded", 1), ("completed", "failure"))
        self.assertEqual(check_mapping("succeeded", None), ("completed", "failure"))
        self.assertEqual(check_mapping("failed", 1), ("completed", "failure"))
        self.assertEqual(check_mapping("cancelled", None), ("completed", "cancelled"))
        self.assertEqual(check_mapping("lost", None), ("completed", "failure"))
        self.assertEqual(check_mapping("unknown", 0), ("completed", "failure"))
        self.assertEqual(check_summary("succeeded", 0), "succeeded exit_code 0")
        self.assertEqual(check_summary("queued", None), "queued")
        self.assertNotIn(
            "neutral",
            {
                item[1]
                for item in (
                    check_mapping("lost", None),
                    check_mapping("cancelled", None),
                )
            },
        )


class BoundsTests(unittest.TestCase):
    def test_capability_check_yml_and_rejected_writes_stay_put(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        text = (ROOT / ".github/workflows/check.yml").read_text()
        self.assertNotIn("--app-key", text)
        self.assertNotIn("github-app", text)
        self.assertNotIn("private-key", text)
        for scope in ("security-events", "actions"):
            workflow = (
                f"permissions:\n  {scope}: write\n"
                "on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
            )
            with self.assertRaises(PlanError) as caught:
                plan_workflow(workflow.encode(), "build")
            self.assertEqual(caught.exception.kind, "CAPABILITY_UNSUPPORTED")
            self.assertIn(scope, caught.exception.field)

    def test_the_job_environment_has_no_github_token(self):
        token = _ATTEMPT.set({"arch": "X64", "event_name": "push", "sha": COMMIT})
        try:
            names = set(_protected_defaults())
            names.update(_soft_defaults())
            names.update(_reserved_env("step"))
        finally:
            _ATTEMPT.reset(token)
        self.assertNotIn("GITHUB_TOKEN", names)

    def test_poll_refuses_both_credentials_and_neither_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for app_key, credential in ((str(root / "key"), str(root / "token")), (None, None)):
                with self.assertRaises(PollError) as caught:
                    poll_once(
                        repository="acme/demo",
                        clone=root,
                        jobs=[("check.yml", "check")],
                        image=None,
                        credential_file=credential,
                        app_key=app_key,
                        api_base="http://127.0.0.1:9",
                        state=root,
                        caller=lambda method, params: {},
                    )
                self.assertIn("one post", str(caught.exception))
                self.assertIn("credential", str(caught.exception))


class CliCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="execution-app-key-")
        cls.pem_path = Path(cls.keys.name) / "private-key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(cls.pem_path), "2048"],
            check=True,
            capture_output=True,
        )
        cls.public = Path(cls.keys.name) / "public.pem"
        subprocess.run(
            ["openssl", "pkey", "-in", str(cls.pem_path), "-pubout", "-out", str(cls.public)],
            check=True,
            capture_output=True,
        )
        cls.pem_bytes = cls.pem_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execution-test-")
        # /var is a symlink on macOS. A key path with a symlink component is refused.
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.key = self._install()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.routes = _routes()
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

    def _install(self, pem=None, mode=0o600, directory_mode=0o700):
        directory = self.home / "Secrets" / "github-app" / "rookrunner-app"
        directory.mkdir(parents=True)
        os.chmod(directory, directory_mode)
        key = directory / "private-key.pem"
        key.write_bytes(self.pem_bytes if pem is None else pem)
        os.chmod(key, mode)
        client = directory / "client-id"
        client.write_text(CLIENT + "\n")
        os.chmod(client, 0o600)
        install = directory / "installation-id"
        install.write_text(INSTALL + "\n")
        os.chmod(install, 0o600)
        return key

    def _insert(self, **record_kwargs):
        snapshot_kwargs = {}
        for name in ("dirty", "included", "base"):
            if name in record_kwargs:
                snapshot_kwargs[name] = record_kwargs.pop(name)
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
                "workflow": "workflow.yml",
                "workflow_digest": "ef" * 32,
                "plan_digest": "11" * 32,
                "job_id": "check",
                "event_digest": "22" * 32,
                "image_digest": "sha256:" + ("33" * 32),
                "image_reference": "example@sha256:" + ("33" * 32),
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
        with sqlite3.connect(self.state / "runs.sqlite3", timeout=5) as connection:
            connection.execute(
                "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?, ?, ?, ?, ?)",
                (record["run_id"], record["run_id"], "{}", canonical(record), b""),
            )
        return record

    def _cli(self, run_id, *, key=None, credential=None, tested=COMMIT, sha=HEAD, both=False):
        command = [
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
            "--api-base",
            f"http://127.0.0.1:{self.server.server_address[1]}",
        ]
        app_key = key
        if app_key is None and (both or credential is None):
            app_key = "~/Secrets/github-app/rookrunner-app/private-key.pem"
        if app_key is not None:
            command.extend(["--app-key", str(app_key)])
        if credential is not None or both:
            command.extend(["--credential-file", str(credential or self.root / "token")])
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        return subprocess.run(command, capture_output=True, text=True, timeout=30, env=env)

    def _state_bytes(self):
        chunks = []
        for path in self.state.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            chunks.append(path.read_bytes())
        return b"".join(chunks)

    def _assert_secret_absent(self, result, *extra):
        stored = self._state_bytes()
        for secret in (TOKEN, CLIENT, INSTALL, MARKER, self.pem_bytes, str(self.key), *extra):
            if isinstance(secret, str):
                self.assertNotIn(secret, result.stdout)
                self.assertNotIn(secret, result.stderr)
                self.assertNotIn(secret.encode(), stored)
            else:
                self.assertNotIn(secret, stored)

    def _assert_jwt(self, token):
        header_b, payload_b, signature_b = token.split(".")
        header = json.loads(_b64decode(header_b))
        payload = json.loads(_b64decode(payload_b))
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(set(payload), {"iat", "exp", "iss"})
        self.assertEqual(payload["iss"], CLIENT)
        self.assertEqual(payload["exp"] - payload["iat"], 600)
        now = int(datetime.now(timezone.utc).timestamp())
        self.assertAlmostEqual(payload["iat"], now - 60, delta=5)
        directory = Path(self.tmp.name) / "jwt"
        directory.mkdir()
        data = directory / "data"
        signature = directory / "sig"
        data.write_bytes(f"{header_b}.{payload_b}".encode())
        signature.write_bytes(_b64decode(signature_b))
        verified = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(self.public),
                "-signature",
                str(signature),
                str(data),
            ],
            capture_output=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())

    def test_one_post_exchanges_a_jwt_creates_a_check_and_posts_the_status(self):
        run = self._insert()
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [item["path"] for item in self.server.requests],
            [
                f"/app/installations/{INSTALL}/access_tokens",
                "/repos/moonbase2090/rookrunner/check-runs",
                f"/repos/moonbase2090/rookrunner/statuses/{HEAD}",
            ],
        )
        token_request, check_request, status_request = self.server.requests
        self.assertEqual(token_request["method"], "POST")
        self.assertTrue(token_request["authorization"].startswith("Bearer "))
        jwt = token_request["authorization"].removeprefix("Bearer ")
        self._assert_jwt(jwt)
        self.assertEqual(
            json.loads(token_request["body"]),
            {"permissions": {"checks": "write", "statuses": "write"}},
        )
        self.assertNotIn("contents", json.loads(token_request["body"])["permissions"])
        self.assertEqual(token_request["version"], "2022-11-28")
        self.assertEqual(token_request["agent"], "rookrunner")
        self.assertEqual(token_request["accept"], "application/vnd.github+json")
        self.assertEqual(check_request["authorization"], "Bearer " + TOKEN)
        self.assertNotIn(jwt, check_request["authorization"])
        body = json.loads(check_request["body"])
        self.assertEqual(body["name"], CONTEXT)
        self.assertEqual(body["head_sha"], HEAD)
        self.assertEqual(body["external_id"], run["run_id"])
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["conclusion"], "success")
        self.assertEqual(body["output"], {"title": CONTEXT, "summary": "succeeded exit_code 0"})
        self.assertIn("completed_at", body)
        self.assertNotIn("started_at", body)
        self.assertNotIn("details_url", body)
        self.assertNotIn("actions", body)
        self.assertNotIn("text", body["output"])
        self.assertEqual(status_request["authorization"], "Bearer " + TOKEN)
        self.assertEqual(
            json.loads(status_request["body"]), {"context": CONTEXT, "state": "success"}
        )
        with sqlite3.connect(self.state / "runs.sqlite3") as connection:
            stored_id = connection.execute(
                "SELECT check_run_id, status, conclusion FROM check_posts"
            ).fetchone()
        self.assertEqual(stored_id, (CHECK_ID, "completed", "success"))
        validate_response("run.status", json.loads(result.stdout))
        self._assert_secret_absent(result, jwt)

    def test_a_queued_check_omits_conclusion_and_uses_the_clock(self):
        moment = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            posted = post_check_flow(
                api_base=f"http://127.0.0.1:{self.server.server_address[1]}",
                repository="moonbase2090/rookrunner",
                sha=HEAD,
                context=CONTEXT,
                run_id="run-queued",
                check_status="queued",
                check_conclusion=None,
                check_summary_text="queued",
                check_run_id=None,
                status_state="pending",
                post_status_request=True,
                app_key="~/Secrets/github-app/rookrunner-app/private-key.pem",
                state_dir=self.state,
                repository_root=self.repo,
                clock=moment,
            )
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous
        self.assertEqual(posted.check_id, CHECK_ID)
        self.assertTrue(posted.status_posted)
        self.assertIsNone(posted.error)
        body = json.loads(self.server.requests[1]["body"])
        self.assertEqual(body["status"], "queued")
        self.assertNotIn("conclusion", body)
        self.assertNotIn("started_at", body)
        self.assertEqual(body["head_sha"], HEAD)
        self.assertEqual(body["output"]["summary"], "queued")
        payload = json.loads(_b64decode(self.server.requests[0]["authorization"].split(".", 2)[1]))
        self.assertEqual(payload["iat"], int(moment.timestamp()) - 60)
        self.assertEqual(payload["exp"], payload["iat"] + 600)

    def test_a_later_post_patches_the_stored_id(self):
        # A queued row is claimed by the worker scheduler. `running` is not.
        run = self._insert(state="running", exit_code=None)
        first = self._cli(run["run_id"])
        self.assertEqual(first.returncode, 0, first.stderr)
        created = json.loads(self.server.requests[1]["body"])
        self.assertEqual(created["status"], "in_progress")
        self.assertNotIn("conclusion", created)
        self.assertRegex(created["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(created["head_sha"], HEAD)
        self.assertEqual(json.loads(self.server.requests[2]["body"])["state"], "pending")
        with sqlite3.connect(self.state / "runs.sqlite3", timeout=5) as connection:
            row = connection.execute(
                "SELECT record FROM runs WHERE id=?", (run["run_id"],)
            ).fetchone()
            record = json.loads(row[0])
            record["state"] = "running"
            record["exit_code"] = None
            connection.execute(
                "UPDATE runs SET record=? WHERE id=?", (canonical(record), run["run_id"])
            )
        self.server.requests.clear()
        second = self._cli(run["run_id"])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.server.requests[1]["method"], "PATCH")
        self.assertEqual(
            self.server.requests[1]["path"],
            f"/repos/moonbase2090/rookrunner/check-runs/{CHECK_ID}",
        )
        updated = json.loads(self.server.requests[1]["body"])
        self.assertEqual(updated["status"], "in_progress")
        self.assertNotIn("head_sha", updated)
        self.assertNotIn("conclusion", updated)
        self.assertRegex(updated["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(json.loads(self.server.requests[2]["body"])["state"], "pending")
        with sqlite3.connect(self.state / "runs.sqlite3", timeout=5) as connection:
            row = connection.execute(
                "SELECT record FROM runs WHERE id=?", (run["run_id"],)
            ).fetchone()
            record = json.loads(row[0])
            record["state"] = "succeeded"
            record["exit_code"] = 0
            connection.execute(
                "UPDATE runs SET record=? WHERE id=?", (canonical(record), run["run_id"])
            )
        self.server.requests.clear()
        third = self._cli(run["run_id"])
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads(self.server.requests[1]["body"])["conclusion"], "success")
        os.chmod(self.key, 0o000)
        try:
            self.server.requests.clear()
            again = self._cli(run["run_id"])
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["result"]["action"], "skip")
            self.assertEqual(self.server.requests, [])
        finally:
            os.chmod(self.key, 0o600)

    def test_a_gate_failure_reads_no_key_and_sends_nothing(self):
        os.chmod(self.key, 0o000)
        try:
            dirty = self._insert(dirty=True, state="failed", exit_code=1)
            result = self._cli(dirty["run_id"])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"]["data"]["kind"], "STATUS_REFUSED")
            self.assertEqual(self.server.requests, [])
            self.assertNotIn("APP_KEY", result.stdout)
        finally:
            os.chmod(self.key, 0o600)

    def test_a_refused_key_sends_nothing(self):
        outside = self.root / "private-key.pem"
        outside.write_bytes(self.pem_bytes)
        os.chmod(outside, 0o600)
        link = self.home / "Secrets" / "github-app" / "rookrunner-app" / "linked.pem"
        link.symlink_to(self.key)
        renamed = self.key.with_name("moved.pem")
        self.key.rename(renamed)
        (self.key.parent / "private-key.pem").symlink_to(renamed)
        inside_repo = self.repo / "private-key.pem"
        inside_repo.write_bytes(self.pem_bytes)
        inside_state = self.state / "private-key.pem"
        inside_state.write_bytes(self.pem_bytes)
        workspace = self.state / "attempts" / str(uuid.uuid4()) / "workspace"
        workspace.mkdir(parents=True)
        inside_attempt = workspace / "private-key.pem"
        inside_attempt.write_bytes(self.pem_bytes)
        run = self._insert()
        cases = (
            outside,
            self.key,
            inside_repo,
            inside_state,
            inside_attempt,
            Path("private-key.pem"),
        )
        for path in cases:
            with self.subTest(path=path.name):
                result = self._cli(run["run_id"], key=path)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.server.requests, [])
                payload = json.loads(result.stderr)
                self.assertEqual(payload["error"]["kind"], "APP_KEY_UNREADABLE")
                self.assertNotIn(str(path), result.stderr)
                self.assertNotIn(self.pem_bytes.decode(), result.stderr)

    def test_a_group_readable_key_and_a_bad_pem_send_nothing(self):
        run = self._insert()
        os.chmod(self.key, 0o640)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.requests, [])
        os.chmod(self.key, 0o600)
        os.chmod(self.key.parent, 0o755)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.requests, [])
        os.chmod(self.key.parent, 0o700)
        self.key.write_bytes(b"not a pem\n")
        os.chmod(self.key, 0o600)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(json.loads(result.stderr)["error"]["kind"], "APP_KEY_UNREADABLE")
        self.assertNotIn("not a pem", result.stderr)
        (self.key.parent / "client-id").write_text("two words\n")
        self.key.write_bytes(self.pem_bytes)
        os.chmod(self.key, 0o600)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.requests, [])
        self.assertIn("installation", result.stderr)

    def test_both_credentials_refuse_before_http(self):
        run = self._insert()
        result = self._cli(run["run_id"], both=True, credential=self.root / "token")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(json.loads(result.stderr)["error"]["kind"], "INVALID_PARAMS")
        self.assertNotIn(str(self.key), result.stderr)

    def test_token_exchange_401_and_403_send_no_check_and_no_status(self):
        run = self._insert()
        for code in (401, 403, 404):
            with self.subTest(code=code):
                self.server.routes = _routes(token_code=code)
                self.server.requests.clear()
                result = self._cli(run["run_id"])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(len(self.server.requests), 1)
                self.assertNotIn("check-runs", self.server.requests[0]["path"])
                payload = json.loads(result.stderr)
                self.assertEqual(payload["error"]["kind"], "TOKEN_REJECTED")
                self.assertIs(payload["error"]["retryable"], False)
                self.assertIn(str(code), payload["error"]["message"])
                self._assert_secret_absent(result)
                with sqlite3.connect(self.state / "runs.sqlite3") as connection:
                    record = json.loads(
                        connection.execute(
                            "SELECT record FROM runs WHERE id=?", (run["run_id"],)
                        ).fetchone()[0]
                    )
                    checks = connection.execute("SELECT count(*) FROM check_posts").fetchone()[0]
                    statuses = connection.execute("SELECT count(*) FROM status_posts").fetchone()[0]
                self.assertEqual(record["state"], "succeeded")
                self.assertEqual(checks, 0)
                self.assertEqual(statuses, 0)

    def test_a_rate_limit_is_retryable_and_is_not_retried(self):
        run = self._insert()
        self.server.routes = _routes(token_code=429)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["kind"], "RATE_LIMITED")
        self.assertIs(payload["error"]["retryable"], True)
        self.server.routes = _routes(token_code=403, token_headers=[("x-ratelimit-remaining", "0")])
        self.server.requests.clear()
        result = self._cli(run["run_id"])
        self.assertEqual(len(self.server.requests), 1)
        self.assertIs(json.loads(result.stderr)["error"]["retryable"], True)
        self.server.routes = _routes(check_code=429)
        self.server.requests.clear()
        result = self._cli(run["run_id"])
        self.assertEqual(
            [item["path"].rsplit("/", 1)[-1] for item in self.server.requests],
            [
                "access_tokens",
                "check-runs",
            ],
        )
        self.assertIs(json.loads(result.stderr)["error"]["retryable"], True)
        with sqlite3.connect(self.state / "runs.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM check_posts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM status_posts").fetchone()[0], 0
            )

    def test_a_failed_check_still_posts_the_status(self):
        run = self._insert(state="cancelled", exit_code=None)
        self.server.routes = _routes(check_code=500)
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn("/statuses/", self.server.requests[2]["path"])
        self.assertEqual(json.loads(self.server.requests[2]["body"])["state"], "error")
        self.assertEqual(json.loads(result.stderr)["error"]["kind"], "CHECK_REJECTED")
        self.assertIs(json.loads(result.stderr)["error"]["retryable"], False)
        with sqlite3.connect(self.state / "runs.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM check_posts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT state FROM status_posts").fetchone()[0], "error"
            )
            record = json.loads(
                connection.execute(
                    "SELECT record FROM runs WHERE id=?", (run["run_id"],)
                ).fetchone()[0]
            )
        self.assertEqual(record["state"], "cancelled")
        self._assert_secret_absent(result)

    def test_a_redirect_is_not_followed(self):
        run = self._insert()
        self.server.routes = _routes(
            token_code=302,
            token_headers=[("Location", "http://127.0.0.1/elsewhere")],
        )
        result = self._cli(run["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn("HTTP 302", result.stderr)
        self.assertNotIn("elsewhere", result.stderr)

    def test_cancelled_maps_to_cancelled_and_lost_maps_to_failure(self):
        cancelled = self._insert(state="cancelled", exit_code=None)
        result = self._cli(cancelled["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(self.server.requests[1]["body"])
        self.assertEqual(body["conclusion"], "cancelled")
        self.assertEqual(json.loads(self.server.requests[2]["body"])["state"], "error")
        lost = self._insert(state="lost", exit_code=None)
        self.server.requests.clear()
        result = self._cli(lost["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(self.server.requests[1]["body"])
        self.assertEqual(body["conclusion"], "failure")
        self.assertNotEqual(body["conclusion"], "success")
