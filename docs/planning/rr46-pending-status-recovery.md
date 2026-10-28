# Proposed safe pending-status recovery

Status: proposed; owner decision pending.

## Evidence

The worker's ordinary `run.status` path verifies the retained snapshot, then
requires its `base_commit` to equal the caller's `tested_commit`. It records a
status row only after that validation succeeds. The stuck runs have an existing
`pending` row for their exact run, context, and status SHA, but their poll state
is missing and the request identity can differ from the snapshot base.

## Proposed behavior

When poller state is missing, reconciliation can recover the submitted commit
from the worker's persisted request. The worker still verifies the retained
snapshot and requires that commit to equal the manifest base before posting.
An existing pending row does not override a mismatch; it binds only the run,
context, and status SHA.

For runs whose request identity does not match the manifest, this path refuses
to post and does not manufacture a result. The mismatch seen on the six stuck
runs cannot be repaired by this change.

## Open question

Moonbase must decide whether the six mismatched runs should remain pending and
be replaced by fresh submissions, or whether to accept a deliberate design
change that attributes their result to the snapshot base instead.
