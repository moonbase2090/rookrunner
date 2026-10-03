import re
import unittest
from pathlib import Path

from execution_core.expr import (
    ExprError,
    check_job_if,
    check_job_output,
    check_step_if,
    evaluate,
    job_is_enabled,
    mentions_context,
    step_is_enabled,
)
from execution_core import expr


VALUES = {
    "github": {
        "event": {
            "kind": "local",
            "n": 1,
            "labels": [{"name": "bug"}, {"name": "docs"}],
        }
    },
    "needs": {},
    "strategy": {},
    "matrix": {},
    "job": {"status": "success"},
    "runner": {"os": "Linux"},
    "env": {"MODE": "ci"},
    "vars": {},
    "steps": {},
    "inputs": {},
}


class ExpressionTests(unittest.TestCase):
    def test_source_does_not_call_eval(self):
        source = Path(expr.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?<![\w])eval\s*\(", source))
        self.assertIsNone(re.search(r"(?<![\w])exec\s*\(", source))

    def test_literals_operators_and_coercion(self):
        self.assertIs(evaluate("true", VALUES), True)
        self.assertIs(evaluate("${{ false }}", VALUES), False)
        self.assertIsNone(evaluate("null", VALUES))
        self.assertEqual(evaluate("711", VALUES), 711)
        self.assertEqual(evaluate("0xff", VALUES), 255)
        self.assertEqual(evaluate("-2.99e-2", VALUES), -2.99e-2)
        self.assertEqual(evaluate("'It''s'", VALUES), "It's")
        self.assertTrue(evaluate("!false && true", VALUES))
        self.assertFalse(evaluate("false || true && false", VALUES))
        self.assertTrue(evaluate("(false || true) && true", VALUES))
        self.assertTrue(evaluate("'ABC' == 'abc'", VALUES))
        self.assertTrue(evaluate("1 == '1'", VALUES))
        self.assertTrue(evaluate("'' == null", VALUES))
        self.assertTrue(evaluate("true == 1", VALUES))
        self.assertFalse(evaluate("1 > 'nope'", VALUES))
        self.assertFalse(evaluate("1 == 'nope'", VALUES))
        self.assertTrue(evaluate("1 != 'nope'", VALUES))
        self.assertEqual(evaluate("github.sha", VALUES), "")
        self.assertEqual(evaluate("github.event.missing.child", VALUES), "")
        self.assertEqual(evaluate("github['event']['kind']", VALUES), "local")
        self.assertTrue(evaluate("github.event.kind == 'LOCAL'", VALUES))
        self.assertTrue(evaluate("github.event.n == 1", VALUES))
        self.assertEqual(evaluate("env.MISSING", VALUES), "")
        self.assertEqual(evaluate("env.MODE", VALUES), "ci")

    def test_functions_and_filters(self):
        self.assertTrue(evaluate("contains('Hello world', 'llo')", VALUES))
        self.assertTrue(evaluate("contains('Hello', 'LLO')", VALUES))
        self.assertTrue(evaluate("startsWith('Hello world', 'he')", VALUES))
        self.assertTrue(evaluate("endsWith('Hello world', 'LD')", VALUES))
        self.assertEqual(
            evaluate("format('Hello {0} {1}', 'Mona', 'Octocat')", VALUES),
            "Hello Mona Octocat",
        )
        self.assertEqual(
            evaluate("format('{{Hello {0}!}}', 'Mona')", VALUES),
            "{Hello Mona!}",
        )
        self.assertEqual(evaluate("join(fromJSON('[\"a\",\"b\"]'), ', ')", VALUES), "a, b")
        self.assertEqual(evaluate("join('ab', '-')", VALUES), "ab")
        self.assertEqual(
            evaluate("fromJSON(toJSON(github.event.n))", VALUES),
            1,
        )
        self.assertFalse(evaluate("fromJSON('{}') == fromJSON('{}')", VALUES))
        self.assertTrue(evaluate("github.event == github.event", VALUES))
        self.assertTrue(evaluate("contains(github.event.labels.*.name, 'bug')", VALUES))
        self.assertEqual(evaluate("case(false, 'a', true, 'b', 'c')", VALUES), "b")
        self.assertEqual(evaluate("case(false, fromJSON('['), 'ok')", VALUES), "ok")
        self.assertFalse(evaluate("false && fromJSON('[')", VALUES))
        self.assertTrue(evaluate("true || fromJSON('[')", VALUES))

    def test_unavailable_context_is_an_error(self):
        with self.assertRaises(ExprError) as raised:
            check_step_if("secrets.NAME")
        self.assertIn("context is not available: secrets", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_step_if("hashFiles('*.txt')")
        self.assertIn("function is not available: hashFiles", str(raised.exception))
        with self.assertRaises(ExprError):
            evaluate("__import__('os')", VALUES)
        with self.assertRaises(ExprError):
            check_step_if('"double"')

    def test_step_if_uses_status_functions(self):
        failed = [{"status": "failed", "id": "demo"}]
        skipped = [{"status": "skipped", "id": "skip"}]
        self.assertTrue(step_is_enabled(None, VALUES, [], False))
        self.assertTrue(step_is_enabled("success()", VALUES, [], False))
        self.assertFalse(step_is_enabled("false", VALUES, [], False))
        self.assertFalse(step_is_enabled("github.event.n == 1", VALUES, failed, False))
        self.assertTrue(step_is_enabled("failure()", VALUES, failed, False))
        self.assertTrue(step_is_enabled("always()", VALUES, failed, False))
        self.assertTrue(step_is_enabled("cancelled()", VALUES, [], True))
        self.assertFalse(step_is_enabled("cancelled()", VALUES, [], False))
        self.assertTrue(step_is_enabled("success()", VALUES, skipped, False))

    def test_hyphen_is_part_of_the_property_name(self):
        values = dict(VALUES)
        values["env"] = {"MY-VAR": "kept", "MY": "no"}
        self.assertEqual(evaluate("env.MY-VAR", values), "kept")
        self.assertEqual(evaluate("env['MY-VAR']", values), "kept")
        with self.assertRaises(ExprError):
            evaluate("1-1", values)

    def test_case_returns_the_first_true_branch(self):
        self.assertEqual(
            evaluate("case(true, 'first', false, fromJSON('['), 'no')", VALUES), "first"
        )
        self.assertEqual(evaluate("case(false, 'a', true, 'b', 'c')", VALUES), "b")

    def test_job_if_status_follows_needs(self):
        values = dict(VALUES)
        values["needs"] = {"one": {"result": "success", "outputs": {"kind": "local"}}}
        self.assertTrue(job_is_enabled(None, VALUES, [], False, False))
        self.assertFalse(job_is_enabled(None, VALUES, ["failure"], True, False))
        self.assertFalse(job_is_enabled(None, VALUES, ["skipped"], False, False))
        self.assertTrue(job_is_enabled("always()", VALUES, ["failure"], True, False))
        self.assertTrue(job_is_enabled("failure()", VALUES, ["skipped"], True, False))
        self.assertFalse(job_is_enabled("failure()", VALUES, ["skipped"], False, False))
        self.assertTrue(job_is_enabled("success()", values, ["success"], False, False))
        self.assertTrue(
            job_is_enabled("needs.one.outputs.kind == 'local'", values, ["success"], False, False)
        )
        self.assertFalse(
            job_is_enabled("needs.one.outputs.kind == 'local'", values, ["failure"], True, False)
        )
        self.assertTrue(job_is_enabled("cancelled()", VALUES, [], False, True))
        self.assertFalse(job_is_enabled("cancelled()", VALUES, [], False, False))

    def test_secrets_mention_includes_an_untaken_branch(self):
        self.assertTrue(mentions_context("false && secrets.TOKEN", "secrets"))
        self.assertFalse(mentions_context("github.event.kind", "secrets"))
        check_job_output("secrets.TOKEN")
        check_job_output("case(true, 'kept', 'other')")
        with self.assertRaises(ExprError) as raised:
            check_job_if("secrets.TOKEN")
        self.assertIn("context is not available: secrets", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_job_output("success()")
        self.assertIn("expression is not accepted", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_job_output("hashFiles('*.txt')")
        self.assertIn("function is not available: hashFiles", str(raised.exception))
