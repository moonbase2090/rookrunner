# Submit a development fixture

Submit accepts a named development fixture, runs it on the local worker, and lets the user read the finished state back with `get`.

## Sub-features

- `submit-accept` returns a `run_id` for a new submission key.
- `submit-finish` reaches state `succeeded` with exit code 0 when the fixture asks for exit code 0.
- `submit-reread` shows that same state on a later `get`.

## How to get to it (user POV)

- Run `submit --backend development --key <key>` against the worker state directory.
- Optional flags `--delay-ms`, `--exit-code`, and `--output` change the fixture. Omit them for exit code 0, no delay, and the text `development fixture completed`.
- Run `get <run_id>` to read the stored result.

## Driving it with the execution_core CLI

Preconditions:

- Launch and Doctor have succeeded for this `STATE`.
- The submission key `verify-${RUN_ID}` has not been used on this worker.

- **Submit.** Run `.venv/bin/python -m execution_core --state "$STATE" submit --backend development --key "verify-${RUN_ID}" --delay-ms 200 --output "verify-fixture-ok" >"$EVIDENCE/submit.json"`. Exit code is 0 and `result.run_id` is present.
- **Wait.** Poll `.venv/bin/python -m execution_core --state "$STATE" get "$RUN_ID_VALUE" >"$EVIDENCE/get.json"` until `result.state` is `succeeded`. Stop if 30 seconds pass.
- **Proof.** `result.exit_code` is 0 and `result.state` is `succeeded`. A second `get` writes the same `run_id`, `state`, and `exit_code`.

## Gotchas

- `--backend development` is required for a fixture. Without it the client exits 2.
- Reusing the same `--key` returns the existing run. It does not start a second fixture.
- `submit` exiting 0 means the worker accepted the fixture. Read `get` before calling the run successful.
- Do not combine fixture flags with `--workflow`. The client rejects that mix.
