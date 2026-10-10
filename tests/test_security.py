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

    def test_local_mode_is_not_a_sandbox(self):
        self.assertIn(
            "Local mode is not a sandbox for arbitrary hostile repositories.",
            TEXT,
        )
        self.assertIn("does not promise universal log redaction", TEXT)


if __name__ == "__main__":
    unittest.main()
