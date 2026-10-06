# M3 plan: agent and human access

Status: proposed, 2026-10-06, amended after the gap review on
this pull request. This document is for MB2090's sign-off. It is
not accepted direction. No code in this pull request.
Implementation waits until he accepts the answers below. This
pull request is not a merge.

The amendment records the HTML cancel command and its write
timestamp, the accepted mint risk for any edited workflow, the
adapter's rejection of a `poll-` submission key, the same-key
snapshot rule, the existing artifact path test, the
`status_posts` effect of `run.status`, the installation wording,
and the adapter-local error tests.

The milestone is the roadmap's M3: an MCP adapter, a local
dashboard, and bounded evidence retrieval through the protocol the
CLI already uses. The stories are RR-29 through RR-36 in the
[project plan](../planning/project-plan.md). P11 in the
[PRD](../prd.md) is the agreement rule: the CLI and MCP observe
the same run identifiers, states, logs, and errors.

https://docs.github.com/en/actions/reference/security/secure-use

## Goals

1. A coding agent on this machine can inspect a run, page its logs
   and artifacts, and cancel it, through MCP tools that call the
   worker socket.
2. The same agent can submit one version 1 workflow job when the
   answer to question 1 below stays the recommendation.
3. A person can see the queue, one run, its steps, its logs, and
   its artifacts, and can cancel, from a local dashboard that
   calls that same socket.
4. A malformed tool call returns a structured error and leaves the
   MCP session open for the next call.
5. Closing the adapter, or closing the dashboard, leaves the
   worker and any accepted run in the state the worker already
   recorded.

## Non-goals

- A network listener on the worker, the adapter, or the dashboard.
- A remote worker, a second OS user, or a sandbox between an agent
  and the files of the user who owns the worker.
- Worker startup, `poll`, the `status` command, `run.status`, a
  version 0 development fixture, or the App private key, as an MCP
  tool or a dashboard action.
- A new protocol method, a new plan field, a capability-version
  bump, or an edit to `.github/workflows/check.yml`.
- `write` or `write-all` in a workflow, Contents write on the job
  token, a new App permission, or the poller skip for commits
  pushed by the Rookrunner-App bot.
- A scanner for secret bytes in artifact files. [Secrets](secrets.md)
  already leaves that scanner out.
- A visual direction for the dashboard. Question 5 keeps that
  choice until the options pull request.
- Packaging, a webhook receiver, registration as a GitHub runner,
  and service for unrelated customers. Those stay M4, later
  investigations, and RR-42 and RR-45.

## Implemented facts

These are on `main` at the merge of pull request 71. This plan
reuses them. It does not reopen the accepted P7 answers.

The state directory is mode `0700` and is not a symlink. The
worker socket is `<state>/worker.sock`, mode `0600`. The same OS
user is the only client boundary. `worker.describe` advertises
protocol versions 0 and 1 and the method list in
[the development contract](development-contract.md):
`worker.describe`, `run.submit`, `run.get`, `run.list`,
`run.logs`, `run.artifacts`, `artifact.read`, `run.cancel`, and
`run.status`. One request uses one connection. A log or artifact
page is 1 to 65536 bytes. The message limit is 1 MiB.

`run.submit` with version 1 captures and plans one workflow job.
`event` is required. `event_name` is optional. When `event_name`
is omitted, `on` is not evaluated. Version 0 `run.submit` is a
development fixture. `run.cancel` sends version 0 for a fixture
and for a workflow run. `poll` submits by calling `run.submit` on
this same socket. It is another same-uid client.

File secrets are read only when `worker --secrets` is set and
`allowlist_matches` returns true for the stored event. With both
operator lists empty, the only match is a push whose `ref` is
`refs/heads/` plus `repository.default_branch`. A caller-supplied
event that carries those fields matches. A local submit that
omits them does not match the default rule. The match is
implemented in `allowlist_matches`.

