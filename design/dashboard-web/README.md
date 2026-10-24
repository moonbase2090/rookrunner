# Web dashboard option

Status: proposed design option, 2026-10-23. The owner has not chosen a visual
direction. It is a runnable preview for comparison with the terminal client.
`serve.py` opens its own loopback HTTP listener for the preview. Nothing here
adds a worker method, a worker listener or production worker behavior.

It follows the read-only web phase in the proposed
[CI dashboard design](https://github.com/moonbase2090/rookrunner/pull/195).
Cancel, rerun and every other browser write are out of scope.

## What it shows

The page renders one fixture bundle: literal worker JSON-RPC replies to
`status.view`, `run.list`, `run.get`, `run.logs` and `run.artifacts`. The
bundle is the shared comparison fixture
`tests/fixtures/dashboard/phase1.json` on `feature/local-dashboard-options`.
The page reads it unchanged and looks each reply up by method and exact
parameters. A reply the bundle does not hold is shown as not read, never as
empty.

- Runs on one UTC axis. A thin line is the wait from accepted to started; the
  bar is started to finished. A run still going at observation time ends in an
  open edge. In stale mode the span after the last observation is hatched.
- Only `succeeded` with `exit_code` 0 reads as a success. A `succeeded` record
  without exit code 0 reads "Not a success". Each colour travels with its
  verdict text.
- Stale and unavailable observations raise a banner. Unavailable shows no runs.
- Run detail: recorded identities and digests, artifacts, and stored output.
  Output pages follow the `next_cursor` chain and say "end of stored output"
  only when a page reports `end_of_stream`. An empty running page is reported
  as not ended. This worker stores output when a run finishes, so the
  preview makes no streaming claim.
- Log text goes through `textContent`. SGR colours map to classes. Any other
  control byte becomes a visible Control Picture, so log bytes cannot move the
  cursor, clear the view or add markup.
- Find counts literal matches in the loaded text only.

## Run it

```sh
python3 design/dashboard-web/serve.py \
  --bundle ../rookrunner-dashboard-options/tests/fixtures/dashboard/phase1.json
# open http://127.0.0.1:8766/
# --mode stale or --mode unavailable overrides the bundle's own mode
```

The server binds loopback only and refuses any other address. Whether to
allow a LAN bind, which clients may connect, and how they authenticate are
open owner decisions. It answers GET and HEAD and returns 405 for anything
else. It refuses a Host header that is not the loopback address and port,
and sends a Content-Security-Policy that allows only same-origin scripts and
styles. It needs no network access and loads no web fonts.

To compare on the same data, run the terminal comparison client from a
checkout of `feature/local-dashboard-options` with the same bundle and mode:

```sh
PYTHONPATH=src python3 scripts/dashboard-comparison.py --interactive
PYTHONPATH=src python3 scripts/dashboard-comparison.py --mode stale
```

## Checks

`tests/test_dashboard_web.py` covers the preview server (unchanged bundle
bytes, the separate mode override, refused writes, Host check, CSP and the
loopback-only bind) and, through `node`, the model rules: the success
verdict, observation modes, missing versus error replies, the log cursor
chain and the handling of terminal control bytes.
