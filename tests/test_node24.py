import json
import os
from pathlib import Path
import tempfile
import unittest

from execution_core.actions import tree_digest
from execution_core.node24 import MOUNT, inspect_node24, node_version
from execution_core.run import RunError, run_job
from execution_core.worker import Worker
from schema_support import validate_response


def _distribution(root, version="v24.0.0"):
    binary = root / "bin" / "node"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    binary.chmod(0o755)
    return root


class Node24DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="node24-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_digest_matches_the_tree_and_a_bad_directory_is_refused(self):
        node = _distribution(self.root / "node24")
        inspected = inspect_node24(node)
        self.assertTrue(os.path.samefile(inspected["root"], node))
        self.assertEqual(inspected["digest"], tree_digest(node))
        self.assertEqual(node_version("v24.0.0\n"), "v24.0.0")
        self.assertEqual(node_version("v24.1.2-rc.1\n"), "v24.1.2-rc.1")
        for text in ("v20.0.0\n", "v24.0.0\nextra\n", "not-node\n"):
            with self.assertRaises(ValueError):
                node_version(text)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(inspect_node24("node24")["digest"], inspected["digest"])
        linked = self.root / "linked"
        linked.symlink_to(node, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "node24 directory is not accepted"):
            inspect_node24(linked)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "node24 directory is not accepted"):
            inspect_node24(empty)
        escaped = self.root / "escaped"
        (escaped / "bin").mkdir(parents=True)
        (escaped / "bin" / "node").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "node24 directory is not accepted"):
            inspect_node24(escaped)
        (node / "bin" / "extra").write_bytes(b"x")
        with self.assertRaises(RunError) as caught:
            run_job(
                "missing",
                "ab" * 32,
                "missing",
                {},
                "sha256:" + "cd" * 32,
                {},
                node24=(inspected["root"], inspected["digest"]),
            )
        self.assertEqual(caught.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(caught.exception), "node directory changed")
        self.assertNotIn(str(node), str(caught.exception))

    def test_describe_reports_node24_only_when_the_flag_is_set(self):
        repo = self.root / "repo"
        repo.mkdir()
        state = self.root / "state"
        plain = Worker(repo, state)
        plain.start()
        try:
            described = plain.dispatch("worker.describe", {})
            self.assertNotIn("node24", described)
            validate_response("worker.describe", {"jsonrpc": "2.0", "id": 1, "result": described})
        finally:
            plain.close()
        node = _distribution(self.root / "node24")
        inspected = inspect_node24(node)
        worker = Worker(repo, self.root / "state-node", node24=node)
        worker.start()
        try:
            described = worker.dispatch("worker.describe", {})
            self.assertEqual(
                described["node24"],
                {"digest": inspected["digest"], "mount": MOUNT},
            )
            self.assertNotIn(str(inspected["root"]), json.dumps(described))
            validate_response("worker.describe", {"jsonrpc": "2.0", "id": 1, "result": described})
            self.assertEqual(
                described["capabilities"],
                ["development.fixture", "run.cancel", "run.logs", "workflow.job"],
            )
        finally:
            worker.close()
        with self.assertRaisesRegex(ValueError, "node24 directory is not accepted"):
            Worker(repo, self.root / "state-bad", node24=self.root / "missing")
