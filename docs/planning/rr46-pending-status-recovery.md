# Proposed pending-status recovery

Status: proposed; owner decision pending.

## Evidence

The worker's ordinary `run.status` path verifies the retained snapshot, then
requires its `base_commit` to equal the caller's `tested_commit`. It records a
status row only after that validation succeeds. The stuck runs have an existing
`pending` row for their exact run, context, and status SHA, but their poll state
is missing and the request identity can differ from the snapshot base.

## Proposed behavior

Add an explicit recovery mode for reconciliation. It may operate only on a
terminal workflow run with a matching `pending` row in the worker's status
history. It must verify the retained snapshot digest, cleanliness, and empty
included-file list again. It derives the tested commit from the verified
manifest and posts the worker's current final state for the status SHA already
bound to the pending row. The normal `run.status` path continues to require an
explicit tested commit that matches the snapshot.

The pending row is the authorization for the recovery path: the worker only
records it after the normal path has validated the snapshot and tested commit.
The recovery path does not authorize a new run, context, or status SHA, and it
does not manufacture a successful result.

## Open question

Moonbase must decide whether the existing pending row is sufficient evidence
for this narrowly scoped recovery path or whether these old statuses should
remain pending and be replaced by fresh submissions.
