# SPDX-License-Identifier: MPL-2.0

"""The 0.1.0 tree builds a wheel and a source tarball."""

import shutil
import subprocess
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_version_is_0_1_0_in_metadata_and_code(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["version"], "0.1.0")
        self.assertEqual(project["build-system"]["build-backend"], "hatchling.build")
        init = (ROOT / "src" / "execution_core" / "__init__.py").read_text()
        self.assertIn('__version__ = "0.1.0"', init)

    def test_uv_build_writes_the_wheel_and_the_sdist(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(uv)
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [uv, "build", "--out-dir", directory],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            produced = sorted(
                path.name
                for path in Path(directory).iterdir()
                if path.name.startswith("execution_core-")
            )
            self.assertEqual(
                produced,
                [
                    "execution_core-0.1.0-py3-none-any.whl",
                    "execution_core-0.1.0.tar.gz",
                ],
            )
            wheel = Path(directory) / produced[0]
            with zipfile.ZipFile(wheel) as archive:
                metadata = archive.read("execution_core-0.1.0.dist-info/METADATA").decode()
            self.assertIn("Version: 0.1.0", metadata)
            self.assertIn("License-Expression: MPL-2.0", metadata)
            self.assertIn("Requires-Dist: pyyaml==6.0.3", metadata)


if __name__ == "__main__":
    unittest.main()
