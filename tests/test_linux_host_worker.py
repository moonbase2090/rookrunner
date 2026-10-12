# SPDX-License-Identifier: MIT

"""The second dogfood host uses the shared worker unit and enables linger.

The unit file does not carry a linger directive.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "rookrunner-worker.service"
DOC = ROOT / "docs" / "design" / "linux-host-worker.md"


class LinuxHostWorkerTests(unittest.TestCase):
    def test_a_host_copy_must_set_no_new_privileges(self):
        prose = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn(
            "A host copy of the worker unit must set `NoNewPrivileges=yes`. "
            "The repository unit sets that directive. "
            "A copy that omits it is reported by systemd as "
            "`NoNewPrivileges=no` and must be set to `yes`. "
            "This page does not edit a running unit.",
            prose,
        )

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
