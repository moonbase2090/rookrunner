# SPDX-License-Identifier: MIT

import contextlib
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar

import argparse
import io
from contextlib import redirect_stdout
from unittest import mock

from execution_core import cli
from execution_core.cli import call, worker_timeout
from execution_core.poll import Pass, PollError
from execution_core.reconcile import pending_final, post_with_retry, reconcile
from execution_core.status import StatusError

SHA_A = "a" * 40
SHA_B = "b" * 40


class WorkerTimeoutTest(unittest.TestCase):
    def test_default_is_thirty_seconds(self):
        self.assertEqual(worker_timeout({}), 30.0)

    def test_environment_sets_the_timeout(self):
        self.assertEqual(worker_timeout({"ROOKRUNNER_WORKER_TIMEOUT": "12.5"}), 12.5)

    def test_unusable_values_fall_back(self):
        for raw in ("abc", "0", "-3", "601"):
            self.assertEqual(worker_timeout({"ROOKRUNNER_WORKER_TIMEOUT": raw}), 30.0, raw)

    def _slow_worker(self, directory, delay):
        path = Path(directory) / "worker.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            with conn:
                conn.makefile("rb").readline()
                time.sleep(delay)
                reply = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
                with contextlib.suppress(BrokenPipeError):
                    conn.sendall((json.dumps(reply) + "\n").encode())
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def test_slow_worker_times_out_under_a_short_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            thread = self._slow_worker(directory, 0.6)
            with self.assertRaises(TimeoutError):
                call(directory, "worker.describe", {}, timeout=0.2)
            thread.join(2)

    def test_slow_worker_answers_within_the_configured_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            thread = self._slow_worker(directory, 0.6)
            reply = call(directory, "worker.describe", {}, timeout=3)
            self.assertEqual(reply["result"], {"ok": True})
            thread.join(2)


