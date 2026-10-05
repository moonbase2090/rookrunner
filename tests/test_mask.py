"""Job mask for p7-mask.

A fixture value is masked in stdout, across a split read, in an
annotation, and in base64, JSON, and percent-encoded forms. Tests
point at nothing under ~/Secrets and do not contact api.github.com.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from execution_core.checks import check_summary
from execution_core.commands import (
    MaskList,
    LogStream,
    mask_stored_copy,
    mask_text,
    parse_env,
    parse_output,
    process_stdout,
    register_mask,
)
from execution_core.plan import CAPABILITY_VERSION
from execution_core.run import _job_outputs, _masked_command_copies

FIXTURE = 'p7"secret'
TOKEN = "github_pat_FixtureValue"
ROOT = Path(__file__).resolve().parents[1]


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _forms(value):
    """The encoded forms the design names, computed apart from the engine."""

    raw = value.encode("utf-8")
    full = _b64(raw)
    forms = [full]
    trimmed = full.rstrip("=")
    if trimmed != full:
        forms.append(trimmed)
    for offset in (1, 2):
        if len(raw) <= offset:
            continue
        forms.append(_b64(raw[offset:]).rstrip("="))
    encoded = json.dumps(value, ensure_ascii=False)
    forms.append(encoded[1:-1])
    forms.append(quote(value, safe="-._~"))
    return forms


class PrefixTests(unittest.TestCase):
    def test_token_prefixes_mask_the_run_and_leave_the_marker(self):
        self.assertEqual(mask_text("see github_pat_Ab_c now", []), "see *** now")
        self.assertEqual(mask_text("ghp_", []), "ghp_")
        self.assertEqual(mask_text("ghp_ab_cd", []), "***_cd")
        self.assertEqual(mask_text("gho_AA ghu_BB ghs_CC ghr_DD", []), "*** *** *** ***")
        self.assertEqual(mask_text("not a token", []), "not a token")
        self.assertEqual(mask_text(TOKEN, []), "***")

    def test_capability_stays_12_and_check_yml_is_unchanged(self):
        self.assertEqual(CAPABILITY_VERSION, 12)
        text = (ROOT / ".github" / "workflows" / "check.yml").read_text()
        self.assertNotIn("github_pat_", text)
        self.assertNotIn("--secrets", text)
        self.assertNotIn(FIXTURE, text)


class EncodedFormTests(unittest.TestCase):
    def test_stdout_masks_the_fixture_and_its_encoded_forms(self):
        masks = []
        lines = [f"::add-mask::{FIXTURE}", FIXTURE]
        for form in _forms(FIXTURE):
            self.assertNotEqual(form, FIXTURE)
            lines.append(form)
        log = process_stdout("\n".join(lines) + "\n", masks)
        self.assertNotIn(FIXTURE, log)
        for form in _forms(FIXTURE):
            self.assertGreaterEqual(len(form), 4)
            self.assertNotIn(form, log)
        self.assertIn("***", log)
        self.assertIn(FIXTURE, masks)

    def test_a_short_encoded_form_is_not_registered(self):
        masks = MaskList()
        register_mask(masks, "xy")
        padded = _b64(b"xy")
        trimmed = padded.rstrip("=")
        self.assertGreaterEqual(len(padded), 4)
        self.assertLess(len(trimmed), 4)
        self.assertEqual(mask_text(padded, masks), "***")
        self.assertEqual(mask_text(trimmed, masks), trimmed)
        self.assertEqual(mask_text("xy", masks), "***")
        self.assertEqual(masks.warnings, ["registered value is shorter than 4 characters"])
        self.assertNotIn("xy", "".join(masks.warnings))


class StreamTests(unittest.TestCase):
    def test_a_split_read_of_one_line_is_masked_before_it_is_stored(self):
        masks = MaskList()
        register_mask(masks, FIXTURE)
        stream = LogStream(masks)
        head, tail = FIXTURE[:4], FIXTURE[4:]
        self.assertEqual(stream.feed("pre " + head), "")
        self.assertEqual(stream.held, "pre " + head)
        emitted = stream.feed(tail + " post\n")
        self.assertEqual(emitted, "pre *** post\n")
        self.assertEqual(stream.finish(), "")
        self.assertEqual(stream.held, "")
        self.assertNotIn(FIXTURE, emitted)
        whole = LogStream(masks)
        self.assertEqual(whole.feed("zz" + FIXTURE + "yy"), "")
        self.assertGreaterEqual(len(whole.held), len(FIXTURE))
        self.assertEqual(whole.feed("\n"), "zz***yy\n")

    def test_a_value_split_across_two_lines_is_not_joined(self):
        masks = MaskList()
        register_mask(masks, FIXTURE)
        stream = LogStream(masks)
        first = stream.feed(FIXTURE[:4] + "\n")
        second = stream.feed(FIXTURE[4:] + "\n")
        self.assertIn(FIXTURE[:4], first)
        self.assertIn(FIXTURE[4:], second)
        self.assertNotIn("***", first + second)

    def test_annotation_text_is_masked(self):
        masks = []
        log = process_stdout(
            f"::add-mask::{FIXTURE}\n::error::boom {FIXTURE}\n::warning title=x::{FIXTURE}\n",
            masks,
        )
        self.assertNotIn(FIXTURE, log)
        self.assertNotIn("::error", log)
        self.assertNotIn("::warning", log)
        self.assertIn("***", log)

    def test_an_add_mask_split_across_reads_registers_before_the_next_line(self):
        masks = MaskList()
        stream = LogStream(masks)
        self.assertEqual(stream.feed("::add-mask::" + FIXTURE[:4]), "")
        self.assertEqual(masks, [])
        self.assertEqual(stream.feed(FIXTURE[4:] + "\n" + FIXTURE + "\n"), "***\n")
        self.assertIn(FIXTURE, masks)


class WarningTests(unittest.TestCase):
    def test_a_short_secret_warns_by_name_and_is_still_masked(self):
        masks = MaskList()
        register_mask(masks, "xy", secret=True, name="NPM_TOKEN")
        self.assertEqual(masks.warnings, ["secret NPM_TOKEN is shorter than 4 characters"])
        self.assertNotIn("xy", "".join(masks.warnings))
        self.assertEqual(mask_text("xy", masks), "***")
        self.assertIn("xy", masks.secrets)

    def test_a_short_word_warns_without_the_word(self):
        masks = MaskList()
        register_mask(masks, "Mona The Octocat")
        self.assertEqual(masks.warnings, ["registered value is shorter than 4 characters"])
        self.assertNotIn("The", "".join(masks.warnings))
        self.assertNotIn("Mona", "".join(masks.warnings))
        self.assertEqual(masks.secrets, [])


class OutputTests(unittest.TestCase):
    def test_a_job_output_with_a_secret_or_prefix_is_omitted(self):
        masks = MaskList()
        register_mask(masks, FIXTURE, secret=True, name="TOKEN")
        register_mask(masks, "mask-token")
        produced, _used = _job_outputs(
            {
                "outputs": {
                    "leaked": "'p7\"secret'",
                    "prefixed": "'github_pat_Abcdef'",
                    "masked": "'see mask-token'",
                    "plain": "'local'",
                }
            },
            {},
            0,
            masks,
        )
        self.assertNotIn("leaked", produced)
        self.assertNotIn("prefixed", produced)
        self.assertEqual(produced["masked"], "see ***")
        self.assertEqual(produced["plain"], "local")
        self.assertEqual(masks.omissions, ["leaked", "prefixed"])
        recorded = " ".join(masks.warnings)
        self.assertIn("output leaked omitted", recorded)
        self.assertIn("output prefixed omitted", recorded)
        self.assertNotIn(FIXTURE, recorded)
        self.assertNotIn("github_pat_", recorded)
        self.assertNotIn("mask-token", recorded)

    def test_stored_copies_are_masked_and_live_files_stay_raw(self):
        masks = MaskList()
        register_mask(masks, FIXTURE, secret=True, name="TOKEN")
        raw = f"TOKEN={FIXTURE}\n"
        self.assertEqual(parse_env(raw)["TOKEN"], FIXTURE)
        self.assertEqual(parse_output(raw)["TOKEN"], FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for label in ("env", "output", "state"):
                path = root / label
                path.write_text(raw)
                files[label] = path
            copies = _masked_command_copies(files, masks)
            for label in ("env", "output", "state"):
                self.assertEqual(files[label].read_text(), raw)
                self.assertNotIn(FIXTURE, copies[label])
                self.assertIn("***", copies[label])
        summary = mask_stored_copy(f"summary {FIXTURE}\n", masks)
        self.assertNotIn(FIXTURE, summary)
        self.assertIn("***", summary)
        self.assertEqual(parse_env(raw)["TOKEN"], FIXTURE)


class CheckSummaryTests(unittest.TestCase):
    def test_a_stored_check_summary_masks_a_token_prefix(self):
        self.assertEqual(check_summary("succeeded", 0), "succeeded exit_code 0")
        self.assertEqual(check_summary("queued", None), "queued")
        self.assertEqual(check_summary(TOKEN, None), "***")
        self.assertNotIn(TOKEN, check_summary(TOKEN, None))


if __name__ == "__main__":
    unittest.main()
