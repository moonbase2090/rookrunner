"""Exercise the payload verifier and extractor with real archives and CLI calls."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


RECIPE = Path(__file__).resolve().parents[1] / "images" / "x86-pod"


class PodPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # An architecture header fixture, never used as runtime proof of a tool.
        elf = b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00"
        self.files = {
            "bin/pmux": elf,
            "bin/pmuxd": elf,
            "bin/pmux-mcp": elf,
            "toolchains/node/bin/node": elf,
            "toolchains/node/bin/npm": b"#!/bin/sh\nexit 0\n",
            "toolchains/rust/bin/cargo": elf,
            "toolchains/rust/bin/rustc": elf,
            "clients/bin/client": b"#!/bin/sh\nexit 0\n",
            "cache/cargo/registry/cache/public/example-1.0.0.crate": b"cached crate",
            "cache/cargo/registry/index/public/config.json": b"{}",
            "cache/npm/_cacache/content-v2/example": b"cached package",
            "licenses/mux/LICENSE": b"MIT license fixture",
        }
        self.manifest = {
            "version": 1,
            "architecture": "x86_64",
            "components": [
                {
                    "name": group,
                    "group": group,
                    "version": "1.0.0",
                    "source": "https://example.com/source/v1.0.0",
                    "license": "MIT",
                    "notice": "licenses/mux/LICENSE",
                }
                for group in ("mux", "node", "rust", "clients", "cache")
            ],
        }
        self.extra = []

    def bundle(self):
        self.files["manifest.json"] = json.dumps(self.manifest).encode()
        archive = self.root / "payload.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in self.files.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o755 if "/bin/" in name or name.startswith("bin/") else 0o644
                bundle.addfile(item, io.BytesIO(data))
            for item in self.extra:
                bundle.addfile(item)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def invoke(self, action="verify", digest=None):
        archive, actual = self.bundle()
        return subprocess.run(
            [
                sys.executable,
                str(RECIPE / "payload.py"),
                action,
                str(archive),
                digest or actual,
                "--destination",
                str(self.root / "installed"),
            ],
            capture_output=True,
            text=True,
        )

    def assert_rejected(self, reason):
        result = self.invoke("install")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertFalse((self.root / "installed").exists())

    def test_install_preserves_tool_and_cache_bytes_and_refuses_overwrite(self):
        result = self.invoke("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])
        installed = self.root / "installed"
        self.assertEqual((installed / "clients/bin/client").read_bytes(), b"#!/bin/sh\nexit 0\n")
        self.assertEqual(
            (installed / "cache/npm/_cacache/content-v2/example").read_bytes(), b"cached package"
        )
        self.assertEqual((installed / "bin/pmux").stat().st_mode & 0o777, 0o755)
        again = self.invoke("install")
        self.assertEqual(again.returncode, 1)
        self.assertIn("File exists", again.stderr)

    def test_checksum_rejection_precedes_extraction(self):
        result = self.invoke("install", "0" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertFalse((self.root / "installed").exists())

    def test_rejects_arm_binary_even_with_x86_manifest(self):
        self.files["bin/pmuxd"] = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00"
        self.assert_rejected("not a Linux x86_64 ELF")

    def test_rejects_missing_bridge(self):
        del self.files["bin/pmux-mcp"]
        self.assert_rejected("missing bin/pmux-mcp")

    def test_rejects_empty_client_roster(self):
        del self.files["clients/bin/client"]
        self.assert_rejected("no installed client CLI")

    def test_rejects_cold_cargo_cache(self):
        del self.files["cache/cargo/registry/cache/public/example-1.0.0.crate"]
        self.assert_rejected("cache is cold")

    def test_rejects_missing_license_notice(self):
        del self.files["licenses/mux/LICENSE"]
        self.assert_rejected("license notice is missing")

    def test_rejects_source_url_with_token(self):
        self.manifest["components"][0]["source"] = "https://example.com/source?token=fixture"
        self.assert_rejected("without credentials")

    def test_rejects_credentials_and_home_directories(self):
        for name in (
            "cache/npm/.npmrc",
            "cache/cargo/credentials.toml",
            "clients/.ssh/key",
            "home/user/state",
        ):
            with self.subTest(name=name):
                self.files[name] = b"fixture"
                self.assert_rejected("payload rejected")
                del self.files[name]

    def test_rejects_traversal(self):
        self.files["../escaped"] = b"must not extract"
        self.assert_rejected("path escapes")
        self.assertFalse((self.root / "escaped").exists())

    def test_rejects_absolute_symlink_and_symlink_parent(self):
        link = tarfile.TarInfo("clients/lib")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        self.extra = [link]
        self.assert_rejected("symlink escapes")
        link.linkname = "../cache"
        self.files["clients/lib/write-through"] = b"must not extract"
        self.assert_rejected("writes through a symlink")

    def test_accepts_internal_npm_symlink(self):
        del self.files["toolchains/node/bin/npm"]
        self.files["toolchains/node/lib/bin/npm.js"] = b"#!/usr/bin/env node\n"
        link = tarfile.TarInfo("toolchains/node/bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = "../lib/bin/npm.js"
        self.extra = [link]
        result = self.invoke("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "installed/toolchains/node/bin/npm").read_bytes(), b"#!/usr/bin/env node\n"
        )

    def test_rejects_broken_npm_symlink(self):
        del self.files["toolchains/node/bin/npm"]
        link = tarfile.TarInfo("toolchains/node/bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = "../missing.js"
        self.extra = [link]
        self.assert_rejected("npm must resolve to an executable")


class BridgeLaunchTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "pod_start_bridge", RECIPE / "start-bridge.py"
        )
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)

    def test_launch_passes_credential_path_and_scrubs_inherited_environment(self):
        config = '{"hub_url":"wss://example.com/connect","pod_id":"pod-01"}'
        with (
            patch.object(self.launcher.Path, "read_text", return_value=config),
            patch.dict(
                self.launcher.os.environ,
                {
                    "CREDENTIALS_DIRECTORY": "/run/credentials/pod-bridge.service",
                    "UNRELATED_TOKEN": "must-not-inherit",
                },
                clear=True,
            ),
            patch.object(self.launcher.os, "execve") as execute,
        ):
            self.launcher.main()
        execute.assert_called_once_with(
            "/opt/pod/bin/pmux-mcp",
            [
                "pmux-mcp",
                "--pod",
                "--hub-url",
                "wss://example.com/connect",
                "--pod-id",
                "pod-01",
                "--token-file",
                "/run/credentials/pod-bridge.service/pod-token",
                "--agent-socket",
                "/run/pmux-pod/agent.sock",
            ],
            {"PATH": "/usr/bin:/bin", "HOME": "/var/lib/pmuxbridge"},
        )

    def test_rejects_credentials_and_cleartext_in_hub_url(self):
        for url in (
            "ws://example.com/connect",
            "wss://user:pass@example.com/connect",
            "wss://example.com/connect?token=fixture",
        ):
            with (
                self.subTest(url=url),
                patch.object(
                    self.launcher.Path,
                    "read_text",
                    return_value=json.dumps({"hub_url": url, "pod_id": "pod-01"}),
                ),
                self.assertRaisesRegex(ValueError, "WSS URL without credentials"),
            ):
                self.launcher.main()

    def test_rejects_invalid_pod_identity(self):
        with (
            patch.object(
                self.launcher.Path,
                "read_text",
                return_value='{"hub_url":"wss://example.com/connect","pod_id":"../other"}',
            ),
            self.assertRaisesRegex(ValueError, "invalid pod_id"),
        ):
            self.launcher.main()


if __name__ == "__main__":
    unittest.main()
