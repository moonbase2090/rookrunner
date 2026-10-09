"""Cron matching and the one-tick catch-up rule. Times are UTC."""

import unittest
from datetime import datetime, UTC

from execution_core.cron import cron_matches, parse_cron, schedule_decision
from execution_core.poll import allowlist_matches
from execution_core.protocol import Fault
from execution_core.trigger import submission_triggered


def at(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class CronTests(unittest.TestCase):
    def test_steps_ranges_lists_and_names_match_utc_minutes(self):
        every_fifteen = parse_cron("*/15 * * * *")
        self.assertTrue(cron_matches(every_fifteen, at(2026, 10, 8, 12, 0)))
        self.assertTrue(cron_matches(every_fifteen, at(2026, 10, 8, 12, 45)))
        self.assertFalse(cron_matches(every_fifteen, at(2026, 10, 8, 12, 10)))

        office = parse_cron("0 1-5 * * *")
        self.assertTrue(cron_matches(office, at(2026, 10, 8, 1, 0)))
        self.assertTrue(cron_matches(office, at(2026, 10, 8, 5, 0)))
        self.assertFalse(cron_matches(office, at(2026, 10, 8, 6, 0)))

        listed = parse_cron("0,30 9 * JAN,FEB MON")
        self.assertTrue(cron_matches(listed, at(2026, 1, 5, 9, 0)))
        self.assertTrue(cron_matches(listed, at(2026, 2, 9, 9, 30)))
        self.assertFalse(cron_matches(listed, at(2026, 1, 5, 9, 15)))
        self.assertFalse(cron_matches(listed, at(2026, 3, 2, 9, 0)))
        self.assertFalse(cron_matches(listed, at(2026, 1, 6, 9, 0)))

    def test_utc_minute_does_not_move_on_a_dst_transition_date(self):
        # 2026-03-08 and 2026-11-01 are US DST boundaries. The match is the
        # UTC clock, so 02:30 stays 02:30.
        half = parse_cron("30 2 * * *")
        self.assertTrue(cron_matches(half, at(2026, 3, 8, 2, 30)))
        self.assertTrue(cron_matches(half, at(2026, 11, 1, 2, 30)))
        self.assertFalse(cron_matches(half, at(2026, 3, 8, 1, 30)))

    def test_february_29_matches_only_a_real_leap_day(self):
        leap = parse_cron("0 0 29 2 *")
        self.assertTrue(cron_matches(leap, at(2024, 2, 29, 0, 0)))
        self.assertFalse(cron_matches(leap, at(2023, 2, 28, 0, 0)))
        self.assertFalse(cron_matches(leap, at(2025, 3, 1, 0, 0)))
        self.assertFalse(cron_matches(leap, at(2024, 2, 28, 0, 0)))

    def test_restricted_day_of_month_or_weekday_matches_either(self):
        either = parse_cron("0 0 1 * MON")
        self.assertTrue(cron_matches(either, at(2026, 1, 1, 0, 0)))
        self.assertTrue(cron_matches(either, at(2026, 1, 5, 0, 0)))
        self.assertFalse(cron_matches(either, at(2026, 1, 2, 0, 0)))

    def test_sunday_seven_and_sunday_zero_are_the_same_day(self):
        zero = parse_cron("0 0 * * 0")
        seven = parse_cron("0 0 * * 7")
        sunday = at(2026, 1, 4, 0, 0)
        monday = at(2026, 1, 5, 0, 0)
        self.assertTrue(cron_matches(zero, sunday))
        self.assertTrue(cron_matches(seven, sunday))
        self.assertFalse(cron_matches(zero, monday))
        self.assertFalse(cron_matches(seven, monday))

    def test_invalid_cron_is_rejected(self):
        for expression in (
            "",
            "* * * *",
            "* * * * * *",
            "60 * * * *",
            "*/0 * * * *",
            "5-1 * * * *",
            "FOO * * * *",
            "* * * JAN * extra",
        ):
            with self.assertRaises(Fault) as raised:
                parse_cron(expression)
            self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
            self.assertIn("on.schedule cron is not accepted", str(raised.exception))

    def test_first_observation_records_the_latest_tick_and_does_not_fire(self):
        fire, cursor = schedule_decision(["*/15 * * * *"], None, at(2026, 10, 8, 12, 47))
        self.assertIsNone(fire)
        self.assertEqual(cursor, at(2026, 10, 8, 12, 45))

    def test_a_restart_does_not_fire_the_recorded_tick_again(self):
        recorded = at(2026, 10, 8, 12, 45)
        fire, cursor = schedule_decision(["*/15 * * * *"], recorded, at(2026, 10, 8, 12, 47))
        self.assertIsNone(fire)
        self.assertEqual(cursor, recorded)

    def test_one_catch_up_uses_the_newest_missed_tick(self):
        fire, cursor = schedule_decision(
            ["*/15 * * * *"], at(2026, 10, 8, 12, 0), at(2026, 10, 8, 12, 47)
        )
        self.assertEqual(fire, at(2026, 10, 8, 12, 45))
        self.assertEqual(cursor, fire)
        again, cursor = schedule_decision(["*/15 * * * *"], cursor, at(2026, 10, 8, 12, 47))
        self.assertIsNone(again)
        self.assertEqual(cursor, at(2026, 10, 8, 12, 45))

    def test_the_next_tick_waits_until_it_is_due(self):
        fire, cursor = schedule_decision(
            ["*/15 * * * *"], at(2026, 10, 8, 12, 0), at(2026, 10, 8, 12, 10)
        )
        self.assertIsNone(fire)
        self.assertEqual(cursor, at(2026, 10, 8, 12, 0))
        fire, cursor = schedule_decision(["*/15 * * * *"], cursor, at(2026, 10, 8, 12, 15))
        self.assertEqual(fire, at(2026, 10, 8, 12, 15))


PUSH = {"ref": "refs/heads/main"}
SCHEDULE_ON = {"schedule": [{"cron": "*/5 * * * *"}]}


class ScheduleTriggerTests(unittest.TestCase):
    def test_schedule_submit_matches_only_a_listed_cron(self):
        event = {"schedule": "*/5 * * * *", "ref": "refs/heads/main"}
        self.assertTrue(submission_triggered(SCHEDULE_ON, "schedule", event, None, [], None, False))
        other = {"schedule": "0 0 * * *", "ref": "refs/heads/main"}
        self.assertFalse(
            submission_triggered(SCHEDULE_ON, "schedule", other, None, [], None, False)
        )
        self.assertFalse(
            submission_triggered({"push": None}, "schedule", event, None, [], None, False)
        )

    def test_a_bad_schedule_rejects_the_workflow(self):
        on = {"push": None, "schedule": [{"cron": "60 * * * *"}]}
        with self.assertRaises(Fault) as raised:
            submission_triggered(on, "push", PUSH, None, [], None, False)
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")

    def test_a_timezone_key_is_rejected(self):
        on = {
            "push": None,
            "schedule": [{"cron": "0 9 * * 1-5", "timezone": "America/New_York"}],
        }
        with self.assertRaises(Fault) as raised:
            submission_triggered(on, "push", PUSH, None, [], None, False)
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("timezone", str(raised.exception))

    def test_an_unknown_schedule_key_is_rejected(self):
        on = {"push": None, "schedule": [{"cron": "0 9 * * 1-5", "foo": "1"}]}
        with self.assertRaises(Fault) as raised:
            submission_triggered(on, "push", PUSH, None, [], None, False)
        self.assertEqual(raised.exception.kind, "INVALID_PARAMS")
        self.assertIn("on.schedule key is not accepted", str(raised.exception))

    def test_schedule_on_the_default_branch_gets_push_to_main_secrets(self):
        event = {
            "ref": "refs/heads/main",
            "schedule": "*/5 * * * *",
            "repository": {"default_branch": "main"},
        }
        self.assertTrue(allowlist_matches(event, [], [], "schedule"))
        self.assertTrue(allowlist_matches(event, ["refs/heads/dev"], ["nobody"], "schedule"))
        other = {
            "ref": "refs/heads/dev",
            "schedule": "*/5 * * * *",
            "repository": {"default_branch": "main"},
        }
        self.assertFalse(allowlist_matches(other, [], [], "schedule"))
        pull = {
            "ref": "refs/pull/9/merge",
            "repository": {"default_branch": "main"},
            "pull_request": {"user": {"login": "mona"}},
        }
        self.assertFalse(allowlist_matches(pull, [], [], "pull_request"))
        self.assertFalse(allowlist_matches(pull, [], [], "schedule"))

    def test_workflow_dispatch_on_the_default_branch_gets_push_to_main_secrets(self):
        event = {
            "ref": "refs/heads/main",
            "repository": {"default_branch": "main"},
        }
        self.assertTrue(allowlist_matches(event, [], [], "workflow_dispatch"))
        self.assertTrue(
            allowlist_matches(event, ["refs/heads/dev"], ["nobody"], "workflow_dispatch")
        )
        other = {
            "ref": "refs/heads/dev",
            "repository": {"default_branch": "main"},
        }
        self.assertFalse(allowlist_matches(other, [], [], "workflow_dispatch"))
        self.assertFalse(allowlist_matches(event, [], [], "pull_request"))
