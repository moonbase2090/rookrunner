import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_core.plan import (
    CAPABILITY_VERSION,
    DEFAULT_JOB_TIMEOUT_MINUTES,
    MAX_CALLED_WORKFLOWS,
    MAX_JOB_TIMEOUT_MINUTES,
    MAX_STEP_TIMEOUT_MINUTES,
    MAX_WORKFLOW_BYTES,
    MAX_WORKFLOW_LEVELS,
    PERMISSION_SCOPES,
    STEP_KEYS,
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
        self.assertEqual(plan["capability_version"], 12)
        self.assertNotIn("call", plan["job"])
        self.assertEqual(plan["job"]["services"], [])
        self.assertIsNone(plan["job"]["strategy"])
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

    def test_run_env_with_and_name_keep_source_and_reject_bad_expressions(self):
        workflow = """\
name: ${{ 'demo' }}
on: push
env:
  ROOT: ${{ github.workflow }}
jobs:
  build:
    name: job ${{ matrix.os }}
    env:
      JOB: ${{ github.sha }}
    steps:
      - name: step ${{ env.JOB }}
        run: echo ${{ env.JOB }}-${{ 'x' }}
        env:
          STEP: ${{ vars.MODE }}
"""
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        self.assertEqual(plan["workflow"]["name"], "${{ 'demo' }}")
        self.assertEqual(plan["workflow"]["env"]["ROOT"], "${{ github.workflow }}")
        self.assertEqual(plan["job"]["name"], "job ${{ matrix.os }}")
        self.assertEqual(plan["job"]["env"]["JOB"], "${{ github.sha }}")
        step = plan["job"]["steps"][0]
        self.assertEqual(step["name"], "step ${{ env.JOB }}")
        self.assertEqual(step["run"], "echo ${{ env.JOB }}-${{ 'x' }}")
        self.assertEqual(step["env"]["STEP"], "${{ vars.MODE }}")
        rejected = (
            (
                "jobs:\n  build:\n    steps:\n      - run: \"echo '${{ secrets.TOKEN }}'\"\n",
                "WORKFLOW_INVALID",
                "run is not accepted",
            ),
            (
                "jobs:\n  build:\n    steps:\n      - run: echo ${{ hashFiles('*.txt') }}\n",
                "CAPABILITY_UNSUPPORTED",
                "hashFiles",
            ),
            (
                "jobs:\n  build:\n    steps:\n      - run: echo ${{ success() }}\n",
                "WORKFLOW_INVALID",
                "expression is not accepted",
            ),
            (
                "env:\n  ROOT: ${{ needs.build.result }}\njobs:\n  build:\n    steps:\n      - run: echo hi\n",
                "WORKFLOW_INVALID",
                "context is not available: needs",
            ),
            (
                "jobs:\n  build:\n    env:\n      JOB: ${{ env.ROOT }}\n    steps:\n      - run: echo hi\n",
                "WORKFLOW_INVALID",
                "context is not available: env",
            ),
            (
                "jobs:\n  build:\n    name: ${{ secrets.TOKEN }}\n    steps:\n      - run: echo hi\n",
                "WORKFLOW_INVALID",
                "context is not available: secrets",
            ),
        )
        for text, kind, message in rejected:
            with self.assertRaises(PlanError) as raised:
                plan_workflow(text.encode(), "build")
            self.assertEqual(raised.exception.kind, kind)
            self.assertIn(message, str(raised.exception))
        service = """\
jobs:
  build:
    services:
      db:
        image: sha256:abababababababababababababababababababababababababababababababab
        env:
          TOKEN: ${{ secrets.TOKEN }}
    steps:
      - run: echo hi
"""
        planned = plan_workflow(service.encode(), "build")["plan"]
        self.assertEqual(planned["job"]["services"][0]["env"]["TOKEN"], "${{ secrets.TOKEN }}")

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
      - uses: actions/checkout@v4.2.2
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
            "jobs.test.strategy.fast": """\
jobs:
  test:
    strategy:
      fast: true
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
            "jobs.test.services.db.ports": """\
jobs:
  test:
    services:
      db:
        image: sha256:abababababababababababababababababababababababababababababababab
        ports:
          - 5432:5432
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
            "on.workflow_call.secrets": """\
on:
  workflow_call:
    secrets:
      token:
        required: true
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

    def test_service_image_env_and_command_are_stored(self):
        pin = "sha256:" + "ab" * 32
        named = "redis@sha256:" + "cd" * 32
        workflow = f"""\
on: push
jobs:
  build:
    services:
      cache:
        image: {pin}
        env:
          MODE: on
        command: python -c "print(1)"
        entrypoint: python
      other:
        image: {named}
    steps:
      - run: echo hi
  plain:
    steps:
      - run: echo no
"""
        plan = self.plan(workflow)["plan"]["job"]
        self.assertEqual(plan["services"][0]["id"], "cache")
        self.assertEqual(plan["services"][0]["image"], pin)
        self.assertEqual(plan["services"][0]["env"], {"MODE": "on"})
        self.assertEqual(plan["services"][0]["command"], ["python", "-c", "print(1)"])
        self.assertEqual(plan["services"][0]["entrypoint"], "python")
        self.assertEqual(plan["services"][1]["id"], "other")
        self.assertEqual(plan["services"][1]["image"], named)
        self.assertNotIn("command", plan["services"][1])
        selected = self.plan(workflow, "plain")["plan"]["job"]
        self.assertEqual(selected["services"], [])

    def test_unpinned_service_image_is_invalid(self):
        for image in ("redis", "redis:7", ""):
            with self.subTest(image=image):
                workflow = f"""\
on: push
jobs:
  build:
    services:
      cache:
        image: "{image}"
    steps:
      - run: echo hi
"""
                with self.assertRaises(PlanError) as raised:
                    self.plan(workflow)
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
                self.assertEqual(raised.exception.field, "jobs.build.services.cache.image")
                self.assertIn("pinned by digest", str(raised.exception))

    def test_service_hostname_and_unsupported_keys(self):
        pin = "sha256:" + "ab" * 32
        cases = {
            "jobs.build.services.bad_name": f"""\
on: push
jobs:
  build:
    services:
      bad_name:
        image: {pin}
    steps:
      - run: echo hi
""",
            "jobs.build.services.cache.credentials": f"""\
on: push
jobs:
  build:
    services:
      cache:
        image: {pin}
        credentials:
          username: user
    steps:
      - run: echo hi
""",
            "jobs.build.services.cache.volumes": f"""\
on: push
jobs:
  build:
    services:
      cache:
        image: {pin}
        volumes:
          - /data
    steps:
      - run: echo hi
""",
            "jobs.build.services.cache.options": f"""\
on: push
jobs:
  build:
    services:
      cache:
        image: {pin}
        options: --network host
    steps:
      - run: echo hi
""",
        }
        for field, workflow in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(PlanError) as raised:
                    self.plan(workflow)
                if field.endswith("bad_name"):
                    self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
                else:
                    self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(raised.exception.field, field)

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

    def test_local_composite_expands_from_the_snapshot(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - id: hello
        uses: ./.github/actions/hello
        with:
          who: ${{ 'Mona' }}
"""
        action = """\
name: Hello
description: Say hello
inputs:
  who:
    description: Who
    required: true
    default: ${{ 'Octocat' }}
    deprecationMessage: who is old
outputs:
  tone:
    description: Tone
    value: ${{ steps.say.outputs.tone }}
  note:
    description: Note
    value: hello
runs:
  using: composite
  steps:
    - id: say
      shell: bash
      env:
        WHO: ${{ inputs.who }}
      run: printf '%s' '${{ inputs.who }}'
"""
        with tempfile.TemporaryDirectory(prefix="plan-action-") as temp:
            snapshot = write_snapshot(temp, workflow, {".github/actions/hello/action.yml": action})
            first = plan_snapshot(snapshot, "build")
            second = plan_snapshot(snapshot, "build")
            self.assertEqual(first, second)
            step = first["plan"]["job"]["steps"][0]
            self.assertEqual(first["plan"]["capability_version"], 12)
            self.assertEqual(step["uses"], "./.github/actions/hello")
            self.assertEqual(step["action_path"], ".github/actions/hello")
            self.assertEqual(len(step["action_digest"]), 64)
            self.assertEqual(step["with"], {"who": "${{ 'Mona' }}"})
            self.assertTrue(step["inputs"]["who"]["required"])
            self.assertEqual(step["inputs"]["who"]["default"], "${{ 'Octocat' }}")
            self.assertEqual(step["inputs"]["who"]["deprecation_message"], "who is old")
            inner = step["steps"][0]
            self.assertEqual(inner["env"]["WHO"], "${{ inputs.who }}")
            self.assertIn("${{ inputs.who }}", inner["run"])
            self.assertEqual(step["outputs"]["note"]["value"], "hello")
            self.assertNotIn("run", step)
            dollar = workflow.replace("./.github/actions/hello", "$/.github/actions/hello")
            other = write_snapshot(
                Path(temp) / "dollar",
                dollar,
                {".github/actions/hello/action.yml": action},
            )
            dollar_step = plan_snapshot(other, "build")["plan"]["job"]["steps"][0]
            self.assertEqual(dollar_step["action_digest"], step["action_digest"])
            self.assertEqual(dollar_step["steps"][0]["run"], inner["run"])
            changed = action.replace("printf", "echo")
            (snapshot / "files" / ".github" / "actions" / "hello" / "action.yml").write_text(
                changed
            )
            again = plan_snapshot(snapshot, "build")["plan"]["job"]["steps"][0]
            self.assertNotEqual(again["action_digest"], step["action_digest"])
            self.assertIn("echo", again["steps"][0]["run"])

    def test_local_uses_without_a_snapshot_root_is_unsupported(self):
        workflow = """\
jobs:
  build:
    steps:
      - uses: ./.github/actions/hello
"""
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), "build")
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.steps.0.uses")

    def test_required_input_without_with_still_plans(self):
        workflow = """\
jobs:
  build:
    steps:
      - uses: ./.github/actions/hello
"""
        action = """\
name: Hello
description: Say hello
inputs:
  who:
    description: Who
    required: true
runs:
  using: composite
  steps:
    - shell: bash
      run: echo hi
"""
        with tempfile.TemporaryDirectory(prefix="plan-default-") as temp:
            snapshot = write_snapshot(temp, workflow, {".github/actions/hello/action.yml": action})
            step = plan_snapshot(snapshot, "build")["plan"]["job"]["steps"][0]
        self.assertEqual(step["with"], {})
        self.assertTrue(step["inputs"]["who"]["required"])
        self.assertNotIn("default", step["inputs"]["who"])

    def test_action_yml_is_preferred_over_action_yaml(self):
        workflow = """\
jobs:
  build:
    steps:
      - uses: ./.github/actions/hello
"""
        yml = """\
name: Hello
description: From yml
runs:
  using: composite
  steps:
    - shell: bash
      run: printf yml
"""
        yaml_text = """\
name: Hello
description: From yaml
runs:
  using: composite
  steps:
    - shell: bash
      run: printf yaml
"""
        with tempfile.TemporaryDirectory(prefix="plan-names-") as temp:
            snapshot = write_snapshot(
                temp,
                workflow,
                {
                    ".github/actions/hello/action.yml": yml,
                    ".github/actions/hello/action.yaml": yaml_text,
                },
            )
            step = plan_snapshot(snapshot, "build")["plan"]["job"]["steps"][0]
        self.assertEqual(step["steps"][0]["run"], "printf yml")

    def test_composite_rejections(self):
        cases = {
            "docker-uses": (
                "jobs:\n  test:\n    steps:\n      - uses: docker://alpine:3.8\n",
                {},
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses",
            ),
            "escape": (
                "jobs:\n  test:\n    steps:\n      - uses: ./../outside\n",
                {},
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.uses",
            ),
            "node20": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Js
description: JavaScript
runs:
  using: node20
  main: index.js
"""
                },
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses.runs.using",
            ),
            "node24": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Js
description: JavaScript
runs:
  using: node24
  main: index.js
"""
                },
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses.runs.using",
            ),
            "docker-runtime": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Box
