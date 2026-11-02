# Resource scheduling decisions

Status: Proposed — awaiting owner approval, 2026-11-02. Source baseline:
`d8a8bb103c7ec2fd63d077057c189bbae3597347`. This document recommends an
answer to each unresolved decision in the
[resource scheduling investigation](../research/resource-scheduling.md).
It is not accepted direction. It changes no code, schema, or behavior.
Nothing below is implemented unless it is labeled implemented.

Three labels are used throughout:

- **Implemented**: behavior in the source baseline, cited by path.
- **Accepted**: direction already accepted in the [PRD](../prd.md),
  [architecture](architecture.md), or a [decision record](../decisions/).
- **Proposed**: the recommendation of this document.

## Milestones

AGENTS.md treats remote execution and serving unrelated customers as
separate design milestones. Each decision names its milestone:

- **R, private remote execution for one owner.** The owner's trusted
  repositories on the owner's machines. It covers the implemented
  operator-configured places, local or SSH, and any later authenticated
  remote worker for the same owner. It still needs the
  [remote worker requirements](../research/remote-workers.md) before any
  listener exists.
- **C, unrelated customers.** Hostile code, customer identity, quotas,
  billing, and availability. For C this document states only what an R
  choice does not cover. It selects no C design.

None of these decisions blocks the local preview (M4). The PRD lists
cross-machine scheduling and global fairness as
[non-goals](../prd.md#non-goals) for the first release. The R
recommendations below stay host-local and keep that non-goal intact.

## Summary

| # | Decision | Proposed recommendation | Milestone |
| --- | --- | --- | --- |
| 1 | Reservation owner | A static, operator-owned host share file. Each worker enforces its own share at acceptance. No runtime shared reservation service in R. | R |
| 2 | Project identity | One repository is one project. Key `github:<numeric repository id>`, or `local:<operator name>` for a repository without GitHub. Operator configuration asserts it in R. | R |
| 3 | Pool and group scopes | Pool = one worker's share on one host. Concurrency groups are scoped to the project. Grouped jobs place only on the project's home place. | R |
| 4 | Admission limits | Keep every implemented local limit. Add one refusal before acceptance: a job whose slot need exceeds the worker's share. No fleet quota. | R |
| 5 | Resource units and enforcement | One slot = the implemented per-container limit (2 CPUs, 4 GiB memory and swap, 1024 PIDs). A job needs 1 + its largest service count. Storage stays per worker. | R |
| 6 | Fairness | Dedicated static shares across projects, FIFO within a worker (unchanged). No priority classes. No preemption. | R |
| 7 | Large jobs | A job larger than its worker's share is refused before acceptance. No job waits for host capacity in R. | R |
| 8 | Worker drain | A durable drain flag set by a new versioned method. New keys are refused. Accepted work finishes. Drain completes only with no queued, running, or unresolved attempts. | R |
| 9 | Partition recovery | No automatic reassignment. Claims stay on their place. An explicit operator abandon posts `error` and allows one explicit successor. | R |
| 10 | Protocol versioning | Protocol version 2 adds only the drain method. Versions 0 and 1 and capability version 12 stay unchanged. | R |

The central observation behind 1, 6, and 7 is that a worker executes one
attempt at a time (implemented, see below). A static share that covers a
worker's largest job therefore wastes no capacity that worker could use.
A shared runtime arbiter only adds value under the triggers named in
decision 1.

## Implemented constraints

These facts constrain the decisions. All are implemented at the baseline.

| Fact | Source |
| --- | --- |
| A worker binds one state directory to one repository and refuses another (`ROOT_MISMATCH`). It holds an exclusive `flock` on `worker.lock`. | [`worker.py`](../../src/execution_core/worker.py), `Worker.start` |
| A worker executes serially. One scheduler thread selects one queued run and runs it to completion before selecting another. | `worker.py`, `Worker.execute_queue`, `_select_queued` |
| Every job container and every service container gets `--memory 4g`, `--memory-swap 4g`, `--pids-limit 1024`, and `--cpus 2`. The one-shot account-setup container does not. | [`run.py`](../../src/execution_core/run.py), `_limit_args`, `_CONTAINER_MEMORY`, `_CONTAINER_CPUS`, `_CONTAINER_PIDS` |
| A selected closure runs one job at a time in one container. Each job's deadline is its `timeout-minutes`, default 360, maximum 7200. | `run.py`, `run_job`; [`plan.py`](../../src/execution_core/plan.py), `DEFAULT_JOB_TIMEOUT_MINUTES`, `MAX_JOB_TIMEOUT_MINUTES` |
| A worker with `--docker-socket` lets a job start containers that `_limit_args` does not cover. | `run.py` module notes; `worker.py`, `Worker.__init__` |
| Local admission: 100 queued runs (`QUEUE_FULL`), a 10 GiB default state budget checked at acceptance and watched during execution (`STORAGE_FULL`, `_BudgetWatch`), and 100 pending runs per `queue: max` group. | [`protocol.py`](../../src/execution_core/protocol.py), `MAX_QUEUE`; [`disk.py`](../../src/execution_core/disk.py), `DEFAULT_DISK_BUDGET`; `worker.py`, `submit_workflow`, `_over_budget`; [`concurrency.py`](../../src/execution_core/concurrency.py), `MAX_PENDING` |
| Concurrency groups are casefolded strings compared within one worker's store. | `concurrency.py`, `eligible`, `decide`; `worker.py`, `_apply_concurrency` |
| A place `cap` (1 to 100) bounds queued plus running runs seen by one poller. It is not CPU, memory, or parallelism. Selection takes the first eligible place. | [`poll.py`](../../src/execution_core/poll.py), `accept_place`, `_count_active`, `_choose_place` |
| A claim is checkpointed before submission and stays on its place. An unreachable place leaves the claim unplaced, not replaced. | `poll.py`, `_submit_placed`, `_placed_caller` |
| A place whose `worker.describe` reports `ready: false` gets no new placement, but its existing claims are still reconciled and reported. | `poll.py`, `_classify`, `_submit_placed` |
| A poller cancels a queued run 24 hours after acceptance when the place is reachable. | `poll.py`, `QUEUE_LIMIT`, `queue_expired`, `_expire_placed` |
| Restart records each interrupted `running` attempt as `lost`. Cleanup is `unresolved` when its owned container remains. Nothing is replayed. | `worker.py`, `_reconcile_interrupted`, `_store_restart_lost` |
| A cancel that cannot confirm cleanup blocks every new workflow attempt in that process. After restart, a retained container blocks only reuse of its own identity, so a new attempt can run beside it. | `worker.py`, `conflicts_with_unresolved`, `reuses_unresolved_identity`, `_load_retained` |
| Only `succeeded` with exit code 0 maps to commit status `success`. `lost` maps to `error`. A lost claim is never retired. | [`status.py`](../../src/execution_core/status.py), `github_state`; `poll.py`, `_post_record` |
| `poll --retry-failed KEY` creates one successor for a `failed` run only. Lost, queued, running, and cancelled runs are refused. | [development contract](development-contract.md#explicit-retry-of-a-failed-poll-submission) |
| `worker.describe` reports `protocol_versions` `[0, 1]`. The v0 schema fixes `protocol_versions`, `methods`, and `capabilities` as constants and sets `additionalProperties: false` on the describe result and the run record. | `worker.py`, `dispatch`; [`schemas/v0/contract.schema.json`](../../schemas/v0/contract.schema.json) |
| The plan capability version is 12 and stays 12 until a signed-off change. | `plan.py`, `CAPABILITY_VERSION`; [conventions](../conventions.md) |

Accepted direction that every recommendation keeps: the
[owned engine](../decisions/0003-owned-execution-engine.md), one worker per
repository, local queue ownership by the worker, the success rule
(terminal `succeeded` and `exit_code=0` for the identified input
snapshot), and the separation of remote execution from unrelated customers.

## 1. Reservation owner

**Proposed.** The reservation owner in R is a static host share file that
the operator owns. It lists, for one Docker engine, the host slot budget
and each worker's share in slots (decision 5). Each worker reads it at
start, finds its own entry by state directory and repository, and checks
that the shares on that engine sum to no more than the host budget. A
worker with no entry, or on an over-committed file, reports `ready: false`
with a readiness error. The worker refuses at acceptance any job that
needs more slots than its share (decision 7). Workers never write the file.

**Rationale.** A worker runs one attempt at a time, and every container has
a hard Docker limit. A worker's peak use is therefore its largest job's
slot need. If each share covers that need and the shares fit the host,
the host is never over-committed. That holds however many pollers race
for the same place, because the race only lengthens a bounded local queue.
The file needs no daemon, no new protocol, no durable shared store, and no
recovery. It keeps local queue ownership with each worker, as the
investigation proposes.

**Rejected.**

- *Per-host ledger service* (one daemon per host that grants reservations
  to workers over a local socket). It is the right owner once turn-taking
  is needed, but under serial workers it adds a process, a protocol, and
  restart reconciliation for no capacity gain.
- *Poll placement cap as the owner* (implemented today). Separate poll
  state directories do not share a reservation, and the cap counts runs,
  not resources.
- *Shared SQLite or lock files written by every worker.* The investigation
  rules out coordinating through a shared worker database. A `flock`
  semaphore is released when the worker process exits, while Docker
  containers outlive the process. That releases capacity whose cleanup is
  not established.
- *Central pool coordinator.* It needs a network service, leases, and
  cross-machine fencing. Those belong to C or to managed capacity.

**What would change it.** Adopt the per-host ledger, with the conditional
choices in decisions 6 and 7, when any of these holds: the owner wants
shares that sum to more than the host (workers take turns), worker
execution parallelism above one is accepted, or a
[managed capacity](../research/managed-capacity.md) controller needs a
pool-wide demand view. C needs a central authority with authenticated
tenants. In both cases the host stays the component that enforces and
fences, because it alone can stop its containers.

## 2. Project identity

**Proposed.** A project is one repository. Its key is
`github:<numeric repository id>` for a GitHub repository, or
`local:<operator name>` for a repository without one. The `owner/name`
string stays the configured selector and display name. In R the operator
asserts the key in the host share file and in poll configuration. The
binding that is enforced is the implemented one: state directory to
repository (`ROOT_MISMATCH`), and place to polled repository by origin
(`_remember_repository`). Several workers, on one host or several, may
serve one project.

**Rationale.** The numeric id survives rename and transfer, and the poller
already reads it from the repository body (`_event_repository`). The
pull-request trust gate already compares repositories by numeric id
([secrets](secrets.md)). One
repository per project matches one repository per worker.

In R the key is an accounting and grouping label, not an isolation
boundary. Workers under one service account can read each other's state
directories. Project isolation needs separate accounts or machines, which
is C scope.

**Rejected.**

- *`worker_id`.* It names one state directory, not a project. A project can
  have several workers.
- *`owner/name` as the durable key.* It changes on rename or transfer.
- *Clone path.* It is specific to one host.
- *GitHub App installation or owner account.* It merges many repositories,
  so their concurrency groups and shares would collide.

**What would change it.** C derives the project from authenticated
authorization, such as an installation plus repository id, never from
operator configuration. A requirement to group several repositories under
one budget would add a separate account level above projects.

## 3. Pool and group scopes

**Proposed, pool.** A pool is one worker's share on one Docker engine. The
places configured for a repository's poll pass are that project's
allowed pools, as implemented. No named multi-host pools in R.

**Proposed, groups.** Concurrency groups are scoped to the project. They are
never shared across projects. A grouped job places only on the project's
home place, which is the first configured place. A job is grouped when the
poller cannot rule out a `concurrency` key at the workflow, the job, or a
called workflow in the source it reads. If it cannot read that source, it
treats the job as grouped. When the home place is unreachable or full, the
job stays unplaced. It never falls back to another place.

**Rationale.** Groups are compared only within one worker's store today. A
project whose jobs spread across two places can run two members of one
group at once. That breaks the investigation's rule that moving a run must
not bypass its concurrency restriction. Pinning grouped jobs to one worker
reuses the implemented `concurrency.py` semantics exactly, with no
cross-host lock. The detection is deliberately conservative. A false
"grouped" only loses spreading. A false "ungrouped" would break the rule.

**Rejected.**

- *Worker scope* (implemented). It is correct only while a project has one
  place.
- *Cross-project or global groups.* One repository's workflow text could
  then block or cancel another's work. That is an authorization question,
  not a grouping one.
- *Explicit shared resources* (for example two repositories sharing a
  deploy target). Not needed in R. It would be a separate named lock with
  its own authorization, not a concurrency group.
- *Cross-host group lock service.* The same cost as a central coordinator.

**What would change it.** A ledger or coordinator (decision 1) can hold
project-scoped groups directly and lift the home-place pin. Grouped jobs
that need more throughput than one worker would also force that change.

## 4. Admission limits

**Proposed.** Keep every implemented limit unchanged as a local safeguard:
100 queued runs per worker, the state disk budget with acceptance check and
runtime watch, 100 pending runs per `queue: max` group, the place `cap`,
the 24-hour queued expiry for poll submissions, and the message and page
bounds. Add one check before acceptance: a job whose slot need exceeds its
worker's share is refused (decision 7). The refusal creates no run, so a
retry with the same key after the operator raises the share is a fresh
acceptance. Operators cancel queued work with the implemented `run.cancel`.
No fleet-wide, per-project, or per-account quota in R.

**Rationale.** Each local limit is tested and already fails closed. With
static shares, compatible work never waits for host capacity, so the
queue and the 24-hour expiry bound all waiting. The place `cap` is still
useful as a per-poller queue bound. Its documentation should say it is not
a capacity reservation.

**Rejected.**

- *A per-project quota on top of shares.* The share already bounds a
  project per worker. A second number adds a way to disagree.
- *A worker-side queue deadline for CLI and MCP submissions.* Owner-run
  submissions are watched by the submitter, and `run.cancel` exists.
- *Accepting an oversized job and failing it later.* That produces a
  `failed` record for something that could never run. Unsupported
  requirements must be rejected before acceptance.

**What would change it.** C needs per-customer quotas, rate limits, and
abuse controls. A ledger adds waiting for capacity, which would need a
visible waiting reason and a wait deadline.

## 5. Resource units and enforcement

**Proposed.** The unit is one slot, defined as the implemented container
limit: 2 CPUs, 4 GiB memory with equal swap, and 1024 PIDs. A job's slot
need is 1 plus the largest service count of any job in its selected
closure. The worker computes it from the accepted plan, so no workflow
field is added. The host budget in slots is at most the smaller of
`NCPU / 2` and `MemTotal / 4 GiB`, as the Docker engine reports them.
The engine's view is used because containers run there, including inside
a desktop virtual machine. A worker refuses to become ready when the
declared budget exceeds that bound.

A worker started with `--docker-socket` is unbounded. Its share is the
whole host, so no other worker can share that engine.

An attempt whose cleanup is `unresolved` while its container is present
counts against its worker's share until cleanup is confirmed. The worker
does not start an attempt that would exceed the share with it. This
closes the implemented gap where a new attempt can run beside a retained
container after restart.

Scratch storage is not a slot resource. Each worker's state budget and
runtime watch keep enforcing it. The share file also records each
worker's budget. A worker refuses to become ready when the budgets on one
filesystem sum to more than its size. Docker image and layer storage stays
outside the budget and remains an operator responsibility.

**Rationale.** Docker already enforces each slot, so the accounting
matches a hard limit rather than a request. Memory is not compressible,
so it is never over-committed. Deriving the need from `services` keeps
workflow syntax unchanged and keeps capability version 12.

**Rejected.**

- *Free-form CPU and memory requests per job.* Workflow syntax has no such
  field, and requests need trust and measurement.
- *Labels alone.* A label cannot reserve anything.
- *Measured usage.* That needs metrics collection. It belongs to weighted
  fairness, which is deferred.
- *Host-wide disk reservation.* Docker storage is outside the state
  directory, so a reservation could not be enforced.

**What would change it.** Jobs that need larger containers, as GitHub's
larger runners provide, would add size classes: an operator mapping from a
`runs-on` label to a slot multiple and matching Docker flags. That needs a
plan change and a capability version decision. Measured CPU idleness
could justify CPU over-commit. Memory over-commit stays rejected.

## 6. Fairness

**Proposed.** Across projects, fairness is the dedicated static share. A
project's share on a host is never consumed by another project. Within a
worker, order stays FIFO by stored sequence with group eligibility, as
implemented. No priority classes. Urgent work is handled by the operator:
cancel queued runs, drain, or adjust shares. Running work is never
interrupted to improve fairness.

**Rationale.** With serial workers, cross-project contention on a host can
only be over-commitment, and decision 1 forbids it. No runtime arbiter is
needed, so the investigation's FIFO and round-robin comparison has nothing
to arbitrate in R. Priority classes would bring back starvation and need
rules for who may mark work urgent.

**Rejected.**

- *Global FIFO.* A burst from one project fills capacity ahead of others,
  and it needs a shared arbiter.
- *Project round robin.* Fair turns, but it needs the ledger.
- *Weighted resource accounting.* Needs trusted measurement, weights, and
  rules for unused shares.
- *Preemption.* No accepted cleanup policy, and interrupting a run cannot
  produce a successful result.

**What would change it.** When the ledger trigger in decision 1 fires, the
proposed policy is round robin by project key, FIFO within a project,
granted per worker request. That policy should first be compared with FIFO
on representative mixed workloads, as the investigation recommends.
Weighted shares come only after measurements justify them. C needs
contractual shares, which is a separate decision.

## 7. Large jobs

**Proposed.** In R a job is large only relative to its worker's share. A
job whose slot need exceeds the share is refused before acceptance, with
a message that names the need and the share. The poller records it as a
skipped job and continues the pass. It does not try another place. Because
nothing waits for host capacity, no job can starve behind smaller ones.

**Rationale.** A refusal before acceptance follows the rule that
unsupported requirements fail early. The poller does not reroute, so a
misconfigured share shows up instead of being hidden by placement.

**Rejected.**

- *Wait for capacity with backfill.* Small jobs can starve a large one.
- *Strict head-of-line blocking.* It leaves capacity idle, and it needs
  the ledger.
- *Reroute to a place with a larger share.* It hides configuration errors
  and is not needed while places for one repository are configured alike.

**What would change it.** With a ledger, the proposed treatment is strict
accumulation. The large request at the head of the rotation becomes the
single pending-fit holder. No other request is granted until it fits.
No backfill. Its wait is then bounded by the enforced deadlines of the
running attempts, plus any unresolved cleanup, which needs the operator.
Backfill bounded by `timeout-minutes` should be considered only if
measured idle capacity justifies it.

## 8. Worker drain

**Proposed.** Add a durable drain flag in worker metadata, set and cleared
by a new mutating method `worker.drain` with `version: 2` (decision 10).
The flag survives restart. While draining:

- `worker.describe` reports `ready: false` with the readiness error
  `draining`. That fits the v0 schema. The implemented poller then places
  no new work there and keeps reconciling and reporting existing claims.
- A `run.submit` with a new key is refused with `WORKER_NOT_READY`. A
  repeated key returns its original run, as implemented, because the key
  lookup precedes the readiness check.
- Queued and running runs continue to terminal states. The operator may
  cancel queued runs with `run.cancel`.
- Drain is complete only when no run is `queued` or `running` and no
  `lost` run has cleanup `unresolved`. The operator stops the worker only
  after that. Stopping earlier makes running attempts `lost`, which is not
  success.

For an image change, the operator drains, restarts with the new image,
then clears drain. Acceptance already records `image_digest` and
`image_reference` on each run (`worker.py`, `_accept_planned`). Drain is
per worker. A host drain is a drain of every worker in its share file.

**Rationale.** It reuses the implemented readiness path, so older pollers
behave correctly without an upgrade. Finishing accepted work keeps the
acceptance promise. Moving accepted runs to another worker would change
their identity and is not allowed.

**Rejected.**

- *Drain by stopping the worker.* It turns running attempts into `lost`.
- *A drain that cancels queued work automatically.* It discards accepted
  work that the operator can cancel explicitly.
- *A flag file written by a CLI into the state directory.* The protocol
  is the worker's control contract, and a method also works through the
  implemented SSH `call` path.
- *Moving queued runs to another place.* That needs new keys and
  submissions, which is explicit resubmission, not drain.

**What would change it.** A capacity controller that deletes machines
would wait for drain completion and also require evidence retention
before deletion, as the managed capacity investigation requires.

## 9. Partition recovery

**Proposed.** In R no component reassigns work automatically. Every
implemented rule stays: claims are sticky, an unreachable place leaves its
claims unplaced, a lost claim stays on its place, and worker restart marks
interrupted attempts `lost` without replay. Two explicit operator actions
are added:

- *Abandon a place's claims.* The operator asserts that the place is gone
  or will reconcile on restart. The poller posts `error` for each
  unfinished claim on that place, never `success`. It retires the claims
  with `abandoned` metadata and never posts a later result from that place
  for those contexts. The worker's own run history is untouched.
- *Explicit successor.* The `--retry-failed` rule is extended to `lost`
  runs and abandoned claims. It creates one successor under a derived key
  at the same tested commit, with current trust gates. The successor owns
  the status context.

Unresolved cleanup keeps counting against the share (decision 5) until
cleanup is confirmed, however long that takes.

**Rationale.** A timeout or unreachable host cannot prove that execution
stopped. With no shared reservations, a partition between poller and host
cannot over-commit the host, because the host's share check does not
depend on the poller. Fencing stays on the host that owns the containers:
`_reconcile_interrupted` removes owned containers on restart, and
`owned_container_present` checks them. The abandon step makes the risk of
duplicate side effects an explicit operator decision. Neither action can
create success from queued, running, lost, or unknown states.

**Rejected.**

- *Lease expiry with automatic reassignment.* Expiry cannot prove the old
  attempt stopped. It risks duplicate deploys and conflicting status
  posts.
- *Ownership generations and fencing tokens.* They are only needed once
  reassignment exists. Attempt ids already identify attempts in R.
- *Treating an unreachable host's queued runs as cancelled without
  contact.* The host may still start them.

**What would change it.** Automatic reassignment (C, or a managed capacity
controller) needs monotonic ownership generations. The host must check
them before launching and before publishing results. Cross-machine fencing
must also be shown to stop execution. That remains a separate design.

## 10. Protocol versioning and local invariants

**Proposed.** Unchanged, with no version change: one worker per repository;
serial execution; submission key semantics and `IDEMPOTENCY_CONFLICT`;
run states and terminal semantics; the success rule; `lost` without
replay; every limit in decision 4; concurrency evaluation within a
worker; sticky placement; the container limits; plan capability version
12; and the v0 and v1 request and response shapes.

Protocol version 2 is added only for `worker.drain`. `worker.describe`
then advertises `[0, 1, 2]` and lists the method, which is a revision of
the published schema because both fields are constants. Draining is shown
through the existing `ready` and `readiness_error` fields. The share-fit
refusal uses the existing `CAPABILITY_UNSUPPORTED` kind with a stable
message, which the poller maps to a skipped job. The share file is local
worker configuration, not protocol. A later worker-to-ledger protocol
would get its own version, separate from the client protocol.

**Rationale.** The v0 schema is closed, so any new describe field, run
field, method, or error kind breaks a strict validator. Keeping the change
to one method keeps the schema revision small and its version explicit.
A worker without the method returns `METHOD_NOT_FOUND` before any side
effect.

**Rejected.**

- *New optional fields under versions 0 and 1.* They violate the published
  `additionalProperties: false`.
- *A new `RESOURCE_UNSATISFIABLE` error kind.* It would add an error-kind
  schema change for a case the existing kind describes.
- *A capability version change.* No plan field changes.

**What would change it.** A ledger needs a waiting reason on run records
and capacity fields in `describe`. That would be a further protocol
version with its own schema revision.

## Dependencies and suggested order

This order is a proposal, not a commitment. Each step needs its own
pull request and focused checks.

1. Decision 10 first, so the schema revision and version policy exist
   before any new method.
2. Decision 8 (drain). It depends only on 10 and is useful on its own,
   for example for image changes.
3. Decisions 2 and 5, then 1. The share file needs the project key, the
   slot unit, the engine resource probe, and the unresolved-cleanup
   accounting.
4. Decisions 4 and 7 (share-fit refusal and the poller skip). They depend
   on 1 and 5.
5. Decision 3 (project-scoped groups with the home-place pin). It depends
   on 2 and changes only the poller.
6. Decision 9 (abandon and the extended successor). It depends on the
   implemented retry path and on 2 for the status context.

Decision 6 needs no implementation in R. It is settled by 1.

## Evidence required before implementation is accepted

Every test must fail when the behavior it names is removed:

- A share file whose shares exceed the engine bound leaves the worker not
  ready. A job needing more slots than the share is refused and creates
  no run.
- A retained container whose cleanup is unresolved blocks an attempt that
  would exceed the share after restart.
- Two pollers racing for one place never run two attempts on that worker
  at once.
- A grouped job is never placed on a second place while the home place is
  unreachable.
- Drain refuses a new key, returns the original run for a repeated key,
  survives restart, and is not complete while cleanup is `unresolved`.
- Abandon posts `error`, never `success`. A late terminal result from an
  abandoned place is not posted.
- Lost replies, cancellation races, and restart still produce one
  authoritative terminal result.

Behavior changes also need the repository's clean-VM proof. Only terminal
`succeeded` with `exit_code=0` for the identified input snapshot counts as
a successful run.

## Questions for the owner beyond approval

- **Over-subscription or parallel workers.** If hosts should soon run more
  workers than fit at once, or workers should run jobs in parallel, the
  ledger trigger in decision 1 fires, and decisions 6 and 7 switch to their
  conditional choices.
- **Abandon risk.** Abandoning a place (decision 9) accepts the risk of
  duplicate side effects, such as a deploy that runs twice. That is a
  policy call.
- **Docker socket on shared hosts.** Under decision 5, a worker started
  with `--docker-socket` takes its whole host, so no other worker can share
  that engine.
- **Spread repositories with `concurrency`.** Under decision 3, a
  repository spread across several places that declares `concurrency`
  stops spreading those jobs.
- **Schema sign-off.** Protocol version 2 revises the published schema.
  The conventions require sign-off only for capability version changes, so
  decide whether this revision also needs the sign-off label.

## Approval checklist

Tick each item to accept it as direction. An unticked item stays proposed.

- [ ] 1. Reservation owner: static host share file, enforced by each worker.
- [ ] 2. Project identity: one repository, `github:<numeric id>` or `local:<name>`.
- [ ] 3. Pool = one worker's share; groups scoped to the project; grouped jobs pinned to the home place.
- [ ] 4. Admission: keep local limits; add the share-fit refusal; no fleet quota.
- [ ] 5. Units: the implemented container limit as one slot; 1 + largest service count; memory not over-committed; `--docker-socket` takes the whole host.
- [ ] 6. Fairness: dedicated shares, FIFO within a worker, no priorities, no preemption.
- [ ] 7. Large jobs: refuse before acceptance when larger than the share.
- [ ] 8. Drain: durable flag, versioned method, accepted work finishes, completion includes cleanup.
- [ ] 9. Partition recovery: no automatic reassignment; explicit abandon and successor.
- [ ] 10. Protocol version 2 for `worker.drain` only; capability version stays 12.
- [ ] Conditional choices for the ledger trigger (round robin by project, strict accumulation) are noted, not accepted.

## Validation of this document

This document changes no runtime behavior. It was checked by reading the
cited source at the baseline, by a relative-link check over this file, and
by `scripts/check-release-text.sh`. No scheduler, benchmark, VM, or
infrastructure was run. It measures no throughput, utilization, fairness,
or latency.
