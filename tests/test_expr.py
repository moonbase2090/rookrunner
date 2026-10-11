# SPDX-License-Identifier: MPL-2.0

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from execution_core.expr import (
    ExprError,
    check_job_env,
    check_job_if,
    check_job_name,
    check_job_output,
    check_step_if,
    check_step_run,
    check_step_text,
    check_workflow_env,
    evaluate,
    job_is_enabled,
    mentions_context,
    quote_run_expressions,
    render_text,
    step_is_enabled,
)
from execution_core.run import _render_run
from execution_core import expr


def _execute(script, cwd=None):
    completed = []
    for program in ("bash", "sh"):
        completed.append(
            subprocess.run(
                [program, "-c", script],
                capture_output=True,
                text=True,
                check=False,
                cwd=cwd,
            )
        )
    return completed


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


class TextExpressionTests(unittest.TestCase):
    def test_mixed_text_inserts_string_forms(self):
        self.assertEqual(render_text("plain", VALUES), "plain")
        self.assertEqual(render_text("  ${{ true }}  ", VALUES), "true")
        self.assertEqual(render_text("${{ false }}", VALUES), "false")
        self.assertEqual(render_text("${{ null }}", VALUES), "")
        self.assertEqual(render_text("${{ 7 }}", VALUES), "7")
        self.assertEqual(
            render_text("hello ${{ 'Mona' }} ${{ env.MODE }}", VALUES),
            "hello Mona ci",
        )
        self.assertEqual(render_text("echo ${{ 'a}b' }}", VALUES), "echo a}b")
        self.assertEqual(render_text("pre ${{ 'a${{b' }} post", VALUES), "pre a${{b post")
        inserted = dict(VALUES)
        inserted["env"] = {"MODE": "${{ github.sha }}"}
        self.assertEqual(render_text("v=${{ env.MODE }}", inserted), "v=${{ github.sha }}")

    def test_unclosed_and_nested_expressions_are_errors(self):
        for source in ("echo ${{ github.sha", "echo ${{ 1 ${{ 2 }} }}", "${{ }}", "${{   }}"):
            with self.assertRaises(ExprError):
                render_text(source, VALUES)
            with self.assertRaises(ExprError):
                check_step_text(source)

    def test_text_positions_withhold_secrets_status_and_hashfiles(self):
        check_step_text("echo ${{ github.sha }}")
        check_step_text("hello ${{ inputs.who }}")
        with self.assertRaises(ExprError) as raised:
            check_step_text("echo ${{ secrets.TOKEN }}")
        self.assertIn("context is not available: secrets", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_step_text("echo ${{ hashFiles('*.txt') }}")
        self.assertIn("function is not available: hashFiles", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_step_text("echo ${{ success() }}")
        self.assertIn("expression is not accepted", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_job_env("${{ env.LEVEL }}")
        self.assertIn("context is not available: env", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_job_env("${{ secrets.TOKEN }}")
        self.assertIn("context is not available: secrets", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_workflow_env("${{ needs.build.result }}")
        self.assertIn("context is not available: needs", str(raised.exception))
        with self.assertRaises(ExprError) as raised:
            check_job_name("build ${{ secrets.TOKEN }}")
        self.assertIn("context is not available: secrets", str(raised.exception))
        check_job_name("build ${{ matrix.os }}")
        check_workflow_env("${{ github.workflow }}")


class RunQuotingTests(unittest.TestCase):
    def test_an_unquoted_run_value_is_single_quoted(self):
        values = {"env": {"MSG": "; id"}}
        self.assertEqual(
            quote_run_expressions("echo ${{ env.MSG }}", values, "bash"),
            "echo '; id'",
        )
        rendered, secret_env = _render_run("echo ${{ env.MSG }}", values, "bash", None)
        self.assertEqual(rendered, "echo '; id'")
        self.assertEqual(secret_env, {})

    def test_an_unquoted_apostrophe_stays_one_word(self):
        rendered = quote_run_expressions(
            "echo ${{ env.MSG }}",
            {"env": {"MSG": "it's"}},
            None,
        )
        self.assertEqual(rendered, "echo 'it'\\''s'")

    def test_a_double_quoted_run_value_cannot_leave_the_quotes(self):
        rendered = quote_run_expressions(
            'echo "${{ env.MSG }}"',
            {"env": {"MSG": 'a$(id)"`b'}},
            "bash",
        )
        self.assertEqual(rendered, 'echo "a\\$(id)\\"\\`b"')

    def test_a_single_quoted_run_value_escapes_a_quote(self):
        rendered = quote_run_expressions(
            "echo '${{ env.MSG }}'",
            {"env": {"MSG": "it's"}},
            "sh",
        )
        self.assertEqual(rendered, "echo 'it'\\''s'")

    def test_a_heredoc_expression_is_refused(self):
        with self.assertRaises(ExprError) as raised:
            check_step_run("cat <<EOF\n${{ env.MSG }}\nEOF\n", "bash")
        self.assertEqual(str(raised.exception), "run is not accepted")

    def test_another_shell_cannot_quote_a_run_expression(self):
        with self.assertRaises(ExprError) as raised:
            check_step_run("echo ${{ env.MSG }}", "pwsh")
        self.assertEqual(str(raised.exception), "run is not accepted")
        check_step_run("echo hi", "pwsh")

    def test_an_executed_run_value_stays_data(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            ("printf '%s\\n' ${{ env.MSG }}", "printf '%s\\n' '" + payload + "'"),
            (
                "printf '%s\\n' \"${{ env.MSG }}\"",
                "printf '%s\\n' \"\\$(printf INJECTED >&2)\"",
            ),
        )
        for source, expected in scripts:
            for shell in ("bash", "sh"):
                with self.subTest(shell=shell, source=source):
                    rendered = quote_run_expressions(source, values, shell)
                    self.assertEqual(rendered, expected)
                    completed = subprocess.run(
                        [shell, "-c", rendered],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout, payload + "\n")
                    self.assertNotIn("INJECTED", completed.stderr)

    def test_bash_c_is_refused_before_the_child_runs(self):
        source = "bash -c \"printf 'child ran\\n' ${{ env.MSG }}\""
        values = {"env": {"MSG": "$(printf INJECTED >&2)"}}
        with self.assertRaises(ExprError) as raised:
            quote_run_expressions(source, values, "bash")
        self.assertEqual(str(raised.exception), "run is not accepted")
        with self.assertRaises(ExprError) as raised:
            _render_run(source, values, "bash", None)
        self.assertEqual(str(raised.exception), "run is not accepted")
        with self.assertRaises(ExprError) as raised:
            check_step_run(source, None)
        self.assertEqual(str(raised.exception), "run is not accepted")

    def test_eval_and_sh_c_are_refused(self):
        for source in (
            'eval "echo ${{ env.MSG }}"',
            "eval ${{ env.MSG }}",
            "sh -c 'echo ${{ env.MSG }}'",
            'sh -c "echo ${{ env.MSG }}"',
            '/bin/bash -lc "echo ${{ env.MSG }}"',
            'env bash -c "echo ${{ env.MSG }}"',
            'echo $(bash -c "printf %s ${{ env.MSG }}")',
            'echo `bash -c "printf %s ${{ env.MSG }}"`',
        ):
            with self.subTest(source=source):
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")

    def test_an_escaped_eval_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            '\\eval "printf child\\n ${{ env.MSG }}"',
            'e\\val "printf child\\n ${{ env.MSG }}"',
            '\\bash -c "printf child\\n ${{ env.MSG }}"',
            'b\\ash -c "printf child\\n ${{ env.MSG }}"',
            '{ \\eval "printf child\\n ${{ env.MSG }}"; }',
        )
        for source in scripts:
            with self.subTest(source=source):
                rendered = None
                try:
                    rendered = quote_run_expressions(source, values, "bash")
                except ExprError as exc:
                    self.assertEqual(str(exc), "run is not accepted")
                if rendered is not None:
                    for completed in _execute(rendered):
                        self.assertNotIn("INJECTED", completed.stderr)
                        self.assertNotIn("INJECTED", completed.stdout)
                    self.fail("escaped reparsed command was rendered")
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")
        self.assertEqual(
            quote_run_expressions("echo \\eval ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "echo \\eval '; id'",
        )

    def test_a_continued_command_name_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            'e\\\nval "printf child\\n ${{ env.MSG }}"',
            'b\\\nash -c "printf child\\n ${{ env.MSG }}"',
            'bash -\\\nc "printf child\\n ${{ env.MSG }}"',
            'd\\\no eval "printf child\\n ${{ env.MSG }}"',
        )
        for source in scripts:
            with self.subTest(source=source):
                rendered = None
                try:
                    rendered = quote_run_expressions(source, values, "bash")
                except ExprError as exc:
                    self.assertEqual(str(exc), "run is not accepted")
                if rendered is not None:
                    for completed in _execute(rendered):
                        self.assertNotIn("INJECTED", completed.stderr)
                        self.assertNotIn("INJECTED", completed.stdout)
                    self.fail("continued reparsed command was rendered")
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")

    def test_an_expanded_command_name_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            "$'eval' \"printf child\\n ${{ env.MSG }}\"",
            '$"eval" "printf child\\n ${{ env.MSG }}"',
            '$(printf eval) "printf child\\n ${{ env.MSG }}"',
            "$'ev\\x61l' \"printf child\\n ${{ env.MSG }}\"",
            "$'bash' -c \"printf child\\n ${{ env.MSG }}\"",
            'bash $(printf -- -c) "printf child\\n ${{ env.MSG }}"',
        )
        for source in scripts:
            with self.subTest(source=source):
                rendered = None
                try:
                    rendered = quote_run_expressions(source, values, "bash")
                except ExprError as exc:
                    self.assertEqual(str(exc), "run is not accepted")
                if rendered is not None:
                    for completed in _execute(rendered):
                        self.assertNotIn("INJECTED", completed.stderr)
                        self.assertNotIn("INJECTED", completed.stdout)
                    self.fail("expanded command name was rendered")
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")
        self.assertEqual(
            quote_run_expressions("$'echo' ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "$'echo' '; id'",
        )
        self.assertEqual(
            quote_run_expressions(
                "echo $(printf eval) ${{ env.MSG }}",
                {"env": {"MSG": "; id"}},
                "bash",
            ),
            "echo $(printf eval) '; id'",
        )

    def test_a_glob_command_name_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            'e* "printf child\\n ${{ env.MSG }}"',
            '?val "printf child\\n ${{ env.MSG }}"',
            'e[v]al "printf child\\n ${{ env.MSG }}"',
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "eval").write_text("")
            for source in scripts:
                with self.subTest(source=source):
                    rendered = None
                    try:
                        rendered = quote_run_expressions(source, values, "bash")
                    except ExprError as exc:
                        self.assertEqual(str(exc), "run is not accepted")
                    if rendered is not None:
                        for completed in _execute(rendered, directory):
                            self.assertNotIn("INJECTED", completed.stderr)
                            self.assertNotIn("INJECTED", completed.stdout)
                        self.fail("glob command name was rendered")
                    with self.assertRaises(ExprError) as raised:
                        check_step_run(source, "bash")
                    self.assertEqual(str(raised.exception), "run is not accepted")
        self.assertEqual(
            quote_run_expressions("echo * ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "echo * '; id'",
        )
        self.assertEqual(
            quote_run_expressions("'e*' ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "'e*' '; id'",
        )

    def test_a_shell_command_without_c_stays_quoted(self):
        values = {"env": {"MSG": "; id"}}
        self.assertEqual(
            quote_run_expressions("bash script.sh ${{ env.MSG }}", values, "bash"),
            "bash script.sh '; id'",
        )
        self.assertEqual(
            quote_run_expressions("bash ${{ env.MSG }}", values, "bash"),
            "bash '; id'",
        )
        self.assertEqual(
            quote_run_expressions("echo ${{ env.MSG }}\nbash -c 'printf ok'\n", values, "bash"),
            "echo '; id'\nbash -c 'printf ok'\n",
        )
        self.assertEqual(
            quote_run_expressions('echo "${{ env.MSG }} eval"', {"env": {"MSG": "ok"}}, "bash"),
            'echo "ok eval"',
        )

    def test_a_grouped_value_stays_data_when_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        for shell in ("bash", "sh"):
            with self.subTest(shell=shell):
                rendered = quote_run_expressions(
                    "{ printf '%s\\n' \"${{ env.MSG }}\"; }",
                    values,
                    shell,
                )
                completed = subprocess.run(
                    [shell, "-c", rendered],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, payload + "\n")
                self.assertNotIn("INJECTED", completed.stderr)

    def test_eval_after_a_compound_command_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            '{ eval "printf child\\n ${{ env.MSG }}"; }',
            'if true; then eval "printf child\\n ${{ env.MSG }}"; fi',
            'if false; then echo no; else eval "printf child\\n ${{ env.MSG }}"; fi',
            'while false; do eval "printf child\\n ${{ env.MSG }}"; done',
            'for item in one; do eval "printf child\\n ${{ env.MSG }}"; done',
            '! eval "printf child\\n ${{ env.MSG }}"',
            '( eval "printf child\\n ${{ env.MSG }}" )',
        )
        for source in scripts:
            with self.subTest(source=source):
                rendered = None
                try:
                    rendered = quote_run_expressions(source, values, "bash")
                except ExprError as exc:
                    self.assertEqual(str(exc), "run is not accepted")
                if rendered is not None:
                    for completed in _execute(rendered):
                        self.assertNotIn("INJECTED", completed.stderr)
                        self.assertNotIn("INJECTED", completed.stdout)
                    self.fail("reparsed command was rendered")
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")

    def test_a_test_command_and_a_script_file_stay_quoted(self):
        accepted = (
            '[ -n "${{ github.event_name }}" ]',
            'if [ "${{ github.event_name }}" = "pull_request" ]; then echo pr; fi',
            'test "${{ github.event_name }}" = "pull_request"',
            'sh scripts/stamp-version.sh "${{ steps.latest.outputs.tag }}"',
            'bash scripts/stamp-version.sh "${{ steps.latest.outputs.tag }}"',
            './scripts/stamp-version.sh "${{ steps.latest.outputs.tag }}"',
            'if [ "${{ github.event_name }}" = "pull_request" ]; then\n'
            '  base_sha="${{ github.event.pull_request.base.sha }}"\n'
            "fi\n",
        )
        for source in accepted:
            for shell in ("bash", "sh", None):
                with self.subTest(source=source, shell=shell):
                    check_step_run(source, shell)
        self.assertEqual(
            quote_run_expressions(
                '[ -n "${{ env.NAME }}" ]',
                {"env": {"NAME": "pull_request"}},
                "bash",
            ),
            '[ -n "pull_request" ]',
        )
        self.assertEqual(
            quote_run_expressions(
                'sh scripts/stamp-version.sh "${{ env.TAG }}"',
                {"env": {"TAG": "; id"}},
                "sh",
            ),
            'sh scripts/stamp-version.sh "; id"',
        )
        payload = "$(printf INJECTED >&2)"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "stamp.sh"
            script.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
            script.chmod(0o755)
            for shell in ("sh", "bash"):
                with self.subTest(shell=shell):
                    rendered = quote_run_expressions(
                        shell + ' ./stamp.sh "${{ env.MSG }}"',
                        {"env": {"MSG": payload}},
                        shell,
                    )
                    self.assertEqual(
                        rendered,
                        shell + ' ./stamp.sh "\\$(printf INJECTED >&2)"',
                    )
                    completed = subprocess.run(
                        [shell, "-c", rendered],
                        capture_output=True,
                        text=True,
                        check=False,
                        cwd=directory,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout, payload + "\n")
                    self.assertNotIn("INJECTED", completed.stderr)
        for shell in ("bash", "sh"):
            rendered = quote_run_expressions(
                '[ "${{ env.MSG }}" = "pull_request" ]',
                {"env": {"MSG": "pull_request"}},
                shell,
            )
            self.assertEqual(rendered, '[ "pull_request" = "pull_request" ]')
            completed = subprocess.run(
                [shell, "-c", rendered],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")

    def test_a_nested_substitution_keeps_the_value(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            "printf '%s\\n' \"$(printf '%s' ${{ env.MSG }})\"",
            "printf '%s\\n' \"`printf '%s' ${{ env.MSG }}`\"",
            "printf '%s\\n' \"$(printf '%s' \"${{ env.MSG }}\")\"",
            "printf '%s\\n' \"$(printf '%s' '${{ env.MSG }}')\"",
        )
        for source in scripts:
            for shell in ("bash", "sh"):
                with self.subTest(shell=shell, source=source):
                    rendered = quote_run_expressions(source, values, shell)
                    completed = subprocess.run(
                        [shell, "-c", rendered],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout, payload + "\n")
                    self.assertNotIn("INJECTED", completed.stderr)

    def test_a_multiline_value_stays_data(self):
        payload = "line1\n$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            "printf '%s' ${{ env.MSG }}",
            "printf '%s' \"${{ env.MSG }}\"",
            "printf '%s' '${{ env.MSG }}'",
        )
        for source in scripts:
            for shell in ("bash", "sh"):
                with self.subTest(shell=shell, source=source):
                    rendered = quote_run_expressions(source, values, shell)
                    completed = subprocess.run(
                        [shell, "-c", rendered],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout, payload)
                    self.assertNotIn("INJECTED", completed.stderr)

    def test_a_brace_command_name_is_not_executed(self):
        payload = "$(printf INJECTED >&2)"
        values = {"env": {"MSG": payload}}
        scripts = (
            '{eval,echo} "printf child\\n ${{ env.MSG }}"',
            "{echo,eval} ${{ env.MSG }}",
            'e{val,cho} "printf child\\n ${{ env.MSG }}"',
        )
        for source in scripts:
            with self.subTest(source=source):
                rendered = None
                try:
                    rendered = quote_run_expressions(source, values, "bash")
                except ExprError as exc:
                    self.assertEqual(str(exc), "run is not accepted")
                if rendered is not None:
                    for completed in _execute(rendered):
                        self.assertNotIn("INJECTED", completed.stderr)
                        self.assertNotIn("INJECTED", completed.stdout)
                    self.fail("brace command name was rendered")
                with self.assertRaises(ExprError) as raised:
                    check_step_run(source, "bash")
                self.assertEqual(str(raised.exception), "run is not accepted")
        self.assertEqual(
            quote_run_expressions("echo {a,b} ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "echo {a,b} '; id'",
        )
        self.assertEqual(
            quote_run_expressions("'{eval,echo}' ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "'{eval,echo}' '; id'",
        )
        self.assertEqual(
            quote_run_expressions("\\{eval,echo} ${{ env.MSG }}", {"env": {"MSG": "; id"}}, "bash"),
            "\\{eval,echo} '; id'",
        )

    def test_the_scorecard_workflow_run_steps_stay_accepted(self):
        path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "scorecard.yml"
        document = yaml.safe_load(path.read_text())
        runs = [
            step["run"]
            for job in document["jobs"].values()
            for step in job.get("steps") or ()
            if isinstance(step.get("run"), str)
        ]
        self.assertGreaterEqual(len(runs), 3)
        values = {
            "github": {
                "event_name": "pull_request",
                "event": {"pull_request": {"base": {"sha": "abc123"}}},
            }
        }
        checked = 0
        for source in runs:
            check_step_run(source, "bash")
            if "github.event_name" not in source:
                continue
            rendered = quote_run_expressions(source, values, "bash")
            self.assertNotIn("${{", rendered)
            self.assertIn('[ "pull_request" = "pull_request" ]', rendered)
            self.assertIn('base_sha="abc123"', rendered)
            checked += 1
        self.assertEqual(checked, 1)
