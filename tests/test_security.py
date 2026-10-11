# SPDX-License-Identifier: MPL-2.0

"""SECURITY.md states the local access model and the private reporting path."""

import unittest
from pathlib import Path

TEXT = " ".join((Path(__file__).resolve().parents[1] / "SECURITY.md").read_text().split())


class SecurityPolicyTests(unittest.TestCase):
    def test_reporting_stays_private(self):
        self.assertIn(
            "https://github.com/moonbase2090/rookrunner/security/advisories/new",
            TEXT,
        )
        self.assertIn("Do not open a public issue that contains a secret", TEXT)

    def test_local_access_modes(self):
        self.assertIn("state directory is mode `0700`", TEXT)
        self.assertIn("socket is mode `0600`", TEXT)
        self.assertIn("The worker has no network listener.", TEXT)
        self.assertIn("memory limit of 4g", TEXT)
        self.assertIn("process limit of 1024", TEXT)
        self.assertIn("CPU limit of 2", TEXT)
        self.assertIn("The default network remains `bridge`.", TEXT)
        self.assertIn("`--security-opt no-new-privileges`", TEXT)
        self.assertIn("`--cap-drop ALL`", TEXT)
        self.assertIn("including `CAP_CHOWN`", TEXT)
        self.assertIn("not receive `--cap-drop` or `--cap-add`", TEXT)
        self.assertIn("A step cannot gain privileges through sudo.", TEXT)
        self.assertIn("An unquoted value is single-quoted.", TEXT)
        self.assertIn("An expression in `eval` or in a shell `-c` command is refused.", TEXT)
        self.assertIn("A brace group or an `if`, `while`, or `for` body does not hide it.", TEXT)
        self.assertIn("A backslash in that command name does not hide it either.", TEXT)
        self.assertIn("A backslash before a newline is not part of that name.", TEXT)
        self.assertIn(
            "When that pull request changes a workflow file, or that diff cannot be read,",
            TEXT,
        )
        self.assertIn("A missing association does not match.", TEXT)
        self.assertIn(
            "The fetched merge commit is checked out only when its first parent is the listed base",
            TEXT,
        )

    def test_local_mode_is_not_a_sandbox(self):
        self.assertIn(
            "Local mode is not a sandbox for arbitrary hostile repositories.",
            TEXT,
        )
        self.assertIn("does not promise universal log redaction", TEXT)


if __name__ == "__main__":
    unittest.main()
