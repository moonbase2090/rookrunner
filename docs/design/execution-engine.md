# Owned workflow engine and compatibility plan

Status: owned-engine direction accepted in [decision 0003](../decisions/0003-owned-execution-engine.md).
The component decomposition and sequence below are proposed implementation details.
Parsing, snapshot verification, attempt materialization, the first Bash
subset, version 1 execution of one accepted job, paging of that job's
step stdout and stderr, the job and step timeout bounds, and caller
cancellation of a running container are implemented. If that cleanup cannot
be shown, the run is lost and a new workflow attempt is refused until this
process restarts. Restart removes a container left by a killed worker, or
records that attempt unresolved and does not run it again. A queued workflow
can still start. An unresolved attempt blocks reuse of its container or
workspace identity. A configured disk budget refuses a new submission that
would exceed it. The default is 10 GB per repository of GitHub Actions cache
storage (https://docs.github.com/en/actions/reference/limits), stored as
10 * 1024 * 1024 * 1024 bytes. Active runs and their evidence are kept.
A finished workflow attempt publishes a manifest of the regular files it
wrote under its workspace. `run.artifacts` and `artifact.read` page that
manifest and its bytes. A step `if`, a job `if`, and job outputs are
evaluated by an owned parser. `GITHUB_ENV`, `GITHUB_OUTPUT`, `GITHUB_PATH`,
and stdout workflow commands apply to later steps in the same job. The
selected job runs after the jobs it needs, one at a time, in one
caller-pinned container and one attempt workspace. Workflow `run` and `env`
text stay literal. Local composite actions with `run` steps are read from
the snapshot. A literal job matrix is expanded. A local reusable
workflow is called from that snapshot. `hashFiles`, matrix expressions,
JavaScript actions, Docker actions, remote reusable workflows, and a
secret store stay unsupported. This is not a GitHub-equivalence claim.
The rest of this plan is not.

## Execution path

```mermaid
flowchart LR
    Snapshot[Verified source snapshot] --> Parser[Workflow parser and capability validation]
    Parser --> Plan[Versioned execution plan]
    Plan --> Worker[Durable worker queue]
    Worker --> Scheduler[Job and step scheduler]
    Scheduler --> Runtime[Owned action and container runtime]
    Runtime --> Evidence[Step events, logs, artifacts, cleanup evidence]
```

The parser consumes the captured workflow, not a later checkout version. It
validates the entire submitted workflow under an explicit selection policy before
acceptance. A selected job includes its required dependency closure. Job
selection must never silently omit required work.

An execution plan records workflow/source identities, engine and capability
versions, normalized jobs/steps, event context, environment/shell defaults, image
identities, and limits. Worker acceptance transactionally binds that plan to its
snapshot and idempotency key. Evaluation that depends on prior step outputs occurs
at runtime through the owned expression engine, not by reevaluating mutable inputs.

The runtime owns attempt workspaces, container/process identities, and lifecycle
operations directly. It emits structured events to the worker rather than parsing
another workflow engine's console output. Persist ownership before launch; reconcile
interrupted ownership before accepting conflicting execution. Use disposable test
environments for real execution checks, and do not mount the Docker socket
or host credentials into ordinary job containers unless the worker was
started with `--docker-socket`. That flag exposes the host Docker service
to the job. The job keeps the caller user. Host credentials stay unmounted.

## M2 implementation order

1. **Parse and plan:** choose a maintained YAML parser after provenance review;
   reject duplicate keys, ambiguous unsupported constructs, and unsupported fields.
   Preserve string keys such as `on`, source locations, and expression text.
   Produce a deterministic plan and precise errors without launching work.
2. **Execute the first subset:** verify snapshot content, create a separate attempt
   workspace, and run sequential Linux Bash steps in a digest-identified Docker
   environment. Implement documented shell/default/env/working-directory behavior
   for that subset rather than approximating it silently. Initial local event input
   is explicit; it is not GitHub event delivery.
3. **Supervise durably:** capture-before-acceptance workflow RPC, per-step records,
   bounded logs/artifacts, timeout and cancellation, process/container cleanup,
   crash reconciliation, and disk budgets. Preserve M1 v0 development behavior.
4. **Establish conformance:** paired success/failure/defaults fixtures and captured
   source checks. Compare semantic outcomes and relevant emitted values, not wall
   time or incidental log formatting. Record environment-related differences.

Do not advertise expression positions other than step `if`, job `if`, job
outputs, whole-string expressions in composite `env`, action output
`value`, the calling step's `with`, and `workflow_call` input, default,
and output expressions. Do not advertise JavaScript actions, Docker
actions, remote `uses`, or matrix expressions. Service containers are a
digest-pinned subset and are not a describe capability. Local reusable
workflows are called from the snapshot. Early rejection is temporary capability
status, not a decision to abandon those features.

## Compatibility expansion

| Area | Planned behavior and evidence | Initial status |
| --- | --- | --- |
| Workflow parsing | Actions YAML shape, meaningful diagnostics, bounded parsing, deterministic plans | Implemented for one selected job of sequential `run` steps, plus local composite `uses` expanded from the snapshot |
| Steps and shells | Ordering, script invocation, defaults, environment precedence, working directories | Implemented for the Linux Bash subset in one caller-pinned container. Process env is workflow, job, earlier `GITHUB_ENV`, then step env. Not a GitHub-equivalence claim |
| Conditions and expressions | Own parser/evaluator; types, coercion, contexts, functions, status checks; never Python `eval` | Implemented for step `if`, job `if`, and job output expressions. `run` and `env` stay literal. `case` matches the expression reference and does not evaluate a branch it does not take. `env.MY-VAR` is the property `MY-VAR`, not subtraction. Not a GitHub-equivalence claim |
| Job dependencies | `needs`, outputs, failure/skip propagation, selected dependency closure | Implemented for the selected job's `needs` closure. Jobs run one at a time in one container and one workspace. A dependency outside the workflow fails planning. An output expression that reads `secrets` is omitted. Not a GitHub-equivalence claim |
| Runtime communication | `GITHUB_ENV`, `GITHUB_OUTPUT`, `GITHUB_PATH`, state files, workflow commands and masking | Implemented for the three files and documented stdout commands, including `add-mask`, for later steps in the same job. `set-env` and `add-path` are disabled. State files and job summaries are not. Not a GitHub-equivalence claim |
| Action types | Composite, JavaScript, and Docker actions; inputs/outputs; setup/main/post lifecycle | Implemented for local composite actions whose steps are `run` steps, loaded from the snapshot (`./` and `$/`). The plan records a digest of the parsed action and the run executes that plan. JavaScript and Docker actions are not implemented. Nested `uses` is not implemented. Not a GitHub-equivalence claim |
| Strategies | Matrix expansion, include/exclude, fail-fast, max-parallel, concurrency | Implemented for a literal matrix. Include, exclude, fail-fast, and max-parallel follow the workflow syntax page. A matrix over 256 jobs is rejected. Combinations run one at a time in the one caller-pinned container. Matrix expressions and `continue-on-error` are not implemented. Not a GitHub-equivalence claim |
| Reuse | Reusable workflows, typed inputs, outputs, nesting, explicit secret handling | Implemented for a local `workflow_call` read from the snapshot (`./` and `$/`). Inputs are boolean, number, or string. Nesting stops at ten workflows. Fifty unique called workflows is the maximum. Secrets are not passed. Remote reusable workflows are not implemented. Not a GitHub-equivalence claim |
| Repository behavior | Sanitized Git metadata and checkout semantics consistent with identified source | Capture writes a sibling `git.json` with the base commit, dirty flag, object format, and a local branch name. Credentials and remote URLs are excluded. Checkout execution is not implemented. Checkout actions stay rejected. |
| Services and artifacts | Owned service containers, readiness, cache/artifact interfaces and cleanup | Service containers are implemented for a digest-pinned image, env, command, and entrypoint. The service joins an owned user-defined bridge network and is removed on completion, cancel, and restart. It does not receive the engine socket. `credentials`, `volumes`, `options`, and `ports` are not implemented. Cache beyond the existing artifact manifest is not implemented. Not a GitHub-equivalence claim |
| Hosted capabilities | Token permissions, OIDC, environments/approvals, event delivery, runner labels/images | Requires explicit integration; no local equivalence claim |
| Platforms | Linux first, then separately validated shell/OS/architecture implementations | No real workflow platform validated |

The order after the initial M2 path is provisional; compatibility findings and
real project workflows should determine priority. Secrets remain disabled until
an explicit provisioning and isolation design is implemented.

## Conformance evidence

For each capability, retain its reference documentation, fixture workflow, explicit
inputs, expected outputs/conclusions, local evidence, and any GitHub reference run
identity with runner/action versions. Reference runs are a future validation step;
none have been dispatched or inferred from local tests. Publishing a fixture or
dispatching GitHub Actions is a separate external action requiring authorization.

A locally validated feature remains labeled as such until a reference comparison
exists. Document intentional local differences and reject workflows that require
unavailable hosted capabilities. A workflow passing locally establishes the result
under the recorded capability set, not universal GitHub equivalence.

## Specification references

Reviewed 2026-09-23; references are moving documentation, so each conformance record
must record the behavior/date it was built against:

- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [Expressions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions)
- [Workflow commands and environment files](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands)
- [Action metadata and runtimes](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax)

These are behavioral references. No upstream engine source or binaries have been
copied. Source provenance and licenses must be reviewed before any later import.

A proposed survey of open-source runners, and the requirement tiers that survey
implies, is in
[actions runner requirements](../research/actions-runner-requirements.md).
That note is research. It is not accepted scope.