A job token is minted when `worker --app-key` is set, the resolved
permissions allow Contents read, and a step needs
`secrets.GITHUB_TOKEN` or exact `github.token`, including an
omitted action input whose default is that alias. An omitted
permissions key grants Contents read. `permissions: {}` mints
nothing. `_prepare_job_token` does not call `allowlist_matches`.
The mint is one installation access token for Rookrunner-App, App
ID 5201333, with Contents read on the repository named at mint
time. The run record stores the revocation attempt and does not
store the token. The check-run post token is a different mint. It
is used by `poll` and `status`, and it requests Checks write and
Commit statuses write.

`check.yml` has `permissions: contents: read` and does not pass
`github-token`. The pinned `astral-sh/setup-uv` action stores
`github-token`'s default as `${{ github.token }}` on the step's
`inputs`, and that step's `with` omits the input, so
`job_needs_token` is true for that job. A worker started with
`--app-key` mints when that job runs, whether `poll` submitted it
or a local `submit` did.

`artifact.read` returns the file bytes. It does not compare them
to the job mask. A step that writes a secret into a workspace file
can be read back through that method. The log mask does not apply
to those bytes.

The fork gate is unchanged. A pull request whose head and base
repository ids differ, and a pull request with a null head
repository, runs nothing.

The operator named installation `168290590` for this plan. That
number is not read from disk. The mint reads `installation-id`
and does not compare the file to this number. [Secrets](secrets.md)
and [check runs](check-runs.md) still say the installation and
the private key are pending, and those documents contain no
installation id. This pull request does not edit them. The
numbered slice `m3-install-note` does, after sign-off. The number
does not go into code or into `check.yml`. The private key stays
in `~/Secrets/github-app/rookrunner-app/`. This document does not
read that directory and does not start a sign-in.

The capability version is 12. `write` stays rejected.

## Actors

| Actor | What it is | What it can reach |
| --- | --- | --- |
| Operator | The OS user who starts the worker | The socket, the CLI, `poll`, `status`, and the App key path the worker flags already accept |
| CLI | `python3 -m execution_core` as that user | Every socket method, plus `snapshot`, `poll`, and `status`, which do not all go through one method |
| Poll | One pass of the `poll` command | `run.submit`, `run.get`, `run.cancel`, and the post path. It is a socket client |
| Agent | A coding agent the operator runs as the same OS user | Whatever that user can open. MCP tools are the surface this plan adds. The socket remains open to the user |
| MCP adapter | A stdio process, same user, one socket connection per tool call | The tool list in the access model. It has no `--app-key` flag |
| Dashboard | A local process, same user, after the options pull request | The read methods and `run.cancel`. It does not submit |

There is one principal. The table is four programs and one person,
all the same uid when they run.

## Access model

Proposed. Question 1 and question 5 can replace the submit tool
and the dashboard shape.

The worker keeps owning runs, snapshots, logs, and cancellation.
The adapter and the dashboard are clients. They do not write the
run store. They do not chmod the socket. Disconnecting either
client does not cancel a run and does not stop the worker.

### Agent tools

The adapter speaks MCP on stdin and stdout. Each tool call opens
one new connection to `<state>/worker.sock`, writes one JSON-RPC
request, reads one response, and closes the connection. That
matches the worker's one-request connection. The adapter holds no
App key and no secret file. Its command line takes the state
directory and no `--app-key`, `--secrets`, `--credential-file`, or
API token.

The tool result is the protocol result object, unchanged.
`data_base64` stays base64. A test compares the canonical JSON of
the tool result with the canonical JSON of the socket result.

| Tool | Socket method | Parameters the adapter sends |
| --- | --- | --- |
| `describe` | `worker.describe` | `{}` |
| `get` | `run.get` | `run_id` |
| `list` | `run.list` | optional `cursor`, `limit`, `state` |
| `logs` | `run.logs` | `run_id`, optional `cursor`, `limit` |
| `artifacts` | `run.artifacts` | `run_id`, optional `cursor`, `limit` |
| `artifact_read` | `artifact.read` | `artifact_id`, optional `offset`, `limit` |
| `cancel` | `run.cancel` | `version: 0`, `run_id` |
| `submit` | `run.submit` | `version: 1`, `submission_key`, `workflow`, `job_id`, `event: {}`. Optional `image`. `event_name` omitted |

