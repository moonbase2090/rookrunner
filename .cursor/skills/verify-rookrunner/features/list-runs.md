# List runs

List returns the runs stored on this worker. A state filter limits that list to one state name.

## Sub-features

- `list-includes` includes the `run_id` from a fixture this session submitted.
- `list-filter` with `--filter-state succeeded` includes a succeeded fixture and omits a failed fixture from the same worker.

## How to get to it (user POV)

- Run `list` against the worker state directory.
- Add `--filter-state` with one of `queued`, `running`, `succeeded`, `failed`, `cancelled`, or `lost`.

## Driving it with the execution_core CLI

Preconditions:

- The submit-fixture recipe has produced a succeeded run.
- `RUN_ID_VALUE` is that `result.run_id`.
- `capture` from the skill Evidence section is defined.

- **Add a failed run.** Run `capture list-failed-submit .venv/bin/python -m execution_core --state "$STATE" submit --backend development --key "verify-failed-${RUN_ID}" --exit-code 1 --output "verify-failed"`. `list-failed-submit.exit` is `0`. Poll `capture list-failed-get .venv/bin/python -m execution_core --state "$STATE" get "$FAILED_RUN_ID"` until `list-failed-get.stdout` has `result.state` `failed` and `result.exit_code` `1`. `FAILED_RUN_ID` is `result.run_id` from `list-failed-submit.stdout`.
- **List.** Run `capture list .venv/bin/python -m execution_core --state "$STATE" list`. `list.exit` is `0`.
- **Filter.** Run `capture list-succeeded .venv/bin/python -m execution_core --state "$STATE" list --filter-state succeeded`. `list-succeeded.exit` is `0`.
- **Proof.** `list.stdout` contains both `RUN_ID_VALUE` with state `succeeded` and `FAILED_RUN_ID` with state `failed`. `list-succeeded.stdout` contains `RUN_ID_VALUE` and does not contain `FAILED_RUN_ID`. `list.command` and `list-succeeded.command` record the argv, and both `.stderr` files exist.

## Gotchas

- A succeeded run appearing in the filtered list does not prove omission. The failed run has to be absent from that list and present in the unfiltered list.
- `list` without a filter can include older runs in the same state directory. Match `run_id` instead of assuming the array length is 1.
- An unknown `--filter-state` value is a protocol error. The client exits non-zero.
- `list` does not page unless `--cursor` is sent. A short verification session fits in the default page.
