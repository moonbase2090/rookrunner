# SPDX-License-Identifier: MPL-2.0

"""CHANGELOG keeps the preview section until the wheel and tarball exist."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
PROSE = " ".join(TEXT.split())


class ChangelogTests(unittest.TestCase):
    def test_format_and_unreleased_section(self):
        self.assertIn("The format is Keep a Changelog.", TEXT)
        self.assertIn("## [Unreleased]", TEXT)
        self.assertIn("Mozilla Public License, v. 2.0", PROSE)

    def test_preview_section_waits_for_the_artifact(self):
        self.assertIn("The current package version is 0.1.0.", TEXT)
        self.assertIn(
            "when the published wheel and the tarball exist",
            PROSE,
        )
        self.assertNotIn("## [0.1.0]", TEXT)


if __name__ == "__main__":
    unittest.main()
