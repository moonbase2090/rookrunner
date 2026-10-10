# SPDX-License-Identifier: MPL-2.0

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "signoff.py"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

EXPECTED_PATTERNS = (
    "pyproject.toml",
    "uv.lock",
    ".github/",
    "docs/lints.md",
    "docs/conventions.md",
    "docs/design/static-analysis.md",
    "docs/development-dependencies.md",
)

PYPROJECT_DIFF = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,1 +1,1 @@
-version = "0.0.1"
+version = "0.0.2"
"""

README_DIFF = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Rookrunner
+# Rookrunner draft
"""


def load_signoff():
    spec = importlib.util.spec_from_file_location("rookrunner_signoff", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load .github/signoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SignoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signoff = load_signoff()

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["https_proxy"] = "http://127.0.0.1:9"
        return subprocess.run(
            [sys.executable, str(SCRIPT), *extra],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
        )

    def test_codeowners_lists_the_accepted_paths(self):
        patterns = self.signoff.parse_patterns(CODEOWNERS.read_text(encoding="utf-8"))
        self.assertEqual(tuple(patterns), EXPECTED_PATTERNS)
        for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            self.assertTrue(line.endswith(" @mb2090"), line)

    def test_each_pattern_protects_a_path_and_source_does_not(self):
        patterns = self.signoff.parse_patterns(CODEOWNERS.read_text(encoding="utf-8"))
        for pattern in patterns:
            if pattern.endswith("/"):
                sample = pattern + "owned.txt"
            else:
                sample = pattern[1:] if pattern.startswith("/") else pattern
            self.assertTrue(self.signoff.path_is_protected(sample, [pattern]), pattern)
            code, hits = self.signoff.decision([sample], [pattern], [])
            self.assertEqual(code, 1, pattern)
            self.assertEqual(hits, [sample])
        self.assertFalse(self.signoff.path_is_protected("src/execution_core/cli.py", patterns))
        self.assertEqual(
            self.signoff.decision(["src/execution_core/cli.py"], patterns, []),
            (0, []),
        )

    def test_directory_and_file_patterns_match_only_their_paths(self):
        patterns = list(EXPECTED_PATTERNS)
        self.assertTrue(self.signoff.path_is_protected("pyproject.toml", patterns))
        self.assertTrue(self.signoff.path_is_protected("pkg/pyproject.toml", patterns))
        self.assertFalse(self.signoff.path_is_protected("pyproject.toml.bak", patterns))
        self.assertTrue(self.signoff.path_is_protected(".github/workflows/signoff.yml", patterns))
        self.assertTrue(self.signoff.path_is_protected(".github/signoff.py", patterns))
        self.assertFalse(self.signoff.path_is_protected("github/signoff.py", patterns))
        self.assertTrue(self.signoff.path_is_protected("docs/lints.md", patterns))
        self.assertFalse(self.signoff.path_is_protected("docs/lints.md.bak", patterns))
        self.assertFalse(self.signoff.path_is_protected("other/docs/lints.md", patterns))
        self.assertTrue(self.signoff.path_is_protected("docs/design/static-analysis.md", patterns))

    def test_fixture_diff_of_pyproject_fails_without_the_label(self):
        with tempfile.TemporaryDirectory(prefix="signoff-") as directory:
            diff = Path(directory) / "pyproject.diff"
            diff.write_text(PYPROJECT_DIFF, encoding="utf-8")
            result = self.run_cli("--codeowners", str(CODEOWNERS), "--diff-file", str(diff))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "protected paths changed without mb2090-signoff:\npyproject.toml\n",
        )

    def test_fixture_diff_of_pyproject_passes_with_the_label(self):
        with tempfile.TemporaryDirectory(prefix="signoff-") as directory:
            diff = Path(directory) / "pyproject.diff"
            diff.write_text(PYPROJECT_DIFF, encoding="utf-8")
            result = self.run_cli(
                "--codeowners",
                str(CODEOWNERS),
                "--diff-file",
                str(diff),
                "--label",
                "mb2090-signoff",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sign-off label present\n")

    def test_fixture_diff_of_readme_needs_no_label(self):
        with tempfile.TemporaryDirectory(prefix="signoff-") as directory:
            diff = Path(directory) / "readme.diff"
            diff.write_text(README_DIFF, encoding="utf-8")
            result = self.run_cli("--codeowners", str(CODEOWNERS), "--diff-file", str(diff))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "no protected path changed\n")

    def test_clearing_head_keeps_a_base_rule(self):
        with tempfile.TemporaryDirectory(prefix="signoff-") as directory:
            root = Path(directory)
            head = root / "head"
            base = root / "base"
            diff = root / "pyproject.diff"
            head.write_text("# cleared\n", encoding="utf-8")
            base.write_text("pyproject.toml @mb2090\n", encoding="utf-8")
            diff.write_text(PYPROJECT_DIFF, encoding="utf-8")
            blocked = self.run_cli(
                "--codeowners",
                str(head),
                "--codeowners-base",
                str(base),
                "--diff-file",
                str(diff),
            )
            allowed = self.run_cli(
                "--codeowners",
                str(head),
                "--codeowners-base",
                str(base),
                "--diff-file",
                str(diff),
                "--label",
                "mb2090-signoff",
            )
        self.assertEqual(blocked.returncode, 1, blocked.stderr)
        self.assertIn("pyproject.toml", blocked.stdout)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_absent_base_codeowners_reads_as_empty(self):
        with tempfile.TemporaryDirectory(prefix="signoff-git-") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            codeowners = repo / ".github"
            codeowners.mkdir()
            (codeowners / "CODEOWNERS").write_text("uv.lock @mb2090\n", encoding="utf-8")
            subprocess.run(["git", "add", ".github/CODEOWNERS"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "owners",
                ],
                cwd=repo,
                check=True,
            )
            owned = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            previous = Path.cwd()
            os.chdir(repo)
            try:
                self.assertEqual(self.signoff.git_show_codeowners(sha), "")
                self.assertEqual(
                    self.signoff.git_show_codeowners(owned),
                    "uv.lock @mb2090\n",
                )
            finally:
                os.chdir(previous)
        with tempfile.TemporaryDirectory(prefix="signoff-not-git-") as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with self.assertRaises(SystemExit):
                    self.signoff.git_show_codeowners(sha)
            finally:
                os.chdir(previous)

    def test_workflow_is_read_only_and_reruns_when_labeled(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "signoff.yml").read_text())
        self.assertEqual(
            workflow["permissions"],
            {"contents": "read", "pull-requests": "read"},
        )
        # PyYAML 1.1 parses the workflow key "on" as True.
        trigger = workflow[True]["pull_request"]["types"]
        self.assertIn("labeled", trigger)
        self.assertIn("unlabeled", trigger)
        steps = workflow["jobs"]["signoff"]["steps"]
        checkout = steps[0]
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        check = yaml.safe_load((ROOT / ".github" / "workflows" / "check.yml").read_text())
        check_uses = check["jobs"]["check"]["steps"][0]["uses"]
        self.assertEqual(checkout["uses"], check_uses)
        self.assertEqual(steps[1]["run"], "python3 .github/signoff.py")
        self.assertNotIn("write", yaml.safe_dump(workflow["permissions"]))
