import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zlib
from unittest.mock import patch

from execution_core.artifacts import written_files
from execution_core.attempt import AttemptError, materialize_attempt
from execution_core.protocol import canonical
from execution_core.snapshot import SourceCapture
from execution_core.verify import VerifyError, verify_snapshot


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attempt-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        workflow = self.repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: fixture\non: push\njobs: {}\n")
        (self.repo / "source.txt").write_text("original\n")
        nested = self.repo / "dir" / "nested.txt"
        nested.parent.mkdir()
        nested.write_text("nested\n")
        tool = self.repo / "tool.sh"
        tool.write_text("#!/bin/sh\necho ok\n")
        tool.chmod(0o755)
        (self.repo / "link.txt").symlink_to("source.txt")
        (self.repo / "chain.txt").symlink_to("link.txt")
        capture = SourceCapture(self.repo, self.state)
        capture.git("init", "--initial-branch=main")
        capture.git("add", ".")
        capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        self.captured = capture.capture(".github/workflows/test.yml")
        self.snapshot = self.state / "snapshots" / self.captured["snapshot_id"]
        self.digest = self.captured["digest"]
        self.attempts = self.state / "attempts"
        self.attempts.mkdir(mode=0o700)

    def test_workspace_copies_snapshot_bytes_not_the_checkout(self):
        (self.repo / "source.txt").write_text("mutated checkout\n")
        (self.repo / "tool.sh").write_text("mutated tool\n")
        workspace = self.attempts / "run-1"

        calls = []
        real_run = subprocess.run

        def spy(args, **kwargs):
            calls.append(list(args))
            return real_run(args, **kwargs)

        with patch("subprocess.run", spy):
            manifest = materialize_attempt(self.snapshot, self.digest, workspace)
        self.assertEqual([call[-2:] for call in calls], [["read-tree", "HEAD"]])
        self.assertIn(str(workspace), calls[0])
        self.assertEqual(manifest["workflow"], ".github/workflows/test.yml")
        source = workspace / "source.txt"
        tool = workspace / "tool.sh"
        nested = workspace / "dir" / "nested.txt"
        self.assertEqual(source.read_bytes(), b"original\n")
        self.assertEqual(tool.read_bytes(), b"#!/bin/sh\necho ok\n")
        self.assertEqual(nested.read_bytes(), b"nested\n")
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((workspace / "dir").stat().st_mode), 0o700)
        self.assertEqual(source.stat().st_nlink, 1)
        self.assertNotEqual(
            source.stat().st_ino, (self.snapshot / "files" / "source.txt").stat().st_ino
        )
        self.assertNotEqual(source.stat().st_ino, (self.repo / "source.txt").stat().st_ino)
        self.assertTrue((workspace / "link.txt").is_symlink())
        self.assertEqual((workspace / "link.txt").readlink(), Path("source.txt"))
        self.assertEqual((workspace / "chain.txt").readlink(), Path("link.txt"))
        self.assertFalse((workspace / "link.txt").readlink().is_absolute())
        self.assertTrue((self.snapshot / "git.json").is_file())
        self.assertTrue((self.snapshot / "objects").is_dir())
        self.assertFalse((workspace / "git.json").exists())
        self.assertFalse((workspace / "objects").exists())
        self._assert_owned_git(workspace, self.snapshot)
        (self.repo / "source.txt").write_text("later checkout\n")
        self.assertEqual(source.read_bytes(), b"original\n")
        self.assertFalse(workspace.is_relative_to(self.snapshot))
        verify_snapshot(self.snapshot, self.digest)

    def test_failed_materialization_removes_the_partial_workspace(self):
        rejected = self.attempts / "bad-digest"
        with self.assertRaises(VerifyError):
            materialize_attempt(self.snapshot, "0" * 64, rejected)
        self.assertFalse(rejected.exists())

        inside = self.snapshot / "attempt"
        with self.assertRaises(AttemptError) as raised:
            materialize_attempt(self.snapshot, self.digest, inside)
        self.assertEqual(raised.exception.kind, "ATTEMPT_INVALID")
        self.assertFalse(inside.exists())
        verify_snapshot(self.snapshot, self.digest)

        keep = self.attempts / "keep"
        keep.mkdir()
        (keep / "keep.txt").write_text("keep\n")
        with self.assertRaises(AttemptError) as raised:
            materialize_attempt(self.snapshot, self.digest, keep)
        self.assertEqual(raised.exception.kind, "ATTEMPT_INVALID")
        self.assertEqual((keep / "keep.txt").read_text(), "keep\n")

        partial = self.attempts / "partial"
        with patch("execution_core.attempt.os.symlink", side_effect=OSError("injected")):
            with self.assertRaises(AttemptError) as raised:
                materialize_attempt(self.snapshot, self.digest, partial)
        self.assertEqual(raised.exception.kind, "ATTEMPT_FAILED")
        self.assertFalse(partial.exists())
        self.assertFalse(partial.is_symlink())
        verify_snapshot(self.snapshot, self.digest)

    def _assert_owned_git(self, workspace, snapshot, *, detached=False):
        git = workspace / ".git"
        self.assertTrue(git.is_dir())
        self.assertFalse(git.is_symlink())
        self.assertEqual(stat.S_IMODE(git.stat().st_mode), 0o700)
        self.assertFalse((workspace / "git.json").exists())
        self.assertFalse((workspace / "objects").exists())
        self.assertFalse((snapshot / ".git").exists())
        metadata = json.loads((snapshot / "git.json").read_text())
        manifest = json.loads((snapshot / "manifest.json").read_text())
        head = (git / "HEAD").read_text()
        config = (git / "config").read_text()
        self.assertNotIn(metadata["base_commit"], head)
        self.assertNotIn("fixture@example.invalid", config)
        self.assertNotIn("user.name", config)
        self.assertNotIn("http.extraheader", config)
        self.assertNotIn("remote", config)
        self.assertIn("logallrefupdates = false", config)
        self.assertIn("ignorecase = false", config)
        self.assertFalse((git / "hooks").exists())
        self.assertFalse((git / "objects" / "info").exists())
        self.assertFalse((git / "objects" / "pack").exists())
        self.assertTrue((git / "refs").is_dir())
        commit_id = self._commit_id(git)
        if detached:
            self.assertIsNone(metadata["head"])
            self.assertEqual(head, commit_id + "\n")
            self.assertFalse((git / "refs" / "heads").exists())
        else:
            self.assertEqual(head, f"ref: {metadata['head']}\n")
            self.assertEqual((git / metadata["head"]).read_text(), commit_id + "\n")
        self.assertNotEqual(commit_id, manifest["base_commit"])
        self.assertEqual(stat.S_IMODE((git / "HEAD").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((git / "config").stat().st_mode), 0o600)
        payload = self._commit_payload(git, commit_id)
        self.assertIn(b"author Rookrunner <rookrunner@example.invalid> 0 +0000\n", payload)
        self.assertNotIn(b"parent ", payload)
        self.assertNotIn(b"Fixture", payload)
        listed = written_files(workspace, snapshot)
        self.assertFalse(
            any(item["path"] == ".git" or item["path"].startswith(".git/") for item in listed)
        )

    def _commit_id(self, git):
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            return (git / head[len("ref: ") :]).read_text().strip()
        return head

    def _commit_payload(self, git, commit_id):
        stored = (git / "objects" / commit_id[:2] / commit_id[2:]).read_bytes()
        raw = zlib.decompress(stored)
        _header, payload = raw.split(b"\0", 1)
        return payload

    def test_owned_git_tracks_a_dirty_file_without_resetting_it(self):
        self.write_dirty_and_materialize("dirty-bytes\n")

    def write_dirty_and_materialize(self, text):
        repo = self.root / "dirty-repo"
        repo.mkdir()
        capture = SourceCapture(repo, self.state)
        capture.git("init", "--initial-branch=main")
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: fixture\non: push\njobs: {}\n")
        (repo / "source.txt").write_text("original\n")
        capture.git("add", ".")
        capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        (repo / "source.txt").write_text(text)
        (repo / "extra.txt").write_text("untracked\n")
        captured = capture.capture(".github/workflows/test.yml", ["extra.txt"])
        snapshot = self.state / "snapshots" / captured["snapshot_id"]
        workspace = self.attempts / "dirty"
        materialize_attempt(snapshot, captured["digest"], workspace)
        self.assertEqual((workspace / "source.txt").read_text(), text)
        self.assertEqual((workspace / "extra.txt").read_text(), "untracked\n")
        self._assert_owned_git(workspace, snapshot)
        status = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout, " M source.txt\n?? extra.txt\n")
        return workspace, snapshot

    def test_detached_head_points_at_the_synthesized_commit(self):
        repo = self.root / "detached"
        repo.mkdir()
        capture = SourceCapture(repo, self.state)
        capture.git("init", "--initial-branch=main")
        workflow = repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: fixture\non: push\njobs: {}\n")
        (repo / "source.txt").write_text("original\n")
        capture.git("add", ".")
        capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        capture.git("checkout", "--detach")
        captured = capture.capture(".github/workflows/test.yml")
        snapshot = self.state / "snapshots" / captured["snapshot_id"]
        workspace = self.attempts / "detached"
        materialize_attempt(snapshot, captured["digest"], workspace)
        self._assert_owned_git(workspace, snapshot, detached=True)
        status = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-b"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(status.stdout.startswith("## HEAD (no branch)"))

    def test_absent_store_writes_no_git_directory(self):
        fresh = self.root / "unborn"
        fresh.mkdir()
        capture = SourceCapture(fresh, self.state)
        capture.git("init", "--initial-branch=main")
        (fresh / "workflow.yml").write_text("name: initial\n")
        capture.git("add", "workflow.yml")
        captured = capture.capture("workflow.yml")
        snapshot = self.state / "snapshots" / captured["snapshot_id"]
        workspace = self.attempts / "unborn"
        materialize_attempt(snapshot, captured["digest"], workspace)
        self.assertFalse((workspace / ".git").exists())
        self.assertFalse((workspace / "git.json").exists())

        shutil.rmtree(self.snapshot / "objects")
        kept = self.attempts / "no-store"
        materialize_attempt(self.snapshot, self.digest, kept)
        self.assertFalse((kept / ".git").exists())
        self.assertEqual((kept / "source.txt").read_text(), "original\n")

    def test_rejected_store_creates_no_workspace(self):
        leaf = next((self.snapshot / "objects").glob("*/*"))
        leaf.write_bytes(b"broken")
        rejected = self.attempts / "broken-store"
        with self.assertRaises(AttemptError) as raised:
            materialize_attempt(self.snapshot, self.digest, rejected)
        self.assertEqual(raised.exception.kind, "ATTEMPT_FAILED")
        self.assertFalse(rejected.exists())
        verify_snapshot(self.snapshot, self.digest)

    def test_store_without_metadata_is_invalid(self):
        (self.snapshot / "git.json").unlink()
        rejected = self.attempts / "no-metadata"
        with self.assertRaises(VerifyError) as raised:
            materialize_attempt(self.snapshot, self.digest, rejected)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_INVALID")
        self.assertFalse(rejected.exists())

    def test_unsafe_head_is_invalid(self):
        metadata = json.loads((self.snapshot / "git.json").read_text())
        metadata["head"] = "refs/heads/../../outside"
        encoded = canonical(metadata).encode()
        os.chmod(self.snapshot / "git.json", 0o600)
        (self.snapshot / "git.json").write_bytes(encoded)
        rejected = self.attempts / "unsafe-head"
        with self.assertRaises(VerifyError) as raised:
            materialize_attempt(self.snapshot, self.digest, rejected)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_INVALID")
        self.assertFalse(rejected.exists())
        self.assertFalse((self.attempts / "outside").exists())

    def test_null_base_with_a_store_is_invalid(self):
        manifest = json.loads((self.snapshot / "manifest.json").read_bytes())
        manifest["base_commit"] = None
        encoded = canonical(manifest).encode()
        (self.snapshot / "manifest.json").write_bytes(encoded)
        metadata = json.loads((self.snapshot / "git.json").read_text())
        metadata["base_commit"] = None
        (self.snapshot / "git.json").write_bytes(canonical(metadata).encode())
        digest = hashlib.sha256(encoded).hexdigest()
        rejected = self.attempts / "null-base"
        with self.assertRaises(VerifyError) as raised:
            materialize_attempt(self.snapshot, digest, rejected)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_INVALID")
        self.assertFalse(rejected.exists())

    def test_sha256_repository_gets_an_owned_git_directory(self):
        repo = self.root / "sha256-repo"
        repo.mkdir()
        capture = SourceCapture(repo, self.state)
        created = capture.git(
            "init",
            "--object-format=sha256",
            "--initial-branch=main",
            allow_failure=True,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        workflow = repo / ".github" / "workflows"
        workflow.mkdir(parents=True)
        (workflow / "test.yml").write_text("name: fixture\non: push\njobs: {}\n")
        (repo / "source.txt").write_text("original\n")
        capture.git("add", ".")
        capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        captured = capture.capture(".github/workflows/test.yml")
        snapshot = self.state / "snapshots" / captured["snapshot_id"]
        workspace = self.attempts / "sha256"
        materialize_attempt(snapshot, captured["digest"], workspace)
        self._assert_owned_git(workspace, snapshot)
        config = (workspace / ".git" / "config").read_text()
        self.assertIn("repositoryformatversion = 1", config)
        self.assertIn("objectformat = sha256", config)
        self.assertEqual(len(self._commit_id(workspace / ".git")), 64)

    def test_read_tree_failure_removes_the_workspace(self):
        failed = subprocess.CompletedProcess(args=["git"], returncode=1, stdout=b"", stderr=b"no")
        workspace = self.attempts / "read-tree"
        with patch("execution_core.attempt.subprocess.run", return_value=failed):
            with self.assertRaises(AttemptError) as raised:
                materialize_attempt(self.snapshot, self.digest, workspace)
        self.assertEqual(raised.exception.kind, "ATTEMPT_FAILED")
        self.assertFalse(workspace.exists())
        verify_snapshot(self.snapshot, self.digest)

    def test_checkout_deletion_does_not_change_workspace_bytes(self):
        workspace = self.attempts / "without-checkout"
        shutil.rmtree(self.repo)
        materialize_attempt(self.snapshot, self.digest, workspace)
        self.assertEqual((workspace / "source.txt").read_bytes(), b"original\n")
        self.assertEqual((workspace / "dir" / "nested.txt").read_bytes(), b"nested\n")
        self.assertEqual((workspace / "link.txt").readlink(), Path("source.txt"))
