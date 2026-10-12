# Describe the worker

Describe reports whether the local worker is ready to accept runs, which repository it is bound to, and which capabilities it offers.

## Sub-features

- `describe-ready` reports `ready` true for the worker this run started.
- `describe-repository` reports the same repository path passed to `worker --repository`.
- `describe-fixture` reports the `development.fixture` capability.

## How to get to it (user POV)

- Run `.venv/bin/python -m execution_core --state <state> describe` while that worker is running.

## Driving it with the execution_core CLI

Preconditions:

- Launch and Doctor have succeeded for this `STATE` and `WORKER_PID`.
- `REPO` is the physical path passed to `worker --repository`.

- **Ask the worker.** Run `.venv/bin/python -m execution_core --state "$STATE" describe >"$EVIDENCE/describe.json"`. Exit code is 0.
- **Proof.** `result.ready` is true, `result.repository` equals `REPO`, and `result.capabilities` contains `development.fixture`.

## Gotchas

- Exit 0 from `describe` is not enough. Match `repository` to this launch so a different worker is not accepted.
- `describe` does not create a run and does not print logs.
- A worker that has not opened `worker.sock` yet fails the client. Retry for a few seconds after launch, then treat a still-failing doctor as a broken instance.