`submit` is in this table only if question 1 stays the
recommendation. The agent supplies `submission_key`, `workflow`,
and `job_id`. The adapter fills `version`, `event`, and the
omission of `event_name`. The agent cannot pass `event`,
`event_name`, `ref`, a repository object, a login, a fixture, or
a credential through the tool. A `submission_key` that starts
with `poll-` is rejected by the adapter before it dials. Poll
builds those keys as `poll-` plus the SHA-256 of the repository,
event, tested SHA, workflow, and job. The socket still accepts a
`poll-` key from the CLI or any same-uid client. The tool
description tells the agent to use a new key after each edit of
the checkout and to compare snapshot ids. The worker compares
version, workflow, job, event, image, and `event_name` on a
repeated key. It does not compare the captured bytes, so the same
key after an edit returns the original run and the original
snapshot id.

`describe` returns the worker object, including its `methods` list
and the `development.fixture` capability. The adapter does not
filter that object. The tool list is shorter than `methods`. There
is no tool for `run.status`, version 0 `run.submit`, `poll`,
`status`, `snapshot`, `follow`, or worker startup. An agent that
wants those calls the CLI. `follow` stays a CLI loop of `get` and
`logs`.

A tool error carries the worker's `data.kind` and message when the
worker returned a fault. The stdio session stays open. The next
tool call is a new socket connection.

### Human access

The CLI stays the operator's full client. The dashboard is a
second client for queue, run detail, step list, log pages,
artifact pages, and cancel. It does not submit, does not call
`run.status`, and does not start the worker. It shows the run
record the worker returned. A cancel action displays that record
after `run.cancel`. The result is `succeeded` only when the
record's state is `succeeded` and `exit_code` is 0.

The dashboard's first pull request is two runnable options. This
document does not choose the visual direction. Both options are
socket clients in the existing Python program. Neither binds a
port.

1. A terminal view that polls `run.list`, `run.get`, and `run.logs`.
   Its cancel action sends `run.cancel` and then renders the
   record the worker returned.
2. A command that writes one HTML file and exits. The file
   contains the UTC time of that write and the run state as of
   that time, and it says the snapshot is stale after the
   timestamp. The opened file has no control that reaches the
   socket, so it cannot cancel or refresh. The same command
   accepts a cancel action: it sends `run.cancel`, then writes
   the file again with a new timestamp and the record the worker
   returned. Re-reading state means running the command again.

Question 5 is the choice between them, or a third option he names,
after that pull request is runnable. Either choice has a cancel
path that sends `run.cancel` and then renders the worker's
record. RR-35 and RR-36 ask for browser evidence. This plan's
evidence is the terminal program and the written file. A browser
that talks to the worker waits for a listener, which question 4
leaves out.

### Auth and identity

The credential is the OS user who owns the state directory. The
socket mode and the directory mode are the check. The adapter and
the dashboard do not add a token, a peer name, or a second uid.

A same-uid process can open `<state>/worker.sock` and send any
method the worker implements. An MCP tool that omits a field does
not remove that method from the socket. `poll` cannot be
privileged over that process, because `poll` uses the same
`run.submit`. A client-settable "from poll" flag would be
forgeable by any same-uid caller, so this plan does not add one.

The operator decides whether a worker is started with `--app-key`
or `--secrets`. The adapter cannot pass those flags. A worker that
was started with them serves every same-uid client, including an
agent that does not use MCP.

## Threat model

The P7 boundaries stay. The worker is bound to one repository
path. A version 1 submit captures the working tree at that path,
including uncommitted changes to tracked files. The bytes that
run are that snapshot. A local Docker run is not a sandbox for
hostile code. Forks run nothing. `pull_request_target` is not
evaluated. `write` stays rejected. The job token's permission is
Contents read for the installation named in `installation-id`.
The operator names that installation as `168290590`. The mint
reads the file. The code does not compare the file to that
number. The post token stays on the `poll` and `status` path.

