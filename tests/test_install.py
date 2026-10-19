# SPDX-License-Identifier: MIT

"""The install page names both 0.1.8 procedures and leaves the hosts alone."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "install.md").read_text()
PROSE = " ".join(TEXT.split())


class InstallDocTests(unittest.TestCase):
    def test_a_host_copy_must_set_no_new_privileges(self):
        self.assertIn(
            "A host copy of the worker unit must set `NoNewPrivileges=yes`. "
            "The repository unit sets that directive. "
            "A copy that omits it is reported by systemd as "
            "`NoNewPrivileges=no` and must be set to `yes`. "
            "This page does not edit a running unit.",
            PROSE,
        )

    def test_an_upgrade_that_needs_egress_adds_network_bridge(self):
        self.assertIn(
            "add `--network bridge` to the host copy's `ExecStart` when jobs need egress.",
            PROSE,
        )
        self.assertIn("Cargo and pip need it.", PROSE)
        self.assertIn("The repository unit omits the flag", PROSE)
        self.assertIn("This page does not edit a running unit.", PROSE)

    def test_developer_installs_the_wheel_without_pythonpath(self):
        self.assertIn("execution_core-0.1.8-py3-none-any.whl", TEXT)
        self.assertIn("Do not set `PYTHONPATH`.", TEXT)
        self.assertIn("PyYAML 6.0.3", PROSE)

    def test_host_layout_unpacks_the_tarball(self):
        self.assertIn("execution_core-0.1.8.tar.gz", TEXT)
        self.assertIn("--strip-components=1", TEXT)
        self.assertIn("/var/lib/rookrunner/engine/src", TEXT)
        self.assertIn("pyyaml==6.0.3", TEXT)
        self.assertIn("mode `0700`", TEXT)
        self.assertIn("mode `0600`", TEXT)

    def test_the_page_does_not_touch_the_dogfood_hosts(self):
        self.assertIn("does not install anything on a dogfood host", PROSE)
        self.assertNotIn("/Users", TEXT)
        self.assertNotIn("/home/", TEXT)


if __name__ == "__main__":
    unittest.main()
