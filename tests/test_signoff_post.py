"""The poller posts rookrunner/signoff from CODEOWNERS and the sign-off label.

No test reads a secret or contacts api.github.com. Expected states are
literals. The sign-off script decides; these tests do not reimplement it.
"""

import json
import unittest

from execution_core.poll import PollError
from test_poll import TOKEN, _Handler, _git, PollTests


def _commit(repo):
    _git(repo, "add", "-A")
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


class _RejectPost(_Handler):
    def do_POST(self):
        if getattr(self.server, "reject_post", False):
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
            payload = b"{}"
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("x-ratelimit-remaining", "0")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_POST()


class SignoffPostTests(unittest.TestCase):
    def setUp(self):
        self.host = PollTests("setUp")
        self.host.setUp()

    def tearDown(self):
        self.host.tearDown()

    def _owners(self, text="/pyproject.toml @owner\n"):
        path = self.host.seed / ".github" / "CODEOWNERS"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        toml = self.host.seed / "pyproject.toml"
        if not toml.exists():
            toml.write_text("[project]\nname = 'fixture'\n")
        _commit(self.host.seed)
        _git(self.host.seed, "push", "origin", "main")

    def _pull(self, number, mutate, labels=None):
        tip = self.host._tip()
        _git(self.host.seed, "checkout", "-b", "feature")
        mutate()
        _commit(self.host.seed)
        _git(self.host.seed, "push", "origin", "feature")
        head = _git(self.host.seed, "rev-parse", "feature").stdout.strip()
        _git(self.host.seed, "checkout", "main")
        _git(
            self.host.seed,
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
        _git(self.host.seed, "push", "origin", f"HEAD:refs/pull/{number}/merge")
        item = {
            "number": number,
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
        if labels is not None:
            item["labels"] = labels
        self.host.server.pulls = [item]
        self.host.server.etags["pulls"] = "pulls-2"
        return head

    def _signoff_posts(self):
        found = []
        for item in self.host.posts():
            body = json.loads(item["body"])
            if body.get("context") == "rookrunner/signoff":
                found.append((item, body))
        return found

    def test_a_protected_change_fails_until_the_label_and_then_holds(self):
        self._owners()
        head = self._pull(
            4,
            lambda: (self.host.seed / "pyproject.toml").write_text("[project]\nname = 'changed'\n"),
            labels=[{"name": "ready"}],
        )
        opened = self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "failure"}],
        )
        posted = self._signoff_posts()[0][0]
        self.assertEqual(posted["path"], f"/repos/acme/demo/statuses/{head}")
        self.assertEqual(posted["authorization"], "Bearer " + TOKEN)
        self.assertEqual(
            opened["statuses"],
            [
                {
                    "context": "rookrunner/signoff",
                    "state": "failure",
                    "action": "posted",
                    "sha": head,
                }
            ],
        )
        self.assertNotIn("run_id", opened["statuses"][0])
        self.assertFalse(any("/labels" in item["path"] for item in self.host.posts()))

        self.host.server.requests.clear()
        repeated = self.host.poll()
        self.assertEqual(self.host.posts(), [])
        self.assertEqual(repeated["skipped"], [])

        self.host.server.pulls[0]["labels"] = [
            {"name": "ready"},
            {"name": "mb2090-signoff"},
        ]
        self.host.server.etags["pulls"] = "pulls-3"
        self.host.server.requests.clear()
        labeled = self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "success"}],
        )
        self.assertEqual(self._signoff_posts()[0][0]["path"], f"/repos/acme/demo/statuses/{head}")
        self.assertEqual(
            labeled["statuses"],
            [
                {
                    "context": "rookrunner/signoff",
                    "state": "success",
                    "action": "posted",
                    "sha": head,
                }
            ],
        )

        self.host.server.etags["pulls"] = "pulls-4"
        self.host.server.requests.clear()
        held = self.host.poll()
        self.assertEqual(self._signoff_posts(), [])
        self.assertEqual(held["statuses"], [])
        saved = json.loads((self.host.state / "poll.json").read_text())
        self.assertEqual(saved["signoffs"]["4"]["head"], head)
        self.assertEqual(saved["signoffs"]["4"]["labeled"], True)
        self.assertEqual(saved["signoffs"]["4"]["state"], "success")

    def test_an_unprotected_file_posts_success(self):
        self._owners()
        head = self._pull(5, lambda: (self.host.seed / "notes.txt").write_text("note\n"))
        opened = self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "success"}],
        )
        self.assertEqual(self._signoff_posts()[0][0]["path"], f"/repos/acme/demo/statuses/{head}")
        self.assertEqual(opened["skipped"], [])

    def test_an_empty_codeowners_file_posts_success(self):
        self._owners("")
        self._pull(6, lambda: (self.host.seed / "notes.txt").write_text("note\n"))
        opened = self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "success"}],
        )
        self.assertEqual(opened["skipped"], [])

    def test_a_rule_removed_on_the_head_still_fails_from_the_base(self):
        self._owners()
        head = self._pull(
            7,
            lambda: (
                (self.host.seed / ".github" / "CODEOWNERS").write_text("# none\n"),
                (self.host.seed / "pyproject.toml").write_text("[project]\nname = 'changed'\n"),
            ),
        )
        self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "failure"}],
        )
        self.assertTrue(self._signoff_posts()[0][0]["path"].endswith("/" + head))

    def test_renaming_a_protected_path_fails(self):
        self._owners()

        def mutate():
            _git(self.host.seed, "mv", "pyproject.toml", "other.txt")

        head = self._pull(8, mutate)
        self.host.poll()
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "failure"}],
        )
        self.assertTrue(self._signoff_posts()[0][0]["path"].endswith("/" + head))

    def test_a_repository_without_codeowners_records_the_skip_once(self):
        self._pull(9, lambda: (self.host.seed / "notes.txt").write_text("note\n"))
        opened = self.host.poll()
        self.assertEqual(opened["skipped"], [{"reason": "signoff_absent"}])
        self.assertEqual(self._signoff_posts(), [])
        self.assertEqual(len(self.host.posts()), 1)
        self.assertEqual(
            json.loads(self.host.posts()[0]["body"])["context"],
            "rookrunner/check.yml/check",
        )
        saved = json.loads((self.host.state / "poll.json").read_text())
        self.assertNotIn("signoffs", saved)
        self.host.server.requests.clear()
        again = self.host.poll()
        self.assertEqual(again["skipped"], [])
        self.assertEqual(self.host.posts(), [])

    def test_a_label_field_that_is_not_a_list_is_rejected(self):
        self._owners()
        self._pull(10, lambda: (self.host.seed / "notes.txt").write_text("note\n"), labels="nope")
        with self.assertRaises(PollError) as caught:
            self.host.poll()
        self.assertEqual(caught.exception.kind, "API_REJECTED")
        self.assertEqual(self._signoff_posts(), [])

    def test_a_rate_limit_keeps_the_previous_decision_and_the_next_pass_posts(self):
        self._owners()
        head = self._pull(
            11,
            lambda: (self.host.seed / "pyproject.toml").write_text("[project]\nname = 'changed'\n"),
        )
        self.host.poll()
        self.assertEqual(self._signoff_posts()[0][1]["state"], "failure")
        self.host.server.pulls[0]["labels"] = [{"name": "mb2090-signoff"}]
        self.host.server.etags["pulls"] = "pulls-3"
        self.host.server.RequestHandlerClass = _RejectPost
        self.host.server.reject_post = True
        self.host.server.requests.clear()
        limited = self.host.poll()
        self.assertTrue(limited["stopped"])
        saved = json.loads((self.host.state / "poll.json").read_text())
        self.assertEqual(saved["signoffs"]["11"]["state"], "failure")
        self.assertEqual(saved["signoffs"]["11"]["labeled"], False)
        self.host.server.reject_post = False
        self.host.server.requests.clear()
        resumed = self.host.poll()
        self.assertFalse(resumed["stopped"])
        self.assertEqual(
            [body for _item, body in self._signoff_posts()],
            [{"context": "rookrunner/signoff", "state": "success"}],
        )
        self.assertEqual(self._signoff_posts()[0][0]["path"], f"/repos/acme/demo/statuses/{head}")


if __name__ == "__main__":
    unittest.main()