What M3 adds is two more same-uid clients. The new exposures are
the ones those clients make easy.

**Crafted event.** `allowlist_matches` is true for a
caller-supplied event that carries the default-branch fields, or
a listed ref, or a listed login. An agent that can speak
`run.submit` on the socket can build that event and receive file
secrets when `--secrets` is set. The recommended `submit` tool
sends `event: {}` and omits `event_name`, which does not match
the default rule. That is a property of the tool parameters. The
socket still accepts a full event from the CLI or from any
same-uid process.

**Job token.** Mint does not consult the allowlist, the event, or
whether the snapshot is dirty. Any edited workflow can mint, not
only `check.yml`. An agent that can edit the bound checkout and
submit can add a step that needs a token. On a worker started
with `--app-key`, that submit mints a Contents-read token for
the installation named in `installation-id` (the operator names
it as `168290590`). Omitting `--app-key` on one worker is not a
boundary: the same uid can open any socket that uid owns. M3
accepts this risk and leaves the P7 mint gates in place. A dirty
snapshot still does not post. `run.status` refuses it with
`STATUS_REFUSED`. Question 2 records a clean-snapshot gate as the
alternative.

**Advertised methods.** `worker.describe` lists `run.status` and
`development.fixture`. The `describe` tool returns that list. An
agent that then opens the socket can call them. Hiding the list
in the tool result would make the CLI and MCP disagree, and the
socket would still implement the methods. `run.status` with
`record` set to the run's terminal state inserts a row into
`status_posts`. The next poll decision for that run, context, and
SHA is `skip`, so the GitHub status stays unposted. That write is
accepted under the single-uid boundary. The adapter has no tool
that calls `run.status`.

**Artifact bytes.** `artifact.read` returns workspace bytes with
no mask. The `artifact_read` tool returns those bytes. A secret a
step wrote into a selected file is readable by the CLI, by MCP,
and by the dashboard. Question 3 keeps the P7 decision to leave
scanning out. The tool description and the dashboard say the page
is not masked.

**App key and post token.** The adapter and the dashboard have no
parameter for the key path. They do not call `run.status`. They
do not mint. A same-uid agent can still read
`~/Secrets/github-app/rookrunner-app/` if the process can read
that directory. M3 does not add a second user to prevent that
read. The worker's existing rule stays: `--app-key` accepts only
`~/Secrets/github-app/rookrunner-app/private-key.pem`, and the
key is opened when a mint or a post needs it.

**Docker socket.** `--docker-socket` combined with `--app-key` or
`--secrets` still refuses unless the `~/Secrets` probe exits 0.
M3 does not change that lock. A dashboard or an adapter does not
pass `--docker-socket`.

**Session loss.** The adapter crashing is a client disconnect.
Accepted work continues. A lost run stays `lost`. The dashboard
shows `lost` and an unresolved cleanup as the record states them.

**Network.** The worker still binds no TCP port. The adapter
binds no port. Both dashboard options bind no port. A later
request for a browser listener is a new threat model and waits
for its own sign-off.

## Open questions

Each recommendation is the proposed answer. The pull-request list
uses those answers. A different answer replaces the pull request
that cites it.

1. **Does the agent get a submit tool?**
   - A. Yes. The tool is the `submit` row above. `event` is `{}`.
     `event_name` is omitted, so `on` is not evaluated. The empty
     event does not match the default allowlist. A job that needs
     a token still mints when the worker was started with
     `--app-key`. The same is true of any edited workflow whose
     plan needs a token, because capture stores the working tree.
   - B. No submit tool. The agent gets the read tools and
     `cancel`. Submit stays the CLI and `poll`.
   - Recommendation: A. RR-30 and P11 include submit. The empty
     event is the same shape as a CLI submit that omits `--event`
     and `--event-name`. The mint risk is question 2.

