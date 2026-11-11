# SPDX-License-Identifier: MIT

import sqlite3
import tempfile
import unittest
from pathlib import Path

from execution_core.release_manifest import make_manifest, source_tree_digest, verify_manifest
from execution_core.state_format import ensure_state_format


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


if __name__ == "__main__":
    unittest.main()
