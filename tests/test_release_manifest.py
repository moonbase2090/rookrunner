# SPDX-License-Identifier: MIT

import sqlite3
import tempfile
import unittest
from pathlib import Path

from execution_core.release_manifest import make_manifest, source_tree_digest, verify_manifest
from execution_core.state_format import check_existing_state_format, ensure_state_format
from execution_core.worker import Worker


class ReleaseManifestTests(unittest.TestCase):
    def test_manifest_binds_commit_version_and_installed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "worker.py").write_text("worker = 1\n", encoding="utf-8")
            manifest = make_manifest("a" * 40, "b" * 64, package)
            self.assertEqual(manifest, verify_manifest(package, manifest))
            self.assertEqual(manifest["source_tree_sha256"], source_tree_digest(package))

            (package / "worker.py").write_text("worker = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source digest"):
                verify_manifest(package, manifest)

    def test_manifest_refuses_unsupported_state_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "worker.py").write_text("worker = 1\n", encoding="utf-8")
            manifest = make_manifest("a" * 40, "b" * 64, package)
            manifest["state_format_version"] = 2
            with self.assertRaisesRegex(ValueError, "state format"):
                verify_manifest(package, manifest)


class StateFormatTests(unittest.TestCase):
    def test_legacy_database_gets_a_version_marker(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.assertEqual(1, ensure_state_format(database))
        self.assertEqual(
            "1",
            database.execute(
                "SELECT value FROM metadata WHERE key='state_format_version'"
            ).fetchone()[0],
        )

    def test_unknown_state_format_is_refused_without_rewriting_it(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO metadata VALUES ('state_format_version', '99')")
        database.commit()
        with self.assertRaisesRegex(ValueError, "STATE_FORMAT_UNSUPPORTED"):
            ensure_state_format(database)
        self.assertEqual(
            "99",
            database.execute(
                "SELECT value FROM metadata WHERE key='state_format_version'"
            ).fetchone()[0],
        )

    def test_worker_refuses_future_state_before_schema_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            state = root / "state"
            state.mkdir(mode=0o700)
            database_path = state / "runs.sqlite3"
            database = sqlite3.connect(database_path)
            database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            database.execute("INSERT INTO metadata VALUES ('state_format_version', '99')")
            database.execute("CREATE TABLE future_state (id INTEGER PRIMARY KEY)")
            database.commit()
            database.close()
            before = database_path.read_bytes()

            worker = Worker(repository, state)
            with self.assertRaisesRegex(ValueError, "STATE_FORMAT_UNSUPPORTED"):
                worker.start()

            self.assertEqual(before, database_path.read_bytes())

    def test_existing_state_check_accepts_unmarked_legacy_metadata(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO metadata VALUES ('worker_id', 'legacy')")
        check_existing_state_format(database)


if __name__ == "__main__":
    unittest.main()
