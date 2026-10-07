# Implemented development contract

Status: implemented M1 contract, protocol version 0. The broader
[workflow protocol draft](protocol.md) remains proposed. Machine-readable
[schemas and examples](../../schemas/v0/README.md) define the supported subset.

## Transport

One request and response per Unix socket connection. Both are UTF-8 JSON objects
terminated by a newline. JSON-RPC `jsonrpc` is `"2.0"`; an integer or string `id`
is required: strings are 1–128 Unicode characters; numeric IDs are integers in
[-9007199254740991, 9007199254740991]. Integral numbers such as `1.0` are accepted;
booleans are not numbers. Methods are 1–128 characters. All textual input must
be valid UTF-8, including after JSON escape decoding.
Notifications and batches are unsupported and receive invalid-request
errors. Extra envelope and method parameter fields are rejected. Connections have
a one-second worker I/O timeout; clients use five seconds. Protocol requests are
served serially, independently of the execution scheduler.

Duplicate keys, non-finite numbers, invalid UTF-8 JSON, and more than 64 nested
containers receive `PARSE_ERROR`. Invalid envelopes receive `INVALID_REQUEST`
with a null response ID. Once the envelope is accepted, errors echo its ID.
Missing/extra/wrongly typed parameters produce `INVALID_PARAMS`. Wire framing
is checked before parsing; missing newline and oversized frames produce
`INVALID_REQUEST`. Idle/incomplete clients can time out and disconnect without
a response. Every request requires a new connection.

The state directory must be user-owned mode 0700 and cannot itself be a symlink.
The socket is mode 0600. A second worker using the same state directory fails
before modifying its socket. One canonical repository path is persisted per
state directory; another repository cannot reuse it. Distinct state directories
are independent workers; this prototype does not provide a machine-wide
repository lock. The repository must exist but need not yet be a Git checkout.

## Operations

| Method | Required parameters | Optional parameters |
| --- | --- | --- |
| `worker.describe` | none | none |
| `run.submit` | `version: 0`, `submission_key`, `backend: "development"`, `fixture` | none |
| `run.get` | `run_id` | none |
| `run.list` | none | `cursor`, `limit`, `state` |
| `run.logs` | `run_id` | `cursor`, `limit` |
| `run.artifacts` | `run_id` | `cursor`, `limit` |
| `artifact.read` | `artifact_id` | `offset`, `limit` |
| `run.cancel` | `version: 0`, `run_id` | none |
| `run.status` | `run_id`, `tested_commit`, `status_sha`, `context` | `record` |

`run.artifacts` on a development fixture returns `CAPABILITY_UNSUPPORTED`.
On a workflow run it returns a page of artifact entries and `next_cursor`.
An entry has `id`, workspace-relative `path`, `size`, and SHA-256 `digest`.
A workflow run that wrote no files returns an empty list, not a capability
error. `artifact.read` returns base64 bytes, `next_offset`, and
`end_of_stream`. A path that leaves the attempt workspace is rejected.
`run.status` decides whether one commit status may be posted. It refuses,
with no recorded post, unless the run is a workflow run, the snapshot is
clean, `included` is empty, and `base_commit` equals `tested_commit`. The
credential is not a parameter. `record` stores the state that was posted.
The same terminal state for that run, context, and status SHA is `skip`.
Unknown methods return `METHOD_NOT_FOUND`. On version 0, workflow, command,
secret, and source selection parameters are unsupported and rejected.

