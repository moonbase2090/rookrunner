# Build milestones

Milestones are proposed scope, not delivery dates. Completion requires recorded evidence.
What the current tree implements is the [implementation status](implementation-status.md).
This page does not keep a second status.

Delivery is tracked in Waypoint project `rookrunner` (prefix `RR`). See the
[epics and story plan](planning/project-plan.md) for owners, acceptance criteria,
dependencies, delivery horizons, and completion rules. Waypoint holds live status;
this page records the planning baseline.

| Milestone | Deliverable |
| --- | --- |
| M0 — Project foundation | Independent repository, PRD, architecture, protocol sketch |
| M1 — Executable contract | CLI, local worker, persistence, schemas, protocol checks |
| M2 — Real workflow execution | Owned execution engine, source capture, supervised lifecycle |
| CI-1 — Dogfood and owner CI | Run this repository's `check.yml` from the CLI, then report push and pull request results for owner repositories to GitHub |
| M3 — Agent and human access | MCP adapter, dashboard, bounded evidence retrieval |
| M4 — Downloadable preview | Packaged release and clean-environment acceptance |

Order as of 2026-10-03: M2 finishes NS-29. Then CI-1 runs: dogfood first,
then owner CI. M3 and M4 follow CI-1. The rationale and deferrals are in
[next steps](planning/next-steps.md#re-prioritization-2026-10-03).

## M1 — Executable contract

1. Select the core implementation language and record the decision.
2. Finalize v0 schemas, limits, errors, and submission-key retention.
3. Implement worker startup, repository binding, owner-only socket, and single-instance locking.
4. Implement durable run records and protocol operations using a deterministic development backend.
5. Add CLI discovery, status, log retrieval, cancellation, and JSON output.

Exit evidence: a client can disconnect and reconnect; duplicate submissions remain singular;
invalid requests cannot create work; a second worker cannot own the same state directory.
The development backend validates lifecycle behavior and is not workflow compatibility evidence.

Implementation scope and remaining limitations are recorded in the
[development contract](design/development-contract.md). See the
[M1 completion evidence](validation/m1-completion.md) for checks actually run and
the [v0 schemas](../schemas/v0/README.md) for machine-readable contracts.

## M2 — Real workflow execution

The first slice is [source capture](design/source-capture.md).
The earlier act pin is superseded by the accepted
[owned-engine decision](decisions/0003-owned-execution-engine.md).
See [capture validation](validation/m2-capture.md) and the
[engine implementation/compatibility plan](design/execution-engine.md).
What this tree runs is the [implementation status](implementation-status.md).

1. Implement owned workflow parsing, capability validation, and a versioned execution plan.
2. Capture source and verify snapshot behavior with Git-dependent and checkout workflows.
3. Execute captured inputs through the owned step/job engine and supervised container runtime.
4. Record backend/image identities and separate setup errors from workflow failures.
5. Implement timeout supervision, cancellation cleanup, interrupted-run reconciliation, and disk limits.
6. Expose logs and artifact manifests through the protocol.

Exit evidence: representative workflows produce correct success/failure results; checkout edits do not change accepted inputs;
cancellation leaves no owned work running; restart never silently repeats an interrupted attempt.
Use disposable execution environments for integration checks.

The first executable subset is a starting point, not the compatibility ceiling.
Extend the same engine with expressions/contexts, job dependencies, environment
files, action runtimes, matrices, reusable workflows, and service integrations.
Track local validation separately from GitHub reference comparisons; no M2 exit
claim implies complete GitHub Actions compatibility.

## CI-1 — Dogfood and owner repository CI

Proposed 2026-10-03 from the owner's priorities. The slices are listed in
[next steps](planning/next-steps.md). What this tree contains is the
[implementation status](implementation-status.md).

1. Design the dogfood path, with one document for every `check.yml` gap.
2. Accept read-only `permissions` and a SHA-pinned `actions/checkout`.
3. Fill the `github` and `runner` contexts and the default variables.
4. Resolve remote actions pinned by full commit SHA, provide Node 24, run a `node24` `main` entry, and run `post`.
5. Record this repository's `check.yml` run from the CLI. The record is
   [dogfood check](validation/dogfood-check.md). Related designs are
   [upload artifact](design/upload-artifact.md),
   [checkout tag](design/checkout-tag.md),
   [runner image](design/runner-image.md),
   [check runs](design/check-runs.md), and
   [secrets](design/secrets.md).
6. Design owner CI. Report runs as commit statuses. Evaluate `on` for push and pull request. Poll owner repositories from an OS-scheduled pass.
7. Record Rookrunner reporting its own CI. The record is
   [owner CI](validation/owner-ci-rookrunner.md).

Exit evidence:

- `check.yml` ends `succeeded` with exit code 0 for an identified commit,
  and a forced ruff failure ends `failed`.
- Commit statuses posted for this repository match those terminal results.
- Only `succeeded` with exit code 0 posts `success`.

CI stays portable: plain CLI commands and an OS scheduler. There is no
inbound listener, runner registration, or Terraform. Every limit cites
GitHub's documented limits (https://docs.github.com/en/actions/reference/limits).
`dispatch` submits one workflow on the default-branch tip with
`github.event_name` set to `workflow_dispatch` and then exits. It is
not a listener. Secrets follow the same rule as a push to the default
branch.
Scorecard and the websites need more workflow support. The NS-39 inventory
orders that work.

## Status page

A local read-only page for the dogfood hosts. It is a separate surface
from the M3 dashboard. The design below is the page's scope.

- The worker: up or down, version, runner image, and the last poll.
- Recent runs: repository, pull request or commit, status, duration, and a link to the check.
- The queue and the concurrency groups.

The accepted design is [status page](design/status-page.md).
Whether `status.view`, `poll.record`, and the `status-page` command are in
this tree is the [implementation status](implementation-status.md).
The dogfood start record is [Dogfood start](validation/dogfood.md).

## Planned, paused

Paused until Rookrunner has been dogfooded on the self-hosted Linux hosts.
The dogfood has started. The start record is
[Dogfood start](validation/dogfood.md).
The 14-day window is open. Do not plan or implement these yet.

- macOS jobs.
- `actions/cache` and Docker actions.
- `actions/setup-node` and `hashFiles`.

Untrusted code is not in this list. A run of untrusted code is refused with a warning unless the operator gives an explicit per-run override. A pull request that changes a workflow file is refused unless `--allow-pr-workflow` names that author association. The fetched merge commit is checked out only when its parents are the listed base and head.

## M3 — Agent and human access

1. Map MCP tools to the same protocol operations as the CLI.
2. Return structured failures without losing the MCP connection.
3. Prepare runnable dashboard design options and select a direction.
4. Implement queue, run detail, logs, artifacts, cancellation, and recovery messages.

Exit evidence: CLI, MCP, and dashboard agree on the same run;
reconnection and large logs behave correctly; the visible UI makes uncertainty and compatibility limits clear.

## M4 — Downloadable preview

1. Select the project license and verify bundled third-party notices.
2. Resolve final public names before publishing identifiers.
3. Package for the supported platform with checksums and version metadata.
4. Document prerequisites, installation, first run, troubleshooting, upgrade, and removal.
5. Run P01–P12 acceptance from the PRD against the packaged artifact.
6. Have an external user complete installation and a real workflow run.

Exit evidence: artifact location, digest, tested platform, versions, named workflow run, terminal status,
exit code, and known limitations are recorded in release notes.
Public publishing is a separate step after the preview artifact is reviewable.

## Later investigations

- Authenticated workers on private remote machines.
- Managed GitHub runners using GitHub's official runner application.
  Reporting results for the owner's own repositories moved to CI-1 on
  2026-10-03. Webhook delivery and runner registration stay here.
  The [registration and webhook investigation](research/managed-github-runners.md)
  records upstream requirements and proposed options, without selecting an
  integration or changing the accepted local-worker design.
- Resource scheduling across multiple workers and projects.
  The [scheduling investigation](research/resource-scheduling.md) separates
  implemented placement and local queues from proposed shared admission,
  capacity reservations, and fairness policies. No scheduler design is selected.
- Shared caches with explicit trust and retention boundaries.
- Managed capacity and independent workflow scheduling.
  The [private capacity investigation](research/managed-capacity.md) compares
  proposed pool and scheduling ownership choices. Provider selection,
  credentials, lifecycle, and cost policy remain unresolved.

Each investigation needs its own requirements and evidence before becoming implementation scope.
