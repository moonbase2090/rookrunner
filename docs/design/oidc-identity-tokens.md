# OIDC identity tokens for jobs

Status: accepted direction. Open questions 1 to 5 were decided on
2026-10-17; the answers are under "Decisions" at the end. Questions 6
and 7 stay open. Phases 1 to 3 are implemented: keys and `oidc
publish` (`oidc_keys.py`), claims and the mint decision
(`oidc_claims.py`), and the per-job endpoint and contract behind
`worker --oidc-issuer`, off by default (`oidc_endpoint.py`). Phase 4,
hosting and the live cloud checks, is not done. The capability version
stays 12: a plan is made and run by the same engine, and an engine
without OIDC runs an `id-token: write` job without the contract, as
GitHub does when no token is available. Nothing here is a GitHub-equivalence claim.

## Goal

A private repository whose GitHub Actions jobs authenticate to AWS
(`AssumeRoleWithWebIdentity`) or to Microsoft Entra ID (a federated
identity credential and the `client_credentials` token exchange) can
move those jobs to Rookrunner without a long-lived cloud secret.

Rookrunner becomes an OpenID Connect issuer. A job that asks for
`id-token: write` can request a short-lived signed JWT for one
audience. The cloud provider validates that JWT against a public
JWKS and trades it for its own short-lived credential.

`aws-actions/configure-aws-credentials` and `azure/login` should work
unchanged. Both are `node24` actions with `main`, `post`, and
`post-if`, which the engine already runs. Both read the token through
`ACTIONS_ID_TOKEN_REQUEST_URL` and `ACTIONS_ID_TOKEN_REQUEST_TOKEN`.

This is not a GitHub-equivalence claim. GitHub's issuer is
`https://token.actions.githubusercontent.com`. Rookrunner's issuer is
a different URL, so every cloud trust policy names the new issuer.

https://docs.github.com/en/actions/concepts/security/openid-connect
https://docs.github.com/en/actions/reference/security/oidc

## What the engine does today

These are implemented facts.

- The planner records `id-token: none` and `id-token: read`, and it
  rejects `id-token: write` with every other `write`.
  `docs/design/secrets.md` lists "`id-token` as a minted OIDC token"
  under what stays rejected.
- Step environments are built in `run.py` (`_merged_env`). Names in
  `_reserved_env` cannot be replaced by a step. `GITHUB_TOKEN` is
  added last, only while the job holds a Contents-read token
  (`job_token.py`).
- The job mask registers secret values and their encoded forms before
  the first step (`commands.py`).
- The poll refuses forks by numeric repository id.
  `allowlist_matches` decides which runs receive file secrets. A
  run started with `--allow-untrusted` or `--allow-pr-workflow`
  records that override.
- The container network is `none` unless the worker was started with
  `--network bridge`.
- `checks.py` already signs RS256 JWTs without a new runtime
  dependency.

## Rookrunner as an issuer

### Issuer URL

The issuer is one stable HTTPS URL with no query and no fragment,
called `<issuer>` below. The decided issuer is
`https://oidc.rookrunner.com`. Examples below use
`https://oidc.example.com`. The `iss` claim equals that string byte
for byte. AWS and Entra both compare it exactly.

Two static JSON files are served under it:

- `<issuer>/.well-known/openid-configuration`
- `<issuer>/.well-known/jwks.json` (the `jwks_uri`)

The discovery document:

```json
{
  "issuer": "https://oidc.example.com",
  "jwks_uri": "https://oidc.example.com/.well-known/jwks.json",
  "subject_types_supported": ["public"],
  "response_types_supported": ["id_token"],
  "id_token_signing_alg_values_supported": ["RS256"],
  "scopes_supported": ["openid"],
  "claims_supported": [
    "iss", "sub", "aud", "exp", "iat", "nbf", "jti",
    "repository", "repository_id", "repository_owner", "ref",
    "ref_type", "sha", "run_id", "run_attempt", "workflow",
    "workflow_ref", "job_workflow_ref", "event_name", "actor",
    "environment", "runner_environment"
  ]
}
```

