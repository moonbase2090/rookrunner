# SPDX-License-Identifier: MPL-2.0

"""Job and service containers are created with limits and no new privileges."""

import unittest

from execution_core.run import _account_setup_args, _create_args, _service_create_args

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
        self.assertIn("chown 0:0 /runner-account/sudoers", args[-1])
