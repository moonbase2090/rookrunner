# GitHub runner registration prototype

Status: prototype, off by default. This is not accepted direction. It does
not change the accepted owned-engine decision or the
[owner CI path](owner-ci.md). It follows the proposals in the
[managed GitHub runners research](../research/managed-github-runners.md),
which remain proposals.

## Implemented behavior

One new module, `src/execution_core/github_runners.py`, uses only the
standard library. Nothing imports it. There is no CLI flag, no listener, no
dependency, and no change to the plan, protocol, or worker.

Every entry point refuses with `FeatureDisabled` unless
`ROOKRUNNER_EXPERIMENTAL_GITHUB_RUNNERS=1` is set. Callers and tests may
pass an `environ` mapping instead of using the process environment.

Registration client (`RunnerClient`):

- Requests `generate-jitconfig` and `registration-token` for a repository or
  organization `Scope`, with an injected token provider and API base URL.
- Validates the runner name, labels, runner group id, work folder, owner,
  and repository name before any request. The base URL must be https, or
  http on loopback for a local fake.
- Wraps the encoded JIT config, registration token, and webhook secret in
  `Secret`, whose `repr` and `str` are redacted. Errors carry the HTTP status
  only, never a response body or token. Redirects are refused so a bearer
  token is not forwarded. Nothing is logged or written to disk.

Webhook intake (`handle_delivery`, `WebhookStore`), a pure function with no
socket:

- Bounds the body size, then verifies `X-Hub-Signature-256` (HMAC-SHA256,
  constant-time comparison) over the raw bytes before parsing JSON.
- Accepts only the `workflow_job` event with action `queued`, `in_progress`,
  or `completed`. It requires both the installation id and the repository to
  be on an allowlist. An empty allowlist denies everything.
- Dedupes on `X-GitHub-Delivery` in a sqlite file under a caller-given
  state directory (mode 0600). Delivery identity, the latest report per job,
  and the demand queue are separate tables. A redelivery, even after the
  store is reopened, does not enqueue again.
- Treats a notification as a hint. A job reported `in_progress` or
  `completed` clears its pending demand, and a late `queued` does not
  recreate it. A job's reported state only moves forward, so a late
  `in_progress` cannot erase a `completed` report. A reported conclusion is stored as GitHub's word. The store
  has no local result column, so nothing here can become a local
  `succeeded`, which still needs a terminal result with `exit_code=0`.

## How to enable

Set `ROOKRUNNER_EXPERIMENTAL_GITHUB_RUNNERS=1` and call the module from
Python. Keep the state directory outside the repository.

## Not implemented

- No HTTP server wrapper, TLS, or response deadline handling.
- No provisioning, runner process, cleanup, or reconciliation against the
  GitHub jobs API. The demand queue has no consumer.
- No retries, rate-limit handling, or bounded redelivery recovery.
- No runner listing or deletion, and no GitHub App token minting.
- No admission policy, isolation, or runner binary. No code is imported.

## Unvalidated

No live GitHub registration or webhook delivery was performed. Request
shapes follow the REST reference and were checked only against a local fake
server. The HMAC check uses the documented test vector. The App
permissions, installation scope, and whether a webhook controller is the
right approach against existing controllers are open questions in the
research note.