Neither file is secret. Neither changes per job. The worker never
serves them, so the self-hosted machine needs no inbound port.

### Hosting

The proposal is a static host with a custom domain and a public CA
certificate. Three choices meet the AWS and Entra fetch rules:

1. Cloudflare R2 with a public custom domain. Two objects, served
   with `Content-Type: application/json`. Upload with any
   S3-compatible client.
2. Cloudflare Workers static assets. The same two files, plus a
   place to add headers or a second key set later.
3. GitHub Pages on a small public repository with a custom domain.
   The file `.nojekyll` is required, or `.well-known` is not
   published.

All three are HTTPS on a public CA, need no server on the worker host,
and survive a worker outage. The project chose option 2: Cloudflare Workers
static assets, served alongside the Rookrunner website at
`oidc.rookrunner.com`. Publishing there is phase 4.

`rookrunner --state DIR oidc publish --issuer URL --out DIR` writes
`DIR/.well-known/openid-configuration` and `DIR/.well-known/jwks.json`
from the configured keys. The operator uploads `DIR`. The command
reads the key files to derive their public halves and writes only the
public members `kty`, `use`, `alg`, `kid`, `n`, and `e`. It refuses an
issuer that is not `https`, that has a query, fragment, user info, or
trailing slash, and an output directory inside the key directory. It
prints the issuer, the active `kid`, and every published `kid`.

### Signing key, `kid`, and rotation

- Algorithm RS256, 3072-bit RSA. Entra and AWS both accept RS256,
  and the engine already signs it.
- The operator generates each key with
  `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072`.
  Rookrunner does not generate keys.
- Keys live in the existing key directory, under a separate
  subdirectory `oidc/`, one file per key: `oidc/<kid>.pem`, mode
  `0600`, directory `0700`, owned by the worker user, opened with the
  same `O_NOFOLLOW` and `fstat` checks as the App key. The key
  directory is never mounted into a job, and the `--docker-socket`
  rule in `secrets.md` (answer 6) applies to the OIDC key the same
  way it applies to `--app-key`.
- `kid` is the RFC 7638 JWK thumbprint (SHA-256, base64url) of the
  public key. It is derived, not chosen, so a JWKS cannot carry two
  keys with one `kid`.
- One file `oidc/active` names the signing `kid`. Every other
  `oidc/*.pem` is published but not used to sign. `oidc/active` is
  mode `0600` like the keys.
- A key file whose name is not its derived `kid` is refused, and the
  refusal names the `kid`, so a new key is renamed once after it is
  generated. At most eight keys are loaded.

Rotation has three steps, each a separate operator action:

1. **Add.** Generate the next key. Run `oidc publish`. The JWKS now
   holds the current and the next key. Upload it.
2. **Switch.** After at least 24 hours, so AWS and Entra caches have
   refetched the JWKS, point `oidc/active` at the next key. New
   tokens carry the new `kid`.
3. **Retire.** After the longest token lifetime plus a margin (one
   hour is enough with the lifetimes below), remove the old key file,
   publish, and upload.

`oidc publish` refuses to write a JWKS that does not contain the
active key. The worker refuses to start when the active key is
missing, unreadable, or below 2048 bits. An emergency rotation after
a key leak skips the 24-hour wait: switch and retire at once, accept
that some cloud calls fail until caches refetch, and record it.

## Per-job tokens

### Claims

Claims mirror GitHub's where the meaning is the same. A claim that
Rookrunner cannot fill truthfully is omitted rather than invented.

| Claim | Value |
| --- | --- |
| `iss` | the configured issuer |
| `aud` | the audience the step requested. One string. Required |
| `sub` | see below |
| `iat`, `nbf` | mint time. `nbf` is `iat` |
| `exp` | `iat` + 300 seconds |
| `jti` | 128 random bits, base64url |
| `repository` | `owner/name` from `--github-repository` |
| `repository_id` | the numeric id the poll stored |
| `repository_owner` | `owner` |
| `ref` | full ref, for example `refs/heads/main` |
| `ref_type` | `branch` or `tag` |
| `sha` | the commit the snapshot was captured from |
| `run_id`, `run_attempt` | Rookrunner run id and attempt, as strings |
| `workflow` | the workflow `name`, or its path when unnamed |
| `workflow_ref` | `owner/name/.github/workflows/FILE@ref` |
| `job_workflow_ref` | the same, or the called local reusable workflow |
| `event_name` | `push`, `pull_request`, `schedule`, or `workflow_dispatch` |
| `actor` | the stored actor login, omitted when none |
| `environment` | only once environments exist. Omitted today |
| `runner_environment` | `self-hosted` |

