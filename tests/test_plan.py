import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_core.plan import PlanError, plan_snapshot, plan_workflow
from execution_core.protocol import canonical
from execution_core.snapshot import SourceCapture


WORKFLOW = """\
name: demo
on: yes
defaults:
  run:
    shell: bash
    working-directory: repo
env:
  ROOT: root
jobs:
  build:
    name: Build
    runs-on: ubuntu-latest
    defaults:
      run:
        shell: sh
        working-directory: app
    env:
      JOB: job-value
    steps:
      - id: one
        name: first
        run: echo ${{ github.sha }}
        shell: bash
        working-directory: src
        env:
          STEP: ${{ vars.MODE }}
      - run: echo second
"""


def line_of(snippet):
    for index, line in enumerate(WORKFLOW.splitlines(), start=1):
        if snippet in line:
            return index
    raise AssertionError(snippet)


class PlanTests(unittest.TestCase):
    def plan(self, workflow=WORKFLOW, job_id="build"):
        return plan_workflow(workflow.encode(), job_id)

    def test_selected_job_plans_with_stable_digest(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        plan = first["plan"]
        self.assertEqual(plan["capability_version"], 1)
        self.assertEqual(plan["job"]["id"], "build")
        self.assertEqual(plan["job"]["name"], "Build")
        self.assertEqual(plan["job"]["runs_on"], "ubuntu-latest")
        self.assertEqual(plan["workflow"]["name"], "demo")
        self.assertEqual(plan["workflow"]["on"], "yes")
        self.assertIsInstance(plan["workflow"]["on"], str)
        self.assertIn('"on":"yes"', canonical(plan))
        self.assertEqual(
            plan["workflow"]["defaults"],
            {"shell": "bash", "working_directory": "repo"},
        )
        self.assertEqual(plan["workflow"]["env"], {"ROOT": "root"})
        self.assertEqual(
            plan["job"]["defaults"],
            {"shell": "sh", "working_directory": "app"},
        )
        self.assertEqual(plan["job"]["env"], {"JOB": "job-value"})
        self.assertEqual([step["index"] for step in plan["job"]["steps"]], [0, 1])
        first_step, second_step = plan["job"]["steps"]
        self.assertEqual(first_step["id"], "one")
        self.assertEqual(first_step["name"], "first")
        self.assertEqual(first_step["run"], "echo ${{ github.sha }}")
        self.assertEqual(first_step["shell"], "bash")
        self.assertEqual(first_step["working_directory"], "src")
        self.assertEqual(first_step["env"], {"STEP": "${{ vars.MODE }}"})
        self.assertEqual(first_step["location"]["line"], line_of("id: one"))
        self.assertGreaterEqual(first_step["location"]["column"], 1)
        self.assertIsNone(second_step["shell"])
        self.assertIsNone(second_step["working_directory"])
        self.assertEqual(second_step["env"], {})
        self.assertEqual(second_step["run"], "echo second")
        encoded = canonical(plan).encode("ascii")
        self.assertEqual(first["digest"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(len(first["digest"]), 64)

    def test_nested_on_and_expressions_stay_text(self):
        workflow = """\
on:
  push:
    branches:
      - main
jobs:
  test:
    steps:
      - run: |
          echo ${{ github.sha }}
"""
        plan = plan_workflow(workflow.encode(), "test")["plan"]
        self.assertEqual(plan["workflow"]["on"], {"push": {"branches": ["main"]}})
        script = plan["job"]["steps"][0]["run"]
        self.assertEqual(script, "echo ${{ github.sha }}\n")
        self.assertNotIn("github.sha", script.replace("${{ github.sha }}", ""))

    def test_duplicate_yaml_key_fails(self):
        workflow = """\
on: push
on: pull_request
jobs:
  test:
    steps:
      - run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "test")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("duplicate YAML key", str(raised.exception))
        self.assertIn("on", str(raised.exception))

    def test_no_selected_job(self):
        missing = """\
on: push
jobs:
  test:
    steps:
      - run: echo hi
"""
        cases = [
            (WORKFLOW, ""),
            (WORKFLOW, None),
            (missing, "build"),
            ("on: push\njobs: {}\n", "test"),
            ("on: push\nname: demo\n", "test"),
        ]
        for workflow, job_id in cases:
            with self.subTest(job_id=job_id):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(workflow.encode(), job_id)
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
                self.assertIn("no selected job", str(raised.exception))

    def test_job_must_be_sequential_run_steps(self):
        cases = [
            """\
jobs:
  test:
    steps: []
""",
            """\
jobs:
  test:
    name: idle
""",
            """\
jobs:
  test:
    steps:
      - name: only
""",
        ]
        for workflow in cases:
            with self.subTest(workflow=workflow):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(workflow.encode(), "test")
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
                self.assertIn("not sequential run steps", str(raised.exception))

    def test_unsupported_fields_name_the_field(self):
        cases = {
            "jobs.test.steps.0.uses": """\
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
""",
            "jobs.test.uses": """\
jobs:
  test:
    uses: example/repo/.github/workflows/ci.yml@main
""",
            "jobs.test.needs": """\
jobs:
  test:
    needs: other
    steps:
      - run: echo hi
""",
            "jobs.test.strategy": """\
jobs:
  test:
    strategy:
      matrix:
        py: ['3.11']
    steps:
      - run: echo hi
""",
            "jobs.test.matrix": """\
jobs:
  test:
    matrix:
      py: ['3.11']
    steps:
      - run: echo hi
""",
            "jobs.test.secrets": """\
jobs:
  test:
    secrets:
      TOKEN: value
    steps:
      - run: echo hi
""",
            "jobs.test.services": """\
jobs:
  test:
    services:
      db:
        image: postgres
    steps:
      - run: echo hi
""",
            "jobs.test.container": """\
jobs:
  test:
    container:
      image: ubuntu
      privileged: true
    steps:
      - run: echo hi
""",
            "jobs.test.privileged": """\
jobs:
  test:
    privileged: true
    steps:
      - run: echo hi
""",
            "on.workflow_call": """\
on:
  workflow_call:
jobs:
  test:
    steps:
      - run: echo hi
""",
            "jobs.other": """\
jobs:
  test:
    steps:
      - run: echo hi
  other:
    steps:
      - run: echo there
""",
            "jobs.test.steps.0.if": """\
jobs:
  test:
    steps:
      - if: ${{ false }}
        run: echo hi
""",
        }
        for field, workflow in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(workflow.encode(), "test")
                self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(raised.exception.field, field)
                self.assertIn(field, str(raised.exception))
                self.assertIn("capability is unsupported", str(raised.exception))

    def test_yaml_alias_is_rejected(self):
        workflow = """\
jobs:
  test:
    steps:
      - &step
        run: echo hi
      - *step
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "test")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("aliases", str(raised.exception))

    def test_planner_launches_no_process(self):
        def fail(*_args, **_kwargs):
            raise AssertionError("process launch")

        with (
            patch("subprocess.Popen", fail),
            patch("subprocess.run", fail),
            patch("os.system", fail),
        ):
            planned = self.plan()
            with self.assertRaises(PlanError):
                plan_workflow(
                    b"jobs:\n  build:\n    steps:\n      - uses: example/action@v1\n", "build"
                )
        self.assertEqual(planned["plan"]["job"]["id"], "build")

    def test_snapshot_bytes_match_the_captured_file(self):
        with tempfile.TemporaryDirectory(prefix="plan-test-") as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            workflow = repo / ".github" / "workflows" / "test.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(WORKFLOW)
            capture = SourceCapture(repo, root / "state")
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
            result = capture.capture(".github/workflows/test.yml")
            snapshot = root / "state" / "snapshots" / result["snapshot_id"]
            captured = (snapshot / "files" / ".github" / "workflows" / "test.yml").read_bytes()
            self.assertEqual(captured, WORKFLOW.encode())

            def fail(*_args, **_kwargs):
                raise AssertionError("process launch")

            with (
                patch("subprocess.Popen", fail),
                patch("subprocess.run", fail),
                patch("os.system", fail),
            ):
                from_snapshot = plan_snapshot(snapshot, "build")
            self.assertEqual(from_snapshot, plan_workflow(captured, "build"))
            manifest = json.loads((snapshot / "manifest.json").read_text())
            self.assertEqual(manifest["workflow"], ".github/workflows/test.yml")
            self.assertFalse((root / "state" / "worker.sock").exists())


if __name__ == "__main__":
    unittest.main()
