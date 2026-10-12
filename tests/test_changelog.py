# SPDX-License-Identifier: MIT

"""CHANGELOG records 0.1.3 and 0.1.1 and still has no 0.1.0 section."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
PROSE = " ".join(TEXT.split())


class ChangelogTests(unittest.TestCase):
    def test_format_and_unreleased_section(self):
        self.assertIn("The format is Keep a Changelog.", TEXT)
        self.assertIn("## [Unreleased]", TEXT)
        self.assertIn(
            "A poll pass keeps going when an older claimed run has no readable snapshot.",
            PROSE,
        )
        self.assertIn("That run is skipped and its submission stays.", PROSE)
        self.assertIn("The source distribution omits `tests` and `scripts`.", PROSE)
        self.assertIn("without a header line in `checks.py`.", PROSE)
        self.assertIn("MIT License", PROSE)
        self.assertIn(
            "A host copy of the worker unit must set `NoNewPrivileges=yes`. "
            "The repository unit sets that directive. "
            "A copy that omits it is reported by systemd as "
            "`NoNewPrivileges=no` and must be set to `yes`. "
            "This note does not edit a running unit.",
            PROSE,
        )
        self.assertIn("A pull request that changes a workflow file is refused.", PROSE)
        self.assertIn(
            "A host that upgrades onto a tree where the job network defaults to `none`",
            PROSE,
        )
        self.assertIn("Cargo and pip need it.", PROSE)
        self.assertIn("The repository unit omits the flag.", PROSE)
        self.assertIn("Job containers use Docker network `none` by default.", PROSE)
        self.assertIn("`worker --network bridge` opts in to egress.", PROSE)
        self.assertIn("Service containers start only with that opt-in.", PROSE)
        self.assertIn("Every engine call uses the Docker context named `default`", PROSE)
        self.assertIn("An inherited `DOCKER_CONTEXT` or `DOCKER_HOST` does not select", PROSE)
        self.assertIn("The scheduler thread uses that same context.", PROSE)
        self.assertIn("A bash or sh `run` script quotes each `${{ }}` value.", PROSE)
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
        self.assertIn(
            "A missing or unrecognized pull request author association is refused.", PROSE
        )
        self.assertIn("its first parent is the listed base", PROSE)

    def test_0_1_3_records_the_network_default_and_the_upgrade(self):
        self.assertIn("The current package version is 0.1.3.", TEXT)
        self.assertIn("## [0.1.3] - 2026-10-11", TEXT)
        self.assertNotIn("## [0.1.2]", TEXT)
        section = TEXT.split("## [0.1.3] - 2026-10-11", 1)[1].split("## [0.1.1]", 1)[0]
        prose = " ".join(section.split())
        self.assertIn("Job containers use Docker network `none` by default.", prose)
        self.assertIn("`worker --network bridge` opts in to egress.", prose)
        self.assertIn("Service containers start only with that opt-in.", prose)
        self.assertIn(
            "A host that upgrades onto a tree where the job network defaults to `none`",
            prose,
        )
        self.assertIn(
            "adds `--network bridge` on that host's unit when jobs need egress.",
            prose,
        )
        self.assertIn("Cargo and pip need it.", prose)
        self.assertIn("The repository unit omits the flag.", prose)
        self.assertIn("This change does not create a tag.", prose)

    def test_0_1_1_records_the_tag_fetch_fix(self):
        self.assertIn("## [0.1.1] - 2026-10-10", TEXT)
        self.assertIn("fetches the annotated tag", TEXT)
        self.assertIn("names the wheel and tarball hashes", PROSE)
        self.assertNotIn("## [0.1.0]", TEXT)


if __name__ == "__main__":
    unittest.main()
