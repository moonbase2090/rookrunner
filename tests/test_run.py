import ast
from pathlib import Path
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest

from execution_core.actions import ActionStore, stage_node24_actions
from execution_core.node24 import inspect_node24
from execution_core.artifacts import written_files
from execution_core.attempt import materialize_attempt
from execution_core.disk import usage
from execution_core.plan import plan_snapshot, plan_workflow
from dockerutil import foreign_ids
from execution_core.run import (
    ContainerLease,
    RunError,
    _ATTEMPT,
    _CallInputError,
    _OUTPUT_JOB_BYTES,
    _OUTPUT_RUN_BYTES,
    _WORKFLOW_PATH,
    _commit_sha,
    _consider_step,
    _empty_directory,
    _exec_limit,
    _job_for_combination,
    _job_outputs,
    _read_utf8,
    _resolve_call_inputs,
    _runner_arch,
    _soft_defaults,
    _verify_workspace,
    _with_rendered_step,
    _workflow_for_job,
    _workflow_label,
    owned_container_present,
    release_owned_container,
    run_job,
)
from execution_core.expr import ExprError
from execution_core.verify import verify_snapshot
from execution_core.snapshot import SourceCapture


SUCCESS = """\
name: demo
on: push
env:
  LEVEL: ${{ 'workflow' }}
  TRACE: kept
defaults:
  run:
    working-directory: app
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      LEVEL: ${{ 'job' }}
    steps:
      - name: first
        run: |
          printf '%s' "$LEVEL" > level.txt
          printf '%s' "$TRACE" > trace.txt
          printf '%s' "$PWD" > pwd.txt
          printf '%s' "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/workspace.txt"
          cp "$ROOKRUNNER_EVENT" "$GITHUB_WORKSPACE/event.json"
          if false | true; then printf '%s' ok > "$GITHUB_WORKSPACE/unspecified.txt"
          else printf '%s' bad > "$GITHUB_WORKSPACE/unspecified.txt"; fi
          printf '%s\\n' one > "$GITHUB_WORKSPACE/order.txt"
          cat "$GITHUB_WORKSPACE/link.txt" > "$GITHUB_WORKSPACE/linked.txt"
      - id: nested
        name: nested step
        working-directory: app/nested
        env:
          LEVEL: ${{ 'step' }}
        run: |
          printf '%s' "$LEVEL" > level.txt
          printf '%s' "$TRACE" > trace.txt
          printf '%s' "$PWD" > pwd.txt
          printf '%s\\n' two >> "$GITHUB_WORKSPACE/order.txt"
      - shell: bash
        run: |
          if false | true; then printf '%s' bad > "$GITHUB_WORKSPACE/bash.txt"
          else printf '%s' ok > "$GITHUB_WORKSPACE/bash.txt"; fi
          printf '%s' '${{ github.sha }}' > "$GITHUB_WORKSPACE/expr.txt"
          printf '%s' "$ROOKRUNNER_TEST_MARKER" > "$GITHUB_WORKSPACE/hostenv.txt"
      - shell: sh
        run: |
          if false | true; then printf '%s' ok > "$GITHUB_WORKSPACE/sh.txt"
          else printf '%s' bad > "$GITHUB_WORKSPACE/sh.txt"; fi
          cat "$GITHUB_WORKSPACE/source.txt" > "$GITHUB_WORKSPACE/source-copy.txt"
"""

FAILURE = """\
on: push
jobs:
  build:
    steps:
      - id: first
        name: first
        run: printf '%s\\n' one > "$GITHUB_WORKSPACE/order.txt"
      - id: fail
        name: fail step
        run: exit 3
      - id: later
        name: later
        run: printf '%s\\n' later >> "$GITHUB_WORKSPACE/order.txt"
"""

EVENT = {"kind": "local", "n": 1}
CONDITIONS = """\
on: push
jobs:
  build:
    steps:
      - id: skip
        if: ${{ false }}
        run: printf '%s\\n' skipped > "$GITHUB_WORKSPACE/skipped.txt"
      - id: keep
        if: github.event.kind == 'local'
        run: printf '%s\\n' kept > "$GITHUB_WORKSPACE/kept.txt"
      - id: blank
        if: github.token == ''
        run: printf '%s\\n' blank > "$GITHUB_WORKSPACE/blank.txt"
"""
ALWAYS = """\
on: push
jobs:
  build:
    steps:
      - id: fail
        run: exit 2
      - id: after
        if: always()
        run: printf '%s\\n' after > "$GITHUB_WORKSPACE/after.txt"
"""
OUTPUTS = """\
on: push
jobs:
  one:
    outputs:
      kind: ${{ github.event.kind }}
      token: ${{ secrets.TOKEN }}
    steps:
      - id: show
        run: echo one
  build:
    needs: one
    if: needs.one.outputs.kind == 'local'
    steps:
      - id: keep
        if: needs.one.outputs.token == ''
        run: printf '%s\\n' kept > "$GITHUB_WORKSPACE/kept.txt"
      - id: blank
        run: printf '%s\\n' blank > "$GITHUB_WORKSPACE/blank.txt"
"""
CHAIN = """\
on: push
jobs:
  one:
    outputs:
      kind: ${{ github.event.kind }}
      token: ${{ secrets.TOKEN }}
    steps:
      - id: fail
        name: fail step
        run: exit 4
  two:
    needs: one
    steps:
      - id: run
        run: printf '%s\\n' ran > "$GITHUB_WORKSPACE/ran.txt"
  build:
    needs: [one, two]
    if: always()
    steps:
      - id: keep
        if: >-
          needs.one.result == 'failure' && needs.two.result == 'skipped' &&
          needs.one.outputs.kind == 'local' && needs.one.outputs.token == ''
        run: printf '%s\\n' kept > "$GITHUB_WORKSPACE/kept.txt"
"""
COMMANDS = """\
on: push
jobs:
  one:
    outputs:
      color: ${{ steps.color.outputs.SELECTED_COLOR }}
    steps:
      - id: color
        run: |
          printf '%s' "$ACTION_STATE" > "$GITHUB_WORKSPACE/same.txt"
          echo "SELECTED_COLOR=green" >> "$GITHUB_OUTPUT"
          echo "secret-number=kept" >> "$GITHUB_OUTPUT"
          echo "ACTION_STATE=yellow" >> "$GITHUB_ENV"
          { echo 'MSG<<EOF'; echo one; echo two; echo EOF; } >> "$GITHUB_ENV"
          echo "GITHUB_WORKSPACE=/tmp" >> "$GITHUB_ENV"
          echo "NODE_OPTIONS=blocked" >> "$GITHUB_ENV"
          mkdir -p "$GITHUB_WORKSPACE/bin"
          printf '%s\\n' '#!/bin/sh' 'printf seen' > "$GITHUB_WORKSPACE/bin/marker"
          chmod +x "$GITHUB_WORKSPACE/bin/marker"
          echo "$GITHUB_WORKSPACE/bin" >> "$GITHUB_PATH"
          echo '::set-env name=FROM_CMD::nope'
          echo '::add-path::/workspace/not-added'
          echo '::add-mask::mask-token'
          echo 'mask-token'
          echo '::add-mask::Mona The Octocat'
          echo 'Mona The Octocat'
          echo '::stop-commands::STOP'
          echo '::add-mask::visible-command'
          echo '::STOP::'
          echo '::warning::Missing semicolon'
      - id: later
        if: >-
          steps.color.outputs.SELECTED_COLOR == 'green' &&
          steps.color.outputs.secret-number == 'kept'
        run: |
          printf '%s\\n' "$ACTION_STATE" > "$GITHUB_WORKSPACE/env.txt"
          printf '%s\\n' "$MSG" > "$GITHUB_WORKSPACE/msg.txt"
          printf '%s\\n' "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/ws.txt"
          printf '%s' "$NODE_OPTIONS" > "$GITHUB_WORKSPACE/node.txt"
          printf '%s' "$FROM_CMD" > "$GITHUB_WORKSPACE/cmd.txt"
          printf '%s\\n' "$PATH" > "$GITHUB_WORKSPACE/path.txt"
          marker
          printf '%s\\n' mask-token
          printf '%s\\n' 'Mona The Octocat'
  build:
    needs: one
    steps:
      - if: needs.one.outputs.color == 'green'
        run: |
          printf '%s\\n' kept > "$GITHUB_WORKSPACE/kept.txt"
          printf '%s' "$ACTION_STATE" > "$GITHUB_WORKSPACE/cross.txt"
          printf '%s\\n' mask-token
"""
COMPOSITE = """\
on: push
jobs:
  build:
    steps:
      - name: mutate
        run: |
          python -c 'from pathlib import Path; p = Path("/workspace/.github/actions/hello/action.yml"); p.write_text(p.read_text().replace("PLANNED", "MUTATED"))'
      - id: hello
        name: greet ${{ 'Mona' }}
        uses: ./.github/actions/hello
        with:
          who: ${{ 'Mona' }}
      - id: later
        if: steps.hello.outputs.tone == 'loud'
        run: |
          printf '%s\\n' "$FROM_ACTION" > "$GITHUB_WORKSPACE/from-action.txt"
          printf '%s\\n' loud > "$GITHUB_WORKSPACE/tone.txt"
          printf '%s' "$GITHUB_ACTION_PATH" > "$GITHUB_WORKSPACE/outer-action-path.txt"
"""
COMPOSITE_ACTION = """\
name: Hello
description: Say hello
inputs:
  who:
    description: Who to greet
    required: false
    deprecationMessage: who is old
  title:
    description: Title
    required: true
    default: Dr
outputs:
  tone:
    description: How loud
    value: ${{ steps.say.outputs.tone }}
runs:
  using: composite
  steps:
    - id: say
      shell: bash
      env:
        WHO: ${{ inputs.who }}
        LITERAL: hello ${{ inputs.who }}
        TITLE: ${{ inputs.title }}
      run: |
        printf '%s\\n' PLANNED > "$GITHUB_WORKSPACE/marker.txt"
        printf '%s\\n' "$WHO" > "$GITHUB_WORKSPACE/who.txt"
        printf '%s\\n' "$TITLE" > "$GITHUB_WORKSPACE/title.txt"
        printf '%s\\n' "$LITERAL" > "$GITHUB_WORKSPACE/literal-env.txt"
        printf '%s\\n' '${{ inputs.who }}' > "$GITHUB_WORKSPACE/literal.txt"
        printf '%s\\n' "$GITHUB_ACTION_PATH" > "$GITHUB_WORKSPACE/action-path.txt"
        printf '%s' "$INPUT_WHO" > "$GITHUB_WORKSPACE/input-env.txt"
        echo "tone=loud" >> "$GITHUB_OUTPUT"
        echo "FROM_ACTION=yes" >> "$GITHUB_ENV"
    - id: next
      if: steps.say.outputs.tone == 'loud'
      shell: bash
      run: printf '%s\\n' next > "$GITHUB_WORKSPACE/next.txt"
    - id: skip
      if: false
      shell: bash
      run: printf '%s\\n' skipped > "$GITHUB_WORKSPACE/skipped.txt"
"""
COMPOSITE_FAIL = """\
on: push
jobs:
  build:
    steps:
      - id: boom
        uses: ./.github/actions/fail
"""
COMPOSITE_FAIL_ACTION = """\
name: Fail
description: Fail then continue
runs:
  using: composite
  steps:
    - id: fail
      shell: bash
      run: exit 2
    - id: after
      if: always()
      shell: bash
      run: printf '%s\\n' after > "$GITHUB_WORKSPACE/after.txt"
"""
FAILED_ENV = """\
on: push
jobs:
  build:
    steps:
      - id: fail
        run: |
          echo "AFTER=yes" >> "$GITHUB_ENV"
          echo '::set-env name=NOPE::no'
          exit 2
      - id: after
        if: always() && env.AFTER == 'yes'
        run: printf '%s' "$AFTER$NOPE" > "$GITHUB_WORKSPACE/after.txt"
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _capture(root, workflow_text, extra=None):
    repo = root / "repo"
    repo.mkdir()
    workflow = repo / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(workflow_text)
    for rel, text in (extra or {}).items():
        path = repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (repo / "source.txt").write_text("original\n")
    (repo / "link.txt").symlink_to("source.txt")
    app = repo / "app"
    app.mkdir()
    (app / "keep.txt").write_text("keep\n")
    nested = app / "nested"
    nested.mkdir()
    (nested / "keep.txt").write_text("nested\n")
    state = root / "state"
    capture = SourceCapture(repo, state)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    captured = capture.capture(".github/workflows/test.yml")
    snapshot = state / "snapshots" / captured["snapshot_id"]
    attempts = state / "attempts"
    attempts.mkdir(mode=0o700)
    workspace = attempts / "run-1"
    materialize_attempt(snapshot, captured["digest"], workspace)
    return repo, snapshot, captured["digest"], workspace


def _plan(workflow_text):
    return plan_workflow(workflow_text.encode(), "build")["plan"]


class OutputLimitTests(unittest.TestCase):
    def test_secret_and_oversize_outputs_are_not_copied(self):
        huge = "a" * ((_OUTPUT_JOB_BYTES // 2) + 1)
        produced, used = _job_outputs(
            {
                "outputs": {
                    "token": "secrets.TOKEN",
                    "big": "'" + huge + "'",
                    "kind": "'local'",
                    "flag": "true",
                    "empty": "null",
                    "obj": "fromJSON('{}')",
                }
            },
            {"secrets": {"TOKEN": "super-secret-value"}},
            0,
        )
        self.assertEqual(produced["kind"], "local")
        self.assertEqual(produced["flag"], "true")
        self.assertEqual(produced["empty"], "")
        self.assertNotIn("token", produced)
        self.assertNotIn("big", produced)
        self.assertNotIn("obj", produced)
        self.assertNotIn("super-secret-value", produced.values())
        self.assertLessEqual(used, _OUTPUT_JOB_BYTES)
        blocked, same = _job_outputs({"outputs": {"kind": "'local'"}}, {}, _OUTPUT_RUN_BYTES)
        self.assertEqual(blocked, {})
        self.assertEqual(same, _OUTPUT_RUN_BYTES)


class CallInputTests(unittest.TestCase):
    def test_omitted_inputs_use_documented_defaults(self):
        slots = [
            {
                "name": "flag",
                "type": "boolean",
                "required": False,
                "default": None,
                "passed": None,
            },
            {
                "name": "count",
                "type": "number",
                "required": False,
                "default": None,
                "passed": None,
            },
            {
                "name": "name",
                "type": "string",
                "required": False,
                "default": None,
                "passed": None,
            },
            {
                "name": "title",
                "type": "string",
                "required": False,
                "default": {"literal": "Dr"},
                "passed": None,
            },
            {
                "name": "who",
                "type": "string",
                "required": True,
                "default": None,
                "passed": {"literal": "ada"},
            },
            {
                "name": "echo",
                "type": "string",
                "required": False,
                "default": {"expression": "${{ inputs.who }}"},
                "passed": None,
            },
            {
                "name": "other",
                "type": "string",
                "required": False,
                "default": {"expression": "${{ inputs.echo }}"},
                "passed": None,
            },
        ]
        bound = _resolve_call_inputs(slots, {}, lambda explicit: {"inputs": explicit})
        self.assertIs(bound["flag"], False)
        self.assertIs(bound["count"], 0)
        self.assertEqual(bound["name"], "")
        self.assertEqual(bound["title"], "Dr")
        self.assertEqual(bound["who"], "ada")
        self.assertEqual(bound["echo"], "ada")
        self.assertEqual(bound["other"], "")

    def test_expression_result_must_match_the_declared_type(self):
        slots = [
            {
                "name": "flag",
                "type": "boolean",
                "required": True,
                "default": None,
                "passed": {"expression": "${{ 'true' }}"},
            }
        ]
        with self.assertRaises(_CallInputError) as raised:
            _resolve_call_inputs(slots, {}, lambda explicit: {"inputs": explicit})
        self.assertIn("boolean", str(raised.exception))
        number = [
            {
                "name": "count",
                "type": "number",
                "required": True,
                "default": None,
                "passed": {"expression": "${{ 2 }}"},
            }
        ]
        bound = _resolve_call_inputs(number, {}, lambda explicit: {"inputs": explicit})
        self.assertEqual(bound["count"], 2)
        self.assertIsInstance(bound["count"], int)


class CommandFileTests(unittest.TestCase):
    def test_invalid_utf8_command_file_is_ignored(self):
        temp = tempfile.TemporaryDirectory(prefix="run-cmd-")
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "env"
        path.write_bytes(b"OK=1\xff")
        self.assertIsNone(_read_utf8(path))
        path.write_bytes(b"OK=1\n")
        self.assertEqual(_read_utf8(path), "OK=1\n")
        self.assertIsNone(_read_utf8(path.parent / "gone"))


class TimeoutLimitTests(unittest.TestCase):
    def test_job_deadline_wins_an_equal_step_limit(self):
        self.assertEqual(_exec_limit(10, None), (10, True))
        self.assertEqual(_exec_limit(10, 10), (10, True))
        self.assertEqual(_exec_limit(10, 11), (10, True))
        self.assertEqual(_exec_limit(10, 9), (9, False))
        self.assertEqual(_exec_limit(0, 5), (0, True))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.snapshot, self.digest, self.workspace = _capture(self.root, SUCCESS)
        self.plan = _plan(SUCCESS)
        self.marker = self.root / "docker-called"
        docker = self.root / "docker"
        docker.write_text(f"#!/bin/sh\nprintf '%s\\n' called >> '{self.marker}'\nexit 99\n")
        docker.chmod(0o755)
        self.docker = docker

    def test_unpinned_image_does_not_run(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "ubuntu:latest",
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("not pinned", str(raised.exception))
        self.assertFalse(self.marker.exists())
        self.assertFalse(hasattr(raised.exception, "exit_code"))

    def test_missing_docker_is_setup_failure(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.root / "missing-docker"),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("Docker is missing", str(raised.exception))

    def test_host_network_is_rejected_before_docker(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
                network="host",
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("container network is not accepted", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_missing_docker_socket_is_rejected_before_docker(self):
        plain = self.root / "not-a-socket"
        plain.write_text("no\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
                docker_socket=str(plain),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(raised.exception), "Docker socket is not available")
        self.assertFalse(self.marker.exists())

    def test_changed_workspace_is_not_a_step_exit(self):
        (self.workspace / "source.txt").write_text("changed\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("workspace failed verification", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_owned_git_is_outside_workspace_verification(self):
        manifest = verify_snapshot(self.snapshot, self.digest)
        _verify_workspace(self.workspace, manifest)
        secret = self.root / "secret-dir"
        secret.mkdir()
        (secret / "token").write_text("secret-token\n")
        (self.workspace / ".git").rename(self.root / "saved-git")
        (self.workspace / ".git").symlink_to(secret, target_is_directory=True)
        _verify_workspace(self.workspace, manifest)
        (self.workspace / "source.txt").write_text("changed\n")
        with self.assertRaises(RunError) as raised:
            _verify_workspace(self.workspace, manifest)
        self.assertIn("workspace failed verification", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_changed_snapshot_is_not_a_step_exit(self):
        (self.snapshot / "files" / "source.txt").write_text("changed\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("snapshot failed verification", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_previous_capability_version_is_not_migrated(self):
        self.assertEqual(self.plan["capability_version"], 12)
        stale = dict(self.plan)
        stale["capability_version"] = 11
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                stale,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertFalse(self.marker.exists())

    def test_node24_without_the_directory_is_setup_failure(self):
        step = {
            "index": 0,
            "id": None,
            "name": None,
            "shell": None,
            "working_directory": None,
            "env": {},
            "uses": "acme/node@" + ("a" * 40),
            "action_path": "",
            "action_digest": "b" * 64,
            "with": {},
            "inputs": {"version": {"description": "Version", "required": True}},
            "outputs": {"answer": {"description": "Answer"}},
            "javascript": "node24",
            "main": "main.js",
            "action_owner": "acme",
            "action_repository": "node",
            "action_commit": "a" * 40,
            "content_digest": "c" * 64,
        }
        self.plan["job"]["steps"] = [step]
        self.plan["jobs"][-1]["steps"] = [step]
        actions = self.workspace.parent / "actions" / "acme" / "node" / ("a" * 40)
        actions.mkdir(parents=True)
        (actions / "main.js").write_text("exit 0\n")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                self.plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
                actions=self.workspace.parent / "actions",
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(raised.exception), "Node 24 is not configured")
        self.assertFalse(self.marker.exists())

    def test_read_only_permissions_are_accepted_before_docker(self):
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
        plan = _plan(workflow)
        self.assertEqual(plan["workflow"]["permissions"], {"contents": "read"})
        self.assertEqual(plan["job"]["permissions"], {"contents": "read"})
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.root / "missing-docker"),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("Docker is missing", str(raised.exception))
        self.assertFalse(self.marker.exists())

    def test_sha_pinned_checkout_is_accepted_before_docker(self):
        sha = "11d5960a326750d5838078e36cf38b85af677262"
        workflow = f"""\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@{sha}
