# Open-source Actions runners: requirements for a first-class engine

Status: proposed research, 2026-09-23. Author: grok-rr. Reviewed by maintainer the same day. Still not accepted scope.

This note is not accepted scope. It does not change
[decision 0003](../decisions/0003-owned-execution-engine.md), the
[engine plan](../design/execution-engine.md), or the roadmap.
No runner source or binary was imported.

## Question

What must Rookrunner cover before its owned engine is first-class for GitHub Actions workflows?

## Answer

A first-class local engine owns two roles that GitHub splits across a service and a runner.

1. **Plan.** Parse the workflow, select jobs, evaluate job-level expressions, expand matrices and reusable workflows, build contexts, and resolve actions.
2. **Execute.** Run one planned job: steps, shells, action runtimes, containers, workflow commands, and cleanup.

`actions/runner` implements execution. GitHub's planning service is proprietary. Local engines reimplement planning because they have no GitHub service. Rookrunner already accepted ownership of both roles. The list below ranks the behaviors those projects had to cover before real workflows ran.

GitHub's public docs and small reference fixtures remain the specification. Another engine's behavior is evidence of scope, not the compatibility authority.

## Evidence classes

Three labels are used below.

- **Documented.** Fetched this session from GitHub Docs, the GitHub changelog, or the named project's own README or docs.
- **Reported.** Described by a secondary write-up or by excerpts of runner source. The runner was not executed here.
- **Judgment.** A Rookrunner requirement ranking. It is not upstream specification.

No claim in this note is from a Rookrunner execution test. No GitHub reference run was dispatched.

## Projects surveyed

