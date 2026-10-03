import ast
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from execution_core.attempt import materialize_attempt
from execution_core.plan import plan_snapshot, plan_workflow
from execution_core.run import (
    RunError,
    _OUTPUT_JOB_BYTES,
    _OUTPUT_RUN_BYTES,
    _exec_limit,
    _job_outputs,
    _read_utf8,
    run_job,
)
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
        if: github.sha == ''
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
        if self.previous is None:
            os.environ.pop("ROOKRUNNER_TEST_MARKER", None)
        else:
            os.environ["ROOKRUNNER_TEST_MARKER"] = self.previous

    def _calls(self):
        return [ast.literal_eval(line) for line in self.log.read_text().splitlines() if line]

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
