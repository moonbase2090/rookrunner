# Resource scheduling across workers and projects

Status: proposed investigation, 2026-10-23. Source baseline:
`e51b9fc640ad0a039411093ce0305d5b22eb0f4d`. This note records implemented
placement and local queue behavior, then compares future scheduling choices.
It accepts no new design and changes no execution behavior.

## Implemented boundary

The [architecture](../design/architecture.md) assigns each worker one repository
and a durable local run store. The [owned-engine decision](../decisions/0003-owned-execution-engine.md)
remains accepted. Current placement can select among configured workers without
providing shared resource accounting or cross-project fairness.

| Area | Source-grounded behavior |
| --- | --- |
| Placement input | [`cli.py`](../../src/execution_core/cli.py), `_places_from_args`, accepts ordered `poll --place` entries containing a name, state, cap, image digest, and optional SSH destination. |
| Eligibility and choice | [`poll.py`](../../src/execution_core/poll.py), `Pass._classify`, `_remember_repository`, and `_choose_place`, check reachability, readiness, image compatibility, repository origin, and active count against the configured cap. Selection takes the first eligible place in configuration order. |
| Meaning of cap | `Pass._count_active` counts queued plus running records. This is a poll placement cap, not a CPU or memory reservation, and not worker execution parallelism. |
| Durable ownership | `Pass._submit_placed` checkpoints the selected place before submission. A recorded claim stays on that place, including after an uncertain reply. A missing or unreachable place is not silently replaced. |
| Local execution | [`worker.py`](../../src/execution_core/worker.py), `Worker.start`, `execute_queue`, and `_select_queued`, lock one state directory, persist runs in SQLite, and execute one picked run to completion before selecting another. Queue candidates are ordered by stored sequence. |
| Concurrency semantics | [`concurrency.py`](../../src/execution_core/concurrency.py), `eligible` and `decide`, compare casefolded groups within one worker. `queue: single` replaces queued peers. `cancel-in-progress` also selects running peers for cancellation. `queue: max` preserves pending peers up to the implemented bound and cancels a new excess run. |
| Admission and recovery | `Worker.submit_workflow` and `_plan_and_accept` enforce local queue and storage checks. `_reconcile_interrupted` records interrupted running attempts as `lost`, attempts owned cleanup, and does not automatically replay them. |

The [placement tests](../../tests/test_placement.py) cover cap accounting,
ordered choice, image and repository mismatch, sticky placement, and unreachable
claims. The [concurrency tests](../../tests/test_concurrency.py) cover group
eligibility and replacement. Their fixtures do not establish a distributed
capacity or isolation guarantee.

Inference from these paths: separate poll state directories do not share an
atomic capacity reservation. Two pollers can observe the same available place
before either submits. Local queue admission still applies, but the placement
cap cannot be treated as a host-wide limit. Likewise, separate worker stores
do not coordinate identical concurrency-group strings.

## Proposed ownership and admission

Keep local queue ownership with each worker. If shared scheduling becomes an
accepted milestone, introduce one authoritative reservation owner for each
shared capacity pool. Independent projects must not mutate a shared checkout
or worker database as a coordination mechanism.

A proposed scheduler would distinguish these records:

- A project identity derived from operator configuration or authenticated
  authorization, with its repository, trust policy, and allowed worker pool.
- A worker registration with capability and image identities, resource budget,
  current reservations, health, and drain state.
- A demand record bound to immutable input identity, submission key, required
  capabilities, resource request, and an explicitly scoped concurrency group.
- An assignment with run and attempt identities, worker identity, reservation,
  and a monotonically increasing ownership generation.

These are proposed concepts, not additions to the current protocol or schema.
Separate worker eligibility from capacity accounting and queue ordering.
Authenticate and authorize first. Reject unsupported requirements before
acceptance. Compatible work without capacity may wait under a bounded queue
and retention policy. Explain whether it is waiting for capacity, a group, or
a compatible worker, without representing a waiting job as running.

