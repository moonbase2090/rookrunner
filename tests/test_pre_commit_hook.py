# SPDX-License-Identifier: MIT

"""Staged Python under src/ and tests/ is format-checked. Other files are not."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-commit"
INSTALL = ROOT / "scripts" / "install-hooks.sh"
RUFF = ROOT / ".venv" / "bin" / "ruff"
FIX = ".venv/bin/ruff format src tests\n"


class PreCommitHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        bindir = self.repo / ".venv" / "bin"
        bindir.mkdir(parents=True)
        os.symlink(RUFF, bindir / "ruff")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def hook(self):
        return subprocess.run(
            ["sh", str(HOOK)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_no_staged_python_exits_zero(self):
        self.write("README.md", "notes\n")
        self.write("src/pending.py", "x=1\n")
        self.git("add", "README.md")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual((self.repo / "src" / "pending.py").read_text(encoding="utf-8"), "x=1\n")

    def test_formatted_python_with_a_space_in_the_name_exits_zero(self):
        self.write("src/my file.py", "x = 1\n")
        self.write("tests/test_ok.py", "value = 1\n")
        self.git("add", "src/my file.py", "tests/test_ok.py")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unformatted_python_exits_one_and_names_the_fix(self):
        self.write("tests/test_bad.py", "x=1\n")
        self.git("add", "tests/test_bad.py")
        result = self.hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn(FIX, result.stdout)
        self.assertEqual((self.repo / "tests" / "test_bad.py").read_text(encoding="utf-8"), "x=1\n")

    def test_python_outside_src_and_tests_is_ignored(self):
        self.write("docs/notes.py", "x=1\n")
        self.git("add", "docs/notes.py")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_staged_deletion_is_ignored(self):
        self.write("src/old.py", "x = 1\n")
        self.git("add", "src/old.py")
        self.git("commit", "-m", "add")
        self.git("rm", "src/old.py")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_format_clean_lint_failure_stays_success(self):
        self.write("src/unused.py", "import os\n\n\ndef ready():\n    return 1\n")
        self.git("add", "src/unused.py")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_formatted_worktree_does_not_hide_an_unformatted_staged_blob(self):
        self.write("src/partial.py", "x=1\n")
        self.git("add", "src/partial.py")
        self.write("src/partial.py", "x = 1\n")
        result = self.hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn(FIX, result.stdout)
        self.assertEqual((self.repo / "src" / "partial.py").read_text(encoding="utf-8"), "x = 1\n")
        staged = self.git("show", ":src/partial.py")
        self.assertEqual(staged.stdout, "x=1\n")

    def test_an_unformatted_worktree_does_not_fail_a_formatted_staged_blob(self):
        self.write("src/partial.py", "x = 1\n")
        self.git("add", "src/partial.py")
        self.write("src/partial.py", "x=1\n")
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = self.git("show", ":src/partial.py")
        self.assertEqual(staged.stdout, "x = 1\n")
        self.assertEqual((self.repo / "src" / "partial.py").read_text(encoding="utf-8"), "x=1\n")

    def test_install_hooks_sets_the_hooks_path(self):
        destination = self.repo / "scripts" / "install-hooks.sh"
        destination.parent.mkdir()
        shutil.copyfile(INSTALL, destination)
        result = subprocess.run(
            ["sh", str(destination)],
            cwd=self.tmp.name,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        configured = self.git("config", "--get", "core.hooksPath")
        self.assertEqual(configured.stdout, ".githooks\n")
