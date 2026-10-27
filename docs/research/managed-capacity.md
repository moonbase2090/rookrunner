# Private managed capacity and independent workflow scheduling

Status: proposed investigation, 2026-10-23. Current-source baseline:
`e51b9fc640ad0a039411093ce0305d5b22eb0f4d`. No capacity service or independent
scheduler is selected or implemented by this note.

## Implemented behavior

The accepted [owned-engine decision](../decisions/0003-owned-execution-engine.md)
keeps workflow interpretation and execution in Rookrunner. The
[architecture](../design/architecture.md) gives each local worker one repository,
durable runs, and owner-only socket access. The worker controls local queue
admission, execution, cancellation, and recovery. A successful run still requires
terminal `succeeded` and `exit_code=0` for the identified inputs.

[`poll.py`](../../src/execution_core/poll.py), `Pass.run`, performs one pass.
An external operating-system scheduler chooses when it runs. `Pass._schedules`
reads configured workflows from the recorded default-branch tip.
[`cron.py`](../../src/execution_core/cron.py), `schedule_decision`, evaluates UTC
minutes. First observation records the latest due minute without firing. A
later pass chooses the newest due minute, including one catch-up after downtime.
It does not replay every missed minute.

`Pass._schedule_workflow` persists the due minute, SHA, ref, and matching cron
expressions before submission. `_resume_schedule` retries those recorded inputs
after a lost reply. `submission_key` includes the due minute and cron for a
scheduled run. `dispatch_once` is a separate explicit command using the clone's
`origin/HEAD`; it does not fetch or read poll state. These paths are repository
trigger handling, not an independent hosted schedule service.

Current `poll --place` chooses the first ready, compatible configured worker
under its placement cap. It counts queued plus running runs and checkpoints
the selected place before submitting. This neither provisions a machine nor
reserves shared host CPU or memory. See `Pass._choose_place` and
`_submit_placed` in `poll.py`, and the [owner CI design](../design/owner-ci.md).

## Related proposals remain proposals

These research drafts inform the questions below. They are linked at inspected
revisions because they are not part of the source baseline or accepted design:

- [Private remote workers](https://github.com/moonbase2090/rookrunner/blob/6a6f5f53502ce1f6dbd5da6f3fdee30eca265c3b/docs/research/remote-workers.md)
  leaves identity, authorization, transport, leases, and secret delivery open.
- [Managed GitHub runners](https://github.com/moonbase2090/rookrunner/blob/2a44499bfc993423ef4364e6432c354fcf36cc55/docs/research/managed-github-runners.md)
  separates GitHub-dispatched official-runner execution from the owned engine.
- [Resource scheduling](https://github.com/moonbase2090/rookrunner/blob/615deec73dade6307cc48eb15e7d80a926a8cdb0/docs/research/resource-scheduling.md)
  compares proposed reservations, fairness, admission, and partition recovery.

## Proposed minimal private capacity options

| Option | Control and lifecycle | Cost boundary to decide |
| --- | --- | --- |
| Operator starts a fixed private pool | Keep configured workers and existing placement. Operator owns installation, updates, draining, and removal. | Account for idle machines, storage, maintenance, and retained evidence. |
| Controller starts private capacity on demand | A separate controller owns machine creation, bootstrap, readiness, drain, and deletion. Workers retain execution ownership. | Bound creation rate, active and uncertain reservations, idle lifetime, storage, and cleanup debt before launch. |
| Separate official-runner capacity | Keep GitHub job assignment separate from Rookrunner's owned-engine submissions. Reuse only an explicitly designed machine lifecycle. | Track this pool separately to prevent two dispatch paths consuming the same reservation. |

The proposed first comparison is a fixed owner-only pool against an on-demand
controller. No provider, price, utilization target, or savings is established.
Provider APIs, instance identity, images, licenses, quotas, and billing behavior
need primary-source review once a provider and operating system are selected.
The official-runner option does not replace the accepted owned engine.

Keep machine-management credentials in the controller, separate from repository
reporting credentials and job secrets. Give a bootstrap identity only the
authority needed to join its authorized pool. Define issuance, rotation,
revocation, and attestation before implementation. Specific cloud or GitHub
permission mappings remain unresolved. Scheduling permission must not grant
the job authority to create more machines or change another repository's work.

## Proposed independent schedule ownership

An independent scheduler would need an explicitly authorized schedule definition
bound to a repository, workflow, job selection, revision policy, and owner.
Choose whether it freezes a revision when a tick becomes due or at dispatch.
Persist that choice and the resolved immutable input identity with the demand.
An independent schedule must not silently inherit GitHub credentials or claim
that its execution was triggered by GitHub.

Give each schedule and due tick durable identity separate from worker run and
attempt identities. Retrying acceptance must use the same captured inputs and
submission identity. Decide whether the current newest-tick catch-up rule is
retained, and specify pause, missed-tick retention, edit, deletion, overlapping
run, and explicit rerun behavior. UTC is the implemented basis. Time zones or
daylight-saving behavior would require a separate decision and evidence.

The schedule owner decides when demand is due. A capacity controller decides
whether a compatible reservation can be made. The worker owns acceptance and
execution. A due tick is not a running job, and a booted machine is not a
successful run. Define backpressure when demand exceeds budget without
inventing an unbounded backlog or silently discarding accepted work.

## Proposed recovery and operating boundaries

Persist machine creation intent and provider request identity before external
effects. Reconcile an uncertain create response before creating replacement
capacity. Quarantine a worker whose identity, capabilities, or image cannot be
verified. A timeout, heartbeat failure, or lease expiry cannot establish that
execution stopped. Retain uncertain reservations until termination or fencing
is established, including after controller restart.

Cancellation during bootstrap must also clean up the machine. Drain must stop
new assignments while preserving active ownership. Keep logs and artifacts
before deleting capacity, with explicit access and retention rules. Failed
deletion requires a recorded owner and escalation path. A budget decision
must include orphaned resources and retained storage, not only running jobs.
No teardown deadline, automated retry count, or spending limit is chosen here.

Private remote execution for one owner is the next possible design boundary.
Serving unrelated customers remains a separate milestone under the
[requirements](../prd.md#non-goals), requiring its own authorization, isolation,
abuse, billing, and availability decisions. These proposals do not expand the
local trusted-repository model or authorize provisioning.

## Decisions and evidence needed next

Choose schedule ownership, revision and catch-up policy, pool type, capacity
authority, image provenance, credential delivery, cost accounting, and orphan
recovery before implementation. Preserve existing run identities and terminal
result rules when designing any protocol changes.

An accepted design would need evidence for duplicate ticks, schedule edits,
lost submission and creation replies, worker identity mismatch, revoked
bootstrap access, cancellation during startup, controller restart, exhausted
budget, and failed teardown. This note validates documentation and source
references only. It runs no unchanged runtime suite, benchmark, infrastructure,
UI, or VM, and makes no performance or compatibility claim.
