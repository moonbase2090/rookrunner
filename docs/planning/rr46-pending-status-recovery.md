# Proposed safe pending-status recovery

Status: proposed; owner decision pending.

## Evidence

The worker's ordinary `run.status` path verifies the retained snapshot, then
requires its `base_commit` to equal the caller's `tested_commit`. Legacy
records keep the original request in the worker database. The worker can expose
the request's `event.after` as the submitted commit; for pull requests that is
the merge commit used by the poller.

Read-only checks found that runs `356515ac` and `eb954530` return `retry` with
state `error` because of `SETUP_FAILED` from full storage. The worker's correct
route is a fresh submission after capacity recovers. Run `20f79802` returns
`skip` with state `success`; if GitHub still shows pending, reconciliation can
repost that success using the recovered commit. No status was posted during
these checks.

## Proposed behavior

When poller state is missing, reconciliation recovers the submitted commit
from the worker's persisted request. The worker verifies the retained snapshot
and requires that commit to equal the manifest base before posting. An existing
pending row does not override a mismatch; it binds only the run, context, and
status SHA. If the worker returns `retry`, reconciliation must stop before the
GitHub post and leave the pending status for a fresh run to replace.

For setup failures, this path refuses to post a final status and does not
manufacture a result. The current evidence supports fresh submissions for the
storage-full retries and a success repost for `20f79802` only if its GitHub
status remains pending.

## Open question

Moonbase must route the retry-class runs to fresh submissions and confirm which
remaining stuck runs are eligible for a final status repost.
