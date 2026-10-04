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

from execution_core.actions import ActionStore
from execution_core.artifacts import written_files
from execution_core.attempt import materialize_attempt
from execution_core.disk import usage
from execution_core.plan import plan_snapshot, plan_workflow
from execution_core.run import (
    ContainerLease,
    RunError,
    _CallInputError,
    _OUTPUT_JOB_BYTES,
    _OUTPUT_RUN_BYTES,
    _WORKFLOW_PATH,
    _commit_sha,
    _empty_directory,
    _exec_limit,
    _job_outputs,
    _read_utf8,
    _resolve_call_inputs,
    _runner_arch,
    _verify_workspace,
    _workflow_label,
    owned_container_present,
    release_owned_container,
    run_job,
)
from execution_core.verify import verify_snapshot
from execution_core.snapshot import SourceCapture


SUCCESS = """\
name: demo
on: push
env:
  LEVEL: workflow
  TRACE: kept
defaults:
  run:
    working-directory: app
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      LEVEL: job
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
          LEVEL: step
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
        self.assertEqual(self.plan["capability_version"], 11)
        stale = dict(self.plan)
        stale["capability_version"] = 10
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

    def test_event_name_is_rejected_before_docker(self):
        for name in ("", "push\n", "push\r", "push\0more"):
            with self.assertRaises(RunError) as raised:
                run_job("snap", "digest", "workspace", {}, "image", {}, event_name=name)
            self.assertEqual(raised.exception.kind, "SETUP_FAILED")
            self.assertEqual(str(raised.exception), "event name is not accepted")


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
        for container in names.stdout.split():
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
        self.assertEqual((self.workspace / "expr.txt").read_text(), "${{ github.sha }}")
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
        self.assertEqual(planned["plan"]["capability_version"], 11)
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
        self.assertEqual(listed.stdout.strip(), "")

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
        self.assertEqual((workspace / "who.txt").read_text(), "Mona\n")
        self.assertEqual((workspace / "title.txt").read_text(), "Dr\n")
        self.assertEqual((workspace / "literal.txt").read_text(), "${{ inputs.who }}\n")
        self.assertEqual((workspace / "literal-env.txt").read_text(), "hello ${{ inputs.who }}\n")
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
        self.assertEqual(listed.stdout.strip(), "")
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
        self.assertEqual(listed.stdout.strip(), "")

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
        self.assertEqual(listed.stdout.strip(), "")
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
