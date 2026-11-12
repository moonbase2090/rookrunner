# Snapshot reporting and retention

Status: implemented in this tree. Operational cleanup of deployed poll states
and removal of host-specific retention overrides are separate operator steps.

## Evidence lifetime

A workflow snapshot remains protected until a terminal commit status is
recorded or the worker records a missing-evidence disposition. Queued, running,
lost, and unresolved-cleanup runs remain protected. Shared snapshots stay while
any protected run still references them.

`run.status` normally verifies the snapshot digest and tested commit before
reporting a result. If that evidence is permanently unavailable, a caller can
request `dispose: "snapshot_unreadable"` with the run id, tested commit, status
SHA, and context. The worker accepts this only for succeeded, failed, or
cancelled workflow runs whose stored submitted commit matches and whose
snapshot cannot be verified. The response is `action: "disposed", state:
"error"`. A disposal is idempotent and survives restart. It does not claim a
GitHub status was posted, and it cannot manufacture a successful result.
Later status decisions still return that disposal if snapshot files return.

The worker stores dispositions separately from `status_posts`. Retention and
`storage-plan --dry-run` consider both. Pruning still requires confirmed
execution cleanup and follows the documented capacity policy: 90 days or the
newest 100 finished folders, with older eligible folders reclaimed first when
the disk budget requires room. Increasing that count to 500 does not repair
missing evidence or final reporting.

Setup failures with readable evidence keep the existing final commit status
`error`, including runs on heads that have since moved. A successor that owns
the same SHA and context prevents an older result from replacing it.

## Poll-state migration

The first poll migrates at most 100 legacy submission references before
ordinary terminal reporting. New references carry reporting version 1. The
poll-state envelope remains version 1; `reporting_migration` records its own
version, key cursor, processed count, and completion flag.

Each batch inspects entries in key order. A safely terminal run with unreadable
evidence first receives its worker-side disposition, then moves to the poll
state's `completed` mapping with `reporting_disposition: "snapshot_unreadable"`.
The tombstones and migration cursor commit together through the existing
atomic poll-state checkpoint. If execution stops after worker disposal and
before that checkpoint, the next attempt repeats the same idempotent disposal.
The run key remains retained, so rediscovery does not submit it again.

Queued, running, lost, unavailable, and foreign-worker entries remain intact.
Ordinary reporting proceeds only for entries the bounded migration has visited
and for new entries. Later polls continue the migration until complete. A
normal terminal run whose snapshot becomes unreadable after migration uses
the same worker disposition and poll tombstone path.

## Operator cleanup

Take a backup of `poll.json` outside version control. Install the new worker
and poller code before applying the migration. A dry run reads the worker but
posts nothing to GitHub, submits no execution, records no disposition, and
writes no poll state:

```sh
python -m execution_core --state "$WORKER_STATE" submissions migrate \
  --poll-state "$POLL_STATE" --batch-size 100 --dry-run
```

For a worker reached over SSH, pass `--ssh-host "$WORKER_HOST"` and
`--remote-state "$REMOTE_STATE"`. A placement inventory may store a logical
name instead of its SSH destination; select that stored name with `--host`.
Credentials are not needed by this command. It does not contact GitHub.

Review each item and the remaining count. Repeat without `--dry-run` to apply
one batch. Continue until `complete` is true. Use `--restart` to rescan after
an unavailable worker returns or when selecting another host in the same poll
state. Previously completed tombstones remain retained. The batch limit must
be from 1 to 1000. Unknown migration versions or malformed state are refused.

For each affected deployed repository:

1. Pause its scheduled poller and back up its state.
2. Review the dry-run report, then apply bounded batches to completion.
3. Run the poller and inspect the durable statuses or tombstones. Run it again
   and confirm that retired references produce no repeated unreadable skips.
4. Confirm `storage-plan --dry-run` preserves live and unreported evidence.
5. Identify the exact override with the unit owner. It may select a patched
   `retain500` engine or override the retained folder count. Remove only that
   obsolete override after the intended standard engine is installed and
   selected. Preserve unrelated disk-budget, image, privilege, and credential
   settings.
6. Restart the affected worker, resume its poller, and record the deployed
   revision and cleanup results in the operational tracker.

This repository contains neither a deployed-state inventory nor host-specific
drop-ins. The procedure does not imply those operator steps have been run.

## Verification

`tests/test_poll_migration.py` exercises hundreds of legacy references with
shared and missing snapshots, bounded batches, pruning, worker restart, and a
clean subsequent poll. It also checks interruption after worker disposal,
byte-preserving dry runs, active and unavailable references, unknown migration
versions, and the real CLI through the worker socket. Tests use a local fake
GitHub API and disposable state.
