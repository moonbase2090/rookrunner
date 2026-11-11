# SPDX-License-Identifier: MIT

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from execution_core.engine_rollout import rollout


class EngineRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "engine.tar.gz"
        self.bundle.write_bytes(b"fixed release bytes")
        self.installer = self.root / "engine-release.py"
        self.installer.write_text("installer", encoding="utf-8")
        expected = {
            "source_commit": "a" * 40,
            "package_version": "0.1.19",
            "source_archive_sha256": "b" * 64,
            "source_tree_sha256": "c" * 64,
            "state_format_version": 1,
            "protocol_versions": [0, 1],
            "planner_capability_version": 12,
            "supported_workflow_features": ["workflow.job"],
        }
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text(
            json.dumps(
                {
                    "cohorts": {"stable": expected},
                    "units": [
                        {
                            "name": "canary",
                            "host": "operator@worker-a",
                            "python": "/usr/bin/python3",
                            "engine_root": "/var/lib/rookrunner",
                            "state_dir": "/var/lib/rookrunner/state",
                            "cohort": "stable",
                        },
                        {
                            "name": "worker-b",
                            "host": "operator@worker-b",
                            "python": "/usr/bin/python3",
                            "engine_root": "/var/lib/rookrunner",
                            "state_dir": "/var/lib/rookrunner/state",
                            "cohort": "stable",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.evidence = {
            "engine_commit": "a" * 40,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "commit_sha": "d" * 40,
            "run_state": "succeeded",
            "exit_code": 0,
            "input_digest": "e" * 64,
            "input_digest_verified": True,
            "status_disposition": "posted",
            "second_poll": "clean",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_canary_gate_precedes_activation_of_remaining_units(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            stdout = json.dumps(self.evidence) if command == ["gate"] else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = rollout(
            self.bundle,
            self.installer,
            self.inventory,
            cohort="stable",
            canary_unit="canary",
            engine_commit="a" * 40,
            gate_command=["gate"],
            runner=runner,
        )
        gate_index = calls.index(["gate"])
        canary_restart = next(
            index
            for index, command in enumerate(calls)
            if command[0] == "ssh"
            and command[-1].endswith("systemctl restart rookrunner-worker.service")
            and "worker-a" in command[4]
        )
        remaining_activation = next(
            index
            for index, command in enumerate(calls)
            if command[0] == "ssh" and "activate" in command[-1] and "worker-b" in command[4]
        )
        self.assertLess(canary_restart, gate_index)
        self.assertLess(gate_index, remaining_activation)
        self.assertEqual(["canary", "worker-b"], result["activated_units"])
        self.assertEqual(self.evidence, result["canary"])

    def test_failed_canary_does_not_activate_remaining_units(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            failed = {**self.evidence, "second_poll": "dirty"}
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(failed) if command == ["gate"] else "",
                stderr="",
            )

        with self.assertRaisesRegex(ValueError, "gate did not prove"):
            rollout(
                self.bundle,
                self.installer,
                self.inventory,
                cohort="stable",
                canary_unit="canary",
                engine_commit="a" * 40,
                gate_command=["gate"],
                runner=runner,
            )
        activated = [
            command
            for command in calls
            if command[0] == "ssh" and " activate " in f" {command[-1]} "
        ]
        self.assertEqual(1, len(activated))
        self.assertIn("worker-a", activated[0][4])
        self.assertNotIn("worker-b", activated[0][4])

    def test_gate_requires_verified_digest_and_clean_second_poll(self):
        for mutation in (
            {"input_digest_verified": False},
            {"second_poll": "posted_again"},
            {"exit_code": None},
        ):
            evidence = {**self.evidence, **mutation}

            def runner(command, evidence=evidence, **kwargs):
                stdout = json.dumps(evidence) if command == ["gate"] else ""
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "gate did not prove"),
            ):
                rollout(
                    self.bundle,
                    self.installer,
                    self.inventory,
                    cohort="stable",
                    canary_unit="canary",
                    engine_commit="a" * 40,
                    gate_command=["gate"],
                    runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
