import unittest

from execution_core.commands import mask_text, parse_env, parse_output, parse_path, process_stdout


class EnvFileTests(unittest.TestCase):
    def test_assignment_heredoc_and_ignored_names(self):
        text = "\n".join(
            [
                "ACTION_STATE=yellow",
                "MSG<<EOF",
                "one",
                "two",
                "EOF",
                "FOO=a<<b",
                "b",
                "GITHUB_WORKSPACE=/tmp",
                "RUNNER_OS=Windows",
                "NODE_OPTIONS=blocked",
                "ROOKRUNNER_EVENT=/x",
                "CI=true",
                "BAD NAME=no",
                "1NO=no",
                "A-B=no",
                "KEEP=yes",
                "KEEP=later",
                "NUL=a\0b",
                "OPEN<<EOF",
                "consumed=no",
                "",
            ]
        )
        self.assertEqual(
            parse_env(text),
            {"ACTION_STATE": "yellow", "MSG": "one\ntwo", "CI": "true", "KEEP": "later"},
        )
        self.assertEqual(parse_env("NAME=%25%0A\n"), {"NAME": "%25%0A"})
        self.assertEqual(parse_env("OK=1\r\nNEXT=2\r\n"), {"OK": "1", "NEXT": "2"})

    def test_unclosed_heredoc_is_not_stored(self):
        self.assertEqual(parse_env("MSG<<EOF\none\n"), {})

    def test_output_names_allow_a_hyphen(self):
        parsed = parse_output("SELECTED_COLOR=green\nsecret-number=kept\n1no=x\nBAD=a\0b\n")
        self.assertEqual(parsed, {"SELECTED_COLOR": "green", "secret-number": "kept"})

    def test_path_skips_empty_and_nul_lines(self):
        self.assertEqual(parse_path("/a\n\n/b\0\n/c\n"), ["/a", "/c"])


class CommandTests(unittest.TestCase):
    def test_mask_replaces_the_phrase_and_each_word(self):
        masks = []
        log = process_stdout(
            "::add-mask::Mona The Octocat\nMona The Octocat\nMona\n",
            masks,
        )
        self.assertEqual(log, "***\n***\n")
        self.assertEqual(masks, ["Mona The Octocat"])
        self.assertEqual(mask_text("abcd", ["ab", "abcd"]), "***")
        self.assertEqual(mask_text("xxabcdyy", ["ab", "abcd"]), "xx***yy")

    def test_empty_mask_and_earlier_line_stay(self):
        masks = []
        log = process_stdout("secret\n::add-mask::\nsecret\n::add-mask::   \n", masks)
        self.assertEqual(log, "secret\nsecret\n")
        self.assertEqual(masks, [])

    def test_stop_commands_and_disabled_commands(self):
        masks = []
        log = process_stdout(
            "\n".join(
                [
                    "::set-env name=FROM_CMD::nope",
                    "::add-path::/workspace/not-added",
                    "::debug::secret-debug",
                    "::stop-commands::",
                    "::stop-commands::STOP",
                    "::stop-commands::OTHER",
                    "::add-mask::visible-command",
                    "::stop::",
                    "::STOP::",
                    "::add-mask::hidden",
                    "hidden",
                    "::notice file=a,line=1::Missing semicolon",
                    "::set-output name=SELECTED_COLOR::green",
                    "::echo::on",
                    "::group::Title",
                    "inside",
                    "::endgroup::",
                    "",
                ]
            ),
            masks,
        )
        self.assertIn("::stop-commands::OTHER", log)
        self.assertIn("::add-mask::visible-command", log)
        self.assertIn("::stop::", log)
        self.assertIn("Missing semicolon", log)
        self.assertIn("::set-output name=SELECTED_COLOR::green", log)
        self.assertIn("Title", log)
        self.assertIn("inside", log)
        self.assertNotIn("nope", log)
        self.assertNotIn("not-added", log)
        self.assertNotIn("secret-debug", log)
        self.assertNotIn("hidden", log)
        self.assertNotIn("::notice", log)
        self.assertNotIn("::warning", log)
        self.assertNotIn("::echo", log)
        self.assertNotIn("::endgroup::", log)
        self.assertNotIn("::group::", log)
        self.assertEqual(mask_text("hidden", masks), "***")

    def test_resume_token_is_exact_and_an_incomplete_line_is_not_a_command(self):
        masks = []
        self.assertEqual(process_stdout("::add-mask::secret", masks), "::add-mask::secret")
        self.assertEqual(masks, [])
        log = process_stdout("::ADD-MASK::secret\nsecret\n", [])
        self.assertEqual(log, "***\n")
        stopped = []
        literal = process_stdout("::stop-commands::STOP\n::add-mask::visible-command\n", stopped)
        self.assertEqual(literal, "::add-mask::visible-command\n")
        self.assertEqual(stopped, [])

    def test_stderr_uses_masks_registered_from_stdout(self):
        masks = []
        process_stdout("before\n::add-mask::secret\n", masks)
        self.assertEqual(mask_text("secret\n", masks), "***\n")