"""
        plan = _plan(workflow)
        step = plan["job"]["steps"][0]
        self.assertEqual(step["uses"], f"actions/checkout@{sha}")
        self.assertEqual(step["checkout"], "captured")
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                plan,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.root / "missing-docker"),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("Docker is missing", str(raised.exception))
        rejected = dict(plan)
        rejected_job = dict(plan["job"])
        rejected_step = dict(step)
        rejected_step["uses"] = "actions/checkout@main"
        rejected_job["steps"] = [rejected_step]
        rejected["job"] = rejected_job
        rejected["jobs"] = [rejected_job]
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                rejected,
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.root / "missing-docker"),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("plan is not accepted", str(raised.exception))


class ContextValueTests(unittest.TestCase):
    def test_runner_arch_maps_only_the_dogfood_platforms(self):
        self.assertEqual(_runner_arch("linux", "amd64"), "X64")
        self.assertEqual(_runner_arch("linux", "arm64"), "ARM64")
        self.assertEqual(_runner_arch("linux", "386"), "X86")
        self.assertEqual(_runner_arch("linux", "arm"), "ARM")
        for system, architecture in (
            ("linux", "riscv64"),
            ("windows", "amd64"),
            (None, "amd64"),
            ("linux", None),
        ):
            with self.assertRaises(RunError) as raised:
                _runner_arch(system, architecture)
            self.assertEqual(raised.exception.kind, "SETUP_FAILED")
            self.assertEqual(str(raised.exception), "image platform is not accepted")

    def test_commit_sha_uses_only_a_clean_base_commit(self):
        sha = "ab" * 20
        long_sha = "cd" * 32
        self.assertEqual(
            _commit_sha({"dirty": False, "included": [], "base_commit": sha}),
            sha,
        )
        self.assertEqual(
            _commit_sha({"dirty": False, "included": [], "base_commit": long_sha}),
            long_sha,
        )
        for manifest in (
            {"dirty": True, "included": [], "base_commit": sha},
            {"dirty": False, "included": ["extra"], "base_commit": sha},
            {"dirty": False, "included": [], "base_commit": None},
            {"dirty": False, "included": [], "base_commit": "A" * 40},
            {"dirty": False, "included": [], "base_commit": "ab" * 19},
            None,
        ):
            self.assertIsNone(_commit_sha(manifest))

    def test_workflow_label_uses_the_path_when_the_name_is_empty(self):
        self.assertEqual(_workflow_label({"name": "demo"}), "demo")
        self.assertIsNone(_workflow_label({}))
        token = _WORKFLOW_PATH.set(".github/workflows/called.yml")
        try:
            self.assertEqual(_workflow_label({}), ".github/workflows/called.yml")
            self.assertEqual(_workflow_label({"name": ""}), ".github/workflows/called.yml")
            self.assertEqual(_workflow_label({"name": "demo"}), "demo")
        finally:
            _WORKFLOW_PATH.reset(token)

    def test_empty_directory_keeps_a_child_that_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gone").write_text("x")
            locked = root / "locked"
            locked.mkdir()
            (locked / "file").write_text("stay")
            os.chmod(locked, 0o555)
            _empty_directory(root)
            self.assertFalse((root / "gone").exists())
            self.assertEqual((locked / "file").read_text(), "stay")
            os.chmod(locked, 0o700)

    def test_soft_defaults_set_temp_only_for_a_socket_job(self):
        shared = "/tmp/rookrunner-socket-example"
        token = _ATTEMPT.set({"socket_temp": shared})
        try:
            self.assertEqual(
                _soft_defaults(),
                {
                    "CI": "true",
                    "HOME": "/github/home",
                    "TMP": shared,
                    "TEMP": shared,
                    "TMPDIR": shared,
                },
            )
        finally:
            _ATTEMPT.reset(token)
        token = _ATTEMPT.set({"arch": "ARM64"})
        try:
            self.assertEqual(_soft_defaults(), {"CI": "true", "HOME": "/github/home"})
        finally:
            _ATTEMPT.reset(token)
        self.assertEqual(_soft_defaults(), {})

    def test_event_name_is_rejected_before_docker(self):
        for name in ("", "push\n", "push\r", "push\0more"):
            with self.assertRaises(RunError) as raised:
                run_job("snap", "digest", "workspace", {}, "image", {}, event_name=name)
            self.assertEqual(raised.exception.kind, "SETUP_FAILED")
            self.assertEqual(str(raised.exception), "event name is not accepted")


class TextRenderTests(unittest.TestCase):
    def test_step_env_does_not_read_its_own_map_and_if_sees_it(self):
        workflow = {"name": "${{ 'demo' }}", "env": {"ROOT": "${{ 'root' }}"}}
        job = {"id": "build", "name": "job ${{ matrix.os }}", "env": {"JOB": "${{ 'job' }}"}}
        rendered_workflow = _workflow_for_job(workflow, {}, job, {}, {}, False)
        self.assertEqual(rendered_workflow["env"]["ROOT"], "root")
        self.assertEqual(rendered_workflow["name"], "${{ 'demo' }}")
        self.assertEqual(_workflow_label(rendered_workflow), "${{ 'demo' }}")
        rendered_job = _job_for_combination(
            job, rendered_workflow, {}, {}, {}, False, {"os": "Linux"}, {}
        )
        self.assertEqual(rendered_job["env"]["JOB"], "job")
        self.assertEqual(rendered_job["name"], "job Linux")
        step = {
            "index": 0,
            "id": "gate",
            "name": "n ${{ env.MODE }}",
            "if": "env.MODE == 'loud'",
            "checkout": "captured",
            "env": {"MODE": "${{ 'loud' }}", "OTHER": "${{ env.MODE }}"},
        }
        prepared, values = _with_rendered_step(
            step, {}, rendered_workflow, rendered_job, [], False, {}, None
        )
        self.assertEqual(prepared["env"]["MODE"], "loud")
        self.assertEqual(prepared["env"]["OTHER"], "")
        self.assertEqual(values["env"]["JOB"], "job")
        self.assertEqual(values["env"]["MODE"], "loud")
        result = _consider_step(
            "docker",
            "name",
            step,
            rendered_workflow,
            rendered_job,
            {},
            None,
            True,
            None,
            None,
            [],
            False,
            {},
            None,
            None,
            None,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["name"], "n loud")
        self.assertEqual(result["exit_code"], 0)
        with self.assertRaises(ExprError):
            _job_for_combination(
                {"id": "build", "name": "${{ fromJSON('{') }}", "env": {}},
                rendered_workflow,
                {},
                {},
                {},
                False,
                {},
                {},
            )

    def test_skipped_step_keeps_rendered_name(self):
        step = {
            "index": 0,
            "id": "gate",
            "name": "build ${{ 'one' }}",
            "if": "false",
            "run": "echo ${{ github.sha }}",
            "env": {},
        }
        result = _consider_step(
            "docker",
            "name",
            step,
            {"name": "demo", "env": {}},
            {"id": "build", "env": {}},
            {},
            None,
            True,
            None,
            None,
            [],
            False,
            {},
            None,
            None,
            None,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["name"], "build one")


class DockerRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.image_root = tempfile.TemporaryDirectory(prefix="run-image-")
        root = Path(cls.image_root.name)
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n")
        tag = "rookrunner-ns4-fixture"
        subprocess.run(
            ["docker", "build", "--quiet", "-t", tag, root],
            check=True,
            capture_output=True,
        )
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.image = inspected.stdout.strip()
        cls.tag = tag

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rmi", "-f", cls.tag], capture_output=True)
        cls.image_root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run-docker-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.snapshot, self.digest, self.workspace = _capture(self.root, SUCCESS)
        self.log = self.root / "docker-args"
        wrapper = self.root / "docker"
        real = shutil.which("docker")
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"path = {str(self.log)!r}\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(repr(sys.argv[1:]) + '\\n')\n"
            f"raise SystemExit(subprocess.call([{real!r}, *sys.argv[1:]]))\n"
        )
        wrapper.chmod(0o755)
        self.docker = wrapper
        self.previous = os.environ.get("ROOKRUNNER_TEST_MARKER")
        os.environ["ROOKRUNNER_TEST_MARKER"] = "fixture-secret-value"

    def tearDown(self):
        names = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            capture_output=True,
            text=True,
        )
        for container in foreign_ids(names.stdout):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        networks = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", "name=rookrunner-net-"],
            capture_output=True,
            text=True,
        )
        for network in networks.stdout.split():
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
        if self.previous is None:
            os.environ.pop("ROOKRUNNER_TEST_MARKER", None)
        else:
            os.environ["ROOKRUNNER_TEST_MARKER"] = self.previous

    def _calls(self):
        return [ast.literal_eval(line) for line in self.log.read_text().splitlines() if line]

    def test_read_only_permissions_run_without_a_token(self):
        workflow = """\
