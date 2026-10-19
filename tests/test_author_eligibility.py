# SPDX-License-Identifier: MIT

"""Secret and token eligibility must not follow the commit author identity.

A writer can set any author email on a pushed commit, and the API
resolves that email to the named account's login. The stored push event
therefore carries a login the pusher never had to authenticate as, so a
pusher-allowlist entry naming that account must not grant file-backed
secrets or a minted token for the run.
"""

import copy
import unittest

from execution_core.oidc_claims import build_claims, mint_refusal
from execution_core.poll import allowlist_matches
from execution_core.secrets import SecretAccess, secrets_allowed

ISSUER = "https://oidc.example.com"
REPOSITORY = "octo-org/octo-repo"
REPOSITORY_ID = 101
SHA = "a" * 40
ISSUED = 1_800_000_000
WORKFLOW_PATH = ".github/workflows/deploy.yml"

FORGED_AUTHOR_PUSH = {
    "ref": "refs/heads/feature",
    "before": "b" * 40,
    "after": SHA,
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
    "commits": [{"author": {"login": "octocat"}}],
}

PULL = {
    "ref": "refs/pull/7/merge",
    "before": "c" * 40,
    "after": SHA,
    "repository": {"full_name": REPOSITORY, "id": REPOSITORY_ID, "default_branch": "main"},
    "number": 7,
    "action": "opened",
    "pull_request": {
        "head": {"sha": "d" * 40, "ref": "feature", "repo": {"id": REPOSITORY_ID}},
        "base": {"sha": "c" * 40, "ref": "main", "repo": {"id": REPOSITORY_ID}},
        "user": {"login": "octocat"},
    },
}


def _claims(event, event_name):
    return build_claims(
        issuer=ISSUER,
        audience="sts.amazonaws.com",
        event=event,
        event_name=event_name,
        repository=REPOSITORY,
        sha=SHA,
        run_id="run-1",
        run_attempt=2,
        workflow="Deploy",
        workflow_path=WORKFLOW_PATH,
        issued_at=ISSUED,
        jti="fixture-jti",
    )


class ForgedAuthorEligibilityTests(unittest.TestCase):
    def test_a_forged_author_login_grants_no_secrets_and_no_token(self):
        event = copy.deepcopy(FORGED_AUTHOR_PUSH)
        access = SecretAccess(REPOSITORY, pushers=("OctoCat",))

        self.assertFalse(allowlist_matches(event, [], ["OctoCat"], "push"))
        self.assertFalse(secrets_allowed(access, event, "push"))
        self.assertEqual(
            mint_refusal(event, "push", repository=REPOSITORY, pushers=("OctoCat",)),
            "ref and actor are not on the allowlist",
        )
        self.assertNotIn("actor", _claims(event, "push"))

    def test_a_listed_ref_still_grants_the_same_push(self):
        event = copy.deepcopy(FORGED_AUTHOR_PUSH)
        access = SecretAccess(REPOSITORY, refs=("refs/heads/feature",))

        self.assertTrue(allowlist_matches(event, ["refs/heads/feature"], [], "push"))
        self.assertTrue(secrets_allowed(access, event, "push"))
        self.assertIsNone(
            mint_refusal(event, "push", repository=REPOSITORY, refs=("refs/heads/feature",))
        )

    def test_a_listed_pull_request_author_still_grants_the_run(self):
        access = SecretAccess(REPOSITORY, pushers=("OctoCat",))

        self.assertTrue(allowlist_matches(PULL, [], ["OctoCat"], "pull_request"))
        self.assertTrue(secrets_allowed(access, PULL, "pull_request"))
        self.assertIsNone(
            mint_refusal(
                PULL,
                "pull_request",
                repository=REPOSITORY,
                pushers=("OctoCat",),
                pull_requests=True,
            )
        )
        self.assertEqual(_claims(PULL, "pull_request")["actor"], "octocat")

    def test_a_synchronize_grants_nothing_through_the_request_author(self):
        # The update may have been pushed by a different account than the
        # request author, and the poller cannot see which. Eligibility and
        # claims fail closed instead of using the author as a stand-in.
        event = copy.deepcopy(PULL)
        event["action"] = "synchronize"
        access = SecretAccess(REPOSITORY, pushers=("OctoCat",))

        self.assertFalse(allowlist_matches(event, [], ["OctoCat"], "pull_request"))
        self.assertFalse(secrets_allowed(access, event, "pull_request"))
        # Pull-request token gating is by flag plus same-repo, never by
        # pusher login: the mint is allowed but carries no actor claim.
        self.assertIsNone(
            mint_refusal(
                event,
                "pull_request",
                repository=REPOSITORY,
                pushers=("OctoCat",),
                pull_requests=True,
            )
        )
        self.assertNotIn("actor", _claims(event, "pull_request"))


if __name__ == "__main__":
    unittest.main()
