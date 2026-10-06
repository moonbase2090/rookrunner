# Check runs

Status: implemented. `--app-key` posts one check run and then the
commit status with the installation token. Omitting `--app-key` keeps
the NS-40 token file. The capability version stays 12. A version 11
plan is not migrated. No plan field and no capability entry are added.
The plan schema is unchanged. `run.status` accepts optional check
fields because that schema rejects unknown properties. Those fields
are not on `run.get`. `.github/workflows/check.yml` is unchanged. This
is not a GitHub-equivalence claim. P7 designs secrets,
`GITHUB_TOKEN`, and `write` permissions and does not change this
post ([secrets](secrets.md)). MB2090 accepted the amended answers
on 2026-10-05.

The owner chose the credential on 2026-10-05. It is a new GitHub App.
Its name is Rookrunner-App. It is not the moonbase2090-agents App.
The App exists. Its App ID is 5201333. The operator names the
installation as `168290590`. The mint reads `installation-id` and
the code does not compare the file to that number. The private key
stays out of the repository. This document does not install the
App, does not start a sign-in, and does not read a key. It contains
no client id or key material.

## Credential

Repository permissions on the App:

| Permission | Access |
| --- | --- |
| Checks | Write |
| Commit statuses | Write |
| Contents | Read |

GitHub sets Metadata to read on every App. That is not an extra
permission. No other permission is requested. Contents read is not
used to fetch a repository and is not placed on the installation
token minted for a post. List requests stay unauthenticated, as
NS-42 implemented them.

The private key lives only in `~/Secrets/github-app/rookrunner-app/` on
the operator's Mac. The operator passes the key path. The
implementation resolves `~` for the user running the process and
refuses any other location. It does not create the directory.

The directory holds three files:

| File | Contents |
| --- | --- |
| `private-key.pem` | The App's private key, PEM, as GitHub generated it |
| `client-id` | One line, the App's client ID. This is the JWT `iss` claim |
| `installation-id` | One line, the decimal installation id |

The directory is mode `0700`. Each file is mode `0600`. None of them
is a symlink. The owner is the user running the process. The flag
names the key file. The other two files are read from that file's
parent directory. A missing file, a symlink, a mode that is group- or
world-readable, or a path that resolves outside that directory is a
refusal. The refusal happens before any HTTP request. The error does
not include the path, the key, or file bytes.

The private key stays out of the repository. The key, the client
id, and the `installation-id` file never enter this repository,
`.github/workflows/check.yml`, a job container, a log, or a run
record. The operator names the installation as `168290590`. The
mint reads `installation-id` and the code does not compare the file
to that number. Tests point `HOME` at a temporary directory and generate
a fixture key there. No test reads `~/Secrets/github-app/rookrunner-app/`
and no test contacts `api.github.com`.

https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps

## When a post is attempted

The gate is the NS-40 gate. The command refuses, with no key read and
no HTTP request, unless all of these hold:

- The run is a workflow run.
- Its snapshot is clean, with no included files.
- `base_commit` equals the tested commit.

A fork pull request still runs nothing, and nothing is posted for it.
The status SHA is unchanged: the push tip, or `pull_request.head.sha`
for a pull request. The tested commit for a pull request remains the
merge commit.

`--app-key` is the key path. When it is omitted, no check run is
created and the commit status still uses the NS-40 token file. When
it is set, that post does not also read the token file. Passing both
refuses before HTTP, so one post has one credential.

If the same terminal conclusion is already recorded for that run,
the command sends nothing and does not read the key.

## Token

The token is minted at post time and is not cached.

1. Read the key, the client id, and the installation id.
2. Sign one JWT in memory. `alg` is `RS256`. `iat` is 60 seconds in
   the past. `exp` is 10 minutes after `iat`, which is inside GitHub's
   10-minute maximum. `iss` is the client id. No other claim is set.
3. `POST /app/installations/{installation_id}/access_tokens` with
   `Authorization: Bearer` and that JWT. The body requests
   `checks: write` and `statuses: write` only.
4. Discard the JWT. Hold the installation token in memory for the
   check-run request and the commit-status request, then discard it
   before the command returns, including on failure.

The token is not written to disk, the state directory, the environment
of the job, or `GITHUB_TOKEN`. Redirects are refused, as they are for
a commit status, so the token is not sent to another host. The API
base stays configurable and defaults to `https://api.github.com`.
Headers match the commit-status POST, including
`X-GitHub-Api-Version: 2022-11-28`.

https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app

## Check run

`POST /repos/{owner}/{repo}/check-runs` creates the run. A later post
for the same local run uses `PATCH` on the stored check-run id. Only
a GitHub App can create or update a check run.

The name is the NS-40 context: `rookrunner/` plus the workflow file
name plus `/` plus the job id. `head_sha` is the status SHA.
`external_id` is the run id. `output.title` is the name.
`output.summary` is the run state and, when the run has one, the exit
code. There is no `output.text`, no annotations, no images, no
actions, and no `details_url`. Step logs are not uploaded.