2. **Should mint require a poll-shaped event or a clean snapshot?**
   - A. Leave mint on the P7 gates. A caller-supplied submit of
     any workflow that needs a token can mint, including a
     workflow the agent edited in the bound checkout. The
     allowlist continues to apply only to file secrets. A dirty
     snapshot does not post, because `run.status` refuses it.
     This risk is accepted for M3.
   - B. Mint and file secrets run only when the worker believes
     the submit came from `poll`. That belief would be a field on
     `run.submit`. Any same-uid client can set the same field
     `poll` sets.
   - C. Mint only when the captured snapshot is clean. A dirty
     local or MCP submit does not mint. A poll of a clean clone
     still mints when the other P7 gates pass. This changes P7
     for the operator's own dirty local submits.
   - Recommendation: A. B does not create a boundary the socket
     can enforce. C is a real gate, and it is a P7 behavior
     change, so it waits for an explicit choice. This plan does
     not add the field from B and does not add the clean-snapshot
     gate.

3. **Should artifact bytes be scanned before MCP or the dashboard returns them?**
   - A. Return the bytes. Say in the tool text and on the
     dashboard that the page is not masked.
   - B. Omit `artifacts` and `artifact_read` from MCP and the
     dashboard. The CLI keeps them.
   - C. Scan the bytes and mask matches. That reopens the P7
     rejection of an artifact scanner.
   - Recommendation: A.

4. **May the dashboard bind a port?**
   - A. No. Both options in the access model are socket clients
     with no listener. The worker's "no network listener" rule
     extends to these clients.
   - B. Bind `127.0.0.1` for a live browser UI.
   - Recommendation: A. B is a listener on the machine and needs
     its own sign-off.

5. **Who chooses the dashboard's visual direction?**
   - A. The options pull request builds both runnable programs and
     does not delete either. MB2090 picks on that pull request.
     The inspect and cancel pull requests implement the picked
     option.
   - B. This document picks the terminal view now.
   - Recommendation: A. The project rule is to show runnable
     options before the visual direction is chosen.

6. **Where is installation `168290590` written down?**
   - A. This document records the operator's name for the
     installation, and it says the mint reads `installation-id`
     without comparing that file to the number. `secrets.md` and
     `check-runs.md` stay as they are in this pull request. The
     numbered slice `m3-install-note` updates those two documents
     after sign-off. The private key stays out of the repository.
   - B. This pull request also edits those two documents.
   - C. This document omits the number and keeps saying pending.
   - Recommendation: A. The operator named the installation for
     this plan. The other two documents were written while it was
     pending. This pull request stays one file. The follow-up is
     a numbered slice so the pending wording does not stay
     unscheduled.

7. **What does a tool return for logs and artifacts?**
   - A. The protocol object, including `data_base64`.
   - B. The adapter decodes bytes to text and replaces bytes that
     are not UTF-8.
   - Recommendation: A. P11 is then a comparison of canonical
     JSON. A text tool can be a later slice.

8. **Does the dashboard submit?**
   - A. It lists, shows one run, pages logs and artifacts, and
     cancels. Submit stays the CLI, `poll`, and the MCP `submit`
     tool from question 1. The terminal option cancels from the
     view. The HTML option cancels by running the writer command,
     which then rewrites the file. The opened HTML file has no
     cancel control.
   - B. The dashboard also submits a version 1 job.
   - Recommendation: A. RR-35 and RR-36 are inspect and cancel.
     The HTML file is a snapshot with a write timestamp. Its
     command is the cancel path.

## Pull requests

This document is the sign-off pull request. The eight below start
after the answers are accepted, one at a time, each merged before
the next branch opens. Each is one independently testable slice.
The capability version stays 12. A version 11 plan is not
migrated. No plan field is added. `.github/workflows/check.yml`
is unchanged. `write` stays rejected. Tests point `HOME` at a
temporary directory when they touch a key path. No test reads
`~/Secrets/github-app/rookrunner-app/` and no test contacts
`api.github.com`. Existing contract, secret, job-token, and
artifact tests still pass. Ruff still passes.

