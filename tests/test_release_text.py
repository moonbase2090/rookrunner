# SPDX-License-Identifier: MPL-2.0

"""Release text fails on private infrastructure and ignores a missing denylist."""

import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-release-text.sh"
ALLOW = "322824348+mb2090@users.noreply.github.com"


class ReleaseTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.home = Path(self.tmp.name) / "home"
        self.root.mkdir()
        self.home.mkdir()
        (self.root / "README.md").write_text("Hello\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("Guide\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, *args, deny="", denylist=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env.pop("RELEASE_DENYLIST", None)
        if deny:
            env["RELEASE_DENYLIST"] = deny
        if denylist is not None:
            path = self.home / ".config" / "moonbase"
            path.mkdir(parents=True, exist_ok=True)
            (path / "release-denylist.txt").write_text(denylist, encoding="utf-8")
        return subprocess.run(
            ["sh", str(SCRIPT), "--root", str(self.root), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_hidden(self, result, secret, report):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn(report, result.stdout)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn(str(self.home), result.stdout + result.stderr)

    def test_a_clean_tree_passes_without_a_denylist_file(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.home / ".config" / "moonbase" / "release-denylist.txt").exists())

    def test_the_allowed_email_passes_and_any_other_email_is_file_line_only(self):
        allowed = "\n".join(
            (
                ALLOW,
                "fixture@example.invalid",
                "rookrunner@example.invalid",
                "person@example.com",
                "noreply@github.com",
                "NoReply@GitHub.com",
            )
        )
        (self.root / "README.md").write_text(allowed + "\n", encoding="utf-8")
        clean = self.run_check()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(clean.stdout, "")

        other = "person@contoso.com"
        (self.root / "docs" / "guide.md").write_text(f"See {other} now\n", encoding="utf-8")
        result = self.run_check()
        self.assert_hidden(result, other, "docs/guide.md:1")

        github_user = "user@github.com"
        (self.root / "docs" / "guide.md").write_text(github_user + "\n", encoding="utf-8")
        github = self.run_check()
        self.assert_hidden(github, github_user, "docs/guide.md:1")

        other_noreply = "1+other@users.noreply.github.com"
        (self.root / "docs" / "guide.md").write_text(other_noreply + "\n", encoding="utf-8")
        named = self.run_check()
        self.assert_hidden(named, other_noreply, "docs/guide.md:1")

    def test_a_noreply_address_at_a_private_domain_is_rejected(self):
        secret = "noreply@internal.examplecorp.com"
        (self.root / "docs" / "guide.md").write_text(secret + "\n", encoding="utf-8")
        result = self.run_check()
        self.assert_hidden(result, secret, "docs/guide.md:1")

    def test_the_current_release_text_passes_without_an_extra_list(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env.pop("RELEASE_DENYLIST", None)
        result = subprocess.run(
            ["sh", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_generic_infrastructure_patterns_fail_without_printing_the_match(self):
        samples = {
            "DESKTOP-AB12": "docs/guide.md:1",
            "build.local": "docs/guide.md:1",
            "/Users/someone": "CHANGELOG.md:1",
            "/home/someone": "CHANGELOG.md:1",
            "C:\\Users\\someone": "README.md:1",
            "10.1.2.3": "docs/guide.md:1",
            "192.168.1.9": "docs/guide.md:1",
            "172.16.0.1": "docs/guide.md:1",
            "-----BEGIN " + "PRIVATE KEY-----": "docs/guide.md:1",
            "ghp_" + ("A" * 20): "docs/guide.md:1",
        }
        targets = {
            "CHANGELOG.md:1": self.root / "CHANGELOG.md",
            "README.md:1": self.root / "README.md",
            "docs/guide.md:1": self.root / "docs" / "guide.md",
        }
        for secret, report in samples.items():
            for path in targets.values():
                path.write_text("clean\n", encoding="utf-8")
            targets[report].write_text(secret + "\n", encoding="utf-8")
            result = self.run_check()
            self.assert_hidden(result, secret, report)

    def test_public_addresses_and_short_tokens_pass(self):
        (self.root / "docs" / "guide.md").write_text(
            "8.8.8.8\n192.169.0.1\n172.15.0.1\nghp_short\nlocally\n",
            encoding="utf-8",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_optional_patterns_fail_closed_without_echoing_the_pattern(self):
        secret = "QZ9unique"
        (self.root / "docs" / "guide.md").write_text(f"hello {secret}\n", encoding="utf-8")
        from_file = self.run_check(denylist=secret + "\n")
        self.assert_hidden(from_file, secret, "docs/guide.md:1")

        (self.root / "docs" / "guide.md").write_text("Guide\n", encoding="utf-8")
        absent = self.run_check()
        self.assertEqual(absent.returncode, 0, absent.stderr)

        (self.root / "README.md").write_text(f"hello {secret}\n", encoding="utf-8")
        from_env = self.run_check(deny=secret)
        self.assert_hidden(from_env, secret, "README.md:1")

        rejected = self.run_check(denylist="QZ9unique[\n")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn("QZ9unique", rejected.stdout + rejected.stderr)
        self.assertIn("a pattern was rejected", rejected.stderr)

    def test_title_tag_message_and_archives_report_a_location_only(self):
        secret = "DESKTOP-QQ"
        title = self.run_check("--title", secret)
        self.assert_hidden(title, secret, "release-title:1")

        tag = self.run_check("--tag-message", "note " + secret)
        self.assert_hidden(tag, secret, "tag-message:1")

        dist = self.root / "dist"
        dist.mkdir()
        note = "192.168.0.8"
        archive = dist / "demo.tar.gz"
        with tarfile.open(archive, "w:gz") as packed:
            source = Path(self.tmp.name) / "note.txt"
            source.write_text(note + "\n", encoding="utf-8")
            packed.add(source, arcname="note.txt")
        packed_result = self.run_check("--artifacts", "dist")
        self.assert_hidden(packed_result, note, "dist/demo.tar.gz!note.txt:1")

        wheel = dist / "demo.whl"
        with zipfile.ZipFile(wheel, "w") as packed:
            packed.writestr("pkg/readme.txt", note + "\n")
        archive.unlink()
        wheel_result = self.run_check("--artifacts", "dist")
        self.assert_hidden(wheel_result, note, "dist/demo.whl!pkg/readme.txt:1")

    def test_the_public_rule_and_the_workflow_step_precede_publish(self):
        releasing = " ".join((ROOT / "RELEASING.md").read_text(encoding="utf-8").split())
        self.assertIn(
            "Release text and artifacts must not reference private infrastructure, "
            "hostnames, personal paths, or internal tooling.",
            releasing,
        )
        self.assertIn(
            "A noreply address is allowed only at github.com.",
            releasing,
        )
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        check = workflow.index("sh scripts/check-release-text.sh")
        publish = workflow.index("gh release create")
        self.assertLess(check, publish)
        self.assertEqual(workflow.count("sh scripts/check-release-text.sh"), 1)
        self.assertIn("RELEASE_DENYLIST: ${{ secrets.RELEASE_DENYLIST }}", workflow)
        script = SCRIPT.read_text(encoding="utf-8")
        for pattern in (
            "DESKTOP-[A-Z0-9]+",
            r"[A-Za-z0-9-]+\.local\b",
            "/Users/",
            "/home/[a-z]",
            r"C:\\Users",
            "-----BEGIN [A-Z ]*PRIVATE KEY-----",
            "(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}",
            r"192\.168\.[0-9]+\.[0-9]+",
            ALLOW,
        ):
            self.assertIn(pattern, script)
        denylist = Path.home() / ".config" / "moonbase" / "release-denylist.txt"
        if denylist.is_file():
            for line in denylist.read_text(encoding="utf-8").splitlines():
                if line.strip() and line in script:
                    self.fail("an optional denylist pattern is in the script")
                if line.strip() and line in releasing:
                    self.fail("an optional denylist pattern is in the release note")
                if line.strip() and line in workflow:
                    self.fail("an optional denylist pattern is in the workflow")


if __name__ == "__main__":
    unittest.main()
