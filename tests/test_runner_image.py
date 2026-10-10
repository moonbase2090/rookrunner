# SPDX-License-Identifier: MPL-2.0

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from execution_core.attempt import materialize_attempt
from execution_core.plan import CAPABILITY_VERSION, PlanError, plan_workflow
from execution_core.protocol import Fault
from execution_core.run import RunError, run_job
from execution_core.snapshot import SourceCapture
from execution_core.worker import Worker, _require_ubuntu_latest
from schema_support import validate_response


DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "ef" * 32
NAMED = "example@sha256:" + "ab" * 32
EVENT = {"kind": "local"}
ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _init(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")


def _commit(repo, message):
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _workflow(runs_on="ubuntu-latest", step="echo hi"):
    body = "\n".join(f"          {line}" if line else "" for line in step.splitlines())
    return (
        f"on: push\njobs:\n  build:\n    runs-on: {runs_on}\n    steps:\n      - run: |\n{body}\n"
    )


def _write(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _capture(root, workflow_text):
    repo = root / "repo"
    _init(repo)
    _write(repo, ".github/workflows/test.yml", workflow_text)
    _write(repo, "source.txt", "original\n")
    _commit(repo, "fixture")
    state = root / "state"
    captured = SourceCapture(repo, state).capture(".github/workflows/test.yml")
    snapshot = state / "snapshots" / captured["snapshot_id"]
    workspace = state / "attempts" / "run-1"
    workspace.parent.mkdir(mode=0o700)
    materialize_attempt(snapshot, captured["digest"], workspace)
    return snapshot, captured["digest"], workspace


class RunnerImageFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-image-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.root / "repo"
        _init(self.repo)

    def test_describe_reports_the_digest_only_when_the_flag_is_set(self):
        plain = Worker(self.repo, self.root / "plain")
        plain.start()
        try:
            described = plain.dispatch("worker.describe", {})
            self.assertNotIn("runner_image", described)
            validate_response("worker.describe", {"jsonrpc": "2.0", "id": 1, "result": described})
        finally:
            plain.close()
        named = Worker(self.repo, self.root / "named", runner_image=NAMED)
        named.start()
        try:
            described = named.dispatch("worker.describe", {})
            self.assertEqual(described["runner_image"], DIGEST)
            self.assertNotIn("example@", json.dumps(described))
            validate_response("worker.describe", {"jsonrpc": "2.0", "id": 1, "result": described})
        finally:
            named.close()
        for value in ("ubuntu:latest", "sha256:" + "ab" * 31, ""):
            with self.assertRaisesRegex(ValueError, "runner image is not pinned by digest"):
                Worker(self.repo, self.root / "bad", runner_image=value)

    def test_selected_jobs_must_be_the_literal_ubuntu_latest_label(self):
        _require_ubuntu_latest({"jobs": [{"runs_on": "ubuntu-latest"}]})
        _require_ubuntu_latest({"jobs": [{"call": {"jobs": [{"runs_on": "ubuntu-latest"}]}}]})
        for plan in (
            {"jobs": [{"runs_on": "macos-14"}]},
            {"jobs": [{"runs_on": "ubuntu-24.04"}]},
            {"jobs": [{"runs_on": ["ubuntu-latest"]}]},
            {"jobs": [{"runs_on": None}]},
            {"jobs": [{"call": {"jobs": [{"runs_on": "ubuntu-22.04"}]}}]},
        ):
            with self.assertRaises(Fault) as raised:
                _require_ubuntu_latest(plan)
            self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
            self.assertIn("runs-on", str(raised.exception))

    def _worker(self, workflow):
        _write(self.repo, ".github/workflows/test.yml", workflow)
        _commit(self.repo, "workflow")
        worker = Worker(self.repo, self.root / "state", runner_image=NAMED)
        worker.execute_queue = lambda: worker.stop.wait()
        worker.start()
        self.addCleanup(worker.close)
        return worker

    def _submit(self, worker, **extra):
        params = {
            "version": 1,
            "submission_key": extra.pop("submission_key", "one"),
            "workflow": ".github/workflows/test.yml",
            "job_id": "build",
            "event": EVENT,
        }
        params.update(extra)
        return worker.submit_workflow(params)

    def test_omitted_image_uses_the_flag_for_ubuntu_latest(self):
        worker = self._worker(_workflow())
        record = self._submit(worker)
        self.assertEqual(record["state"], "queued")
        self.assertEqual(record["input"]["image_reference"], NAMED)
        self.assertEqual(record["input"]["image_digest"], DIGEST)
        again = self._submit(worker)
        self.assertEqual(again["run_id"], record["run_id"])

    def test_explicit_image_wins(self):
        worker = self._worker(_workflow())
        record = self._submit(worker, image=OTHER, submission_key="explicit")
        self.assertEqual(record["input"]["image_reference"], OTHER)
        self.assertEqual(record["input"]["image_digest"], OTHER)

    def test_omitted_image_without_the_flag_creates_no_run(self):
        _write(self.repo, ".github/workflows/test.yml", _workflow())
        _commit(self.repo, "workflow")
        worker = Worker(self.repo, self.root / "state")
        worker.execute_queue = lambda: worker.stop.wait()
        worker.start()
        self.addCleanup(worker.close)
        with self.assertRaises(Fault) as raised:
            self._submit(worker)
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("not pinned", str(raised.exception))
        self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_another_label_creates_no_run(self):
        for label in ("macos-14", "ubuntu-24.04", "[ubuntu-latest]"):
            root = self.root / label.strip("[]")
            repo = root / "repo"
            _init(repo)
            _write(repo, ".github/workflows/test.yml", _workflow(label))
            _commit(repo, "workflow")
            worker = Worker(repo, root / "state", runner_image=DIGEST)
            worker.execute_queue = lambda worker=worker: worker.stop.wait()
            worker.start()
            self.addCleanup(worker.close)
            with self.assertRaises(Fault) as raised:
                worker.submit_workflow(
                    {
                        "version": 1,
                        "submission_key": "label",
                        "workflow": ".github/workflows/test.yml",
                        "job_id": "build",
                        "event": EVENT,
                    }
                )
            self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
            self.assertIn("runs-on", str(raised.exception))
            self.assertEqual(worker.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            snaps = root / "state" / "snapshots"
            self.assertFalse(snaps.exists() and any(snaps.iterdir()))

    def test_capability_and_check_workflow_stay(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        text = (ROOT / ".github" / "workflows" / "check.yml").read_text()
        self.assertNotIn("runner-image", text)
        self.assertNotIn("images/ubuntu-runner", text)
        dockerfile = (ROOT / "images" / "ubuntu-runner" / "Dockerfile").read_text()
        self.assertIn(
            "FROM ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55",
            dockerfile,
        )
        for package in ("bash", "ca-certificates", "docker.io", "git", "sudo"):
            self.assertIn(package, dockerfile)
        self.assertNotIn("actions/runner", dockerfile)
        for scope in ("security-events", "actions"):
            with self.assertRaises(PlanError) as raised:
                plan_workflow(
                    (
                        "permissions:\n"
                        f"  {scope}: write\n"
                        "on: push\n"
                        "jobs:\n"
                        "  build:\n"
                        "    runs-on: ubuntu-latest\n"
                        "    steps:\n"
                        "      - run: echo hi\n"
                    ).encode(),
                    "build",
                )
            self.assertEqual(raised.exception.kind, "CAPABILITY_UNSUPPORTED")
            self.assertIn(scope, raised.exception.field)


class RunnerImageCliTests(unittest.TestCase):
    def test_submit_may_omit_image(self):
        state = tempfile.mkdtemp(prefix="runner-cli-")
        self.addCleanup(shutil.rmtree, state)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                state,
                "submit",
                "--key",
                "k",
                "--workflow",
                "a.yml",
                "--job-id",
                "build",
                "--event",
                "{}",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("requires --image", result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_a_bad_flag_fails_worker_start(self):
        root = tempfile.mkdtemp(prefix="runner-cli-worker-")
        self.addCleanup(shutil.rmtree, root)
        repo = Path(root) / "repo"
        _init(repo)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(Path(root) / "state"),
                "worker",
                "--repository",
                str(repo),
                "--runner-image",
                "ubuntu:latest",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runner image is not pinned by digest", result.stderr + result.stdout)


class MissingImageTests(unittest.TestCase):
    def test_a_missing_runner_digest_is_not_pulled(self):
        temp = tempfile.TemporaryDirectory(prefix="runner-missing-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workflow = _workflow("ubuntu-latest", "printf no > marker.txt")
        snapshot, digest, workspace = _capture(root, workflow)
        log = root / "docker-log"
        docker = root / "docker"
        docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(log)!r}\nexit 1\n")
        docker.chmod(0o755)
        plan = plan_workflow(workflow.encode(), "build")["plan"]
        for image in (DIGEST, NAMED):
            log.write_text("")
            with self.assertRaises(RunError) as raised:
                run_job(
                    snapshot,
                    digest,
                    workspace,
                    plan,
                    image,
                    EVENT,
                    docker=str(docker),
                    runner_image=DIGEST,
                )
            self.assertEqual(raised.exception.kind, "SETUP_FAILED")
            self.assertIn("will not resolve", str(raised.exception))
            text = log.read_text()
            self.assertNotIn("\npull ", "\n" + text)
            self.assertNotIn(" pull ", text)
            self.assertNotIn("create", text)
        self.assertFalse((workspace / "marker.txt").exists())


def _calls(log):
    return [ast.literal_eval(line) for line in log.read_text().splitlines() if line]


class RunnerAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.image_root = tempfile.TemporaryDirectory(prefix="runner-account-image-")
        root = Path(cls.image_root.name)
        sudo_dir = root / "sudo"
        sudo_dir.mkdir()
        (sudo_dir / "Dockerfile").write_text(
            "FROM python:3.12-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends sudo \\\n"
            " && rm -rf /var/lib/apt/lists/*\n"
        )
        plain = root / "plain"
        plain.mkdir()
        (plain / "Dockerfile").write_text("FROM python:3.12-slim\n")
        cls.sudo_tag = "rookrunner-p6-sudo"
        cls.plain_tag = "rookrunner-p6-plain"
        subprocess.run(
            ["docker", "build", "--quiet", "-t", cls.sudo_tag, sudo_dir],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "build", "--quiet", "-t", cls.plain_tag, plain],
            check=True,
            capture_output=True,
        )
        collision = root / "collision"
        collision.mkdir()
        (collision / "Dockerfile").write_text(
            f"FROM {cls.sudo_tag}\n"
            f"RUN printf 'runner-{os.getuid()}:x:1:1::/tmp:/bin/sh\\n' >> /etc/passwd\n"
        )
        cls.collision_tag = "rookrunner-p6-collision"
        subprocess.run(
            ["docker", "build", "--quiet", "-t", cls.collision_tag, collision],
            check=True,
            capture_output=True,
        )
        cls.sudo_image = _image_id(cls.sudo_tag)
        cls.plain_image = _image_id(cls.plain_tag)
        cls.collision_image = _image_id(cls.collision_tag)

    @classmethod
    def tearDownClass(cls):
        for tag in (cls.collision_tag, cls.sudo_tag, cls.plain_tag):
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
        cls.image_root.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-account-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "docker-args"
        real = shutil.which("docker")
        wrapper = self.root / "docker"
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

    def _run(self, workflow, image, runner_image):
        snapshot, digest, workspace = _capture(self.root / "cap", workflow)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_workflow(workflow.encode(), "build")["plan"],
            image,
            EVENT,
            docker=str(self.docker),
            runner_image=runner_image,
            step_timeout=60,
        )
        return result, workspace

    def test_runner_image_sudo_uses_the_host_files(self):
        uid = os.getuid()
        workflow = _workflow(
            step=(
                "uid=$(id -u)\n"
                'printf \'%s\\n\' "$uid" > "$GITHUB_WORKSPACE/uid.txt"\n'
                'test "$uid" != 0\n'
                "sudo -n true\n"
                'sudo apt-get --version > "$GITHUB_WORKSPACE/apt.txt"\n'
                'test -s "$GITHUB_WORKSPACE/apt.txt"\n'
                "if echo x >> /etc/passwd; then exit 4; fi\n"
                'test "$(stat -c %u /etc/passwd)" = 0\n'
                'test "$(stat -c %a /etc/sudoers.d/rookrunner)" = 440\n'
                'grep "^runner-${uid}:" /etc/passwd > "$GITHUB_WORKSPACE/line.txt"\n'
            )
        )
        result, workspace = self._run(workflow, self.sudo_image, self.sudo_image)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["image_digest"], self.sudo_image)
        self.assertEqual((workspace / "uid.txt").read_text().strip(), str(uid))
        self.assertIn(f"runner-{uid}:", (workspace / "line.txt").read_text())
        account = workspace.parent / "runner-account"
        self.assertEqual((account / "passwd").stat().st_mode & 0o777, 0o644)
        self.assertEqual((account / "sudoers").stat().st_mode & 0o777, 0o440)
        self.assertIn(f"runner-{uid}:", (account / "passwd").read_text())
        self.assertIn("/github/home", (account / "passwd").read_text())
        calls = _calls(self.log)
        setup = next(call for call in calls if call and call[0] == "run" and "0:0" in call)
        self.assertIn("--rm", setup)
        self.assertNotIn("--privileged", setup)
        mounted = " ".join(setup)
        self.assertIn("destination=/runner-account", mounted)
        self.assertNotIn("/workspace", mounted)
        self.assertNotIn("docker.sock", mounted)
        job = next(call for call in calls if call and call[0] == "create")
        self.assertIn("--user", job)
        self.assertIn(f"{uid}:{os.getgid()}", job)
        self.assertNotIn("--privileged", job)
        self.assertTrue(any("destination=/etc/passwd,readonly" in part for part in job))
        self.assertTrue(
            any("destination=/etc/sudoers.d/rookrunner,readonly" in part for part in job)
        )
        self.assertNotIn("pull", [call[0] for call in calls if call])

    def test_a_different_image_does_not_gain_sudoers(self):
        workflow = _workflow(
            step=(
                "test ! -e /etc/sudoers.d/rookrunner\n"
                'printf \'%s\\n\' "$(id -u)" > "$GITHUB_WORKSPACE/uid.txt"\n'
            )
        )
        result, workspace = self._run(workflow, self.plain_image, self.sudo_image)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual((workspace / "uid.txt").read_text().strip(), str(os.getuid()))
        calls = _calls(self.log)
        self.assertFalse(any(call and call[0] == "run" and "0:0" in call for call in calls))
        job = next(call for call in calls if call and call[0] == "create")
        self.assertNotIn("sudoers", " ".join(job))
        self.assertIn(self.plain_image, job)
        self.assertNotIn(self.sudo_image, job)

    def test_missing_sudo_fails_before_a_step(self):
        workflow = _workflow(step='printf ran > "$GITHUB_WORKSPACE/marker.txt"')
        snapshot, digest, workspace = _capture(self.root / "cap", workflow)
        with self.assertRaises(RunError) as raised:
            run_job(
                snapshot,
                digest,
                workspace,
                plan_workflow(workflow.encode(), "build")["plan"],
                self.plain_image,
                EVENT,
                docker=str(self.docker),
                runner_image=self.plain_image,
                step_timeout=60,
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(raised.exception), "sudo is missing")
        self.assertFalse((workspace / "marker.txt").exists())
        self.assertFalse(any(call and call[0] == "create" for call in _calls(self.log)))

    def test_an_existing_account_name_fails_setup(self):
        workflow = _workflow(step='printf ran > "$GITHUB_WORKSPACE/marker.txt"')
        snapshot, digest, workspace = _capture(self.root / "cap", workflow)
        with self.assertRaises(RunError) as raised:
            run_job(
                snapshot,
                digest,
                workspace,
                plan_workflow(workflow.encode(), "build")["plan"],
                self.collision_image,
                EVENT,
                docker=str(self.docker),
                runner_image=self.collision_image,
                step_timeout=60,
            )
        self.assertEqual(raised.exception.kind, "SETUP_FAILED")
        self.assertEqual(str(raised.exception), "runner account name is already present")
        self.assertFalse((workspace / "marker.txt").exists())


def _image_id(tag):
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        check=True,
        capture_output=True,
        text=True,
    )
    return inspected.stdout.strip()


class UbuntuRunnerImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise AssertionError("Docker is required")
        cls.tag = "rookrunner-p6-ubuntu"
        subprocess.run(
            ["docker", "build", "-t", cls.tag, ROOT / "images" / "ubuntu-runner"],
            check=True,
            capture_output=True,
        )
        cls.image = _image_id(cls.tag)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rmi", "-f", cls.tag], capture_output=True)

    def test_the_operator_image_runs_sudo_and_apt_as_the_caller(self):
        temp = tempfile.TemporaryDirectory(prefix="runner-ubuntu-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workflow = _workflow(
            step=(
                'test "$(id -u)" != 0; sudo -n true; sudo apt-get --version; '
                "docker --version; git --version; test -f /etc/ssl/certs/ca-certificates.crt"
            )
        )
        snapshot, digest, workspace = _capture(root, workflow)
        log = root / "docker-args"
        real = shutil.which("docker")
        wrapper = root / "docker"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"path = {str(log)!r}\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(repr(sys.argv[1:]) + '\\n')\n"
            f"raise SystemExit(subprocess.call([{real!r}, *sys.argv[1:]]))\n"
        )
        wrapper.chmod(0o755)
        result = run_job(
            snapshot,
            digest,
            workspace,
            plan_workflow(workflow.encode(), "build")["plan"],
            self.image,
            EVENT,
            docker=str(wrapper),
            runner_image=self.image,
            step_timeout=90,
        )
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        passwd = workspace.parent / "runner-account" / "passwd"
        self.assertEqual(passwd.stat().st_mode & 0o777, 0o644)
        calls = _calls(log)
        self.assertNotIn("pull", [call[0] for call in calls if call])
        self.assertNotIn("build", [call[0] for call in calls if call])