| Run | Check |
| --- | --- |
| `queued` | `status` `queued`. No conclusion |
| `running` | `status` `in_progress`. No conclusion. `started_at` is the post time |
| `succeeded` with exit code 0 | `status` `completed`, `conclusion` `success` |
| `failed` | `status` `completed`, `conclusion` `failure` |
| `cancelled` | `status` `completed`, `conclusion` `cancelled` |
| `lost`, or any other state | `status` `completed`, `conclusion` `failure` |

Nothing else is `success`. The Checks API has no `error` conclusion.
`neutral` is not used, because it is not a failure. `waiting`,
`pending`, and `requested` are not sent. GitHub reserves those for
GitHub Actions.

A completed check sends `completed_at` as the post time. One create
and one later update stay under GitHub's limit of 1,000 check runs
with the same name. A rate-limit response is not retried in the same
call.

After the check request, the same token posts the NS-40 commit status.
The state mapping for that status is unchanged. If the check request
fails, the status request is still attempted. Each result is recorded
on its own. Neither failure changes the local run state.

https://docs.github.com/en/rest/checks/runs
https://docs.github.com/en/rest/commits/statuses
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api

## Failure modes

| Condition | Result |
| --- | --- |
| The NS-40 gate fails, or the same terminal conclusion is already recorded | No key read. No HTTP |
| `--app-key` and `--credential-file` are both set | Refusal. No HTTP |
| The key path is outside `~/Secrets/github-app/rookrunner-app/`, is not absolute, is a symlink, or is inside the repository, the state directory, or an attempt workspace | Refusal. No HTTP. The error names none of the bytes |
| The directory or a file is missing, not mode `0700` or `0600`, or not owned by the caller | Refusal. No HTTP |
| The client id or installation id is empty or not a single token | Refusal. No HTTP |
| The PEM cannot be loaded | Refusal. No HTTP |
| Token exchange returns 401, 403, or 404 | No check and no status. Not retried. The local run state is unchanged |
| Token exchange or either post returns 429, or 403 with a rate-limit header | Retryable error. Not retried in this call. The token is discarded |
| Any other HTTP or network failure | The request that failed is not success. Not retried. The other request still proceeds only when the token was minted and the failure was the check request |
| A redirect | Refused. The token is not forwarded |

Response bodies are not logged and are not stored. An error names the
class and, for HTTP, the status code. It does not include the key, the
JWT, the installation token, or the `Authorization` header.

## What stays out of plans and records

No plan field and no capability entry are added. The plan schema is
unchanged. `run.status` gains optional check fields because that
schema rejects unknown properties. The worker stores the integer
check-run id and the mapped status and conclusion in `check_posts`,
the same way NS-40 stores a posted commit-status state in
`status_posts`. `run.get` does not gain those fields. A plan does not
gain a credential, a key path, a client id, or an installation id.

That stored row does not include the private key, the PEM text, the
JWT, the installation token, the client id, the installation id, the
key path, or a response body.

`GITHUB_TOKEN` stays unset. The installation token is not exported to
the job. `security-events: write` and `actions: write` stay rejected.
A `write` permissions value stays rejected. Job secrets stay disabled.
The secrets design does not change this post
([secrets](secrets.md)). The key directory is not a job mount.

## Host control

All-command `NOPASSWD` on the P6 image, together with its Docker
client and `--docker-socket`, is control of the host engine
([runner image](runner-image.md)). This post does not mount the key
directory into the job, and it does not put the installation token in
the job. A job on a worker started with `--docker-socket` can still
ask that engine to mount the host directory that holds the key. P5
does not remove that warning.

## What the implementation proves

1. A gate failure or a repeated terminal conclusion reads no key and
   sends no HTTP.
2. Against a local stub, one post exchanges a JWT for an installation
   token, creates or updates one check run, then posts the commit
   status. The recorded id is the stub's id.
3. The key, the JWT, and the token are absent from the run record, the
   state directory, and the error text.
4. A key path outside the operator directory, a symlink, or a path
   inside the repository or the state directory sends nothing.
5. A rate-limit response is retryable and is not retried. HTTP 401 or
   403 on the token exchange sends no check and no status.
6. `.github/workflows/check.yml` does not contain the key path or
   `--app-key`. The capability version stays 12.
7. The job environment still has no `GITHUB_TOKEN`. `security-events:
   write` and `actions: write` still fail planning.

## What this implementation does not do

The App exists. Its name is Rookrunner-App and its App ID is
5201333. The operator names the installation as `168290590`. The
mint reads `installation-id` and the code does not compare the file
to that number. The private key stays out of the repository.
No sign-in is started. No file under
`~/Secrets/github-app/rookrunner-app/` is read by a test or by this
repository. Omitting `--app-key` still posts a commit status from
the NS-40 token file. Poll list requests stay unauthenticated.
Contents read does not become a fetch. There is no webhook, no
runner registration, and no `GITHUB_TOKEN` in the job. The secrets
design does not change this post ([secrets](secrets.md)).
