# SPDX-License-Identifier: MIT

"""Replay shared synthetic read responses through the existing terminal dashboard."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from execution_core.dashboard import Dashboard, _interact


def visible_controls(text):
    return "".join(
        char if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127) else f"\\x{ord(char):02x}"
        for char in text
    )


class Comparison(Dashboard):
    def __init__(self, bundle, mode):
        self.bundle = bundle
        self.mode = mode
        super().__init__("fixture-only")
        self.timestamp = bundle["observed_at"]

    def _call(self, method, params):
        if self.mode == "unavailable":
            self.unavailable = True
            return None
        for row in self.bundle["responses"]:
            if row["method"] == method and row["params"] == params:
                return deepcopy(row["reply"])
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32601,
                "message": "Read-only comparison has no matching fixture response",
                "data": {"kind": "METHOD_NOT_FOUND", "retryable": False},
            },
        }

    def render(self):
        stamp = self.bundle["last_observed_at"] if self.mode == "stale" else self.timestamp
        header = (
            f"Synthetic comparison. Observation mode: {self.mode}. Last observation: {stamp}.\n"
        )
        return header + visible_controls(super().render())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=Path("tests/fixtures/dashboard/phase1.json")
    )
    parser.add_argument("--mode", choices=("current", "stale", "unavailable"))
    parser.add_argument("--run", help="Select a run by its full identifier")
    parser.add_argument("--next-log", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    bundle = json.loads(args.fixture.read_text())
    board = Comparison(bundle, args.mode or bundle["mode"])
    if args.run and not board.unavailable:
        for index, run in enumerate(board.runs):
            if run["run_id"] == args.run:
                board.selected = index
                board.press("enter")
                break
        else:
            parser.error("run is not in the comparison list")
    if args.next_log:
        board.press("n")
    if args.interactive:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("interactive comparison requires a terminal")
        _interact(board)
    else:
        sys.stdout.write(board.render())


if __name__ == "__main__":
    main()
