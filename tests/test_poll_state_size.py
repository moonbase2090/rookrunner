# SPDX-License-Identifier: MIT

"""The poll state must stay readable: a save never writes more than a load accepts."""

import json
import tempfile
import unittest
from pathlib import Path

import execution_core.poll as poll
from execution_core.poll import PollError, _load_state, _save_state

REPO = "owner/repo"


def commit(n):
    return f"{n:040x}"


def entry(n):
    return {
        "context": "ci/test",
        "event": "push",
        "status_sha": commit(n),
        "tested_commit": commit(n),
    }


def state(completed, **extra):
    base = {
        "version": 1,
        "repository": REPO,
        "etags": {},
        "tips": {},
        "pulls": {},
        "submissions": [],
        "completed": completed,
        "forks": [],
        "merge_absent": [],
    }
    base.update(extra)
    return base


class StateSizeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "poll.json"

    def over_cap_completed(self, live):
        """Enough completed entries to pass the read cap; the first `live` are still named."""
        count = poll._MAX_BODY // 150
        completed = {f"poll-{n:064x}": entry(n) for n in range(count)}
        tips = {f"branch-{n}": commit(n) for n in range(live)}
        return state(completed, tips=tips)

    def test_a_state_over_the_read_cap_is_pruned_and_reads_back(self):
        payload = self.over_cap_completed(live=5)
        self.assertGreater(len(json.dumps(payload, separators=(",", ":"))), poll._MAX_BODY)
        _save_state(self.path, payload)
        self.assertLessEqual(self.path.stat().st_size, poll._MAX_BODY)
        loaded = _load_state(self.path, REPO)
        kept = {meta["status_sha"] for meta in loaded["completed"].values()}
        self.assertEqual(kept, {commit(n) for n in range(5)})

    def test_pruning_changes_the_dict_the_pass_already_holds(self):
        payload = self.over_cap_completed(live=3)
        held = payload["completed"]
        _save_state(self.path, payload)
        self.assertIs(payload["completed"], held)
        self.assertEqual(len(held), 3)

    def test_a_state_under_the_cap_is_written_whole(self):
        payload = state({f"poll-{n:064x}": entry(n) for n in range(50)})
        _save_state(self.path, payload)
        self.assertEqual(len(_load_state(self.path, REPO)["completed"]), 50)

    def test_a_state_that_cannot_shrink_leaves_the_last_readable_file(self):
        good = state({"poll-a": entry(1)}, tips={"main": commit(1)})
        _save_state(self.path, good)
        before = self.path.read_bytes()
        names = {f"branch-{n}": commit(n) for n in range(poll._MAX_BODY // 40)}
        with self.assertRaises(PollError) as raised:
            _save_state(self.path, state({}, tips=names))
        self.assertEqual(raised.exception.kind, "STATE_INVALID")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(_load_state(self.path, REPO)["tips"], {"main": commit(1)})


if __name__ == "__main__":
    unittest.main()
