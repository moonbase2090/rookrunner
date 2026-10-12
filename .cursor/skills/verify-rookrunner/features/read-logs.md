# Read run logs

Logs returns the fixture's output bytes for a run the user already submitted. The bytes are base64 in the JSON result.

## Sub-features

- `logs-text` returns the `--output` text after the fixture succeeds.
- `logs-end` reports `end_of_stream` true once the run is terminal and the page covers the log.

## How to get to it (user POV)

- Run `logs <run_id>` after `submit` and `get`.
- Optional `--limit` and `--cursor` page the same log. The first page omits `--cursor`.

## Driving it with the execution_core CLI

Preconditions:

- The submit-fixture recipe has produced a succeeded run whose `--output` was `verify-fixture-ok`.
- `RUN_ID_VALUE` is that `result.run_id`.

- **Read the log.** Run `capture logs .venv/bin/python -m execution_core --state "$STATE" logs "$RUN_ID_VALUE" --limit 1024`.
- **Proof.** `logs.exit` is `0`, `logs.command` lists the argv, and `logs.stderr` exists. Decode `result.data_base64` from `logs.stdout` as UTF-8. The text is `verify-fixture-ok`. `result.end_of_stream` is true.

## Gotchas

- The log is not plain JSON text. Compare the decoded bytes with the `--output` value.
- An empty `data_base64` page can appear before the fixture finishes. Read `logs` only after `get` reports a terminal state.
- `follow <run_id>` prints status and log pages and exits 0 only for `succeeded` with exit code 0. A successful `logs` call still exits 0 when the fixture's own exit code is non-zero.
