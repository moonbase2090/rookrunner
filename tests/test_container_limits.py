# SPDX-License-Identifier: MIT

"""Job and service containers are created with limits and no new privileges."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from execution_core.run import (
    RunError,
    _account_setup_args,
    _create_args,
    _job_isolation_args,
    _service_create_args,
)

_EXPECTED = {
    "--memory": "4g",
    "--memory-swap": "4g",
    "--pids-limit": "1024",
    "--cpus": "2",
}


def _flag(args, name):
    return args[args.index(name) + 1]


class ContainerLimitTests(unittest.TestCase):
    def test_job_and_service_creates_use_the_same_limits(self):
        job = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {
                "home": "/home",
                "runner-temp": "/temp",
                "tool-cache": "/tools",
            },
            None,
            None,
        )
        service = _service_create_args(
            "svc",
            "net",
            {"id": "echo", "image": "image@sha256:aa"},
            "job",
        )
        for args in (job, service):
            self.assertEqual(
                {name: _flag(args, name) for name in _EXPECTED},
                _EXPECTED,
            )
            self.assertEqual(args.count("--memory"), 1)
        self.assertEqual(_flag(job, "--network"), "bridge")
        self.assertNotIn("--privileged", job)
        self.assertNotIn("--privileged", service)

    def test_a_job_drops_capabilities_and_a_service_keeps_the_default_set(self):
        job = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {
                "home": "/home",
                "runner-temp": "/temp",
                "tool-cache": "/tools",
            },
            None,
            None,
        )
        service = _service_create_args(
            "svc",
            "net",
            {"id": "echo", "image": "image@sha256:aa"},
            "job",
        )
        self.assertEqual(_flag(job, "--security-opt"), "no-new-privileges")
        self.assertEqual(_flag(job, "--cap-drop"), "ALL")
        self.assertEqual(job.count("--security-opt"), 1)
        self.assertEqual(job.count("--cap-drop"), 1)
        self.assertNotIn("--cap-add", job)
        self.assertNotIn("--privileged", job)
        self.assertEqual(_flag(service, "--security-opt"), "no-new-privileges")
        self.assertEqual(service.count("--security-opt"), 1)
        self.assertNotIn("--cap-drop", service)
        self.assertNotIn("--cap-add", service)
        self.assertNotIn("--privileged", service)

    def test_account_setup_stays_root_without_the_isolation_flags(self):
        args = _account_setup_args("image@sha256:aa", "/account")
        self.assertEqual(_flag(args, "--user"), "0:0")
        self.assertNotIn("--cap-drop", args)
        self.assertNotIn("--security-opt", args)
        self.assertNotIn("--privileged", args)
        self.assertIn("chown 0:0 /runner-account/passwd", args[-1])
        self.assertNotIn("sudoers", args[-1])
        self.assertNotIn("NOPASSWD", args[-1])
        self.assertNotIn("command -v sudo", args[-1])

    def test_passwordless_sudo_writes_sudoers_and_keeps_only_sudo_capabilities(self):
        account = Path("/account")
        setup = _account_setup_args("image@sha256:aa", account, True)
        self.assertIn("command -v sudo", setup[-1])
        self.assertIn("NOPASSWD: ALL", setup[-1])
        self.assertIn("chown 0:0 /runner-account/sudoers", setup[-1])
        self.assertNotIn("--cap-drop", setup)
        job = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {
                "home": "/home",
                "runner-temp": "/temp",
                "tool-cache": "/tools",
            },
            None,
            None,
            account=account,
            passwordless_sudo=True,
        )
        self.assertNotIn("no-new-privileges", job)
        self.assertEqual(_flag(job, "--cap-drop"), "ALL")
        self.assertEqual(job.count("--cap-drop"), 1)
        adds = [job[index + 1] for index, part in enumerate(job) if part == "--cap-add"]
        self.assertEqual(
            adds,
            [
                "SETUID",
                "SETGID",
                "AUDIT_WRITE",
                "CHOWN",
                "DAC_OVERRIDE",
                "DAC_READ_SEARCH",
                "FOWNER",
            ],
        )
        self.assertTrue(any("destination=/etc/passwd,readonly" in part for part in job))
        self.assertTrue(
            any("destination=/etc/sudoers.d/rookrunner,readonly" in part for part in job)
        )

    def test_job_privileges_default_output_is_byte_identical(self):
        self.assertEqual(
            _job_isolation_args(),
            ["--security-opt", "no-new-privileges", "--cap-drop", "ALL"],
        )
        self.assertEqual(
            _job_isolation_args(passwordless_sudo=True),
            [
                "--cap-drop",
                "ALL",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "AUDIT_WRITE",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "DAC_READ_SEARCH",
                "--cap-add",
                "FOWNER",
            ],
        )
        for args in (_job_isolation_args(), _job_isolation_args(passwordless_sudo=True)):
            self.assertNotIn("SYS_CHROOT", args)
            self.assertFalse(any(part.startswith("seccomp=") for part in args))
            self.assertNotIn("seccomp=unconfined", args)

    def test_job_privileges_adds_only_sys_chroot_and_personality_seccomp(self):
        import json

        args = _job_isolation_args(job_privileges=True)
        self.assertIn("no-new-privileges", args)
        self.assertEqual(_flag(args, "--cap-drop"), "ALL")
        adds = [args[index + 1] for index, part in enumerate(args) if part == "--cap-add"]
        self.assertEqual(adds, ["SYS_CHROOT"])
        seccomp = [part for part in args if part.startswith("seccomp=")]
        self.assertEqual(len(seccomp), 1)
        self.assertNotIn("unconfined", seccomp[0])
        profile_path = seccomp[0].removeprefix("seccomp=")
        profile = json.loads(Path(profile_path).read_text())
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        personality = [
            entry for entry in profile["syscalls"] if entry.get("names") == ["personality"]
        ]
        self.assertTrue(personality)
        for entry in personality:
            self.assertEqual(entry.get("action"), "SCMP_ACT_ALLOW")
            self.assertTrue(
                entry.get("args"),
                "personality entry without an argument filter",
            )
            self.assertEqual(len(entry["args"]), 1)
            self.assertEqual(entry["args"][0]["index"], 0)
            self.assertEqual(entry["args"][0]["op"], "SCMP_CMP_EQ")
        self.assertEqual(
            sorted(entry["args"][0]["value"] for entry in personality),
            [0, 262144, 4294967295],
        )
        chroot = [entry for entry in profile["syscalls"] if "chroot" in entry.get("names", [])]
        self.assertEqual(len(chroot), 1)
        self.assertEqual(chroot[0]["includes"], {"caps": ["CAP_SYS_CHROOT"]})

    def test_job_privileges_composes_with_passwordless_sudo(self):
        args = _job_isolation_args(passwordless_sudo=True, job_privileges=True)
        self.assertNotIn("no-new-privileges", args)
        adds = [args[index + 1] for index, part in enumerate(args) if part == "--cap-add"]
        self.assertEqual(
            adds,
            [
                "SETUID",
                "SETGID",
                "AUDIT_WRITE",
                "CHOWN",
                "DAC_OVERRIDE",
                "DAC_READ_SEARCH",
                "FOWNER",
                "SYS_CHROOT",
            ],
        )
        self.assertTrue(any(part.startswith("seccomp=") for part in args))

    def test_job_privileges_create_args_carry_the_grants(self):
        job = _create_args(
            "job",
            "/workspace",
            "/private",
            "/commands",
            "image@sha256:aa",
            "bridge",
            None,
            None,
            {"home": "/home", "runner-temp": "/temp", "tool-cache": "/tools"},
            None,
            None,
            job_privileges=True,
        )
        self.assertIn("SYS_CHROOT", job)
        self.assertTrue(any(part.startswith("seccomp=") for part in job))
        self.assertNotIn("--privileged", job)

    def test_job_privileges_rejects_a_non_boolean(self):
        with self.assertRaises(RunError) as raised:
            _job_isolation_args(job_privileges="yes")
        self.assertEqual(str(raised.exception), "job privileges must be a boolean")

    def test_passwordless_sudo_rejects_a_non_boolean(self):
        with self.assertRaises(RunError) as raised:
            _account_setup_args("image@sha256:aa", "/account", "yes")
        self.assertEqual(str(raised.exception), "passwordless sudo must be a boolean")


_PROBE = """
import ctypes, os, platform
libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_ulong]
nr = 92 if platform.machine() in ("aarch64", "arm64") else 135
def personality(value):
    ctypes.set_errno(0)
    ret = libc.syscall(nr, ctypes.c_ulong(value))
    return ret, ctypes.get_errno()
