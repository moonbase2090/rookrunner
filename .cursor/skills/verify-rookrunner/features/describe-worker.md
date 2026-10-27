# Describe the worker

Describe reports whether the local worker is ready to accept runs, which repository it is bound to, and which capabilities it offers.

## Sub-features

- `describe-ready` reports `ready` true for the worker this run started.
- `describe-repository` reports the same repository path passed to `worker --repository`.
- `describe-fixture` reports the `development.fixture` capability.
- `describe-runner-image` reports `ready` false with a reason when the configured
  runner image is unavailable locally. Startup and every describe inspect the
  pin without pulling, with a 2-second bound. Recovery is visible on describe.

## How to get to it (user POV)

- Run `.venv/bin/python -m execution_core --state <state> describe` while that worker is running.

## Driving it with the execution_core CLI

Preconditions:

- Launch and Doctor have succeeded for this `STATE` and `WORKER_PID`.
- `REPO` is the physical path passed to `worker --repository`.

- **Ask the worker.** Run `capture describe .venv/bin/python -m execution_core --state "$STATE" describe`.
- **Proof.** `describe.exit` is `0`, `describe.command` ends with `describe`, and `describe.stderr` exists. In `describe.stdout`, `result.ready` is true, `result.repository` equals `REPO`, and `result.capabilities` contains `development.fixture`.

## Gotchas

- Exit 0 from `describe` is not enough. Match `repository` to this launch so a different worker is not accepted.
- `describe` does not create a run and does not print logs.
- A worker that has not opened `worker.sock` yet fails the client. Retry for a few seconds after launch, then treat a still-failing doctor as a broken instance.

## Configured image readiness

Use a separate state directory and worker process for this check. Keep the
normal Launch/Doctor run without `--runner-image` as the development-fixture
control. Do not reuse its state.

1. Start the CLI worker with `--runner-image` set to a valid digest pin confirmed
   absent from the selected Docker context. Keep the same repository and private
   state setup as Launch. Do not pull an image for the negative case.
2. Capture `describe`. The command returns a valid response with exit 0, but
   `result.ready` is false and `result.readiness_error` names the unavailable
   runner image. Capture the failed readiness result before calling Doctor.
3. With an available local pin in a separate worker, capture `describe` and
   require `ready` true and a null readiness error. Confirm the returned
   `runner_image` matches that pin. A matching image proves availability only.
4. Stop each owned worker as in Cleanup. Preserve the command and response
   evidence. Do not remove shared images for this check.

The focused automated counterpart uses an external Docker CLI fixture:

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest test_runner_readiness -v
```

It covers missing images, removal/recovery, a mismatched pin, unavailable Docker,
inspection timeouts, scheduler failure, pinned context, and no-image configuration.
