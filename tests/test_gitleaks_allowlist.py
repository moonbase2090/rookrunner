# SPDX-License-Identifier: MPL-2.0

"""The gitleaks allowlist names fixture text and still rejects a key body."""

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"
# gitleaks 8.30.1 private-key rule, so the parser span can be checked in-process.
PRIVATE_KEY_RULE = re.compile(
    "(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY"
    "(?: BLOCK)?-----"
    r"[\s\S-]{64,}?"
    "KEY(?: BLOCK)?-----"
)


def _header():
    return "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def _footer():
    return "-----" + "END RSA PRIVATE KEY" + "-----"


def _key_body():
    return _header() + "\n" + ("M" * 80) + "\n" + _footer() + "\n"


class GitleaksAllowlistTests(unittest.TestCase):
    def test_allowlists_require_path_and_regex(self):
        data = tomllib.loads(CONFIG.read_text())
        self.assertTrue(data["extend"]["useDefault"])
        allowlists = data["allowlists"]
        self.assertEqual(len(allowlists), 2)

        parser, fixtures = allowlists
        self.assertEqual(parser["condition"], "AND")
        self.assertEqual(parser["regexTarget"], "secret")
        self.assertEqual(parser["paths"], [r"src/execution_core/checks\.py"])
        self.assertEqual(len(parser["regexes"]), 1)
        pattern = re.compile(parser["regexes"][0])
        source = (ROOT / "src/execution_core/checks.py").read_text()
        found = PRIVATE_KEY_RULE.search(source)
        self.assertIsNotNone(found)
        self.assertIsNotNone(pattern.search(found.group(0)))
        self.assertIsNotNone(PRIVATE_KEY_RULE.search(_key_body()))
        self.assertIsNone(pattern.search(_key_body()))
        quoted = _header() + '"' + "\n" + ("M" * 80) + "\n" + _footer()
        self.assertIsNone(pattern.search(quoted))

        self.assertEqual(fixtures["condition"], "AND")
        self.assertEqual(fixtures["regexTarget"], "line")
        self.assertEqual(
            fixtures["paths"],
            [
                r"docs/validation/dogfood-check-evidence\.json",
                r"docs/validation/m1-completion-evidence\.json",
            ],
        )
        self.assertEqual(len(fixtures["regexes"]), 1)
        line_re = re.compile(fixtures["regexes"][0])
        evidence = (ROOT / "docs/validation/m1-completion-evidence.json").read_text().splitlines()
        submission_lines = [line for line in evidence if '"submission_key"' in line]
        self.assertGreaterEqual(len(submission_lines), 1)
        for line in submission_lines:
            self.assertIsNotNone(line_re.search(line))
        other_lines = [line for line in evidence if ":" in line and '"submission_key"' not in line]
        self.assertTrue(other_lines)
        self.assertTrue(all(line_re.search(line) is None for line in other_lines))

    def test_history_is_clean_and_a_key_body_is_still_reported(self):
        gitleaks = shutil.which("gitleaks")
        if gitleaks is None:
            self.skipTest("gitleaks is not installed")
        scan = subprocess.run(
            [
                gitleaks,
                "detect",
                "--source",
                str(ROOT),
                "--config",
                str(CONFIG),
                "--log-opts=--all",
                "--no-banner",
                "--redact",
                "--exit-code",
                "1",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(scan.returncode, 0, scan.stderr[-800:] + scan.stdout[-800:])

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "planted.txt").write_text(_key_body())
            report_path = Path(tmp) / "report.json"
            planted = subprocess.run(
                [
                    gitleaks,
                    "detect",
                    "--no-git",
                    "--source",
                    tmp,
                    "--config",
                    str(CONFIG),
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report_path),
                    "--no-banner",
                    "--redact",
                    "--exit-code",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            raw = report_path.read_text() if report_path.exists() else ""
        findings = json.loads(raw) if raw.strip() else []
        rules = [item.get("RuleID") for item in findings]
        self.assertEqual(planted.returncode, 1, rules)
        self.assertEqual(rules, ["private-key"])


if __name__ == "__main__":
    unittest.main()
