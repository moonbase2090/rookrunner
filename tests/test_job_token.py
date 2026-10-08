"""Job-token mint, delivery, and revocation against a local HTTP stub.

No test reads ~/Secrets/github-app/rookrunner-app/ and no test contacts
api.github.com. The fixture token is compared by hash so a failure does
not print it.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from execution_core.checks import _exchange, sign_app_jwt
from execution_core.expr import ExprError, rewrite_run_secrets
from execution_core.job_token import (
    TOKEN_EXPIRY_WARNING,
    TOKEN_WARNING_AFTER_SECONDS,
    JobTokenConfig,
    TokenExpiry,
    contents_read_granted,
    finish_job_token,
    job_needs_token,
    mint_job_token,
    public_token_revocation,
    resolved_permissions,
)
from execution_core.plan import CAPABILITY_VERSION, PlanError, plan_workflow
from execution_core.protocol import canonical
from execution_core.run import _JobRuntime, _prepare_job_token, _render_run, _render_secret_or_expr
from execution_core.status import StatusError
from execution_core.worker import Worker
from schema_support import ROOT, SCHEMA, validator
from test_run import _capture


JOB_TOKEN = "ghs_jobfixturetokenvalue"
CLIENT = "Iv1.fixtureclient"
INSTALL = "424242"
POST_TOKEN = "ghs_postfixturetokenvalue"
TOKEN_DIGEST = hashlib.sha256(JOB_TOKEN.encode()).hexdigest()


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _absent(test, blob, secret):
    """Fail when `secret` occurs in `blob` without printing `secret`."""

    test.assertEqual(blob.find(secret), -1)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._take("POST")

    def do_DELETE(self):
        self._take("DELETE")

    def do_GET(self):
        self._take("GET")

    def _take(self, method):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        code, headers, payload = self.server.reply(method, self.path)
        self.send_response(code)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return


class _Stub:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.reply = self._reply
        self.token_code = 201
        self.delete_code = 204
        self.redirect = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def requests(self):
        return self.server.requests

    @property
    def origin(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _reply(self, method, path):
        if self.redirect:
            return 302, [("Location", self.origin + "/stolen")], b""
        if method == "DELETE":
            payload = b"" if self.delete_code == 204 else b"no"
            return self.delete_code, [], payload
        if path.endswith("/access_tokens") and self.token_code == 201:
            payload = json.dumps(
                {"token": JOB_TOKEN, "expires_at": "2026-10-05T00:00:00Z"}
            ).encode()
            return 201, [], payload
        return self.token_code, [], b""

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class _Home(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="job-token-key-")
        cls.pem_path = Path(cls.keys.name) / "private-key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(cls.pem_path), "2048"],
            check=True,
            capture_output=True,
        )
        cls.pem_bytes = cls.pem_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="job-token-")
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.key = self._install()
        self.previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.stub = _Stub()

    def tearDown(self):
        self.stub.close()
        if self.previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous
        self.tmp.cleanup()

    def _install(self):
        directory = self.home / "Secrets" / "github-app" / "rookrunner-app"
        directory.mkdir(parents=True)
        os.chmod(directory, 0o700)
        key = directory / "private-key.pem"
        key.write_bytes(self.pem_bytes)
        os.chmod(key, 0o600)
        client = directory / "client-id"
        client.write_text(CLIENT + "\n")
        os.chmod(client, 0o600)
        install = directory / "installation-id"
        install.write_text(INSTALL + "\n")
        os.chmod(install, 0o600)
        return key

    def _config(self, **overrides):
        values = {
            "app_key": str(self.key),
            "api_base": self.stub.origin,
            "state_dir": self.state,
            "repository_root": self.repo,
            "github_repository": "owner/demo",
        }
        values.update(overrides)
        return JobTokenConfig(
            values["app_key"],
            values["api_base"],
            state_dir=values["state_dir"],
            repository_root=values["repository_root"],
            github_repository=values["github_repository"],
        )


class PermissionTests(unittest.TestCase):
    def test_contents_read_is_the_only_mint_grant(self):
        self.assertTrue(contents_read_granted(None))
        self.assertTrue(contents_read_granted("read-all"))
        self.assertTrue(contents_read_granted({"contents": "read"}))
        self.assertTrue(contents_read_granted({"contents": "read", "checks": "read"}))
        self.assertFalse(contents_read_granted({}))
        self.assertFalse(contents_read_granted({"contents": "none"}))
        self.assertFalse(contents_read_granted({"metadata": "read"}))
        self.assertFalse(contents_read_granted({"contents": "write"}))

    def test_job_permissions_replace_workflow_permissions(self):
        job = {"permissions": {}}
        workflow = {"permissions": {"contents": "read"}}
        self.assertEqual(resolved_permissions(job, workflow), {})
        self.assertEqual(resolved_permissions({"id": "build"}, workflow), {"contents": "read"})
        self.assertEqual(resolved_permissions({"id": "build"}, {"name": "demo"}), None)
        self.assertEqual(
            resolved_permissions({"permissions": "read-all"}, {"permissions": {}}),
            "read-all",
        )

    def test_a_step_needs_a_token_only_from_env_with_run_or_an_omitted_default(self):
        asked = {"steps": [{"env": {"ASKED": "${{ secrets.GITHUB_TOKEN }}"}, "run": "echo hi"}]}
        self.assertTrue(job_needs_token(asked))
        self.assertTrue(job_needs_token({"steps": [{"run": 'token="${{ github.token }}"'}]}))
        self.assertFalse(
            job_needs_token(
                {
                    "steps": [
                        {
                            "if": "github.token == ''",
                            "name": "${{ github.token }}",
                            "run": "echo hi",
                        }
                    ]
                }
            )
        )
        omitted = {
            "steps": [
                {
                    "with": {},
                    "inputs": {"token": {"default": "${{ github.token }}"}},
                    "run": "echo hi",
                }
            ]
        }
        self.assertTrue(job_needs_token(omitted))
        supplied = {
            "steps": [
                {
                    "with": {"token": "literal"},
                    "inputs": {"token": {"default": "${{ github.token }}"}},
                    "run": "echo hi",
                }
            ]
        }
        self.assertFalse(job_needs_token(supplied))


class MintTests(_Home):
    def test_the_job_body_names_the_repository_and_contents_read(self):
        token = mint_job_token(self._config())
        self.assertEqual(_digest(token), TOKEN_DIGEST)
        self.assertEqual(len(self.stub.requests), 1)
        request = self.stub.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["path"].endswith(f"/app/installations/{INSTALL}/access_tokens"))
        self.assertEqual(
            json.loads(request["body"]),
            {"repositories": ["owner/demo"], "permissions": {"contents": "read"}},
        )
        self.assertEqual(canonical(json.loads(request["body"])), request["body"].decode())

    def test_the_post_body_stays_checks_and_statuses_without_repositories(self):
        moment = datetime.now(UTC)
        jwt = sign_app_jwt(self.pem_bytes, CLIENT, moment)
        self.stub.server.reply = lambda method, path: (
            201,
            [],
            json.dumps({"token": POST_TOKEN}).encode(),
        )
        token = _exchange(self.stub.origin, INSTALL, jwt)
        self.assertEqual(_digest(token), _digest(POST_TOKEN))
        body = json.loads(self.stub.requests[0]["body"])
        self.assertEqual(body, {"permissions": {"checks": "write", "statuses": "write"}})
        self.assertNotIn("repositories", body)

    def test_prepare_mints_once_and_registers_the_mask(self):
        runtime = _JobRuntime()
        runtime.token_expiry = TokenExpiry()
        job = {
            "id": "build",
            "steps": [{"env": {"ASKED": "${{ secrets.GITHUB_TOKEN }}"}, "run": "echo hi"}],
        }
        notes = []
        ready = _prepare_job_token(runtime, self._config(), job, {}, lambda: 10, notes.append)
        self.assertTrue(ready)
        self.assertEqual(notes, [])
        self.assertEqual(_digest(runtime.job_token), TOKEN_DIGEST)
        self.assertEqual(runtime.token_minted_at, 10)
        self.assertTrue(any(_digest(item) == TOKEN_DIGEST for item in runtime.masks.secrets))
        self.assertEqual(len(self.stub.requests), 1)

    def test_permissions_empty_mints_nothing_and_does_not_read_the_key(self):
        os.chmod(self.key, 0)
        runtime = _JobRuntime()
        job = {
            "id": "build",
            "permissions": {},
            "steps": [{"run": "echo ${{ secrets.GITHUB_TOKEN }}"}],
        }
        notes = []
        ready = _prepare_job_token(
            runtime,
            self._config(),
            job,
            {"permissions": {"contents": "read"}},
            lambda: 0,
            notes.append,
        )
        self.assertTrue(ready)
        self.assertEqual(notes, [])
        self.assertIsNone(getattr(runtime, "job_token", None))
        self.assertEqual(self.stub.requests, [])

    def test_a_job_that_never_asks_does_not_read_the_key(self):
        os.chmod(self.key, 0)
        runtime = _JobRuntime()
        notes = []
        ready = _prepare_job_token(
            runtime,
            self._config(),
            {"id": "build", "steps": [{"if": "github.token == ''", "run": "echo hi"}]},
            {},
            lambda: 0,
            notes.append,
        )
        self.assertTrue(ready)
        self.assertEqual(notes, [])
        self.assertEqual(self.stub.requests, [])

    def test_mint_http_401_names_the_status_and_not_the_secret(self):
        self.stub.token_code = 401
        with self.assertRaises(StatusError) as caught:
            mint_job_token(self._config())
        self.assertEqual(caught.exception.kind, "TOKEN_REJECTED")
        self.assertIn("HTTP 401", str(caught.exception))
        _absent(self, str(caught.exception), JOB_TOKEN)
        self.assertNotIn("private-key", str(caught.exception))
        self.assertNotIn(str(self.home), str(caught.exception))

    def test_a_bad_repository_is_refused_before_the_key_is_read(self):
        os.chmod(self.key, 0)
        with self.assertRaises(StatusError) as caught:
            mint_job_token(self._config(github_repository="demo"))
        self.assertIn("repository must be owner/name", str(caught.exception))
        self.assertEqual(self.stub.requests, [])
        self.assertNotIn(str(self.home), str(caught.exception))

    def test_a_key_outside_the_allowed_directory_sends_no_request(self):
        foreign = self.root / "elsewhere" / "private-key.pem"
        foreign.parent.mkdir()
        foreign.write_bytes(self.pem_bytes)
        os.chmod(foreign, 0o600)
        with self.assertRaises(StatusError) as caught:
            mint_job_token(self._config(app_key=str(foreign)))
        self.assertEqual(self.stub.requests, [])
        self.assertNotIn(str(foreign), str(caught.exception))
        _absent(self, str(caught.exception), JOB_TOKEN)

    def test_a_key_inside_the_state_directory_sends_no_request(self):
        with self.assertRaises(StatusError):
            mint_job_token(self._config(state_dir=self.home / "Secrets"))
        self.assertEqual(self.stub.requests, [])

    def test_a_redirect_is_not_followed(self):
        self.stub.redirect = True
        with self.assertRaises(StatusError) as caught:
            mint_job_token(self._config())
        self.assertEqual(caught.exception.kind, "TOKEN_REJECTED")
        self.assertIn("HTTP 302", str(caught.exception))
        self.assertEqual(len(self.stub.requests), 1)
        self.assertNotEqual(self.stub.requests[0]["path"], "/stolen")
        _absent(self, str(caught.exception), JOB_TOKEN)
        self.assertNotIn("private-key", str(caught.exception))
        self.assertNotIn(str(self.home), str(caught.exception))


class RevokeTests(_Home):
    def _runtime(self):
        runtime = _JobRuntime()
        runtime.job_token = JOB_TOKEN
        return runtime

    def test_delete_204_is_accepted_and_the_record_has_no_token(self):
        runtime = self._runtime()
        records = []
        finish_job_token(runtime, "build", records, self._config())
        self.assertIsNone(runtime.job_token)
        self.assertEqual(records, [{"job_id": "build", "attempted": True, "accepted": True}])
        request = self.stub.requests[0]
        self.assertEqual(request["method"], "DELETE")
        self.assertEqual(request["path"], "/installation/token")
        self.assertEqual(_digest(request["authorization"]), _digest("Bearer " + JOB_TOKEN))
        _absent(self, json.dumps(records), JOB_TOKEN)

    def test_delete_500_is_not_accepted_and_does_not_raise(self):
        self.stub.delete_code = 500
        runtime = self._runtime()
        records = []
        finish_job_token(runtime, "build", records, self._config())
        self.assertEqual(records, [{"job_id": "build", "attempted": True, "accepted": False}])
        self.assertIsNone(runtime.job_token)

    def test_a_job_that_minted_nothing_records_that_revocation_was_not_needed(self):
        records = []
        finish_job_token(_JobRuntime(), "build", records, self._config())
        self.assertEqual(records, [{"job_id": "build", "attempted": False}])
        self.assertEqual(self.stub.requests, [])

    def test_the_stored_record_drops_a_token_field(self):
        stored = public_token_revocation(
            [
                {"job_id": "build", "attempted": True, "accepted": True, "token": JOB_TOKEN},
                {"job_id": "build", "attempted": False, "accepted": True, "token": JOB_TOKEN},
            ]
        )
        self.assertEqual(stored[0], {"job_id": "build", "attempted": True, "accepted": True})
        self.assertEqual(stored[1], {"job_id": "build", "attempted": False})
        _absent(self, json.dumps(stored), JOB_TOKEN)


class ExpiryTests(unittest.TestCase):
    def test_one_warning_at_55_minutes_and_the_text_has_no_token(self):
        self.assertEqual(TOKEN_WARNING_AFTER_SECONDS, 55 * 60)
        expiry = TokenExpiry()
        self.assertIsNone(expiry.observe(0, 55 * 60 - 1))
        self.assertEqual(
            expiry.observe(0, 55 * 60),
            "job token expires one hour after mint",
        )
        self.assertIsNone(expiry.observe(0, 55 * 60 + 30))
        self.assertEqual(TOKEN_EXPIRY_WARNING, "job token expires one hour after mint")
        self.assertNotIn("ghs_", TOKEN_EXPIRY_WARNING)


class RewriteTests(unittest.TestCase):
    def test_exact_github_token_rewrites_and_is_not_a_file(self):
        self.assertEqual(
            rewrite_run_secrets("echo ${{ github.token }}", None),
            ("echo ${RR_SECRET_GITHUB_TOKEN}", []),
        )
        self.assertEqual(
            rewrite_run_secrets('echo "${{ github.token }}-x"', None),
            ('echo "${RR_SECRET_GITHUB_TOKEN}-x"', []),
        )

    def test_a_non_exact_github_token_read_is_refused(self):
        with self.assertRaises(ExprError) as caught:
            rewrite_run_secrets("echo ${{ github.token || 'x' }}", None)
        self.assertEqual(str(caught.exception), "github.token reference is not accepted")

    def test_github_token_does_not_require_the_secret_store(self):
        runtime = _JobRuntime()
        text, env = _render_run("echo ${{ github.token }}", {}, None, runtime)
        self.assertEqual(text, "echo ${RR_SECRET_GITHUB_TOKEN}")
        self.assertEqual(env, {})
        self.assertEqual(_render_secret_or_expr("${{ secrets.GITHUB_TOKEN }}", {}, runtime), "")
        self.assertEqual(_render_secret_or_expr("${{ github.token }}", {}, runtime), "")
        runtime.job_token = JOB_TOKEN
        text, env = _render_run("echo ${{ github.token }}", {}, None, runtime)
        self.assertEqual(text, "echo ${RR_SECRET_GITHUB_TOKEN}")
        self.assertEqual(list(env), ["GITHUB_TOKEN"])
        self.assertEqual(_digest(env["GITHUB_TOKEN"]), TOKEN_DIGEST)
        self.assertEqual(
            _digest(_render_secret_or_expr("${{ secrets.GITHUB_TOKEN }}", {}, runtime)),
            TOKEN_DIGEST,
        )

    def test_a_file_secret_mixed_with_the_token_still_withholds(self):
        runtime = _JobRuntime()
        with self.assertRaises(ExprError) as caught:
            _render_run("echo ${{ secrets.API }} ${{ github.token }}", {}, None, runtime)
        self.assertEqual(str(caught.exception), "context is not available: secrets")


class PlanTests(unittest.TestCase):
    def _action(self, root, default):
        path = root / "show" / "action.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "name: show\n"
            "description: show\n"
            "inputs:\n"
            "  token:\n"
            "    description: token\n"
            f"    default: {default}\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      run: echo hi\n"
        )
        return "on: push\njobs:\n  build:\n    steps:\n      - uses: ./show\n"

    def test_exact_token_defaults_are_accepted(self):
        root = Path(tempfile.mkdtemp(prefix="job-token-plan-"))
        self.addCleanup(lambda: shutil.rmtree(root))
        workflow = self._action(root, "${{ github.token }}")
        planned = plan_workflow(workflow.encode(), "build", action_root=root)
        default = planned["plan"]["job"]["steps"][0]["inputs"]["token"]["default"]
        self.assertEqual(default, "${{ github.token }}")
        workflow = self._action(root, "${{ secrets.GITHUB_TOKEN }}")
        planned = plan_workflow(workflow.encode(), "build", action_root=root)
        default = planned["plan"]["job"]["steps"][0]["inputs"]["token"]["default"]
        self.assertEqual(default, "${{ secrets.GITHUB_TOKEN }}")

    def test_another_secret_in_a_default_names_the_input(self):
        root = Path(tempfile.mkdtemp(prefix="job-token-plan-"))
        self.addCleanup(lambda: shutil.rmtree(root))
        workflow = self._action(root, "${{ secrets.API }}")
        with self.assertRaises(PlanError) as caught:
            plan_workflow(workflow.encode(), "build", action_root=root)
        self.assertEqual(caught.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("token", caught.exception.field)
        self.assertIn("default", caught.exception.field)
        self.assertIn("secrets reference is not accepted", str(caught.exception))

    def test_write_scopes_still_fail_planning(self):
        for scope in ("security-events", "actions"):
            workflow = (
                f"permissions:\n  {scope}: write\n"
                "on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
            )
            with self.assertRaises(PlanError) as caught:
                plan_workflow(workflow.encode(), "build")
            self.assertEqual(caught.exception.kind, "CAPABILITY_UNSUPPORTED")
            self.assertIn(scope, caught.exception.field)

    def test_check_yml_and_the_capability_version_stay_put(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        text = (ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
        self.assertNotIn("--app-key", text)
        self.assertNotIn("github-app", text)
        self.assertNotIn("private-key", text)
        self.assertNotIn("GITHUB_TOKEN", text)


class SchemaTests(unittest.TestCase):
    def test_token_revocation_is_optional_and_cannot_store_a_token(self):
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        success = next(item["value"] for item in examples if item["name"] == "succeeded run")
        self.assertNotIn("token_revocation", SCHEMA["$defs"]["Run"]["required"])
        validator("Run").validate(success)
        added = json.loads(json.dumps(success))
        added["token_revocation"] = [
            {"job_id": "build", "attempted": True, "accepted": True},
            {"job_id": "build", "attempted": False},
        ]
        validator("Run").validate(added)
        leaked = json.loads(json.dumps(added))
        leaked["token_revocation"][0]["token"] = JOB_TOKEN
        self.assertFalse(validator("Run").is_valid(leaked))
        extra = json.loads(json.dumps(added))
        extra["token_revocation"][1]["accepted"] = False
        self.assertFalse(validator("Run").is_valid(extra))


class WorkerConfigTests(_Home):
    def test_the_worker_passes_the_api_base_only_when_the_key_flag_is_set(self):
        worker = Worker(
            self.repo,
            self.state,
            app_key=str(self.key),
            github_repository="owner/demo",
            api_base=self.stub.origin,
        )
        config = worker.job_token_config()
        self.assertEqual(config.api_base, self.stub.origin)
        self.assertEqual(config.github_repository, "owner/demo")
        self.assertEqual(config.app_key, str(self.key))
        plain = Worker(self.repo, self.state)
        self.assertIsNone(plain.job_token_config())
        self.assertEqual(self.stub.requests, [])


class DockerJobTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", "rookrunner-ns4-fixture"],
            capture_output=True,
            text=True,
        )
        cls.built = inspected.returncode != 0
        if cls.built:
            cls.image_root = tempfile.TemporaryDirectory(prefix="job-token-image-")
            root = Path(cls.image_root.name)
            (root / "Dockerfile").write_text("FROM python:3.12-slim\n")
            subprocess.run(
                ["docker", "build", "--quiet", "-t", "rookrunner-ns4-fixture", str(root)],
                check=True,
                capture_output=True,
            )
            inspected = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", "rookrunner-ns4-fixture"],
                check=True,
                capture_output=True,
                text=True,
            )
        cls.image = inspected.stdout.strip()

    @classmethod
    def tearDownClass(cls):
        if cls.built:
            subprocess.run(["docker", "rmi", "-f", "rookrunner-ns4-fixture"], capture_output=True)
            cls.image_root.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="job-token-docker-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self._keep_docker_config()
        self.previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.pem = Path(tempfile.mkdtemp(prefix="job-token-pem-")) / "private-key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(self.pem), "2048"],
            check=True,
            capture_output=True,
        )
        directory = self.home / "Secrets" / "github-app" / "rookrunner-app"
        directory.mkdir(parents=True)
        os.chmod(directory, 0o700)
        key = directory / "private-key.pem"
        key.write_bytes(self.pem.read_bytes())
        os.chmod(key, 0o600)
        for name, text in (("client-id", CLIENT), ("installation-id", INSTALL)):
            path = directory / name
            path.write_text(text + "\n")
            os.chmod(path, 0o600)
        self.key = key
        self.stub = _Stub()
        self.addCleanup(self._restore)

    def _keep_docker_config(self):
        """Keep the Docker context when HOME points at the fixture key.

        The Docker CLI reads its context from the home directory. This
        test changes HOME. An already set DOCKER_CONFIG is left alone.
        """

        if os.environ.get("DOCKER_CONFIG"):
            return
        config = Path.home() / ".docker"
        if not config.is_dir():
            return
        os.environ["DOCKER_CONFIG"] = str(config)
        self.addCleanup(lambda: os.environ.pop("DOCKER_CONFIG", None))

    def _restore(self):
        self.stub.close()
        if self.previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            capture_output=True,
            text=True,
        )
        for container in names.stdout.split():
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    def _config(self, state, repo):
        return JobTokenConfig(
            str(self.key),
            self.stub.origin,
            state_dir=state,
            repository_root=repo,
            github_repository="owner/demo",
        )

    def _event(self):
        return {
            "ref": "refs/pull/7/merge",
            "repository": {"full_name": "owner/demo", "default_branch": "main", "id": 9},
            "pull_request": {
                "number": 7,
                "head": {"repo": {"id": 9}},
                "base": {"repo": {"id": 9}},
            },
        }

    def _run(self, name, workflow, extra=None):
        from execution_core.plan import plan_snapshot
        from execution_core.run import run_job

        root = self.root / name
        root.mkdir()
        repo, snapshot, digest, workspace = _capture(root, workflow, extra)
        plan = plan_snapshot(snapshot, "build")["plan"]
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan,
            self.image,
            self._event(),
            event_name="pull_request",
            secrets=None,
            job_token=self._config(root / "state", repo),
        )
        return result, plan, workspace, snapshot

    def _stored(self, result, plan, workspace, snapshot):
        chunks = [json.dumps(result), json.dumps(plan)]
        for tree in (workspace, snapshot):
            for path in tree.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)

    def test_a_pull_request_receives_the_token_and_revokes_it(self):
        action = """\
