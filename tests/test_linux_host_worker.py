# SPDX-License-Identifier: MPL-2.0

"""The second dogfood host uses the shared worker unit and enables linger.

The unit file does not carry a linger directive.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "rookrunner-worker.service"
DOC = ROOT / "docs" / "design" / "linux-host-worker.md"


class LinuxHostWorkerTests(unittest.TestCase):
    def test_the_shared_unit_has_no_linger_directive(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("Restart=always\n", text)
        self.assertIn("WorkingDirectory=/var/lib/rookrunner/engine\n", text)
        self.assertNotIn("loginctl", text)
        self.assertNotIn("enable-linger", text)
        self.assertNotIn("Linger=", text)

    def test_the_install_enables_linger_for_the_service_user(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn('sudo loginctl enable-linger "$USER"', text)
        self.assertIn("deploy/rookrunner-worker.service", text)
        self.assertIn("Linger=yes", text)