description: Docker
runs:
  using: docker
  image: Dockerfile
"""
                },
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses.runs.using",
            ),
            "nested": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: Nested
runs:
  using: composite
  steps:
    - uses: ./.github/actions/other
"""
                },
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses.runs.steps.0.uses",
            ),
            "continue": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: Continue
runs:
  using: composite
  steps:
    - run: echo hi
      shell: bash
      continue-on-error: true
"""
                },
                "CAPABILITY_UNSUPPORTED",
                "jobs.test.steps.0.uses.runs.steps.0.continue-on-error",
            ),
            "missing-shell": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: No shell
runs:
  using: composite
  steps:
    - run: echo hi
"""
                },
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.uses.runs.steps.0.shell",
            ),
            "missing-file": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {},
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.uses",
            ),
            "unknown-with": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n        with:\n          missing: Mona\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: Inputs
inputs:
  who:
    description: Who
runs:
  using: composite
  steps:
    - shell: bash
      run: echo hi
"""
                },
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.with.missing",
            ),
            "bool-with": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n        with:\n          who: true\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: Inputs
inputs:
  who:
    description: Who
runs:
  using: composite
  steps:
    - shell: bash
      run: echo hi
"""
                },
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.with.who",
            ),
            "duplicate-id": (
                "jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/hello\n",
                {
                    ".github/actions/hello/action.yml": """\
name: Hello
description: Duplicate
runs:
  using: composite
  steps:
    - id: say
      shell: bash
      run: echo one
    - id: say
      shell: bash
      run: echo two
"""
                },
                "WORKFLOW_INVALID",
                "jobs.test.steps.0.uses.runs.steps.1",
            ),
            "both": (
                "jobs:\n  test:\n    steps:\n      - run: echo hi\n        uses: ./.github/actions/hello\n",
                {},
                "WORKFLOW_INVALID",
                "jobs.test.steps.0",
            ),
        }
        for name, (workflow, files, kind, field) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(prefix="plan-reject-") as temp:
                    snapshot = write_snapshot(temp, workflow, files)
                    with self.assertRaises(PlanError) as raised:
                        plan_snapshot(snapshot, "test" if "test:" in workflow else "build")
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.field, field)

    def test_action_symlink_is_rejected(self):
        workflow = """\
jobs:
  test:
    steps:
      - uses: ./.github/actions/hello
"""
        action = """\
name: Hello
description: Linked
runs:
  using: composite
  steps:
    - shell: bash
      run: echo hi
"""
        with tempfile.TemporaryDirectory(prefix="plan-link-") as temp:
            root = Path(temp)
            snapshot = write_snapshot(root / "file", workflow, {})
            files = snapshot / "files"
            real = files / "real.yml"
            real.write_text(action)
            action_dir = files / ".github" / "actions" / "hello"
            action_dir.mkdir(parents=True)
            (action_dir / "action.yml").symlink_to(real)
            with self.assertRaises(PlanError) as raised:
                plan_snapshot(snapshot, "test")
            self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
            self.assertIn("not a regular file", str(raised.exception))
            directory = write_snapshot(root / "dir", workflow, {})
            real_dir = directory / "files" / "real-action"
            real_dir.mkdir()
            (real_dir / "action.yml").write_text(action)
            link_parent = directory / "files" / ".github" / "actions"
            link_parent.mkdir(parents=True)
            (link_parent / "hello").symlink_to(real_dir, target_is_directory=True)
            with self.assertRaises(PlanError) as raised:
                plan_snapshot(directory, "test")
            self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
            self.assertIn("not a regular file", str(raised.exception))

    def test_concurrency_keeps_source_and_rejects_the_invalid_combination(self):
        workflow = """\
name: demo
on: push
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  build:
    concurrency:
      group: job-${{ matrix.os }}
      queue: max
      cancel-in-progress: ${{ github.ref == 'refs/heads/main' }}
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        self.assertEqual(plan["capability_version"], CAPABILITY_VERSION)
        self.assertEqual(
            plan["workflow"]["concurrency"],
            {
                "group": "ci-${{ github.ref }}",
                "cancel_in_progress": True,
                "queue": "single",
            },
        )
        self.assertEqual(
            plan["job"]["concurrency"],
            {
                "group": "job-${{ matrix.os }}",
                "cancel_in_progress": "${{ github.ref == 'refs/heads/main' }}",
                "queue": "max",
            },
        )
        string_form = plan_workflow(
            "concurrency: plain\njobs:\n  build:\n    steps:\n      - run: echo hi\n".encode(),
            "build",
        )["plan"]
        self.assertEqual(
            string_form["workflow"]["concurrency"],
            {"group": "plain", "cancel_in_progress": False, "queue": "single"},
        )
        rejected = {
            "queue max": """\
