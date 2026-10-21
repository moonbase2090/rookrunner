# SPDX-License-Identifier: MIT

"""The 0.1.11 tree builds a wheel and a source tarball."""

import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_version_is_0_1_4_in_metadata_and_code(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["version"], "0.1.11")
        self.assertEqual(project["build-system"]["build-backend"], "hatchling.build")
        init = (ROOT / "src" / "execution_core" / "__init__.py").read_text()
        self.assertIn('__version__ = "0.1.11"', init)

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
                    "execution_core-0.1.11-py3-none-any.whl",
                    "execution_core-0.1.11.tar.gz",
                ],
            )
            wheel = Path(directory) / produced[0]
            with zipfile.ZipFile(wheel) as archive:
                metadata = archive.read("execution_core-0.1.11.dist-info/METADATA").decode()
                checks = archive.read("execution_core/checks.py").decode()
            self.assertIn("Version: 0.1.11", metadata)
            self.assertIn("License-Expression: MIT", metadata)
            self.assertIn("Requires-Dist: pyyaml==6.0.3", metadata)
            header = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
            self.assertNotRegex(checks, header)
            self.assertIn("_RSA_PEM_HEADER", checks)
            sdist = Path(directory) / produced[1]
            with tarfile.open(sdist, "r:gz") as packed:
                names = packed.getnames()
            self.assertTrue(any(name.endswith("src/execution_core/checks.py") for name in names))
            self.assertFalse(any("/tests/" in name or name.startswith("tests/") for name in names))
            self.assertFalse(
                any("/scripts/" in name or name.startswith("scripts/") for name in names)
            )


if __name__ == "__main__":
    unittest.main()
