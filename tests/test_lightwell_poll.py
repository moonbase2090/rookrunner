# SPDX-License-Identifier: MPL-2.0

"""The Lightwell poll configuration names the Linux jobs and the places."""

import re
import unittest
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "design" / "lightwell-poll.md"


def _section(text, heading):
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


class LightwellPollTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")
        self.x86 = _section(self.text, "## x86_64")
        self.arm = _section(self.text, "## aarch64")

    def test_x86_jobs_use_nexus_then_vertex_and_skip_the_mac(self):
        self.assertLess(self.text.index("## x86_64"), self.text.index("## aarch64"))
        for job in ("checks", "linux-cli-x86_64", "tauri-linux-artifact"):
            self.assertIn(f"rookrunner.yml {job}", self.x86)
        self.assertNotIn("linux-cli-aarch64", self.x86)
        self.assertLess(self.x86.index('"name":"nexus"'), self.x86.index('"name":"vertex"'))
        self.assertNotIn('"name":"mac"', self.x86)
        self.assertIn('"cap":4', self.x86)
        self.assertIn('"cap":2', self.x86)

    def test_aarch64_cli_uses_only_the_mac_place(self):
        self.assertIn("rookrunner.yml linux-cli-aarch64", self.arm)
        self.assertIn('"name":"mac"', self.arm)
        self.assertNotIn('"ssh"', self.arm)
        self.assertIn('"cap":1', self.arm)
        for job in ("checks", "linux-cli-x86_64", "tauri-linux-artifact"):
            self.assertNotIn(job, self.arm)

    def test_omits_macos_jobs_digests_and_host_paths(self):
        for item in (
            "macos-cli",
            "tauri-macos-artifact",
            "--docker-socket",
            "sha256:",
            "/Users",
            ".pem",
            "ListenStream",
        ):
            self.assertNotIn(item, self.text, item)
        self.assertIsNone(re.search(r"/home/[^/\s]+", self.text))
        self.assertIn("ci.yml", self.text)
        self.assertIn("signoff_absent", self.text)
