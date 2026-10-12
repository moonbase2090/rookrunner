# List runs

List returns the runs stored on this worker. A state filter limits that list to one state name.

## Sub-features

- `list-includes` includes the `run_id` from a fixture this session submitted.
- `list-filter` with `--filter-state succeeded` includes a succeeded fixture and omits a run that is not succeeded.

## How to get to it (user POV)

- Run `list` against the worker state directory.
- Add `--filter-state` with one of `queued`, `running`, `succeeded`, `failed`, `cancelled`, or `lost`.

## Driving it with the execution_core CLI

Preconditions:

- The submit-fixture recipe has produced a succeeded run.
- `RUN_ID_VALUE` is that `result.run_id`.

- **List.** Run `.venv/bin/python -m execution_core --state "$STATE" list >"$EVIDENCE/list.json"`. Exit code is 0.
- **Filter.** Run `.venv/bin/python -m execution_core --state "$STATE" list --filter-state succeeded >"$EVIDENCE/list-succeeded.json"`. Exit code is 0.
- **Proof.** Both `result.runs` arrays contain an object whose `run_id` is `RUN_ID_VALUE` and whose `state` is `succeeded`.

## Gotchas

- `list` without a filter can include older runs in the same state directory. Match `run_id` instead of assuming the array length is 1.
- An unknown `--filter-state` value is a protocol error. The client exits non-zero.
- `list` does not page unless `--cursor` is sent. A short verification session fits in the default page.
