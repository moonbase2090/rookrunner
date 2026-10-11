# SPDX-License-Identifier: MPL-2.0

"""The worker's Docker CLI calls use a pinned context."""

import os
import subprocess
import unittest
from unittest.mock import patch

from execution_core.run import _discover_socket, _invoke
from execution_core.socket_lock import (
    _DOCKER_CONTEXT,
    docker_subprocess_env,
    reset_docker_context,
    resolve_docker_context,
)


class DockerContextTests(unittest.TestCase):
    def test_an_inherited_context_does_not_select_the_engine(self):
        token = _DOCKER_CONTEXT.set(None)
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
        token = _DOCKER_CONTEXT.set(None)
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
        token = _DOCKER_CONTEXT.set(None)

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

    def test_a_blank_explicit_context_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            resolve_docker_context("has space")
        self.assertEqual(str(raised.exception), "worker --docker-context must name one context")
        with self.assertRaises(ValueError):
            resolve_docker_context("")


if __name__ == "__main__":
    unittest.main()
