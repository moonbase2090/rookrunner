# SPDX-License-Identifier: MIT

"""Job identity token claims and the mint decision, with stored-event fixtures
shaped like the ones the poll writes."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from execution_core.oidc_claims import (
    OidcRefusal,
    build_claims,
    mint,
    mint_refusal,
    require_audience,
)
from execution_core.checks import _load_rsa_key
from execution_core.oidc_keys import OidcError, OidcKey, load_key_set, publish

ISSUER = "https://oidc.example.com"
REPOSITORY = "octo-org/octo-repo"
REPOSITORY_ID = 101
SHA = "a" * 40
ISSUED = 1_800_000_000
WORKFLOW_PATH = ".github/workflows/deploy.yml"

PUSH = {
    "ref": "refs/heads/main",
    "before": "b" * 40,
    "after": SHA,
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
    "commits": [{"author": {"login": "octocat"}}],
}
TAG = {
    "ref": "refs/tags/v1.2.3",
    "before": "0" * 40,
    "after": SHA,
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
}
SCHEDULE = {
    "ref": "refs/heads/main",
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
    "schedule": "0 3 * * *",
}
DISPATCH = {
    "ref": "refs/heads/main",
    "repository": {"full_name": REPOSITORY, "default_branch": "main"},
}
PULL = {
    "ref": "refs/pull/7/merge",
    "before": "c" * 40,
    "after": SHA,
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
    "number": 7,
    "pull_request": {
        "head": {"sha": "d" * 40, "ref": "feature", "repo": {"id": REPOSITORY_ID}},
        "base": {"sha": "c" * 40, "ref": "main", "repo": {"id": REPOSITORY_ID}},
        "user": {"login": "octocat"},
    },
}


def _claims(event, event_name, **extra):
    arguments = {
        "issuer": ISSUER,
        "audience": "sts.amazonaws.com",
        "event": event,
        "event_name": event_name,
        "repository": REPOSITORY,
        "sha": SHA,
        "run_id": "run-1",
        "run_attempt": 2,
        "workflow": "Deploy",
        "workflow_path": WORKFLOW_PATH,
        "issued_at": ISSUED,
        "jti": "fixture-jti",
    }
    arguments.update(extra)
    return build_claims(**arguments)


def _refusal(event, event_name, **extra):
    arguments = {"repository": REPOSITORY}
    arguments.update(extra)
    return mint_refusal(event, event_name, **arguments)


class ClaimTests(unittest.TestCase):
    def test_a_default_branch_push_has_every_claim(self):
        self.assertIsNone(_refusal(PUSH, "push"))
        self.assertEqual(
            _claims(PUSH, "push"),
            {
                "iss": "https://oidc.example.com",
                "aud": "sts.amazonaws.com",
                "sub": "repo:octo-org/octo-repo:ref:refs/heads/main",
                "iat": 1_800_000_000,
                "nbf": 1_800_000_000,
                "exp": 1_800_000_300,
                "jti": "fixture-jti",
                "repository": "octo-org/octo-repo",
                "repository_id": "101",
                "repository_owner": "octo-org",
                "ref": "refs/heads/main",
                "ref_type": "branch",
                "sha": SHA,
                "run_id": "run-1",
                "run_attempt": "2",
                "workflow": "Deploy",
                "workflow_ref": "octo-org/octo-repo/.github/workflows/deploy.yml@refs/heads/main",
                "job_workflow_ref": (
                    "octo-org/octo-repo/.github/workflows/deploy.yml@refs/heads/main"
                ),
                "event_name": "push",
                "actor": "octocat",
                "runner_environment": "self-hosted",
            },
        )

    def test_subjects_follow_the_default_template(self):
        cases = [
            (
                TAG,
                "push",
                {"refs": ("refs/tags/v1.2.3",)},
                "repo:octo-org/octo-repo:ref:refs/tags/v1.2.3",
            ),
            (SCHEDULE, "schedule", {}, "repo:octo-org/octo-repo:ref:refs/heads/main"),
            (DISPATCH, "workflow_dispatch", {}, "repo:octo-org/octo-repo:ref:refs/heads/main"),
            (PULL, "pull_request", {"pull_requests": True}, "repo:octo-org/octo-repo:pull_request"),
        ]
        for event, event_name, gate, expected in cases:
            with self.subTest(event_name=event_name, ref=event["ref"]):
                self.assertIsNone(_refusal(event, event_name, **gate))
                self.assertEqual(_claims(event, event_name)["sub"], expected)

    def test_a_tag_is_a_tag_ref_and_unknown_fields_are_omitted(self):
        tag = _claims(TAG, "push")
        self.assertEqual(tag["ref_type"], "tag")
        self.assertNotIn("actor", tag)
        dispatch = _claims(DISPATCH, "workflow_dispatch")
        self.assertNotIn("repository_id", dispatch)
        self.assertNotIn("actor", dispatch)
        self.assertNotIn("environment", dispatch)

    def test_a_called_workflow_sets_job_workflow_ref(self):
        claims = _claims(PUSH, "push", job_workflow_path=".github/workflows/reusable.yml")
        self.assertEqual(
            claims["job_workflow_ref"],
            "octo-org/octo-repo/.github/workflows/reusable.yml@refs/heads/main",
        )
        self.assertEqual(
            claims["workflow_ref"],
            "octo-org/octo-repo/.github/workflows/deploy.yml@refs/heads/main",
        )

    def test_the_token_lives_five_minutes(self):
        claims = _claims(PUSH, "push", issued_at=1_900_000_123)
        self.assertEqual(claims["exp"] - claims["iat"], 300)
        self.assertEqual(claims["nbf"], 1_900_000_123)

    def test_every_token_gets_a_new_128_bit_jti(self):
        first = _claims(PUSH, "push", jti=None)["jti"]
        second = _claims(PUSH, "push", jti=None)["jti"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(base64.urlsafe_b64decode(first + "==")), 16)

    def test_the_audience_is_required_and_checked(self):
        self.assertEqual(require_audience("x" * 256), "x" * 256)
        self.assertEqual(
            require_audience("api://AzureADTokenExchange"), "api://AzureADTokenExchange"
        )
        for bad in (None, "", "x" * 257, "sts amazonaws", "aud\n", "café", ["sts"]):
            with self.subTest(audience=bad), self.assertRaises(OidcRefusal):
                _claims(PUSH, "push", audience=bad)

    def test_the_workflow_file_cannot_choose_identity_fields(self):
        for name, value in (
            ("sha", "main"),
            ("repository", "octo-org"),
            ("workflow_path", "../deploy.yml"),
            ("issuer", "http://oidc.example.com"),
        ):
            with self.subTest(field=name), self.assertRaises((OidcRefusal, OidcError)):
                _claims(PUSH, "push", **{name: value})


class MintDecisionTests(unittest.TestCase):
    def test_a_fork_pull_request_is_refused_even_with_the_flag(self):
        fork = copy.deepcopy(PULL)
        fork["pull_request"]["head"]["repo"] = {"id": 202}
        self.assertEqual(
            _refusal(fork, "pull_request", pull_requests=True), "pull request is from a fork"
        )

    def test_a_pull_request_into_another_base_repository_is_refused(self):
        moved = copy.deepcopy(PULL)
        moved["pull_request"]["base"]["repo"] = {"id": 303}
        self.assertEqual(
            _refusal(moved, "pull_request", pull_requests=True), "pull request is from a fork"
        )

    def test_a_null_head_repository_is_refused(self):
        deleted = copy.deepcopy(PULL)
        deleted["pull_request"]["head"]["repo"] = None
        self.assertEqual(
            _refusal(deleted, "pull_request", pull_requests=True),
            "pull request head repository is missing",
        )

    def test_a_same_repository_pull_request_needs_the_worker_flag(self):
        self.assertEqual(
            _refusal(PULL, "pull_request"), "pull requests do not receive tokens on this worker"
        )
        self.assertIsNone(_refusal(PULL, "pull_request", pull_requests=True))

    def test_an_override_run_is_refused(self):
        for event, event_name in ((PUSH, "push"), (PULL, "pull_request")):
            with self.subTest(event_name=event_name):
                self.assertEqual(
                    _refusal(event, event_name, pull_requests=True, untrusted_override=True),
                    "run was started by an untrusted override",
                )

    def test_a_local_submit_without_stored_fields_is_refused(self):
        bare = {"ref": "refs/heads/main"}
        self.assertEqual(_refusal(bare, "push"), "event repository is not the worker repository")
        self.assertEqual(_refusal({}, "push"), "event repository is not the worker repository")
        self.assertEqual(_refusal(None, "push"), "run has no stored event")
        no_id = copy.deepcopy(PUSH)
        del no_id["repository"]["id"]
        self.assertEqual(_refusal(no_id, "push"), "event has no stored repository id")
        other = copy.deepcopy(PUSH)
        other["repository"]["full_name"] = "octo-org/other"
        self.assertEqual(_refusal(other, "push"), "event repository is not the worker repository")

    def test_a_branch_off_the_allowlist_is_refused_until_listed(self):
        branch = copy.deepcopy(PUSH)
        branch["ref"] = "refs/heads/feature"
        self.assertEqual(_refusal(branch, "push"), "ref and actor are not on the allowlist")
        self.assertIsNone(_refusal(branch, "push", refs=("refs/heads/feature",)))
        self.assertIsNone(_refusal(branch, "push", pushers=("OctoCat",)))
        self.assertEqual(_refusal(TAG, "push"), "ref and actor are not on the allowlist")

    def test_other_events_are_refused(self):
        self.assertEqual(
            _refusal(PUSH, "issue_comment"),
            "event is not push, pull_request, schedule, or workflow_dispatch",
        )


def _int(value):
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def _rs256_verifies(jwk, token):
    """RFC 8017 RSASSA-PKCS1-v1_5 verification with SHA-256, from the JWK alone."""

    header, payload, signature = token.split(".")
    modulus, exponent = _int(jwk["n"]), _int(jwk["e"])
    width = (modulus.bit_length() + 7) // 8
    recovered = pow(_int(signature), exponent, modulus).to_bytes(width, "big")
    digest = hashlib.sha256(f"{header}.{payload}".encode("ascii")).digest()
    info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    expected = b"\x00\x01" + b"\xff" * (width - len(info) - 3) + b"\x00" + info
    return recovered == expected


class SignedTokenTests(unittest.TestCase):
    def test_a_minted_token_verifies_against_the_published_jwks(self):
        with tempfile.TemporaryDirectory(prefix="execution-test-") as name:
            root = Path(name).resolve()
            oidc = root / "keys" / "oidc"
            oidc.mkdir(parents=True)
            os.chmod(root / "keys", 0o700)
            os.chmod(oidc, 0o700)
            generated = root / "generated.pem"
            subprocess.run(
                [
                    "openssl",
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    str(generated),
                ],
                check=True,
                capture_output=True,
            )
            kid = OidcKey(*_load_rsa_key(generated.read_bytes())).kid
            generated.rename(oidc / f"{kid}.pem")
            os.chmod(oidc / f"{kid}.pem", 0o600)
            (oidc / "active").write_text(kid)
            os.chmod(oidc / "active", 0o600)
            key_set = load_key_set(str(root / "keys"))
            publish(key_set, ISSUER, root / "site", str(root / "keys"))
            jwks = json.loads((root / "site" / ".well-known" / "jwks.json").read_text())

            claims = _claims(PUSH, "push")
            token = mint(key_set, claims)

        header, payload, _ = token.split(".")
        decoded_header = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        self.assertEqual(decoded_header, {"alg": "RS256", "typ": "JWT", "kid": kid})
        self.assertEqual(
            json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))), claims
        )
        published = [key for key in jwks["keys"] if key["kid"] == kid]
        self.assertEqual(len(published), 1)
        self.assertTrue(_rs256_verifies(published[0], token))
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        self.assertFalse(_rs256_verifies(published[0], tampered))


if __name__ == "__main__":
    unittest.main()
