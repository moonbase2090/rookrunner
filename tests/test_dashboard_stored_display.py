# SPDX-License-Identifier: MIT

"""Stored log and artifact text must render as data in the dashboard."""

import base64
import unittest

from execution_core.dashboard import Dashboard, _display_stored


class StoredDisplayTests(unittest.TestCase):
    def test_crlf_and_lone_cr_become_single_lf(self):
        self.assertEqual(_display_stored("a\r\nb\rc"), "a\nb\nc")
        self.assertNotIn("\r", _display_stored("line\r\nline\r"))

    def test_escape_and_other_controls_become_caret_text(self):
        self.assertEqual(_display_stored("ok\x1b[2Jhid\x07den\x7f"), "ok^[[2Jhid^Gden^?")
        self.assertNotIn("\x1b", _display_stored("\x1b[0m"))
        self.assertNotIn("\x07", _display_stored("\x07"))

    def test_tab_lf_and_unicode_stay(self):
        text = "café\t日本語\n"
        self.assertEqual(_display_stored(text), text)

    def test_detail_render_keeps_controls_out_of_log_and_artifact_lines(self):
        board = Dashboard.__new__(Dashboard)
        board.unavailable = False
        board.screen = "detail"
        board.detail = {
            "run_id": "run-1",
            "state": "succeeded",
            "exit_code": 0,
            "cleanup": "complete",
            "input": {"snapshot_id": "snap"},
            "steps": [],
        }
        board.log_pages = [_display_stored("head\r\n\x1b[2Jmid\rend")]
        board.log_index = 0
        board.log_end = True
        board.log_cursor = None
        board.artifacts = []
        board.artifact_text = _display_stored("bin\x1b[H\r\n")
        board.artifact_error = None
        rendered = board.render()
        self.assertIn("head\n^[[2Jmid\nend\n", rendered)
        self.assertIn("bin^[[H\n", rendered)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_decode_path_sanitizes_base64_payloads(self):
        toxic = base64.b64encode(b"x\r\n\x1b[1my").decode()
        from execution_core.dashboard import _decode_b64

        self.assertEqual(_decode_b64(toxic), "x\n^[[1my")