name: ask
description: ask
inputs:
  token:
    description: token
    default: ${{ github.token }}
runs:
  using: composite
  steps:
    - shell: bash
      env:
        ASKED: ${{ secrets.GITHUB_TOKEN }}
        FROM_DEFAULT: ${{ inputs.token }}
      run: |
        token="${{ github.token }}"
        cp /run/rookrunner-cmd/step-0-script "$GITHUB_WORKSPACE/script.txt"
        python3 - <<'PY'
        import hashlib, os
        root = os.environ["GITHUB_WORKSPACE"]
        for name, value in (("asked", os.environ.get("ASKED", "")), ("default", os.environ.get("FROM_DEFAULT", "")), ("run", os.environ.get("RR_SECRET_GITHUB_TOKEN", ""))):
            open(root + "/" + name + ".sha256", "w").write(hashlib.sha256(value.encode()).hexdigest())
        print(os.environ.get("ASKED", ""))
        PY
"""
        workflow = """\
on: pull_request
jobs:
  build:
    steps:
      - uses: ./ask
      - run: |
          cp /run/rookrunner-cmd/step-1-script "$GITHUB_WORKSPACE/second.txt"
          python3 - <<'PY'
          import os
          flag = "set" if os.environ.get("GITHUB_TOKEN") else "unset"
          open(os.environ["GITHUB_WORKSPACE"] + "/job-token.txt", "w").write(flag)
          print(os.environ.get("GITHUB_TOKEN", ""))
          PY
