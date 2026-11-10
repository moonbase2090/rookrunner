# SPDX-License-Identifier: MIT

"""A workflow is checked against the deployed engine's capability floor."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from execution_core import __version__
from execution_core.capabilities import (
    CAPABILITIES,
    assess,
    capabilities_used,
    check_workflow,
    engine_version_from_describe,
    require_engine_version,
)
from execution_core.plan import PlanError

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"


def _run(*args):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    return subprocess.run(
        [sys.executable, "-m", "execution_core", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class CapabilityFloorTests(unittest.TestCase):
    def test_catalog_names_the_first_release_that_accepts_each_capability(self):
        self.assertEqual(
            [(item.name, item.minimum_engine) for item in CAPABILITIES],
            [
                ("workflow.job", "0.1.0"),
                ("step.continue-on-error", "0.1.6"),
                ("permissions.id-token-write", "0.1.8"),
                ("upload.retention-days", "0.1.16"),
            ],
        )
        for item in CAPABILITIES:
            self.assertLessEqual(
                tuple(int(part) for part in item.minimum_engine.split(".")),
                tuple(int(part) for part in __version__.split(".")),
                item.name,
            )

    def test_a_run_step_needs_only_the_first_job_engine(self):
        result = check_workflow(
            b"on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n", "build", "0.1.0"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["capabilities"], ["workflow.job"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["message"], "deployed engine 0.1.0 covers this job")

    def test_an_engine_older_than_the_planner_names_workflow_job(self):
        result = check_workflow(
            b"on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n", "build", "0.0.9"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["missing"],
            [
                {
                    "capability": "workflow.job",
                    "minimum_engine": "0.1.0",
                    "field": "jobs",
                }
            ],
        )
        self.assertIn("missing capability workflow.job", result["message"])
        self.assertIn("minimum engine version 0.1.0", result["message"])
        self.assertIn("deployed engine 0.0.9", result["message"])

    def test_continue_on_error_is_refused_until_0_1_6(self):
        workflow = (
            b"on: push\njobs:\n  build:\n    steps:\n"
            b"      - run: echo hi\n        continue-on-error: true\n"
        )
        early = check_workflow(workflow, "build", "0.1.5")
        self.assertFalse(early["ok"])
        self.assertEqual(early["missing"][0]["capability"], "step.continue-on-error")
        self.assertEqual(early["missing"][0]["minimum_engine"], "0.1.6")
        self.assertEqual(early["missing"][0]["field"], "jobs.build.steps.0.continue-on-error")
        self.assertNotIn("upload.retention-days", early["message"])
        covered = check_workflow(workflow, "build", "0.1.6")
        self.assertTrue(covered["ok"])
        self.assertEqual(covered["capabilities"], ["step.continue-on-error", "workflow.job"])

    def test_version_order_is_numeric(self):
        workflow = (
            b"on: push\njobs:\n  build:\n    steps:\n"
            b"      - run: echo hi\n        continue-on-error: true\n"
        )
        self.assertTrue(check_workflow(workflow, "build", "0.1.10")["ok"])

    def test_id_token_write_is_refused_until_0_1_8(self):
        workflow = (
            b"on: push\npermissions:\n  id-token: write\n  contents: read\n"
            b"jobs:\n  build:\n    steps:\n      - run: echo hi\n"
        )
        early = check_workflow(workflow, "build", "0.1.7")
        self.assertEqual(
            early["missing"],
            [
                {
                    "capability": "permissions.id-token-write",
                    "minimum_engine": "0.1.8",
                    "field": "permissions.id-token",
                }
            ],
        )
        self.assertIn("missing capability permissions.id-token-write", early["message"])
        self.assertIn("minimum engine version 0.1.8", early["message"])
        self.assertTrue(check_workflow(workflow, "build", "0.1.8")["ok"])
        self.assertTrue(check_workflow(workflow, "build", "0.1.16")["ok"])
        read_only = check_workflow(
            b"on: push\npermissions:\n  id-token: read\njobs:\n  build:\n    steps:\n      - run: echo hi\n",
            "build",
            "0.1.0",
        )
        self.assertEqual(read_only["capabilities"], ["workflow.job"])

    def test_retention_days_is_refused_until_0_1_16(self):
        workflow = (
            "on: push\njobs:\n  build:\n    steps:\n"
            f"      - uses: {UPLOAD}\n        with:\n"
            "          name: coverage\n          path: coverage.txt\n"
            "          retention-days: 7\n"
        ).encode()
        early = check_workflow(workflow, "build", "0.1.15")
        self.assertEqual(early["missing"][0]["capability"], "upload.retention-days")
        self.assertEqual(early["missing"][0]["minimum_engine"], "0.1.16")
        self.assertEqual(early["missing"][0]["field"], "jobs.build.steps.0.with.retention-days")
        self.assertTrue(check_workflow(workflow, "build", "0.1.16")["ok"])
        without = check_workflow(
            (
                "on: push\njobs:\n  build:\n    steps:\n"
                f"      - uses: {UPLOAD}\n        with:\n"
                "          name: coverage\n          path: coverage.txt\n"
            ).encode(),
            "build",
            "0.1.0",
        )
        self.assertEqual(without["capabilities"], ["workflow.job"])

    def test_several_missing_capabilities_are_all_named(self):
        workflow = (
            b"on: push\npermissions:\n  id-token: write\n"
            b"jobs:\n  build:\n    steps:\n"
            b"      - run: echo one\n        continue-on-error: true\n"
            b"      - run: echo two\n        continue-on-error: false\n"
        )
        result = check_workflow(workflow, "build", "0.1.5")
        self.assertEqual(
            [item["capability"] for item in result["missing"]],
            ["permissions.id-token-write", "step.continue-on-error"],
        )
        self.assertEqual(result["missing"][1]["field"], "jobs.build.steps.0.continue-on-error")
        self.assertIn("minimum engine version 0.1.8", result["message"])
        self.assertIn("minimum engine version 0.1.6", result["message"])
        self.assertIn("deployed engine 0.1.5", result["message"])

    def test_a_called_job_is_part_of_the_same_floor(self):
        plan = {
            "jobs": [
                {
                    "id": "caller",
                    "call": {
                        "workflow": {"permissions": {"id-token": "write"}},
                        "jobs": [
                            {
                                "id": "inner",
                                "steps": [
                                    {
                                        "index": 0,
                                        "continue_on_error": True,
                                        "upload": "files",
                                        "with": {"retention-days": "7"},
                                    }
                                ],
                            }
                        ],
                    },
                }
            ]
        }
        result = assess(plan, "0.1.0")
        fields = {item["capability"]: item["field"] for item in result["missing"]}
        self.assertEqual(
            fields["step.continue-on-error"],
            "jobs.caller.call.jobs.inner.steps.0.continue-on-error",
        )
        self.assertEqual(
            fields["permissions.id-token-write"],
            "jobs.caller.call.permissions.id-token",
        )
        self.assertEqual(
            fields["upload.retention-days"],
            "jobs.caller.call.jobs.inner.steps.0.with.retention-days",
        )
        self.assertNotIn("workflow.job", fields)

    def test_the_current_package_covers_every_catalog_capability(self):
        workflow = (
            "on: push\npermissions:\n  id-token: write\n"
            "jobs:\n  build:\n    steps:\n"
            "      - run: echo hi\n        continue-on-error: true\n"
            f"      - uses: {UPLOAD}\n        with:\n"
            "          name: coverage\n          path: coverage.txt\n"
            "          retention-days: 1\n"
        ).encode()
        result = check_workflow(workflow, "build", __version__)
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(
            result["capabilities"],
            [
                "permissions.id-token-write",
                "step.continue-on-error",
                "upload.retention-days",
                "workflow.job",
            ],
        )

    def test_syntax_no_release_accepts_stays_a_plan_error(self):
        with self.assertRaises(PlanError) as raised:
            check_workflow(
                b"on: push\njobs:\n  build:\n    container: alpine\n    steps:\n      - run: echo hi\n",
                "build",
                "0.1.16",
            )
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("container", str(raised.exception))
        self.assertNotIn("minimum engine version", str(raised.exception))

    def test_engine_version_must_be_a_dotted_triple(self):
        for value in ("0.1", "v0.1.6", "0.1.06", "0.1.6 ", "", "latest", None, 12):
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_engine_version(value)
        self.assertEqual(require_engine_version("0.1.16"), "0.1.16")

    def test_describe_version_is_the_deployed_engine(self):
        self.assertEqual(engine_version_from_describe({"version": "0.1.5", "ready": True}), "0.1.5")
        with self.assertRaises(ValueError):
            engine_version_from_describe({"ready": True})
        with self.assertRaises(ValueError):
            engine_version_from_describe({"version": "0.1"})

    def test_a_plan_without_jobs_is_not_assessed(self):
        with self.assertRaises(ValueError):
            capabilities_used({"capability_version": 12})


class CapabilityFloorCommandTests(unittest.TestCase):
    def test_check_refuses_a_capability_the_named_engine_lacks(self):
        path = ROOT / "tests" / "_floor_continue.yml"
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            "on: push\njobs:\n  build:\n    steps:\n"
            "      - run: echo hi\n        continue-on-error: true\n"
        )
        completed = _run(
            "--state",
            str(ROOT / "tests"),
            "check",
            "--workflow",
            str(path),
            "--job-id",
            "build",
            "--engine-version",
            "0.1.5",
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["engine_version"], "0.1.5")
        self.assertEqual(result["engine_version_from"], "flag")
        self.assertEqual(result["missing"][0]["capability"], "step.continue-on-error")
        self.assertEqual(result["missing"][0]["minimum_engine"], "0.1.6")
        self.assertIn("minimum engine version 0.1.6", result["message"])

    def test_check_accepts_a_job_the_named_engine_can_run(self):
        path = ROOT / "tests" / "_floor_run.yml"
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text("on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n")
        completed = _run(
            "--state",
            str(ROOT / "tests"),
            "check",
            "--workflow",
            str(path),
            "--job-id",
            "build",
            "--engine-version",
            "0.1.0",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["capabilities"], ["workflow.job"])

    def test_omitted_engine_version_asks_the_deployed_worker(self):
        path = ROOT / "tests" / "_floor_worker.yml"
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            "on: push\njobs:\n  build:\n    steps:\n"
            "      - run: echo hi\n        continue-on-error: true\n"
        )
        completed = _run(
            "--state",
            str(ROOT / "missing-worker-state"),
            "check",
            "--workflow",
            str(path),
            "--job-id",
            "build",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn('"ok":true', completed.stdout)
        self.assertNotIn("minimum engine version", completed.stdout)

    def test_unsupported_syntax_is_not_given_a_minimum_version(self):
        path = ROOT / "tests" / "_floor_container.yml"
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            "on: push\njobs:\n  build:\n    container: alpine\n    steps:\n      - run: echo hi\n"
        )
        completed = _run(
            "--state",
            str(ROOT / "tests"),
            "check",
            "--workflow",
            str(path),
            "--job-id",
            "build",
            "--engine-version",
            "0.1.16",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        error = json.loads(completed.stderr)
        self.assertEqual(error["error"]["kind"], "CAPABILITY_UNSUPPORTED")
        self.assertIn("container", error["error"]["message"])
        self.assertNotIn("minimum engine version", error["error"]["message"])

    def test_submission_compares_the_plan_with_this_package(self):
        source = (ROOT / "src" / "execution_core" / "worker.py").read_text()
        function = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "_accept_planned":
                function = node
        self.assertIsNotNone(function)
        found = False
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            if not isinstance(call.func, ast.Name) or call.func.id != "assess":
                continue
            self.assertEqual(len(call.args), 2)
            self.assertIsInstance(call.args[1], ast.Name)
            self.assertEqual(call.args[1].id, "__version__")
            found = True
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
