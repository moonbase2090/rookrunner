"""Startup lock for worker --docker-socket, --secrets, and --app-key.

The probe is exercised with a fake Docker client. No test reads
~/Secrets/github-app/rookrunner-app/ and no test contacts api.github.com.
HOME points at a temporary directory.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from execution_core.cli import call
from execution_core.socket_lock import (
    HOST_CONTROL_WARNING,
    accept_app_key,
    probe_secrets_unshared,
)
from execution_core.worker import Worker

IMAGE = "example.invalid/runner@sha256:" + "ab" * 32
APP_KEY = "~/Secrets/github-app/rookrunner-app/private-key.pem"
KEY_PARTS = ("Secrets", "github-app", "rookrunner-app", "private-key.pem")
PROBE_SCRIPT = (
    "if [ -d /rr-secrets ] && [ -r /rr-secrets ] && [ -x /rr-secrets ]; "
    "then exit 42; else exit 0; fi"
)


def run_cli(*args, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "execution_core", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class _HomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="socket-lock-")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self._env = patch.dict(os.environ, {"HOME": str(self.home)})
        self._env.start()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def worker(self, **kwargs):
        state = Path(self.tmp.name) / "state"
        return Worker(self.repo, state, **kwargs)

    def key_path(self):
        return str(self.home.joinpath(*KEY_PARTS))


class FlagTests(_HomeTest):
    def test_secrets_takes_no_path_and_requires_owner_name(self):
        with self.assertRaisesRegex(ValueError, "worker --secrets takes no path"):
            self.worker(secrets="CANARY-SECRET-PATH")
        with self.assertRaisesRegex(ValueError, "worker --secrets requires --github-repository"):
            self.worker(secrets=True)
        for value in (
            "CANARY",
            "/tmp/CANARY",
            "owner/demo/CANARY",
            "owner//demo",
            "../CANARY/demo",
        ):
            with self.assertRaisesRegex(ValueError, "worker --github-repository") as caught:
                self.worker(github_repository=value)
            self.assertNotIn(value, str(caught.exception))
        worker = self.worker(secrets=True, github_repository="Owner/Demo")
        self.assertIs(worker.secrets, True)
        self.assertEqual(worker.github_repository, "Owner/Demo")
        self.assertFalse((self.home / "Secrets").exists())

    def test_app_key_accepts_only_the_home_path_and_does_not_open_it(self):
        with (
            patch("builtins.open", side_effect=AssertionError("opened")),
            patch("os.open", side_effect=AssertionError("opened")),
        ):
            accepted = accept_app_key(APP_KEY)
        self.assertEqual(accepted, self.key_path())
        self.assertFalse(Path(accepted).exists())
        worker = self.worker(app_key=self.key_path())
        self.assertEqual(worker.app_key, self.key_path())
        bad = str(self.home / "CANARY-NOT-THIS-PATH.pem")
        with self.assertRaisesRegex(ValueError, "worker --app-key") as caught:
            self.worker(app_key=bad)
        self.assertNotIn("CANARY-NOT-THIS-PATH", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "worker --app-key"):
            self.worker(app_key="Secrets/github-app/rookrunner-app/private-key.pem")

    def test_app_key_refuses_a_symlink_without_reading_the_target(self):
        allowed = self.home.joinpath(*KEY_PARTS)
        allowed.parent.mkdir(parents=True)
        target = self.home / "elsewhere.pem"
        target.write_text("CANARY-KEY-BYTES")
        allowed.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "worker --app-key") as caught:
            self.worker(app_key=APP_KEY)
        self.assertNotIn("CANARY-KEY-BYTES", str(caught.exception))

    def test_secret_ref_and_pusher_are_repeatable(self):
        worker = self.worker(
            secret_refs=["refs/heads/main", "refs/pull/9/merge", "refs/heads/Dev"],
            secret_pushers=["Mona", "github-actions[bot]"],
        )
        self.assertEqual(
            worker.secret_refs,
            ("refs/heads/main", "refs/pull/9/merge", "refs/heads/Dev"),
        )
        self.assertEqual(worker.secret_pushers, ("Mona", "github-actions[bot]"))
        for value in ("main", "heads/main", "refs/heads/a b", "refs/heads/../x"):
            with self.assertRaisesRegex(ValueError, "worker --secret-ref") as caught:
                self.worker(secret_refs=[value])
            self.assertNotIn(value, str(caught.exception))
        for value in ("", "-mona", "mona-", "bad login", "İ", "a" * 40):
            with self.assertRaisesRegex(ValueError, "worker --secret-pusher"):
                self.worker(secret_pushers=[value])

    def test_docker_socket_alone_warns_and_names_both_directories(self):
        worker = self.worker(docker_socket=True)
        self.assertIs(worker.docker_socket, True)
        self.assertIn("~/Secrets/github-app/rookrunner-app/", worker.host_control_warning)
        self.assertIn("~/Secrets/rookrunner-secrets/", worker.host_control_warning)
        self.assertEqual(worker.host_control_warning, HOST_CONTROL_WARNING)
        self.assertIsNone(self.worker().host_control_warning)

    def test_combination_without_a_runner_image_names_both_flags(self):
        with patch("execution_core.socket_lock.probe_secrets_unshared") as probe:
            with self.assertRaises(ValueError) as caught:
                self.worker(docker_socket=True, secrets=True, github_repository="owner/demo")
            self.assertIn("worker --docker-socket", str(caught.exception))
            self.assertIn("worker --secrets", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                self.worker(docker_socket=True, app_key=APP_KEY)
            text = str(caught.exception)
            self.assertIn("worker --docker-socket", text)
            self.assertIn("worker --app-key", text)
            with self.assertRaises(ValueError) as caught:
                self.worker(
                    docker_socket=True,
                    secrets=True,
                    github_repository="owner/demo",
                    app_key=APP_KEY,
                )
            text = str(caught.exception)
            self.assertIn("worker --app-key", text)
            self.assertIn("worker --secrets", text)
        probe.assert_not_called()

    def test_a_passing_probe_stores_the_flags_and_a_failure_names_them(self):
        with patch("execution_core.socket_lock.probe_secrets_unshared", return_value=True) as probe:
            worker = self.worker(
                docker_socket=True,
                secrets=True,
                github_repository="owner/demo",
                app_key=APP_KEY,
                runner_image=IMAGE,
                secret_refs=["refs/heads/main"],
                secret_pushers=["mona"],
            )
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], IMAGE)
        self.assertEqual(probe.call_args.args[1], ["--app-key", "--secrets"])
        self.assertEqual(probe.call_args.kwargs["secrets_dir"], self.home / "Secrets")
        self.assertIs(worker.docker_socket, True)
        self.assertIs(worker.secrets, True)
        self.assertEqual(worker.app_key, self.key_path())
        self.assertIsNotNone(worker.host_control_warning)
        with (
            patch("execution_core.socket_lock.probe_secrets_unshared", return_value=False),
            self.assertRaises(ValueError) as caught,
        ):
            self.worker(
                docker_socket=True,
                app_key=APP_KEY,
                runner_image=IMAGE,
            )
        text = str(caught.exception)
        self.assertIn("worker --docker-socket", text)
        self.assertIn("worker --app-key", text)
        self.assertIn("usable directory", text)
        self.assertNotIn(str(self.home), text)


def _client(
    *,
    version=(0, b"1.44\n", b""),
    inspect=(0, b"sha256:ab\n", b""),
    create=(0, b"id\n", b""),
    start=(0, b"", b""),
    wait=(0, b"0\n", b""),
    remove=(0, b"", b""),
):
    calls = []

    def invoke(args, timeout):
        calls.append((list(args), timeout))
        command = args[0]
        if command == "version":
            return version
        if command == "image":
            return inspect
        if command == "create":
            return create
        if command == "start":
            return start
        if command == "wait":
            return wait
        if command == "rm":
            return remove
        raise AssertionError(command)

    return calls, invoke


class ProbeTests(_HomeTest):
    def _probe(self, invoke, secrets_dir=None):
        if secrets_dir is None:
            secrets_dir = self.home / "Secrets"
        return probe_secrets_unshared(
            IMAGE,
            ["--secrets"],
            secrets_dir=secrets_dir,
            invoke=invoke,
        )

    def _create(self, calls):
        return next(args for args, _timeout in calls if args[0] == "create")

    def test_mount_rejection_passes_and_removes_the_container(self):
        calls, invoke = _client(
            create=(125, b"", b"error while creating mount source path '/hidden': denied")
        )
        self.assertIs(self._probe(invoke), True)
        self.assertFalse(any(args[0] == "start" for args, _timeout in calls))
        created = self._create(calls)
        removed = next(args for args, _timeout in calls if args[0] == "rm")
        self.assertEqual(removed[1:], ["-f", created[created.index("--name") + 1]])
        self.assertTrue(
            created[created.index("--name") + 1].startswith("rookrunner-secrets-probe-")
        )
        self.assertFalse((self.home / "Secrets").exists())

    def test_a_directory_that_is_not_usable_passes(self):
        calls, invoke = _client(wait=(0, b"0\n", b"CANARY-BYTES"))
        self.assertIs(self._probe(invoke), True)
        create = self._create(calls)
        mount = create[create.index("--mount") + 1]
        source = str(self.home / "Secrets")
        self.assertEqual(
            mount,
            f"type=bind,source={source},target=/rr-secrets,readonly",
        )
        self.assertEqual(create.count("--mount"), 1)
        self.assertNotIn("-v", create)
        self.assertNotIn("--volume", create)
        self.assertNotIn("docker.sock", " ".join(create))
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertEqual(create[create.index("--user") + 1], "0:0")
        self.assertEqual(create[create.index("--entrypoint") + 1], "sh")
        self.assertEqual(create[-2:], ["-c", PROBE_SCRIPT])
        self.assertNotRegex(PROBE_SCRIPT, r"\b(echo|ls|cat|find)\b")
        self.assertEqual(create[create.index(IMAGE) + 1], "-c")
        self.assertNotIn("pull", [args[0] for args, _timeout in calls])
        self.assertTrue(all(timeout <= 60 for _args, timeout in calls))
        self.assertIn(
            ["rm", "-f", create[create.index("--name") + 1]], [args for args, _t in calls]
        )

    def test_a_usable_directory_fails_and_names_both_flags(self):
        _calls, invoke = _client(wait=(0, b"42\n", b""))
        with self.assertRaises(ValueError) as caught:
            self._probe(invoke)
        text = str(caught.exception)
        self.assertIn("worker --docker-socket", text)
        self.assertIn("worker --secrets", text)
        self.assertIn("usable directory", text)
        self.assertNotIn(str(self.home), text)

    def test_other_probe_failures_refuse_without_their_output(self):
        canary = b"CANARY-FILE-BYTES /secret/private-key.pem"
        cases = (
            ("Docker daemon cannot be contacted", _client(version=(1, b"", canary))[1]),
            (
                "runner image failed for a reason other than the mount",
                _client(inspect=(1, b"", canary))[1],
            ),
            (
                "runner image failed for a reason other than the mount",
                _client(create=(1, b"", canary))[1],
            ),
            (
                "runner image failed for a reason other than the mount",
                _client(wait=(0, b"1\n", canary))[1],
            ),
        )
        for detail, invoke in cases:
            with self.assertRaises(ValueError) as caught:
                self._probe(invoke)
            text = str(caught.exception)
            self.assertIn("worker --docker-socket", text)
            self.assertIn("worker --secrets", text)
            self.assertIn(detail, text)
            self.assertNotIn("CANARY-FILE-BYTES", text)
            self.assertNotIn("private-key.pem", text)

    def test_a_missing_daemon_and_an_unsafe_path_do_not_run_a_container(self):
        def missing(_args, _timeout):
            raise OSError("docker missing")

        with self.assertRaisesRegex(ValueError, "Docker daemon cannot be contacted"):
            self._probe(missing)
        calls, invoke = _client()
        with self.assertRaisesRegex(ValueError, "cannot be combined") as caught:
            self._probe(invoke, secrets_dir=Path("/tmp/has,comma"))
        self.assertNotIn("has,comma", str(caught.exception))
        self.assertEqual(calls, [])

    def test_the_container_is_removed_when_removal_is_the_only_failure(self):
        calls, invoke = _client(remove=(1, b"", b"busy"))
        with self.assertRaisesRegex(ValueError, "probe container could not be removed"):
            self._probe(invoke)
        self.assertEqual(calls[-1][0][0], "rm")


class CliTests(_HomeTest):
    def test_worker_help_has_the_flags_and_poll_does_not(self):
        worker = run_cli("--state", "unused", "worker", "--help")
        self.assertEqual(worker.returncode, 0, worker.stderr)
        for flag in (
            "--secrets",
            "--github-repository",
            "--app-key",
            "--secret-ref",
            "--secret-pusher",
        ):
            self.assertIn(flag, worker.stdout)
        self.assertIn("Takes no path", worker.stdout)
        poll = run_cli("--state", "unused", "poll", "--help")
        status = run_cli("--state", "unused", "status", "--help")
        self.assertNotIn("--secrets", poll.stdout)
        self.assertNotIn("--secret-ref", poll.stdout)
        self.assertNotIn("--secrets", status.stdout)
        self.assertIn("--app-key", poll.stdout)
        self.assertIn("--credential-file", status.stdout)

    def test_secrets_value_is_an_argument_error(self):
        result = run_cli(
            "--state",
            "unused",
            "worker",
            "--repository",
            "unused",
            "--secrets",
            "CANARY-PATH",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertIn("CANARY-PATH", result.stderr)

    def test_cli_refuses_the_combination_and_a_foreign_key_path(self):
        result = run_cli(
            "--state",
            str(self.tmp.name + "/state"),
            "worker",
            "--repository",
            str(self.repo),
            "--docker-socket",
            "--secrets",
            "--github-repository",
            "owner/demo",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stderr)
        text = payload["error"]["message"]
        self.assertEqual(payload["error"]["kind"], "CLIENT_ERROR")
        self.assertIn("worker --docker-socket", text)
        self.assertIn("worker --secrets", text)
        foreign = run_cli(
            "--state",
            str(Path(self.tmp.name) / "state"),
            "worker",
            "--repository",
            str(self.repo),
            "--app-key",
            str(self.home / "CANARY-FOREIGN.pem"),
        )
        self.assertEqual(foreign.returncode, 1, foreign.stderr)
        message = json.loads(foreign.stderr)["error"]["message"]
        self.assertIn("worker --app-key", message)
        self.assertNotIn("CANARY-FOREIGN", message)

    def _start(self, state, stderr_path, *flags):
        with stderr_path.open("w") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "execution_core",
                    "--state",
                    str(state),
                    "worker",
                    "--repository",
                    str(self.repo),
                    *flags,
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(stderr_path.read_text())
            try:
                described = call(state, "worker.describe", {})["result"]
                return process, described
            except (OSError, ValueError):
                time.sleep(0.01)
        process.terminate()
        self.fail("worker did not become ready")

    def test_cli_starts_with_the_flags_and_warns_for_the_socket(self):
        secret_state = Path(self.tmp.name) / "secret-flags"
        secret_err = Path(self.tmp.name) / "secret-flags.err"
        socket_state = Path(self.tmp.name) / "socket-warn"
        socket_err = Path(self.tmp.name) / "socket-warn.err"
        secret_process = None
        socket_process = None
        try:
            secret_process, described = self._start(
                secret_state,
                secret_err,
                "--secrets",
                "--github-repository",
                "owner/demo",
                "--app-key",
                APP_KEY,
                "--secret-ref",
                "refs/heads/main",
                "--secret-pusher",
                "Mona",
            )
            for key in (
                "app_key",
                "secrets",
                "secret_refs",
                "secret_pushers",
                "github_repository",
            ):
                self.assertNotIn(key, described)
            text = json.dumps(described)
            self.assertNotIn("private-key", text)
            self.assertNotIn("Secrets", text)
            self.assertNotIn("owner/demo", text)
            self.assertNotIn("rookrunner-secrets", secret_err.read_text())
            self.assertFalse((self.home / "Secrets").exists())
            socket_process, _described = self._start(
                socket_state,
                socket_err,
                "--docker-socket",
            )
            warning = socket_err.read_text()
            self.assertIn("~/Secrets/github-app/rookrunner-app/", warning)
            self.assertIn("~/Secrets/rookrunner-secrets/", warning)
            self.assertNotIn(str(self.home), warning)
        finally:
            for process in (secret_process, socket_process):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
