# SPDX-License-Identifier: MPL-2.0

"""The Vertex install letter enables linger and does not open a port.

The commands name the vertex host. The pull request does not run them.
"""

import re
import unittest
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "design" / "vertex-install.md"


class VertexInstallTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_enables_linger_on_vertex_before_the_service(self):
        self.assertIn('sudo loginctl enable-linger "$USER"', self.text)
        self.assertIn("ssh -o BatchMode=yes vertex", self.text)
        self.assertIn("Linger=yes", self.text)
        self.assertIn("/var/lib/rookrunner/engine", self.text)
        self.assertIn("deploy/rookrunner-worker.service", self.text)
        self.assertLess(
            self.text.index('sudo loginctl enable-linger "$USER"'),
            self.text.index('"enable", "--now", "rookrunner-worker.service"'),
        )

    def test_omits_key_socket_digest_and_nexus(self):
        for item in (
            "--app-key",
            "--docker-socket",
            "ListenStream",
            "sha256:",
            "/Users",
            ".pem",
            "BatchMode=yes nexus",
        ):
            self.assertNotIn(item, self.text, item)
        self.assertIsNone(re.search(r"/home/[^/\s]+", self.text))
