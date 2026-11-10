# SPDX-License-Identifier: MIT

"""diagnose --since bounds, redacts, and filters the skip journal."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, UTC

from execution_core.diagnose import (
    BUNDLE_CAP_BYTES,
    JOURNAL_CAP_BYTES,
    DiagnoseError,
    append_skip,
    bundle,
    parse_since,
)
from execution_core.poll import Pass
from execution_core.protocol import Fault, canonical
from execution_core.worker import Worker

IMAGE = "rust@sha256:" + "ab" * 32
WORKFLOW = """\
name: demo
on: push
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def _state():
    root = tempfile.TemporaryDirectory(prefix="diagnose-")
    path = Path(root.name) / "state"
    path.mkdir()
    os.chmod(path, 0o700)
    return root, path


def _line(at, reason, detail):
    return {
        "at": at.isoformat(),
        "outcome": "skipped",
        "reason": reason,
        "detail": detail,
    }


class SinceTests(unittest.TestCase):
    def test_duration_and_aware_timestamp_are_accepted(self):
        moment = datetime(2026, 11, 10, 12, 30, tzinfo=UTC)
        self.assertEqual(parse_since("2h", moment), moment - timedelta(hours=2))
        self.assertEqual(parse_since("90m", moment), moment - timedelta(minutes=90))
        self.assertEqual(parse_since("1w", moment), moment - timedelta(days=7))
        self.assertEqual(
            parse_since("2026-11-01T00:00:00+00:00", moment),
            datetime(2026, 11, 1, tzinfo=UTC),
        )
        self.assertEqual(
            parse_since("2026-11-01T00:00:00Z", moment),
            datetime(2026, 11, 1, tzinfo=UTC),
        )

    def test_naive_zero_and_words_are_rejected(self):
        moment = datetime(2026, 11, 10, tzinfo=UTC)
        for text in ("2026-11-01T00:00:00", "0h", "2 hours", ""):
            with self.assertRaises(DiagnoseError) as caught:
                parse_since(text, moment)
            self.assertEqual(caught.exception.kind, "INVALID_PARAMS")


