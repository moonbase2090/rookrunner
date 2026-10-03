import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_core.plan import (
    DEFAULT_JOB_TIMEOUT_MINUTES,
    MAX_JOB_TIMEOUT_MINUTES,
    MAX_STEP_TIMEOUT_MINUTES,
    MAX_WORKFLOW_BYTES,
    PlanError,
    plan_snapshot,
    plan_workflow,
)
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
        self.assertEqual(plan["capability_version"], 3)
        self.assertEqual(plan["job"]["id"], "build")
        self.assertEqual(plan["job"]["needs"], [])
        self.assertEqual(plan["job"]["outputs"], {})
        self.assertNotIn("if", plan["job"])
        self.assertEqual([item["id"] for item in plan["jobs"]], ["build"])
        self.assertIs(plan["jobs"][-1], plan["job"])
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
        self.assertEqual(plan["job"]["timeout_minutes"], DEFAULT_JOB_TIMEOUT_MINUTES)
        self.assertEqual([step["index"] for step in plan["job"]["steps"]], [0, 1])
        first_step, second_step = plan["job"]["steps"]
        self.assertEqual(first_step["id"], "one")
        self.assertEqual(first_step["name"], "first")
        self.assertEqual(first_step["run"], "echo ${{ github.sha }}")
        self.assertEqual(first_step["shell"], "bash")
        self.assertEqual(first_step["working_directory"], "src")
        self.assertEqual(first_step["env"], {"STEP": "${{ vars.MODE }}"})
        self.assertNotIn("timeout_minutes", first_step)
        self.assertNotIn("timeout_minutes", second_step)
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

    def test_step_records_have_no_count_cap(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/v0/contract.schema.json").read_text()
        )
        steps_schema = schema["$defs"]["Run"]["properties"]["steps"]
        self.assertNotIn("maxItems", steps_schema)
        count = 300
        lines = "\n".join(f"      - run: echo {index}" for index in range(count))
        workflow = f"on: push\njobs:\n  build:\n    steps:\n{lines}\n"
        planned = plan_workflow(workflow.encode(), "build")
        self.assertEqual(len(planned["plan"]["job"]["steps"]), count)
        self.assertLess(len(workflow.encode()), MAX_WORKFLOW_BYTES)

    def test_workflow_file_over_500kb_is_a_capability_error(self):
        body = b"on: push\njobs:\n  build:\n    steps:\n      - run: echo ok\n"
        pad = MAX_WORKFLOW_BYTES - len(body)
        exact = body + b"#" + b"x" * (pad - 1)
        self.assertEqual(len(exact), MAX_WORKFLOW_BYTES)
        planned = plan_workflow(exact, "build")
        self.assertEqual(len(planned["plan"]["job"]["steps"]), 1)
        with self.assertRaises(PlanError) as raised:
            plan_workflow(exact + b"\n", "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("500 KB", str(raised.exception))
        self.assertIn("docs.github.com/en/actions/reference/limits", str(raised.exception))

    def test_job_time_bound_is_timeout_minutes(self):
        def workflow(minutes):
            return f"on: push\njobs:\n  build:\n    timeout-minutes: {minutes}\n    steps:\n      - run: echo ok\n"

        short = plan_workflow(workflow(10).encode(), "build")
        self.assertEqual(short["plan"]["job"]["timeout_minutes"], 10)
        hosted = plan_workflow(workflow(DEFAULT_JOB_TIMEOUT_MINUTES).encode(), "build")
        self.assertEqual(hosted["plan"]["job"]["timeout_minutes"], DEFAULT_JOB_TIMEOUT_MINUTES)
        ceiling = plan_workflow(workflow(MAX_JOB_TIMEOUT_MINUTES).encode(), "build")
        self.assertEqual(ceiling["plan"]["job"]["timeout_minutes"], MAX_JOB_TIMEOUT_MINUTES)
        above_hosted = plan_workflow(workflow(DEFAULT_JOB_TIMEOUT_MINUTES + 1).encode(), "build")
        self.assertEqual(
            above_hosted["plan"]["job"]["timeout_minutes"],
            DEFAULT_JOB_TIMEOUT_MINUTES + 1,
        )
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow(MAX_JOB_TIMEOUT_MINUTES + 1).encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("5 day", str(raised.exception))
        self.assertIn("docs.github.com/en/actions/reference/limits", str(raised.exception))
        for minutes in ("0", "-1", "'30'", "1.5"):
            with self.subTest(minutes=minutes):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(workflow(minutes).encode(), "build")
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")

    def test_step_timeout_minutes_is_recorded_up_to_360(self):
        workflow = """\
on: push
jobs:
  build:
    timeout-minutes: 1
    steps:
      - timeout-minutes: 1
        run: echo ok
      - run: echo later
"""
        planned = plan_workflow(workflow.encode(), "build")
        steps = planned["plan"]["job"]["steps"]
        self.assertEqual(steps[0]["timeout_minutes"], 1)
        self.assertNotIn("timeout_minutes", steps[1])
        self.assertEqual(planned["plan"]["job"]["timeout_minutes"], 1)
        longer_than_job = workflow.replace(
            "      - timeout-minutes: 1\n", "      - timeout-minutes: 360\n"
        )
        accepted = plan_workflow(longer_than_job.encode(), "build")
        self.assertEqual(
            accepted["plan"]["job"]["steps"][0]["timeout_minutes"], MAX_STEP_TIMEOUT_MINUTES
        )
        self.assertEqual(accepted["plan"]["job"]["timeout_minutes"], 1)
        above = workflow.replace("      - timeout-minutes: 1\n", "      - timeout-minutes: 361\n")
        with self.assertRaises(PlanError) as raised:
            plan_workflow(above.encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.steps.0.timeout-minutes")
        self.assertIn("360", str(raised.exception))
        self.assertIn("workflow-syntax", str(raised.exception))
        for minutes in ("0", "-1", "'30'", "1.5"):
            with self.subTest(minutes=minutes):
                body = workflow.replace(
                    "      - timeout-minutes: 1\n", f"      - timeout-minutes: {minutes}\n"
                )
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(body.encode(), "build")
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")

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
            "jobs.test.steps.0.needs": """\
jobs:
  test:
    steps:
      - needs: other
        run: echo hi
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
        }
        for field, workflow in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(workflow.encode(), "test")
                self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(raised.exception.field, field)
                self.assertIn(field, str(raised.exception))
                self.assertIn("capability is unsupported", str(raised.exception))

    def test_step_if_is_stored_and_not_evaluated(self):
        workflow = """\
jobs:
  test:
    steps:
      - if: ${{ github.event.kind == 'local' }}
        run: echo hi
      - if: false
        run: echo no
"""
        steps = plan_workflow(workflow.encode(), "test")["plan"]["job"]["steps"]
        self.assertEqual(steps[0]["if"], "${{ github.event.kind == 'local' }}")
        self.assertEqual(steps[0]["run"], "echo hi")
        self.assertEqual(steps[1]["if"], "false")
        self.assertNotIn("result", steps[0])

    def test_unavailable_context_and_hashfiles_are_rejected(self):
        secret = """\
jobs:
  test:
    steps:
      - if: secrets.NAME
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(secret.encode(), "test")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.test.steps.0.if")
        self.assertIn("context is not available: secrets", str(raised.exception))
        hashed = """\
jobs:
  test:
    steps:
      - if: hashFiles('*.txt')
        run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(hashed.encode(), "test")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.test.steps.0.if")
        self.assertIn("hashFiles", str(raised.exception))
        self.assertIn("capability is unsupported", str(raised.exception))

    def test_closure_follows_workflow_order(self):
        workflow = """\
jobs:
  build:
    needs: [left, right]
    steps:
      - run: echo build
  extra:
    steps:
      - run: echo extra
  right:
    steps:
      - run: echo right
  left:
    steps:
      - run: echo left
"""
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        self.assertEqual([job["id"] for job in plan["jobs"]], ["right", "left", "build"])
        self.assertIs(plan["job"], plan["jobs"][-1])
        self.assertEqual(plan["job"]["needs"], ["left", "right"])
        upstream = plan_workflow(workflow.encode(), "left")["plan"]
        self.assertEqual([job["id"] for job in upstream["jobs"]], ["left"])

    def test_unneeded_job_is_omitted_and_still_checked(self):
        workflow = """\
jobs:
  extra:
    steps:
      - run: echo extra
  build:
    steps:
      - run: echo build
"""
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        self.assertEqual([job["id"] for job in plan["jobs"]], ["build"])
        rejected = """\
jobs:
  extra:
    uses: example/repo/.github/workflows/ci.yml@main
  build:
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(rejected.encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.extra.uses")

    def test_missing_dependency_is_outside_the_selection(self):
        workflow = """\
jobs:
  build:
    needs: missing
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.build.needs")
        self.assertIn("outside the selection", str(raised.exception))

    def test_dependency_cycle_is_rejected(self):
        cases = {
            "jobs.one.needs": """\
jobs:
  one:
    needs: two
    steps:
      - run: echo one
  two:
    needs: one
    steps:
      - run: echo two
""",
            "jobs.build.needs": """\
jobs:
  build:
    needs: build
    steps:
      - run: echo build
""",
            "jobs.build.needs.duplicate": """\
jobs:
  one:
    steps:
      - run: echo one
  build:
    needs: [one, one]
    steps:
      - run: echo build
""",
        }
        for field, workflow in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(PlanError) as raised:
                    selected = "one" if field.startswith("jobs.one") else "build"
                    plan_workflow(workflow.encode(), selected)
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
                self.assertIn("dependency cycle", str(raised.exception))

    def test_job_if_and_outputs_are_stored(self):
        workflow = """\
jobs:
  one:
    outputs:
      kind: ${{ github.event.kind }}
      token: ${{ secrets.TOKEN }}
      label: ${{ case(true, 'kept', 'other') }}
    steps:
      - run: echo one
  build:
    needs: one
    if: success()
    steps:
      - run: echo build
"""
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        self.assertEqual([job["id"] for job in plan["jobs"]], ["one", "build"])
        self.assertEqual(plan["jobs"][0]["outputs"]["kind"], "${{ github.event.kind }}")
        self.assertEqual(plan["jobs"][0]["outputs"]["token"], "${{ secrets.TOKEN }}")
        self.assertEqual(plan["jobs"][0]["outputs"]["label"], "${{ case(true, 'kept', 'other') }}")
        self.assertEqual(plan["job"]["if"], "success()")
        self.assertEqual(plan["job"]["needs"], ["one"])
        secret = """\
jobs:
  build:
    if: secrets.TOKEN
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(secret.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.build.if")
        self.assertIn("context is not available: secrets", str(raised.exception))
        steps = """\
jobs:
  build:
    if: steps.one.outcome == 'success'
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(steps.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertIn("context is not available: steps", str(raised.exception))
        hashed = """\
jobs:
  build:
    if: hashFiles('*.txt')
    outputs:
      kind: ${{ github.event.kind }}
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(hashed.encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.if")
        self.assertIn("hashFiles", str(raised.exception))
        output = """\
jobs:
  build:
    outputs:
      kind: ${{ hashFiles('*.txt') }}
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(output.encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.outputs.kind")
        status = """\
jobs:
  build:
    outputs:
      kind: ${{ success() }}
    steps:
      - run: echo build
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(status.encode(), "build")
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.build.outputs.kind")
        self.assertIn("expression is not accepted", str(raised.exception))

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
