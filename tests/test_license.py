# SPDX-License-Identifier: MIT

"""The MIT grant is the license file, the project field, and the headers."""

import hashlib
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPDX = "# SPDX-License-Identifier: MIT"
LICENSE_SHA256 = "4e67c803cc1ddefb7fc79ec9792e4186dc61b64b061e6c614c27047f5e8ba69d"


def python_files():
    files = list((ROOT / "src").rglob("*.py"))
    files.extend((ROOT / "tests").rglob("*.py"))
    files.append(ROOT / ".github" / "signoff.py")
    return files


class LicenseTests(unittest.TestCase):
    def test_license_file_is_the_mit_text(self):
        data = (ROOT / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), LICENSE_SHA256)
        text = data.decode("utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Moonbase 2090 LLC", text)

    def test_project_metadata_names_mit(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["license"], "MIT")

    def test_notice_names_pyyaml_and_skips_development_tools(self):
        text = (ROOT / "NOTICE").read_text()
        self.assertIn("PyYAML 6.0.3", text)
        self.assertIn("MIT", text)
        self.assertIn("https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE", text)
        self.assertNotIn("jsonschema", text)

    def test_python_files_carry_the_spdx_header(self):
        missing = []
        for path in python_files():
            lines = path.read_text().splitlines()
            if lines and lines[0].startswith("#!"):
                header = lines[1] if len(lines) > 1 else ""
            else:
                header = lines[0] if lines else ""
            if header != SPDX:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
