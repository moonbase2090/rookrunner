# SPDX-License-Identifier: MIT

import http.client
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

OPTION = Path(__file__).resolve().parent.parent / "design" / "dashboard-web"


def _serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_web_serve", OPTION / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreviewServerTest(unittest.TestCase):
    def setUp(self):
        self.serve = _serve_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "bundle.json"
        # Deliberately unusual spacing: the server must not re-encode it.
        self.bundle.write_bytes(b'{"mode":  "current",\n "responses": []}\n')
        self.server = self.serve.build(self.bundle, port=0, mode="stale")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        reply = conn.getresponse()
        body = reply.read()
        conn.close()
        return reply, body

    def test_bundle_bytes_are_served_unchanged(self):
        reply, body = self.request("GET", "/bundle.json")
        self.assertEqual(reply.status, 200)
        self.assertEqual(body, b'{"mode":  "current",\n "responses": []}\n')

    def test_mode_override_is_separate_from_bundle(self):
        reply, body = self.request("GET", "/observation.json")
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(body), {"mode": "stale"})

    def test_writes_are_refused(self):
        for verb in ("POST", "PUT", "DELETE"):
            reply, _ = self.request(verb, "/bundle.json")
            self.assertEqual(reply.status, 405, verb)
            self.assertEqual(reply.getheader("Allow"), "GET, HEAD")

    def test_foreign_host_header_is_refused(self):
        reply, body = self.request("GET", "/bundle.json", host=f"attacker.example:{self.port}")
        self.assertEqual(reply.status, 421)
        self.assertEqual(body, b"")

    def test_unknown_path_is_not_found(self):
        reply, _ = self.request("GET", "/serve.py")
        self.assertEqual(reply.status, 404)

    def test_page_forbids_inline_and_foreign_script(self):
        reply, _ = self.request("GET", "/")
        policy = reply.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self';", policy)
        self.assertIn("default-src 'none';", policy)
        self.assertNotIn("unsafe-inline", policy)

    def test_non_loopback_bind_is_refused(self):
        for host in ("0.0.0.0", "192.168.1.20", "example.com"):
            with self.assertRaises(ValueError, msg=host):
                self.serve.build(self.bundle, host=host, port=0)


