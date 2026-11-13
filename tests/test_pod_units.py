# SPDX-License-Identifier: MIT

"""Check the pod unit files, account setup and credential permissions."""

import configparser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


RECIPE = Path(__file__).resolve().parents[1] / "images" / "x86-pod"
UBUNTU = "ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55"


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(RECIPE / "systemd" / f"{name}.service")
    return parser


class PodUnitTests(unittest.TestCase):
    def test_only_the_bridge_unit_loads_the_token(self):
        for name in ("pod-mux", "pod-agent"):
            with self.subTest(unit=name):
                self.assertNotIn("LoadCredential", unit(name)["Service"])
        credential = unit("pod-bridge")["Service"]["LoadCredential"]
        self.assertEqual(credential, "pod-token:/run/pmux-pod-boot/pod-token")

    def test_shell_side_units_hide_the_bridge_credential_and_boot_directory(self):
        for name in ("pod-mux", "pod-agent"):
            with self.subTest(unit=name):
                hidden = unit(name)["Service"]["InaccessiblePaths"].split()
                self.assertIn("-/run/credentials/pod-bridge.service", hidden)
                self.assertIn("-/run/pmux-pod-boot", hidden)

    def test_the_bridge_cannot_reach_the_private_mux_socket(self):
        self.assertIn("/run/pmux-mux", unit("pod-bridge")["Service"]["InaccessiblePaths"].split())
        mux = unit("pod-mux")["Service"]
        self.assertEqual(mux["RuntimeDirectory"], "pmux-mux")
        self.assertEqual(mux["RuntimeDirectoryMode"], "0700")
        self.assertEqual((mux["User"], mux["Group"]), ("podshell", "podshell"))

    def test_the_agent_and_its_commands_never_hold_the_bridge_group(self):
        agent = unit("pod-agent")["Service"]
        self.assertEqual((agent["User"], agent["Group"]), ("podshell", "podshell"))
        for name in ("pod-mux", "pod-agent"):
            service = unit(name)["Service"]
            self.assertNotIn("SupplementaryGroups", service)
            self.assertNotEqual(service["Group"], "pmuxbridge")
        self.assertEqual(unit("pod-bridge")["Service"]["User"], "pmuxbridge")

    def test_the_agent_socket_directory_hands_the_bridge_group_to_the_socket(self):
        lines = [
            line.split()
            for line in (RECIPE / "tmpfiles" / "pod-agent.conf").read_text().splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(lines, [["d", "/run/pmux-pod", "2770", "podshell", "pmuxbridge", "-"]])
        agent = unit("pod-agent")["Service"]
        self.assertIn("--socket /run/pmux-pod/agent.sock", agent["ExecStart"])
        self.assertIn("/run/pmux-pod", agent["ReadWritePaths"].split())
        self.assertNotIn("RuntimeDirectory", agent)

    def test_install_creates_the_shell_account_without_the_bridge_group(self):
        script = (RECIPE / "install.sh").read_text()
        useradd = [line for line in script.splitlines() if "useradd" in line]
        shell = [line for line in useradd if line.rstrip().endswith("podshell")]
        self.assertEqual(len(shell), 1)
        self.assertNotRegex(shell[0], r"(-G|--groups|--gid|-g)\b")
        self.assertNotRegex(script, r"usermod[^\n]*podshell")
        self.assertNotRegex(script, r"gpasswd[^\n]*podshell")
        self.assertIn("/etc/tmpfiles.d/pod-agent.conf", script)

    def test_pod_verify_refuses_a_shell_account_in_the_bridge_group(self):
        script = (RECIPE / "verify-installed.sh").read_text()
        self.assertRegex(script, r'\*" pmuxbridge "\*\)')

    def test_readme_matches_the_units_on_the_agent_group(self):
        readme = (RECIPE / "README.md").read_text()
        row = next(line for line in readme.splitlines() if line.startswith("| `pod-agent`"))
        self.assertIn("group `podshell`", row)
        self.assertNotIn("Group=pmuxbridge", readme)
        self.assertIsNone(re.search(r"agent unit selects the bridge group", readme))


@unittest.skipUnless(shutil.which("docker"), "Docker is required for the permission proof")
class PodCredentialProofTests(unittest.TestCase):
    def run_proof(self, script):
        return subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "-i", UBUNTU, "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_shell_and_agent_cannot_read_the_token_and_the_bridge_reaches_the_socket(self):
        result = self.run_proof((RECIPE / "credential-proof.sh").read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("socket: podshell:pmuxbridge 770", result.stdout)
        self.assertIn("allowed: bridge reads its credential copy", result.stdout)

    def test_the_proof_fails_when_the_socket_directory_loses_its_setgid_bit(self):
        script = (RECIPE / "credential-proof.sh").read_text()
        broken = script.replace("-m 2770 /run/pmux-pod", "-m 0770 /run/pmux-pod")
        self.assertNotEqual(broken, script)
        self.assertNotEqual(self.run_proof(broken).returncode, 0)

    def test_the_proof_fails_when_the_shell_account_joins_the_bridge_group(self):
        script = (RECIPE / "credential-proof.sh").read_text()
        broken = script.replace(
            "useradd --no-create-home --shell /bin/bash podshell",
            "useradd --no-create-home --shell /bin/bash --groups pmuxbridge podshell",
        )
        self.assertNotEqual(broken, script)
        self.assertNotEqual(self.run_proof(broken).returncode, 0)
