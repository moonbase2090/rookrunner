# Cancel a run

Cancel stops a queued or running development fixture. A later `get` stays `cancelled` and has no exit code.

## Sub-features

- `cancel-running` cancels a fixture that is still running because of `--delay-ms`.
- `cancel-stays` keeps state `cancelled` on a later `get`.

## How to get to it (user POV)

- Submit a fixture with a delay long enough to still be running.
- Run `cancel <run_id>`.
- Run `get <run_id>` again.

## Driving it with the execution_core CLI

Preconditions:

- Launch and Doctor have succeeded for this `STATE`.
- Use a submission key that this worker has not seen, such as `verify-cancel-${RUN_ID}`.

- **Start a slow fixture.** Run `capture cancel-submit .venv/bin/python -m execution_core --state "$STATE" submit --backend development --key "verify-cancel-${RUN_ID}" --delay-ms 5000 --output "verify-cancel"`. `cancel-submit.exit` is `0`.
- **Cancel.** Run `capture cancel .venv/bin/python -m execution_core --state "$STATE" cancel "$RUN_ID_VALUE"`. `cancel.exit` is `0`. In `cancel.stdout`, `result.state` is `cancelled` and `result.exit_code` is null.
- **Proof.** Run `capture cancel-get .venv/bin/python -m execution_core --state "$STATE" get "$RUN_ID_VALUE"`. `cancel-get.exit` is `0`. In `cancel-get.stdout`, `result.state` is still `cancelled` and `result.exit_code` is still null. `cancel.command` and `cancel-get.command` record the argv, and the matching `.stderr` files exist.

## Gotchas

- The CLI sends cancel version 0. Do not add a version flag.
- Cancelling a run that already reached a terminal state returns that terminal record. It does not change a succeeded fixture into `cancelled`.
- Killing the worker process is not a cancel. A killed running fixture is `lost` after the next start, which is a different observation.
