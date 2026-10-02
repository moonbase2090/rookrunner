# M2 next steps

Status: build order, 2026-10-02. Derived from the
[PRD](../prd.md) and the [roadmap](../roadmap.md). The
[engine plan](../design/execution-engine.md) supplies the sequence inside
roadmap step 1 and the first executable subset. NS-1 through NS-6 are
implemented. Later items are not. Continuous integration runs ruff and the
unit test suite on push and pull request. This file is not Waypoint status
and not an acceptance of open PRD questions.

Capture of working files already exists and is not repeated here. Roadmap
step 2's Git-dependent and checkout verification does not. The PRD leaves
sanitized Git metadata undesigned, so those workflows stay unsupported until
a later item. The act pin stays historical. Development `run.submit` stays
version 0 and fixture-only. Version 1 accepts one planned job and the worker
executes it.

Each item is one PR. A PR does not start the next item. Existing M1 and
capture tests must still pass. Real execution checks use disposable
environments. No item publishes a GitHub Actions compatibility claim.

## First slice

**NS-1. Parse a workflow into a versioned plan, and do not run it.**

Status: implemented.

This was the first slice. It is roadmap step 1. It needs no Docker,
no protocol change, and no run record.

The PR adds one maintained YAML parser after its license and source are
recorded in [development dependency provenance](../development-dependencies.md).
The planner reads workflow bytes, including bytes taken from an existing
snapshot. It does not fetch actions, pull images, start a container, or
accept a run.

Acceptance criteria:

- One explicitly selected job with sequential `run` steps produces a
  canonical plan. The plan records a capability version, the job id, step
  order, declared shell, env, and working-directory, source locations, and a
  digest of the plan bytes. Repeating the parse yields the same digest.
- String keys such as `on` survive parsing. Duplicate YAML keys fail.
  Expression text is preserved and not evaluated.
- `uses`, `needs`, `strategy`, matrix, `secrets`, service containers,
  reusable workflow calls, and host or privileged execution fail before a
  plan exists. The error names the field and says the capability is
  unsupported. Unsupported is a current limit, not a decision to drop the
  feature.
- A workflow with no selected job, or a job that is not sequential `run`
  steps, produces no plan.