"""
        result, plan, workspace, snapshot = self._run("mint", workflow, {"ask/action.yml": action})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        for name in ("asked", "default", "run"):
            self.assertEqual((workspace / f"{name}.sha256").read_text(), TOKEN_DIGEST)
        script = (workspace / "script.txt").read_text()
        self.assertIn("${RR_SECRET_GITHUB_TOKEN}", script)
        _absent(self, script, JOB_TOKEN)
        second = (workspace / "second.txt").read_text()
        self.assertNotIn("secrets.", second)
        self.assertNotIn("github.token", second)
        _absent(self, second, JOB_TOKEN)
        self.assertEqual((workspace / "job-token.txt").read_text(), "set")
        logged = "\n".join(
            (step.get("stdout") or "") + (step.get("stderr") or "") for step in result["steps"]
        )
        self.assertIn("***", logged)
        _absent(self, logged, JOB_TOKEN)
        _absent(self, self._stored(result, plan, workspace, snapshot), JOB_TOKEN)
        posts = [item for item in self.stub.requests if item["method"] == "POST"]
        deletes = [item for item in self.stub.requests if item["method"] == "DELETE"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            json.loads(posts[0]["body"]),
            {"repositories": ["owner/demo"], "permissions": {"contents": "read"}},
        )
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["path"], "/installation/token")
        self.assertEqual(_digest(deletes[0]["authorization"]), _digest("Bearer " + JOB_TOKEN))
        self.assertEqual(
            result["token_revocation"],
            [{"job_id": "build", "attempted": True, "accepted": True}],
        )

    def test_empty_permissions_mint_nothing(self):
        workflow = """\
