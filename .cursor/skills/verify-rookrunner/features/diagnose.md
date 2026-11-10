# Diagnose skipped runs

`diagnose --since` prints one JSON object from the worker state directory. It counts runs accepted in the window and lists journaled skips and admissions that did not create a run.

## Sub-features

- `diagnose-since` with `--since 1h` includes a decision from this session and reports `cap_bytes` 262144.
- `diagnose-filter` with `--since 1s` after a short wait omits a decision whose timestamp is older than that window.

## How to get to it (user POV)

- Run `diagnose` against the worker state directory.
- Pass `--since` as a duration (`30s`, `5m`, `2h`, `7d`, `1w`) or an aware ISO-8601 timestamp.

## Driving it with the execution_core CLI

Preconditions:

- The baseline worker from the skill Launch section is ready.
- `capture` from the skill Evidence section is defined.
- The worker repository is this checkout, whose `.github/workflows/check.yml` does not list `workflow_dispatch`.

- **Miss the trigger.** Run `capture diagnose-submit .venv/bin/python -m execution_core --state "$STATE" submit --key "verify-diagnose-${RUN_ID}" --workflow .github/workflows/check.yml --job-id check --event '{}' --event-name workflow_dispatch --image rust@sha256:abababababababababababababababababababababababababababababababab`. `diagnose-submit.exit` is `0`. `diagnose-submit.stdout` has `triggered` false and does not have `run_id`.
- **Read the bundle.** Run `capture diagnose .venv/bin/python -m execution_core --state "$STATE" diagnose --since 1h`. `diagnose.exit` is `0`.
- **Proof.** `diagnose.stdout` contains `"reason":"not_triggered"` and `"cap_bytes":262144`. It contains `verify-diagnose-${RUN_ID}` inside `submission_key`. `diagnose.command` records the argv, and `diagnose.stderr` exists.

## Gotchas

- `diagnose` reads the state directory. It does not call the worker socket, so it still prints after the worker process has stopped.
- A duration is one integer and one unit. `2h` is accepted. `2 hours` is not.
- The bundle redacts token-shaped text. A submission key that looks like a token is not a useful marker in the proof.
- An image pin is required for this submit because the baseline worker has no `--runner-image`. The pin is not pulled.
