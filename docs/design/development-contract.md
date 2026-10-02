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
| `run.cancel` | `version: 0`, `run_id` | none |

`run.artifacts` and `artifact.read` return `CAPABILITY_UNSUPPORTED`.
Unknown methods return `METHOD_NOT_FOUND`. On version 0, workflow, command,
secret, and source selection parameters are unsupported and rejected.

Version 1 `run.submit` is separate. It requires `workflow`, `job_id`, `event`,
and an image pinned by digest. It captures and plans one sequential `run` job,
then stores a queued run. The stored image digest keeps the `sha256:` prefix.
It does not execute steps. Unsupported workflow
fields and invalid YAML create no run. Version 0 fixtures are unchanged.

`worker.describe` advertises the supported methods, capabilities, worker identity,
repository, version, readiness, and limits. A scheduler failure makes `ready=false`
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
there is no pruning or key expiry in this prototype. Persistent storage is not
bounded yet, so this is a development service rather than an unattended release.
The retry window is therefore the entire lifetime of the retained worker state,
including across restarts. No tombstone expiry is needed because no run or key
can be pruned through the API. Deleting the state directory outside the service
discards its identity and history; it is not a supported retry/retention operation.
Artifacts and event payloads are unsupported, so no artifact/event retention is
implied. The captured fixture and logs are SQLite values committed atomically
with acceptance/completion, not references to mutable checkout files.

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
| -32000 | `VERSION_UNSUPPORTED`, `CAPABILITY_UNSUPPORTED`, `QUEUE_FULL`, `RUN_NOT_FOUND`, `CURSOR_EXPIRED`, `IDEMPOTENCY_CONFLICT`, `WORKER_NOT_READY`, `STORAGE_FULL` |

Startup root mismatch fails before serving requests. SQLite full errors on protocol
operations produce `STORAGE_FULL`; failed acceptance is rolled back without
consuming the submission key. Other database errors produce sanitized
`INTERNAL_ERROR` replies. If the scheduler cannot persist a transition, it stops
and reports not-ready; restart reconciles any active attempt as lost. Broader
disk budgets, retention, and external-resource recovery remain M2 work.

CLI results are JSON-RPC envelopes. Exit zero means the requested protocol
operation succeeded, including fetching a failed or active run. **A successful
execution requires `result.state == "succeeded"` and `result.exit_code == 0`, for
the expected `result.input.digest`.** Queued acceptance is not execution success.
Protocol errors exit 1 and print JSON to stdout; local transport/startup errors
exit 1 and print JSON to stderr.

Durable format migrations, doctor diagnostics, external backend supervision, and
release compatibility guarantees remain later milestones. The v0 schemas and
checks establish the M1 development contract, not compatibility with workflows.
