# SPDX-License-Identifier: MPL-2.0

"""The install page names both 0.1.0 procedures and leaves the hosts alone."""

import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "install.md").read_text()
PROSE = " ".join(TEXT.split())


class InstallDocTests(unittest.TestCase):
    def test_developer_installs_the_wheel_without_pythonpath(self):
        self.assertIn("execution_core-0.1.0-py3-none-any.whl", TEXT)
        self.assertIn("Do not set `PYTHONPATH`.", TEXT)
        self.assertIn("PyYAML 6.0.3", PROSE)

    def test_host_layout_unpacks_the_tarball(self):
        self.assertIn("execution_core-0.1.0.tar.gz", TEXT)
        self.assertIn("--strip-components=1", TEXT)
        self.assertIn("/var/lib/rookrunner/engine/src", TEXT)
        self.assertIn("pyyaml==6.0.3", TEXT)
        self.assertIn("mode `0700`", TEXT)
        self.assertIn("mode `0600`", TEXT)

    def test_the_page_does_not_touch_the_dogfood_hosts(self):
        self.assertIn("does not install anything on Nexus or Vertex", PROSE)
        self.assertNotIn("/Users", TEXT)
        self.assertNotIn("/home/", TEXT)


if __name__ == "__main__":
    unittest.main()
