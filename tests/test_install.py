# SPDX-License-Identifier: MIT

"""The install page covers wheel installs and immutable worker releases."""

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
            "`NoNewPrivileges=no` and must be set to `yes`.",
            PROSE,
        )

    def test_an_upgrade_that_needs_egress_adds_network_bridge(self):
        self.assertIn(
            "Add `--network bridge` to the host copy's `ExecStart` when jobs need egress.",
            PROSE,
        )
        self.assertIn("Cargo and pip need it.", PROSE)
        self.assertIn("The repository unit omits the flag", PROSE)

    def test_developer_installs_the_wheel_without_pythonpath(self):
        self.assertIn("execution_core-0.1.21-py3-none-any.whl", TEXT)
        self.assertIn("Do not set `PYTHONPATH`.", TEXT)
        self.assertIn("PyYAML 6.0.3", PROSE)

    def test_host_installs_an_immutable_release_and_selects_it_by_symlink(self):
        self.assertIn("rookrunner-engine-<version>-<full-commit>.tar.gz", TEXT)
        self.assertIn("scripts/engine-release.py build", TEXT)
        self.assertIn("pyyaml==6.0.3", TEXT)
        self.assertIn("engine-release.py install", TEXT)
        self.assertIn("engine-release.py activate", TEXT)
        self.assertIn("/var/lib/rookrunner/releases/<commit>", TEXT)
        self.assertIn("The release directory is read-only.", PROSE)
        self.assertIn("Reinstalling identical bytes is idempotent", PROSE)
        self.assertIn("To roll back, activate the previous full commit", PROSE)
        self.assertLess(
            TEXT.index("systemctl stop rookrunner-worker.service"),
            TEXT.index("mv /var/lib/rookrunner/engine"),
        )

    def test_the_page_keeps_credentials_out_of_release_trees(self):
        self.assertIn("Do not copy a private key into a release tree", PROSE)
        self.assertNotIn("/Users", TEXT)
        self.assertNotIn("/home/", TEXT)


if __name__ == "__main__":
    unittest.main()