`sub` follows GitHub's default template so an existing trust policy
needs only its issuer changed:

- push, schedule, and dispatch: `repo:<owner>/<repo>:ref:<ref>`
- same-repository pull request: `repo:<owner>/<repo>:pull_request`
- a job with an environment, once environments are supported:
  `repo:<owner>/<repo>:environment:<name>`

The header is `{"alg":"RS256","typ":"JWT","kid":"<kid>"}`. Claim
construction is a pure function of the stored event, the plan, and
the clock. Nothing from the workflow file chooses `iss`, `sub`, or
`repository`.

### When a job receives the request contract

All of these must hold, checked before the first step:

1. The worker was started with `--oidc-issuer URL`. It is off by
   default.
2. The job's resolved permissions grant `id-token: write`. The
   planner accepts `write` for `id-token` only. Every other `write`
   stays `CAPABILITY_UNSUPPORTED`. `permissions: {}`, `read-all`, and
   an omitted key do not grant it, which matches GitHub.
3. The run is trusted (next section).
4. The worker network is `bridge`. A job that asks on a `none`
   worker fails before the first step with an error that names the
   network, because neither the endpoint nor the cloud is reachable.

When any check fails, the two variables are unset and the job still
runs, as on GitHub. A step that then calls `getIDToken` fails on its
own with GitHub's message.

### How a step requests a token

The worker starts one HTTP listener per job, on an ephemeral port,
bound to the Docker bridge gateway address, not to all interfaces.
The container reaches it through `host.docker.internal`, which the
engine maps with `--add-host host.docker.internal:host-gateway`. The
listener closes when the job ends, on every end path.

Every step of that job receives two reserved names:

- `ACTIONS_ID_TOKEN_REQUEST_URL` =
  `http://host.docker.internal:<port>/token?job=<opaque id>`
- `ACTIONS_ID_TOKEN_REQUEST_TOKEN` = 256 random bits, base64url,
  unique to the job

The URL already contains `?` because `@actions/core` appends
`&audience=<aud>`. A request is
`GET <url>&audience=<aud>` with `Authorization: Bearer <request token>`.
The response is `{"value":"<jwt>"}`, as `@actions/core` expects.

The listener refuses, with 401 or 400 and no token in the body, when
the bearer token is wrong, the job id is unknown or finished, or
`audience` is missing, empty, longer than 256 characters, or not
printable ASCII. It accepts at most 60 requests per job. A step can
ask more than once, for example once for AWS and once for Entra, or
again after five minutes. Redirects are not followed and nothing is
proxied.

As implemented in phase 3:

- The bind address is the `bridge` network's IPv4 gateway from
  `docker network inspect bridge`. On macOS, Docker Desktop forwards
  `host.docker.internal` to the host loopback, so the listener binds
  `127.0.0.1` there.
- The network check runs only after checks 1 to 3 pass, so a job
  that would not receive the contract never fails for its network.
- A job whose run is refused, or whose snapshot is not one clean
  commit (so `sha` cannot be filled truthfully), runs without the
  contract. The first step's stderr then names the reason, and the
  refusal is audited.
- The 61st request answers 429. Every other refusal is 400 or 401
  and is audited with its reason.
- The job log line is `oidc token minted for aud <aud> sub <sub>`,
  prepended to the stderr of the step that asked.

The request token and every minted JWT are registered on the job
mask before they can reach a log line. A step `env` key named
`ACTIONS_ID_TOKEN_REQUEST_URL` or `ACTIONS_ID_TOKEN_REQUEST_TOKEN` is
a refusal that names the key. Neither value is written to the plan,
the run record, the workspace, or a stored env file.

