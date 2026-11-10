# Rookrunner — product requirements

Status: draft product requirements. What the tree implements is the
[implementation status](implementation-status.md). This document does not
restate it. See the [manifesto](manifesto.md) for why the project exists.

Accepted direction:

- Begin with a downloadable local tool, worker agent, and documented execution protocol.
- Build a useful product before developing a paid service.
- Develop in an independent repository, outside Local Actions.
- Use Rookrunner as the working title.
- Own the workflow execution engine and pursue broad GitHub Actions compatibility
  through explicit, tested increments. Do not delegate execution to act.

Accepted decisions are
[0001](decisions/0001-executable-foundation.md),
[0002](decisions/0002-m2-capture-and-backend.md) (capture only; the act pin is
historical), and [0003](decisions/0003-owned-execution-engine.md).
The [development contract](design/development-contract.md), the v0
[schemas](../schemas/v0/README.md), and the
[capture contract](design/source-capture.md) describe those slices.
Other architecture below is still a proposal.

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
- Universal secret detection and universal log redaction.
- Importing Local Actions configuration, credentials, state, sockets, or command names.
- Treating a local Docker run of a trusted repository as a sandbox for hostile code.

## What the code does now

The commands, protocol methods, capabilities, and features in this tree are
the [implementation status](implementation-status.md). A check fails when a
listed command or feature is missing from the code. This document does not
keep a second inventory.

Validation records live in [M1 completion](validation/m1-completion.md) and
[M2 capture](validation/m2-capture.md). Those records are historical. They do
not make the requirements below done.

## Scope by milestone

Milestones are outcome gates, not dates. Detail and exit evidence are in the
[roadmap](roadmap.md). What the tree implements is the
[implementation status](implementation-status.md). Live ticket status is outside
this document.

| Milestone | Scope |
| --- | --- |
| M0 — Project foundation | Independent repository, PRD, architecture, protocol sketch. The workflow protocol in [protocol.md](design/protocol.md) is a draft. |
| M1 — Executable contract | CLI, local worker, persistence, schemas, and protocol checks on a development backend. |
| M2 — Real workflow execution | Owned parser and planner, capture bound to acceptance, and a supervised container lifecycle. The subset in this tree is the implementation status. |
| M3 — Agent and human access | MCP adapter, dashboard, and bounded evidence retrieval through the same protocol. |
| M4 — Downloadable preview | License, public names, packaged artifact, and P01–P12 on a clean machine. |

M2's first executable subset is a starting point: an explicitly selected job,
sequential steps, and a digest-pinned image, with unsupported syntax rejected.
Later increments of the same engine stay in the design notes. They are not a
second status record. The differences still under discussion are in the
[next steps](planning/next-steps.md).

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

P01–P12 are release requirements. Whether the tree meets one of them is the
[implementation status](implementation-status.md). This table does not restate it.

| ID | Requirement | Required evidence |
| --- | --- | --- |
| P01 | Install outside the development checkout | Release archive works in a clean supported environment; uninstall instructions exist |
| P02 | Diagnose readiness | Doctor reports versions, supported backend, repository root, Docker access, and actionable missing prerequisites |
| P03 | Persist accepted submissions | Client disconnect after submission does not discard the run; retry does not create duplicate work |
| P04 | Execute captured source | Editing the original checkout after acceptance leaves the run's input digest unchanged |
| P05 | Report truthful outcomes | Success requires backend exit zero; backend errors, cancellation, and interrupted execution remain distinguishable |
| P06 | Bound output consumption | Clients page logs by cursor; large output does not require loading the entire log |
| P07 | Cancel owned execution | Cancellation stops backend processes and owned containers, then records confirmed cleanup or an unresolved outcome |
| P08 | Recover after restart | Accepted queued work remains discoverable; interrupted work cannot silently become successful or execute twice |
| P09 | Explain compatibility | Unsupported requested features produce explicit capability errors or recorded limitations before execution |
| P10 | Protect local access | Other OS users cannot submit through the socket; secrets are excluded from automatic source capture |
| P11 | Serve agents and humans consistently | CLI and MCP observe the same run identifiers, states, logs, and errors |
| P12 | Preserve execution evidence | Result identifies source/workflow digests, engine version, image identity, timing, and exit status |

The initial release supports trusted repositories owned by the local user.
Docker access and workflow execution are powerful capabilities. Local mode is
not a sandbox for arbitrary hostile repositories.

## Product flow

The intended release flow. Which of these steps the tree can run is the
[implementation status](implementation-status.md).

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
that still would not fit, and stops a running attempt that exceeds it: that
run fails. The worker is not an unattended production service.

## Open questions

| Question | Current position | Needed before |
| --- | --- | --- |
| Project license | Permissive open-source license proposed. Not selected. Notices for locked development tools are inventoried and are not a project license. | Public distribution |
| Public name and executable | Rookrunner and `execution_core` / `execution-core` are working names. The executable is not registered. | Public package registration |
| Supported release platform | Linux x86_64 with Docker is the proposed first artifact target. Recorded tests do not cover Python 3.11 or other operating systems. | Artifact packaging |
| Runner image | A digest-pinned image is the local-worker design in [runner image](design/runner-image.md). What this tree implements is the [implementation status](implementation-status.md). Publishing an image stays rejected. | Closed for the local worker. Publishing stays rejected. |
| Workflow parser | PyYAML 6.0.3 parses workflow text for the planner. License and source are recorded. Parsing does not execute a workflow. | Closed for planning |
| Git metadata for checkout | Capture and checkout metadata are specified in [git metadata](design/git-metadata.md), [checkout](design/checkout.md), [git objects](design/git-objects.md), [checkout tag](design/checkout-tag.md), [synthesized commit](design/synthesized-commit.md), and [Git directory](design/git-directory.md). What this tree implements is the [implementation status](implementation-status.md). | Claiming Git-dependent workflows |
| Submission-key retention once pruning exists | The workflow draft requires tombstones across a documented retry window. That window is undefined. What this tree retains is the [implementation status](implementation-status.md). | Workflow submission |
| Secret provisioning | The design for this one local worker is [secrets](design/secrets.md). What this tree implements is the [implementation status](implementation-status.md). | See the [implementation status](implementation-status.md). |
| Dashboard direction | Runnable visual options, then a selection. | See the [implementation status](implementation-status.md). |
| Private remote workers and managed capacity | Discovery only. Each needs its own requirements. Neither blocks the local preview. | Any remote or hosted design |
| Event payload and workflow wire version | The workflow methods in the protocol draft are not the implemented v0 submit body. They need a negotiated version. | Workflow acceptance |
| Reporting results to GitHub | Owner CI and check runs are specified in [owner CI](design/owner-ci.md) and [check runs](design/check-runs.md). What this tree implements is the [implementation status](implementation-status.md). Installing the App and placing the key remain operator steps. This repository does not install the App and does not read a key. | See the [implementation status](implementation-status.md). Installing the App and placing the key remain operator steps. This repository does not install the App and does not read a key. |

Source capture of tracked working files plus explicitly included untracked
files is described in the [capture contract](design/source-capture.md).
Whether this tree binds that snapshot to run acceptance is the
[implementation status](implementation-status.md).

## Release decision

The release requires passing evidence for P01–P12 on the supported platform,
the measurements in Success metrics, and one external install-to-result run.
Public publishing is a separate step after the preview artifact is reviewable.