concurrency:
  group: deploy
  queue: max
  cancel-in-progress: true
jobs:
  build:
    steps:
      - run: echo hi
""",
            "secrets": """\
concurrency:
  group: ${{ secrets.TOKEN }}
jobs:
  build:
    steps:
      - run: echo hi
""",
            "needs": """\
concurrency:
  group: ${{ needs.build.result }}
jobs:
  build:
    steps:
      - run: echo hi
""",
        }
        for name, text in rejected.items():
            with self.subTest(name=name):
                with self.assertRaises(PlanError) as raised:
                    plan_workflow(text.encode(), "build")
                self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        with self.assertRaises(PlanError) as raised:
            plan_workflow(
                "concurrency:\n  group: ${{ hashFiles('x') }}\njobs:\n  build:\n    steps:\n      - run: echo hi\n".encode(),
                "build",
            )
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("hashFiles", str(raised.exception))


class MatrixTests(unittest.TestCase):
    def plan(self, workflow, job_id="build"):
        return plan_workflow(workflow.encode(), job_id)["plan"]

    def test_include_matches_the_documented_combinations(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        fruit: [apple, pear]
        animal: [cat, dog]
        include:
          - color: green
          - color: pink
            animal: cat
          - fruit: apple
            shape: circle
          - fruit: banana
          - fruit: banana
            animal: cat
    steps:
      - run: echo hi
"""
        combinations = self.plan(workflow)["job"]["strategy"]["combinations"]
        self.assertEqual(
            combinations,
            [
                {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
                {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
                {"fruit": "pear", "animal": "cat", "color": "pink"},
                {"fruit": "pear", "animal": "dog", "color": "green"},
                {"fruit": "banana"},
                {"fruit": "banana", "animal": "cat"},
            ],
        )
        self.assertIs(self.plan(workflow)["job"]["strategy"]["fail_fast"], True)
        self.assertIsNone(self.plan(workflow)["job"]["strategy"]["max_parallel"])

    def test_exclude_is_a_partial_match_and_runs_before_include(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        os: [macos-latest, windows-latest]
        version: [12, 14, 16]
        environment: [staging, production]
        exclude:
          - os: macos-latest
            version: 12
            environment: production
          - os: windows-latest
            version: 16
        include:
          - os: windows-latest
            version: 16
            environment: restored
    steps:
      - run: echo hi
"""
        combinations = self.plan(workflow)["job"]["strategy"]["combinations"]
        self.assertEqual(
            combinations,
            [
                {"os": "macos-latest", "version": 12, "environment": "staging"},
                {"os": "macos-latest", "version": 14, "environment": "staging"},
                {"os": "macos-latest", "version": 14, "environment": "production"},
                {"os": "macos-latest", "version": 16, "environment": "staging"},
                {"os": "macos-latest", "version": 16, "environment": "production"},
                {"os": "windows-latest", "version": 12, "environment": "staging"},
                {"os": "windows-latest", "version": 12, "environment": "production"},
                {"os": "windows-latest", "version": 14, "environment": "staging"},
                {"os": "windows-latest", "version": 14, "environment": "production"},
                {"os": "windows-latest", "version": 16, "environment": "restored"},
            ],
        )
        self.assertIsInstance(combinations[0]["version"], int)

    def test_unknown_exclude_key_removes_nothing(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        os: [a, b]
        exclude:
          - missing: a
    steps:
      - run: echo hi
"""
        combinations = self.plan(workflow)["job"]["strategy"]["combinations"]
        self.assertEqual(combinations, [{"os": "a"}, {"os": "b"}])

    def test_include_key_matches_axis_case_and_keeps_the_axis_spelling(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        os: [linux]
        include:
          - OS: linux
            arch: arm
    steps:
      - run: echo hi
"""
        self.assertEqual(
            self.plan(workflow)["job"]["strategy"]["combinations"],
            [{"os": "linux", "arch": "arm"}],
        )

    def test_duplicate_axis_case_is_invalid(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        os: [a]
        OS: [b]
    steps:
      - run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            self.plan(workflow)
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.build.strategy.matrix.OS")

    def test_fail_fast_and_max_parallel_are_stored_without_a_ceiling(self):
        workflow = """\
jobs:
  build:
    strategy:
      fail-fast: false
      max-parallel: 100000
      matrix:
        version: [1, 2]
    steps:
      - run: echo hi
"""
        strategy = self.plan(workflow)["job"]["strategy"]
        self.assertIs(strategy["fail_fast"], False)
        self.assertEqual(strategy["max_parallel"], 100000)
        self.assertEqual(strategy["combinations"], [{"version": 1}, {"version": 2}])
        rejected = """\
jobs:
  build:
    strategy:
      max-parallel: 0
    steps:
      - run: echo hi
"""
        with self.assertRaises(PlanError) as raised:
            self.plan(rejected)
        self.assertEqual(raised.exception.kind, "WORKFLOW_INVALID")
        self.assertEqual(raised.exception.field, "jobs.build.strategy.max-parallel")

    def test_matrix_expression_and_unknown_strategy_key_are_unsupported(self):
        cases = {
            "jobs.build.strategy.matrix.version": """\
jobs:
  build:
    strategy:
      matrix:
        version: ${{ github.event.versions }}
    steps:
      - run: echo hi
""",
            "jobs.build.strategy.fail-fast": """\
jobs:
  build:
    strategy:
      fail-fast: ${{ true }}
    steps:
      - run: echo hi
""",
            "on.strategy": """\
on:
  strategy: push
jobs:
  build:
    steps:
      - run: echo hi
""",
        }
        for field, workflow in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(PlanError) as raised:
                    self.plan(workflow)
                self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(raised.exception.field, field)

    def test_matrix_limit_is_256_jobs(self):
        def workflow(count):
            values = ", ".join(str(index) for index in range(count))
            return f"""\
jobs:
  build:
    strategy:
      matrix:
        a: [{values}]
        b: [{values}]
    steps:
      - run: echo hi
"""

        accepted = self.plan(workflow(16))
        self.assertEqual(len(accepted["job"]["strategy"]["combinations"]), 256)
        with self.assertRaises(PlanError) as raised:
            self.plan(workflow(17))
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.build.strategy.matrix")
        self.assertIn("256", str(raised.exception))
        self.assertIn(
            "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax",
            str(raised.exception),
        )

    def test_empty_axis_keeps_only_include_combinations(self):
        workflow = """\
jobs:
  build:
    strategy:
      matrix:
        version: []
        include:
          - version: 9
    steps:
      - run: echo hi
"""
        self.assertEqual(
            self.plan(workflow)["job"]["strategy"]["combinations"],
            [{"version": 9}],
        )

    def test_forbidden_strategy_key_on_an_unselected_job_still_fails(self):
        workflow = """\
jobs:
  other:
    strategy:
      fast: true
    steps:
      - run: echo x
  build:
    steps:
      - run: echo y
"""
        with self.assertRaises(PlanError) as raised:
            self.plan(workflow)
        self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(raised.exception.field, "jobs.other.strategy.fast")


_CALLED = """\
on: workflow_call
jobs:
  build:
    steps:
      - run: echo hi
"""

_REUSE_HOWTO = (
    "https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows"
    "#nesting-reusable-workflows"
)
_REUSE_LIMITS = (
    "https://docs.github.com/en/actions/reference/workflows-and-actions/"
    "reusing-workflow-configurations#limitations-of-reusable-workflows"
)


class ReusableWorkflowTests(unittest.TestCase):
    def plan_files(self, workflow, files, job_id="build"):
        with tempfile.TemporaryDirectory(prefix="plan-reuse-") as temp:
            snapshot = write_snapshot(temp, workflow, files)
            return plan_snapshot(snapshot, job_id)["plan"]

    def reject(self, workflow, files, job_id="build"):
        with tempfile.TemporaryDirectory(prefix="plan-reuse-") as temp:
            snapshot = write_snapshot(temp, workflow, files)
            with self.assertRaises(PlanError) as raised:
                plan_snapshot(snapshot, job_id)
            return raised.exception

    def test_documented_limits_match_the_cited_pages(self):
        self.assertEqual(MAX_WORKFLOW_LEVELS, 10)
        self.assertEqual(MAX_CALLED_WORKFLOWS, 50)

    def test_workflow_call_trigger_plans_a_normal_job(self):
        plan = plan_workflow(
            b"on: workflow_call\njobs:\n  build:\n    steps:\n      - run: echo hi\n",
            "build",
        )["plan"]
        self.assertEqual(plan["capability_version"], 12)
        self.assertNotIn("call", plan["job"])
        self.assertEqual(plan["job"]["services"], [])
        self.assertEqual(plan["workflow"]["on"], "workflow_call")

    def test_called_job_stores_services_and_the_caller_cannot(self):
        pin = "sha256:" + "ab" * 32
        called = f"""\
on: workflow_call
jobs:
  build:
    services:
      cache:
        image: {pin}
        env:
          MODE: on
    steps:
      - run: echo hi
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": called}, "call")
        self.assertNotIn("services", plan["job"])
        inner = plan["job"]["call"]["jobs"][0]
        self.assertEqual(inner["services"], [{"id": "cache", "image": pin, "env": {"MODE": "on"}}])
        rejected = (
            """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    services:
      cache:
        image: %s
"""
            % pin
        )
        error = self.reject(rejected, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.call.services")

    def test_inputs_and_outputs_keep_their_types(self):
        called = """\
on:
  workflow_call:
    inputs:
      username:
        description: Name
        required: true
        type: string
      count:
        required: true
        type: number
        default: 2
      flag:
        required: false
        type: boolean
        default: false
      note:
        type: string
    outputs:
      word:
        description: Word
        value: ${{ jobs.build.outputs.word }}
jobs:
  build:
    steps:
      - run: echo hi
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    with:
      username: ada
      count: 2
      flag: true
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": called}, "call")
        slots = {item["name"]: item for item in plan["job"]["call"]["inputs"]}
        self.assertEqual(slots["username"]["passed"], {"literal": "ada"})
        self.assertEqual(slots["username"]["type"], "string")
        self.assertTrue(slots["username"]["required"])
        self.assertEqual(slots["count"]["passed"], {"literal": 2})
        self.assertIsInstance(slots["count"]["passed"]["literal"], int)
        self.assertNotIsInstance(slots["count"]["passed"]["literal"], bool)
        self.assertEqual(slots["count"]["default"], {"literal": 2})
        self.assertIs(slots["flag"]["passed"]["literal"], True)
        self.assertEqual(slots["flag"]["default"], {"literal": False})
        self.assertIsNone(slots["note"]["passed"])
        self.assertIsNone(slots["note"]["default"])
        self.assertFalse(slots["note"]["required"])
        self.assertEqual(
            plan["job"]["call"]["outputs"],
            {"word": "${{ jobs.build.outputs.word }}"},
        )
        self.assertEqual(plan["job"]["call"]["path"], ".github/workflows/called.yml")
        self.assertNotIn("steps", plan["job"])

    def test_dollar_form_uses_the_same_snapshot_path(self):
        workflow = """\
on: push
jobs:
  call:
    uses: $/.github/workflows/called.yml
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": _CALLED}, "call")
        self.assertEqual(plan["job"]["call"]["path"], ".github/workflows/called.yml")

    def test_wrong_literal_type_is_invalid(self):
        called = """\
on:
  workflow_call:
    inputs:
      flag:
        required: true
        type: boolean
jobs:
  build:
    steps:
      - run: echo hi
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    with:
      flag: "true"
"""
        error = self.reject(workflow, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("must be a boolean", str(error))

    def test_unknown_and_missing_inputs_are_invalid(self):
        called = """\
on:
  workflow_call:
    inputs:
      username:
        required: true
        type: string
jobs:
  build:
    steps:
      - run: echo hi
"""
        files = {".github/workflows/called.yml": called}
        unknown = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    with:
      username: ada
      extra: no
"""
        error = self.reject(unknown, files, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("input is not defined", str(error))
        missing = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        error = self.reject(missing, files, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("is required", str(error))

    def test_choice_is_not_a_workflow_call_input_type(self):
        called = """\
on:
  workflow_call:
    inputs:
      color:
        type: choice
jobs:
  build:
    steps:
      - run: echo hi
"""
        workflow = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/called.yml\n"
        error = self.reject(workflow, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("boolean, number, or string", str(error))

    def test_output_requires_an_expression_without_secrets(self):
        missing = """\
on:
  workflow_call:
    outputs:
      word:
        description: Word
jobs:
  build:
    steps:
      - run: echo hi
"""
        workflow = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/called.yml\n"
        error = self.reject(workflow, {".github/workflows/called.yml": missing}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("value is required", str(error))
        secret = """\
on:
  workflow_call:
    outputs:
      word:
        value: ${{ secrets.TOKEN }}
jobs:
  build:
    steps:
      - run: echo hi
"""
        error = self.reject(workflow, {".github/workflows/called.yml": secret}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("context is not available", str(error))

    def test_with_expression_can_read_needs_and_not_secrets(self):
        workflow = """\
on: push
jobs:
  first:
    outputs:
      name: ${{ 'ada' }}
    steps:
      - run: echo hi
  call:
    needs: first
    uses: ./.github/workflows/called.yml
    with:
      username: ${{ needs.first.outputs.name }}
"""
        called = """\
on:
  workflow_call:
    inputs:
      username:
        required: true
        type: string
jobs:
  build:
    steps:
      - run: echo hi
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": called}, "call")
        slot = plan["job"]["call"]["inputs"][0]
        self.assertEqual(slot["passed"], {"expression": "${{ needs.first.outputs.name }}"})
        rejected = workflow.replace("needs.first.outputs.name", "secrets.TOKEN")
        error = self.reject(rejected, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("context is not available", str(error))

    def test_secrets_inherit_is_unsupported(self):
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    secrets: inherit
"""
        error = self.reject(workflow, {".github/workflows/called.yml": _CALLED}, "call")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.call.secrets")
        self.assertIn("capability is unsupported", str(error))

    def test_caller_strategy_and_steps_are_unsupported(self):
        strategy = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    strategy:
      matrix:
        version: [1]
"""
        error = self.reject(strategy, {".github/workflows/called.yml": _CALLED}, "call")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.call.strategy")
        steps = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    steps:
      - run: echo hi
"""
        error = self.reject(steps, {".github/workflows/called.yml": _CALLED}, "call")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.call.steps")

    def test_caller_env_is_not_copied_into_the_called_workflow(self):
        workflow = """\
on: push
env:
  FOO: from-caller
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        called = """\
on: workflow_call
env:
  BAR: from-called
jobs:
  build:
    steps:
      - run: echo hi
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(plan["workflow"]["env"], {"FOO": "from-caller"})
        self.assertEqual(plan["job"]["call"]["workflow"]["env"], {"BAR": "from-called"})

    def test_same_file_called_twice_is_one_workflow(self):
        workflow = """\
on: push
jobs:
  first:
    uses: ./.github/workflows/called.yml
  second:
    needs: first
    uses: ./.github/workflows/called.yml
"""
        plan = self.plan_files(workflow, {".github/workflows/called.yml": _CALLED}, "second")
        self.assertEqual([job["id"] for job in plan["jobs"]], ["first", "second"])
        self.assertIsNot(plan["jobs"][0]["call"], plan["jobs"][1]["call"])

    def test_subdirectory_expression_and_missing_trigger_are_invalid(self):
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/nested/called.yml
"""
        error = self.reject(workflow, {}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("reusable workflow path is not accepted", str(error))
        expressed = """\
on: push
jobs:
  call:
    uses: "${{ './.github/workflows/called.yml' }}"
"""
        error = self.reject(expressed, {".github/workflows/called.yml": _CALLED}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("expression is not accepted", str(error))
        pushed = """\
on: push
jobs:
  build:
    steps:
      - run: echo hi
"""
        caller = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/called.yml\n"
        error = self.reject(caller, {".github/workflows/called.yml": pushed}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("workflow_call is required", str(error))

    def test_a_cycle_is_invalid(self):
        called = """\
on: workflow_call
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        workflow = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/called.yml\n"
        error = self.reject(workflow, {".github/workflows/called.yml": called}, "call")
        self.assertEqual(error.kind, "WORKFLOW_INVALID")
        self.assertIn("loop", str(error))

    def test_ten_levels_are_accepted_and_eleven_cites_the_docs(self):
        workflow, files = _chain(10)
        plan = self.plan_files(workflow, files, "call")
        current = plan["job"]
        depth = 1
        while "call" in current:
            current = current["call"]["jobs"][0]
            depth += 1
        self.assertEqual(depth, 10)
        self.assertEqual(current["steps"][0]["run"], "echo hi")
        workflow, files = _chain(11)
        error = self.reject(workflow, files, "call")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("10", str(error))
        self.assertIn(_REUSE_HOWTO, str(error))

    def test_fifty_unique_workflows_are_accepted_and_fifty_one_cites_the_docs(self):
        workflow, files = _wide(50, duplicate=True)
        plan = self.plan_files(workflow, files, "twice")
        self.assertEqual(plan["job"]["id"], "twice")
        workflow, files = _wide(51, duplicate=False)
        error = self.reject(workflow, files, "j1")
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertIn("50", str(error))
        self.assertIn(_REUSE_LIMITS, str(error))


def _chain(levels):
    """Return a caller plus called files that form `levels` workflows."""

    files = {}
    for level in range(2, levels + 1):
        if level == levels:
            body = "on: workflow_call\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
        else:
            body = (
                "on: workflow_call\njobs:\n  call:\n"
                f"    uses: ./.github/workflows/w{level + 1}.yml\n"
            )
        files[f".github/workflows/w{level}.yml"] = body
    workflow = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/w2.yml\n"
    return workflow, files


def _wide(unique, duplicate):
    """Return `unique` called files. One file is called twice when requested."""

    files = {}
    lines = ["on: push", "jobs:"]
    count = unique - 1 if duplicate else unique
    for index in range(1, count + 1):
        name = f"w{index}.yml"
        files[f".github/workflows/{name}"] = _CALLED
        lines.append(f"  j{index}:")
        lines.append(f"    uses: ./.github/workflows/{name}")
    if duplicate:
        files[".github/workflows/extra.yml"] = _CALLED
        lines.extend(
            [
                "  again:",
                "    uses: ./.github/workflows/extra.yml",
                "  twice:",
                "    uses: ./.github/workflows/extra.yml",
            ]
        )
    return "\n".join(lines) + "\n", files


def write_snapshot(root, workflow, files):
    base = Path(root)
    file_root = base / "files"
    workflow_path = file_root / ".github" / "workflows" / "test.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(workflow)
    for rel, text in files.items():
        path = file_root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (base / "manifest.json").write_text(json.dumps({"workflow": ".github/workflows/test.yml"}))
    return base


class CheckoutPlanTests(unittest.TestCase):
    def plan(self, workflow, job_id="build"):
        return plan_workflow(workflow.encode(), job_id)["plan"]

    def reject(self, workflow, job_id="build"):
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), job_id)
        return raised.exception

    def test_owned_checkout_records_captured_files(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        self.assertNotIn("checkout", STEP_KEYS)
        workflow = """\
on: push
jobs:
  build:
    steps:
      - id: co
        name: checkout
        if: github.sha == ''
        shell: not-a-shell
        working-directory: missing-dir
        timeout-minutes: 1
        env:
          MODE: kept
        uses: actions/checkout@v4
"""
        plan = self.plan(workflow)
        self.assertEqual(plan["capability_version"], 12)
        step = plan["job"]["steps"][0]
        self.assertEqual(step["uses"], "actions/checkout@v4")
        self.assertEqual(step["checkout"], "captured")
        self.assertEqual(step["id"], "co")
        self.assertEqual(step["name"], "checkout")
        self.assertEqual(step["if"], "github.sha == ''")
        self.assertEqual(step["shell"], "not-a-shell")
        self.assertEqual(step["working_directory"], "missing-dir")
        self.assertEqual(step["timeout_minutes"], 1)
        self.assertEqual(step["env"], {"MODE": "kept"})
        self.assertNotIn("with", step)
        for absent in ("action_path", "action_digest", "steps", "inputs", "outputs", "run"):
            self.assertNotIn(absent, step)

    def test_false_checkout_flags_are_stored(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
          clean: false
"""
        step = self.plan(workflow)["job"]["steps"][0]
        self.assertEqual(step["with"], {"persist-credentials": False, "clean": False})
        self.assertEqual(step["checkout"], "captured")
        self.assertNotIn("action_path", step)

    def test_rejected_checkout_inputs_name_the_field(self):
        keys = [
            "token",
            "ssh-key",
            "ssh-known-hosts",
            "ssh-strict",
            "ssh-user",
            "repository",
            "ref",
            "path",
            "fetch-tags",
            "submodules",
            "lfs",
            "sparse-checkout",
            "sparse-checkout-cone-mode",
            "filter",
            "set-safe-directory",
            "github-server-url",
            "show-progress",
        ]
        cases = [("clean", "true"), ("persist-credentials", "true")]
        cases.extend((key, "ignored") for key in keys)
        for key, value in cases:
            field = f"jobs.build.steps.0.with.{key}"
            workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          {key}: {value}
"""
            with self.subTest(field=field):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, field)
                self.assertIn(field, str(error))

    def test_non_boolean_checkout_flag_is_invalid(self):
        for value in ('"false"', "0", "yes"):
            workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          clean: {value}
"""
            with self.subTest(value=value):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "WORKFLOW_INVALID")
                self.assertEqual(error.field, "jobs.build.steps.0.with.clean")
                self.assertIn("must be a boolean", str(error))

    def test_other_checkout_uses_stay_rejected(self):
        for uses in (
            "actions/checkout",
            "actions/checkout@v4.2.2",
            "actions/checkout@v5.0.0",
            "actions/checkout@v",
            "actions/checkout@V5",
            "actions/setup-node@v4",
        ):
            workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: {uses}
"""
            with self.subTest(uses=uses):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, "jobs.build.steps.0.uses")

    def test_check_yml_sha_is_the_owned_checkout(self):
        sha = "11d5960a326750d5838078e36cf38b85af677262"
        check = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "check.yml"
        self.assertIn(f"uses: actions/checkout@{sha}", check.read_text())
        workflow = f"""\
on: push
jobs:
  build:
    steps:
      - name: Checkout
        uses: actions/checkout@{sha}
"""
        step = self.plan(workflow)["job"]["steps"][0]
        self.assertEqual(step["uses"], f"actions/checkout@{sha}")
        self.assertEqual(step["checkout"], "captured")
        self.assertEqual(step["name"], "Checkout")
        for absent in ("action_path", "action_digest", "steps", "inputs", "outputs", "run"):
            self.assertNotIn(absent, step)

    def test_unverified_sha_is_recorded_verbatim(self):
        sha = "a" * 40
        workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@{sha}
        with:
          clean: false
"""
        step = self.plan(workflow)["job"]["steps"][0]
        self.assertEqual(step["uses"], f"actions/checkout@{sha}")
        self.assertEqual(step["checkout"], "captured")
        self.assertEqual(step["with"], {"clean": False})

    def test_sha_checkout_rejects_the_same_with_keys(self):
        sha = "b" * 40
        workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@{sha}
        with:
          clean: true
"""
        error = self.reject(workflow)
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.build.steps.0.with.clean")

    def test_short_sha_branch_and_other_tags_stay_rejected(self):
        real = "11d5960a326750d5838078e36cf38b85af677262"
        for uses in (
            f"actions/checkout@{real[:39]}",
            "actions/checkout@main",
            f"actions/checkout@{real.upper()}",
            f"actions/checkout@{'c' * 41}",
        ):
            workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: {uses}
"""
            with self.subTest(uses=uses):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, "jobs.build.steps.0.uses")


class PermissionsPlanTests(unittest.TestCase):
    def plan(self, workflow, job_id="build"):
        return plan_workflow(workflow.encode(), job_id)["plan"]

    def reject(self, workflow, job_id="build"):
        with self.assertRaises(PlanError) as raised:
            plan_workflow(workflow.encode(), job_id)
        return raised.exception

    def test_scope_list_matches_the_syntax_page(self):
        self.assertEqual(
            PERMISSION_SCOPES,
            {
                "actions",
                "artifact-metadata",
                "attestations",
                "checks",
                "code-quality",
                "contents",
                "deployments",
                "discussions",
                "id-token",
                "issues",
                "packages",
                "pages",
                "pull-requests",
                "security-events",
                "statuses",
                "vulnerability-alerts",
            },
        )

    def test_contents_read_is_recorded_at_each_level(self):
        workflow = """\
permissions:
  contents: read
on: push
jobs:
  build:
    permissions:
      contents: read
    steps:
      - run: echo hi
"""
        plan = self.plan(workflow)
        self.assertEqual(plan["capability_version"], 12)
        self.assertEqual(plan["workflow"]["permissions"], {"contents": "read"})
        self.assertEqual(plan["job"]["permissions"], {"contents": "read"})
        self.assertNotIn("token", plan["workflow"])
        self.assertNotIn("token", plan["job"])

    def test_read_all_and_empty_map_are_recorded(self):
        read_all = self.plan(
            "permissions: read-all\non: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(read_all["workflow"]["permissions"], "read-all")
        empty = self.plan(
            "permissions: {}\non: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(empty["workflow"]["permissions"], {})

    def test_every_documented_scope_accepts_read_or_none(self):
        lines = ["permissions:"]
        for index, scope in enumerate(sorted(PERMISSION_SCOPES)):
            lines.append(f"  {scope}: {'read' if index % 2 == 0 else 'none'}")
        lines.append("on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n")
        plan = self.plan("\n".join(lines))
        recorded = plan["workflow"]["permissions"]
        self.assertEqual(set(recorded), set(PERMISSION_SCOPES))
        self.assertTrue(set(recorded.values()) <= {"read", "none"})

    def test_omitted_permissions_leave_the_plan_unchanged(self):
        plan = self.plan("on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n")
        self.assertNotIn("permissions", plan["workflow"])
        self.assertNotIn("permissions", plan["job"])

    def test_write_write_all_and_unknown_scope_name_the_field(self):
        cases = {
            "permissions": "permissions: write-all\n",
            "permissions.contents": "permissions:\n  contents: write\n",
            "permissions.not-a-scope": "permissions:\n  not-a-scope: read\n",
            "jobs.build.permissions": "jobs:\n  build:\n    permissions: write-all\n",
            "jobs.build.permissions.contents": (
                "jobs:\n  build:\n    permissions:\n      contents: write\n"
            ),
            "jobs.build.permissions.not-a-scope": (
                "jobs:\n  build:\n    permissions:\n      not-a-scope: none\n"
            ),
        }
        for field, fragment in cases.items():
            if fragment.startswith("permissions"):
                workflow = (
                    fragment + "on: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n"
                )
            else:
                workflow = "on: push\n" + fragment + "    steps:\n      - run: echo hi\n"
            with self.subTest(field=field):
                error = self.reject(workflow)
                self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(error.field, field)
                self.assertIn(field, str(error))

    def test_caller_job_still_rejects_permissions(self):
        workflow = """\
on: push
jobs:
  build:
    permissions:
      contents: read
    uses: ./.github/workflows/called.yml
"""
        error = self.reject(workflow)
        self.assertEqual(error.kind, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.field, "jobs.build.permissions")

    def test_called_workflow_records_its_own_permissions(self):
        workflow = """\
on: push
jobs:
  build:
    uses: ./.github/workflows/called.yml
"""
        called = """\
permissions:
  contents: read
on: workflow_call
jobs:
  inner:
    steps:
      - run: echo hi
"""
        with tempfile.TemporaryDirectory(prefix="plan-permissions-") as temp:
            snapshot = write_snapshot(temp, workflow, {".github/workflows/called.yml": called})
            plan = plan_snapshot(snapshot, "build")["plan"]
        self.assertNotIn("permissions", plan["job"])
        self.assertEqual(plan["job"]["call"]["workflow"]["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