permissions:
  contents: read
on: push
jobs:
  build:
    permissions:
      contents: none
    steps:
      - run: |
          if [ -n "$GITHUB_TOKEN" ]; then exit 3; fi
          printf '%s' ok > "$GITHUB_WORKSPACE/ran.txt"
"""
        root = self.root / "permissions"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        plan = _plan(workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan,
            self.image,
            EVENT,
            docker=str(self.docker),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "ran.txt").read_text(), "ok")
        self.assertEqual(plan["workflow"]["permissions"], {"contents": "read"})
        self.assertEqual(plan["job"]["permissions"], {"contents": "none"})

    def test_node24_is_read_only_and_leaves_path_unchanged(self):
        node = self.root / "node24"
        binary = node / "bin" / "node"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nprintf '%s\\n' 'v24.0.0'\n")
        binary.chmod(0o755)
        inspected = inspect_node24(node)
        mounted_text = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: version
        run: /opt/node24/bin/node --version
      - id: path
        run: printf '%s' "$PATH"
      - id: write
        run: |
          if touch /opt/node24/probe 2>/dev/null; then exit 4; fi
          if printf x >> /opt/node24/bin/node 2>/dev/null; then exit 5; fi
          printf '%s' kept
"""
        plain_text = """\
name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: path
        run: printf '%s' "$PATH"
"""
        mounted_root = self.root / "mounted"
        mounted_root.mkdir()
        _repo, snapshot, digest, workspace = _capture(mounted_root, mounted_text)
        before = len(self._calls()) if self.log.exists() else 0
        mounted = run_job(
            snapshot,
            digest,
            workspace,
            _plan(mounted_text),
            self.image,
            EVENT,
            docker=str(self.docker),
            node24=(inspected["root"], inspected["digest"]),
        )
        creates = [call for call in self._calls()[before:] if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)
        mounts = [
            item
            for item in creates[0]
            if item.startswith("type=bind,") and "destination=/opt/node24" in item
        ]
        self.assertEqual(
            mounts,
            [f"type=bind,source={inspected['root']},destination=/opt/node24,readonly"],
        )
        self.assertEqual(mounted["status"], "succeeded")
        self.assertEqual(mounted["exit_code"], 0)
        self.assertEqual(
            mounted["node24"],
            {"digest": inspected["digest"], "version": "v24.0.0"},
        )
        self.assertEqual(mounted["steps"][0]["stdout"], "v24.0.0\n")
        self.assertEqual(mounted["steps"][2]["stdout"], "kept")
        self.assertFalse((node / "probe").exists())
        self.assertTrue(binary.read_text().startswith("#!/bin/sh\n"))
        plain_root = self.root / "plain"
        plain_root.mkdir()
        _repo, snapshot, digest, workspace = _capture(plain_root, plain_text)
        plain = run_job(
            snapshot,
            digest,
            workspace,
            _plan(plain_text),
            self.image,
            EVENT,
            docker=str(self.docker),
        )
        self.assertNotIn("node24", plain)
        self.assertNotIn("/opt/node24", mounted["steps"][1]["stdout"])
        self.assertEqual(mounted["steps"][1]["stdout"], plain["steps"][0]["stdout"])
        wrong = self.root / "node20"
        wrong_bin = wrong / "bin" / "node"
        wrong_bin.parent.mkdir(parents=True)
        wrong_bin.write_text("#!/bin/sh\nprintf '%s\\n' 'v20.0.0'\n")
        wrong_bin.chmod(0o755)
        wrong_info = inspect_node24(wrong)
        with self.assertRaises(RunError) as caught:
            run_job(
                snapshot,
                digest,
                workspace,
                _plan(plain_text),
                self.image,
                EVENT,
                docker=str(self.docker),
                node24=(wrong_info["root"], wrong_info["digest"]),
            )
        self.assertEqual(caught.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(caught.exception), "node is not Node 24")
        self.assertNotIn(str(wrong), str(caught.exception))

    def test_node24_main_runs_from_the_attempt_copy(self):
        node = self.root / "node24"
        binary = node / "bin" / "node"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            "  printf '%s\\n' v24.0.0\n"
            "  exit 0\n"
            "fi\n"
            'exec /bin/sh "$@"\n'
        )
        binary.chmod(0o755)
        inspected = inspect_node24(node)
        remote = self.root / "remote"
        action = remote / "acme" / "nodepin.git"
        nested = action / "js"
        nested.mkdir(parents=True)
        _git(action, "init", "--initial-branch=main")
        _git(action, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _git(action, "config", "uploadpack.allowAnySHA1InWant", "true")
        (nested / "action.yml").write_text(
            "name: Node\n"
            "description: javascript\n"
            "inputs:\n"
            "  version:\n"
            "    description: Version\n"
            "    required: true\n"
            "  place:\n"
            "    description: Place\n"
            "    default: ${{ github.workspace }}\n"
            "  token:\n"
            "    description: Token\n"
            "    default: ${{ github.token }}\n"
            "  python-version:\n"
            "    description: Python\n"
            "    default: '3.12'\n"
            "  mode:\n"
            "    description: Mode\n"
            "    default: write\n"
            "outputs:\n"
            "  answer:\n"
            "    description: Answer\n"
            "runs:\n"
            "  using: node24\n"
            "  main: main.js\n"
        )
        (nested / "main.js").write_text(
            "#!/bin/sh\n"
            'if [ -n "$GITHUB_TOKEN" ] || [ -n "$GITHUB_ACTIONS" ] || '
            '[ -n "$GITHUB_ACTION_PATH" ]; then exit 4; fi\n'
            'pwd > "$GITHUB_WORKSPACE/pwd.txt"\n'
            'printf \'%s\' "$INPUT_VERSION" > "$GITHUB_WORKSPACE/version.txt"\n'
            'printf \'%s\' "$INPUT_PLACE" > "$GITHUB_WORKSPACE/place.txt"\n'
            'printf \'%s\' "$INPUT_TOKEN" > "$GITHUB_WORKSPACE/token.txt"\n'
            'if [ "$INPUT_MODE" = "read" ]; then\n'
            '  printf \'%s\' "${secret-}" > "$GITHUB_WORKSPACE/seen.txt"\n'
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' 'answer=from-action' >> \"$GITHUB_OUTPUT\"\n"
            'mkdir -p "$GITHUB_WORKSPACE/bin"\n'
            "printf '%s\\n' '#!/bin/sh' 'printf %s path-ok' > "
            '"$GITHUB_WORKSPACE/bin/marker-cmd"\n'
            'chmod +x "$GITHUB_WORKSPACE/bin/marker-cmd"\n'
            'printf \'%s\\n\' "$GITHUB_WORKSPACE/bin" >> "$GITHUB_PATH"\n'
            "printf '%s\\n' 'secret=hidden-state' >> \"$GITHUB_STATE\"\n"
            "printf '%s\\n' 'summary text' >> \"$GITHUB_STEP_SUMMARY\"\n"
            "printf '%s\\n' '::add-matcher::{\"owner\":\"x\"}'\n"
            'printf \'%s\\n\' mutated > "$(dirname "$0")/mutated.txt"\n'
        )
        _git(action, "add", ".")
        _git(
            action,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "action",
        )
        sha = subprocess.run(
            ["git", "-C", action, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        workflow = (
            "name: demo\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - id: writer\n"
            "        working-directory: missing-dir\n"
            f"        uses: acme/nodepin/js@{sha}\n"
            "        with:\n"
            "          version: '3.12'\n"
            "      - id: reader\n"
            f"        uses: acme/nodepin/js@{sha}\n"
            "        with:\n"
            "          version: '3.12'\n"
            "          mode: read\n"
            "      - id: show\n"
            "        if: steps.writer.outputs.answer == 'from-action'\n"
            "        run: printf '%s' read-ok > \"$GITHUB_WORKSPACE/answer.txt\"\n"
            "      - id: path\n"
            "        run: |\n"
            "          if command -v node >/dev/null 2>&1; then exit 9; fi\n"
            '          marker-cmd > "$GITHUB_WORKSPACE/path.txt"\n'
        )
        root = self.root / "node-run"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        store = ActionStore(root / "state", remote, 10 * 1024**3)
        planned = plan_snapshot(snapshot, "build", action_store=store)
        step = planned["plan"]["job"]["steps"][0]
        self.assertEqual(step["javascript"], "node24")
        self.assertEqual(step["main"], "main.js")
        self.assertEqual(step["action_path"], "js")
        self.assertNotIn("steps", step)
        self.assertNotIn("value", step["outputs"]["answer"])
        actions = workspace.parent / "actions"
        staged = stage_node24_actions(planned["plan"], store, actions, lambda _size: False)
        self.assertEqual(store.fetches, 1)
        with self.assertRaises(RunError) as missing:
            run_job(
                snapshot,
                digest,
                workspace,
                planned["plan"],
                self.image,
                EVENT,
                docker=str(self.docker),
                actions=staged,
            )
        self.assertEqual(str(missing.exception), "Node 24 is not configured")
        self.assertFalse(self.log.exists())
        result = run_job(
            snapshot,
            digest,
            workspace,
            planned["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            node24=(inspected["root"], inspected["digest"]),
            actions=staged,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["node24"]["version"], "v24.0.0")
        self.assertEqual(result["node24"]["digest"], inspected["digest"])
        self.assertEqual((workspace / "pwd.txt").read_text(), "/workspace\n")
        self.assertEqual((workspace / "version.txt").read_text(), "3.12")
        self.assertEqual((workspace / "place.txt").read_text(), "/workspace")
        self.assertEqual((workspace / "token.txt").read_text(), "")
        self.assertEqual((workspace / "seen.txt").read_text(), "")
        self.assertEqual((workspace / "answer.txt").read_text(), "read-ok")
        self.assertEqual((workspace / "path.txt").read_text(), "path-ok")
        writer = result["steps"][0]
        self.assertEqual(writer["summary"], "summary text\n")
        self.assertNotIn("summary text", writer["stdout"])
        self.assertIn("::add-matcher::", writer["stdout"])
        self.assertNotIn("hidden-state", result["steps"][1]["stdout"])
        self.assertNotIn("hidden-state", writer["stdout"])
        copied = staged / "acme" / "nodepin" / sha / "js" / "mutated.txt"
        self.assertEqual(copied.read_text(), "mutated\n")
        stored = root / "state" / "actions" / "objects" / step["content_digest"]
        self.assertFalse((stored / "mutated.txt").exists())
        self.assertEqual((stored / "main.js").read_bytes(), (nested / "main.js").read_bytes())
        self.assertFalse((workspace / "main.js").exists())
        self.assertFalse((workspace / "mutated.txt").exists())
        names = [entry["path"] for entry in written_files(workspace, snapshot)]
        self.assertNotIn("main.js", names)
        self.assertNotIn("mutated.txt", names)
        self.assertIn("answer.txt", names)
        created = next(call for call in self._calls() if call and call[0] == "create")
        self.assertIn(
            f"type=bind,source={staged},destination=/actions",
            created,
        )
        self.assertNotIn(
            "readonly", created[created.index(f"type=bind,source={staged},destination=/actions")]
        )
        self.assertFalse(any(str(stored) in item for item in created))
        main = f"/actions/acme/nodepin/{sha}/js/main.js"
        ran = next(
            call for call in self._calls() if "/opt/node24/bin/node" in call and main in call
        )
        self.assertEqual(ran[ran.index("--workdir") + 1], "/workspace")
        self.assertIn("INPUT_VERSION=3.12", ran)
        self.assertIn("INPUT_PLACE=/workspace", ran)
        self.assertIn("INPUT_TOKEN=", ran)
        self.assertIn("INPUT_PYTHON-VERSION=3.12", ran)
        self.assertFalse(any(item.startswith("GITHUB_TOKEN=") for item in ran))
        self.assertFalse(any(item.startswith("GITHUB_ACTIONS=") for item in ran))
        self.assertFalse(any(item.startswith("GITHUB_ACTION_PATH=") for item in ran))
        self.assertEqual(store.fetches, 1)

        fail_repo = remote / "acme" / "failpin.git"
        fail_repo.mkdir(parents=True)
        _git(fail_repo, "init", "--initial-branch=main")
        _git(fail_repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _git(fail_repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        (fail_repo / "action.yml").write_text(
            "name: Fail\ndescription: nonzero\nruns:\n  using: node24\n  main: main.js\n"
        )
        (fail_repo / "main.js").write_text("#!/bin/sh\nexit 7\n")
        _git(fail_repo, "add", ".")
        _git(
            fail_repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fail",
        )
        fail_sha = subprocess.run(
            ["git", "-C", fail_repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        fail_workflow = (
            "name: demo\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            f"      - id: bad\n        uses: acme/failpin@{fail_sha}\n"
            "      - id: later\n"
            "        if: always()\n"
            "        run: printf '%s' still > \"$GITHUB_WORKSPACE/still.txt\"\n"
        )
        fail_root = self.root / "fail-run"
        fail_root.mkdir()
        _repo, fail_snapshot, fail_digest, fail_workspace = _capture(fail_root, fail_workflow)
        fail_store = ActionStore(fail_root / "state", remote, 10 * 1024**3)
        fail_plan = plan_snapshot(fail_snapshot, "build", action_store=fail_store)
        fail_actions = stage_node24_actions(
            fail_plan["plan"],
            fail_store,
            fail_workspace.parent / "actions",
            lambda _size: False,
        )
        failed = run_job(
            fail_snapshot,
            fail_digest,
            fail_workspace,
            fail_plan["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            node24=(inspected["root"], inspected["digest"]),
            actions=fail_actions,
        )
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["exit_code"], 7)
        self.assertEqual(failed["steps"][0]["exit_code"], 7)
        self.assertEqual(failed["steps"][1]["status"], "succeeded")
        self.assertEqual((fail_workspace / "still.txt").read_text(), "still")

    def test_node24_post_runs_after_main_in_reverse_order(self):
        node = self.root / "node24"
        binary = node / "bin" / "node"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            "  printf '%s\\n' v24.0.0\n"
            "  exit 0\n"
            "fi\n"
            'exec /bin/sh "$@"\n'
        )
        binary.chmod(0o755)
        inspected = inspect_node24(node)
        remote = self.root / "remote"
        main_js = (
            "#!/bin/sh\n"
            'case "$INPUT_KIND" in\n'
            "  a)\n"
            "    printf '%s\\n' main-a\n"
            "    printf '%s\\n' main-a >> \"$GITHUB_WORKSPACE/order.txt\"\n"
            "    printf '%s\\n' 'token=from-a' >> \"$GITHUB_STATE\"\n"
            "    ;;\n"
            "  b)\n"
            "    printf '%s\\n' main-b\n"
            "    printf '%s\\n' main-b >> \"$GITHUB_WORKSPACE/order.txt\"\n"
            "    printf '%s\\n' 'token=from-b' >> \"$GITHUB_STATE\"\n"
            "    ;;\n"
            "  skip) printf '%s\\n' skipped-main ;;\n"
            "  always) printf '%s\\n' always-main ;;\n"
            "  success) printf '%s\\n' success-main ;;\n"
            "  badpost) exit 0 ;;\n"
            "  badmain) exit 3 ;;\n"
            "esac\n"
        )
        post_js = (
            "#!/bin/sh\n"
            'case "$INPUT_KIND" in\n'
            "  a)\n"
            "    printf '%s\\n' post-a\n"
            "    printf '%s\\n' post-a >> \"$GITHUB_WORKSPACE/order.txt\"\n"
            '    printf \'%s\' "$STATE_token" > "$GITHUB_WORKSPACE/state-a.txt"\n'
            "    ;;\n"
            "  b)\n"
            "    printf '%s\\n' post-b\n"
            "    printf '%s\\n' post-b >> \"$GITHUB_WORKSPACE/order.txt\"\n"
            '    printf \'%s\' "$STATE_token" > "$GITHUB_WORKSPACE/state-b.txt"\n'
            "    ;;\n"
            "  skip) printf '%s\\n' skipped-post ;;\n"
            "  always) printf '%s\\n' always-post ;;\n"
            "  success) printf '%s\\n' success-post ;;\n"
            "  badpost) exit 7 ;;\n"
            "  badmain) exit 9 ;;\n"
            "esac\n"
        )
        plain_yml = (
            "name: Plain\n"
            "description: post\n"
            "inputs:\n"
            "  kind:\n"
            "    description: Kind\n"
            "    required: true\n"
            "runs:\n"
            "  using: node24\n"
            "  main: main.js\n"
            "  post: post.js\n"
        )

        def commit(repository, action_yml):
            repo = remote / "acme" / f"{repository}.git"
            repo.mkdir(parents=True)
            _git(repo, "init", "--initial-branch=main")
            _git(repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
            _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
            (repo / "action.yml").write_text(action_yml)
            (repo / "main.js").write_text(main_js)
            (repo / "post.js").write_text(post_js)
            _git(repo, "add", ".")
            _git(
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                repository,
            )
            return subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        plain = commit("plain", plain_yml)
        gated = commit("gated", plain_yml + "  post-if: success()\n")

        def execute(workflow, label):
            root = self.root / label
            root.mkdir()
            _repo, snapshot, digest, workspace = _capture(root, workflow)
            store = ActionStore(root / "state", remote, 10 * 1024**3)
            planned = plan_snapshot(snapshot, "build", action_store=store)
            staged = stage_node24_actions(
                planned["plan"],
                store,
                workspace.parent / "actions",
                lambda _size: False,
            )
            before = len(self._calls()) if self.log.exists() else 0
            result = run_job(
                snapshot,
                digest,
                workspace,
                planned["plan"],
                self.image,
                EVENT,
                docker=str(self.docker),
                step_timeout=60,
                node24=(inspected["root"], inspected["digest"]),
                actions=staged,
            )
            return result, workspace, self._calls()[before:]

        ordered, workspace, calls = execute(
            (
                "name: demo\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - id: first\n"
                "        working-directory: missing-dir\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: a\n"
                "      - id: second\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: b\n"
                "      - id: skipped\n"
                "        if: 'false'\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: skip\n"
                "      - id: middle\n"
                "        run: |\n"
                '          if [ -n "${STATE_token-}" ]; then exit 5; fi\n'
                "          printf '%s\\n' middle\n"
                "          printf '%s\\n' middle >> \"$GITHUB_WORKSPACE/order.txt\"\n"
            ),
            "order",
        )
        self.assertEqual(ordered["status"], "succeeded", ordered)
        self.assertEqual(ordered["exit_code"], 0)
        self.assertEqual(
            (workspace / "order.txt").read_text(),
            "main-a\nmain-b\nmiddle\npost-b\npost-a\n",
        )
        self.assertEqual((workspace / "state-a.txt").read_text(), "from-a")
        self.assertEqual((workspace / "state-b.txt").read_text(), "from-b")
        self.assertNotIn("skipped", (workspace / "order.txt").read_text())
        self.assertEqual(
            [step["stdout"] for step in ordered["steps"]],
            ["main-a\n", "main-b\n", "", "middle\n", "post-b\n", "post-a\n"],
        )
        self.assertEqual(ordered["steps"][2]["status"], "skipped")
        self.assertEqual(
            [step["id"] for step in ordered["steps"][4:]],
            ["second", "first"],
        )
        self.assertEqual([step["index"] for step in ordered["steps"][4:]], [4, 5])
        posts = [
            call
            for call in calls
            if "/opt/node24/bin/node" in call and call[-1].endswith("/post.js")
        ]
        first_post = next(call for call in posts if "INPUT_KIND=a" in call)
        second_post = next(call for call in posts if "INPUT_KIND=b" in call)
        self.assertIn("STATE_token=from-a", first_post)
        self.assertNotIn("STATE_token=from-b", first_post)
        self.assertIn("STATE_token=from-b", second_post)
        self.assertNotIn("STATE_token=from-a", second_post)
        self.assertEqual(first_post[first_post.index("--workdir") + 1], "/workspace")
        self.assertFalse(any(item.startswith("GITHUB_TOKEN=") for item in first_post))
        self.assertFalse(any(item.startswith("GITHUB_ACTION_PATH=") for item in first_post))

        skipped, _workspace, _calls = execute(
            (
                "name: demo\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - id: gated\n"
                f"        uses: acme/gated@{gated}\n"
                "        with:\n"
                "          kind: success\n"
                "      - id: always\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: always\n"
                "      - id: fail\n"
                "        run: exit 1\n"
            ),
            "gated",
        )
        self.assertEqual(skipped["status"], "failed", skipped)
        self.assertEqual(skipped["exit_code"], 1)
        self.assertEqual(skipped["failed_step"]["index"], 2)
        self.assertEqual(skipped["steps"][3]["status"], "succeeded")
        self.assertEqual(skipped["steps"][3]["stdout"], "always-post\n")
        self.assertEqual(skipped["steps"][4]["status"], "skipped")
        self.assertEqual(skipped["steps"][4]["id"], "gated")
        self.assertNotIn("success-post", "".join(step["stdout"] for step in skipped["steps"]))

        failed_post, _workspace, _calls = execute(
            (
                "name: demo\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - id: bad\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: badpost\n"
            ),
            "badpost",
        )
        self.assertEqual(failed_post["status"], "failed", failed_post)
        self.assertEqual(failed_post["exit_code"], 7)
        self.assertEqual(failed_post["failed_step"]["index"], 1)
        self.assertEqual(failed_post["steps"][0]["exit_code"], 0)
        self.assertEqual(failed_post["steps"][1]["exit_code"], 7)

        failed_main, _workspace, _calls = execute(
            (
                "name: demo\n"
                "on: push\n"
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - id: bad\n"
                f"        uses: acme/plain@{plain}\n"
                "        with:\n"
                "          kind: badmain\n"
            ),
            "badmain",
        )
        self.assertEqual(failed_main["status"], "failed", failed_main)
        self.assertEqual(failed_main["exit_code"], 3)
        self.assertEqual(failed_main["failed_step"]["index"], 0)
        self.assertEqual(failed_main["steps"][1]["exit_code"], 9)
        self.assertNotEqual(failed_main["status"], "succeeded")

    def test_steps_run_in_one_pinned_container(self):
        (self.repo / "source.txt").write_text("mutated checkout\n")
        result = run_job(
            self.snapshot,
            self.digest,
            self.workspace,
            _plan(SUCCESS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["failed_step"])
        self.assertEqual(result["image_digest"], self.image)
        self.assertEqual(result["image_reference"], self.image)
        self.assertEqual([step["exit_code"] for step in result["steps"]], [0, 0, 0, 0])
        self.assertEqual((self.workspace / "order.txt").read_text(), "one\ntwo\n")
        self.assertEqual((self.workspace / "app" / "level.txt").read_text(), "job")
        self.assertEqual((self.workspace / "app" / "trace.txt").read_text(), "kept")
        self.assertEqual((self.workspace / "app" / "pwd.txt").read_text(), "/workspace/app")
        self.assertEqual((self.workspace / "workspace.txt").read_text(), "/workspace")
        self.assertEqual((self.workspace / "event.json").read_text(), '{"kind":"local","n":1}\n')
        self.assertEqual((self.workspace / "unspecified.txt").read_text(), "ok")
        self.assertEqual((self.workspace / "app" / "nested" / "level.txt").read_text(), "step")
        self.assertEqual((self.workspace / "app" / "nested" / "trace.txt").read_text(), "kept")
        self.assertEqual(
            (self.workspace / "app" / "nested" / "pwd.txt").read_text(), "/workspace/app/nested"
        )
        self.assertEqual((self.workspace / "bash.txt").read_text(), "ok")
        self.assertEqual((self.workspace / "sh.txt").read_text(), "ok")
        manifest = verify_snapshot(self.snapshot, self.digest)
        self.assertEqual((self.workspace / "expr.txt").read_text(), manifest["base_commit"])
        self.assertEqual((self.workspace / "hostenv.txt").read_text(), "")
        self.assertEqual((self.workspace / "source-copy.txt").read_text(), "original\n")
        self.assertEqual((self.workspace / "linked.txt").read_text(), "original\n")
        self.assertEqual((self.repo / "source.txt").read_text(), "mutated checkout\n")
        calls = self._calls()
        creates = [call for call in calls if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)
        create = creates[0]
        self.assertIn("--network", create)
        self.assertEqual(create[create.index("--network") + 1], "bridge")
        self.assertNotIn("--privileged", create)
        rendered = "\n".join(repr(call) for call in calls)
        self.assertNotIn("docker.sock", rendered)
        self.assertNotIn(".ssh", rendered)
        self.assertNotIn("fixture-secret-value", rendered)
        self.assertNotIn(self.image.split(":", 1)[0] + ":latest", rendered)
        name = create[create.index("--name") + 1]
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.stdout.strip(), "")
        self.assertEqual(stat.S_IMODE((self.workspace / "order.txt").stat().st_mode) & 0o777, 0o644)

    def _owned_checkout(self, workflow, label, uses="actions/checkout@v4"):
        root = self.root / label
        root.mkdir()
        repo = root / "repo"
        repo.mkdir()
        workflow_path = repo / ".github" / "workflows" / "test.yml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(workflow)
        (repo / "source.txt").write_text("original\n")
        app = repo / "app"
        app.mkdir()
        (app / "keep.txt").write_text("keep\n")
        state = root / "state"
        capture = SourceCapture(repo, state)
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "add", ".")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        (repo / "source.txt").write_text("dirty-bytes\n")
        captured = capture.capture(".github/workflows/test.yml")
        snapshot = state / "snapshots" / captured["snapshot_id"]
        attempts = state / "attempts"
        attempts.mkdir(mode=0o700)
        workspace = attempts / "run-1"
        materialize_attempt(snapshot, captured["digest"], workspace)
        manifest = (snapshot / "manifest.json").read_bytes()
        digest_before = hashlib.sha256(manifest).hexdigest()
        self.assertEqual(digest_before, captured["digest"])
        planned = plan_snapshot(snapshot, "build")
        step = planned["plan"]["job"]["steps"][0]
        self.assertEqual(planned["plan"]["capability_version"], 12)
        self.assertEqual(step["uses"], uses)
        self.assertEqual(step["checkout"], "captured")
        for absent in ("action_path", "action_digest", "steps", "inputs", "outputs"):
            self.assertNotIn(absent, step)
        before = self._calls() if self.log.exists() else []
        result = run_job(
            snapshot,
            captured["digest"],
            workspace,
            planned["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            hashlib.sha256((snapshot / "manifest.json").read_bytes()).hexdigest(),
            digest_before,
        )
        self.assertEqual((snapshot / "files" / "source.txt").read_text(), "dirty-bytes\n")
        self.assertEqual((workspace / "source.txt").read_text(), "dirty-bytes\n")
        self.assertEqual((workspace / "seen.txt").read_text(), "dirty-bytes\n")
        self.assertTrue((workspace / ".git").is_dir())
        self.assertFalse((workspace / ".git").is_symlink())
        self.assertFalse((workspace / "git.json").exists())
        self.assertFalse((workspace / "objects").exists())
        self.assertFalse((snapshot / "files" / ".git").exists())
        self.assertEqual((repo / "source.txt").read_text(), "dirty-bytes\n")
        fresh = self._calls()[len(before) :]
        execs = [call for call in fresh if call and call[0] == "exec"]
        probes = [call for call in execs if call[-3:] == ["bash", "-c", "exit 0"]]
        self.assertEqual(len(probes), 1)
        self.assertEqual(len(execs), 2)
        return result

    def test_owned_checkout_leaves_captured_files(self):
        omitted = """\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        shell: not-a-shell
        working-directory: missing-dir
        timeout-minutes: 1
        env:
          MODE: kept
      - if: false
        uses: actions/checkout@v4
      - id: see
        if: github.sha == '' && github.token == ''
        run: |
          cat "$GITHUB_WORKSPACE/source.txt" > "$GITHUB_WORKSPACE/seen.txt"
          if [ ! -d "$GITHUB_WORKSPACE/.git" ] || [ -e "$GITHUB_WORKSPACE/git.json" ] || [ -e "$GITHUB_WORKSPACE/objects" ]; then exit 2; fi
"""
        result = self._owned_checkout(omitted, "omitted")
        self.assertEqual(
            [(step["status"], step["exit_code"]) for step in result["steps"]],
            [("succeeded", 0), ("skipped", None), ("succeeded", 0)],
        )
        self.assertEqual(result["steps"][0]["stdout"], "")
        self.assertNotIn("outputs", result["steps"][0])
        flagged = """\
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          clean: false
          persist-credentials: false
      - id: see
        if: github.sha == '' && github.token == ''
        run: |
          cat "$GITHUB_WORKSPACE/source.txt" > "$GITHUB_WORKSPACE/seen.txt"
          if [ ! -d "$GITHUB_WORKSPACE/.git" ] || [ -e "$GITHUB_WORKSPACE/git.json" ] || [ -e "$GITHUB_WORKSPACE/objects" ]; then exit 2; fi
"""
        flagged_result = self._owned_checkout(flagged, "flagged")
        self.assertEqual(
            [(step["status"], step["exit_code"]) for step in flagged_result["steps"]],
            [("succeeded", 0), ("succeeded", 0)],
        )
        stored = plan_workflow(flagged.encode(), "build")["plan"]["job"]["steps"][0]
        self.assertEqual(stored["with"], {"clean": False, "persist-credentials": False})

    def test_check_yml_sha_leaves_captured_files(self):
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
      - id: see
        if: github.sha == '' && github.token == ''
        run: |
          cat "$GITHUB_WORKSPACE/source.txt" > "$GITHUB_WORKSPACE/seen.txt"
          if [ ! -d "$GITHUB_WORKSPACE/.git" ] || [ -e "$GITHUB_WORKSPACE/git.json" ] || [ -e "$GITHUB_WORKSPACE/objects" ]; then exit 2; fi
"""
        result = self._owned_checkout(workflow, "sha", uses=f"actions/checkout@{sha}")
        self.assertEqual(
            [(step["status"], step["exit_code"]) for step in result["steps"]],
            [("succeeded", 0), ("succeeded", 0)],
        )
        self.assertEqual(result["steps"][0]["stdout"], "")
        self.assertNotIn("outputs", result["steps"][0])

    def test_network_none_is_an_explicit_create_argument(self):
        offline = "on: push\njobs:\n  build:\n    steps:\n      - run: echo ok\n"
        root = self.root / "offline"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, offline)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(offline),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            network="none",
        )
        self.assertEqual(result["status"], "succeeded")
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(creates[-1][creates[-1].index("--network") + 1], "none")
        rendered = "\n".join(repr(call) for call in self._calls())
        self.assertNotIn("docker.sock", rendered)

    def test_docker_socket_reaches_the_engine(self):
        probe = """\
on: push
jobs:
  build:
    steps:
      - run: |
          python -c 'import socket; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5); s.connect("/var/run/docker.sock"); s.sendall(b"GET /version HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n"); data=s.recv(256); raise SystemExit(0 if data.startswith(b"HTTP/") else 1)'
"""
        root = self.root / "socket"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, probe)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(probe),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            docker_socket=True,
        )
        self.assertEqual(result["status"], "succeeded", result)
        creates = [call for call in self._calls() if call and call[0] == "create"]
        create = creates[-1]
        mounts = [
            item
            for item in create
            if item.startswith("type=bind,source=") and "destination=/var/run/docker.sock" in item
        ]
        self.assertEqual(len(mounts), 1)
        source = mounts[0].split("source=", 1)[1].split(",", 1)[0]
        info = os.stat(source)
        self.assertTrue(stat.S_ISSOCK(info.st_mode), "mounted source is not a socket")
        self.assertEqual(create[create.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        groups = [create[index + 1] for index, item in enumerate(create) if item == "--group-add"]
        expected = {"0"}
        if info.st_gid != 0:
            expected.add(str(info.st_gid))
        self.assertEqual(set(groups), expected)
        self.assertNotIn("--privileged", create)
        rendered = "\n".join(repr(call) for call in self._calls())
        self.assertNotIn(".ssh", rendered)

    def _same_path_mounts(self, create):
        found = []
        for item in create:
            if not isinstance(item, str) or not item.startswith("type=bind,"):
                continue
            parts = {}
            for piece in item.split(","):
                if "=" not in piece:
                    continue
                key, value = piece.split("=", 1)
                parts[key] = value
            if "source" in parts and parts["source"] == parts.get("destination"):
                found.append(parts["source"])
        return found

    def _volume_mounts(self, create):
        found = []
        for item in create:
            if not isinstance(item, str) or not item.startswith("type=volume,"):
                continue
            parts = {}
            for piece in item.split(","):
                if "=" not in piece:
                    continue
                key, value = piece.split("=", 1)
                parts[key] = value
            if "source" in parts and "destination" in parts:
                found.append((parts["source"], parts["destination"]))
        return found

    def _assert_socket_volume_removed(self, create):
        mounts = self._volume_mounts(create)
        self.assertEqual(len(mounts), 1)
        name, destination = mounts[0]
        self.assertTrue(name.startswith("rookrunner-socket-"))
        self.assertTrue(destination.startswith("/"))
        self.assertNotIn(",", destination)
        listed = subprocess.run(
            ["docker", "volume", "inspect", name],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(listed.returncode, 0)
        return name, destination

    def test_socket_volume_is_mounted_at_its_mountpoint(self):
        probe = """\
on: push
jobs:
  build:
    steps:
      - run: |
          printf '%s\\n' "$TMPDIR"
          test "$TEMP" = "$TMPDIR"
          test "$TMP" = "$TMPDIR"
          test -d "$TMPDIR"
          test -w "$TMPDIR"
          test "$(stat -c %a "$TMPDIR")" = 700
          touch "$TMPDIR/probe"
"""
        root = self.root / "socket-temp"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, probe)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(probe),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            docker_socket=True,
        )
        self.assertEqual(result["status"], "succeeded", result)
        path = result["steps"][0]["stdout"].strip()
        creates = [call for call in self._calls() if call and call[0] == "create"]
        _name, destination = self._assert_socket_volume_removed(creates[-1])
        self.assertEqual(destination, path)
        # The engine socket is also a same-path mount when it is already
        # `/var/run/docker.sock`. The temporary directory is the volume.
        self.assertNotIn(path, self._same_path_mounts(creates[-1]))
        self.assertNotIn("--privileged", creates[-1])

    def test_socket_off_does_not_share_a_temp_directory(self):
        probe = """\
on: push
jobs:
  build:
    steps:
      - run: |
          if [ -n "${TMPDIR:-}" ]; then exit 1; fi
          if [ -n "${TEMP:-}" ]; then exit 1; fi
          if [ -n "${TMP:-}" ]; then exit 1; fi
          printf unset
"""
        root = self.root / "socket-off"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, probe)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(probe),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["steps"][0]["stdout"], "unset")
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(self._volume_mounts(creates[-1]), [])
        self.assertEqual(self._same_path_mounts(creates[-1]), [])

    def test_workflow_env_replaces_the_socket_temp(self):
        probe = """\
on: push
env:
  TMPDIR: /tmp/from-workflow
  TEMP: /tmp/from-workflow
  TMP: /tmp/from-workflow
jobs:
  build:
    steps:
      - run: printf '%s %s %s\\n' "$TMPDIR" "$TEMP" "$TMP"
"""
        root = self.root / "socket-override"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, probe)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(probe),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            docker_socket=True,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(
            result["steps"][0]["stdout"],
            "/tmp/from-workflow /tmp/from-workflow /tmp/from-workflow\n",
        )
        creates = [call for call in self._calls() if call and call[0] == "create"]
        _name, destination = self._assert_socket_volume_removed(creates[-1])
        self.assertNotEqual(destination, "/tmp/from-workflow")

    def test_nonzero_step_stops_and_names_the_digest(self):
        fail_root = self.root / "fail"
        fail_root.mkdir()
        _root, snapshot, digest, workspace = _capture(fail_root, FAILURE)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(FAILURE),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["image_digest"], self.image)
        self.assertEqual(result["failed_step"]["index"], 1)
        self.assertEqual(result["failed_step"]["id"], "fail")
        self.assertEqual(result["failed_step"]["name"], "fail step")
        self.assertEqual([step["index"] for step in result["steps"]], [0, 1, 2])
        self.assertEqual(result["steps"][1]["exit_code"], 3)
        self.assertEqual(result["steps"][2]["status"], "skipped")
        self.assertIsNone(result["steps"][2]["exit_code"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "order.txt").read_text(), "one\n")
        self.assertFalse((workspace / "order.txt").read_text().endswith("later\n"))

    def test_step_if_skips_a_false_condition(self):
        result = run_job(
            self.snapshot,
            self.digest,
            self.workspace,
            _plan(CONDITIONS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["skipped", "succeeded", "succeeded"],
        )
        self.assertFalse((self.workspace / "skipped.txt").exists())
        self.assertEqual((self.workspace / "kept.txt").read_text(), "kept\n")
        self.assertEqual((self.workspace / "blank.txt").read_text(), "blank\n")

    def test_always_runs_after_a_failed_step(self):
        result = run_job(
            self.snapshot,
            self.digest,
            self.workspace,
            _plan(ALWAYS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual([step["status"] for step in result["steps"]], ["failed", "succeeded"])
        self.assertEqual((self.workspace / "after.txt").read_text(), "after\n")

    def test_needed_job_passes_outputs_and_withholds_secrets(self):
        root = self.root / "outputs"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, OUTPUTS)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(OUTPUTS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([step["job_id"] for step in result["steps"]], ["one", "build", "build"])
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual((workspace / "kept.txt").read_text(), "kept\n")
        self.assertEqual((workspace / "blank.txt").read_text(), "blank\n")
        rendered = "\n".join(
            (step.get("stdout") or "") + (step.get("stderr") or "") for step in result["steps"]
        )
        self.assertNotIn("super-secret-value", rendered)
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)

    def test_failed_need_skips_the_next_job_and_always_runs(self):
        root = self.root / "chain"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, CHAIN)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(CHAIN),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["failed_step"]["id"], "fail")
        self.assertEqual([step["job_id"] for step in result["steps"]], ["one", "two", "build"])
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["failed", "skipped", "succeeded"],
        )
        self.assertFalse((workspace / "ran.txt").exists())
        self.assertEqual((workspace / "kept.txt").read_text(), "kept\n")
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)

    def test_false_job_if_succeeds_without_running(self):
        workflow = """\
on: push
jobs:
  build:
    if: false
    steps:
      - run: printf '%s\\n' ran > "$GITHUB_WORKSPACE/ran.txt"
"""
        root = self.root / "skip-job"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["steps"][0]["status"], "skipped")
        self.assertEqual(result["steps"][0]["job_id"], "build")
        self.assertFalse((workspace / "ran.txt").exists())

    def test_environment_files_apply_to_later_steps(self):
        root = self.root / "commands"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, COMMANDS)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(COMMANDS),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([step["job_id"] for step in result["steps"]], ["one", "one", "build"])
        self.assertEqual((workspace / "same.txt").read_text(), "")
        self.assertEqual((workspace / "env.txt").read_text(), "yellow\n")
        self.assertEqual((workspace / "msg.txt").read_text(), "one\ntwo\n")
        self.assertEqual((workspace / "ws.txt").read_text(), "/workspace\n")
        self.assertEqual((workspace / "node.txt").read_text(), "")
        self.assertEqual((workspace / "cmd.txt").read_text(), "")
        self.assertEqual((workspace / "cross.txt").read_text(), "")
        path = (workspace / "path.txt").read_text()
        self.assertTrue(path.startswith("/workspace/bin:"))
        self.assertNotIn("not-added", path)
        color, later, build = result["steps"]
        self.assertIn("***", color["stdout"])
        self.assertNotIn("mask-token", color["stdout"])
        self.assertNotIn("Mona The Octocat", color["stdout"])
        self.assertIn("::add-mask::visible-command", color["stdout"])
        self.assertIn("Missing semicolon", color["stdout"])
        self.assertNotIn("::warning::", color["stdout"])
        self.assertIn("seen", later["stdout"])
        self.assertNotIn("mask-token", later["stdout"])
        self.assertNotIn("Mona The Octocat", later["stdout"])
        self.assertIn("mask-token", build["stdout"])
        self.assertEqual((workspace / "kept.txt").read_text(), "kept\n")
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)

    def test_failed_step_env_reaches_a_later_step(self):
        root = self.root / "failed-env"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, FAILED_ENV)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(FAILED_ENV),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual([step["status"] for step in result["steps"]], ["failed", "succeeded"])
        self.assertEqual((workspace / "after.txt").read_text(), "yes")
        self.assertNotIn("no", result["steps"][0]["stdout"])

    def test_unresolvable_digest_is_setup_failure(self):
        with self.assertRaises(RunError) as raised:
            run_job(
                self.snapshot,
                self.digest,
                self.workspace,
                _plan(SUCCESS),
                "sha256:" + "ab" * 32,
                EVENT,
                docker=str(self.docker),
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("will not resolve", str(raised.exception))
        self.assertFalse(any(call and call[0] == "create" for call in self._calls()))

    def test_step_timeout_argument_stops_before_the_job_deadline(self):
        workflow = """\
on: push
jobs:
  build:
    timeout-minutes: 1
    steps:
      - id: sleep
        run: sleep infinity
      - id: after
        run: echo after
"""
        root = self.root / "step-timeout"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        started = time.monotonic()
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker="docker",
            step_timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["steps"][0]["error"], "step timed out")
        self.assertEqual([step["id"] for step in result["steps"]], ["sleep"])
        self.assertNotEqual(result["status"], "succeeded")
        # 2s step ceiling plus the 7.5s + 2.5s cancellation grace, with slack.
        self.assertLess(elapsed, 20)
        self.assertGreater(elapsed, 1)
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(foreign_ids(listed.stdout), [])

    def test_remote_composite_runs_and_stays_out_of_the_workspace_git(self):
        remote = self.root / "remote"
        action = remote / "acme" / "hello.git"
        action.mkdir(parents=True)
        _git(action, "init", "--initial-branch=main")
        _git(action, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _git(action, "config", "uploadpack.allowAnySHA1InWant", "true")
        (action / "action.yml").write_text(
            "name: Hello\n"
            "description: remote hello\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      run: |\n"
            '        if [ -n "$GITHUB_TOKEN" ]; then exit 4; fi\n'
            "        printf '%s\\n' remote-ok > \"$GITHUB_WORKSPACE/marker.txt\"\n"
        )
        _git(action, "add", ".")
        _git(
            action,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "action",
        )
        sha = subprocess.run(
            ["git", "-C", action, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        workflow = (
            "name: demo\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            f"      - uses: acme/hello@{sha}\n"
        )
        root = self.root / "remote-run"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        store = ActionStore(root / "state", remote, 10 * 1024**3)
        planned = plan_snapshot(snapshot, "build", action_store=store)
        step = planned["plan"]["job"]["steps"][0]
        self.assertEqual(step["action_commit"], sha)
        self.assertEqual(len(step["content_digest"]), 64)
        result = run_job(
            snapshot,
            digest,
            workspace,
            planned["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((workspace / "marker.txt").read_text(), "remote-ok\n")
        self.assertFalse((workspace / "action.yml").exists())
        listed = subprocess.run(
            ["git", "-C", workspace, "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        names = listed.stdout.split()
        self.assertNotIn("action.yml", names)
        self.assertNotIn("marker.txt", names)
        count = subprocess.run(
            ["git", "-C", workspace, "rev-list", "--count", "--all"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(count.stdout.strip(), "1")
        self.assertEqual(store.fetches, 1)

    def test_local_composite_uses_the_planned_action(self):
        root = self.root / "composite"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(
            root,
            COMPOSITE,
            {".github/actions/hello/action.yml": COMPOSITE_ACTION},
        )
        plan = plan_snapshot(snapshot, "build")["plan"]
        self.assertIn("PLANNED", plan["job"]["steps"][1]["steps"][0]["run"])
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan,
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([step["id"] for step in result["steps"]], [None, "hello", "later"])
        self.assertEqual(result["steps"][1]["stdout"], "who is old\n")
        self.assertEqual((workspace / "marker.txt").read_text(), "PLANNED\n")
        self.assertIn(
            "MUTATED",
            (workspace / ".github" / "actions" / "hello" / "action.yml").read_text(),
        )
        self.assertIn(
            "PLANNED",
            (snapshot / "files" / ".github" / "actions" / "hello" / "action.yml").read_text(),
        )
        self.assertEqual(result["steps"][1]["name"], "greet Mona")
        self.assertEqual((workspace / "who.txt").read_text(), "Mona\n")
        self.assertEqual((workspace / "title.txt").read_text(), "Dr\n")
        self.assertEqual((workspace / "literal.txt").read_text(), "Mona\n")
        self.assertEqual((workspace / "literal-env.txt").read_text(), "hello Mona\n")
        self.assertEqual(
            (workspace / "action-path.txt").read_text(),
            "/workspace/.github/actions/hello\n",
        )
        self.assertEqual((workspace / "input-env.txt").read_text(), "")
        self.assertEqual((workspace / "next.txt").read_text(), "next\n")
        self.assertFalse((workspace / "skipped.txt").exists())
        self.assertEqual((workspace / "from-action.txt").read_text(), "yes\n")
        self.assertEqual((workspace / "tone.txt").read_text(), "loud\n")
        self.assertEqual((workspace / "outer-action-path.txt").read_text(), "")

    def test_failed_composite_step_still_runs_always(self):
        root = self.root / "composite-fail"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(
            root,
            COMPOSITE_FAIL,
            {".github/actions/fail/action.yml": COMPOSITE_FAIL_ACTION},
        )
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_snapshot(snapshot, "build")["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["failed_step"]["id"], "boom")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertEqual((workspace / "after.txt").read_text(), "after\n")

    def _matrix_job(self, workflow, job_id="build"):
        root = self.root / job_id
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_workflow(workflow.encode(), job_id)["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        return result, workspace

    def test_fail_fast_skips_later_combinations(self):
        workflow = """\
on: push
jobs:
  build:
    strategy:
      matrix:
        version: [1, 2]
    steps:
      - if: matrix.version == 1
        run: printf 'one\\n' >> "$GITHUB_WORKSPACE/order.txt"
      - if: matrix.version == 1
        run: exit 1
      - if: matrix.version == 2
        run: printf 'two\\n' >> "$GITHUB_WORKSPACE/order.txt"
"""
        result, workspace = self._matrix_job(workflow)
        self.assertEqual(result["status"], "failed")
        self.assertEqual((workspace / "order.txt").read_text(), "one\n")
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["succeeded", "failed", "skipped"],
        )

    def test_fail_fast_false_runs_every_combination(self):
        workflow = """\
on: push
jobs:
  build:
    strategy:
      fail-fast: false
      max-parallel: 1
      matrix:
        version: [1, 2]
    steps:
      - if: matrix.version == 1
        run: printf 'one\\n' >> "$GITHUB_WORKSPACE/order.txt"
      - if: "${{ strategy.max-parallel == 1 && strategy.job-total == 2 }}"
        run: printf 'cap\\n' >> "$GITHUB_WORKSPACE/order.txt"
      - if: matrix.version == 1
        run: exit 1
      - if: matrix.version == 2
        run: printf 'two\\n' >> "$GITHUB_WORKSPACE/order.txt"
"""
        result, workspace = self._matrix_job(workflow)
        self.assertEqual(result["status"], "failed")
        self.assertEqual((workspace / "order.txt").read_text(), "one\ncap\ncap\ntwo\n")
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            [
                "succeeded",
                "succeeded",
                "failed",
                "skipped",
                "skipped",
                "succeeded",
                "skipped",
                "succeeded",
            ],
        )

    def test_combinations_run_in_order_under_max_parallel(self):
        workflow = """\
on: push
jobs:
  build:
    strategy:
      max-parallel: 2
      matrix:
        version: [1, 2]
    steps:
      - if: strategy.job-index == 0
        run: printf 'zero\\n' >> "$GITHUB_WORKSPACE/order.txt"
      - if: strategy.job-index == 1
        run: printf 'one\\n' >> "$GITHUB_WORKSPACE/order.txt"
      - if: strategy.fail-fast
        run: printf 'fast\\n' >> "$GITHUB_WORKSPACE/order.txt"
"""
        result, workspace = self._matrix_job(workflow)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "order.txt").read_text(), "zero\nfast\none\nfast\n")

    def test_matrix_job_outputs_are_not_copied_to_needs(self):
        workflow = """\
on: push
jobs:
  build:
    strategy:
      matrix:
        version: [1]
    outputs:
      value: ${{ matrix.version }}
    steps:
      - run: printf 'built\\n' >> "$GITHUB_WORKSPACE/order.txt"
  report:
    needs: build
    steps:
      - if: "${{ needs.build.outputs.value == '' }}"
        run: printf 'empty\\n' >> "$GITHUB_WORKSPACE/order.txt"
"""
        result, workspace = self._matrix_job(workflow, "report")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "order.txt").read_text(), "built\nempty\n")

    def test_excluded_matrix_runs_no_steps(self):
        workflow = """\
on: push
jobs:
  build:
    strategy:
      matrix:
        version: [1]
        exclude:
          - version: 1
    steps:
      - run: printf 'ran\\n' > "$GITHUB_WORKSPACE/order.txt"
"""
        result, workspace = self._matrix_job(workflow)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"], [])
        self.assertFalse((workspace / "order.txt").exists())

    def _reusable(self, name, workflow, extra, job_id):
        root = self.root / name
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow, extra)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_snapshot(snapshot, job_id)["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        return result, workspace

    def test_called_workflow_sees_typed_inputs(self):
        called = """\
on:
  workflow_call:
    inputs:
      username:
        required: true
        type: string
      count:
        required: true
        type: number
      flag:
        required: true
        type: boolean
      optional_flag:
        type: boolean
      optional_count:
        type: number
      optional_name:
        type: string
jobs:
  build:
    steps:
      - if: "${{ inputs.username == 'ada' && inputs.count == 2 && inputs.flag == true && inputs.optional_flag == false && inputs.optional_count == 0 && inputs.optional_name == '' }}"
        run: printf 'typed\\n' > "$GITHUB_WORKSPACE/marker.txt"
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
        result, workspace = self._reusable(
            "typed",
            workflow,
            {".github/workflows/called.yml": called},
            "call",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "typed\n")

    def test_workflow_output_reaches_the_caller_needs(self):
        called = """\
on:
  workflow_call:
    outputs:
      word:
        value: ${{ jobs.build.outputs.word }}
jobs:
  build:
    outputs:
      word: ${{ steps.say.outputs.word }}
    steps:
      - id: say
        run: echo "word=hi" >> "$GITHUB_OUTPUT"
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
  report:
    needs: call
    steps:
      - if: "${{ needs.call.outputs.word == 'hi' }}"
        run: printf 'seen\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        result, workspace = self._reusable(
            "outputs",
            workflow,
            {".github/workflows/called.yml": called},
            "report",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "seen\n")

    def test_caller_env_does_not_enter_the_called_workflow(self):
        called = """\
on: workflow_call
env:
  FOO: from-called
jobs:
  build:
    steps:
      - run: printf '%s %s\\n' "$FOO" "$ONLY_CALLER" > "$GITHUB_WORKSPACE/marker.txt"
"""
        workflow = """\
on: push
env:
  FOO: from-caller
  ONLY_CALLER: from-caller
jobs:
  call:
    uses: ./.github/workflows/called.yml
"""
        result, workspace = self._reusable(
            "env",
            workflow,
            {".github/workflows/called.yml": called},
            "call",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "from-called \n")

    def test_nested_reusable_workflow_runs(self):
        inner = """\
on: workflow_call
jobs:
  build:
    steps:
      - run: printf 'nested\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        middle = """\
on: workflow_call
jobs:
  call:
    uses: ./.github/workflows/inner.yml
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/middle.yml
"""
        result, workspace = self._reusable(
            "nested",
            workflow,
            {
                ".github/workflows/middle.yml": middle,
                ".github/workflows/inner.yml": inner,
            },
            "call",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "nested\n")

    def test_with_expression_reads_an_earlier_output(self):
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
      - if: "${{ inputs.username == 'ada' }}"
        run: printf 'ada\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        workflow = """\
on: push
jobs:
  first:
    outputs:
      name: ${{ steps.say.outputs.name }}
    steps:
      - id: say
        run: echo "name=ada" >> "$GITHUB_OUTPUT"
  call:
    needs: first
    uses: ./.github/workflows/called.yml
    with:
      username: ${{ needs.first.outputs.name }}
"""
        result, workspace = self._reusable(
            "passed",
            workflow,
            {".github/workflows/called.yml": called},
            "call",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "ada\n")

    def test_false_caller_if_skips_the_called_workflow(self):
        called = """\
on: workflow_call
jobs:
  build:
    steps:
      - run: printf 'ran\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        workflow = """\
on: push
jobs:
  call:
    if: "${{ false }}"
    uses: ./.github/workflows/called.yml
"""
        result, workspace = self._reusable(
            "skipped",
            workflow,
            {".github/workflows/called.yml": called},
            "call",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"][0]["status"], "skipped")
        self.assertFalse((workspace / "marker.txt").exists())

    def test_runtime_input_type_mismatch_fails_the_run(self):
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
      - run: printf 'ran\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        workflow = """\
on: push
jobs:
  call:
    uses: ./.github/workflows/called.yml
    with:
      flag: ${{ 'true' }}
"""
        result, workspace = self._reusable(
            "mismatch",
            workflow,
            {".github/workflows/called.yml": called},
            "call",
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertIn("boolean", result["steps"][0]["error"])
        self.assertFalse((workspace / "marker.txt").exists())

    def test_entry_workflow_call_defaults_are_not_applied(self):
        workflow = """\
on:
  workflow_call:
    inputs:
      name:
        type: string
        default: ada
jobs:
  build:
    steps:
      - if: "${{ inputs.name == '' }}"
        run: printf 'empty\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        result, workspace = self._reusable("entry", workflow, {}, "build")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "marker.txt").read_text(), "empty\n")

    def _service_workflow(self, body):
        return f"on: push\njobs:\n  build:\n{body}\n"

    def _run_service(self, workflow, **kwargs):
        root = self.root / "services"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            **kwargs,
        )
        return result, workspace

    def test_service_is_reachable_by_its_label(self):
        workflow = self._service_workflow(
            f"""\
    services:
      echo:
        image: {self.image}
        env:
          ROLE: service
        command: python -c "import http.server; http.server.ThreadingHTTPServer(('0.0.0.0', 8080), http.server.BaseHTTPRequestHandler).serve_forever()"
    steps:
      - run: |
          python -c 'import socket,time
          last=None
          for _ in range(50):
            try:
              s=socket.create_connection(("echo", 8080), 2)
              s.close()
              raise SystemExit(0)
            except OSError as exc:
              last=exc
              time.sleep(0.2)
          raise SystemExit(last)'
"""
        )
        result, _workspace = self._run_service(workflow)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["steps"][0]["exit_code"], 0)
        creates = [call for call in self._calls() if call and call[0] == "create"]
        service = [call for call in creates if "--label" in call]
        job = [call for call in creates if "--label" not in call]
        self.assertEqual(len(job), 1)
        self.assertEqual(len(service), 1)
        self.assertEqual(job[0][job[0].index("--network") + 1], "bridge")
        self.assertNotIn("--privileged", service[0])
        self.assertNotIn("--user", service[0])
        self.assertNotIn("/workspace", " ".join(service[0]))
        self.assertNotIn("docker.sock", " ".join(service[0]))
        self.assertIn("rookrunner.owner=" + job[0][job[0].index("--name") + 1], service[0])
        self.assertEqual(service[0][service[0].index("--network-alias") + 1], "echo")
        self.assertIn("--env", service[0])
        self.assertIn("ROLE=service", service[0])
        networks = [call for call in self._calls() if call[:2] == ["network", "create"]]
        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0][networks[0].index("--driver") + 1], "bridge")
        self.assertTrue(networks[0][-1].startswith("rookrunner-net-"))
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(foreign_ids(listed.stdout), [])
        left = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", "name=rookrunner-net-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(left.stdout.strip(), "")

    def test_skipped_job_does_not_start_a_service(self):
        workflow = self._service_workflow(
            f"""\
    if: "${{{{ false }}}}"
    services:
      echo:
        image: {self.image}
        command: sleep infinity
    steps:
      - run: echo hi
"""
        )
        result, _workspace = self._run_service(workflow)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"][0]["status"], "skipped")
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertNotIn("--label", creates[0])

    def test_exited_service_fails_setup_before_steps(self):
        workflow = self._service_workflow(
            f"""\
    services:
      gone:
        image: {self.image}
        command: python -c "import sys; sys.exit(1)"
    steps:
      - run: printf 'ran\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        )
        root = self.root / "exited"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        with self.assertRaises(RunError) as raised:
            run_job(
                snapshot,
                digest,
                workspace,
                _plan(workflow),
                self.image,
                EVENT,
                docker=str(self.docker),
                step_timeout=60,
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("service container", str(raised.exception))
        self.assertFalse((workspace / "marker.txt").exists())
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(foreign_ids(listed.stdout), [])

    def test_network_none_does_not_start_services(self):
        workflow = self._service_workflow(
            f"""\
    services:
      echo:
        image: {self.image}
        command: sleep infinity
    steps:
      - run: printf 'ran\\n' > "$GITHUB_WORKSPACE/marker.txt"
"""
        )
        root = self.root / "offline-service"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        with self.assertRaises(RunError) as raised:
            run_job(
                snapshot,
                digest,
                workspace,
                _plan(workflow),
                self.image,
                EVENT,
                docker=str(self.docker),
                step_timeout=60,
                network="none",
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertIn("network none", str(raised.exception))
        self.assertFalse((workspace / "marker.txt").exists())
        creates = [call for call in self._calls() if call and call[0] == "create"]
        self.assertTrue(creates)
        self.assertTrue(all("--label" not in call for call in creates))

    def test_service_does_not_receive_the_engine_socket(self):
        workflow = self._service_workflow(
            f"""\
    services:
      echo:
        image: {self.image}
        command: sleep infinity
    steps:
      - run: |
          python -c 'import json,socket
          def get(path):
            s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect("/var/run/docker.sock")
            s.sendall(("GET "+path+" HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n").encode())
            data=b""
            while True:
              chunk=s.recv(65536)
              if not chunk:
                break
              data+=chunk
            return json.loads(data.split(b"\\r\\n\\r\\n",1)[1])
          rows=get("/containers/json?all=1")
          found=False
          for row in rows:
            if any("rookrunner-svc-" in name for name in row.get("Names") or []):
              found=True
              info=get("/containers/"+row["Id"]+"/json")
              mounts=info.get("Mounts") or []
              if any(item.get("Destination")=="/var/run/docker.sock" for item in mounts):
                raise SystemExit("socket")
              if info.get("HostConfig",{{}}).get("Privileged"):
                raise SystemExit("privileged")
          raise SystemExit(0 if found else 2)'
"""
        )
        result, _workspace = self._run_service(workflow, docker_socket=True)
        self.assertEqual(result["status"], "succeeded", result)
        creates = [call for call in self._calls() if call and call[0] == "create"]
        job = next(call for call in creates if "--label" not in call)
        service = next(call for call in creates if "--label" in call)
        self.assertTrue(
            any("destination=/var/run/docker.sock" in item for item in job),
        )
        self.assertFalse(any("docker.sock" in item for item in service))
        self.assertNotIn("--privileged", service)
        self.assertEqual(self._volume_mounts(service), [])
        self.assertEqual(self._same_path_mounts(service), [])
        self.assertEqual(len(self._volume_mounts(job)), 1)
        self.assertNotIn("rookrunner-socket-", " ".join(service))

    def test_cancel_removes_the_service_container(self):
        workflow = self._service_workflow(
            f"""\
    services:
      echo:
        image: {self.image}
        command: sleep infinity
    steps:
      - run: sleep 30
"""
        )
        root = self.root / "cancel-service"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        owner = ContainerLease()

        def cancel_when_started():
            for _ in range(100):
                listed = subprocess.run(
                    ["docker", "ps", "-aq", "--filter", "name=rookrunner-svc-"],
                    capture_output=True,
                    text=True,
                )
                if listed.stdout.strip():
                    owner.request_cancel()
                    owner.stop()
                    return
                time.sleep(0.1)

        thread = threading.Thread(target=cancel_when_started)
        thread.start()
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            owner=owner,
        )
        thread.join(40)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["status"], "cancelled", result)
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=rookrunner-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(foreign_ids(listed.stdout), [])
        left = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", "name=rookrunner-net-"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(left.stdout.strip(), "")

    def test_release_removes_a_service_container_and_its_network(self):
        suffix = os.urandom(8).hex()
        name = "rookrunner-" + suffix
        other = "rookrunner-" + os.urandom(8).hex()
        network = "rookrunner-net-" + suffix
        service = "rookrunner-svc-" + suffix + "-0"
        subprocess.run(
            ["docker", "network", "create", "--driver", "bridge", network],
            check=True,
            capture_output=True,
        )
        try:
            subprocess.run(
                [
                    "docker",
                    "create",
                    "--name",
                    service,
                    "--network",
                    network,
                    "--label",
                    f"rookrunner.owner={name}",
                    self.image,
                    "sleep",
                    "infinity",
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(["docker", "start", service], check=True, capture_output=True)
            subprocess.run(
                ["docker", "create", "--name", other, self.image, "sleep", "infinity"],
                check=True,
                capture_output=True,
            )
            self.assertTrue(owned_container_present(name))
            self.assertTrue(release_owned_container(name))
            self.assertFalse(owned_container_present(name))
            left = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=^{service}$"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(left.stdout.strip(), "")
            nets = subprocess.run(
                ["docker", "network", "ls", "-q", "--filter", f"name=^{network}$"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(nets.stdout.strip(), "")
            kept = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=^{other}$"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(kept.stdout.strip(), "")
        finally:
            subprocess.run(["docker", "rm", "-f", service, other], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)

    def test_default_variables_match_the_contexts(self):
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Os}} {{.Architecture}}", self.image],
            check=True,
            capture_output=True,
            text=True,
        )
        system, architecture = inspected.stdout.split()
        arch = _runner_arch(system, architecture)
        manifest = verify_snapshot(self.snapshot, self.digest)
        sha = manifest["base_commit"]
        self.assertIs(manifest["dirty"], False)
        self.assertEqual(manifest["included"], [])
        workflow = f"""\
name: demo
on: push
jobs:
  build:
    steps:
      - id: gate
        if: github.workspace == '/workspace' && github.job == 'build' && github.workflow == 'demo' && github.event_path == '/run/rookrunner/event.json' && github.event_name == '' && github.token == '' && github.actor == '' && github.ref == '' && github.repository == '' && runner.os == 'Linux' && runner.arch == '{arch}' && runner.environment == 'self-hosted' && runner.temp == '/github/runner-temp' && runner.tool_cache == '/github/tool-cache' && env.CI == 'true' && env.HOME == '/github/home' && github.sha == '{sha}'
        run: |
          printf '%s' "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/workspace.txt"
          printf '%s' "$GITHUB_JOB" > "$GITHUB_WORKSPACE/job.txt"
          printf '%s' "$GITHUB_WORKFLOW" > "$GITHUB_WORKSPACE/workflow.txt"
          printf '%s' "$GITHUB_EVENT_PATH" > "$GITHUB_WORKSPACE/event-path.txt"
          printf '%s' "$GITHUB_SHA" > "$GITHUB_WORKSPACE/sha.txt"
          printf '%s' "$RUNNER_OS" > "$GITHUB_WORKSPACE/os.txt"
          printf '%s' "$RUNNER_ARCH" > "$GITHUB_WORKSPACE/arch.txt"
          printf '%s' "$RUNNER_ENVIRONMENT" > "$GITHUB_WORKSPACE/environment.txt"
          printf '%s' "$RUNNER_TEMP" > "$GITHUB_WORKSPACE/temp.txt"
          printf '%s' "$RUNNER_TOOL_CACHE" > "$GITHUB_WORKSPACE/tool.txt"
          printf '%s' "$HOME" > "$GITHUB_WORKSPACE/home.txt"
          printf '%s' "$CI" > "$GITHUB_WORKSPACE/ci.txt"
          if [ -n "${{GITHUB_ACTIONS+x}}" ]; then exit 3; fi
          if [ -n "${{GITHUB_EVENT_NAME+x}}" ]; then exit 4; fi
          if [ -n "${{GITHUB_TOKEN+x}}" ]; then exit 5; fi
          printf note > "$HOME/note"
      - id: rewrite
        run: |
          printf '%s\\n' 'GITHUB_WORKSPACE=/tmp' >> "$GITHUB_ENV"
          printf '%s\\n' 'RUNNER_TEMP=/tmp' >> "$GITHUB_ENV"
          printf '%s\\n' 'GITHUB_SHA=stolen' >> "$GITHUB_ENV"
          printf '%s\\n' 'CI=false' >> "$GITHUB_ENV"
          printf '%s\\n' 'HOME=/tmp' >> "$GITHUB_ENV"
      - id: read
        if: env.CI == 'false' && env.HOME == '/tmp' && github.sha == '{sha}'
        run: |
          printf '%s' "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/ws2.txt"
          printf '%s' "$RUNNER_TEMP" > "$GITHUB_WORKSPACE/temp2.txt"
          printf '%s' "$GITHUB_SHA" > "$GITHUB_WORKSPACE/sha2.txt"
          printf '%s' "$CI" > "$GITHUB_WORKSPACE/ci2.txt"
          printf '%s' "$HOME" > "$GITHUB_WORKSPACE/home2.txt"
"""
        result = run_job(
            self.snapshot,
            self.digest,
            self.workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["succeeded", "succeeded", "succeeded"],
        )
        workspace = self.workspace
        self.assertEqual((workspace / "workspace.txt").read_text(), "/workspace")
        self.assertEqual((workspace / "job.txt").read_text(), "build")
        self.assertEqual((workspace / "workflow.txt").read_text(), "demo")
        self.assertEqual((workspace / "event-path.txt").read_text(), "/run/rookrunner/event.json")
        self.assertEqual((workspace / "sha.txt").read_text(), sha)
        self.assertEqual((workspace / "os.txt").read_text(), "Linux")
        self.assertEqual((workspace / "arch.txt").read_text(), arch)
        self.assertEqual((workspace / "environment.txt").read_text(), "self-hosted")
        self.assertEqual((workspace / "temp.txt").read_text(), "/github/runner-temp")
        self.assertEqual((workspace / "tool.txt").read_text(), "/github/tool-cache")
        self.assertEqual((workspace / "home.txt").read_text(), "/github/home")
        self.assertEqual((workspace / "ci.txt").read_text(), "true")
        self.assertEqual((workspace / "ws2.txt").read_text(), "/workspace")
        self.assertEqual((workspace / "temp2.txt").read_text(), "/github/runner-temp")
        self.assertEqual((workspace / "sha2.txt").read_text(), sha)
        self.assertEqual((workspace / "ci2.txt").read_text(), "false")
        self.assertEqual((workspace / "home2.txt").read_text(), "/tmp")
        head = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(head, sha)
        home = workspace.parent / "home"
        note = home / "note"
        self.assertEqual(note.read_text(), "note")
        for name in ("home", "runner-temp", "tool-cache"):
            directory = workspace.parent / name
            self.assertTrue(directory.is_dir(), name)
            self.assertFalse(directory.is_symlink())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        paths = [item["path"] for item in written_files(workspace, self.snapshot)]
        self.assertNotIn("note", paths)
        self.assertFalse(any(path.startswith("home/") or path == "note" for path in paths))
        state = workspace.parent.parent
        used = usage(state)
        size = note.stat().st_size
        note.unlink()
        self.assertGreaterEqual(used - usage(state), size)

    def test_event_name_is_a_context_and_a_variable(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - if: github.event_name == 'name=push' && github.token == ''
        run: printf '%s' "$GITHUB_EVENT_NAME" > "$GITHUB_WORKSPACE/name.txt"
"""
        root = self.root / "event-name"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
            event_name="name=push",
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual((workspace / "name.txt").read_text(), "name=push")

    def test_dirty_capture_leaves_github_sha_unset(self):
        workflow = """\
on: push
jobs:
  build:
    steps:
      - if: github.sha == '' && github.token == ''
        run: |
          if [ -n "${GITHUB_SHA+x}" ]; then exit 3; fi
          printf empty > "$GITHUB_WORKSPACE/sha.txt"
"""
        root = self.root / "dirty-sha"
        root.mkdir()
        repo = root / "repo"
        repo.mkdir()
        workflow_path = repo / ".github" / "workflows" / "test.yml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(workflow)
        (repo / "source.txt").write_text("original\n")
        state = root / "state"
        capture = SourceCapture(repo, state)
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "add", ".")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        (repo / "source.txt").write_text("dirty-bytes\n")
        captured = capture.capture(".github/workflows/test.yml")
        snapshot = state / "snapshots" / captured["snapshot_id"]
        manifest = verify_snapshot(snapshot, captured["digest"])
        self.assertIs(manifest["dirty"], True)
        attempts = state / "attempts"
        attempts.mkdir(mode=0o700)
        workspace = attempts / "run-1"
        materialize_attempt(snapshot, captured["digest"], workspace)
        result = run_job(
            snapshot,
            captured["digest"],
            workspace,
            _plan(workflow),
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual((workspace / "sha.txt").read_text(), "empty")

    def test_runner_temp_is_emptied_per_job_and_home_is_kept(self):
        workflow = """\
on: push
jobs:
  one:
    steps:
      - run: |
          printf kept > "$HOME/stay"
          printf gone > "$RUNNER_TEMP/gone"
          mkdir -p "$RUNNER_TEMP/locked"
          printf stay > "$RUNNER_TEMP/locked/file"
          chmod 555 "$RUNNER_TEMP/locked"
          printf tool > "$RUNNER_TOOL_CACHE/tool"
  two:
    needs: one
    steps:
      - if: github.job == 'two'
        run: |
          test -f "$HOME/stay"
          test ! -e "$RUNNER_TEMP/gone"
          test -f "$RUNNER_TEMP/locked/file"
          test -f "$RUNNER_TOOL_CACHE/tool"
          printf '%s' "$GITHUB_JOB" > "$GITHUB_WORKSPACE/job.txt"
"""
        root = self.root / "temp-job"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_workflow(workflow.encode(), "two")["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual((workspace / "job.txt").read_text(), "two")
        self.assertEqual((workspace.parent / "home" / "stay").read_text(), "kept")
        self.assertFalse((workspace.parent / "runner-temp" / "gone").exists())
        self.assertEqual(
            (workspace.parent / "runner-temp" / "locked" / "file").read_text(),
            "stay",
        )
        self.assertEqual((workspace.parent / "tool-cache" / "tool").read_text(), "tool")
        os.chmod(workspace.parent / "runner-temp" / "locked", 0o700)

    def test_called_workflow_without_a_name_uses_its_path(self):
        called = """\
on: workflow_call
jobs:
  inner:
    steps:
      - if: github.workflow == '.github/workflows/called.yml' && github.job == 'inner' && github.token == ''
        run: printf '%s' "$GITHUB_WORKFLOW $GITHUB_JOB" > "$GITHUB_WORKSPACE/called.txt"
"""
        caller = """\
on: push
jobs:
  use:
    uses: ./.github/workflows/called.yml
"""
        root = self.root / "called-path"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(
            root,
            caller,
            {".github/workflows/called.yml": called},
        )
        planned = plan_snapshot(snapshot, "use")
        result = run_job(
            snapshot,
            digest,
            workspace,
            planned["plan"],
            self.image,
            EVENT,
            docker=str(self.docker),
            step_timeout=60,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(
            (workspace / "called.txt").read_text(),
            ".github/workflows/called.yml inner",
        )

    def _keep_docker_config(self):
        """Keep the Docker context when a test points HOME at a secret store.

        The Docker CLI reads its context from the home directory. These
        tests change HOME. Pinning DOCKER_CONFIG leaves that context in
        place. An already set DOCKER_CONFIG is left alone.
        """

        if os.environ.get("DOCKER_CONFIG"):
            return
        config = Path.home() / ".docker"
        if not config.is_dir():
            return
        os.environ["DOCKER_CONFIG"] = str(config)

        def restore():
            os.environ.pop("DOCKER_CONFIG", None)

        self.addCleanup(restore)

    def _secret_home(self, files, repo_mode=0o700, repository="owner/demo"):
        self._keep_docker_config()
        slot = self.root / f"sec-{len(list(self.root.iterdir()))}"
        slot.mkdir()
        home = slot / "home"
        owner, repo = repository.split("/", 1)
        repo_dir = home / "Secrets" / "rookrunner-secrets" / owner / repo
        repo_dir.mkdir(parents=True)
        for path in (
            home,
            home / "Secrets",
            home / "Secrets" / "rookrunner-secrets",
            home / "Secrets" / "rookrunner-secrets" / owner,
            repo_dir,
        ):
            os.chmod(path, 0o700)
        os.chmod(repo_dir, repo_mode)
        for name, payload in files.items():
            target = repo_dir / name
            target.write_bytes(payload)
            os.chmod(target, 0o600)
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(home)

        def restore():
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous

        self.addCleanup(restore)
        return home

    def _push_event(self, ref="refs/heads/main", full_name="owner/demo"):
        return {
            "ref": ref,
            "repository": {"full_name": full_name, "default_branch": "main"},
        }

    def _secret_forms(self, value):
        import base64
        import json
        from urllib.parse import quote

        return (
            value,
            base64.b64encode(value.encode()).decode("ascii"),
            json.dumps(value)[1:-1],
            quote(value, safe="-._~"),
        )

    def _stored(self, result, plan, workspace, snapshot):
        import json

        chunks = [json.dumps(result), json.dumps(plan)]
        for root in (workspace, snapshot):
            for path in root.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)

    def _assert_secret_hidden(self, blob, forms):
        for form in forms:
            if form and form in blob:
                self.fail("stored text contains a secret form")

    def _run_secret(self, name, workflow, event, secrets, extra=None, job_id="build"):
        root = self.root / name
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(root, workflow, extra)
        plan = plan_snapshot(snapshot, job_id)["plan"]
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan,
            self.image,
            event,
            docker=str(self.docker),
            step_timeout=60,
            event_name="push",
            secrets=secrets,
        )
        return result, plan, workspace, snapshot

    def test_step_env_receives_the_file_and_the_log_is_masked(self):
        from execution_core.secrets import SecretAccess

        value = 'fixture secret "9f3a"'
        forms = self._secret_forms(value)
        self._secret_home({"API": value.encode() + b"\n"})
        workflow = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: ${{ secrets.API }}
          GONE: ${{ secrets.MISSING }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python3 - <<'PY'
          import base64, hashlib, json, os
          from urllib.parse import quote
          value = os.environ["TOKEN"]
          print(value)
          print(base64.b64encode(value.encode()).decode())
          print(json.dumps(value)[1:-1])
          print(quote(value, safe="-._~"))
          root = os.environ["GITHUB_WORKSPACE"]
          digest = hashlib.sha256(value.encode()).hexdigest()
          open(root + "/token.sha256", "w").write(digest)
          open(root + "/gone.txt", "w").write(os.environ.get("GONE", "missing"))
          flag = os.environ.get("GITHUB_TOKEN", "missing")
          open(root + "/token-env.txt", "w").write("empty" if flag == "" else "set")
          PY
      - run: |
          if [ -n "${TOKEN:-}" ]; then printf leaked; else printf absent; fi > "$GITHUB_WORKSPACE/second.txt"
"""
        result, plan, workspace, snapshot = self._run_secret(
            "secret-env",
            workflow,
            self._push_event(),
            SecretAccess("owner/demo"),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            (workspace / "token.sha256").read_text(),
            hashlib.sha256(value.encode()).hexdigest(),
        )
        self.assertEqual((workspace / "gone.txt").read_text(), "")
        self.assertEqual((workspace / "token-env.txt").read_text(), "empty")
        self.assertEqual((workspace / "second.txt").read_text(), "absent")
        self.assertEqual(plan["job"]["steps"][0]["env"]["TOKEN"], "${{ secrets.API }}")
        logged = "\n".join(
            (step.get("stdout") or "") + (step.get("stderr") or "") for step in result["steps"]
        )
        self.assertIn("***", logged)
        self.assertIn("secret MISSING is not set", result["steps"][0]["stderr"])
        self._assert_secret_hidden(self._stored(result, plan, workspace, snapshot), forms)

    def test_composite_with_receives_the_file_on_that_step_only(self):
        from execution_core.secrets import SecretAccess

        value = 'fixture secret "9f3a"'
        forms = self._secret_forms(value)
        self._secret_home({"API": value.encode() + b"\n"})
        action = """\
name: pass
description: pass
inputs:
  token:
    description: token
    required: true
runs:
  using: composite
  steps:
    - shell: bash
      env:
        TOKEN: ${{ inputs.token }}
      run: |
        python3 - <<'PY'
        import hashlib, os
        value = os.environ["TOKEN"]
        print(value)
        digest = hashlib.sha256(value.encode()).hexdigest()
        open(os.environ["GITHUB_WORKSPACE"] + "/token.sha256", "w").write(digest)
        PY
"""
        workflow = """\
on: push
jobs:
  build:
    steps:
      - uses: ./pass
        with:
          token: ${{ secrets.API }}
      - run: |
          if [ -n "${TOKEN:-}" ]; then printf leaked; else printf clean; fi > "$GITHUB_WORKSPACE/later.txt"
"""
        result, plan, workspace, snapshot = self._run_secret(
            "secret-with",
            workflow,
            self._push_event(),
            SecretAccess("owner/demo"),
            {"pass/action.yml": action},
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(plan["job"]["steps"][0]["with"]["token"], "${{ secrets.API }}")
        self.assertEqual(
            (workspace / "token.sha256").read_text(),
            hashlib.sha256(value.encode()).hexdigest(),
        )
        self.assertEqual((workspace / "later.txt").read_text(), "clean")
        logged = "\n".join((step.get("stdout") or "") for step in result["steps"])
        self.assertIn("***", logged)
        self._assert_secret_hidden(self._stored(result, plan, workspace, snapshot), forms)

    def test_call_outputs_receive_the_job_secret_mask(self):
        from unittest.mock import patch

        from execution_core.secrets import SecretAccess

        value = 'fixture secret "9f3a"'
        forms = self._secret_forms(value)
        self._secret_home({"API": value.encode() + b"\n"})
        called = """\
on:
  workflow_call:
    outputs:
      marker:
        value: ${{ jobs.build.outputs.marker }}
jobs:
  build:
    outputs:
      marker: ${{ steps.one.outputs.marker }}
    steps:
      - id: one
        env:
          TOKEN: ${{ secrets.API }}
        run: |
          python3 - <<'PY'
          import hashlib, os
          value = os.environ["TOKEN"]
          digest = hashlib.sha256(value.encode()).hexdigest()
          root = os.environ["GITHUB_WORKSPACE"]
          open(root + "/token.sha256", "w").write(digest)
          open(os.environ["GITHUB_OUTPUT"], "a").write("marker=ok" + chr(10))
          PY
"""
        workflow = """\
on: push
jobs:
  use:
    uses: ./.github/workflows/called.yml
  report:
    needs: use
    steps:
      - if: "${{ needs.use.outputs.marker == 'ok' }}"
        run: printf seen > "$GITHUB_WORKSPACE/seen.txt"
"""
        root = self.root / "secret-call"
        root.mkdir()
        _repo, snapshot, digest, workspace = _capture(
            root, workflow, {".github/workflows/called.yml": called}
        )
        plan = plan_snapshot(snapshot, "report")["plan"]
        flags = []
        real = _job_outputs

        def spy(job, values, used, masks=None):
            found = getattr(masks, "secrets", None)
            flags.append(bool(found))
            return real(job, values, used, masks)

        with patch("execution_core.run._job_outputs", spy):
            result = run_job(
                snapshot,
                digest,
                workspace,
                plan,
                self.image,
                self._push_event(),
                docker=str(self.docker),
                step_timeout=60,
                event_name="push",
                secrets=SecretAccess("owner/demo"),
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "seen.txt").read_text(), "seen")
        self.assertGreaterEqual(sum(1 for item in flags if item), 2)
        self.assertIn(False, flags)
        self._assert_secret_hidden(self._stored(result, plan, workspace, snapshot), forms)

    def test_allowlist_miss_does_not_open_or_inject(self):
        from execution_core.secrets import SecretAccess

        self._secret_home({"API": b"value\n"}, repo_mode=0o777)
        workflow = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: ${{ secrets.API }}
        run: |
          if [ -z "$TOKEN" ]; then printf empty; else printf filled; fi > "$GITHUB_WORKSPACE/seen.txt"
"""
        result, _plan, workspace, _snapshot = self._run_secret(
            "secret-miss",
            workflow,
            self._push_event(ref="refs/heads/feature"),
            SecretAccess("owner/demo"),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "seen.txt").read_text(), "empty")
        text = "\n".join(
            (step.get("error") or "") + (step.get("stderr") or "") for step in result["steps"]
        )
        self.assertNotIn("secret directory", text)
        self.assertNotIn("is not set", text)

    def test_omitted_secrets_keep_the_withheld_context_error(self):
        self._secret_home({"API": b"value\n"}, repo_mode=0o777)
        workflow = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: ${{ secrets.API }}
        run: printf ran > "$GITHUB_WORKSPACE/seen.txt"
"""
        result, _plan, workspace, _snapshot = self._run_secret(
            "secret-off",
            workflow,
            self._push_event(),
            None,
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse((workspace / "seen.txt").exists())
        text = "\n".join((step.get("error") or "") for step in result["steps"])
        self.assertIn("context is not available: secrets", text)
        self.assertNotIn("secret directory", text)

    def test_empty_secret_file_fails_before_the_step_runs(self):
        from execution_core.secrets import SecretAccess

        self._secret_home({"API": b"\n"})
        workflow = """\
on: push
jobs:
  build:
    steps:
      - env:
          TOKEN: ${{ secrets.API }}
        run: printf ran > "$GITHUB_WORKSPACE/ran.txt"
"""
        result, _plan, workspace, _snapshot = self._run_secret(
            "secret-empty",
            workflow,
            self._push_event(),
            SecretAccess("owner/demo"),
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse((workspace / "ran.txt").exists())
        text = "\n".join((step.get("error") or "") for step in result["steps"])
        self.assertIn("secret API is empty", text)

    def test_secret_directory_is_checked_when_nothing_is_referenced(self):
        from execution_core.secrets import SecretAccess

        self._keep_docker_config()
        home = self.root / "empty-home"
        home.mkdir()
        os.chmod(home, 0o700)
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(home)

        def restore():
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous

        self.addCleanup(restore)
        workflow = """\
on: push
jobs:
  build:
    steps:
      - run: printf ran > "$GITHUB_WORKSPACE/ran.txt"
"""
        result, _plan, workspace, _snapshot = self._run_secret(
            "secret-missing-dir",
            workflow,
            self._push_event(),
            SecretAccess("owner/demo"),
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse((workspace / "ran.txt").exists())
        text = "\n".join((step.get("error") or "") for step in result["steps"])
        self.assertIn("secret directory is not accepted", text)
        self.assertNotIn(str(home), text)

        self._secret_home({"HUGE": b"h" * (100 * 1024)})
        workflow_ok = """\
on: push
jobs:
  build:
    steps:
      - run: printf ran > "$GITHUB_WORKSPACE/ran.txt"
"""
        result, _plan, workspace, _snapshot = self._run_secret(
            "secret-unreferenced",
            workflow_ok,
            self._push_event(),
            SecretAccess("owner/demo"),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((workspace / "ran.txt").read_text(), "ran")
