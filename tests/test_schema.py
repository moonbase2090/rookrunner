# SPDX-License-Identifier: MPL-2.0

import copy
import json
import unittest

from schema_support import ROOT, SCHEMA, validator


class SchemaTests(unittest.TestCase):
    def test_examples(self):
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        for example in examples:
            with self.subTest(example["name"]):
                check = validator(example["definition"])
                self.assertEqual(check.is_valid(example["value"]), example["valid"])

    def test_every_method_has_request_response_and_examples(self):
        methods = SCHEMA["$defs"]["DescribeResult"]["properties"]["methods"]["const"]
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        for method in methods:
            self.assertIn(f"{method}.request", SCHEMA["$defs"])
            self.assertIn(f"{method}.response", SCHEMA["$defs"])
            self.assertTrue(
                any(
                    e["valid"] and e["definition"] == "Request" and e["value"]["method"] == method
                    for e in examples
                )
            )

    def test_success_requires_zero_and_terminal_attempt_evidence(self):
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        success = next(e["value"] for e in examples if e["name"] == "succeeded run")
        validator("Run").validate(success)
        for field, value in [
            ("exit_code", None),
            ("exit_code", 7),
            ("finished_at", None),
            ("started_at", None),
            ("attempt_id", None),
            ("cancel_requested", True),
            ("cleanup", "not_started"),
        ]:
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(success)
                bad[field] = value
                self.assertFalse(validator("Run").is_valid(bad))

    def test_job_timeout_cancel_allows_cancel_requested_false(self):
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        success = next(e["value"] for e in examples if e["name"] == "succeeded run")
        timed_out = copy.deepcopy(success)
        timed_out.update(
            state="cancelled",
            exit_code=None,
            error=None,
            cancel_requested=False,
            steps=[
                {
                    "index": 0,
                    "id": "sleep",
                    "name": None,
                    "status": "failed",
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "error": "job timed out",
                }
            ],
        )
        validator("Run").validate(timed_out)
        caller = copy.deepcopy(timed_out)
        caller["cancel_requested"] = True
        del caller["steps"]
        validator("Run").validate(caller)
        for field, value in (("exit_code", 1), ("error", {"kind": "STEP_FAILED", "message": "no"})):
            with self.subTest(field=field):
                bad = copy.deepcopy(timed_out)
                bad[field] = value
                self.assertFalse(validator("Run").is_valid(bad))

    def test_unresolved_cleanup_is_only_a_lost_run(self):
        examples = json.loads((ROOT / "schemas/v0/examples.json").read_text())
        success = next(e["value"] for e in examples if e["name"] == "succeeded run")
        lost = copy.deepcopy(success)
        lost.update(
            state="lost",
            exit_code=None,
            cancel_requested=True,
            cleanup="unresolved",
            error={
                "kind": "WORKER_INTERRUPTED",
                "message": "owned container cleanup was not confirmed",
            },
        )
        validator("Run").validate(lost)
        restart = copy.deepcopy(lost)
        restart.update(cancel_requested=False, cleanup="confirmed_no_external_resources")
        validator("Run").validate(restart)
        unresolved_restart = copy.deepcopy(lost)
        unresolved_restart["cancel_requested"] = False
        validator("Run").validate(unresolved_restart)
        cancelled = copy.deepcopy(lost)
        cancelled.update(state="cancelled", error=None, cleanup="unresolved")
        self.assertFalse(validator("Run").is_valid(cancelled))

    def test_error_codes_and_envelopes_are_unambiguous(self):
        reply = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "invalid input",
                "data": {"kind": "INVALID_PARAMS", "retryable": False},
            },
        }
        validator("Response").validate(reply)
        reply["error"]["code"] = -32000
        self.assertFalse(validator("Response").is_valid(reply))
        reply["error"]["code"] = -32602
        reply["result"] = {}
        self.assertFalse(validator("Response").is_valid(reply))