class RetryTest(unittest.TestCase):
    def test_retries_with_doubling_backoff_then_returns(self):
        sleeps, outcomes = [], [StatusError("STATUS_REJECTED", "x"), OSError("y"), "done"]

        def post():
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.assertEqual(post_with_retry(post, sleep=sleeps.append), "done")
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_rate_limit_is_not_retried(self):
        sleeps = []

        def post():
            raise StatusError("RATE_LIMITED", "stop", retryable=True)

        with self.assertRaises(StatusError):
            post_with_retry(post, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_worker_status_refusal_is_not_retried(self):
        sleeps = []

        def post():
            raise StatusError("STATUS_REFUSED", "no matching pending status")

        with self.assertRaises(StatusError):
            post_with_retry(post, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_gives_up_after_the_last_attempt(self):
        sleeps, calls = [], []

        def post():
            calls.append(1)
            raise StatusError("STATUS_REJECTED", "down")

        with self.assertRaises(StatusError):
            post_with_retry(post, attempts=3, sleep=sleeps.append)
        self.assertEqual((len(calls), sleeps), (3, [1.0, 2.0]))


def _state_db(directory, rows, posts):
    db = sqlite3.connect(Path(directory) / "runs.sqlite3")
    db.executescript(
        "CREATE TABLE runs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,"
        " submission_key TEXT UNIQUE, request TEXT, record TEXT, log BLOB);"
        "CREATE TABLE status_posts (run_id TEXT, context TEXT, sha TEXT, state TEXT,"
        " PRIMARY KEY (run_id, context, sha, state));"
    )
    for run_id, state, exit_code in rows:
        record = json.dumps({"state": state, "exit_code": exit_code})
        db.execute(
            "INSERT INTO runs(id, submission_key, request, record, log) VALUES (?,?,?,?,?)",
            (run_id, run_id, "{}", record, b""),
        )
    db.executemany("INSERT INTO status_posts VALUES (?,?,?,?)", posts)
    db.commit()
    db.close()
    return Path(directory) / "runs.sqlite3"


class PendingFinalTest(unittest.TestCase):
    def test_finds_only_final_runs_stuck_at_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _state_db(
                directory,
                [
                    ("ok", "succeeded", 0),
                    ("done", "succeeded", 0),
                    ("live", "running", None),
                    ("bad", "failed", 1),
                ],
                [
                    ("ok", "ci/test", SHA_A, "pending"),
                    ("done", "ci/fmt", SHA_A, "pending"),
                    ("done", "ci/fmt", SHA_A, "success"),
                    ("live", "ci/gui", SHA_A, "pending"),
                    ("bad", "ci/gui", SHA_B, "pending"),
                ],
            )
            found = pending_final(path)
            self.assertEqual(
                [(c["run_id"], c["final"]) for c in found], [("ok", "success"), ("bad", "failure")]
            )
            self.assertEqual([c["run_id"] for c in pending_final(path, ("bbbb",))], ["bad"])
            named = pending_final(path, ("aaaa",))
            self.assertEqual([c["run_id"] for c in named], ["ok", "done"])

    def test_a_rerun_replaces_the_older_run_for_the_same_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _state_db(
                directory,
                [("first", "failed", 1), ("rerun", "succeeded", 0)],
                [("first", "ci/test", SHA_A, "pending"), ("rerun", "ci/test", SHA_A, "pending")],
            )
            self.assertEqual(
                [(c["run_id"], c["final"]) for c in pending_final(path, ("aaaa",))],
                [("rerun", "success")],
            )

    def test_a_newer_active_run_blocks_an_older_final_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _state_db(
                directory,
                [("first", "failed", 1), ("active", "running", None)],
                [
                    ("first", "ci/test", SHA_A, "pending"),
                    ("active", "ci/test", SHA_A, "pending"),
                ],
            )
            self.assertEqual(pending_final(path, ("aaaa",)), [])


class ReconcileTest(unittest.TestCase):
    candidate: ClassVar[dict] = {
        "run_id": "ok",
        "context": "ci/test",
        "sha": SHA_A,
        "final": "success",
    }

    def test_dry_run_posts_nothing(self):
        posted = []
        actions = reconcile([self.candidate], lambda sha: {"ci/test": "pending"}, posted.append)
        self.assertEqual((actions[0]["action"], posted), ("would_post", []))

    def test_skips_a_context_github_already_shows_final(self):
        posted = []
        actions = reconcile(
            [self.candidate], lambda sha: {"ci/test": "success"}, posted.append, dry_run=False
        )
        self.assertEqual((actions[0]["action"], posted), ("already_final", []))

    def test_live_posts_then_second_run_is_a_no_op(self):
        github = {"ci/test": "pending"}
        posted = []

        def post(candidate):
            posted.append(candidate["run_id"])
            github[candidate["context"]] = candidate["final"]

        first = reconcile([self.candidate], lambda sha: dict(github), post, dry_run=False)
        second = reconcile([self.candidate], lambda sha: dict(github), post, dry_run=False)
        self.assertEqual(
            (first[0]["action"], second[0]["action"], posted), ("posted", "already_final", ["ok"])
        )

    def test_live_failure_is_reported_not_raised(self):
        def post(candidate):
            raise StatusError("STATUS_REJECTED", "down")

        actions = reconcile(
            [self.candidate], lambda sha: {}, post, dry_run=False, sleep=lambda s: None
        )
        self.assertEqual(actions[0]["action"], "failed")


class PerRunErrorTest(unittest.TestCase):
    def test_one_failing_run_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Pass(
                repository="owner/name",
                clone=directory,
                jobs=[],
                image=None,
                credential_file=None,
                api_base="https://api.github.com",
                state=directory,
                caller=None,
                clock=None,
            )
            run.state = {
                "submissions": [
                    {"run_id": "r1", "host": None},
                    {"run_id": "r2", "host": None},
                    {"run_id": "r3", "host": None},
                ]
            }
            reached = []

            def post_record(entry):
                reached.append(entry["run_id"])
                if entry["run_id"] == "r1":
                    raise PollError("INVALID_PARAMS", "recorded check does not match the run")
                if entry["run_id"] == "r2":
                    raise TimeoutError("timed out")

            run._post_record = post_record
            run._post_terminal()
            self.assertEqual(reached, ["r1", "r2", "r3"])
            self.assertEqual(
                [(e["run_id"], e["kind"]) for e in run.result["errors"]],
                [("r1", "INVALID_PARAMS"), ("r2", "CLIENT_ERROR")],
            )


def _args(state, **overrides):
    values = {
        "state": str(state),
        "repository": "owner/name",
        "sha": [],
        "dry_run": True,
        "credential_file": None,
        "app_key": None,
        "key_dir": None,
        "clone": None,
        "poll_state": None,
        "api_base": "https://api.github.com",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ReportReconcileTest(unittest.TestCase):
    def _run(self, args, github):
        out = io.StringIO()
        with (
            mock.patch("execution_core.reconcile.github_statuses", side_effect=github),
            redirect_stdout(out),
            self.assertRaises(SystemExit) as stop,
        ):
            cli.report_reconcile(args)
        return stop.exception.code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_dry_run_prints_one_line_per_candidate_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "succeeded", 0), ("bad", "failed", 1)],
                [("ok", "ci/test", SHA_A, "pending"), ("bad", "ci/gui", SHA_B, "pending")],
            )
            seen = []

            def github(api_base, repository, sha, token):
                seen.append((repository, sha, token))
                return {"ci/test": "success"} if sha == SHA_A else {}

            code, lines = self._run(_args(directory), github)
            self.assertEqual(code, 0)
            self.assertEqual(
                [(line["run_id"], line["action"]) for line in lines],
                [("ok", "already_final"), ("bad", "would_post")],
            )
            self.assertEqual(seen, [("owner/name", SHA_A, None), ("owner/name", SHA_B, None)])

    def test_a_failed_post_exits_one(self):
        self.assertEqual(cli._print_reconcile_actions([{"action": "posted"}]), 0)
        with redirect_stdout(io.StringIO()):
            code = cli._print_reconcile_actions([{"action": "posted"}, {"action": "failed"}])
        self.assertEqual(code, 1)

    def test_live_run_with_no_candidates_does_not_need_poll_state(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(directory, [], [])
            out = io.StringIO()
            with (
                mock.patch.object(cli, "read_credential", return_value="token"),
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(_args(directory, dry_run=False, credential_file="/credential"))
            self.assertEqual(stop.exception.code, 0)
            self.assertEqual(out.getvalue(), "")

    def test_live_reconcile_uses_worker_submission_identity_without_poll_state(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "succeeded", 0)],
                [("ok", "ci/test", SHA_A, "pending")],
            )
            calls = []

            def caller(method, params):
                calls.append((method, params))
                if method == "run.get":
                    return {"input": {"tested_commit": "b" * 40}}
                return {"action": "skip", "state": "success"}

            args = _args(
                directory,
                dry_run=False,
                sha=[SHA_A],
                credential_file="/credential",
            )
            out = io.StringIO()
            with (
                mock.patch(
                    "execution_core.reconcile.github_statuses", return_value={"ci/test": "pending"}
                ),
                mock.patch.object(cli, "_reconcile_token", return_value=None),
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token"),
                mock.patch.object(cli, "post_status") as post_status,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(args)

            self.assertEqual(stop.exception.code, 0)
            self.assertEqual(json.loads(out.getvalue())["action"], "posted")
            self.assertEqual(
                [method for method, _ in calls], ["run.get", "run.status", "run.status"]
            )
            self.assertEqual(calls[1][1]["tested_commit"], "b" * 40)
            self.assertEqual(calls[2][1]["record"], "success")
            post_status.assert_called_once_with(
                "https://api.github.com", "owner/name", SHA_A, "success", "ci/test", "token"
            )

    def test_live_reconcile_does_not_post_when_worker_rejects_recovered_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "succeeded", 0)],
                [("ok", "ci/test", SHA_A, "pending")],
            )
            calls = []

            def caller(method, params):
                calls.append((method, params))
                if method == "run.get":
                    return {"input": {"tested_commit": "c" * 40}}
                raise StatusError("STATUS_REFUSED", "base commit does not match submitted commit")

            args = _args(
                directory,
                dry_run=False,
                sha=[SHA_A],
                credential_file="/credential",
            )
            out = io.StringIO()
            with (
                mock.patch(
                    "execution_core.reconcile.github_statuses", return_value={"ci/test": "pending"}
                ),
                mock.patch.object(cli, "_reconcile_token", return_value=None),
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token"),
                mock.patch.object(cli, "post_status") as post_status,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(args)

            self.assertEqual(stop.exception.code, 1)
            self.assertEqual(json.loads(out.getvalue())["action"], "failed")
            self.assertEqual([method for method, _ in calls], ["run.get", "run.status"])
            post_status.assert_not_called()

    def test_live_reconcile_does_not_post_when_worker_requests_a_fresh_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "failed", 1)],
                [("ok", "ci/test", SHA_A, "pending")],
            )
            calls = []

            def caller(method, params):
                calls.append((method, params))
                if method == "run.get":
                    return {"input": {"tested_commit": "b" * 40}}
                return {"action": "retry", "state": "error"}

            args = _args(
                directory,
                dry_run=False,
                sha=[SHA_A],
                credential_file="/credential",
            )
            out = io.StringIO()
            with (
                mock.patch(
                    "execution_core.reconcile.github_statuses", return_value={"ci/test": "pending"}
                ),
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token"),
                mock.patch.object(cli, "post_status") as post_status,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(args)

            self.assertEqual(stop.exception.code, 1)
            self.assertEqual(json.loads(out.getvalue())["action"], "failed")
            self.assertEqual([method for method, _ in calls], ["run.get", "run.status"])
            post_status.assert_not_called()

    def test_live_reconcile_posts_when_worker_says_local_status_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "succeeded", 0)],
                [
                    ("ok", "ci/test", SHA_A, "pending"),
                    ("ok", "ci/test", SHA_A, "success"),
                ],
            )
            poll_state = Path(directory) / "poll-state"
            poll_state.mkdir()
            (poll_state / "poll.json").write_text(
                json.dumps({"submissions": [{"run_id": "ok", "tested_commit": "c" * 40}]})
            )
            args = _args(
                directory,
                dry_run=False,
                sha=[SHA_A],
                poll_state=str(poll_state),
                credential_file="/credential",
            )
            calls = []

            def caller(method, params):
                calls.append((method, params))
                return {"action": "skip", "state": "success"}

            out = io.StringIO()
            with (
                mock.patch(
                    "execution_core.reconcile.github_statuses", return_value={"ci/test": "pending"}
                ),
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token"),
                mock.patch.object(cli, "post_status") as post_status,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(args)

            self.assertEqual(stop.exception.code, 0)
            self.assertEqual(json.loads(out.getvalue())["action"], "posted")
            post_status.assert_called_once_with(
                "https://api.github.com", "owner/name", SHA_A, "success", "ci/test", "token"
            )
            self.assertEqual([method for method, _ in calls], ["run.status", "run.status"])
            self.assertEqual(calls[1][1]["record"], "success")

    def test_live_reconcile_refuses_worker_state_that_differs_from_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_db(
                directory,
                [("ok", "succeeded", 0)],
                [("ok", "ci/test", SHA_A, "pending")],
            )
            poll_state = Path(directory) / "poll-state"
            poll_state.mkdir()
            (poll_state / "poll.json").write_text(
                json.dumps({"submissions": [{"run_id": "ok", "tested_commit": "c" * 40}]})
            )
            calls = []

            def caller(method, params):
                calls.append((method, params))
                return {"action": "skip", "state": "failure"}

            args = _args(
                directory,
                dry_run=False,
                sha=[SHA_A],
                poll_state=str(poll_state),
                credential_file="/credential",
            )
            out = io.StringIO()
            with (
                mock.patch(
                    "execution_core.reconcile.github_statuses", return_value={"ci/test": "pending"}
                ),
                mock.patch.object(cli, "_reconcile_token", return_value=None),
                mock.patch.object(cli, "_worker_caller", return_value=caller),
                mock.patch.object(cli, "read_credential", return_value="token") as read,
                mock.patch.object(cli, "post_status") as post_status,
                redirect_stdout(out),
                self.assertRaises(SystemExit) as stop,
            ):
                cli.report_reconcile(args)

            self.assertEqual(stop.exception.code, 1)
            self.assertEqual(json.loads(out.getvalue())["action"], "failed")
            self.assertEqual([method for method, _ in calls], ["run.status"])
            read.assert_not_called()
            post_status.assert_not_called()

    def test_live_reconcile_refuses_pending_or_unknown_worker_states(self):
        for worker_state in ("pending", "bogus"):
            with (
                self.subTest(worker_state=worker_state),
                tempfile.TemporaryDirectory() as directory,
            ):
                _state_db(
                    directory,
                    [("ok", "succeeded", 0)],
                    [("ok", "ci/test", SHA_A, "pending")],
                )
                poll_state = Path(directory) / "poll-state"
                poll_state.mkdir()
                (poll_state / "poll.json").write_text(
                    json.dumps({"submissions": [{"run_id": "ok", "tested_commit": "c" * 40}]})
                )
                args = _args(
                    directory,
                    dry_run=False,
                    sha=[SHA_A],
                    poll_state=str(poll_state),
                    credential_file="/credential",
                )
                out = io.StringIO()
                with (
                    mock.patch(
                        "execution_core.reconcile.github_statuses",
                        return_value={"ci/test": "pending"},
                    ),
                    mock.patch.object(cli, "_reconcile_token", return_value=None),
                    mock.patch.object(
                        cli,
                        "_worker_caller",
                        return_value=lambda _method, _params, state=worker_state: {
                            "action": "post",
                            "state": state,
                        },
                    ),
                    mock.patch.object(cli, "read_credential", return_value="token") as read,
                    mock.patch.object(cli, "post_status") as post_status,
                    redirect_stdout(out),
                    self.assertRaises(SystemExit) as stop,
                ):
                    cli.report_reconcile(args)

                self.assertEqual(stop.exception.code, 1)
                self.assertEqual(json.loads(out.getvalue())["action"], "failed")
                read.assert_not_called()
                post_status.assert_not_called()


class ReconcileTokenTest(unittest.TestCase):
    def test_no_credentials_reads_anonymously(self):
        self.assertIsNone(cli._reconcile_token(_args("/state")))

    def test_credential_file_wins_over_an_app_key(self):
        args = _args("/state", credential_file="/cred", app_key="/key", clone="/clone")
        with (
            mock.patch.object(cli, "read_credential", return_value="file-token") as read,
            mock.patch.object(cli, "mint_list_token") as mint,
        ):
            self.assertEqual(cli._reconcile_token(args), "file-token")
        read.assert_called_once_with("/cred", "/state", "/clone")
        mint.assert_not_called()

    def test_credential_file_without_a_clone_uses_the_state(self):
        with mock.patch.object(cli, "read_credential", return_value="t") as read:
            cli._reconcile_token(_args("/state", credential_file="/cred"))
        read.assert_called_once_with("/cred", "/state", "/state")

    def test_app_key_mints_a_list_token(self):
        args = _args("/state", app_key="/key", key_dir="/keys", clone="/clone")
        with mock.patch.object(cli, "mint_list_token", return_value="minted") as mint:
            self.assertEqual(cli._reconcile_token(args), "minted")
        mint.assert_called_once_with(
            "/key", "/state", "/clone", "owner/name", "https://api.github.com", key_dir="/keys"
        )

    def test_only_a_minted_string_token_is_revoked(self):
        with mock.patch.object(cli, "revoke_installation_token") as revoke:
            cli._release_reconcile_token(_args("/state", app_key="/key"), "minted")
            cli._release_reconcile_token(_args("/state", app_key="/key"), None)
            cli._release_reconcile_token(_args("/state"), "file-token")
        revoke.assert_called_once_with("https://api.github.com", "minted")


if __name__ == "__main__":
    unittest.main()
