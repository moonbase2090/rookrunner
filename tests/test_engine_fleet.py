# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from execution_core.engine_fleet import inspect_fleet


class EngineFleetTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "source_commit": "a" * 40,
            "package_version": "0.1.19",
            "source_archive_sha256": "b" * 64,
            "source_tree_sha256": "c" * 64,
            "state_format_version": 1,
            "protocol_versions": [0, 1],
            "planner_capability_version": 12,
            "supported_workflow_features": ["workflow.job"],
        }
        self.inventory = {
            "cohorts": {"stable": self.expected},
            "units": [
                {
                    "name": "worker-a",
                    "host": "host.example",
                    "python": "/usr/bin/python3",
                    "engine_root": "/srv/rookrunner",
                    "state_dir": "/var/lib/rookrunner/state",
                    "cohort": "stable",
                }
            ],
        }
        self.temporary = tempfile.TemporaryDirectory()
        self.inventory_path = Path(self.temporary.name) / "fleet.json"
        self.inventory_path.write_text(json.dumps(self.inventory), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_inspect_reports_actual_identity_and_strips_unapproved_fields(self):
        description = {
            "engine_release": self.expected,
            "capabilities": ["workflow.job", "run.cancel"],
            "key_path": "/private/credential/path",
        }

        def ssh(*args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": description}),
            )

        report = inspect_fleet(self.inventory_path, ssh=ssh)
        self.assertEqual("ok", report[0]["status"])
        self.assertEqual("/var/lib/rookrunner/state", report[0]["state_dir"])
        self.assertEqual(["run.cancel", "workflow.job"], report[0]["capabilities"])
        self.assertNotIn("host", report[0])
        self.assertNotIn("key_path", json.dumps(report))

    def test_inspect_marks_cohort_identity_drift(self):
        actual = dict(self.expected, source_commit="c" * 40)

        def ssh(*args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": {"engine_release": actual, "capabilities": []}}),
            )

        report = inspect_fleet(self.inventory_path, ssh=ssh)
        self.assertEqual("drift", report[0]["status"])
        self.assertEqual(["source_commit"], report[0]["drift"])

    def test_inventory_rejects_credential_fields(self):
        self.inventory["units"][0]["token"] = "must-not-be-accepted"
        self.inventory_path.write_text(json.dumps(self.inventory), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unit fields"):
            inspect_fleet(self.inventory_path)

    def test_inventory_rejects_ssh_option_injection(self):
        self.inventory["units"][0]["host"] = "-oProxyCommand=unsafe"
        self.inventory_path.write_text(json.dumps(self.inventory), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SSH destination"):
            inspect_fleet(self.inventory_path)


if __name__ == "__main__":
    unittest.main()