A file-based contract is deferred. `web-identity-token-file` and
`AWS_WEB_IDENTITY_TOKEN_FILE` can be served later by a short `run`
step that calls the endpoint and writes the file inside the
container. A Unix socket was considered and rejected, because
`@actions/core` speaks only HTTP to a URL.

## Security

- **Trusted runs only.** A token is minted only when
  `allowlist_matches` is true for the stored event: by default a
  push, schedule, or dispatch on the default branch, plus the
  operator's `--secret-ref` and `--secret-pusher` entries. A
  same-repository pull request receives a token only when the worker
  also sets `--oidc-pull-request`. Its `sub` is
  `repo:<owner>/<repo>:pull_request`, so a cloud policy that pins a
  branch never accepts it.
- **Never forks.** A pull request whose head repository id differs
  from the base id, or whose head repository is null, never receives
  the contract. That holds even when the run exists because of
  `--allow-untrusted` or `--allow-pr-workflow`. A run with either
  override, and a local submit without the stored event fields, is
  refused by the mint itself, not only by the env builder.
- **The mint decision, as implemented in phase 2.** `mint_refusal`
  returns a reason, or none when the mint may go ahead. It refuses
  a run with `untrusted_override`; an event other than push,
  pull_request, schedule, or workflow_dispatch; an event whose
  `repository.full_name` is not the worker repository; an event with
  no `ref`; and, except for workflow_dispatch (whose stored event
  carries no id), an event with no integer `repository.id`. A pull
  request is refused when its head repository is missing or null, or
  when the head id, the base id, and the event repository id are not
  all equal. A same-repository pull request then needs
  `--oidc-pull-request` and does not consult the allowlist. Every
  other event needs `allowlist_matches`. A pull request admitted by
  `--allow-pr-workflow` records `untrusted_override` on its run, the
  same as `--allow-untrusted`, so the mint refuses it.
- **Short lifetime.** `exp` is five minutes after mint. The request
  token dies with the job. A leaked JWT is good for one audience
  until `exp`.
- **Key storage.** The private key stays in the key directory, is
  read once at worker start, and is never mounted, logged, or passed
  to a step. The job process only ever holds signed JWTs. A worker
  with `--docker-socket` can reach the host and therefore the key;
  that combination follows answer 6 in `secrets.md`.
- **Audit log.** Each mint and each refusal appends one JSON line to
  `oidc-audit.jsonl` in the state directory, mode `0600`: time,
  run id, attempt, job id, step index, `aud`, `sub`, `jti`, `kid`,
  `exp`, and the refusal reason. It never stores the JWT or the
  request token. The job log shows one line per mint with `aud` and
  `sub`. Cloud-side logs (CloudTrail `AssumeRoleWithWebIdentity`,
  Entra sign-in logs for the service principal) carry the same
  `jti` or `sub` for correlation.
- **Cloud policy is the last gate.** The design does not replace a
  tight trust policy. Every example below pins `aud` and an exact
  `sub`.
- **Accepted risk: hand-rolled RS256.** Signing builds the PKCS#1 v1.5
  padding in Python and exponentiates with `pow`, which is not
  constant-time. This is accepted, not migrated, for three reasons.
  The worker keeps zero third-party runtime dependencies, and taking a
  vetted implementation would change packaging and the dependency
  audit for this one call site. Signing is local-only: the key is read
  once at worker start behind a `0700` directory, and no remote party
  can time the signing operation. Re-evaluate if tokens are ever minted
  across a boundary where signing timing is observable, or when
  packaging can take a vetted constant-time implementation without
  widening the runtime dependency set.

## Cloud setup examples

Generic examples. `<issuer-host>` is `<issuer>` without `https://`.

### AWS IAM

Create the provider once per account:

```sh
aws iam create-open-id-connect-provider \
  --url https://<issuer-host> \
  --client-id-list sts.amazonaws.com
```