class BundleTests(unittest.TestCase):
    def test_since_keeps_the_cutoff_and_drops_earlier_rows(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        moment = datetime(2026, 11, 10, tzinfo=UTC)
        append_skip(state, _line(datetime(2026, 11, 1, tzinfo=UTC), "before-window", "old"))
        append_skip(state, _line(datetime(2026, 11, 8, tzinfo=UTC), "on-cutoff", "edge"))
        append_skip(
            state,
            _line(datetime(2026, 11, 8, tzinfo=UTC) - timedelta(seconds=1), "just-before", "no"),
        )
        result = bundle(state, "2d", now=moment)
        self.assertEqual([item["reason"] for item in result["skips"]], ["on-cutoff"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["omitted"], 0)
        self.assertNotIn("omitted_reasons", result)
        self.assertNotIn("before-window", canonical(result))
        self.assertNotIn("just-before", canonical(result))

    def test_bundle_stays_within_cap_and_keeps_the_newest_rows(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        moment = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for index in range(6):
            append_skip(
                state,
                _line(moment - timedelta(minutes=6 - index), f"reason{index}", "d" * 180),
            )
        result = bundle(state, "1d", now=moment, cap_bytes=700)
        encoded = canonical(result).encode()
        self.assertLessEqual(len(encoded), 700)
        self.assertEqual(result["cap_bytes"], 700)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted"] + len(result["skips"]), 6)
        self.assertGreater(result["omitted"], 0)
        reasons = [item["reason"] for item in result["skips"]]
        self.assertEqual(reasons, ["reason5"])
        self.assertEqual(result["omitted_reasons"]["reason0"], 1)
        self.assertNotIn("reason5", result["omitted_reasons"])
        self.assertEqual(BUNDLE_CAP_BYTES, 256 * 1024)

    def test_tokens_in_a_journal_line_are_redacted(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        token = "ghp_" + "abcDEFghijk"
        pat = "github_pat_" + ("A" * 8)
        bearer = "Bearer " + ("t" * 20)
        header = "-----BEGIN " + "PRIVATE KEY-----"
        footer = "-----END " + "PRIVATE KEY-----"
        pem = header + "\n" + ("a" * 20) + "\n" + footer
        jwt = "eyJ" + ("a" * 8) + "." + ("b" * 8) + "." + ("c" * 8)
        akia = "AKIA" + "IOSFODNN7EXAMP" + "LE"
        assignment = "token=" + ("s" * 12)
        detail = "queue is full " + " ".join((token, pat, bearer, pem, jwt, akia, assignment))
        line = {
            "at": "2026-11-10T00:00:00+00:00",
            "outcome": "not_admitted",
            "reason": "QUEUE_FULL",
            "detail": detail,
        }
        journal = state / "skip-journal.jsonl"
        journal.write_text(canonical(line) + "\n")
        os.chmod(journal, 0o600)
        result = bundle(
            state,
            datetime(2026, 11, 9, tzinfo=UTC),
            now=datetime(2026, 11, 10, 1, tzinfo=UTC),
        )
        text = canonical(result)
        shown = result["skips"][0]["detail"]
        for secret in (token, pat, bearer, pem, jwt, akia, assignment, header):
            self.assertNotIn(secret, shown)
            self.assertNotIn(secret, text)
        self.assertIn("queue is full", shown)
        self.assertIn("***", shown)
        self.assertIn("token=***", shown)
        self.assertEqual(result["skips"][0]["reason"], "QUEUE_FULL")

    def test_journal_file_keeps_the_newest_lines_under_its_cap(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        moment = datetime(2026, 11, 10, tzinfo=UTC)
        for index in range(8):
            append_skip(
                state,
                _line(moment + timedelta(seconds=index), f"slot{index}", "x" * 40),
                journal_cap=600,
            )
        raw = (state / "skip-journal.jsonl").read_bytes()
        self.assertLessEqual(len(raw), 600)
        self.assertIn(b"slot7", raw)
        self.assertNotIn(b"slot0", raw)
        self.assertEqual(JOURNAL_CAP_BYTES, 1024 * 1024)


class CliDiagnoseTests(unittest.TestCase):
    def test_cli_since_filters_redacts_and_reports_the_cap(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        moment = datetime.now(UTC)
        token = "ghs_" + "abcDEFghijk"
        append_skip(
            state,
            _line(moment - timedelta(hours=5), "old-decision", "stale " + token),
        )
        append_skip(
            state,
            _line(moment - timedelta(minutes=10), "new-decision", "queue is full " + token),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(state),
                "diagnose",
                "--since",
                "2h",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([item["reason"] for item in payload["skips"]], ["new-decision"])
        self.assertNotIn(token, result.stdout)
        self.assertIn("queue is full", payload["skips"][0]["detail"])
        self.assertEqual(payload["cap_bytes"], BUNDLE_CAP_BYTES)
        self.assertLessEqual(len(result.stdout.encode()), BUNDLE_CAP_BYTES)

    def test_bad_since_is_invalid_params(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution_core",
                "--state",
                str(state),
                "diagnose",
                "--since",
                "yesterday",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["kind"], "INVALID_PARAMS")
        self.assertEqual(result.stdout, "")

    def test_missing_since_is_a_usage_error(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        result = subprocess.run(
            [sys.executable, "-m", "execution_core", "--state", str(state), "diagnose"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--since", result.stderr)


class WorkerJournalTests(unittest.TestCase):
    def test_admission_and_trigger_misses_are_explained(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        repo = Path(root.name) / "repo"
        workflow = repo / ".github" / "workflows" / "dogfood.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(WORKFLOW)
        subprocess.run(
            ["git", "-C", repo, "init", "--initial-branch=main"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )
        token = "ghp_" + "abcDEFghijk"
        worker = Worker(repo, state)
        worker.start()
        self.addCleanup(worker.close)
        admitted = worker.dispatch(
            "run.submit",
            {
                "version": 0,
                "submission_key": "kept",
                "backend": "development",
                "fixture": {"exit_code": 0, "delay_ms": 0, "output": "ok\n"},
            },
        )
        self.assertIn("run_id", admitted)
        self.assertFalse((state / "skip-journal.jsonl").exists())
        with self.assertRaises(Fault) as caught:
            worker.dispatch(
                "run.submit",
                {
                    "version": 0,
                    "submission_key": token,
                    "backend": "other",
                    "fixture": {},
                },
            )
        self.assertEqual(caught.exception.kind, "CAPABILITY_UNSUPPORTED")
        missed = worker.dispatch(
            "run.submit",
            {
                "version": 1,
                "submission_key": "not-triggered",
                "workflow": ".github/workflows/dogfood.yml",
                "job_id": "ci",
                "event": {},
                "event_name": "pull_request",
                "image": IMAGE,
            },
        )
        self.assertEqual(missed, {"triggered": False})
        journal = (state / "skip-journal.jsonl").read_text()
        self.assertNotIn(token, journal)
        self.assertIn("CAPABILITY_UNSUPPORTED", journal)
        self.assertIn("not_triggered", journal)
        shown = canonical(bundle(state, "1h"))
        self.assertNotIn(token, shown)
        self.assertIn("CAPABILITY_UNSUPPORTED", shown)
        self.assertIn("workflow on did not match this event", shown)
        self.assertEqual(bundle(state, "1h")["runs"]["accepted"], 1)
        later = bundle(state, datetime.now(UTC) + timedelta(days=1))
        self.assertEqual(later["skips"], [])
        self.assertEqual(later["runs"]["accepted"], 0)


class PollJournalTests(unittest.TestCase):
    def test_fork_refusal_is_journaled_with_its_reason(self):
        root, state = _state()
        self.addCleanup(root.cleanup)
        actor = Pass(
            repository="owner/name",
            clone=Path(root.name) / "clone",
            jobs=[],
            image=None,
            credential_file=None,
            api_base="https://example.invalid",
            state=state,
            caller=lambda _method, _params: {},
            clock=datetime(2026, 11, 10, tzinfo=UTC),
        )
        actor.state = {"forks": []}
        actor._record_fork(9, "ab" * 20, "other/name", "head repository is a fork")
        self.assertEqual(actor.result["skipped"], [{"reason": "fork", "number": 9}])
        result = bundle(state, "1h", now=datetime(2026, 11, 10, 0, 1, tzinfo=UTC))
        self.assertEqual(len(result["skips"]), 1)
        self.assertEqual(result["skips"][0]["outcome"], "skipped")
        self.assertEqual(result["skips"][0]["reason"], "fork")
        self.assertEqual(result["skips"][0]["number"], 9)
        self.assertIn("fork", result["skips"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
