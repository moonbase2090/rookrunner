import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from execution_core.protocol import canonical
from execution_core.snapshot import SourceCapture
from execution_core.verify import VerifyError, verify_snapshot


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="verify-test-")
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

    def assert_no_attempt(self):
        names = {path.name for path in self.root.rglob("*")}
        self.assertFalse(names & {"attempt", "attempts"})

    def rewrite(self, snapshot, manifest):
        raw = canonical(manifest).encode()
        (snapshot / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_matching_snapshot_is_accepted_without_the_checkout(self):
        shutil.rmtree(self.repo)
        manifest = verify_snapshot(self.snapshot, self.digest)
        self.assertEqual(manifest["workflow"], ".github/workflows/test.yml")
        workflow = next(entry for entry in manifest["entries"] if entry["path"] == "source.txt")
        self.assertEqual(workflow["sha256"], hashlib.sha256(b"original\n").hexdigest())
        tool = next(entry for entry in manifest["entries"] if entry["path"] == "tool.sh")
        self.assertEqual(tool["mode"], "100755")
        link = next(entry for entry in manifest["entries"] if entry["path"] == "link.txt")
        self.assertEqual(link["target"], "source.txt")
        self.assert_no_attempt()

        def fail(*_args, **_kwargs):
            raise AssertionError("process launch")

        with patch("subprocess.run", fail), patch("subprocess.Popen", fail):
            again = verify_snapshot(self.snapshot, self.digest)
        self.assertEqual(again["workflow_digest"], manifest["workflow_digest"])

    def test_changed_byte_mode_or_manifest_field_is_rejected(self):
        byte_copy = self.root / "byte"
        shutil.copytree(self.snapshot, byte_copy, symlinks=True)
        source = byte_copy / "files" / "source.txt"
        source.write_bytes(b"changed\n")
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(byte_copy, self.digest)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_MISMATCH")
        self.assertIn("entry bytes do not match", str(raised.exception))
        self.assert_no_attempt()

        mode_copy = self.root / "mode"
        shutil.copytree(self.snapshot, mode_copy, symlinks=True)
        (mode_copy / "files" / "source.txt").chmod(0o755)
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(mode_copy, self.digest)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_MISMATCH")
        self.assertIn("entry mode does not match", str(raised.exception))
        self.assert_no_attempt()

        field_copy = self.root / "field"
        shutil.copytree(self.snapshot, field_copy, symlinks=True)
        manifest = json.loads((field_copy / "manifest.json").read_text())
        manifest["workflow_digest"] = "0" * 64
        rewritten = self.rewrite(field_copy, manifest)
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(field_copy, self.digest)
        self.assertIn("manifest digest does not match", str(raised.exception))
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(field_copy, rewritten)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_MISMATCH")
        self.assertIn("manifest field is not accepted", str(raised.exception))
        self.assert_no_attempt()

    def test_symlink_to_the_checkout_is_not_followed(self):
        copy = self.root / "linked-file"
        shutil.copytree(self.snapshot, copy, symlinks=True)
        captured = copy / "files" / "source.txt"
        captured.unlink()
        captured.symlink_to(self.repo / "source.txt")
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(copy, self.digest)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_MISMATCH")
        self.assertIn("entry mode does not match", str(raised.exception))
        self.assert_no_attempt()

        manifest_copy = self.root / "linked-manifest"
        shutil.copytree(self.snapshot, manifest_copy, symlinks=True)
        outside = self.root / "outside-manifest.json"
        outside.write_bytes((manifest_copy / "manifest.json").read_bytes())
        (manifest_copy / "manifest.json").unlink()
        (manifest_copy / "manifest.json").symlink_to(outside)
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(manifest_copy, self.digest)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_INVALID")
        self.assertIn("refusing to follow a symlink", str(raised.exception))

        directory_copy = self.root / "linked-dir"
        shutil.copytree(self.snapshot, directory_copy, symlinks=True)
        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "nested.txt").write_text("nested\n")
        stored = directory_copy / "files" / "dir"
        shutil.rmtree(stored)
        stored.symlink_to(outside_dir)
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(directory_copy, self.digest)
        self.assertIn("does not match", str(raised.exception))
        self.assertNotIn("not readable", str(raised.exception))

    def test_symlink_target_outside_the_snapshot_is_rejected(self):
        copy = self.root / "escape"
        shutil.copytree(self.snapshot, copy, symlinks=True)
        outside = self.root / "outside-secret"
        outside.write_text("do not read\n")
        outside.chmod(0)
        link = copy / "files" / "link.txt"
        link.unlink()
        link.symlink_to("../outside-secret")
        manifest = json.loads((copy / "manifest.json").read_text())
        entry = next(item for item in manifest["entries"] if item["path"] == "link.txt")
        target = "../outside-secret"
        data = target.encode()
        entry["target"] = target
        entry["size"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        digest = self.rewrite(copy, manifest)
        with self.assertRaises(VerifyError) as raised:
            verify_snapshot(copy, digest)
        self.assertEqual(raised.exception.kind, "SNAPSHOT_INVALID")
        self.assertIn("symlink escapes the snapshot", str(raised.exception))
        self.assertNotIn("not readable", str(raised.exception))
        self.assert_no_attempt()
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0)