1. **m3-mcp-read.** Depends on question 7 staying A. Add the stdio
   adapter and the tools `describe`, `get`, `list`, and `logs`.
   One tool call uses one socket connection. The tool result is
   the protocol object. There is no `submit` tool yet, and there
   is no tool for `run.status`, a fixture, `poll`, or the App key.
   The adapter binds no port and takes no key flag.
   Tests, in `tests/test_mcp.py`, using the development backend
   and a temporary state directory: canonical JSON of `get` and of
   one `logs` page equals the socket response for the same
   parameters; a second tool call on the same stdio session
   succeeds; exiting the adapter leaves the run's state as the
   worker stored it; a missing socket is a tool error and is a
   different kind from `RUN_NOT_FOUND`. The test reads the tool
   table and fails if `submit`, `run.status`, or a fixture tool is
   present. `check.yml` is byte-identical.
2. **m3-mcp-errors.** Depends on m3-mcp-read. A malformed tool
   call returns a tool error, and the same stdio session accepts
   a later `get`. When the worker answered, the error includes
   the worker `data.kind`. The adapter also rejects, before it
   dials, an unknown tool name, a missing required parameter such
   as `run_id`, and a cursor that is not a string of 1 to 1024
   characters. Those three errors use kind `INVALID_PARAMS`,
   `retryable` false, and the test fails if the adapter opened
   the socket for them. The test then builds four worker-answered
   outcomes and checks the kind on each: worker socket absent,
   `CAPABILITY_UNSUPPORTED`, a development run whose state is
   `failed`, and a run whose state is `lost`. The `lost` record
   is written through the store the development tests already
   use, without starting a container. A `failed` run is not
   reported as `lost`, and a `lost` run is not reported as
   `succeeded`. After each adapter-local rejection and after each
   worker-answered error, a later `get` on the same session
   succeeds.
3. **m3-mcp-evidence.** Depends on m3-mcp-read and on question 3
   staying A. Add `artifacts` and `artifact_read`. The tool takes
   the artifact id, which is a UUID, and it has no path
   parameter. A development fixture still returns
   `CAPABILITY_UNSUPPORTED` for artifacts, and the session stays
   up. For a workflow run, the artifact page matches the CLI
   page, including `id`, `path`, `size`, and `digest`.
   `artifact_read` of a workspace file whose bytes contain a
   fixed sentinel returns those bytes with the sentinel intact.
   The test does not add a mask. A path that leaves the attempt
   workspace is already covered by
   `test_publish_lists_only_written_files_and_pages_their_bytes`
   in `tests/test_artifacts.py`: that test plants a row whose
   path is `../outside` and expects `INTERNAL_ERROR` without the
   outside bytes. This pull request does not re-plant that row.
   The workflow case uses the same disposable-container setup as
   `tests/test_artifacts.py`.
4. **m3-mcp-submit.** Depends on m3-mcp-read and on question 1
   staying A and question 2 staying A. Add `submit` and `cancel`.
   `submit` sends version 1, `event: {}`, and no `event_name`. The
   test inspects the request the adapter wrote and fails if
   `event` is anything other than `{}`, if `event_name` is
   present, or if the request contains a key path, a fixture, or
   `backend`. A `submission_key` that starts with `poll-` returns
   a tool error and the test fails if the adapter opened the
   socket. The same `submission_key` and the same parameters
   return the same `run_id`. The same key with a different
   `job_id` returns `IDEMPOTENCY_CONFLICT` and creates no second
   run. The same key after a tracked file in the checkout changes
   returns the original `run_id` and the original snapshot id.
   The tool description says to use a new key after each edit and
   to compare snapshot ids. `cancel` sends version 0 and returns
   the worker record.
   Cancelling a queued run yields `cancelled`. The result is
   `succeeded` only when the record's state is `succeeded` and
   `exit_code` is 0. `allowlist_matches({}, (), (), None)` is
   false. This pull request leaves `_prepare_job_token` unchanged.
   A job that needs a token still
   mints when `--app-key` is set; the existing job-token tests
   cover that mint. The new test shows the adapter has no flag
   that passes a key.
