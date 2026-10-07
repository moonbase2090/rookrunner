"""The systemd user unit restarts the worker from a fixed checkout.

The file names no key, no Docker socket, no image digest, and no home path.
"""

import unittest
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "rookrunner-worker.service"


class WorkerUnitTests(unittest.TestCase):
    def setUp(self):
        self.text = UNIT.read_text(encoding="utf-8")

    def test_restarts_from_the_engine_checkout(self):
        self.assertIn("Type=simple\n", self.text)
        self.assertIn("Restart=always\n", self.text)
        self.assertIn("NoNewPrivileges=yes\n", self.text)
        self.assertIn("WorkingDirectory=/var/lib/rookrunner/engine\n", self.text)
        self.assertIn(
            "Environment=PYTHONPATH=/var/lib/rookrunner/engine/src\n",
            self.text,
        )
        self.assertIn(
            "ExecStart=/usr/bin/python3 -m execution_core "
            "--state /var/lib/rookrunner/state worker "
            "--repository /var/lib/rookrunner/clone\n",
            self.text,
        )
        self.assertIn("WantedBy=default.target\n", self.text)

    def test_omits_key_socket_digest_and_home(self):
        forbidden = (
            "--app-key",
            "--docker-socket",
            "--runner-image",
            "sha256:",
            "/home",
            "/Users",
            "%h",
            "ListenStream",
            "ListenDatagram",
            "private-key",
            ".pem",
            "User=",
        )
        for item in forbidden:
            self.assertNotIn(item, self.text, item)
