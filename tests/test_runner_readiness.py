# SPDX-License-Identifier: MIT

"""Worker readiness against a controlled external Docker CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from execution_core.socket_lock import reset_docker_context
from execution_core.worker import Worker
from schema_support import validate_response


IMAGE = "sha256:" + "ab" * 32
NAMED = "example.invalid/runner@" + IMAGE


class RunnerReadinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rr-ready-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.reply = self.root / "reply.json"
        self.log = self.root / "docker.jsonl"
        self.reply.write_text(json.dumps({"code": 1, "stdout": "", "stderr": "missing"}))
        docker = self.root / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(self.log)!r}, 'a') as log:\n"
            "    log.write(json.dumps({'args': sys.argv[1:], "
            "'context': os.environ.get('DOCKER_CONTEXT'), "
            "'host': os.environ.get('DOCKER_HOST')}) + '\\n')\n"
            f"with open({str(self.reply)!r}) as source:\n"
            "    reply = json.load(source)\n"
            "print(reply['stdout'])\n"
            "print(reply['stderr'], file=sys.stderr)\n"
            "sys.exit(reply['code'])\n"
        )
        docker.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    def _worker(self, image=NAMED):
        worker = Worker(self.repo, self.root / "state", runner_image=image)
        worker.start()
        self.addCleanup(worker.close)
        return worker

    def _describe(self, worker):
        result = worker.dispatch("worker.describe", {})
        validate_response("worker.describe", {"jsonrpc": "2.0", "id": 1, "result": result})
        return result

    def _present(self, image_id=IMAGE):
        self.reply.write_text(
            json.dumps(
                {
                    "code": 0,
                    "stdout": json.dumps(
                        {"Id": image_id, "RepoDigests": [], "Os": "linux", "Architecture": "arm64"}
                    ),
                    "stderr": "",
                }
            )
        )

    def test_missing_image_is_not_ready_and_is_never_pulled(self):
        worker = self._worker()
        result = self._describe(worker)
        self.assertIs(result["ready"], False)
        self.assertEqual(
            result["readiness_error"], "runner image unavailable: image digest will not resolve"
        )
        self.assertEqual(result["runner_image"], IMAGE)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)  # Startup and describe each inspect locally.
        self.assertTrue(all(row["args"][:2] == ["image", "inspect"] for row in calls))
        self.assertTrue(all(row["args"][-1] == NAMED for row in calls))

    def test_describe_detects_image_removal_and_recovery(self):
        self._present()
        worker = self._worker(IMAGE)
        self.assertIs(self._describe(worker)["ready"], True)
        self.reply.write_text(json.dumps({"code": 1, "stdout": "", "stderr": "missing"}))
        self.assertIs(self._describe(worker)["ready"], False)
        self._present()
        recovered = self._describe(worker)
        self.assertIs(recovered["ready"], True)
        self.assertIsNone(recovered["readiness_error"])

    def test_no_configured_image_needs_no_docker(self):
        worker = self._worker(None)
        result = self._describe(worker)
        self.assertIs(result["ready"], True)
        self.assertIsNone(result["readiness_error"])
        self.assertFalse(self.log.exists())

    def test_an_inspected_image_must_match_the_configured_pin(self):
        self._present("sha256:" + "cd" * 32)
        result = self._describe(self._worker())
        self.assertIs(result["ready"], False)
        self.assertEqual(
            result["readiness_error"], "runner image unavailable: image digest will not resolve"
        )

    def test_unreachable_docker_reports_a_safe_reason(self):
        self.reply.write_text(
            json.dumps({"code": 1, "stdout": "", "stderr": "Cannot connect to the Docker daemon"})
        )
        result = self._describe(self._worker())
        self.assertIs(result["ready"], False)
        self.assertEqual(
            result["readiness_error"],
            "runner image unavailable: Docker is unreachable; the worker cannot execute this run",
        )

    def test_missing_docker_is_not_ready(self):
        with patch("execution_core.run.shutil.which", return_value=None):
            result = self._describe(self._worker())
        self.assertIs(result["ready"], False)
        self.assertEqual(result["readiness_error"], "runner image unavailable: Docker is missing")

    def test_inspection_timeout_is_not_ready(self):
        def timed_out(args, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 2)
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        with patch("execution_core.run.subprocess.run", side_effect=timed_out):
            result = self._describe(self._worker())
        self.assertIs(result["ready"], False)
        self.assertEqual(result["readiness_error"], "runner image inspection timed out")

    def test_scheduler_failure_stays_not_ready_after_image_recovers(self):
        worker = self._worker()
        worker.execution_error = "scheduler stopped; restart worker to reconcile runs"
        self._present()
        result = self._describe(worker)
        self.assertIs(result["ready"], False)
        self.assertEqual(
            result["readiness_error"], "scheduler stopped; restart worker to reconcile runs"
        )

    def test_inspection_uses_the_pinned_docker_context(self):
        token = reset_docker_context(None)
        self.addCleanup(reset_docker_context, token)
        self._present()
        with patch.dict(
            os.environ,
            {"DOCKER_CONTEXT": "other", "DOCKER_HOST": "tcp://example.invalid:2375"},
        ):
            result = self._describe(self._worker())
        self.assertIs(result["ready"], True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([row["context"] for row in calls], ["default", "default"])
        self.assertEqual([row["host"] for row in calls], [None, None])
