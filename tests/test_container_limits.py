# SPDX-License-Identifier: MPL-2.0

"""Job and service containers are created with memory, pid, and cpu limits."""

import unittest

from execution_core.run import _create_args, _service_create_args

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