ret, err = personality(0xFFFFFFFF)
print(f"personality_read ret={ret} errno={err}", flush=True)
ret, err = personality(0x0080000)
print(f"personality_denied ret={ret} errno={err}", flush=True)
ret, err = personality(0x0040000)
print(f"personality_set ret={ret} errno={err}", flush=True)
ret, err = personality(0)
print(f"personality_plain ret={ret} errno={err}", flush=True)
pid = os.fork()
if pid == 0:
    try:
        os.chroot("/tmp")
        print("chroot_ok", flush=True)
        os._exit(0)
    except OSError as exc:
        print(f"chroot_fail errno={exc.errno}", flush=True)
        os._exit(1)
os.waitpid(pid, 0)
"""


@unittest.skipUnless(shutil.which("docker"), "Docker is required")
class JobPrivilegesDockerTests(unittest.TestCase):
    def test_opt_in_allows_personality_and_chroot_default_does_not(self):
        image = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim"],
            capture_output=True,
        )
        if image.returncode != 0:
            self.skipTest("python:3.12-slim image is not available")
        with tempfile.TemporaryDirectory(prefix="job-privileges-") as directory:
            probe = Path(directory) / "probe.py"
            probe.write_text(_PROBE)
            mount = f"{probe}:/probe.py:ro"
            default = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *_job_isolation_args(),
                    "-v",
                    mount,
                    "python:3.12-slim",
                    "python3",
                    "/probe.py",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            opted = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *_job_isolation_args(job_privileges=True),
                    "-v",
                    mount,
                    "python:3.12-slim",
                    "python3",
                    "/probe.py",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertIn("personality_set ret=-1 errno=1", default.stdout, default.stderr)
        self.assertIn("chroot_fail errno=1", default.stdout, default.stderr)
        self.assertIn("personality_read ret=0 errno=0", opted.stdout, opted.stderr)
        self.assertIn("personality_denied ret=-1 errno=1", opted.stdout, opted.stderr)
        self.assertIn("personality_set ret=0 errno=0", opted.stdout, opted.stderr)
        self.assertIn("personality_plain ret=262144 errno=0", opted.stdout, opted.stderr)
        self.assertIn("chroot_ok", opted.stdout, opted.stderr)