| Project | Role | What it shows | License observed |
| --- | --- | --- | --- |
| [actions/runner](https://github.com/actions/runner) | Job executor for github.com | Listener, worker, step lifecycle, action handlers, log and result reporting | MIT |
| [actions/runner-images](https://github.com/actions/runner-images) | Hosted VM images | Tooling on `ubuntu-latest`. Separate from engine semantics | MIT |
| [nektos/act](https://github.com/nektos/act) | Local planner and executor | The feature set a local engine needs, and the gaps that still break workflows | MIT |
| [Forgejo Runner](https://code.forgejo.org/forgejo/runner) | Forgejo job executor, act lineage | Docker, LXC, and host execution. Forgejo documents familiarity, not GitHub compatibility | README copies state MIT, plus Apache-2.0 under `act/container`. The license file was not fetched (Codeberg bot check) |
| [ChristopherHX/runner.server](https://github.com/ChristopherHX/runner.server) | Local Actions service plus official runner | Open reimplementation of planning, matrix, reusable workflows, contexts, secrets, cache, and artifacts | MIT fork of `actions/runner` |
| [ChristopherHX/github-act-runner](https://github.com/ChristopherHX/github-act-runner) | GitHub-registered runner | Same broker protocol as `actions/runner`, with act executing steps | See that repository before any use |

`runner.server` is the clearest map of the proprietary service. It lists these reimplemented parts: runner management, job parsing and scheduling, matrix evaluation, callable workflows, `on` filters, `github` / `needs` / `matrix` / `strategy` / `inputs` contexts, job inputs and outputs, secrets, cache, and artifacts. Expression evaluation and step execution stay with the runner.

## How a GitHub job actually runs

**Documented.** [actions/runner README](https://github.com/actions/runner/blob/main/README.md): the runner runs a job from a GitHub Actions workflow, on GitHub-hosted images or on a self-hosted machine. [Workflow commands](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands) and [expressions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions) define the step contract the worker must honor. Read 2026-09-23.

**Reported.** [Depot, runner listener](https://depot.dev/blog/github-actions-runner-architecture-part-1-the-listener) (2025-08-15), plus public excerpts of `src/Runner.Listener/JobDispatcher.cs` and `src/Runner.Worker/JobRunner.cs`. This session did not run the runner.

1. The runner registers and opens a session.
2. The listener long-polls the broker.
3. A `RunnerJobRequest` carries a run-service URL and a request id. The runner then acquires the job. Depot reports an acquire budget of about two minutes.
4. The listener starts a worker process and sends an `AgentJobRequestMessage`.
5. That message contains steps, variables, resources, and context data. Workflow planning happens before the worker starts. Composite expansion and action download still happen in the worker. This note does not claim that every `uses` resolution is finished inside the service.
6. The worker runs main steps, then post-steps. Write-ups of `StepsRunner` show post-steps taken from a stack, so they run in reverse order. That reverse order is reported, not pinned to a commit.
7. The worker reports the result, outputs, step results, and annotations.
8. Containers and the workspace are removed. Cancellation arrives as a separate broker message.

The job message fields that matter for a later adapter are `Steps`, `Variables`, `Resources`, `ContextData`, the timeline, and the completion payload (result, outputs, step results, annotations). Those names identify types in `src/Runner.Listener/JobDispatcher.cs` and `src/Runner.Worker/JobRunner.cs` on the default branch, as seen 2026-09-23. No commit SHA is recorded. Identified paths are not implementation evidence. Record a commit before treating that source as evidence. The list was not copied from a captured GitHub job.

Rookrunner's execution plan should stay mappable to one job request. The GitHub broker protocol itself stays a later integration. The roadmap already lists managed GitHub runners as a separate investigation.

## Requirement tiers

**Judgment.** The tiers are a proposed ranking for Rookrunner. Upstream docs define the behaviors. They do not define these tiers.

### Tier A — first real job

The [engine plan](../design/execution-engine.md) already assigns this to the first M2 slice. The survey agrees. These behaviors are the floor.

| ID | Requirement | Evidence to keep |
| --- | --- | --- |
| ER-01 | Parse Actions YAML. Preserve the string key `on`. Reject duplicate keys. Reject unsupported execution-affecting fields with a location and a capability error before acceptance | Parser fixtures for valid, duplicate, and unknown fields |
| ER-02 | Run sequential Linux bash steps in a digest-identified container. Apply documented shell flags, `defaults.run`, env precedence, and `working-directory` | Paired success and failure fixtures |
| ER-03 | Bind the attempt workspace to the accepted snapshot. Later edits to the checkout do not change the run input | Existing capture checks plus an execution check |
| ER-04 | Supervise timeout, cancellation, cleanup, and restart. `succeeded` requires exit code 0. `failed`, `cancelled`, and `lost` stay distinct | M1 recovery checks plus a real step |

### Tier B — workflows from real repositories

act, Forgejo, and `runner.server` all hit this set before ordinary CI workflows run. act still leaves several of these unfinished. A first-class engine has to implement them or reject them before execution.

| ID | Requirement | Why it blocks real workflows |
| --- | --- | --- |
| ER-05 | Expression evaluator. Literals, indexing, property access, `!`, comparisons, `==`, `!=`, `&&`, `\|\|`. Case-insensitive string compare. Loose equality with the documented numeric coercion. Functions: `contains`, `startsWith`, `endsWith`, `format`, `join`, `toJSON`, `fromJSON`, `hashFiles`, `case`. Status functions: `success`, `failure`, `always`, `cancelled`. Object filters (`*`). Default `if` is `success()`. Never use Python `eval`. Evaluate each expression in the phase and context set of its workflow key | `if`, `env`, and `with` are expressions in almost every workflow. **Documented** in the [expressions reference](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions) |
| ER-06 | Contexts: `github`, `env`, `vars`, `job`, `jobs`, `steps`, `runner`, `secrets`, `strategy`, `matrix`, `needs`, `inputs`. Availability is per workflow key, including `hashFiles` only where the table allows it. `jobs.<job_id>.if` may use `github`, `needs`, `vars`, `inputs`, and the status functions. It may not use `matrix` or `strategy`, because that `if` is evaluated before matrix expansion. A missing property of an available context is an empty string. An unavailable context or function is an error. Do not invent local `github` values for properties the supplied event does not define | **Documented** in the [contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts): context-availability table, and the empty-string rule for a nonexistent property. act's incomplete `github` context is a competitor gap, not a model |
| ER-07 | Job graph: `needs`, job outputs, `if`, and failure, skip, and cancel propagation. A selected job includes its dependency closure | Multi-job workflows are the common shape |
| ER-08 | Matrix expansion with `include`, `exclude`, `fail-fast`, and `max-parallel` | Forgejo Runner 13.0.0 (2026-08-03) rejects an `exclude` key that the matrix does not declare. That is competitor behavior. A GitHub rule for the same case was not verified here. Adopt it only after a GitHub citation or a reference fixture |
| ER-09 | Step `id`, `if`, `continue-on-error`, and `timeout-minutes`. Record `steps.<id>.outcome` before `continue-on-error` and `steps.<id>.conclusion` after it. A tolerated failure has outcome `failure` and conclusion `success`. Values are `success`, `failure`, `cancelled`, and `skipped`. Define job-level `continue-on-error` with a fixture: the workflow can pass when that job fails | **Documented** for the step pair in the [steps context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#steps-context). act still ignores job `timeout-minutes`, job `continue-on-error`, and step cancellation |
| ER-10 | Per-step files: `GITHUB_ENV`, `GITHUB_PATH`, `GITHUB_OUTPUT`, `GITHUB_STATE`, `GITHUB_STEP_SUMMARY`, `GITHUB_ARTIFACTS`. `GITHUB_ARTIFACTS_LIST` is the job-level aggregate. Apply writes at the step boundary. The writing step does not see its own `GITHUB_ENV` update. Support the multiline delimiter and UTF-8. `GITHUB_ENV` cannot set `NODE_OPTIONS`. Do not overwrite `GITHUB_*` or `RUNNER_*`. Bound output size. Keep log redaction, step-output handling, and job-output rejection separate. See Masking | **Documented** file paths in [workflow commands](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands) and the `github.env` / `github.path` / `github.artifacts` properties. Command reassembly across log-chunk boundaries is an implementation requirement. It is unverified against a GitHub fixture |
| ER-11 | Workflow commands: `debug`, `notice`, `warning`, `error`, `group`, `endgroup`, `add-mask`, `stop-commands`, `echo`. Names are case insensitive. Honor `stop-commands` until the same token resumes parsing. Keep deprecated `set-env` and `add-path` disabled. `add-mask` registers a log mask. It does not by itself define step-output or job-output policy | **Documented** command list and `stop-commands` behavior. Masking details are in the Masking section |
| ER-12 | Action resolution for `owner/repo@ref`, `./path`, `docker://image`, and the `$/` same-repository form where supported. Record the resolved digest. Load the action from the snapshot or from that digest. A moving ref after acceptance does not change the run | Remote and local actions are the normal `uses` forms. `$/` is **documented** in the [metadata syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax) |
| ER-13 | Action runtimes, with separate input rules. JavaScript and Docker actions receive `INPUT_<NAME>`. Composite actions do not. They read the `inputs` context. `required: true` does not by itself fail a missing input. Docker actions see inputs only through `args`. JavaScript `pre` / `main` / `post`, with `pre-if` and `post-if` defaulting to `always()` against the job status. `runs.pre` is not supported for local actions. Docker uses `pre-entrypoint` and `post-entrypoint` in separate containers. Run `post` on the way out, including after failure, subject to `post-if`. Keep `STATE_*` inside the action that wrote it. Provision Node 24 for `runs.using: node24`. `node20` remains in the metadata syntax while hosted runners no longer provide Node 20. Legacy `runs.using` values need an explicit policy and fixture. Do not silently remap them | **Documented** input and lifecycle rules in the metadata syntax. Node 20 retirement: [GitHub changelog, 2026-09-23](https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/). act's gap is that the job image must already have Node on `PATH` |
| ER-14 | Checkout behavior that preserves the snapshot digest and sanitized Git metadata. Do not copy credentials from the user Git config. Pair this with ER-12 and ER-13. A checkout, setup, and build workflow needs it as soon as actions run | `actions/checkout` is the first step of most workflows. Priority is **judgment** from maintainer's review |

### Masking

Three documented behaviors. They are not one rule.

1. **Log redaction.** `add-mask` registers a value once per job. Later log text replaces each registered word, split on whitespace, with `*`. Register the mask before printing the value. Source: [workflow commands](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands), "Masking a value in a log", read 2026-09-23. The [toolkit command doc](https://github.com/actions/toolkit/blob/main/docs/commands.md) says future logs are masked, and a multiline value must be escaped or each line is not covered. Whether lines already emitted are rewritten is not stated on the GitHub doc page. That case is unverified.
2. **Step outputs.** The same workflow-commands page says that after you mask a value, you cannot set that value as an output. The same page also shows a within-job example that writes the value to `GITHUB_OUTPUT` after `add-mask` and reads `steps.<id>.outputs`. Both statements are on the page. A fixture must show which one the runner does. This note does not choose between them.
3. **Job outputs.** [Workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idoutputs) says job outputs that contain secrets are redacted on the runner and are not sent to GitHub Actions. The documented warning is `Skip output '{output.Key}' since it may contain secret.` The documented way to pass a secret to a later job is an external store, not a job output.

Registered masks do not cover every encoding of a secret. The product promise already excludes universal log redaction.

### Tier C — broad compatibility

These are the gaps act and Forgejo still document, and the services `runner.server` had to rebuild. Each one needs its own capability version and fixtures.

| ID | Requirement | Current open-source gap |
| --- | --- | --- |
| ER-15 | Reusable workflows: `workflow_call`, typed inputs, explicit secrets, outputs, and the documented nesting limit | `runner.server` treats this as service work |
| ER-16 | Service containers, job `container:`, user-defined networks, published ports, and readiness. Cleanup on cancel and crash | act's older docs still mark services incomplete. Forgejo runs services inside its job container model |
| ER-17 | Two separate artifact surfaces. `GITHUB_ARTIFACTS` declares file and OCI metadata. `GITHUB_ARTIFACTS_LIST` reads that metadata back. The HTTP upload, download, and cache APIs used by `actions/upload-artifact` and `actions/cache` are a different capability. Supporting the file does not establish those APIs. `cache-mode` changes cache credentials. This session confirmed the `read` value in workflow syntax and did not copy the full value table. Leave `cache-mode` unsupported until the cache capability exists | **Documented** file protocol in workflow commands. `cache-mode` is **documented** in workflow syntax. `runner.server` exposes a local cache and artifact service for the HTTP half |
| ER-18 | `permissions` and `GITHUB_TOKEN` scopes. Until that design exists, reject workflows whose permissions would change execution. Secrets stay disabled until the accepted provisioning design lands | act ignores `job.permissions`. Forgejo ignores `permissions` |
| ER-19 | OIDC token endpoint (`ACTIONS_ID_TOKEN_REQUEST_URL` and token). Cloud federation is a separate tested integration | act leaves the OpenID Connect URL undefined. Forgejo uses its own `enable-openid-connect` key |
| ER-20 | `concurrency`, `cancel-in-progress`, and `queue` (`single` or `max`), defined for a single local worker. `queue: max` combined with `cancel-in-progress: true` is a validation error | **Documented** in workflow syntax. act ignores `concurrency` |
| ER-21 | Deployment `environment` and environment-scoped secrets | act ignores `job.environment` |
| ER-22 | Annotations, problem matchers, and job-summary rendering | act ignores annotations, problem matchers, and step summaries |
| ER-23 | `runs-on` mapped to an explicit image digest. Record the digest on the run. Tool parity with `actions/runner-images` is a separate environment milestone | act and Forgejo default images are intentionally smaller than GitHub's Ubuntu image. Docker does not provide systemd |
| ER-24 | Step scheduling keys `background`, `wait`, `wait-all`, `cancel`, and `parallel`. They change when outputs become visible, when failure is reported, and when post-job cleanup starts. An implicit `wait-all` runs before post-job cleanup. A job allows 10 concurrent background steps. The first sequential subset rejects these keys before acceptance | **Documented** in [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsbackground), read 2026-09-23. Composite actions cannot declare `background` steps |

### Tier D — hosted integration

Keep these off the local engine milestone. The architecture already names them as later work.

- Broker registration, `RunnerJobRequest`, job acquire, and job complete against github.com.
- An adapter that hosts the official `actions/runner` for GitHub-dispatched jobs.
- GitHub event delivery, check-run UI, and deployment approvals.
- macOS and Windows shells and images.
- A runner fleet, labels, and scheduling across machines.

Linux host prerequisites for the official runner (libicu, OpenSSL, Kerberos, zlib, LTTng) matter only if Rookrunner later shells out to that runner. They are not prerequisites for the owned engine.

## Isolation choices the survey supports

Forgejo maps each `runs-on` label to `docker://`, `lxc://`, or `host://`. Docker runs steps as root in a container. LXC is their option when the job must run Docker without the host socket. Host mode runs steps in a shell on the runner machine.

Rookrunner's accepted local runtime is a supervised container. The architecture already refuses to mount the Docker socket into ordinary job containers. That matches Forgejo's later security stance. A nested-Docker or systemd job needs an explicit isolation design before it is advertised.

## Proposed order after the first slice

**Judgment.** The M2 slice stays Tier A. maintainer's review reordered the next increments:

1. ER-05 and ER-06 together. Expressions carry the context-availability table, including job `if` before matrix expansion.
2. ER-09 outcome and conclusion, with ER-10 and ER-11 file protocol and workflow commands.
3. ER-07 job graph.
4. ER-12, ER-13, and ER-14 together. Checkout, action resolution, and Node 24 land before matrix expansion. A checkout plus setup plus build workflow needs that group immediately.
5. ER-08 matrix, after a GitHub citation or fixture for unknown `exclude` keys.
6. ER-15 through ER-17 reusable workflows, services, and the separate artifact and cache APIs.
7. ER-18 through ER-23 tokens, OIDC, concurrency, environments, and annotations.
8. ER-24 stays rejected until a scheduler supports it.

This order is a proposal. Real workflows can reorder it. Each increment still fails closed on unsupported execution-affecting syntax.

## Gap against the current tree

| Area | State on 2026-09-23 |
| --- | --- |
| Durable worker, protocol, cancellation, restart | Implemented for the development backend |
| Source snapshot | Implemented. Execution does not consume it yet |
| YAML planning, expressions, steps, actions, containers | Not implemented |
| GitHub reference runs | None recorded |

## Provenance

Use these repositories as behavior references. Review license and source provenance again before any import. Decision 0003 already forbids using act as the engine or as a silent fallback.

## Review record

The first draft was reviewed and the corrections were recorded. This revision folds that review.

Accepted into the note:

- Pair snapshot checkout with action resolution, before matrix expansion.
- Bind each expression to the context set and phase of its workflow key.
- Record step `outcome` and `conclusion` as different values.
- Keep composite actions off automatic `INPUT_*` variables. `required: true` does not fail a missing input by itself.
- Reject `background`, `wait`, `wait-all`, `cancel`, and `parallel` before acceptance in the sequential subset.
- Treat file-command parsing, step-boundary visibility, and best-effort masking as their own requirements.
- Keep `GITHUB_ARTIFACTS` separate from upload and cache HTTP APIs.
- Label the Forgejo unknown-key matrix rule as competitor behavior until a GitHub source exists.
- Cite the Node 20 changelog on ER-13, and require a separate policy for legacy `runs.using` values.
- Stop claiming that action resolution happens only in the service. Identify runner file paths. Mark that boundary reported.

The revision was reviewed. This pass applies that review.

- The runner paths identify files. They are not implementation evidence until a commit SHA is recorded.
- Log redaction, step-output propagation, and job-output rejection are separate documented behaviors. The within-job step-output sentences on the workflow-commands page disagree. That conflict stays unresolved until a fixture.

Still unverified: GitHub's exact diagnostic for an unavailable context, job-level `continue-on-error` aggregation beyond the documented step pair, command reassembly across log chunks, the unknown-key matrix rule, whether `add-mask` rewrites lines already emitted, and which of the two step-output sentences the runner follows.

Those two follow-ups were reviewed. That close covers the research note only. It does not validate runtime behavior or change accepted scope. The unverified fixture questions stay open for implementation.
