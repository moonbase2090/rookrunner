# SPDX-License-Identifier: MPL-2.0

"""The clean-container record names the wheel, the platform, and describe."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = (ROOT / "docs" / "validation" / "m4-container-acceptance.md").read_text()
SHA_LINE = re.compile(r"^[0-9a-f]{64}  \S+$")


class CleanContainerAcceptanceTests(unittest.TestCase):
    def test_record_quotes_the_two_checksums_and_the_describe_result(self):
        self.assertIn("44b5fe4dead0c5a2e64989db01f4085a3b83fe5d", RECORD)
        self.assertIn("ubuntu:24.04", RECORD)
        self.assertIn("linux/amd64", RECORD)
        self.assertIn("x86_64", RECORD)
        self.assertIn("Python 3.12.3", RECORD)
        self.assertIn("PyYAML==6.0.3", RECORD)
        self.assertIn("The checkout was not mounted.", RECORD)
        self.assertIn("--network none", RECORD)
        self.assertIn("`describe` exited 0", RECORD)
        self.assertIn('"version":"0.1.0"', RECORD)
        self.assertIn('"ready":true', RECORD)
        self.assertIn("not a P01" + "\u2013" + "P12 claim", RECORD)
        checksums = [line for line in RECORD.splitlines() if SHA_LINE.match(line)]
        self.assertEqual(
            checksums,
            [
                "51b6ec7a19fb4fe2b5461ffab0336d7c78444e73ca14b23861cd80e9ab0fb2f8"
                "  execution_core-0.1.0-py3-none-any.whl",
                "8fe1d30f1a370111dc0c95f64d46fbfb9c4aecca151c3e15d33c9fcd7cd20f59"
                "  execution_core-0.1.0.tar.gz",
            ],
        )


if __name__ == "__main__":
    unittest.main()