- A workflow file larger than 500 KB produces no plan. The error is a
  capability error and cites that limit. The plan records job
  `timeout-minutes` as the job time bound, default 360 minutes, and rejects
  a value above 5 days. Matrix expansion stays unsupported. GitHub's
  documented limits for this slice are a 500 KB workflow file, 256 matrix
  jobs per run, and job execution time of 6 hours on hosted runners or 5
  days on self-hosted runners
  (https://docs.github.com/en/actions/reference/limits).
- Tests cover one valid workflow and these rejections without Docker. The
  planner launches no process.

## Then, in order

**NS-2. Verify a snapshot before anything reads it as an attempt.**

Status: implemented.

Acceptance criteria:

- Verification recomputes each entry digest and the manifest digest. A
  matching snapshot is accepted. A changed byte, mode, or manifest field
  fails with no attempt directory left behind.
- The check does not follow a symlink outside the snapshot and does not read
  the original checkout.
- Tests mutate a disposable snapshot and assert rejection.

**NS-3. Materialize an attempt workspace from a verified snapshot.**

Status: implemented.

Acceptance criteria:

- The workspace is a new private directory, not the checkout and not the
  snapshot store. Regular files and internal symlink rules match the capture
  contract. Modes are the captured 0644 or 0755 modes.
- A failed materialization removes the partial workspace.
- Tests build a workspace from a captured fixture and show that editing the
  original checkout does not change the workspace bytes.

**NS-4. Run that workspace as sequential Bash steps in Docker.**

Status: implemented.

This is the library used by later worker code. It is not a protocol method.
The caller passes an image already pinned by digest. No project default
image is selected. That choice remains the open PRD question.

Omitted shell, `bash`, and `sh` follow the Linux runner commands reviewed
2026-10-02. Explicit `bash` enables pipefail. An omitted shell does not.
Workflow env is overridden by job env, then by step env. The runner then
sets `GITHUB_WORKSPACE` and `ROOKRUNNER_EVENT`. The event file is the
caller's canonical JSON, not a GitHub event delivery. The container network
is `none`. The Docker socket and host credentials are not mounted. A nonzero
step stops the sequence. The result names that step, its exit code, and the
image digest. Docker missing, an unresolvable digest, and a workspace or
snapshot that fails verification raise `SETUP_FAILED` and are not exit 0.

Acceptance criteria:

- One job's `run` steps execute in order in one digest-identified container.
  Documented shell, env, and working-directory behavior for this subset is
  tested, not approximated.
- Exit 0 is success. A later step's nonzero exit is failure and stops the
  sequence. The result names the step, the exit code, and the image digest.
- The event input is an explicit local value supplied by the caller. It is
  not a GitHub event delivery.
- Docker missing, a digest that will not resolve, or a workspace that fails
  verification is a setup failure. It is not stored as a step exit 0.
- Tests use a disposable environment. No socket is mounted into the
  container. Host credentials are not mounted.

**NS-5. Accept a workflow run only after capture and planning succeed.**

Status: implemented.

Version 1 `run.submit` captures, verifies, and plans one job, then commits a
queued run bound to the snapshot id and the manifest, workflow, plan, and
image digests. The image digest keeps the `sha256:` prefix recorded by
`run_job`. The same key and normalized input return that run. A changed
input conflicts. Invalid YAML, an unsupported field, and an unpinned image
create no run. Execution of the queued job is NS-6. Version 0 development
submission is unchanged. `worker.describe` advertises `workflow.job` and does
not advertise actions, needs, secrets, matrices, or services.

Acceptance criteria:

- A new submission version, beside unchanged development `run.submit`,
  captures the repository, verifies the workflow bytes, builds an NS-1 plan,
  and only then commits a queued run bound to that snapshot id, manifest
  digest, workflow digest, plan digest, and image digest.
- The same submission key and the same normalized input return the same run.
  A changed input under that key returns `IDEMPOTENCY_CONFLICT` and does not
  create a second run. Invalid YAML or an unsupported field creates no run.
- Editing the checkout after acceptance does not change the stored digests.
- Disconnect before the reply still leaves the accepted run retrievable.
  `worker.describe` advertises the new capability without claiming the
  rejected ones.

**NS-6. Execute an accepted run through the supervised runtime.**

Status: implemented.

The scheduler claims the oldest queued run. For a workflow job it stores
`running` and an attempt id before materializing the workspace or starting a
container. It runs the accepted plan with `run_job` and the accepted image
pin. Success is `succeeded` with exit code 0 and the accepted digests
unchanged. A nonzero step is `failed` with that exit code. A setup failure
is `failed`, with a null exit code and error kind `SETUP_FAILED`. Step
records are stored on the run. A workflow file larger than 500 KB is
rejected before a run exists. The job time bound recorded on the plan is
`timeout-minutes` (default 360 minutes). Stopping the container at that
bound is NS-8. Stdout and stderr on those records are capped
at 65536 characters, and `run.logs` does not page them yet. Closing the
client does not stop the worker or the container. A cancel that commits
while the job is still queued does not start a container. Stopping a running
container early, and removing a container left by a killed worker, are later
items. Development fixtures are unchanged.

Acceptance criteria:

- The worker creates the NS-3 workspace, records the attempt before launch,
  and runs NS-4. Success requires terminal `succeeded` and `exit_code` 0 for
  those digests. A nonzero step is `failed`. A setup failure is `failed`
  with a structured error and a null exit code.
- Per-step records are persisted. Client disconnect does not stop the
  container. Development fixtures still behave as in M1.
- One representative success fixture and one failure fixture are recorded
  with their digests, image digest, terminal state, and exit code.

**NS-7. Page step logs through `run.logs`.**

Acceptance criteria:

- Stdout and stderr from the subset are stored and returned in bounded
  pages. `end_of_stream` is true only after the run is terminal and the
  bytes are consumed. An empty page while the run is active is not the end.
- Log retrieval does not require reading the whole output. Bytes match the
  container output for the fixture, independent of page size.

**NS-8. Enforce a timeout.**

The job time bound is `timeout-minutes` (default 360 minutes). GitHub-hosted
job execution time is 6 hours and self-hosted job execution time is 5 days
(https://docs.github.com/en/actions/reference/limits).

Acceptance criteria:

- A step or job timeout stops the owned container within a bounded grace
  period. The run ends `failed` or `cancelled` as specified by the timeout
  rule this PR documents. It does not end `succeeded`.
- A run that finishes inside the timeout is unchanged. The timeout value is
  the explicit submission value, not an unstated default that hides a hang.

**NS-9. Cancel owned containers.**

Acceptance criteria:

- Cancelling a queued workflow run records `cancelled` and does not start a
  container.
- Cancelling a running attempt stops the owned container, then records
  `cancelled` only after cleanup is confirmed. If cleanup cannot be shown,
  the run is `lost` and a conflicting attempt is refused.
- The first committed terminal state wins a completion-versus-cancel race.
  Development-fixture cancellation still passes its existing tests.

**NS-10. Reconcile a restart without running the attempt twice.**

Acceptance criteria:

- After a kill during an active workflow attempt, restart marks that attempt
  `lost`, does not launch it again, and still returns it for the original
  submission key.
- Queued workflow runs remain queued and can start after restart. A second
  worker still cannot take the same state directory.
- An unresolved lost attempt blocks a new attempt that would reuse its
  container or workspace identity.

**NS-11. Refuse new work when the disk budget is exhausted.**

Acceptance criteria:

- A configured budget covers state, snapshots, and attempt workspaces. A
  submission that would exceed it returns `STORAGE_FULL`, creates no run,
  and does not consume the submission key.
- Active runs and their evidence are not deleted to make room. The
  development backend's existing full-disk rollback test still passes.

**NS-12. Publish an artifact manifest for the subset.**

Acceptance criteria:

- Files the subset writes under the attempt workspace, and only those files,
  are listed with id, relative path, size, and digest. `run.artifacts` and
  `artifact.read` return pages of that manifest and its bytes.
- Paths outside the workspace are rejected. A run with no artifacts returns
  an empty manifest, not a capability error. Development fixtures still
  report artifacts unsupported.

## After the first path

These are the roadmap's later M2 increments. They are not part of the first
twelve PRs. Each one updates the capability version, rejects anything it
still does not implement, and records local evidence separately from any
future GitHub reference run. None of them is authorized to call the result
GitHub-equivalent.

| Order | PR | Acceptance criteria |
| --- | --- | --- |
| NS-13 | Expression and context evaluator | A dedicated evaluator implements the documented operators, types, and functions used by the subset. Python `eval` is not used. An unavailable context is an error. A missing property of an available context is an empty string. `if` on a step can skip it. |
| NS-14 | Job `needs` and outputs | A selected job includes its dependency closure. Skip and failure propagation match the documented rules under test. A dependency outside the selection fails planning. Outputs that look like secrets are not copied into the next job. |
| NS-15 | Environment files and workflow commands | `GITHUB_ENV`, `GITHUB_OUTPUT`, `GITHUB_PATH`, and the documented commands apply to later steps. `add-mask` masks subsequent logs of that exact string. Deprecated `set-env` and `add-path` stay disabled. |
| NS-16 | One action runtime | Composite, JavaScript, or Docker actions are added one runtime per PR. The action comes from the snapshot or from a recorded digest. A moving ref after acceptance does not change the run. |
| NS-17 | Matrix | Include, exclude, fail-fast, and max-parallel are tested. Unsupported matrix keys fail at plan time. |
| NS-18 | Reusable workflows | `workflow_call` inputs and outputs type-check. Nesting over the documented limit fails. Secrets are not passed implicitly. |
| NS-19 | Service containers | Owned service containers become ready or fail setup. Cancellation and crash cleanup remove them. The Docker socket is not mounted. |
| NS-20 | Sanitized Git metadata | A written design lands before code. The following PR copies only the metadata that design allows, still excludes credentials and remote URLs, and proves a checkout fixture against the captured digest. Until that PR, checkout actions remain an explicit rejection. |

M2 exit evidence is NS-6 through NS-10 plus the captured-input check in NS-5:
representative success and failure, unchanged digests after checkout edits,
no owned container left after cancel, and no silent second execution after
restart. NS-13 through NS-20 can follow that evidence. They are not required
to say the first subset runs.
