# SPDX-License-Identifier: MPL-2.0

"""The v0.1.0 release workflow verifies the tag and publishes three assets."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text()
NOTES = (ROOT / ".github" / "v0.1.0-release-notes.md").read_text()
SIGNERS = (ROOT / ".github" / "allowed_signers").read_text()
SCRIPT = ROOT / ".github" / "verify-tag.sh"


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _repo(path, email):
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.name", "MB2090")
    _git(path, "config", "user.email", email)
    _git(path, "config", "tag.gpgsign", "false")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README").write_text("fixture\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "fixture")
    github = path / ".github"
    github.mkdir()
    shutil.copy(SCRIPT, github / "verify-tag.sh")
    return github


class ReleaseWorkflowTests(unittest.TestCase):
    def test_workflow_is_the_tag_build_and_a_separate_publish_job(self):
        self.assertIn("tags:\n      - v0.1.0\n", WORKFLOW)
        self.assertNotIn("pull_request", WORKFLOW)
        self.assertNotIn("macos", WORKFLOW)
        self.assertNotIn("actions/cache", WORKFLOW)
        self.assertNotIn("actions/setup-node", WORKFLOW)
        self.assertNotIn("hashFiles", WORKFLOW)
        self.assertNotIn("docker", WORKFLOW)
        self.assertNotIn("actions: write", WORKFLOW)
        self.assertNotIn("actions: read", WORKFLOW)
        self.assertNotIn("upload-artifact", WORKFLOW)
        self.assertNotIn("download-artifact", WORKFLOW)
        self.assertEqual(WORKFLOW.count("enable-cache: false"), 2)
        self.assertEqual(WORKFLOW.count("uv build --out-dir dist"), 2)
        self.assertEqual(WORKFLOW.count("contents: write"), 1)
        self.assertEqual(WORKFLOW.count('sh .github/verify-tag.sh "$GITHUB_REF_NAME"'), 2)
        self.assertIn("execution_core-0.1.0-py3-none-any.whl", WORKFLOW)
        self.assertIn("execution_core-0.1.0.tar.gz", WORKFLOW)
        self.assertIn("--prerelease", WORKFLOW)
        self.assertIn("--verify-tag", WORKFLOW)
        self.assertIn(".github/v0.1.0-release-notes.md", WORKFLOW)
        script = SCRIPT.read_text()
        self.assertIn("git verify-tag", script)
        self.assertNotIn("|| true", script)

    def test_notes_and_signers_match_the_preview_plan(self):
        self.assertIn("This artifact is a preview.", NOTES)
        self.assertIn("The version string is 0.1.0.", NOTES)
        self.assertIn("The capability version is 12.", NOTES)
        self.assertIn("Python 3.12", NOTES)
        self.assertIn("PyYAML 6.0.3", NOTES)
        self.assertIn("There is no doctor command.", NOTES)
        self.assertIn("`--allow-untrusted`", NOTES)
        self.assertIn("The App private key is not in the artifact.", NOTES)
        self.assertIn("322824348+mb2090@users.noreply.github.com", SIGNERS)
        self.assertIn('namespaces="git" ssh-ed25519 ', SIGNERS)
        self.assertNotIn("PRIVATE", SIGNERS)

    def test_verify_tag_rejects_unsigned_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsigned = Path(tmp) / "unsigned"
            github = _repo(unsigned, "322824348+mb2090@users.noreply.github.com")
            shutil.copy(ROOT / ".github" / "allowed_signers", github / "allowed_signers")
            _git(unsigned, "tag", "-a", "v0.1.0", "-m", "unsigned")
            rejected = subprocess.run(
                ["sh", str(github / "verify-tag.sh"), "v0.1.0"],
                cwd=unsigned,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

            signed = Path(tmp) / "signed"
            github = _repo(signed, "release-test@example.com")
            key = Path(tmp) / "id"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    str(key),
                    "-N",
                    "",
                    "-C",
                    "release-workflow-test",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            public = key.with_suffix(".pub").read_text().strip()
            (github / "allowed_signers").write_text(
                f'# fixture\nrelease-test@example.com namespaces="git" {public}\n'
            )
            _git(signed, "config", "gpg.format", "ssh")
            _git(signed, "config", "user.signingkey", str(key))
            _git(signed, "tag", "-s", "v0.1.0", "-m", "signed")
            accepted = subprocess.run(
                ["sh", str(github / "verify-tag.sh"), "v0.1.0"],
                cwd=signed,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            shutil.copy(ROOT / ".github" / "allowed_signers", github / "allowed_signers")
            unknown = subprocess.run(
                ["sh", str(github / "verify-tag.sh"), "v0.1.0"],
                cwd=signed,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unknown.returncode, 0)


if __name__ == "__main__":
    unittest.main()
