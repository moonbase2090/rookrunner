# SPDX-License-Identifier: MPL-2.0

"""The README states the license, the milestones, and the untrusted refusal."""

import unittest
from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text()
PROSE = " ".join(README.split())


class ReadmeTests(unittest.TestCase):
    def test_license_is_mpl_2_0(self):
        self.assertIn("Mozilla Public License, v. 2.0", PROSE)
        self.assertNotIn("project license remain undecided", PROSE)
        self.assertNotIn("A project license must be selected", PROSE)

    def test_milestones_match_the_roadmap(self):
        self.assertIn("M3 is implemented", PROSE)
        self.assertIn("are not yet published", PROSE)
        self.assertNotIn("M2 started", PROSE)
        self.assertNotIn("Packaging, MCP, and dashboard work remain", PROSE)

    def test_startup_requires_pyyaml(self):
        self.assertIn("PyYAML 6.0.3", PROSE)
        self.assertNotIn("No third-party dependencies", PROSE)

    def test_untrusted_code_is_refused(self):
        self.assertIn("A poll refuses untrusted code.", PROSE)
        self.assertIn("`--allow-untrusted` with that commit's SHA is the only override.", PROSE)
        self.assertIn("No run is created.", PROSE)


if __name__ == "__main__":
    unittest.main()
