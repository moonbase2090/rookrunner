# SPDX-License-Identifier: MPL-2.0

"""CHANGELOG records 0.1.1 and still has no published 0.1.0 section."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
PROSE = " ".join(TEXT.split())


class ChangelogTests(unittest.TestCase):
    def test_format_and_unreleased_section(self):
        self.assertIn("The format is Keep a Changelog.", TEXT)
        self.assertIn("## [Unreleased]", TEXT)
        self.assertIn("Mozilla Public License, v. 2.0", PROSE)

    def test_0_1_1_records_the_tag_fetch_fix(self):
        self.assertIn("The current package version is 0.1.1.", TEXT)
        self.assertIn("## [0.1.1] - 2026-10-10", TEXT)
        self.assertIn("fetches the annotated tag", TEXT)
        self.assertIn("names the wheel and tarball hashes", PROSE)
        self.assertNotIn("## [0.1.0]", TEXT)


if __name__ == "__main__":
    unittest.main()