Resource requests and enforceable worker budgets need defined units for CPU,
memory, and scratch storage. A label alone cannot reserve them. Count a
reservation until completion and cleanup are established, including uncertain
assignments. Existing local queue and disk bounds remain local safeguards.
No fleet quotas, queue deadlines, or resource defaults are selected here.

## Proposed fairness choices

| Policy | Advantage | Tradeoff |
| --- | --- | --- |
| Global FIFO among eligible demands | Simple order and predictable audit trail | One project's burst can fill capacity ahead of others. |
| Round robin across projects, FIFO within each project | Gives each active project a turn | Equal turns do not mean equal CPU time or memory consumption. |
| Weighted resource accounting | Can express unequal shares and resource use | Requires trustworthy requests, measured usage, weights, and rules for unused shares. |

For a first owner-only design, compare FIFO and project round robin using
representative mixed workloads before choosing. Weighted shares remain a later
option if measurements justify the extra policy. This is a recommendation for
evaluation, not an accepted scheduling algorithm or a performance claim.

Ordering alone does not solve fit. Decide whether a large waiting request can
reserve capacity while smaller jobs proceed, and how to prevent indefinite
waiting. Define how operators prioritize urgent work, drain workers, or cancel
queued demand. Do not interrupt active work merely to improve fairness without
an explicit preemption and cleanup policy.

Workflow concurrency and resource fairness serve different purposes. Preserve
the accepted workflow grouping semantics while deciding whether future group
identity is scoped to repository, project, or an explicit shared resource.
Moving a run to another worker must not bypass its concurrency restriction.

## Proposed failure and trust requirements

| Event | Required design outcome before remote scheduling is accepted |
| --- | --- |
| Lost submit reply | Retry the same assignment and submission identity on its owner. Reconcile acceptance before considering another worker. |
| Worker heartbeat or lease expires | Treat ownership as uncertain. Expiry alone cannot prove that a partitioned worker stopped executing. Block conflicting reassignment until termination or fencing is established. |
| Coordinator restart | Recover durable assignments and reservations before admitting new work. A stale owner generation must not launch or publish authoritative results. |
| Cancellation races with completion | Keep one authoritative terminal result. Retain capacity until cleanup is confirmed. |
| Worker crashes or cleanup fails | Preserve evidence and unresolved resource ownership. An explicit new attempt needs a resubmission policy, not an automatic claim of success or transparent replay. |
| Worker is drained or image changes | Stop new assignments and reconcile active ownership. Keep the accepted image and input identities attached to each attempt. |

Generation checks at a coordinator do not by themselves stop code on a
partitioned machine. A future design must specify which component can fence
execution and how that fact is proved. Do not promise exactly-once side effects
from submission deduplication or lease expiry.

Shared scheduling needs authenticated worker identity and encrypted transport
with authorization for each project and repository. Current SSH placement and
owner-only local sockets are existing access paths, not customer isolation.
Workers must not receive control-plane credentials for other projects. Avoid
shared writable caches and workspaces across trust boundaries unless their
access, integrity, and cleanup rules are separately accepted.

Remote execution for one owner and execution for unrelated customers remain
separate milestones under the [project requirements](../prd.md#non-goals).
Hostile-code isolation, customer quotas, billing, and service availability need
their own decisions. Nothing here expands the current trusted-repository model.

## Unresolved decisions and evidence

Before implementation, choose the reservation owner, project identity model,
pool and group scopes, admission limits, resource units and enforcement,
fairness policy, large-job treatment, worker drain behavior, and partition
recovery policy. Decide which existing local invariants remain unchanged and
which need an explicit protocol version change.

Validate a proposed scheduler with competing projects, simultaneous pollers,
incompatible workers, oversized requests, lost replies, stale owners,
partitioned workers, cancellation, and unresolved cleanup. Bind results to
identified inputs and attempts. Only terminal `succeeded` with `exit_code=0`
counts as success. Queue acceptance, placement, heartbeat, or lease expiry
cannot establish it.

This investigation measures no throughput, utilization, fairness, or latency.
Its focused checks inspect current placement and concurrency behavior and
documentation links. Any later behavior change requires the repository's
clean-VM proof. No scheduler, provisioning, UI, or VM is introduced here.
