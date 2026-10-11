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
        self.assertIn(
            "A pull request that changes a workflow file is also refused.",
            PROSE,
        )
        self.assertIn("`--allow-pr-workflow` names that pull request's author association.", PROSE)
        self.assertIn("An unquoted value is single-quoted.", PROSE)
        self.assertIn("An expression in `eval` or in a shell `-c` command is refused.", PROSE)
        self.assertIn("A brace group or an `if`, `while`, or `for` body does not hide it.", PROSE)
        self.assertIn("A backslash in that command name does not hide it either.", PROSE)
        self.assertIn("A backslash before a newline is not part of that name.", PROSE)
        self.assertIn(
            "`$'eval'`, `$\"eval\"`, and a command substitution used as the command", PROSE
        )
        self.assertIn("An unquoted `*`, `?`, or `[` in that command name is refused", PROSE)
        self.assertIn("The `[` command stays accepted.", PROSE)
        self.assertIn("An unquoted `{` in that command name is refused", PROSE)
        self.assertIn("`builtin eval` is refused the same way.", PROSE)
        self.assertIn("A missing or unrecognized author association is refused.", PROSE)
        self.assertIn(
            "The fetched merge commit is checked out only when its first parent is the listed base",
            PROSE,
        )


if __name__ == "__main__":
    unittest.main()