5. **m3-dashboard-options.** Depends on question 4 staying A and
   question 5 staying A. Add two runnable local programs, both
   socket clients, neither binding a port. Each can show a queued
   run, a `failed` run, a `lost` run, a client that cannot open
   the socket, and a `CAPABILITY_UNSUPPORTED` error. The HTML
   command writes one file and exits. The file contains a UTC
   write timestamp and the run state as of that time, and it says
   the snapshot is stale after the timestamp. The file contains
   no control that calls the socket. The terminal option reads
   the same records. This pull request does not delete either
   option and does not choose a typeface, a color, or a layout as
   the product. The test runs each option against fixture records
   in a temporary directory and checks that the four states and
   the unsupported error appear in the output. The HTML test
   checks the timestamp and the state-as-of text. The test fails
   if either program binds a socket that accepts connections.
6. **m3-dashboard-inspect.** Depends on m3-dashboard-options, on
   MB2090's recorded choice, and on m3-mcp-evidence for the
   artifact behavior it shows. Implement the chosen option.
   Remove the other option in this pull request. The view shows
   the snapshot id, the state, the steps, a log page, and an
   artifact page, using the socket. A log longer than one page is
   read with the cursor. Reopening the terminal view shows the
   same `run_id` and state. For the HTML option, reopening means
   running the writer again: the new file has a later timestamp
   and the same `run_id`, and the previous file is unchanged.
   The artifact view states that the bytes are not masked. The
   test drives the chosen option against a worker in a temporary
   state directory and checks those fields. Keyboard navigation
   is part of the test when the chosen option is the terminal
   program. The HTML test reads the written file and checks the
   same fields, the timestamp, and the staleness sentence.
7. **m3-dashboard-cancel.** Depends on m3-dashboard-inspect and on
   question 8 staying A. The chosen option's cancel path sends
   `run.cancel` with version 0 and then renders the record the
   worker returned. On the terminal option that path is the view.
   On the HTML option it is the writer command, which then writes
   a new file. The opened HTML file still has no cancel control.
   A queued cancel shows `cancelled`. A `lost` run shows `lost`
   and the cleanup value on the record. The chosen option has no
   control that submits a run. The test cancels a queued
   development run and checks the rendered state is `cancelled`.
   A second test feeds a `lost` record with cleanup `unresolved`
   and checks the view shows both values. The rendered result is
   `succeeded` only when the record's state is `succeeded` and
   `exit_code` is 0.
8. **m3-install-note.** Depends on question 6 staying A. Docs
   only. Update `secrets.md` and `check-runs.md` so they say the
   operator names the installation as `168290590`, the mint reads
   `installation-id`, and the code does not compare the file to
   that number. The private key stays out of the repository.
   This slice does not read `~/Secrets` and does not change code.
   `check.yml` is unchanged. The test is the diff: those two
   documents and no other file.

Pull requests 4 and 7 change if question 1 or question 8 is
answered B. Pull request 4 is dropped when submit is out. Pull
request 5's programs change if question 4 is answered B, and that
answer needs a new threat-model note before the branch opens.
Pull request 8 is dropped if question 6 is answered C. If
question 2 is answered C, the clean-snapshot mint gate is its own
pull request ahead of m3-mcp-submit, and this list gains that
slice before the branch opens.

## What this plan leaves as it is

The worker socket stays mode `0600`. The state directory stays
mode `0700`. The protocol method list stays the list
`worker.describe` already returns. The capability version stays
12. `check.yml` stays unchanged. `write` stays rejected. The job
token stays Contents read for the installation named in
`installation-id` (the operator names it as `168290590`), minted
only under the P7 gates, revoked when the job ends, and absent
from the run record. A dirty snapshot still does not post. File secrets still follow
`allowlist_matches`. `artifact.read` still returns unmasked bytes.
`poll` and `status` still own the check-run post. No App
permission is added. No sign-in is started. No file under
`~/Secrets` is read by this document.
