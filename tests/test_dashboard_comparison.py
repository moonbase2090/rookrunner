# SPDX-License-Identifier: MIT

"""Shared comparison data exercises the existing terminal renderer."""

from copy import deepcopy
import json
from pathlib import Path
import runpy
import unittest

from schema_support import validate_response, validator

ROOT = Path(__file__).resolve().parents[1]
Comparison = runpy.run_path(str(ROOT / "scripts/dashboard-comparison.py"))["Comparison"]


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.bundle = json.loads((ROOT / "tests/fixtures/dashboard/phase1.json").read_text())

    def test_current_list_distinguishes_terminal_success_from_invalid_success(self):
        text = Comparison(self.bundle, "current").render()
        self.assertIn("00000000-0000-4000-8000-000000000001 queued result queued", text)
        self.assertIn("00000000-0000-4000-8000-000000000002 running result running", text)
        self.assertIn("00000000-0000-4000-8000-000000000004 lost result lost", text)
        self.assertIn("00000000-0000-4000-8000-000000000005 succeeded result succeeded", text)
        self.assertIn("00000000-0000-4000-8000-000000000006 succeeded result not succeeded", text)

    def test_failed_detail_displays_control_bytes_and_navigates_stored_pages(self):
        board = Comparison(self.bundle, "current")
        board.selected = 2
        board.press("enter")
        text = board.render()
        self.assertIn('Compile <script>alert("fixture")</script>', text)
        self.assertIn("^[[31merror^[[0m^G", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("out/report.txt", text)
        board.press("n")
        self.assertIn("Test failed with exit code 1", board.render())
        self.assertNotIn("^[[31merror", board.render())

    def test_stale_and_unavailable_observations_do_not_invent_run_outcomes(self):
        stale = Comparison(self.bundle, "stale").render()
        self.assertIn("Observation mode: stale", stale)
        self.assertIn("00000000-0000-4000-8000-000000000002 running result running", stale)
        unavailable = Comparison(self.bundle, "unavailable").render()
        self.assertIn("worker socket is not available", unavailable)
        self.assertNotIn("00000000-0000-4000-8000-000000000005", unavailable)

    def test_fixture_positive_replies_match_contract_and_negative_success_is_rejected(self):
        for row in self.bundle["responses"]:
            reply = deepcopy(row["reply"])
            if row["method"] == "run.list":
                reply["result"]["runs"] = [
                    record
                    for record in reply["result"]["runs"]
                    if record["run_id"] not in self.bundle["negative_run_ids"]
                ]
            if (
                row["method"] == "run.get"
                and row["params"]["run_id"] in self.bundle["negative_run_ids"]
            ):
                errors = list(validator("Run").iter_errors(reply["result"]))
                self.assertIn(["exit_code"], [list(error.path) for error in errors])
            else:
                validate_response(row["method"], reply)


if __name__ == "__main__":
    unittest.main()
