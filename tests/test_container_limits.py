# SPDX-License-Identifier: MIT

"""Job and service containers are created with limits and no new privileges."""

from pathlib import Path
import unittest

from execution_core.run import RunError, _account_setup_args, _create_args, _service_create_args

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
        self.assertEqual(adds, ["SETUID", "SETGID", "AUDIT_WRITE"])
        self.assertTrue(any("destination=/etc/passwd,readonly" in part for part in job))
        self.assertTrue(
            any("destination=/etc/sudoers.d/rookrunner,readonly" in part for part in job)
        )

    def test_passwordless_sudo_rejects_a_non_boolean(self):
        with self.assertRaises(RunError) as raised:
            _account_setup_args("image@sha256:aa", "/account", "yes")
        self.assertEqual(str(raised.exception), "passwordless sudo must be a boolean")
