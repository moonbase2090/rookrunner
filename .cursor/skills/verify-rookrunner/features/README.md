# Rookrunner verification map

This directory is the maintained source for verifying the user-facing worker CLI. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch one worker with `--network none` and a new `VERIFY_ROOT`, as the skill's Launch section describes.
- Run `uv sync --locked` once in the checkout, then export `PYTHONPATH` as `$PWD/src`.
- Invoke the CLI with `.venv/bin/python`, not a system interpreter that lacks PyYAML.
- Pass that checkout's physical path as `--repository`.
- Run the skill's Doctor section and require `ready` true, the same repository path, and the `development.fixture` capability.
- Never drive a worker this run did not start, and never reuse `.execution-state` from another session.

## Driving conventions

- Start every recipe from the baseline worker unless its preconditions say otherwise.
- Treat every command as literal. Keep flags and the fixture output string unchanged.
- Run actions through `.venv/bin/python -m execution_core --state "$STATE"`.
- Run each client call through `capture` from the skill Evidence section. That stores `name.command`, `name.stdout`, `name.stderr`, and `name.exit`.
- Do not remove proof files during cleanup.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the submit reply.
- CLI proof includes the command, stdout, stderr, and exit code.
- Mutation proof includes a later `get` or `logs` read of the same run.
- Record the feature id and the entry point with every artifact.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with the execution_core CLI` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [Describe the worker](./describe-worker.md) covers readiness of the worker this run started.
- [Submit a development fixture](./submit-fixture.md) covers accepting a fixture and reading its finished result.
- [Read run logs](./read-logs.md) covers the fixture text returned by `logs`.
- [Cancel a run](./cancel-run.md) covers cancelling a queued or running fixture.
- [List runs](./list-runs.md) covers the run list and a state filter.
