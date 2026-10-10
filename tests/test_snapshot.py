# SPDX-License-Identifier: MPL-2.0

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
import zlib

from execution_core.plan import plan_workflow
from execution_core.protocol import canonical
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
        git_path = location / "git.json"
        git_bytes = git_path.read_bytes()
        self.assertEqual(stat.S_IMODE(git_path.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(git_bytes).hexdigest(), result["git_metadata_digest"])
        metadata = json.loads(git_bytes)
        self.assertEqual(metadata["base_commit"], manifest["base_commit"])
        self.assertEqual(metadata["dirty"], manifest["dirty"])
        self.assertEqual(metadata["git_object_format"], manifest["git_object_format"])
        self.assertNotIn("@", git_bytes.decode())
        self._assert_objects(location, result, manifest)
        for entry in manifest["entries"]:
            path = location / "files" / entry["path"]
            contents = (
                os.readlink(path).encode() if entry["kind"] == "symlink" else path.read_bytes()
            )
            self.assertEqual(hashlib.sha256(contents).hexdigest(), entry["sha256"])
        return result, manifest, location / "files"

    def _assert_objects(self, location, result, manifest, capture=None):
        capture = self.capture if capture is None else capture
        names = sorted(path.name for path in location.iterdir())
        store = location / "objects"
        if result["git_objects_digest"] is None:
            self.assertEqual(names, ["files", "git.json", "manifest.json"])
            self.assertFalse(store.exists())
            return []
        self.assertEqual(names, ["files", "git.json", "manifest.json", "objects"])
        self.assertFalse((location / "HEAD").exists())
        self.assertFalse((store / "info").exists())
        self.assertFalse((store / "pack").exists())
        algorithm = manifest["git_object_format"]
        width = 40 if algorithm == "sha1" else 64
        ids = []
        commits = []
        for bucket in store.iterdir():
            self.assertTrue(bucket.is_dir())
            self.assertEqual(stat.S_IMODE(bucket.stat().st_mode), 0o700)
            self.assertEqual(len(bucket.name), 2)
            for leaf in bucket.iterdir():
                self.assertTrue(leaf.is_file())
                self.assertFalse(leaf.is_symlink())
                self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o600)
                oid = bucket.name + leaf.name
                self.assertEqual(len(oid), width)
                raw = zlib.decompress(leaf.read_bytes())
                header, payload = raw.split(b"\0", 1)
                kind, size = header.split(b" ")
                self.assertIn(kind, (b"blob", b"tree", b"commit"))
                self.assertEqual(int(size), len(payload))
                self.assertEqual(hashlib.new(algorithm, raw).hexdigest(), oid)
                self.assertNotIn(b"fixture@example.invalid", payload)
                self.assertNotIn(b"Fixture", payload)
                if kind == b"commit":
                    commits.append((oid, payload))
                ids.append(oid)
        ids.sort()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(commits), 1)
        commit_id, payload = commits[0]
        root = (
            capture.git("rev-parse", "--verify", f"{manifest['base_commit']}^{{tree}}")
            .stdout.decode("ascii")
            .strip()
        )
        expected = (
            f"tree {root}\n"
            "author Rookrunner <rookrunner@example.invalid> 0 +0000\n"
            "committer Rookrunner <rookrunner@example.invalid> 0 +0000\n"
            "\n"
            "captured tree\n"
        ).encode("ascii")
        self.assertEqual(payload, expected)
        self.assertNotIn(b"parent ", payload)
        hashed = (
            capture.git("hash-object", "-t", "commit", "--stdin", input=payload)
            .stdout.decode("ascii")
            .strip()
        )
        self.assertEqual(hashed, commit_id)
        self.assertNotIn(commit_id, (location / "git.json").read_text())
        self.assertEqual(
            hashlib.sha256(canonical(ids).encode()).hexdigest(),
            result["git_objects_digest"],
        )
        self.assertNotIn(manifest["base_commit"], ids)
        return ids

    def _commit(self, message):
        self.capture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            message,
        )

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
        self.assertEqual(first["git_objects_digest"], second["git_objects_digest"])
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
        recorded = reply["snapshot"]
        git_bytes = (self.state / "snapshots" / recorded["snapshot_id"] / "git.json").read_bytes()
        self.assertEqual(hashlib.sha256(git_bytes).hexdigest(), recorded["git_metadata_digest"])
        self.assertIsNotNone(recorded["git_objects_digest"])
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
        metadata = json.loads(
            (self.state / "snapshots" / result["snapshot_id"] / "git.json").read_text()
        )
        self.assertIsNone(metadata["head"])
        self.assertEqual(
            hashlib.sha256(
                (self.state / "snapshots" / result["snapshot_id"] / "git.json").read_bytes()
            ).hexdigest(),
            result["git_metadata_digest"],
        )
        self.assertIsNone(result["git_objects_digest"])
        self.assertFalse((self.state / "snapshots" / result["snapshot_id"] / "objects").exists())

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

    def test_sanitized_metadata_survives_a_later_checkout_edit(self):
        self.capture.git("remote", "add", "origin", "https://example.invalid/fixture.git")
        credential = self.repo / ".git" / "fixture-credential"
        credential.write_text("fixture-credential\n")
        result, manifest, files = self.snapshot()
        snapshot = files.parent
        self.capture.git("remote", "set-url", "origin", "https://example.invalid/later.git")
        credential.write_text("fixture-credential-later\n")
        manifest_bytes = (snapshot / "manifest.json").read_bytes()
        git_bytes = (snapshot / "git.json").read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), result["digest"])
        self.assertEqual(hashlib.sha256(git_bytes).hexdigest(), result["git_metadata_digest"])
        text = git_bytes.decode()
        for absent in (
            "https://example.invalid/fixture.git",
            "https://example.invalid/later.git",
            "fixture-credential",
            "user.email",
            "fixture@example.invalid",
            "Fixture",
        ):
            self.assertNotIn(absent, text)
        metadata = json.loads(text)
        self.assertEqual(metadata["base_commit"], manifest["base_commit"])
        self.assertEqual(metadata["dirty"], manifest["dirty"])
        self.assertEqual(metadata["git_object_format"], manifest["git_object_format"])
        self.assertEqual(metadata["head"], "refs/heads/main")
        self.assertFalse((files / ".git").exists())
        planned = plan_workflow(
            b"name: fixture\non: push\njobs:\n  build:\n    steps:\n"
            b"      - uses: actions/checkout@v4\n",
            "build",
        )["plan"]
        step = planned["job"]["steps"][0]
        self.assertEqual(step["checkout"], "captured")
        self.assertNotIn("action_path", step)
        major = plan_workflow(
            b"name: fixture\non: push\njobs:\n  build:\n    steps:\n"
            b"      - uses: actions/checkout@v7\n",
            "build",
        )["plan"]
        major_step = major["job"]["steps"][0]
        self.assertEqual(major_step["uses"], "actions/checkout@v7")
        self.assertEqual(major_step["checkout"], "captured")
        self.assertNotIn("action_path", major_step)

    def test_detached_head_stores_null(self):
        self.capture.git("checkout", "--detach")
        result, _, files = self.snapshot()
        metadata = json.loads((files.parent / "git.json").read_text())
        self.assertIsNone(metadata["head"])
        self.assertEqual(metadata["base_commit"], result["base_commit"])
        self.assertIsNotNone(result["base_commit"])

    def test_remote_tracking_head_is_not_copied(self):
        self.capture.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.capture.git("symbolic-ref", "HEAD", "refs/remotes/origin/main")
        result, _, files = self.snapshot()
        git_bytes = (files.parent / "git.json").read_bytes()
        self.assertIsNone(json.loads(git_bytes)["head"])
        self.assertNotIn(b"refs/remotes", git_bytes)
        self.assertIsNotNone(result["base_commit"])

    def test_branch_name_containing_at_is_not_copied(self):
        self.capture.git("checkout", "-b", "user@host")
        _, _, files = self.snapshot()
        git_bytes = (files.parent / "git.json").read_bytes()
        self.assertIsNone(json.loads(git_bytes)["head"])
        self.assertNotIn("@", git_bytes.decode())

    def test_symbolic_ref_change_during_capture_is_unstable(self):
        self.capture.git("branch", "other")
        original = self.capture.symbolic_head
        seen = False

        def head():
            nonlocal seen
            if not seen:
                seen = True
                return original()
            self.capture.git("symbolic-ref", "HEAD", "refs/heads/other")
            return original()

        with patch.object(self.capture, "symbolic_head", side_effect=head):
            self.assert_rejected("SOURCE_UNSTABLE")

    def test_worktree_git_file_is_not_copied(self):
        worktree = self.root / "worktree"
        self.capture.git("worktree", "add", "--detach", str(worktree))
        capture = SourceCapture(worktree, self.state)
        result = capture.capture(".github/workflows/test.yml")
        self.assertFalse(result["dirty"])
        snapshot = self.state / "snapshots" / result["snapshot_id"]
        files = snapshot / "files"
        self.assertFalse((files / ".git").exists())
        self.assertFalse((snapshot / "HEAD").exists())
        self.assertIsNotNone(result["git_objects_digest"])
        self.assertEqual((files / "source.txt").read_text(), "original\n")
        headers = [
            zlib.decompress(leaf.read_bytes()).split(b"\0", 1)[0]
            for bucket in (snapshot / "objects").iterdir()
            for leaf in bucket.iterdir()
        ]
        self.assertEqual(sum(header.startswith(b"commit ") for header in headers), 1)

    def test_dirty_file_keeps_the_committed_blob(self):
        clean, _, _ = self.snapshot()
        self.write("source.txt", "dirty-bytes\n")
        result, _, files = self.snapshot()
        self.assertTrue(result["dirty"])
        self.assertNotEqual(result["digest"], clean["digest"])
        self.assertEqual(result["base_commit"], clean["base_commit"])
        self.assertEqual(result["git_objects_digest"], clean["git_objects_digest"])
        self.assertEqual((files / "source.txt").read_text(), "dirty-bytes\n")
        payloads = []
        store = files.parent / "objects"
        for bucket in store.iterdir():
            for leaf in bucket.iterdir():
                raw = zlib.decompress(leaf.read_bytes())
                _, payload = raw.split(b"\0", 1)
                payloads.append(payload)
        self.assertIn(b"original\n", payloads)
        self.assertNotIn(b"dirty-bytes\n", payloads)

    def test_new_tree_changes_the_synthesized_commit(self):
        first, _, _ = self.snapshot()
        self.write("added.txt", "added\n")
        self.capture.git("add", "added.txt")
        self._commit("add")
        second, second_manifest, _ = self.snapshot()
        self.assertNotEqual(second["base_commit"], first["base_commit"])
        self.assertNotEqual(second["git_objects_digest"], first["git_objects_digest"])
        self.assertNotEqual(second["digest"], first["digest"])
        self.assertEqual(second_manifest["base_commit"], second["base_commit"])

    def test_sha256_repository_stores_one_sha256_commit(self):
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
        result = capture.capture(".github/workflows/test.yml")
        location = self.state / "snapshots" / result["snapshot_id"]
        manifest = json.loads((location / "manifest.json").read_bytes())
        self.assertEqual(manifest["git_object_format"], "sha256")
        self.assertEqual(manifest["base_commit"], result["base_commit"])
        ids = self._assert_objects(location, result, manifest, capture)
        self.assertTrue(all(len(oid) == 64 for oid in ids))

    def test_excluded_tree_path_stores_no_objects(self):
        self.write(".env", "not-a-secret\n")
        self.capture.git("add", ".env")
        self._commit("exclude")
        result, manifest, _ = self.snapshot()
        self.assertIsNone(result["git_objects_digest"])
        self.assertIn(".env", manifest["excluded"])
        self.assertNotIn(".env", {entry["path"] for entry in manifest["entries"]})

    def test_parent_commit_and_removed_blob_are_not_copied(self):
        self.write("old.txt", "old-blob\n")
        self.capture.git("add", "old.txt")
        self._commit("add old")
        parent = self.capture.git("rev-parse", "HEAD").stdout.decode().strip()
        old = self.capture.git("rev-parse", "HEAD:old.txt").stdout.decode().strip()
        self.capture.git("rm", "old.txt")
        self._commit("remove old")
        _, _, files = self.snapshot()
        ids = []
        for bucket in (files.parent / "objects").iterdir():
            ids.extend(bucket.name + leaf.name for leaf in bucket.iterdir())
        self.assertNotIn(parent, ids)
        self.assertNotIn(old, ids)
        self.assertNotIn(
            b"old-blob\n",
            b"".join(
                zlib.decompress(leaf.read_bytes())
                for bucket in (files.parent / "objects").iterdir()
                for leaf in bucket.iterdir()
            ),
        )

    def test_empty_alternates_file_still_copies_objects(self):
        info = self.repo / ".git" / "objects" / "info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_bytes(b"")
        result, _, _ = self.snapshot()
        self.assertIsNotNone(result["git_objects_digest"])

    def test_nonempty_alternates_file_is_rejected(self):
        info = self.repo / ".git" / "objects" / "info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_text("../outside\n")
        with self.assertRaises(CaptureError) as raised:
            self.capture.capture(".github/workflows/test.yml")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("alternate", str(raised.exception))
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])

    def test_alternates_symlink_is_rejected(self):
        info = self.repo / ".git" / "objects" / "info"
        info.mkdir(exist_ok=True)
        (info / "alternates").symlink_to("unused")
        self.assert_rejected("SOURCE_INVALID")

    def test_alternates_directory_is_rejected(self):
        info = self.repo / ".git" / "objects" / "info"
        info.mkdir(exist_ok=True)
        (info / "alternates").mkdir()
        self.assert_rejected("SOURCE_INVALID")

    def test_alternates_change_during_capture_is_unstable(self):
        original = self.capture._alternates
        seen = False

        def flip():
            nonlocal seen
            if not seen:
                seen = True
                return original()
            return "nonempty"

        with patch.object(self.capture, "_alternates", side_effect=flip):
            self.assert_rejected("SOURCE_UNSTABLE")

    def test_missing_blob_is_invalid_and_publishes_nothing(self):
        oid = self.capture.git("rev-parse", "HEAD:source.txt").stdout.decode().strip()
        loose = self.repo / ".git" / "objects" / oid[:2] / oid[2:]
        self.assertTrue(loose.is_file())
        loose.unlink()
        self.assert_rejected("SOURCE_INVALID")

    def test_committed_symlink_blob_is_copied(self):
        (self.repo / "link.txt").symlink_to("source.txt")
        self.capture.git("add", "link.txt")
        self._commit("link")
        _, _, files = self.snapshot()
        self.assertEqual((files / "link.txt").read_text(), "original\n")
        payloads = [
            zlib.decompress(leaf.read_bytes()).split(b"\0", 1)[1]
            for bucket in (files.parent / "objects").iterdir()
            for leaf in bucket.iterdir()
        ]
        self.assertIn(b"source.txt", payloads)
