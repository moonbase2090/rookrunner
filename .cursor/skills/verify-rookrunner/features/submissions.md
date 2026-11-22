# Close a poll submission that has no final status

Verify `submissions list` and the `poll inspect`, `abandon`, `retry`, and
`repair` recovery commands against an isolated poll state. The recipe does not
contact GitHub. A setup failure that never recorded a commit status is repaired
to `error`.

## Sub-features

- `submissions-list` prints only submissions with no final commit status.
- `submissions-repair` posts `error` for a setup failure and does not post
  again once that status is recorded.
- `submissions-repair-dry-run` decides `would_post` and leaves `poll.json`
  unchanged.
- `submissions-abandon` posts `error`, refuses a successful run, and records
  `failure` when that is the run's final state.
- `submissions-retry` submits one derived successor and repeats as that same
  successor.
- `submissions-cli` accepts `--dry-run` on every command and a live repair
  posts `error`.
- `poll-blocked` calls the worker once for a permanent capability refusal,
  keeps the error fingerprint and unblock metadata, and does not call it again
  while the engine identity is unchanged.
- `poll-abandon-tombstone` requires an operator reason, replaces `poll.json`
  atomically, and prevents rediscovery of that submission key.

## How to get to it (user POV)

From the checkout, with the worker state and the poll state:

`python3 -m execution_core --state STATE submissions list`

`python3 -m execution_core --state STATE submissions inspect KEY`

`python3 -m execution_core --state STATE submissions repair KEY --repository OWNER/NAME --credential-file FILE`

`python3 -m execution_core --state STATE submissions abandon KEY --repository OWNER/NAME --credential-file FILE`

`python3 -m execution_core --state STATE submissions retry KEY`

`python3 -m execution_core --state STATE poll inspect KEY`

`python3 -m execution_core --state STATE poll repair KEY --repository OWNER/NAME --credential-file FILE`

`python3 -m execution_core --state STATE poll abandon KEY --reason "operator decision" --repository OWNER/NAME --credential-file FILE`

`python3 -m execution_core --state STATE poll retry KEY`

Add `--dry-run` to read without posting, submitting, or writing `poll.json`.
`--poll-state` selects the directory that holds `poll.json` when it is not
`--state`.

## Driving it with the execution_core CLI

Preconditions: `uv sync --locked` has been run. `PYTHONPATH` is `src`. No
GitHub token and no live repository are required. The tests build a temporary
poll file and drive `cli.main`.

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest test_poll.PollTests.test_old_poll_state_blocks_a_capability_refusal_after_one_worker_call test_poll.PollTests.test_tested_commit_mismatch_blocks_after_the_first_reporting_pass test_submissions test_status.WorkerStatusTests.test_setup_failure_is_neutral_but_a_workflow_failure_stays_failure test_checks.CliCheckTests.test_setup_failure_posts_a_neutral_check_and_an_error_status -v
```

Require every case to pass. A dry run must leave `poll.json` bytes unchanged
and must not post. A live repair of a setup failure must post `error` once.
Abandon of a succeeded run must post nothing. Retry must submit one run whose
key is `poll-62a441d9f31c6d7aa0b6edc5d12d0c00c9ea3a235b56fae6d9bc82db962bd777`
for the fixture key of `poll-` plus `ab` repeated.

## Gotchas

- `--dry-run` still reads the worker. It does not write `poll.json`.
- A live repair or abandon reads `--credential-file` only when it posts.
- Repair posts `success` only when the worker's final state is `success`.
- Abandon posts `error` and does not post `success`.
- `poll abandon` refuses an omitted or empty `--reason`; legacy
  `submissions abandon` remains accepted without one.
- A capability block is retried only after the worker's engine identity
  changes. A tested-commit mismatch needs `poll retry` or `poll abandon`.
- A successor already recorded for the key is not overwritten by another post.