AWS validates the issuer certificate against its trusted CA library,
so no thumbprint is needed for a public CA certificate.

Role trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<account-id>:oidc-provider/<issuer-host>"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "<issuer-host>:aud": "sts.amazonaws.com",
          "<issuer-host>:sub": "repo:<owner>/<repo>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

Workflow step, unchanged from GitHub:

```yaml
permissions:
  id-token: write
  contents: read
steps:
  - uses: aws-actions/configure-aws-credentials@<sha>
    with:
      role-to-assume: arn:aws:iam::<account-id>:role/<role>
      aws-region: us-east-1
```

### Microsoft Entra ID

Add a federated identity credential to the app registration (or a
user-assigned managed identity):

```sh
az ad app federated-credential create --id <app-object-id> --parameters '{
  "name": "rookrunner-main",
  "issuer": "https://<issuer-host>",
  "subject": "repo:<owner>/<repo>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

Entra matches `issuer`, `subject`, and `audiences` exactly. One
credential per subject; an app allows at most 20.

`azure/login` requests a token for `api://AzureADTokenExchange` and
runs `az login --federated-token`, so the runner image needs the
Azure CLI. A job without the CLI can do the exchange directly:

```sh
curl -s -X POST \
  "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" \
  -d client_id=<client-id> \
  -d grant_type=client_credentials \
  -d scope=https://management.azure.com/.default \
  -d client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer \
  --data-urlencode client_assertion="$JWT"
```

## Phased plan

Each phase is one pull request and merges before the next opens.
Phases 1 and 2 add no runtime behavior. Phase 3 is behind
`--oidc-issuer`, off by default.

1. **Keys and publish.** `oidc/` key loading with the file checks
   above, JWK and RFC 7638 `kid` derivation, the `active` pointer,
   and `rookrunner oidc publish`. Tests: the `kid` of a fixture key
   matches the RFC 7638 value computed independently; a JWKS during
   rotation holds both keys and signing uses only the active one;
   publish refuses a JWKS without the active key; a retired key is
   gone from the next JWKS; a symlinked or `0644` key is refused.
2. **Claims and trust gate.** Pure functions for claim construction
   and the mint decision. Tests: push, tag, schedule, dispatch, and
   same-repository pull request produce the exact `sub` strings
   above; a fork, a null head repository, an override run, and a
   local submit are refused; `aud` is required and validated; `exp`
   is `iat` + 300; a JWT signed with a fixture key verifies against
   the published JWKS.
3. **Endpoint and contract.** Planner accepts `id-token: write` only.
   Worker flags `--oidc-issuer` and `--oidc-pull-request`. The
   per-job listener, the two reserved names, masking, and the audit
   log. Tests: an action that calls `getIDToken` gets a token whose
   claims match; a wrong bearer token, a finished job, and a missing
   audience are refused; neither value appears in the plan, run
   record, or stored logs; a `none` network refuses before the first
   step.
4. **Hosting and live check.** Publish the two files at the chosen
   domain. Run one sandbox AWS role and one sandbox Entra app
   against a disposable private repository. Record the evidence in
   `docs/validation/`.
5. **Later.** Environments and the `environment` subject, a
   file-based token for tools that read `AWS_WEB_IDENTITY_TOKEN_FILE`,
   and a custom `sub` template.

## Decisions

These were decided on 2026-10-17.

1. **Issuer host and domain.** `https://oidc.rookrunner.com`, served
   from Cloudflare Workers static assets alongside the Rookrunner
   website.
2. **Capability version.** Phase 3 may raise the capability version
   to 13 in a signed-off pull request.
3. **Same-repository pull requests.** Off unless the worker sets
   `--oidc-pull-request`. Forks never receive a token.
4. **Trusted-ref policy.** Reuse the file-secret allowlist
   (`--secret-ref` and `--secret-pusher`).
5. **Token lifetime.** Five minutes.

## Open decisions

6. **Azure CLI in the runner image** for `azure/login`, or document
   the direct token exchange only.
7. **Network.** Jobs that use OIDC need `--network bridge`. Accept
   that, or design a narrower egress for those jobs.
