from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from execution_core.attempt import AttemptError, materialize_attempt
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

        def fail(*_args, **_kwargs):
            raise AssertionError("process launch")

        with patch("subprocess.run", fail), patch("subprocess.Popen", fail):
            manifest = materialize_attempt(self.snapshot, self.digest, workspace)
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

    def test_checkout_deletion_does_not_change_workspace_bytes(self):
        workspace = self.attempts / "without-checkout"
        shutil.rmtree(self.repo)
        materialize_attempt(self.snapshot, self.digest, workspace)
        self.assertEqual((workspace / "source.txt").read_bytes(), b"original\n")
        self.assertEqual((workspace / "dir" / "nested.txt").read_bytes(), b"nested\n")
        self.assertEqual((workspace / "link.txt").readlink(), Path("source.txt"))
