# Rookrunner — product requirements

Status: draft product requirements, revised against the implemented M1 contract
and the M2 capture slice. M1 is complete for the development backend. M2 has a
preparatory source capture command and an accepted owned-engine direction. Real
workflow execution is not implemented. See the
[manifesto](manifesto.md) for why the project exists.

Accepted direction:

- Begin with a downloadable local tool, worker agent, and documented execution protocol.
- Build a useful product before developing a paid service.
- Develop in an independent repository, outside Local Actions.
- Use Rookrunner as the working title.
- Own the workflow execution engine and pursue broad GitHub Actions compatibility
  through explicit, tested increments. Do not delegate execution to act.

Implemented behavior is the M1
[development contract](design/development-contract.md), the v0
[schemas](../schemas/v0/README.md), and the M2
[capture contract](design/source-capture.md). Decisions
[0001](decisions/0001-executable-foundation.md),
[0002](decisions/0002-m2-capture-and-backend.md) (capture only; the act pin is
historical), and [0003](decisions/0003-owned-execution-engine.md) record what
has been accepted. Other architecture below is still a proposal.

## Problem

Developers and coding agents need reliable build and test results while source
files are changing. Commands launched inside a client session can lose their
visible history or ownership when that session exits. Local and remote
execution often expose different controls and result formats. An exit code
alone does not identify which source, workflow, and environment produced the
result.

## Users

| User | Need | Completion signal |
| --- | --- | --- |
| Developer | Run a repository's build or tests and inspect failures | Can install, submit, follow, and inspect a completed run |
| Coding agent | Submit work without holding a tool connection open | Receives a run ID and can reconnect for structured results |
| Machine owner | Control resource use and stop work | Can inspect the queue, set concurrency, and verify cancellation |

The same run identifiers, states, logs, and errors serve the developer and the
coding agent. A dashboard is a later view of that same contract, not a second
source of truth.

## Goals

- Submit work, follow it, and inspect a result tied to identifiable inputs.
- Use the same controls whether a human or a coding agent submits the work.
- Preserve enough evidence to investigate failures and attempt reproduction.
  Reproduction depends on external services, dependencies, and environment
  differences. It is not a bit-for-bit guarantee.
- Keep accepted work when the client disconnects. Retrying the same submission
  does not create a second run.
- Execute the source that was submitted, not a later edit of the checkout.
- Report success only when the run reached `succeeded` and `exit_code` is 0 for
  that identified input. Cancellation, failure, and interrupted execution stay
  distinguishable.
- Own workflow semantics and match documented GitHub Actions behavior in
  tested increments, rejecting unsupported execution-affecting syntax before
  acceptance.
- Ship a local release that runs outside this checkout, with checksums,
  dependency instructions, version information, and third-party notices.

## Non-goals

These are outside the first usable release. Some are later investigations, not
rejected ideas.

- Public managed workers, billing, customer accounts, and service availability guarantees.
- A network listener, worker registration, or execution for unrelated customers.
- Replacing GitHub's workflow service, or claiming Actions feature parity.
- A new mandatory workflow language.
- Using act, or any other engine, as the execution implementation or as a silent fallback.
- Cross-machine scheduling, automatic capacity expansion, shared caches, and global fairness.
- Native desktop packaging and broad operating-system support.
- Universal secret detection, universal log redaction, and secret provisioning.
  Secrets stay disabled until a reviewed design exists.
- Importing Local Actions configuration, credentials, state, sockets, or command names.
- Treating a local Docker run of a trusted repository as a sandbox for hostile code.

## What the code does now

`execution_core` 0.0.1 is a local prototype invoked as
`PYTHONPATH=src python3 -m execution_core`. It is not an installed public CLI.
The runtime uses the Python standard library. Linux is the implementation
target. Recorded M1 and M2 evidence used Python 3.14.6. The declared minimum
is 3.11. Other versions and operating systems are not release evidence.

Implemented:

- A user-owned Unix socket and SQLite state for one bound repository.
- Protocol version 0 methods: `worker.describe`, `run.submit`, `run.get`,
  `run.list`, `run.logs`, `run.artifacts`, `artifact.read`, `run.cancel`,
  and `run.status`.
  The CLI submits an explicit `development` fixture, or a version 1 workflow
  job with `--workflow`, `--job-id`, and `--event`. `--image` is optional when the worker was started with `--runner-image`. `follow` polls
  that run's status and log pages until the run is terminal. Fixture text is
  never executed.
