# SPDX-License-Identifier: MIT

"""The worker's Docker CLI calls use a pinned context."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from execution_core.run import _discover_socket, _invoke
from execution_core.socket_lock import (
    docker_subprocess_env,
    pin_docker_context,
    reset_docker_context,
    resolve_docker_context,
)
from execution_core.worker import Worker


class DockerContextTests(unittest.TestCase):
    def test_an_inherited_context_does_not_select_the_engine(self):
        token = reset_docker_context(None)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "DOCKER_CONTEXT": "desktop-linux",
                        "DOCKER_HOST": "tcp://203.0.113.5:2375",
                        "ROOKRUNNER_DOCKER_CONTEXT": "",
                    },
                ),
                patch("execution_core.run.subprocess.run", fake_run),
            ):
                code, _stdout, _stderr = _invoke("docker", ["version"], 5)
        finally:
            reset_docker_context(token)
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], ["docker", "version"])
        self.assertEqual(seen["env"]["DOCKER_CONTEXT"], "default")
        self.assertNotIn("DOCKER_HOST", seen["env"])
        self.assertNotIn("203.0.113.5", seen["env"]["DOCKER_CONTEXT"])

    def test_an_explicit_context_is_the_child_environment(self):
        token = reset_docker_context(None)
        try:
            with patch.dict(
                os.environ,
                {"ROOKRUNNER_DOCKER_CONTEXT": "rootless", "DOCKER_HOST": "tcp://203.0.113.9:2375"},
            ):
                env = docker_subprocess_env()
                self.assertEqual(resolve_docker_context("other"), "other")
        finally:
            reset_docker_context(token)
        self.assertEqual(env["DOCKER_CONTEXT"], "rootless")
        self.assertNotIn("DOCKER_HOST", env)

    def test_socket_discovery_uses_the_pinned_context(self):
        token = reset_docker_context(None)

        def fake_run(argv, **kwargs):
            self.assertEqual(
                argv[1:],
                ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            )
            self.assertEqual(kwargs["env"]["DOCKER_CONTEXT"], "default")
            self.assertNotIn("DOCKER_HOST", kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, b"unix:///run/engine.sock\n", b"")

        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "DOCKER_HOST": "unix:///tmp/not-the-engine.sock",
                        "DOCKER_CONTEXT": "desktop-linux",
                        "ROOKRUNNER_DOCKER_CONTEXT": "",
                    },
                ),
                patch("execution_core.run.subprocess.run", fake_run),
            ):
                path = _discover_socket("docker")
        finally:
            reset_docker_context(token)
        self.assertEqual(path, "/run/engine.sock")

    def test_the_scheduler_thread_uses_the_pinned_context(self):
        token = reset_docker_context(None)
        root = tempfile.TemporaryDirectory(prefix="docker-context-worker-")
        worker = None
        try:
            repo = Path(root.name) / "repo"
            state = Path(root.name) / "state"
            repo.mkdir()
            state.mkdir()
            state.chmod(0o700)
            pin_docker_context("operator-ctx")
            worker = Worker(repo, state)
            seen = {}

            def record():
                seen["context"] = docker_subprocess_env()["DOCKER_CONTEXT"]
                worker.stop.set()

            worker.schedule = record
            with patch.dict(os.environ, {"ROOKRUNNER_DOCKER_CONTEXT": "env-ctx"}):
                worker.start()
                worker.scheduler.join(timeout=5)
                self.assertEqual(seen["context"], "operator-ctx")
                self.assertEqual(docker_subprocess_env()["DOCKER_CONTEXT"], "operator-ctx")
        finally:
            if worker is not None:
                worker.close()
            reset_docker_context(token)
            root.cleanup()

    def test_a_blank_explicit_context_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            resolve_docker_context("has space")
        self.assertEqual(str(raised.exception), "worker --docker-context must name one context")
        with self.assertRaises(ValueError):
            resolve_docker_context("")


if __name__ == "__main__":
    unittest.main()
