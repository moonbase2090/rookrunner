# SPDX-License-Identifier: MIT

import hashlib
import unittest

from execution_core.poll import resubmit_after_setup_refusal, retry_submission_key

STORAGE_FULL = {
    "state": "failed",
    "error": {
        "kind": "SETUP_FAILED",
        "message": "worker storage is full; free space before retrying",
    },
}


class SetupRetryTests(unittest.TestCase):
    def test_storage_setup_refusal_is_resubmitted_once_under_the_retry_key(self):
        key = "poll-" + "a" * 64
        retry = retry_submission_key(key)
        calls = []

        def caller(method, params):
            calls.append(params["submission_key"])
            if params["submission_key"] == key:
                return dict(STORAGE_FULL, run_id="old")
            return {"state": "running", "run_id": "new"}

        result = resubmit_after_setup_refusal(
            caller, {"submission_key": key}, dict(STORAGE_FULL, run_id="old")
        )
        self.assertEqual(result["run_id"], "new")
        self.assertEqual(calls, [retry])

    def test_other_failures_are_not_resubmitted(self):
        key = "poll-" + "b" * 64
        exit_failure = {"state": "failed", "exit_code": 1, "error": None, "run_id": "old"}
        calls = []

        def caller(method, params):
            calls.append(params["submission_key"])
            return {"state": "running", "run_id": "new"}

        result = resubmit_after_setup_refusal(caller, {"submission_key": key}, exit_failure)
        self.assertIs(result, exit_failure)
        self.assertEqual(calls, [])

    def test_retry_key_matches_the_explicit_retry_derivation(self):
        key = "poll-" + "d" * 64
        expected = "poll-" + hashlib.sha256(("retry\0" + key).encode()).hexdigest()
        self.assertEqual(retry_submission_key(key), expected)


if __name__ == "__main__":
    unittest.main()