Version 1 `run.submit` is separate. It requires `workflow`, `job_id`, and
`event`. `image`, when present, is a digest pin. When it is omitted, the
worker uses `--runner-image` only if every selected job is literal
`runs-on: ubuntu-latest`. Without that flag, the omission is rejected and
no run is stored. It captures and plans one sequential `run` job,
then stores a queued run. When `event_name` is `push` or `pull_request`,
`on` is checked before that run is stored. Optional parameters for that
check are `activity_type`, `changed_files`, `commit_count`, and
`diff_unavailable`. A workflow that does not match returns
`{"triggered": false}`, creates no run, and does not consume the
submission key. `workflow_dispatch` and `schedule` are not evaluated.
Without `event_name`, `on` is not evaluated. The caller supplies the
changed-file list. A push with `commit_count` above 1,000, or
`diff_unavailable`, skips path filters. Only the first 3,000 changed
files count. Branch, tag, and activity-type filters still apply. A tag push skips the path filters. `tags` and `tags-ignore` under `pull_request` are ignored. NS-42's `poll` command supplies this check for one owner repository and then exits. NS-43 recorded those statuses ([validation](../validation/owner-ci-rookrunner.md)). NS-44 evaluates expressions in `run`, `env`, `with`, and step and job `name`, including mixed text. NS-45 evaluates `concurrency` and `cancel-in-progress` on this one worker. With `queue: max`, at most 100 runs can be pending in a group. NS-46 names selected workspace files in the artifact manifest and records one local CodeQL SARIF file ([upload artifact](upload-artifact.md)). The capability version stays 12. P4 accepts `actions/checkout@v` plus digits as an owned checkout and copies ancestor history when that plan sets `fetch-depth` to the YAML integer `0` ([checkout tag](checkout-tag.md)). Omitting `fetch-depth` keeps one parentless synthesized commit and still excludes the original commit. `.github/workflows/check.yml` is unchanged. P6 runs the operator-built image when `worker --runner-image` is set and `image` is omitted and every selected job is literal `runs-on: ubuntu-latest` ([runner image](runner-image.md)). An explicit `image` still wins. Any other image stays a caller pin. `run_job` still requires a digest and still does not select a default. P5 posts one check run through the Rookrunner GitHub App when `--app-key` is set, then posts the commit status with that installation token. Omitting `--app-key` keeps the NS-40 token file. The plan schema is unchanged. P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions for owner-repository push and pull-request runs on this one local worker ([secrets](secrets.md)). p7-mask changes the job mask. p7-trust-gate compares a pull request's head and base repository ids, records a null head repository as a fork, and copies the repository id, the default branch, and the actor login into the stored event. The allowlist match is a pure function of that event. p7-socket-lock adds the worker secret flags and refuses --docker-socket combined with --app-key or --secrets unless the ~/Secrets probe exits 0. p7-secret-env reads a file secret into a step env or with expression that is exactly secrets.NAME when --secrets is set and the allowlist matches, and passes those secret registrations into reusable-workflow call outputs. p7-secret-run is implemented. p7-job-token is implemented. MB2090 accepted the amended answers on 2026-10-05. The rest of the provisional list is not numbered yet. The stored image digest keeps the `sha256:` prefix.
The worker records an attempt and executes that plan. Success is `succeeded`
with exit code 0. A nonzero step is `failed` with that exit code. A setup
failure is `failed`, with a null exit code and a structured error. Closing
the client does not stop the container. The job container is attached to
Docker network `bridge` unless the worker was started with `--network none`.
`bridge` gives the job outbound access to the public internet. GitHub-hosted
runners have that access by default
(https://docs.github.com/en/actions/concepts/runners/private-networking).
`none` is the documented way to turn it off. Other network names are
rejected. The Docker socket stays unmounted unless the worker is started
with `--docker-socket`. That flag bind-mounts the engine socket this worker
already uses into the job at `/var/run/docker.sock`. The job keeps the
caller uid. It is added to the socket's group and to group 0, because the
mounted socket is often mode `0660` and some engines present it as owned
by root. The job image must already contain the Docker client. Rookrunner
does not install one into a caller image. `worker --runner-image` selects the operator image, which includes the client, when `image` is omitted and every selected job is literal `runs-on: ubuntu-latest`. GitHub requires Docker to be
installed and the service running for container-dependent work on a
self-hosted runner
(https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners).
A missing socket fails setup and creates no container. Host credential
directories are not mounted. The container is not privileged. When the
socket is mounted, a private Docker volume is mounted into the job at
that volume's mountpoint. `TMPDIR`, `TEMP`, and `TMP` default to it,
and a later env layer can replace them. The volume is removed with the
job container. A bind of the host's `/tmp` is not used. On that shared
folder, chmod of a Unix socket fails and Git does not see a repository
it just created. The host `/tmp` is not mounted. A service container
does not receive the volume. This is not a
private-network, nested-daemon, or egress-policy implementation. A job may
declare service containers. Each image must be digest-pinned. GitHub allows
a tag. The runner creates a user-defined bridge network for that job and
the service label is the hostname. The service container does not receive
the engine socket, including when the job was started with
`--docker-socket`. `credentials`, `volumes`, `options`, and `ports` create
no run. `worker --network none` does not start service containers. Cancel
and restart remove them and the network. A job may
declare `strategy` with `fail-fast`, `max-parallel`, and a literal `matrix`.
Include and exclude are expanded at plan time. `fail-fast` defaults to true.
Combinations of that job run one at a time in the same container and
workspace. `max-parallel` does not start more containers. GitHub publishes
no numeric ceiling for `max-parallel`. A matrix that would generate more
than 256 jobs creates no run
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
An unknown `strategy` key creates no run. A matrix expression is not
evaluated. A job may call a local reusable workflow with `uses` of
`./.github/workflows/<file>.yml` or the same `$/` path. Inputs are
`boolean`, `number`, or `string`. Secrets are not passed, including
`secrets: inherit`. A call chain longer than ten workflows creates no run
(https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#nesting-reusable-workflows).
More than 50 unique called workflows creates no run
(https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#limitations-of-reusable-workflows).
Exactly ten levels and exactly 50 unique workflows are accepted. Remote
reusable workflows are still rejected. Unsupported workflow
fields and invalid YAML create no run. A workflow file larger than 500 KB
creates no run; that rejection is a capability error. The job time bound is
`timeout-minutes`, default 360 minutes, and a value above 5 days is a
capability error. GitHub-hosted job execution time is 6 hours. Self-hosted
job execution time is 5 days. A step `timeout-minutes` is optional, has no
default, and cannot exceed 360 minutes
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
The worker enforces those bounds. A job timeout stops the owned container and
records `cancelled` with a null exit code, a null error, and
`cancel_requested` false. A step timeout stops the container, skips later
steps, and records `failed` with a null exit code and error kind
`STEP_FAILED`. A run that finishes inside the timeout is unchanged. The stop
uses the cancellation grace of SIGINT, 7500 ms, SIGTERM, then 2500 ms
(https://docs.github.com/en/actions/reference/workflow-cancellation-reference).
Caller `run.cancel` on a queued workflow records `cancelled` and does not
start a container. On a running workflow it stops the owned container with
that same grace. The container's main process is a shell that exits on
SIGINT or SIGTERM, so the grace returns as soon as the container stops.
`sleep` keeps the container alive as a child of that shell. The run is
recorded `cancelled` with `cancel_requested` true only
after the container and its service containers are gone. If one is still
present, the run is `lost`, with `cancel_requested` true, error kind
`WORKER_INTERRUPTED`, and cleanup `unresolved`. A new workflow submission
then returns `WORKER_NOT_READY` and a queued workflow job is not started.
Development fixtures still run, and `worker.describe` `ready` stays the
scheduler flag. That block is in memory and ends when this process stops.
Restart removes a container recorded for the attempt, service containers
labeled for it, and their network, or records the attempt `lost` with
cleanup `unresolved` and `cancel_requested` false. The same submission key
returns that run. It is not launched again. A queued workflow can start. A
new attempt that would reuse the unresolved container name or attempt
workspace is refused. The container name is not stored on the run record.
A job matrix is expanded at plan time. GitHub's matrix limit is 256 jobs
per workflow run. Those limits are documented at
https://docs.github.com/en/actions/reference/limits.
`run.logs` pages the executed steps' stdout and stderr with the same 1–65536
byte pages as development fixtures. `end_of_stream` is true only after the
run is terminal and those bytes are consumed. A finished workflow attempt
publishes the regular files it wrote under its workspace. Unchanged snapshot
files are omitted. Symlinks are not followed. `run.artifacts` pages that
manifest with the existing list page of 100. `artifact.read` pages bytes with
the existing 65536-byte log page. GitHub's artifact storage quota depends on
the plan and the limits page states no per-file or per-job count
(https://docs.github.com/en/actions/reference/limits). The manifest rows stay in the run database. When the run is terminal and
container cleanup is confirmed, the attempt directory is removed. A later
`artifact.read` reports that the bytes are not available. This is not an
upload-artifact zip. NS-46 names selected files in that manifest,
including files that match the snapshot, and still does not zip.
Step `if`, job `if`, and job outputs are evaluated with the documented
operators, types, and functions used by this subset
(https://docs.github.com/en/actions/reference/workflows-and-actions/expressions).
Contexts follow the availability table
(https://docs.github.com/en/actions/reference/workflows-and-actions/contexts).
An unavailable context is an error. A missing property of an available
context is an empty string. Workflow `env`, job `env`, step `env`,
`run`, the calling step's `with`, and step and job `name` are
evaluated, including mixed text. A composite `run` is evaluated.
Workflow `name` stays literal. Service `env` stays literal. An action
output `value` is evaluated only when it is one whole expression.
A concurrency group on a workflow or job is stored and evaluated when
the run is accepted. `secrets` is not available there. `queue: max`
with a true `cancel-in-progress` is rejected. On this one worker,
`queue: single` replaces another queued run in that group,
`cancel-in-progress` also cancels the running run, and `queue: max`
keeps at most 100 pending runs.
Job outputs have no special functions in that table, so status functions and
`hashFiles` are not accepted there. `hashFiles` is not implemented. The
selected job runs after the jobs it needs, one at a time, in one
caller-pinned container and one attempt workspace. An output expression that
names `secrets` is not copied. GitHub skips an output whose value contains a
registered secret; this subset has no secret store, so it withholds the
`secrets` context instead of scanning values. Job outputs use the documented
1 MB per job and 50 MB per workflow run, measured here as 1024-based
UTF-16-LE bytes
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
The syntax page says 1 MB and 50 MB and does not define MB. `case` matches
the expression reference and does not evaluate an unused branch. A property
name may contain `-`. `env.MY-VAR` is that property. The operators table
does not list subtraction, and `-` is not subtraction here.
`GITHUB_ENV`, `GITHUB_OUTPUT`, and `GITHUB_PATH` apply to later steps in the
same job
(https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands).
The writing step does not see its own env or PATH update. `GITHUB_*` and
`RUNNER_*` are ignored, and `GITHUB_ENV` cannot set `NODE_OPTIONS`
(https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
`set-env` and `add-path` are not applied. `add-mask` masks later logs in
that job, including each whitespace-separated word, the base64, JSON, and
percent-encoded forms, and the `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, and
`github_pat_` token prefixes. A partial line is held until the next read.
The commands page says a masked value cannot be set as an output, and its
example writes the value to `GITHUB_OUTPUT` and reads it back. This subset
follows the example: the step output is kept and the log is masked. A job
output that contains an injected secret or a token prefix is omitted, and
the omission is recorded without the value. An `add-mask` value in a job
output is stored as masked text. A later job does not inherit the mask. Workflow `run` and YAML `env` text are
evaluated, including mixed text. A local composite action reads inputs from `with` and the `inputs`
context. Composite actions do not receive `INPUT_*`. `github.action_path`
and `GITHUB_ACTION_PATH` are set only for steps inside that action. A
remote `node24` action with `main` receives `INPUT_*`. Its `GITHUB_OUTPUT`,
env, and PATH files apply to later steps. `GITHUB_STATE` is stored for
that action and is not exported to other steps. The action's `post`
receives those values as `STATE_<name>` and the same inputs. An omitted
`post-if` is `always()`. A failed post fails the job and does not replace
an earlier main-step failure. A caller cancel or a job deadline does not
run `post`. `GITHUB_STEP_SUMMARY` is kept as step evidence and is not
rendered. `github.action_path` is not set for that process. This is not
a GitHub-equivalence claim.
Version 0 fixtures are unchanged.

`worker.describe` advertises the supported methods, capabilities, worker identity,
repository, version, readiness, and limits. `worker --node24 DIR` adds `node24`
to that result, with the directory's content digest and mount `/opt/node24`.
Without the flag the property is absent. A workflow run that uses the mount
records the same digest and the Node 24 version printed in the job container.
A scheduler failure makes `ready=false`
with a `readiness_error`; new submissions then return `WORKER_NOT_READY`.
Existing idempotent submissions can still retrieve their original record.
Inspect records and restart to reconcile interrupted attempts before submitting
new work. This readiness check describes the synthetic backend only; owned-engine/container
doctor diagnostics belong to M2.

The fixture object permits only:

- `exit_code`: integer 0–255, default 0.
- `delay_ms`: integer 0–5000, default 0.
- `output`: UTF-8 text, maximum 65536 encoded bytes, default
  `"development fixture completed\n"`.

The worker waits the requested duration, then records the output and exit code.
It never executes fixture text. Logs become available upon completion. Empty
output during queued/running states is not end-of-stream. Synthetic execution has
no artifacts, images, workflows, or source files. Its snapshot ID/digest is SHA-256
of the stored normalized backend/fixture JSON (sorted keys, compact separators,
ASCII-escaped strings); input kind is `development_fixture`.

Example request:

```json
{"jsonrpc":"2.0","id":1,"method":"run.submit","params":{"version":0,"submission_key":"example-1","backend":"development","fixture":{"delay_ms":100,"exit_code":0,"output":"hello\n"}}}
```

This invalid fixture cannot create work:

```json
{"jsonrpc":"2.0","id":2,"method":"run.submit","params":{"version":0,"submission_key":"invalid-1","backend":"development","fixture":{"command":"make test"}}}
```

## Persistence, pagination, and limits

Submission keys are 1–128 characters and scoped to the state directory's stable
worker ID. Omitted fixture defaults normalize to explicit defaults. Same key and
normalized input return the existing run; changed parameters produce
`IDEMPOTENCY_CONFLICT`. All runs, inputs, logs, and keys are retained indefinitely;
there is no key expiry in this prototype. When a run is terminal and container
cleanup is confirmed, its attempt directory is removed. An unresolved cleanup
keeps that directory. New submissions stop when the configured disk budget
would still be exceeded after those finished directories are removed. That
refusal does not delete runs, keys, snapshots, or an active attempt.
The retry window is therefore the entire lifetime of the retained worker state,
including across restarts. No tombstone expiry is needed because no run or key
can be pruned through the API. Deleting the state directory outside the service
discards its identity and history; it is not a supported retry/retention operation.
Workflow artifact manifest rows stay in the run database. The attempt directory
does not. Event payloads are still unsupported. Development fixtures
publish no artifacts. The captured fixture and logs are SQLite values committed
atomically with acceptance/completion, not references to mutable checkout files.

The request limit is 1 MiB including its newline. Queue capacity is 100 queued
runs, excluding the single active run. Log pages are 1–65536 bytes, default 65536;
list pages are 1–100 records, default 20. Log bytes are base64 encoded and cursors
are opaque, scoped to a run. List cursors are scoped to the worker and state
filter. Lists use insertion order; they are live views, not consistent snapshots
across changing state filters. A null list cursor means no further entries at
the time of the query. Log pages always return a next cursor; finality is indicated
by `end_of_stream`, after all output has been read and the run is terminal.
Run identifiers accepted as lookup parameters are nonempty UTF-8 strings of at
most 128 characters; generated IDs are UUIDs. Cursors are nonempty UTF-8 strings
of at most 1024 characters. Malformed, wrong-query, or out-of-range cursor content
produces `CURSOR_EXPIRED`; decoded offsets are bounded to signed 64-bit range.
`run.list` accepts a null state filter as no filter. Runs with unknown IDs produce
`RUN_NOT_FOUND`; unknown state filters and page sizes outside their bounds produce
`INVALID_PARAMS`. Successful responses fit within the same 1 MiB client frame cap.

Cancellation is serialized with completion. First terminal state wins. Active
development fixtures can be cancelled immediately because they own no external
resources. The scheduler may finish its bounded wait before starting another
fixture. Restart marks formerly active runs `lost` with a null exit code and
`WORKER_INTERRUPTED`; it does not retry them. Queued work remains eligible.

## Errors and CLI status

Errors have numeric `code`, readable `message`, and `data.kind`/`data.retryable`.
The current worker never automatically recommends retries (`retryable: false`).

| Numeric code | Kinds |
| --- | --- |
| -32700 | `PARSE_ERROR` |
| -32600 | `INVALID_REQUEST` |
| -32601 | `METHOD_NOT_FOUND` |
| -32602 | `INVALID_PARAMS` |
| -32603 | `INTERNAL_ERROR` |
| -32000 | `VERSION_UNSUPPORTED`, `CAPABILITY_UNSUPPORTED`, `QUEUE_FULL`, `RUN_NOT_FOUND`, `CURSOR_EXPIRED`, `IDEMPOTENCY_CONFLICT`, `WORKER_NOT_READY`, `STORAGE_FULL`, `SOURCE_UNSTABLE`, `ACTION_UNAVAILABLE` |

Startup root mismatch fails before serving requests. SQLite full errors on protocol
operations produce `STORAGE_FULL`; failed acceptance is rolled back without
consuming the submission key. Other database errors produce sanitized
`INTERNAL_ERROR` replies. If the scheduler cannot persist a transition, it stops
and reports not-ready; restart reconciles any active attempt as lost. A
killed worker's owned container is removed on restart or left unresolved.
A configured disk budget covers the state directory, including snapshots,
fetched action trees, and
attempt workspaces. The default is GitHub Actions cache storage, 10 GB per
repository on every plan in the storage table
(https://docs.github.com/en/actions/reference/limits), stored as
`10 * 1024 * 1024 * 1024` bytes. GitHub documents 10 GB and does not define
the byte multiple; this repository uses 1024, matching its 500 KB
workflow-file limit. GitHub may evict cache entries past a repository cache
limit. This budget does not delete snapshots, run records, or an attempt
whose container cleanup is still unresolved. Before it refuses a submission,
it removes attempt directories for terminal runs whose cleanup is confirmed.
A submission that still would exceed it returns
`STORAGE_FULL`, creates no run, and does not consume the submission key. The
same key still returns a run that was already accepted. The snapshot captured
for a refused workflow submission is removed because it is not yet evidence
of a run. Active runs and their snapshots are not deleted. SQLite full errors
still roll back acceptance the same way. Runs and keys stay until the state
directory is removed. The budget is worker
configuration and is not a `worker.describe` limit. GitHub Free artifact
storage, 500 MB, is an account artifact quota and is not this budget.

CLI results are JSON-RPC envelopes. Exit zero means the requested protocol
operation succeeded, including fetching a failed or active run. **A successful
execution requires `result.state == "succeeded"` and `result.exit_code == 0`, for
the expected `result.input.digest`.** Queued acceptance is not execution success.
Protocol errors exit 1 and print JSON to stdout; local transport/startup errors
exit 1 and print JSON to stderr.

`submit` sends version 0 when workflow flags are omitted. That command
requires `--backend development` and `--key`. Omitted `--exit-code`,
`--delay-ms`, and `--output` use the fixture defaults above.

`submit` sends version 1 when `--workflow`, `--job-id`, and `--event` are
present. `--event` is one JSON value. `--image` is optional. When it is
present it is the caller's digest pin and is not rewritten by the CLI.
`--backend development`
may be present on that command and is not sent; version 1 has no backend
field. The source is the repository the worker was started with. Fixture
options cannot be combined with a workflow submit. A partial set of those
flags exits 2 before a request is sent.

`follow RUN_ID` polls `run.get` and pages `run.logs` until the run is
terminal and `end_of_stream` is true. It prints one canonical JSON-RPC
envelope for each state change and each nonempty log page. The pause between
polls is client pacing, not a protocol or Actions limit. Exit zero means the
run is `succeeded` with exit code 0. Any other terminal state exits 1 after
those envelopes. A protocol error exits 1. `get` and `logs` stay one-shot:
they still exit zero when the protocol call succeeds, including when the run
failed.

Durable format migrations, doctor diagnostics, external backend supervision, and
release compatibility guarantees remain later milestones. The v0 schemas and
checks establish the M1 development contract, not compatibility with workflows.
