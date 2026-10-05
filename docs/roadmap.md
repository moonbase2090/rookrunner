# Build milestones

Milestones are proposed scope, not delivery dates. Completion requires recorded evidence.

Delivery is tracked in Waypoint project `rookrunner` (prefix `RR`). See the
[epics and story plan](planning/project-plan.md) for owners, acceptance criteria,
dependencies, delivery horizons, and completion rules. Waypoint holds live status;
the document records the planning baseline.

| Milestone | Deliverable | Current state |
| --- | --- | --- |
| M0 — Project foundation | Independent repository, PRD, architecture, protocol sketch | Draft documents created |
| M1 — Executable contract | CLI, local worker, persistence, schemas, protocol checks | Complete for the development backend; evidence recorded 2026-09-23 |
| M2 — Real workflow execution | Owned execution engine, source capture, supervised lifecycle | In progress: source capture implemented; custom engine accepted, planner/runtime pending |
| CI-1 — Dogfood and owner CI | Run this repository's `check.yml` from the CLI, then report push and pull request results for owner repositories to GitHub | NS-30 designed. NS-31 through NS-37 implemented. NS-38 recorded a `check.yml` run that ended `failed`. That gap is closed: a later run ended `succeeded` with exit code 0, and a disposable ruff violation ended `failed`. NS-39 designs owner CI. The credential is the GitHub App Rookrunner-App, App ID 5201333 ([check runs](design/check-runs.md)). The installation and the private key are still pending. NS-40 posts one commit status. NS-41 evaluates `on` for push and pull request. A tag push skips the path filters. Tags under `pull_request` are ignored. NS-42 polls one owner repository. NS-43 recorded those statuses |
| M3 — Agent and human access | MCP adapter, dashboard, bounded evidence retrieval | Not started |
| M4 — Downloadable preview | Packaged release and clean-environment acceptance | Not started |

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

The first slice implements [source capture](design/source-capture.md).
The earlier act pin is superseded by the accepted
[owned-engine decision](decisions/0003-owned-execution-engine.md).
See [capture validation](validation/m2-capture.md) and the
[engine implementation/compatibility plan](design/execution-engine.md).
Real workflows do not execute yet.

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

Proposed 2026-10-03 from the owner's priorities. The slices are NS-30
through NS-43 in [next steps](planning/next-steps.md).

1. Design the dogfood path, with one document for every `check.yml` gap (NS-30, designed).
2. Accept read-only `permissions` (NS-31, implemented) and a SHA-pinned `actions/checkout` (NS-32, implemented).
3. Fill the `github` and `runner` contexts and the default variables (NS-33, implemented).
4. Resolve remote actions pinned by full commit SHA (NS-34, implemented) and provide Node 24
   (NS-35, implemented). Run a `node24` `main` entry (NS-36, implemented). Run `post`
   (NS-37, implemented).
5. Record this repository's `check.yml` run from the CLI (NS-38, recorded).
   The identified commit ended `failed`. The gap named in
   [that record](validation/dogfood-check.md) is closed. A later run of
   `21a3f5ad5058027fda62b2b9af6bbba9e336bf83` ended `succeeded` with
   exit code 0. NS-39 designs owner CI. NS-40 posts one commit status.
   NS-41 evaluates `on` for push and pull request. A tag push skips the
   path filters. Tags under `pull_request` are ignored. NS-42 polls one
   owner repository. NS-43 recorded those statuses
   ([validation](validation/owner-ci-rookrunner.md)). NS-44 evaluates
   expressions in `run`, `env`, `with`, and step and job `name`,
   including mixed text. NS-45 evaluates `concurrency` and
   `cancel-in-progress` on this one worker. With `queue: max`, at most
   100 runs can be pending in a group. NS-46 names selected workspace
   files in the artifact manifest and records one local CodeQL SARIF
   file ([upload artifact](design/upload-artifact.md)). The capability
   version stays 12. P4 accepts `actions/checkout@v` plus digits as an
   owned checkout and copies ancestor history when that plan sets
   `fetch-depth` to the YAML integer `0`
   ([checkout tag](design/checkout-tag.md)). Omitting `fetch-depth`
   keeps one parentless synthesized commit and still excludes the
   original commit. `.github/workflows/check.yml` is unchanged. P6
   runs the operator-built image when `worker --runner-image` is set
   and `image` is omitted and every selected job is literal
   `runs-on: ubuntu-latest`
   ([runner image](design/runner-image.md)). An explicit `image` still
   wins. Any other image stays a caller pin. `run_job` still requires
   a digest and still does not select a default. P5 posts one check
   run through the Rookrunner GitHub App when `--app-key` is set, then
   posts the commit status with that installation token. Omitting
   `--app-key` keeps the NS-40 token file
   ([check runs](design/check-runs.md)). The plan schema is unchanged.
   P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions for
   owner-repository push and pull-request runs on this one local
   worker and does not change the engine
   ([secrets](design/secrets.md)). Open questions remain for MB2090.
   The rest of the provisional list is not numbered
   yet.
6. Design owner CI (NS-39, designed). Report runs as commit statuses
   (NS-40, implemented). Evaluate `on` for push and pull request
   (NS-41, implemented). Poll owner repositories from an OS-scheduled
   pass (NS-42, implemented).
7. Record Rookrunner reporting its own CI (NS-43, recorded).

Exit evidence:

- `check.yml` ends `succeeded` with exit code 0 for an identified commit,
  and a forced ruff failure ends `failed`.
- Commit statuses posted for this repository match those terminal results.
- Only `succeeded` with exit code 0 posts `success`.

CI stays portable: plain CLI commands and an OS scheduler. There is no
inbound listener, runner registration, or Terraform. Every limit cites
GitHub's documented limits (https://docs.github.com/en/actions/reference/limits).
Scorecard and the websites need more workflow support. The NS-39 inventory
orders that work.

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
- Resource scheduling across multiple workers and projects.
- Shared caches with explicit trust and retention boundaries.
- Managed capacity and independent workflow scheduling.

Each investigation needs its own requirements and evidence before becoming implementation scope.