- Durable submission keys, bounded queue and log pages, cancellation races,
  and restart behavior that keeps queued fixtures and marks an interrupted
  active fixture `lost`.
- Restart marks an interrupted workflow attempt `lost` and does not run it
  again. The same submission key returns that run. Restart removes the
  container recorded for that attempt, or leaves the attempt unresolved
  without reusing its container or workspace. Queued workflow runs can start.
- A configured disk budget covers state, snapshots, and attempt workspaces.
  The default is 10 GB per repository, GitHub Actions cache storage
  (https://docs.github.com/en/actions/reference/limits), stored as
  `10 * 1024 * 1024 * 1024` bytes. A submission that would exceed it returns
  `STORAGE_FULL` and does not consume the submission key. Active runs and
  their evidence are kept.
- A `snapshot` command that captures Git working files into the state
  directory. It does not submit a run.
- A planner reads one selected job of sequential `run` steps. Snapshot
  verification and attempt materialization rebuild a private workspace from
  a captured digest. A remote action pinned by 40 lowercase hexadecimal
  characters is fetched before acceptance and stored under its content
  digest. A remote composite's `run` steps execute from that plan.
  A remote `node24` action with `main` runs as one step from a copy
  mounted read-write at `/actions`. Its `post` runs after the job's
  main steps when that main ran. The store is not mounted. `pre`,
  `node20`, and Docker actions are still rejected.
  `worker --node24 DIR` mounts that directory read-only at
  `/opt/node24` and does not put it on `PATH`. `worker.describe`
  reports its digest only when the flag is set. A run that uses the
  mount records that digest and the version `node --version` prints.
  The worker does not download Node. The capability version stays 12.
  A version 11 plan is not migrated.
- Version 1 `run.submit` accepts that job only after capture, verification,
  and planning. The worker records an attempt, then executes it with `run_job`
  in one caller-pinned container. Success is `succeeded` and exit code 0. A
  nonzero step is `failed`. A setup failure is `failed`, with a null exit
  code and a structured error. The job container uses Docker network `bridge`
  by default, so it can reach the public internet. GitHub-hosted runners have
  that access by default
  (https://docs.github.com/en/actions/concepts/runners/private-networking).
  `worker --network none` turns it off. The Docker socket stays unmounted
  unless the worker is started with `--docker-socket`. That mount gives the
  job the host Docker service. The job keeps the caller user. When the
  socket is mounted, a private Docker volume is mounted into the job at
  that volume's mountpoint, and `TMPDIR`, `TEMP`, and `TMP` default to
  it. A later env layer can replace those three. The volume is removed
  with the job container. The host `/tmp` is not mounted. The image
  must already contain the Docker client. GitHub requires that service to be installed
  and running for container-dependent work on a self-hosted runner
  (https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners).
  Host credential directories are not mounted. The container is not privileged.
- A finished workflow attempt publishes a manifest of the regular files it
  wrote under its workspace. `run.artifacts` and `artifact.read` page that
  manifest and its bytes. A workflow run with no such files returns an empty
  manifest. Development fixtures still reject artifact requests. There is no
  doctor command, MCP adapter, dashboard, or packaging.
- Push and pull request checks run ruff and the unit test suite.

Validation records live in [M1 completion](validation/m1-completion.md) and
[M2 capture](validation/m2-capture.md). Those records are historical. They do
not make the requirements below done.

## Scope by milestone

Milestones are outcome gates, not dates. Detail and exit evidence are in the
[roadmap](roadmap.md). Live ticket status is outside this document.

| Milestone | Scope | State |
| --- | --- | --- |
| M0 — Project foundation | Independent repository, PRD, architecture, protocol sketch | Documents exist. The workflow protocol in [protocol.md](design/protocol.md) is still a draft. |
| M1 — Executable contract | CLI, local worker, persistence, schemas, protocol checks on a development backend | Complete for synthetic fixtures. Not workflow compatibility. |
| M2 — Real workflow execution | Owned parser and planner, capture bound to acceptance, supervised container lifecycle | Capture, planning, snapshot verification, attempt materialization, digest-pinned Bash execution, version 1 execution of one accepted job, paging of that job's step stdout and stderr, job and step timeouts, caller cancellation of a running container, restart reconciliation of a container left by a killed worker, a disk budget that refuses a new submission when state, snapshots, and attempt workspaces would exceed it, an artifact manifest of files a workflow attempt writes under its workspace, evaluation of step `if` on that job, the selected job's `needs` closure and job outputs, and environment files and stdout workflow commands for later steps in the same job are implemented. Local composite actions whose steps are `run` steps are loaded from the snapshot. Expressions in workflow `run`, `env`, `with`, and step and job `name` are evaluated, including mixed text. A concurrency group is enforced on this one worker. With `queue: max`, at most 100 runs can be pending in a group. NS-46 names selected workspace files in the artifact manifest and records one local CodeQL SARIF file ([upload artifact](design/upload-artifact.md)). The capability version stays 12. P4 accepts `actions/checkout@v` plus digits as an owned checkout and copies ancestor history when that plan sets `fetch-depth` to the YAML integer `0` ([checkout tag](design/checkout-tag.md)). Omitting `fetch-depth` keeps one parentless synthesized commit and still excludes the original commit. `.github/workflows/check.yml` is unchanged. P6 runs the operator-built image when `worker --runner-image` is set and `image` is omitted and every selected job is literal `runs-on: ubuntu-latest` ([runner image](design/runner-image.md)). An explicit `image` still wins. Any other image stays a caller pin. `run_job` still requires a digest and still does not select a default. P5 posts one check run through the Rookrunner GitHub App when `--app-key` is set, then posts the commit status with that installation token. Omitting `--app-key` keeps the NS-40 token file. The plan schema is unchanged. P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions for owner-repository push and pull-request runs on this one local worker . p7-mask, p7-trust-gate, p7-socket-lock, p7-secret-env, p7-secret-run, and p7-job-token are implemented ([secrets](design/secrets.md)). MB2090 accepted the amended answers on 2026-10-05. The rest of the provisional list is not numbered yet. A remote node24 main runs from a copy in the attempt. pre, post, node20, and Docker actions are not implemented. Local reusable workflows from the snapshot are called by a job `uses`. A job may run digest-pinned service containers. Those containers do not receive the engine socket. |
| M3 — Agent and human access | MCP adapter, dashboard, bounded evidence retrieval through the same protocol | Not started. |
| M4 — Downloadable preview | License, public names, packaged artifact, P01–P12 on a clean machine | Not started. |

M2's first executable subset is a starting point: an explicitly selected job,
sequential steps, and a digest-pinned image, with unsupported syntax rejected.
A step `if` and a job `if` are evaluated. The worker runs the selected job
and the jobs it needs, one at a time, in one caller-pinned container.
Expressions in workflow `run`, `env`, `with`, and step and job `name` are evaluated, including mixed text. A concurrency group is enforced on this one worker. With `queue: max`, at most 100 runs can be pending in a group. NS-46 names selected workspace files in the artifact manifest and records one local CodeQL SARIF file ([upload artifact](design/upload-artifact.md)). The capability version stays 12. P4 accepts `actions/checkout@v` plus digits as an owned checkout and copies ancestor history when that plan sets `fetch-depth` to the YAML integer `0` ([checkout tag](design/checkout-tag.md)). Omitting `fetch-depth` keeps one parentless synthesized commit and still excludes the original commit. `.github/workflows/check.yml` is unchanged. P6 runs the operator-built image when `worker --runner-image` is set and `image` is omitted and every selected job is literal `runs-on: ubuntu-latest` ([runner image](design/runner-image.md)). An explicit `image` still wins. Any other image stays a caller pin. `run_job` still requires a digest and still does not select a default. P5 posts one check run through the Rookrunner GitHub App when `--app-key` is set, then posts the commit status with that installation token. Omitting `--app-key` keeps the NS-40 token file. The plan schema is unchanged. P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions for owner-repository push and pull-request runs on this one local worker . p7-mask, p7-trust-gate, p7-socket-lock, p7-secret-env, p7-secret-run, and p7-job-token are implemented ([secrets](design/secrets.md)). MB2090 accepted the amended answers on 2026-10-05. The rest of the provisional list is not numbered yet. A composite `run`
is evaluated. Environment files and workflow commands for later steps in
the same job are implemented. Local composite actions from the snapshot are
implemented. The differences are in the [next steps](planning/next-steps.md).
A local reusable workflow is called from a job `uses` in the same
snapshot. Inputs are boolean, number, or string. Secrets are not passed.
A remote `node24` `main` and its `post` are implemented. `pre`, `node20`, and Docker
actions remain later increments of the same engine. Those later increments are not promised in the first slice, and
they are not permanently excluded. An owned checkout accepts
`uses: actions/checkout@v4` and `actions/checkout@v` plus digits, and does not replace the captured files. A job may run service containers from
a digest-pinned image. They do not receive the engine socket. A literal
job matrix, with
include, exclude, fail-fast, and max-parallel, runs one combination at a
time in the same container.

## First usable release

Proposed support target: Linux x86_64 with Docker available. Additional
platforms require separate evidence.

- A CLI and a worker that run independently of this checkout.
- One worker bound to one explicitly selected repository. One running job by default.
- GitHub Actions workflow input through Rookrunner's own engine, with a declared
  supported subset and explicit rejection of unsupported execution behavior.
- Versioned local communication over a user-owned Unix socket.
- Durable run state, bounded log retrieval, explicit cancellation, and restart recovery.
- Source capture so queued jobs execute their submitted inputs rather than later checkout edits.
- Machine-readable results through the CLI and an MCP adapter.
- A basic dashboard after the execution path is dependable.
- A release archive with checksums, dependency instructions, version information, and third-party notices.

Neither a hosted account nor an internal coordination service is required for
local operation. Initial dependency downloads and workflow network activity can
require internet access.

## Requirements

P01–P12 are release requirements. "Now" says what the current prototype
demonstrates. It is not a waiver.

| ID | Requirement | Required evidence | Now |
| --- | --- | --- | --- |
| P01 | Install outside the development checkout | Release archive works in a clean supported environment; uninstall instructions exist | Not started. Invocation is `PYTHONPATH=src python3 -m execution_core`. |
| P02 | Diagnose readiness | Doctor reports versions, supported backend, repository root, Docker access, and actionable missing prerequisites | Development `worker.describe` reports synthetic-backend readiness only. No doctor command. |
| P03 | Persist accepted submissions | Client disconnect after submission does not discard the run; retry does not create duplicate work | Met for development fixtures, including across restart. Met for an accepted workflow job, including a disconnect before the reply. A changed input under the same key conflicts and does not create a second run. |
| P04 | Execute captured source | Editing the original checkout after acceptance leaves the run's input digest unchanged | Capture stores an independent copy and digest. Verification accepts only a matching snapshot, and materialization copies it into a new private workspace. An accepted workflow job keeps that snapshot id and its manifest, workflow, plan, and image digests after the checkout changes, and the worker executes that copy. A development run's digest identifies fixture JSON, not repository source. |
| P05 | Report truthful outcomes | Success requires backend exit zero; backend errors, cancellation, and interrupted execution remain distinguishable | Met for synthetic fixtures: `succeeded` requires exit 0; failed, cancelled, and lost stay distinct. An executed workflow job is `succeeded` only at exit 0. A nonzero step is `failed` with that exit code. A setup failure is `failed`, with a null exit code and a structured error. A job timeout is `cancelled` with a null exit code, a null error, and `cancel_requested` false. A step timeout is `failed` with a null exit code and error kind `STEP_FAILED`. Neither is `succeeded`. |
| P06 | Bound output consumption | Clients page logs by cursor; large output does not require loading the entire log | Met for development logs and for workflow step stdout and stderr. Pages are 1–65536 bytes. A page contains only the requested slice. |
| P07 | Cancel owned execution | Cancellation stops backend processes and owned containers, then records confirmed cleanup or an unresolved outcome | Development fixtures own no processes or containers. Their cancellation is recorded. Cancelling a queued workflow run does not start a container. Cancelling a running workflow run stops the owned container and any service containers, then records `cancelled` with `cancel_requested` true after they are gone. If one remains, the run is `lost` with cleanup `unresolved` and a new workflow attempt is refused until this process stops. A job or step timeout still stops the owned container and records the timeout result. Restart removes a leftover owned container, its service containers, and their network, or records that attempt `lost` with cleanup `unresolved` and does not reuse its container or workspace. |
| P08 | Recover after restart | Accepted queued work remains discoverable; interrupted work cannot silently become successful or execute twice | Met for the development backend and for a workflow attempt. Queued work remains and can start. An interrupted attempt becomes `lost` and is not run again. The same submission key returns that run. A second worker cannot take the state directory. |
| P09 | Explain compatibility | Unsupported requested features produce explicit capability errors or recorded limitations before execution | The planner rejects unsupported workflow fields by name before a plan exists. A workflow file larger than 500 KB is a capability error and creates no run. A step `timeout-minutes` above 360 minutes is a capability error and creates no run. Version 1 submission of those fields creates no run. `run_job` rejects an unsupported shell instead of reporting exit 0. `worker.describe` advertises `workflow.job` and does not advertise JavaScript or Docker actions, secrets, or services. A job may run service containers. Unsupported service keys are rejected by name. A job matrix is expanded, with the differences recorded in the next steps. Development command, secret, and artifact requests are rejected. |
| P10 | Protect local access | Other OS users cannot submit through the socket; secrets are excluded from automatic source capture | State directory mode 0700 and socket mode 0600 are tested. Capture excludes known credential paths and ignores an explicit include of those paths. This is not universal secret detection. |
| P11 | Serve agents and humans consistently | CLI and MCP observe the same run identifiers, states, logs, and errors | CLI JSON only. MCP is not implemented. |
| P12 | Preserve execution evidence | Result identifies source/workflow digests, engine version, image identity, timing, and exit status | A development result identifies the fixture digest, backend name and version `0.0.1`, timestamps, and exit status. An executed workflow job records the snapshot, manifest, workflow, plan, and image digests, per-step records, and an exit status. A setup failure records those digests, a null exit code, and a structured error. |

The initial release supports trusted repositories owned by the local user.
Docker access and workflow execution are powerful capabilities. Local mode is
not a sandbox for arbitrary hostile repositories.

## Product flow

The intended release flow. Install, doctor, and artifact retrieval are not
available. Today a developer starts the worker, submits a development
fixture or a version 1 workflow job, follows its status and logs, and reads
JSON from the CLI.

1. Install the release and run doctor against a selected repository.
2. Start its worker and inspect supported execution capabilities.
3. Submit a workflow or job selection and capture the intended source.
4. Receive a durable run identifier after validation and source capture succeed.
5. Follow output, or disconnect and return later.
6. Inspect the final result, compatibility notes, and available artifacts.
7. Cancel work, or explicitly submit another attempt when needed.

CLI examples in older notes use `<cli>` until the executable name is selected.
The prototype command is `python3 -m execution_core`.

## Success metrics

The release gate is evidence, not a performance target. Do not publish a
speed or resource claim that was not measured.

- P01–P12 pass on the supported platform, against the packaged artifact rather
  than this checkout.
- Cold startup, warm startup, job duration, peak memory, and disk use are
  recorded for that run.
- At least one person outside the development environment completes the
  documented install-to-result flow.
- M2 exit evidence, before that release gate: representative workflows return
  correct success and failure; checkout edits do not change accepted inputs;
  cancellation leaves no owned work running; restart does not silently repeat
  an interrupted attempt.
- A local workflow result is reported under the recorded capability set. It is
  not reported as GitHub Actions equivalence unless a reference comparison
  exists.

No numeric service level is defined. The development worker retains run
records and submission keys. Finished run folders older than 90 days, or
beyond the newest 100, are removed, oldest first when the disk budget needs
room. In-flight runs stay. A configured disk budget refuses a new submission
that still would not fit. The worker is not an unattended production service.

## Open questions

| Question | Current position | Needed before |
| --- | --- | --- |
| Project license | Permissive open-source license proposed. Not selected. Notices for locked development tools are inventoried and are not a project license. | Public distribution |
| Public name and executable | Rookrunner and `execution_core` / `execution-core` are working names. The executable is not registered. | Public package registration |
| Supported release platform | Linux x86_64 with Docker is the proposed first artifact target. Recorded tests do not cover Python 3.11 or other operating systems. | Artifact packaging |
| Runner image | Workflow execution needs an image pinned by digest. `run_job` requires the caller to pass one and does not select a default. The worker uses `--runner-image` when `image` is omitted and every selected job is literal `runs-on: ubuntu-latest` ([runner image](design/runner-image.md)). An explicit `image` still wins. The operator builds Ubuntu 24.04 with passwordless sudo for the caller uid and the Docker client. The plan schema is unchanged. `worker.describe` reports optional `runner_image` when the flag is set. Version 1 `image` is optional. Publishing an image stays rejected. | Closed for the local worker. Publishing stays rejected. |
| Workflow parser | PyYAML 6.0.3 parses workflow text for the planner. License and source are recorded. Parsing does not execute a workflow. | Closed for planning |
| Git metadata for checkout | Capture copies working files and excludes `.git`. The allow-list is copied to a sibling `git.json` ([design](design/git-metadata.md)). An owned checkout accepts `uses: actions/checkout@v4` and `actions/checkout@v` plus digits, and does not replace those files ([design](design/checkout.md)). Dotted tags and other actions stay unsupported. The trees and blobs of the captured base commit are stored beside the manifest ([design](design/git-objects.md)). The default capture excludes the original commit object. When the accepted plan sets `fetch-depth` to the YAML integer `0`, that commit and its ancestors are stored ([design](design/checkout-tag.md)). One parentless synthesized commit for that tree is stored on the default path. ([design](design/synthesized-commit.md)). When the object store is present, the attempt workspace receives an owned `.git` directory ([design](design/git-directory.md)). An absent store still has no `.git`. The `check.yml` dogfood path was run from the CLI. NS-38's identified commit ended `failed`. The gap that record names is closed: a later run ended `succeeded` with exit code 0 ([validation](validation/dogfood-check.md)). The design is [dogfood check](design/dogfood-check.md). | Claiming Git-dependent workflows |
| Submission-key retention once pruning exists | M1 retains keys for the life of the state directory. The workflow draft requires tombstones across a documented retry window. That window is undefined. | Workflow submission |
| Secret provisioning | Designed for this one local worker. p7-secret-env reads a file secret into an exact `secrets.NAME` in step `env` or `with` when `--secrets` is set and the allowlist matches. p7-secret-run rewrites an exact `secrets.NAME` in `run` to `${RR_SECRET_NAME}` for bash and sh when the lexer proves the context. The script contains the rewritten text and does not contain the value. A job mints one Contents-read installation token when `--app-key` is set, permissions allow Contents read, and a step needs `secrets.GITHUB_TOKEN` or exact `github.token`. Otherwise those aliases stay empty. `permissions: {}` mints nothing. The record stores the revocation attempt, not the token. Filename exclusions are not a secret system. MB2090 accepted the amended answers on 2026-10-05 ([secrets](design/secrets.md)). | Implemented |
| Dashboard direction | Runnable visual options, then a selection. | Dashboard implementation |
| Private remote workers and managed capacity | Discovery only. Each needs its own requirements. Neither blocks the local preview. | Any remote or hosted design |
| Event payload and workflow wire version | The workflow methods in the protocol draft are not the implemented v0 submit body. They need a negotiated version. | Workflow acceptance |
| Reporting results to GitHub | NS-40 posts one commit status for one clean workflow run ([owner CI](design/owner-ci.md)). The status SHA and the tested commit may differ. NS-41 evaluates `on` for push and pull request before a run is accepted. A non-match creates no run. A tag push skips the path filters. Tags under `pull_request` are ignored. NS-42 runs one poll pass for one owner repository and then exits. NS-43 recorded statuses for one push and one pull request of this repository ([validation](validation/owner-ci-rookrunner.md)). Only `succeeded` with exit code 0 posted `success`. Outbound HTTPS only. No listener. The credential is the GitHub App Rookrunner-App, chosen on 2026-10-05 ([check runs](design/check-runs.md)). Its App ID is 5201333. It is not the moonbase2090-agents App. Permissions are Checks write, Commit statuses write, and Contents read. The private key lives only in `~/Secrets/github-app/rookrunner-app/` and is passed by path. An installation token is minted at post time. When `--app-key` is set, one post exchanges a JWT for that token, posts one check run, then posts the commit status. Omitting `--app-key` keeps the NS-40 token file. The plan schema is unchanged. The capability version stays 12. `.github/workflows/check.yml` is unchanged. The installation and the private key are still pending. P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions . p7-mask, p7-trust-gate, p7-socket-lock, p7-secret-env, p7-secret-run, and p7-job-token are implemented ([secrets](design/secrets.md)). MB2090 accepted the amended answers on 2026-10-05. | Implemented for the post. Installing the App and placing the key remain operator steps. This repository does not install the App and does not read a key. |

Source capture of tracked working files plus explicitly included untracked
files is no longer an open product choice. It is the implemented capture
slice. Binding that snapshot to durable run acceptance is still M2 work.

## Release decision

The release requires passing evidence for P01–P12 on the supported platform,
the measurements in Success metrics, and one external install-to-result run.
Public publishing is a separate step after the preview artifact is reviewable.
