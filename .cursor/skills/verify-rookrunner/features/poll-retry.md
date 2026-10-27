# Retry a failed poll submission

Verify the explicit `poll --retry-failed KEY` path with disposable local Git,
HTTP, and worker fixtures. The recipe does not contact GitHub or execute a
workflow container.

## Sub-features

- `poll-retry-cli` creates one successor through the real CLI flag and repeats
  that command without duplicating the run.
- `poll-retry-trust` rechecks push/PR permissions and workflow-edit permission.
- `poll-retry-recovery` resumes the same successor after a lost submit reply.
- `poll-retry-eligibility` refuses successful and active runs and unknown keys.

## How to get to it (user POV)

For a live poll deployment, retain its normal repository, clone, worker/place,
job, image, and posting arguments. Add `--retry-failed KEY`, where KEY is the
failed run's `submission_key`. The source must still be current. Inspect
`submitted` and `skipped`, then follow the returned run to a terminal result.
Do not use a live deployment for this verification recipe.

## Driving it with the execution_core CLI

The fixture owns its temporary repository, worker, HTTP endpoint, synthetic
credential, and socket serving thread. It starts the actual CLI subprocess
with the retry flag and reads the created worker record. The same command is
run twice. Capture the test's command, stdout, stderr, and exit code under a
fresh evidence directory using the skill's `capture` function:

```sh
PYTHONPATH=src:tests capture poll-retry .venv/bin/python -m unittest test_poll_retry -v
```

Require all cases to pass. The CLI case must report one new run on the first
command and no new submission on the second, with two total worker records.
Trust refusals must leave the failed run as the only record. A queued retry
is acceptance evidence only, not a successful workflow result.

## Gotchas

- A successful run cannot be retried. Repeating an ancestor's key never creates
  another successor after that successor finishes.
- To retry a failed successor, name its key explicitly.
- Schedules and historical commits are outside this flag's contract.
- The fixture marks selected records failed to prepare the retry input; it does
  not claim that it executed a failing container workflow.
- Fixture cleanup owns its temporary worker and serving thread. Preserve only
  scrubbed command/test evidence outside those temporary directories.
