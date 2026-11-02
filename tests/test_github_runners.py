# SPDX-License-Identifier: MIT

"""The experimental GitHub runner prototype: flag, registration client, webhook intake.

The client talks to a local fake server on loopback. The HMAC expectation is the
test vector from GitHub's webhook validation documentation, not a value computed
by the code under test.
"""

import hashlib
import hmac
import json
import logging
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import MappingProxyType

from execution_core import github_runners as gr

ON = {gr.FLAG_ENV: "1"}

# https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
DOC_SECRET = "It's a Secret to Everybody"
DOC_PAYLOAD = b"Hello, World!"
DOC_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"

JIT_SECRET = "ENCODED-JIT-CONFIG-SENTINEL-9f3a"
REG_SECRET = "REGISTRATION-TOKEN-SENTINEL-77c1"
API_SECRET = "API-BEARER-SENTINEL-1e5b"


class FakeGitHub:
    def __init__(self, status=201, body=None, redirect_to=None):
        self.redirect_to = redirect_to
        self.requests = []
        self.status = status
        self.body = body
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(
                    (self.path, dict(self.headers), self.rfile.read(length).decode())
                )
                if outer.status in (301, 302):
                    self.send_response(outer.status)
                    self.send_header("Location", outer.redirect_to or "http://127.0.0.1:1/x")
                    self.end_headers()
                    return
                payload = json.dumps(outer.body or {}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def job_payload(action="queued", job_id=501, repo="Octo/Widgets", installation=77, **job):
    workflow_job = {"id": job_id, "run_id": 9, "labels": ["self-hosted", "linux"]}
    workflow_job.update(job)
    return {
        "action": action,
        "workflow_job": workflow_job,
        "repository": {"full_name": repo},
        "installation": {"id": installation},
    }


class FlagTests(unittest.TestCase):
    def test_default_off_refuses_every_entry_point(self):
        for env in ({}, {gr.FLAG_ENV: "0"}, {gr.FLAG_ENV: "true"}, {gr.FLAG_ENV: ""}):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(gr.FeatureDisabled):
                    gr.RunnerClient("https://example.invalid", lambda: "t", environ=env)
                with self.assertRaises(gr.FeatureDisabled):
                    gr.WebhookStore(tmp, environ=env)
                with self.assertRaises(gr.FeatureDisabled):
                    gr.handle_delivery({}, b"", "s", None, gr.Allowlist(), environ=env)
                self.assertEqual(list(Path(tmp).iterdir()), [], "no state created when off")

    def test_ambient_environment_is_off_by_default(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(gr.FeatureDisabled):
            gr.RunnerClient("https://example.invalid", lambda: "t")

    def test_flag_one_enables(self):
        gr.RunnerClient("https://example.invalid", lambda: "t", environ=ON)


class SecretTests(unittest.TestCase):
    def test_secret_never_shows_in_repr_or_str(self):
        secret = gr.Secret(JIT_SECRET)
        self.assertNotIn(JIT_SECRET, repr(secret))
        self.assertNotIn(JIT_SECRET, str(secret))
        self.assertNotIn(JIT_SECRET, f"{secret}")
        self.assertEqual(secret.reveal(), JIT_SECRET)

    def test_secret_refuses_pickling(self):
        import pickle

        with self.assertRaises(TypeError):
            pickle.dumps(gr.Secret(JIT_SECRET))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.close()

    def client(self, status=201, body=None, provider=None):
        self.fake = FakeGitHub(status, body)
        return gr.RunnerClient(self.fake.url, provider or (lambda: API_SECRET), environ=ON)

    JIT_BODY = MappingProxyType(
        {"runner": {"id": 12, "name": "rr-1"}, "encoded_jit_config": JIT_SECRET}
    )

    def test_jit_request_shape_repo_scope(self):
        client = self.client(body=dict(self.JIT_BODY))
        result = client.generate_jit_config(
            gr.Scope("octo", "widgets"),
            name="rr-1",
            runner_group_id=1,
            labels=["self-hosted", "linux"],
            work_folder="_work",
        )
        path, headers, body = self.fake.requests[0]
        self.assertEqual(path, "/repos/octo/widgets/actions/runners/generate-jitconfig")
        self.assertEqual(headers["Authorization"], f"Bearer {API_SECRET}")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertEqual(
            json.loads(body),
            {
                "name": "rr-1",
                "runner_group_id": 1,
                "labels": ["self-hosted", "linux"],
                "work_folder": "_work",
            },
        )
        self.assertEqual((result.runner_id, result.runner_name), (12, "rr-1"))
        self.assertEqual(result.encoded_jit_config.reveal(), JIT_SECRET)

    def test_jit_request_shape_org_scope_omits_work_folder(self):
        client = self.client(body=dict(self.JIT_BODY))
        client.generate_jit_config(
            gr.Scope("octo"), name="rr-1", runner_group_id=3, labels=["linux"]
        )
        path, _, body = self.fake.requests[0]
        self.assertEqual(path, "/orgs/octo/actions/runners/generate-jitconfig")
        self.assertEqual(
            json.loads(body), {"name": "rr-1", "runner_group_id": 3, "labels": ["linux"]}
        )

    def test_registration_token_request_and_redaction(self):
        client = self.client(body={"token": REG_SECRET, "expires_at": "2026-01-01T00:00:00Z"})
        token = client.create_registration_token(gr.Scope("octo", "widgets"))
        self.assertEqual(
            self.fake.requests[0][0], "/repos/octo/widgets/actions/runners/registration-token"
        )
        self.assertEqual(token.token.reveal(), REG_SECRET)
        self.assertEqual(token.expires_at, "2026-01-01T00:00:00Z")
        for text in (repr(token), str(token), repr(client), str(client)):
            self.assertNotIn(REG_SECRET, text)
            self.assertNotIn(API_SECRET, text)

    def test_jit_result_repr_hides_config(self):
        client = self.client(body=dict(self.JIT_BODY))
        result = client.generate_jit_config(
            gr.Scope("octo"), name="rr-1", runner_group_id=1, labels=["linux"]
        )
        self.assertNotIn(JIT_SECRET, repr(result))
        self.assertNotIn(JIT_SECRET, str(result))

    def test_secrets_not_logged_or_written(self):
        client = self.client(body=dict(self.JIT_BODY))
        with tempfile.TemporaryDirectory() as tmp, self.assertNoLogs(level=logging.DEBUG):
            import os

            old = os.getcwd()
            os.chdir(tmp)
            try:
                client.generate_jit_config(
                    gr.Scope("octo"), name="rr-1", runner_group_id=1, labels=["linux"]
                )
                written = list(Path(tmp).rglob("*"))
            finally:
                os.chdir(old)
        self.assertEqual(written, [])

    def test_error_message_has_no_token_or_body(self):
        client = self.client(
            status=422, body={"message": f"bad {JIT_SECRET} {API_SECRET} {REG_SECRET}"}
        )
        with self.assertRaises(gr.GitHubRunnerError) as caught:
            client.generate_jit_config(
                gr.Scope("octo"), name="rr-1", runner_group_id=1, labels=["linux"]
            )
        message = str(caught.exception)
        self.assertIn("422", message)
        for secret in (JIT_SECRET, API_SECRET, REG_SECRET):
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, repr(caught.exception))

    def test_malformed_response_error_has_no_secret(self):
        client = self.client(body={"encoded_jit_config": JIT_SECRET})
        with self.assertRaises(gr.GitHubRunnerError) as caught:
            client.generate_jit_config(
                gr.Scope("octo"), name="rr-1", runner_group_id=1, labels=["linux"]
            )
        self.assertNotIn(JIT_SECRET, str(caught.exception))

    def test_redirect_is_refused_and_token_not_forwarded(self):
        target = FakeGitHub(body=dict(self.JIT_BODY))
        self.addCleanup(target.close)
        client = self.client(status=302)
        self.fake.redirect_to = target.url + "/stolen"
        with self.assertRaises(gr.GitHubRunnerError):
            client.generate_jit_config(
                gr.Scope("octo"), name="rr-1", runner_group_id=1, labels=["linux"]
            )
        self.assertEqual(target.requests, [])
        self.assertEqual(len(self.fake.requests), 1)

    def test_token_provider_failure_sends_nothing(self):
        client = self.client(body=dict(self.JIT_BODY), provider=lambda: "")
        with self.assertRaises(gr.GitHubRunnerError):
            client.create_registration_token(gr.Scope("octo"))
        self.assertEqual(self.fake.requests, [])

    def test_invalid_inputs_refused_before_any_request(self):
        client = self.client(body=dict(self.JIT_BODY))
        scope = gr.Scope("octo")
        good = {"name": "rr-1", "runner_group_id": 1, "labels": ["linux"]}
        bad = [
            {"name": ""},
            {"name": "has space"},
            {"name": "../x"},
            {"name": "a" * 65},
            {"runner_group_id": 0},
            {"runner_group_id": -2},
            {"runner_group_id": True},
            {"runner_group_id": "1"},
            {"labels": []},
            {"labels": "linux"},
            {"labels": ["a,b"]},
            {"labels": ["ok", "ok"]},
            {"labels": [f"l{i}" for i in range(11)]},
            {"labels": [5]},
            {"work_folder": "../escape"},
            {"work_folder": "/abs"},
        ]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(gr.ValidationError):
                client.generate_jit_config(scope, **{**good, **override})
        self.assertEqual(self.fake.requests, [])

    def test_invalid_scope_and_base_url_refused(self):
        for owner, repo in [
            ("", None),
            ("a/b", None),
            ("..", None),
            ("octo", "x/../y"),
            ("octo", ""),
        ]:
            with self.subTest(owner=owner, repo=repo), self.assertRaises(gr.ValidationError):
                gr.Scope(owner, repo)
        for url in ["http://example.com", "ftp://x", "https://user:pw@example.com", "", None]:
            with self.subTest(url=url), self.assertRaises(gr.ValidationError):
                gr.RunnerClient(url, lambda: "t", environ=ON)


class SignatureTests(unittest.TestCase):
    def test_documented_vector_verifies(self):
        self.assertTrue(gr.verify_signature(DOC_SECRET, DOC_PAYLOAD, DOC_SIGNATURE))

    def test_documented_vector_rejects_tampering(self):
        self.assertFalse(gr.verify_signature(DOC_SECRET, DOC_PAYLOAD + b" ", DOC_SIGNATURE))
        self.assertFalse(gr.verify_signature("another secret", DOC_PAYLOAD, DOC_SIGNATURE))

    def test_malformed_headers_rejected(self):
        digest = DOC_SIGNATURE.removeprefix("sha256=")
        for header in [None, "", digest, "sha1=" + digest, "sha256=" + digest.upper(), "sha256=ab"]:
            with self.subTest(header=header):
                self.assertFalse(gr.verify_signature(DOC_SECRET, DOC_PAYLOAD, header))


class WebhookTests(unittest.TestCase):
    secret = "hook-secret"
    allow = gr.Allowlist(installation_ids={77}, repositories={"octo/widgets"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = gr.WebhookStore(self.tmp.name, environ=ON)
        self.addCleanup(self.store.close)

    def deliver(
        self,
        payload=None,
        *,
        delivery="d-1",
        event="workflow_job",
        raw=None,
        signature=True,
        store=None,
        allow=None,
        **kwargs,
    ):
        body = raw if raw is not None else json.dumps(payload or job_payload()).encode()
        headers = {"X-GitHub-Event": event}
        if delivery is not None:
            headers["X-GitHub-Delivery"] = delivery
        if signature is True:
            headers["X-Hub-Signature-256"] = sign(self.secret, body)
        elif signature:
            headers["X-Hub-Signature-256"] = signature
        return gr.handle_delivery(
            headers,
            body,
            self.secret,
            store or self.store,
            allow or self.allow,
            environ=ON,
            **kwargs,
        )

    def test_valid_queued_is_accepted_and_becomes_demand(self):
        result = self.deliver()
        self.assertEqual((result.status, result.outcome), (202, "accepted"))
        self.assertEqual(
            self.store.pending_demand(),
            [
                {
                    "job_id": 501,
                    "installation_id": 77,
                    "repository": "octo/widgets",
                    "labels": ["self-hosted", "linux"],
                }
            ],
        )

    def test_headers_are_case_insensitive(self):
        body = json.dumps(job_payload()).encode()
        headers = {
            "x-github-event": "workflow_job",
            "X-GITHUB-DELIVERY": "d-1",
            "x-hub-signature-256": sign(self.secret, body),
        }
        result = gr.handle_delivery(headers, body, self.secret, self.store, self.allow, environ=ON)
        self.assertEqual(result.outcome, "accepted")

    def test_bad_or_missing_signature_rejected_without_parsing(self):
        # Invalid JSON proves the parser was not reached: a parse attempt would give 400.
        junk = b"{not json"
        for signature in (False, "sha256=" + "0" * 64, sign("wrong", junk), "garbage"):
            with self.subTest(signature=signature):
                result = self.deliver(raw=junk, signature=signature)
                self.assertEqual((result.status, result.outcome), (401, "rejected"))
        self.assertEqual(self.store.delivery_count(), 0)

    def test_signature_is_checked_over_raw_bytes_not_reserialized_json(self):
        raw = b'{"action":  "queued"}'
        result = self.deliver(raw=raw, signature=sign(self.secret, b'{"action": "queued"}'))
        self.assertEqual(result.status, 401)

    def test_oversized_body_rejected_even_when_signed(self):
        raw = json.dumps(job_payload(), indent=1).encode()
        result = self.deliver(raw=raw, max_body_bytes=len(raw) - 1)
        self.assertEqual((result.status, result.outcome), (413, "rejected"))
        self.assertEqual(self.deliver(raw=raw, max_body_bytes=len(raw)).status, 202)

    def test_wrong_event_is_ignored_and_not_recorded(self):
        result = self.deliver(event="push")
        self.assertEqual((result.status, result.outcome), (200, "ignored"))
        self.assertEqual(self.store.delivery_count(), 0)
        self.assertEqual(self.store.pending_demand(), [])

    def test_unhandled_action_ignored(self):
        for action in ("waiting", "requested", "", None):
            with self.subTest(action=action):
                result = self.deliver(job_payload(action=action))
                self.assertEqual(result.outcome, "ignored")
        self.assertEqual(self.store.delivery_count(), 0)

    def test_non_allowlisted_repository_or_installation_rejected(self):
        for payload in (
            job_payload(repo="octo/other"),
            job_payload(installation=78),
            job_payload(repo="evil/widgets"),
        ):
            with self.subTest(payload=payload["repository"], inst=payload["installation"]):
                result = self.deliver(payload)
                self.assertEqual((result.status, result.outcome), (403, "rejected"))
        self.assertEqual(self.store.delivery_count(), 0)
        self.assertEqual(self.store.pending_demand(), [])

    def test_empty_allowlist_denies_all(self):
        self.assertEqual(self.deliver(allow=gr.Allowlist()).status, 403)

    def test_repository_match_ignores_case(self):
        self.assertEqual(self.deliver(job_payload(repo="OCTO/WIDGETS")).status, 202)

    def test_malformed_payload_rejected_after_valid_signature(self):
        self.assertEqual(self.deliver(raw=b"{not json").status, 400)
        self.assertEqual(self.deliver(raw=b"[1]").status, 400)
        broken = job_payload()
        del broken["installation"]
        self.assertEqual(self.deliver(broken).status, 400)
        self.assertEqual(self.deliver(delivery=None).status, 400)
        self.assertEqual(self.deliver(delivery="bad id!").status, 400)

    def test_duplicate_delivery_id_not_enqueued_twice(self):
        self.assertEqual(self.deliver(delivery="same").outcome, "accepted")
        again = self.deliver(delivery="same")
        self.assertEqual((again.status, again.outcome), (200, "duplicate"))
        self.assertEqual(len(self.store.pending_demand()), 1)
        self.assertEqual(self.store.delivery_count(), 1)

    def test_duplicate_delivery_id_survives_store_reopen(self):
        self.deliver(delivery="same")
        self.store.close()
        reopened = gr.WebhookStore(self.tmp.name, environ=ON)
        self.addCleanup(reopened.close)
        again = self.deliver(delivery="same", store=reopened)
        self.assertEqual(again.outcome, "duplicate")
        self.assertEqual(len(reopened.pending_demand()), 1)

    def test_distinct_delivery_for_same_job_does_not_add_demand(self):
        self.deliver(delivery="a")
        self.deliver(delivery="b")
        self.assertEqual(self.store.delivery_count(), 2)
        self.assertEqual(len(self.store.pending_demand()), 1)

    def test_delivery_identity_is_separate_from_job_identity(self):
        self.deliver(job_payload(job_id=1), delivery="d-1")
        self.deliver(job_payload(job_id=2), delivery="d-2")
        self.assertEqual(self.store.delivery_count(), 2)
        self.assertEqual({d["job_id"] for d in self.store.pending_demand()}, {1, 2})

    def test_in_progress_clears_demand(self):
        self.deliver(delivery="a")
        self.deliver(job_payload("in_progress"), delivery="b")
        self.assertEqual(self.store.pending_demand(), [])
        self.assertEqual(self.store.job(501)["reported_action"], "in_progress")

    def test_late_queued_after_in_progress_does_not_recreate_demand(self):
        self.deliver(job_payload("in_progress"), delivery="a")
        self.deliver(job_payload("queued"), delivery="b")
        self.assertEqual(self.store.pending_demand(), [])
        self.assertEqual(self.store.job(501)["reported_action"], "in_progress")

    def test_late_in_progress_after_completed_keeps_the_conclusion(self):
        self.deliver(job_payload("completed", conclusion="failure"), delivery="a")
        self.deliver(job_payload("in_progress"), delivery="b")
        self.assertEqual(
            self.store.job(501),
            {"reported_action": "completed", "reported_conclusion": "failure"},
        )
        self.assertEqual(self.store.pending_demand(), [])

    def test_completed_failure_is_a_hint_not_success(self):
        self.deliver(delivery="a")
        self.deliver(job_payload("completed", conclusion="failure"), delivery="b")
        job = self.store.job(501)
        self.assertEqual(job, {"reported_action": "completed", "reported_conclusion": "failure"})
        self.assertEqual(self.store.pending_demand(), [])
        self.assertNotIn("succe", json.dumps(job).lower())

    def test_completed_success_is_still_only_a_reported_hint(self):
        self.deliver(job_payload("completed", conclusion="success"), delivery="a")
        self.assertEqual(
            self.store.job(501),
            {"reported_action": "completed", "reported_conclusion": "success"},
        )
        columns = {
            row[1]
            for table in ("deliveries", "jobs", "demand")
            for row in self.store._conn.execute(f"PRAGMA table_info({table})")
        }
        # No column can hold a local run result or exit code.
        self.assertFalse(columns & {"result", "exit_code", "succeeded", "local_state"})

    def test_state_files_are_private(self):
        mode = self.store.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
