# CI dashboard design

Status: proposed, 2026-10-23. Owner decision required before implementation.
This document adds no runtime behavior, listener, API or visual selection.
Wireframes below are design text. Future UI work must present runnable options
before the owner chooses its visual direction.

The dashboard should answer: which source is being tested, what is happening
now, why did a run fail, and can the operator safely cancel or retry it?
Rookrunner owns every answer. The worker's `runs.sqlite3`, captured inputs and
engine records are authoritative, exposed through worker APIs and MCP tools.
GitHub is never a dashboard data backend or a fallback when a worker is down.
An optional outbound source/PR link is navigation, not evidence of run success.

## Recorded direction and the decision being reopened

The dated record is more specific than the current M3 plan's proposed header:

| Record | What it establishes |
| --- | --- |
| [M3 sign-off, October 6](https://github.com/moonbase2090/rookrunner/pull/72#issuecomment-6018019143) | The owner accepted recommended answers to the amended plan. [M3 question 4](m3-access.md#open-questions) recommends socket clients with no listener; question 5 defers visuals until runnable options exist. |
| [Options PR #77](https://github.com/moonbase2090/rookrunner/pull/77), commit `50c6fe4` | Two socket clients: terminal and a static HTML writer. The HTML writer exits; its file cannot refresh or send controls. Neither option binds a port. |
| [Inspect PR #78](https://github.com/moonbase2090/rookrunner/pull/78), commit `e15c1c2` | The PR explicitly records the terminal choice and removes `dashboard-html`. This is direct historical evidence of selection, not an inference from today's code. |
| [Cancel PR #79](https://github.com/moonbase2090/rookrunner/pull/79) | Adds cancel and reconnect behavior to the terminal client. |
| [Status page, October 9](status-page.md) | Separately accepted read-only loopback HTTP page. The worker still has no network listener. This does not authorize an interactive browser control plane. |

The requested Graphite-style comparison is inspiration for this proposal.
The original October 6 record establishing that specific style was not located;
it must not be presented as a verified historical web-UI decision. Owner review
should clarify whether it means compact run lists, PR-oriented navigation,
another interaction, or a specific visual reference. The proven choice above
is terminal access after runnable options.

## Implemented data and gaps

Source baseline: `cb8b3cb9200010d7fefbd6e7a7e4757f1313c62a`.
Each worker owns its database. UI processes must not open or write those
databases directly, aggregate private filesystem paths, or query GitHub to
fill missing fields. A missing value is shown as unknown.

| Need | Implemented source/API | Missing for this dashboard |
| --- | --- | --- |
| Run identity and outcome | [`worker.py`](../../src/execution_core/worker.py): `run.get` and `run.list` expose run/worker/attempt IDs, snapshot and workflow/plan/image digests, state, nullable exit code, cleanup and acceptance/start/finish times. `run.list` pages in insertion order and filters by state. | Repository, branch, PR and date filters; newest-first stable history paging; workflow-run grouping across separately submitted jobs. Only terminal `succeeded` with `exit_code=0` is success. |
| Repository/PR context | `status.view` derives repository and a PR-number or commit subject from the stored request/post records. `_view_run` derives elapsed seconds. That view covers recent runs and the queue. | A complete history projection, explicit branch/ref, head/base/tested SHAs, display metadata provenance and a stable grouping ID. `run.get` does not expose the raw event. Do not derive branch from the worker's current checkout. |
| Jobs and steps | [`run.py`](../../src/execution_core/run.py) produces step results. `_public_steps` stores index, optional ID/name/job ID, status, exit code, bounded output and error when the worker saves the outcome. One submission selects a job and can execute its dependency closure. | Persisted incremental job/step state, stable matrix/job-instance identities and per-step start/finish times. A running run does not establish which step is currently running. |
| Logs | `run.logs` pages base64 bytes with an opaque cursor, `next_cursor` and `end_of_stream`. `_save_workflow_outcome` stores workflow output after execution, assembled as each step's stdout then stderr, not an interleaved live stream. [`run.py`](../../src/execution_core/run.py) applies command processing and masks to stored output. | Live durable append, step/stream identity, resumable change events and search. Client polling alone cannot create streaming output. An empty page with `end_of_stream=false` is not completion. |
| Queue/workers | `worker.describe` reports readiness and advertised limits; `status.view` supplies queue, groups, waiting state, worker version/image and last completed poll. | Worker catalog, last successful observation, boot/session identity, capacity/active-attempt telemetry and stale/unreachable states. Readiness does not guarantee submission admission or mean idle. Poll placement caps are not worker capacity fields. |
| Controls | `run.cancel` returns the worker record. Terminal cancellation uses version 0. Same-key `run.submit` retries return the original accepted run/snapshot, including after checkout edits. | Atomic rerun of retained inputs, parent/child linkage, separate new-source submission, and operation identity for uncertain replies. The existing dashboard has no submit/rerun control. |

[`mcp.py`](../../src/execution_core/mcp.py) currently exposes `describe`, `get`,
`list`, `logs`, `artifacts`, `artifact_read`, `submit` and `cancel`. There is no
MCP `status.view`, search, stream, worker catalog or rerun tool. MCP `submit`
captures the bound checkout, sends `event: {}` and omits `event_name`; it must
not be relabeled as rerunning historical inputs.

## Options and recommendation for the owner

| Option | Strength | Cost and reopened decisions |
| --- | --- | --- |
| Extend TUI only | Preserves the selected no-listener client model; keyboard operation fits the existing program. | Limited room for long logs and trends; text selection/search and continuous refresh need work. Does not supply browser navigation. |
| Web only | Room for run lists, job trees, log search and history; linkable local views. | Replaces the selected terminal direction and needs a listener, browser access policy, safe rendering and write authorization. Static HTML cannot provide these controls. |
| Web primary, retain a small TUI | Web handles inspection/history; TUI remains a compact local queue/detail/cancel surface using the same contracts. | Two clients need parity tests. Still requires explicit approval of the new listener/control boundary and an agreed limit on TUI scope. |

Recommend the third option for owner decision, with a read-only web phase
before browser writes. Retain the existing TUI and plan its rendering fix;
do not build two full competing products. Compare runnable TUI and web options
with the same representative data before visual selection. This recommendation
does not supersede the recorded terminal choice until approved.

A web adapter would call allowlisted worker socket methods. Reusing the
read-only status page is not permission to add POST, serve raw files, or bind
beyond loopback. A control UI reopens M3's no-listener boundary for the dashboard,
its prohibition on dashboard submission, and its same-OS-user access model.
It also reopens the status page's GET/HEAD-only and restrictive content policy.
The worker itself would continue to expose only its existing local transport.

Before writes, approve a browser threat model covering local authentication,
session lifecycle, exact Host/Origin checks, DNS rebinding, CSRF, cross-origin
requests and body limits. Loopback alone is not authentication. Commands must
be explicit authenticated POST operations, never GET links. Escape all labels
and log content, render terminal control bytes safely, and never render workflow
HTML as trusted content. Keep credentials, absolute checkout paths and raw
events out of responses. Remote transport and unrelated customers remain
separate milestones.

## Proposed views

All labels and values below are illustrative, not observed runs or metrics.
Every view shows its observation time and worker reachability. A stale view
keeps the last known state with a stale banner, never turns it into success.

```text
Runs   Repository [sample/engine v]   Branch [all v]   PR [all v]
Outcome [all v]   Time [selected range]   Last observation 14:06:10Z

Run     Source                 Workflow / job   State       Elapsed
r-104   feature/parser  PR 47  ci / test        running       02:14
r-103   main  commit <sha>     ci / build       failed        01:08
r-102   main  commit <sha>     ci / lint        succeeded     00:19

Queue 2    worker-a busy    worker-b idle    worker-c stale
[Open selected run]              [History and failure trends]
```

```text
sample/engine  r-104  PR 47   head <sha>   tested merge <sha>
snapshot <digest>   workflow <digest>   image <digest>
running 02:14   data current at 14:06:10Z       [Request cancel]

Jobs / steps                   | Logs [Follow on] [Find literal text]
 test [job instance] running    | 14:05:40Z  Compile   stdout  ...
   Checkout succeeded 00:02     | 14:05:41Z  Compile   stderr  ...
   Compile  running   01:32     | [Older page] [Next match] [New output]
   Test     not started        | Find scope: this step / this run
                               | Masked stored text; safe text rendering
[Artifacts: availability]       | Connection lost? Resume from cursor
[Rerun these captured inputs]   | New run; original evidence retained
```

```text
Workers / queue                          History: sample/engine / ci / test
worker-a busy   observed 14:06:10Z        Completed attempts: <count>
  r-104 test   cancel not requested      Success / failure / lost / cancelled
worker-b idle   observed 14:06:09Z        Failure share: <failed>/<eligible>
worker-c unreachable  last 14:02:00Z     Setup failures shown separately

Queued r-105   accepted 14:05:00Z         [Open exact failed attempt]
  Waiting on concurrency group <name>    [Show retry chain]
```

Run detail preserves the selected job/step while new output arrives. Follow
stops when the operator scrolls back and offers an explicit return-to-tail.
Keyboard focus and mouse actions share one command path. Narrow views stack
the job tree above logs; full log text remains selectable. Explain missing
steps, pruned artifacts and unsupported capabilities rather than hiding them.

## Proposed contracts and ownership

Names below are sketches, not existing protocol methods. Negotiate capabilities
and publish schemas before either client calls them. MCP tools would forward
the same results, not maintain a separate state store.

| Proposed method | Minimum contract |
| --- | --- |
| `ci.runs` | Whitelisted repo/ref/PR/outcome/time filters, bounded page and opaque stable cursor. Return worker/run identity, observed time, source labels with provenance and a workflow grouping ID when known. |
| `ci.run` | Run evidence plus job/step instances, timestamps, revision and log-stream references. Missing historical timings are null, not estimated from the run total. |
| `worker.health` | Worker/session ID, observation time, admission readiness, active attempt IDs and known capacity. The client distinguishes busy, idle, not-ready, stale and unreachable. Unknown capacity stays unknown. |
| `run.events` | Bounded cursor pages of durable state/step/log changes with monotonic revision within a worker session. Reconnect resumes; an expired cursor requires a new snapshot and an explicit gap marker. |
| `run.log_search` | Literal text search over authorized masked stored bytes, with run/step scope, bounded result/cursor, byte offsets and revision. Return snippets from those same bytes. Do not add regex initially. |
| `run.rerun` | Parent run, expected input identity and durable operation key. Atomically create at most one new run from retained captured inputs; return its distinct run ID or an explicit unavailable/conflict error. |

The worker would persist normalized source metadata at acceptance: repository
identity, event/ref, optional branch and PR number, head/base/tested commit,
snapshot identity and metadata provenance. Branch/PR labels from arbitrary local
events are caller-supplied, not proof of origin. Older runs with no verifiable
metadata remain unknown. Never query today's checkout or GitHub to backfill them.
Do not expose the full stored request; it can contain sensitive event data.

Engine progress would become durable worker-owned records. Use stable
job-instance IDs, including matrix/dependency instances, and step-instance IDs;
a display name or step index alone is insufficient across jobs. The engine
reports transitions and times to the worker's serialized persistence path.
The worker owns revisions, log append and outcome commit. UI adapters own
formatting and transport only. A UI disconnect cannot cancel work.

Streaming requires engine changes before browser SSE or TUI refresh is useful.
Process command/mask state before publishing or indexing text, including chunk
boundaries and UTF-8 fragments. Never stream raw subprocess pipes or use raw
output for search. Preserve stream identity and ordering instead of the current
stdout-then-stderr concatenation. Existing masking is not a universal secret
scanner and cannot promise removal of secrets already emitted before a mask.
Bound retention, backpressure and indexing work; choose their policies before
implementation rather than inventing a throughput or latency guarantee here.
Artifact bytes remain a separate, unmasked surface with explicit availability.

Worker health is an observation, not a freshened label on cached data. Measure
reachability independently of the last poll, which records a completed pass.
Choose a documented stale threshold with the eventual polling/heartbeat policy.
Do not infer idle from an unreachable worker or admission-ready from an empty
queue. Multiple workers require an explicit local catalog and approved transport;
they do not imply remote worker registration is implemented.

## Cancel, rerun and history semantics

Cancel sends the existing worker command for the selected worker/run identity.
Show `cancel_requested` while cleanup proceeds; only the returned durable result
can show cancelled. Finish-before-cancel keeps the terminal result. Failed
cleanup may yield `lost` with unresolved resources. A timeout means outcome
unknown; re-read the same run before offering another action. Stale or
unreachable views disable writes and explain how to reconnect.

Proposed rerun means the original snapshot, workflow/plan, event and image
identity, with a new run and parent link. Revalidate admission, current security
policy and required dependencies; mint fresh eligible credentials rather than
replaying a token. Refuse if the retained snapshot or required pinned inputs
are unavailable. Never silently capture the current checkout. A future
"run current checkout" action is a distinct submit operation with a source
preview and new submission key. Retrying an uncertain rerun reuses its operation
key and returns the same child; a deliberate additional rerun uses a new key.
Neither operation silently adds a GitHub posting action.

History queries use retained Rookrunner records and identify coverage gaps.
Count attempts separately from logical workflow/job groups and show retry
chains. Define failure share as `failed / (succeeded_with_exit_0 + failed)`
for terminal attempts in the selected window. Show the
numerator and denominator; report cancelled, lost and malformed/unknown outcomes
separately. Split setup failures from command exit failures where stored errors
allow it. Exclude queued/running attempts from completed trends and do not treat
an empty denominator as zero failures. No performance target is claimed.

## Planned raw-mode rendering correction

The reported stair-step rows have a source-supported explanation, not a verified
fix. [`dashboard.py`](../../src/execution_core/dashboard.py) `_interact` calls
`tty.setraw(fd)`, then writes `board.render()` directly. `render` joins rows
with LF. Python's [`cfmakeraw`](https://github.com/python/cpython/blob/v3.12.14/Lib/tty.py)
disables output postprocessing, so LF no longer
implies return to column zero. `_interact` also waits on `os.read` for input;
it has no periodic live refresh despite older plan language about polling.

In a later fix, make terminal frame emission explicitly position each row or
normalize logical newlines to CRLF once at the interactive output boundary.
Keep piped output LF-only; do not scatter carriage returns through the model
or log decoder. Handle existing CRLF/control bytes without duplicate CRs, and
preserve termios/mouse restoration on quit, EOF and exceptions.

Verification must drive the actual raw-mode path through a pseudo-terminal
with output postprocessing disabled. Feed fixed unequal-length rows and assert
their literal displayed columns using a terminal screen model, then exercise
resize, navigation, cancel/reconnect and exit restoration. The check must fail
on the current LF-only interactive writer. Keep a piped-output assertion that
no terminal escape codes or CRs are introduced, plus one visual capture in the
approved clean environment at the fix's exact head. Render-only string tests
cannot reproduce this mode mismatch. No renderer code changes in this PR.

## Bounded phases and owner decisions

| Phase | Boundary and acceptance before proceeding |
| --- | --- |
| 0. Owner design decision | Confirm primary surface/TUI scope, Graphite reference, listener and browser-write threat model. Approve any changes to M3 constraints. This document alone does not authorize code. |
| 1. Contracts and terminal repair | Specify schemas, unknown/provenance fields, grouping, health and retention policies. Deliver the isolated raw-mode fix with failing-before/passing-after PTY and clean-environment proof. |
| 2. Read-only runnable options | Compare TUI and web using the same queued/running/failed/lost/stale examples. Owner selects visuals after running both. If web approved, prove local-only authenticated access and escaped logs; no browser writes. |
| 3. Engine progress and inspection | Show real job/step transitions and elapsed time before completion; reconnect across paged logs without duplicates/gaps, search masked bytes across page boundaries, and preserve selection. CLI/MCP/UI agree on one recorded snapshot/outcome. |
| 4. Controls and trends | Exercise queued/running/finish-before-cancel, unresolved cleanup, uncertain replies, duplicate reruns and missing snapshots. Show new run IDs and original input identities. Verify trends against fixed literal counts, including zero denominator and retry chains. |

Each behavior change requires focused checks and clean-environment proof in
its PR before reviewer SHIP. Only the current design-text PR is exempt from
runtime/VM proof. Keep phases independently reviewable; remote serving and
unrelated customers are not implicit follow-ups within these phases.