permissions: {}
on: pull_request
jobs:
  build:
    steps:
      - env:
          ASKED: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python3 - <<'PY'
          import os
          asked = os.environ.get("ASKED", "missing")
          flag = "set" if os.environ.get("GITHUB_TOKEN") else "unset"
          root = os.environ["GITHUB_WORKSPACE"]
          open(root + "/asked.txt", "w").write("empty" if asked == "" else "other")
          open(root + "/flag.txt", "w").write(flag)
          PY
"""
        result, _plan, workspace, _snapshot = self._run("empty", workflow)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "asked.txt").read_text(), "empty")
        self.assertEqual((workspace / "flag.txt").read_text(), "unset")
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(result["token_revocation"], [{"job_id": "build", "attempted": False}])

    def test_a_mint_failure_stops_before_the_first_step(self):
        self.stub.token_code = 401
        workflow = """\
on: pull_request
jobs:
  build:
    steps:
      - env:
          ASKED: ${{ secrets.GITHUB_TOKEN }}
        run: printf ran > "$GITHUB_WORKSPACE/ran.txt"
"""
        result, _plan, workspace, _snapshot = self._run("reject", workflow)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((workspace / "ran.txt").exists())
        error = result["steps"][0]["error"] or ""
        self.assertIn("HTTP 401", error)
        _absent(self, error, JOB_TOKEN)
        self.assertNotIn("private-key", error)
        self.assertNotIn(str(self.home), error)
        self.assertEqual(result["token_revocation"], [{"job_id": "build", "attempted": False}])
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(self.stub.requests[0]["method"], "POST")
        self.assertFalse(any(item["method"] == "DELETE" for item in self.stub.requests))