MODEL_PROBE = r"""
const m = await import(process.argv[1]);
const b64 = (s) => Buffer.from(s, "utf8").toString("base64");
const ok = (result) => ({ jsonrpc: "2.0", id: 1, result });
const bundle = {
  mode: "current",
  observed_at: "2026-10-23T12:00:00Z",
  responses: [
    { method: "run.get", params: { run_id: "a" }, reply: ok({ run_id: "a", state: "failed", exit_code: 2 }) },
    { method: "run.get", params: { run_id: "gone" },
      reply: { jsonrpc: "2.0", id: 1, error: { code: -32000, message: "no run", data: { kind: "RUN_NOT_FOUND" } } } },
    { method: "run.logs", params: { run_id: "a" },
      reply: ok({ data_base64: b64("one "), next_cursor: "c2", end_of_stream: false }) },
    { method: "run.logs", params: { cursor: "c2", run_id: "a" },
      reply: ok({ data_base64: b64("two"), next_cursor: "c3", end_of_stream: true }) },
    { method: "run.logs", params: { run_id: "u" },
      reply: ok({ data_base64: Buffer.from([0x63, 0x61, 0x66, 0xc3]).toString("base64"), next_cursor: "u2", end_of_stream: false }) },
    { method: "run.logs", params: { run_id: "u", cursor: "u2" },
      reply: ok({ data_base64: Buffer.from([0xa9]).toString("base64"), next_cursor: null, end_of_stream: true }) },
    { method: "run.logs", params: { run_id: "bad" },
      reply: ok({ data_base64: Buffer.from([0x63, 0xc3]).toString("base64"), next_cursor: null, end_of_stream: true }) },
    { method: "run.logs", params: { run_id: "r" },
      reply: ok({ data_base64: "", next_cursor: "r2", end_of_stream: false }) },
    { method: "run.logs", params: { run_id: "r", cursor: "r2" },
      reply: ok({ data_base64: "", next_cursor: "r2", end_of_stream: false }) },
  ],
};
const v = (record) => { const x = m.verdict(record); return [x.key, x.success, x.text]; };
const logs = (id) => { const x = m.logPages(bundle, id);
  return [x.pages.length, x.ended, m.pagesText(x.pages)]; };
console.log(JSON.stringify({
  verdicts: [
    v({ state: "succeeded", exit_code: 0 }),
    v({ state: "succeeded", exit_code: null }),
    v({ state: "succeeded", exit_code: 1 }),
    v({ state: "succeeded", exit_code: "0" }),
    v({ state: "failed", exit_code: 1 }),
    v({ state: "lost", cleanup: "unresolved" }),
    v({ state: "running" }),
    v({}),
  ],
  observations: [
    m.observation({ mode: "current", observed_at: "t" }).current,
    m.observation({ observed_at: "t" }).current,
    m.observation({ mode: "stale", observed_at: "t", last_observed_at: "s" }),
    m.observation({ mode: "current", observed_at: "t" }, "unavailable").current,
  ],
  calls: [
    m.call(bundle, "run.get", { run_id: "a" }).result.state,
    m.call(bundle, "run.get", { run_id: "zzz" }),
    m.errorText(m.call(bundle, "run.get", { run_id: "gone" }).error),
  ],
  logs: [logs("a"), logs("r"), logs("absent")],
  split: [logs("u"), m.countMatches(m.pagesText(m.logPages(bundle, "u").pages), "\u00e9"), logs("bad")],
  terminal: [
    m.terminalRuns("\x1b[1;31mbad\x1b[0m ok"),
    m.terminalRuns("a\x1b[2Jb\x07c\r\nd\re"),
  ],
  matches: [m.countMatches("aaaa", "aa"), m.countMatches("abc", ""), m.countMatches("x.y", ".")],
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ModelRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(
            ["node", "--input-type=module", "-e", MODEL_PROBE, (OPTION / "model.js").as_uri()],
            capture_output=True,
            text=True,
            check=True,
        )
        cls.probe = json.loads(out.stdout)

    def test_only_succeeded_with_exit_zero_is_success(self):
        self.assertEqual(
            self.probe["verdicts"],
            [
                ["succeeded", True, "Succeeded, exit 0"],
                ["malformed", False, "Not a success: succeeded with no exit code"],
                ["malformed", False, "Not a success: succeeded with exit 1"],
                ["malformed", False, "Not a success: succeeded with no exit code"],
                ["failed", False, "Failed, exit 1"],
                ["lost", False, "Lost, cleanup unresolved"],
                ["running", False, "Running"],
                ["unknown", False, "Unknown state"],
            ],
        )

    def test_only_an_explicit_current_mode_is_current(self):
        current, unmarked, stale, overridden = self.probe["observations"]
        self.assertIs(current, True)
        self.assertIs(unmarked, False)
        self.assertEqual(stale, {"mode": "stale", "at": "t", "last": "s", "current": False})
        self.assertIs(overridden, False)

    def test_missing_reply_is_not_an_empty_result(self):
        state, missing, error = self.probe["calls"]
        self.assertEqual(state, "failed")
        self.assertEqual(missing, {"missing": True})
        self.assertEqual(error, "RUN_NOT_FOUND: no run")

    def test_log_chain_ends_only_on_end_of_stream(self):
        ended, empty_running, absent = self.probe["logs"]
        self.assertEqual(ended, [2, True, "one two"])
        # Empty pages with end_of_stream false never read as finished, and a
        # repeated cursor stops the walk instead of looping.
        self.assertEqual(empty_running, [2, False, ""])
        self.assertEqual(absent, [0, False, ""])

    def test_character_split_across_pages_decodes_once(self):
        # Page one ends with the first byte of U+00E9, page two holds the second.
        text, matches, truncated = self.probe["split"]
        self.assertEqual(text, [2, True, "caf\u00e9"])
        self.assertEqual(matches, 1)
        # A stream that ends inside a character shows one replacement mark.
        self.assertEqual(truncated, [1, True, "c\ufffd"])

    def test_terminal_bytes_cannot_control_the_view(self):
        styled, controls = self.probe["terminal"]
        self.assertEqual(
            styled,
            [
                {"text": "bad", "fg": "red", "bold": True},
                {"text": " ok", "fg": None, "bold": False},
            ],
        )
        self.assertEqual(
            controls,
            [{"text": "a␛[2Jb␇c\nd␍e", "fg": None, "bold": False}],
        )

    def test_literal_find_counts_non_overlapping_matches(self):
        self.assertEqual(self.probe["matches"], [2, 0, 1])


if __name__ == "__main__":
    unittest.main()
