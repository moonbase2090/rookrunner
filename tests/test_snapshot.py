import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from execution_core.snapshot import CaptureError, SourceCapture


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.capture = SourceCapture(self.repo, self.state)
        self.capture.git("init", "--initial-branch=main")
        self.write(".github/workflows/test.yml", "name: fixture\non: push\njobs: {}\n")
        self.write("source.txt", "original\n")
        self.capture.git("add", ".")
        self.capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def snapshot(self, include=()):
        result = self.capture.capture(".github/workflows/test.yml", include)
        location = self.state / "snapshots" / result["snapshot_id"]
        manifest_bytes = (location / "manifest.json").read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), result["digest"])
        manifest = json.loads(manifest_bytes)
        for entry in manifest["entries"]:
            path = location / "files" / entry["path"]
            contents = (
                os.readlink(path).encode() if entry["kind"] == "symlink" else path.read_bytes()
            )
            self.assertEqual(hashlib.sha256(contents).hexdigest(), entry["sha256"])
        return result, manifest, location / "files"

    def assert_rejected(self, kind, include=()):
        with self.assertRaises(CaptureError) as raised:
            self.capture.capture(".github/workflows/test.yml", include)
        self.assertEqual(raised.exception.kind, kind)
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])

    def test_clean_content_digests_and_later_checkout_edits(self):
        result, manifest, files = self.snapshot()
        self.assertFalse(result["dirty"])
        self.assertEqual(
            result["base_commit"], self.capture.git("rev-parse", "HEAD").stdout.decode().strip()
        )
        self.assertEqual(
            result["workflow_digest"],
            hashlib.sha256((files / ".github/workflows/test.yml").read_bytes()).hexdigest(),
        )
        self.write("source.txt", "later\n")
        self.assertEqual((files / "source.txt").read_text(), "original\n")
        next_result, _, _ = self.snapshot()
        self.assertNotEqual(next_result["digest"], result["digest"])
        self.assertTrue(next_result["dirty"])
        self.assertFalse((files / ".git").exists())
        self.assertEqual(manifest["deleted"], [])

    def test_repeated_capture_has_stable_digest_and_separate_storage(self):
        first, _, _ = self.snapshot()
        second, _, _ = self.snapshot()
        self.assertEqual(first["digest"], second["digest"])
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])

    def test_working_changes_modes_symlinks_and_explicit_untracked(self):
        self.write("source.txt", "working changes\n").chmod(0o755)
        self.write("explicit.txt", "included\n")
        self.write("not-selected.txt", "excluded\n")
        (self.repo / "link.txt").symlink_to("source.txt")
        self.capture.git("add", "link.txt")
        result, _, files = self.snapshot(["explicit.txt"])
        self.assertTrue(result["dirty"])
        self.assertEqual((files / "link.txt").read_text(), "working changes\n")
        self.assertTrue((files / "source.txt").stat().st_mode & stat.S_IXUSR)
        self.assertEqual((files / "explicit.txt").read_text(), "included\n")
        self.assertFalse((files / "not-selected.txt").exists())

    def test_deletions_are_recorded_for_worktree_and_index(self):
        self.write("removed.txt", "remove\n")
        self.capture.git("add", "removed.txt")
        self.capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "add removed",
        )
        self.capture.git("rm", "removed.txt")
        (self.repo / "source.txt").unlink()
        result, manifest, files = self.snapshot()
        self.assertTrue(result["dirty"])
        self.assertEqual(manifest["deleted"], ["removed.txt", "source.txt"])
        self.assertFalse((files / "source.txt").exists())

    def test_credentials_and_custom_state_are_excluded_even_if_tracked(self):
        for name in (".env", ".env.production", ".aws/credentials", "secret.pem", ".npmrc"):
            self.write(name, "fixture secret placeholder\n")
        self.state = self.repo / "local-state"
        self.write("local-state/never-copy.txt", "execution data\n")
        self.state.chmod(0o700)
        self.capture.git("add", ".")
        self.capture = SourceCapture(self.repo, self.state)
        _, manifest, files = self.snapshot()
        self.assertIn("local-state/never-copy.txt", manifest["excluded"])
        self.assertIn(".env", manifest["excluded"])
        self.assertEqual(
            {e["path"] for e in manifest["entries"]}, {"source.txt", ".github/workflows/test.yml"}
        )
        self.assertFalse((files / "local-state").exists())

    def test_explicit_secrets_cannot_override_exclusions(self):
        self.write(".env", "not for capture")
        self.assert_rejected("SOURCE_EXCLUDED", [".env"])

    def test_absolute_external_and_dangling_symlinks_rejected(self):
        for target in (str(self.root / "external"), "../external", "not-selected.txt"):
            with self.subTest(target=target):
                link = self.repo / "link"
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                self.assert_rejected("CAPABILITY_UNSUPPORTED", ["link"])

    def test_links_to_excluded_inputs_and_cycles_rejected(self):
        self.write(".env", "secret placeholder")
        (self.repo / "link").symlink_to(".env")
        self.assert_rejected("CAPABILITY_UNSUPPORTED", ["link"])
        (self.repo / "link").unlink()
        (self.repo / "link").symlink_to("other")
        (self.repo / "other").symlink_to("link")
        self.assert_rejected("CAPABILITY_UNSUPPORTED", ["link", "other"])

    def test_symlink_parent_cannot_read_outside_source(self):
        self.write("directory/file", "tracked")
        self.capture.git("add", "directory/file")
        (self.repo / "directory/file").unlink()
        (self.repo / "directory").rmdir()
        external = self.root / "external"
        external.mkdir()
        (external / "file").write_text("must not read")
        (self.repo / "directory").symlink_to(external, target_is_directory=True)
        self.assert_rejected("SOURCE_IO_ERROR")

    def test_lfs_pointer_and_materialized_lfs_are_rejected(self):
        self.write(
            "source.txt", "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3\n"
        )
        self.assert_rejected("CAPABILITY_UNSUPPORTED")
        self.write("source.txt", "materialized")
        self.write(".gitattributes", "source.txt filter=lfs\n")
        self.assert_rejected("CAPABILITY_UNSUPPORTED")

    def test_submodules_are_rejected_without_visiting_them(self):
        head = self.capture.git("rev-parse", "HEAD").stdout.decode().strip()
        self.capture.git("update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
        self.assert_rejected("CAPABILITY_UNSUPPORTED")

    def test_mutation_during_capture_rejects_and_removes_staging(self):
        original = self.capture.read_entry
        changed = False

        def read(*args):
            nonlocal changed
            result = original(*args)
            if args[0] == "source.txt" and not changed:
                changed = True
                self.write("source.txt", "changed during capture\n")
            return result

        with patch.object(self.capture, "read_entry", side_effect=read):
            self.assert_rejected("SOURCE_UNSTABLE")

    def test_index_changes_during_capture_are_rejected(self):
        original = self.capture.scan
        changed = False

        def scan(*args):
            nonlocal changed
            result = original(*args)
            if not changed:
                changed = True
                self.write("new.txt", "new index input")
                self.capture.git("add", "new.txt")
            return result

        with patch.object(self.capture, "scan", side_effect=scan):
            self.assert_rejected("SOURCE_UNSTABLE")

    def test_path_traversal_and_special_files_rejected(self):
        for name in ("../external", "/tmp/external", "source.txt/../source.txt", "./source.txt"):
            with self.subTest(name=name), self.assertRaises(CaptureError):
                self.capture.capture(".github/workflows/test.yml", [name])
        os.mkfifo(self.repo / "pipe")
        self.assert_rejected("CAPABILITY_UNSUPPORTED", ["pipe"])

    def test_limits_cleanup_and_missing_inputs(self):
        with patch("execution_core.snapshot.MAX_FILE_BYTES", 2):
            self.assert_rejected("SOURCE_LIMIT")
        with patch("execution_core.snapshot.MAX_BYTES", 2):
            self.assert_rejected("SOURCE_LIMIT")
        with patch("execution_core.snapshot.MAX_FILES", 1):
            self.assert_rejected("SOURCE_LIMIT")
        self.assert_rejected("SOURCE_UNSTABLE", ["missing"])

    def test_no_global_config_or_git_environment_redirection(self):
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.root / "nonexistent"),
                "GIT_INDEX_FILE": str(self.root / "bad-index"),
            },
        ):
            result, _, _ = self.snapshot()
            self.assertFalse(result["dirty"])

    def test_cli_snapshot_is_not_run_acceptance(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(self.state),
                "snapshot",
                "--repository",
                str(self.repo),
                "--workflow",
                ".github/workflows/test.yml",
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertIn("snapshot_id", reply["snapshot"])
        self.assertNotIn("run_id", reply["snapshot"])
        self.assertFalse((self.state / "runs.sqlite3").exists())

    def test_unborn_repository_captures_staged_source(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        capture = SourceCapture(fresh, self.state)
        capture.git("init", "--initial-branch=main")
        (fresh / "workflow.yml").write_text("name: initial\n")
        capture.git("add", "workflow.yml")
        result = capture.capture("workflow.yml")
        self.assertIsNone(result["base_commit"])
        self.assertTrue(result["dirty"])

    def test_attribute_changes_during_capture_are_rejected(self):
        original = self.capture.scan
        changed = False

        def scan(*args):
            nonlocal changed
            result = original(*args)
            if not changed:
                changed = True
                self.write(".gitattributes", "source.txt filter=lfs\n")
            return result

        with patch.object(self.capture, "scan", side_effect=scan):
            self.assert_rejected("SOURCE_UNSTABLE")

    def test_sparse_checkout_is_explicitly_unsupported(self):
        self.capture.git("config", "core.sparseCheckout", "true")
        self.assert_rejected("CAPABILITY_UNSUPPORTED")

    def test_directory_symlinks_are_not_supported(self):
        (self.repo / "link").symlink_to(".github", target_is_directory=True)
        self.assert_rejected("CAPABILITY_UNSUPPORTED", ["link"])

    def test_symlink_normalization_cannot_hide_invalid_traversal(self):
        for target in ("source.txt/../source.txt", "source.txt/", "./source.txt"):
            with self.subTest(target=target):
                link = self.repo / "link"
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                self.assert_rejected("CAPABILITY_UNSUPPORTED", ["link"])

    def test_worktree_git_file_is_not_copied(self):
        worktree = self.root / "worktree"
        self.capture.git("worktree", "add", "--detach", str(worktree))
        capture = SourceCapture(worktree, self.state)
        result = capture.capture(".github/workflows/test.yml")
        self.assertFalse(result["dirty"])
        files = self.state / "snapshots" / result["snapshot_id"] / "files"
        self.assertFalse((files / ".git").exists())
        self.assertEqual((files / "source.txt").read_text(), "original\n")
