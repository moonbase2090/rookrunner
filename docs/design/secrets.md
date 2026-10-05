# Secrets, GITHUB_TOKEN, and write permissions

Status: designed. This slice writes this document and does not change
the engine. The capability version stays 12. A version 11 plan is not
migrated. No plan field and no capability entry are added. The plan
schema is unchanged. `.github/workflows/check.yml` is unchanged.
`write` and `write-all` stay rejected. `GITHUB_TOKEN` and
`github.token` stay unset. The `secrets` context stays withheld. No
job token is minted. This is not a GitHub-equivalence claim.

The owner questions in this document are open. Each one names one
recommended option. The recommendation is a proposal for MB2090. This
document does not accept it. Deploy workflows and macOS jobs stay
deferred until an implementation that follows the owner's answers.

The Rookrunner GitHub App exists. Its name is Rookrunner-App. Its
App ID is 5201333. It is not the moonbase2090-agents App. The
installation and the private key are still pending. This document
does not install the App, does not start a sign-in, and does not
read a key. It contains no client id, installation id, or key
material. Permissions on the App stay Checks write, Commit statuses
write, and Contents read. GitHub sets Metadata to read on every App.
That is not an extra permission.

https://docs.github.com/en/actions/concepts/security/github_token
https://docs.github.com/en/actions/reference/security/secrets
https://docs.github.com/en/actions/reference/security/secure-use
https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types

## What the engine does today

These are implemented facts. This design leaves them in place.

The planner records `read`, `none`, and `read-all`. It rejects
`write`, `write-all`, and an unknown scope. No token is created from
a recorded value. `id-token` is `write|none` on GitHub's permissions
syntax, and `vulnerability-alerts` is `read|none`. This engine
accepts `read` or `none` for every scope it recognizes and rejects
`write`.

https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions

The expression evaluator withholds `secrets` from step `run`, step
`env`, step `with`, step `name`, job `name`, job `env`, workflow
`env`, and concurrency. GitHub's contexts table lists `secrets` in
several of those places. An output expression that reads `secrets`
is omitted.
Reusable-workflow `secrets` and `secrets: inherit` stay rejected.
The snapshot excludes `.secrets`. Filename exclusions are not a
secret system.

`github.token` and `GITHUB_TOKEN` stay unset. The check-run post
mints a separate installation token when `--app-key` is set. That
token requests Checks write and Commit statuses write only. It is
discarded before the post command returns. It is not exported to the
job and it is not `GITHUB_TOKEN`. The key path rules in
[check runs](check-runs.md) stay. The key directory is not a job
mount.

Stdout `add-mask` registers a value for later log text in the same
job. The mask replaces that value, and each of its
whitespace-separated words, with `***`. An empty or whitespace-only
value is not a mask. The mask list is not copied into the next job.
An output that contains a masked value is still stored. stderr is
masked with the masks registered while that step's stdout was read.

## Threat model

These boundaries are already accepted. This design does not reopen
them.

Only a repository the operator configured runs. That repository is
trusted code. A pull request whose head repository full name differs
from the configured repository is a fork. The poll records it and
runs nothing. `pull_request_target` is not evaluated. A local Docker
run is not a sandbox for hostile code. GitHub's secure-use guidance
says a self-hosted runner should almost never run a pull request
from a public fork. Forks and other untrusted code stay excluded.
This design adds no path that runs them.

https://docs.github.com/en/actions/reference/security/secure-use

All-command `NOPASSWD` on the P6 image, together with its Docker
client and `--docker-socket`, is control of the host engine
([runner image](runner-image.md)). The step uid is the caller uid.
The container is not privileged. A secret value or a `GITHUB_TOKEN`
that reaches a step on that worker can be copied to the host. The
default container network is `bridge`, so a step can also send a
value to the network. Masking a log does not stop either copy. This
design does not remove that warning and does not mount the App key
directory into the job.

On GitHub, events caused by `GITHUB_TOKEN` do not start a new
workflow run, with the exceptions GitHub lists. This engine's poll
does not apply that rule. A push to a configured owner repository is
trusted code on the next poll. That is one reason the recommended
job token does not receive a write permission.

https://docs.github.com/en/actions/concepts/security/github_token

## Where secret values live

Open question 1. The recommended store is an operator directory
passed by a flag. The flag names the directory. The directory is
outside the repository, the state directory, and every attempt
workspace. It is a different directory from
`~/Secrets/github-app/rookrunner-app/`, so the key reader and the secret
reader are not the same directory. The implementation resolves `~`
for the user running the process and refuses any other location,
the same way `--app-key` refuses a key path outside the key
directory.

One file per secret. The file name is the secret name. The directory
is mode `0700`. Each file is mode `0600`. None of them is a symlink.
The owner is the user running the process. A missing directory, a
symlink, a mode that is group- or world-readable, or a path that
resolves outside that directory is a refusal before a step runs.
The error does not include the path or the file bytes.

A secret name contains only ASCII letters, digits, and underscores,
does not start with a digit, and does not start with `GITHUB_`.
GitHub stores names as uppercase. The recommended reader accepts
only an uppercase name, so the file name and the workflow reference
are the same string. A value larger than 48 KB is refused. An empty
value is refused. Those limits match the secrets reference. A value
is not JSON, XML, or YAML wrapped around another secret. GitHub says
structured data makes redaction fail. This design does not parse
the value.

The store is not the GitHub secrets API. The App has no Secrets
permission, and this design does not ask for one. The store is not
the worker process environment. Values are read when the job is
about to start, for names that job's steps reference, and are not
copied into the snapshot, the plan, or the run record.

https://docs.github.com/en/actions/reference/security/secrets

## How a value reaches a step

Open question 2. The recommended delivery is the step that
references the name, and no other step.

A step whose `env` or `with` text references `secrets.NAME` receives
that value in the environment variable or the action input. The
stored plan keeps the expression text. It does not keep the value.
The value is not written into the workspace, `GITHUB_ENV`, or a file
the artifact manifest scans. A step that does not reference the name
does not receive it.

A `run` script is different. GitHub splices the secret into the
shell script before the shell starts. The recommendation does not.
The script text that is stored and executed has `$NAME` in place of
`${{ secrets.NAME }}`, and `NAME` is set in that step's environment.
The replacement is that expression only. The rest of the script is
unchanged. The rewrite applies when the expression is exactly
`secrets.NAME` or `github.token`. Any other expression that reads
`secrets` or `github.token`, and any of those expressions inside
single quotes, is a refusal and names the field. The script is not
rewritten and the step does not run. This is an intentional
difference from GitHub if the owner chooses it. The value is not
written into the script file.

`github.token` in `env`, `with`, and `run`, and the environment
variable `GITHUB_TOKEN`, receive the job token when that job is
allowed to have one. A `run` script gets `$GITHUB_TOKEN` in place of
`${{ github.token }}` by the same rule. Both stay unset when the job
is not allowed to have a token.

`secrets` stays withheld from step `if`, job `if`, job outputs,
concurrency, job `env`, workflow `env`, and step and job `name`.
GitHub lists `secrets` in more of those places. The recommendation
keeps the narrower set so a field this engine stores cannot receive
a secret value. That difference is intentional if the owner chooses
the recommendation.

## Masking in logs and run records

Open question 3. The recommended mask uses the existing job mask
list.

Before the first step of a job that received a secret or a job
token, the engine registers each injected value and the job token.
`mask_text` then replaces the value, and each whitespace-separated
word of it, with `***` in that job's stdout, stderr, stored log
pages, and error text. The replacement happens before the text is
written. An empty value is not registered. The mask list stays in
memory for that job. It is not stored in the run record, and the
next job does not inherit it.

Registering the four characters `ghp_` on the existing mask list
would replace only those four characters. The rest of a printed
token would remain. The recommendation adds one rule beyond
`mask_text`: a run of ASCII letters and digits after `ghp_`,
`gho_`, `ghu_`, `ghs_`, or `ghr_` is masked together with that
prefix. The secrets reference lists those prefixes among values
GitHub redacts. The same rule masks a token that is not one of the
injected values. It also masks unrelated text that uses the prefix.

A transformation bypasses this mask. Base64, a split across
commands, or any other change of the bytes will not match. GitHub
says redaction is not guaranteed for the same reason. A step that
prints a transformed secret has exposed it. The operator rotates
that secret. This design does not scan artifact file bytes.
`artifact.read` can return a workspace file that contains a value.
Existing `add-mask` behavior stores a job output that contains a
masked value. The recommendation is stricter for secrets and the
job token: an output whose text contains one of those values is
omitted, and the omission is recorded without the value.

https://docs.github.com/en/actions/reference/security/secrets
https://docs.github.com/en/actions/reference/security/secure-use

## Token minting, scope, and lifetime

Open questions 4 and 5. The job token is not the check-run post
token.

The recommended mint is one installation access token per job, at
job start, for Rookrunner-App, limited to the repository that owns
the workflow. The requested permissions are the job's resolved
permissions intersected with the App registration, and then cut to
the set question 5 allows. The token is held in memory. It is not
written to disk, the state directory, the workspace, or a log. It
is discarded when the job container exits, including on setup
failure and on cancel. It is not cached across jobs. Redirects are
refused, as they are for the check-run post, so the token is not
sent to another host.

An installation access token expires after one hour. Its prefix is
`ghs_`. GitHub creates `GITHUB_TOKEN` at job start and expires it
when the job finishes or at the effective maximum. On a
GitHub-hosted runner that maximum is 6 hours. On a self-hosted
runner GitHub refreshes the token for up to 24 hours, while the job
itself may run for 5 days. The recommendation does not refresh. A
Rookrunner job that runs longer than one hour keeps an expired
token. The alternative in question 4 is a worker-side refresh for
up to 24 hours, still discarded when the job exits.

The App registration grants Checks write, Commit statuses write,
and Contents read. The post token keeps Checks write and Commit
statuses write and is minted only for that post. The recommended
job token does not receive those two write scopes. It may request
Contents read only. `write` in a workflow stays
`CAPABILITY_UNSUPPORTED` until the owner changes the App and
accepts an implementation. This design adds no App permission. If
the owner later authorizes one write, the recommendation is
Contents write only.

An omitted `permissions` key records nothing today and mints no
token. The recommendation, once implemented, treats omission as
Contents read and mints the job token with that scope when the
other gates in this document pass. `read-all` stays a read. The
recommendation maps it to Contents read, not to every scope the App
has, and not to write.

https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
https://docs.github.com/en/actions/concepts/security/github_token
https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types

## What stays rejected

These stay out of this design. They are not open questions.

- A fork pull request, any other untrusted code, and
  `pull_request_target`.
- Mounting `~/Secrets/github-app/rookrunner-app/` into the job, and
  putting the post token or the private key in the job.
- A new App permission, a Secrets API permission, and any use of
  the GitHub secrets API.
- `write-all`, `id-token` as a minted OIDC token, `security-events:
  write`, `actions: write`, packages write, and pull-requests write.
- Organization secrets, environment secrets, Dependabot secrets,
  environments, and approvals.
- Reusable-workflow `secrets` and `secrets: inherit`.
- A private action fetch that uses the job token.
- `actions/download-artifact`, `actions/setup-node`, and macOS jobs.
- A webhook receiver, registration as a GitHub runner, and a remote
  worker.
- `GITHUB_TOKEN` in `.github/workflows/check.yml`.
- A capability-version bump and a plan-schema change.
- Scanning artifact bytes for a secret value.
- Creating the secret directory, minting a token, installing the
  App, starting a sign-in, or reading a key. The App ID is 5201333.
  The installation and the private key are still pending, so an
  implementation cannot
  mint against the live App until the operator finishes those steps.

## Open questions for MB2090

Each question is open. The recommended option is a proposal. This
document does not accept it.

1. **Where secret values live.** Recommend an operator directory
   passed by flag, outside the repository, the state directory, and
   attempt workspaces, and separate from
   `~/Secrets/github-app/rookrunner-app/`. One file per secret. Mode
   `0700` on the directory and `0600` on each file. No symlinks.
   Owned by the caller. Names are uppercase letters, digits, and
   underscores, and do not start with a digit or `GITHUB_`. A value
   over 48 KB is refused. The alternative is to keep secrets
   disabled and leave `GITHUB_TOKEN` unset.
2. **Which fields receive a value.** Recommend step `env`, step
   `with`, and step `run`. `env` and `with` receive the value
   directly. A `run` script is not given the spliced secret.
   An expression that is exactly `secrets.NAME` becomes `$NAME`,
   with `NAME` set in the step environment. `github.token` becomes
   `$GITHUB_TOKEN` by the same rule, and `GITHUB_TOKEN` is set
   when the job has a token. Any other expression that reads
   either context, including one in single quotes, is a refusal.
   Keep `secrets` withheld from `if`,
   job outputs, concurrency, job `env`, workflow `env`, and names.
   The alternative is to splice the value into the `run` script,
   which is what GitHub does, and to evaluate `secrets` in the
   fields this engine stores.
3. **Masking.** Recommend registering each injected value and the
   job token on the existing job mask list before the first step.
   Also mask a whole token that starts with `ghp_`, `gho_`,
   `ghu_`, `ghs_`, or `ghr_` and continues with ASCII letters and
   digits. Stored log pages and error text then contain `***`.
   Omit a job output that contains one of those values. The
   alternative is to mask only the full injected values with
   today's `mask_text`, and to keep storing an output that
   contains a masked value.
4. **Token lifetime.** Recommend one installation access token per
   job, minted at job start, held in memory, and discarded when the
   job container exits. Do not refresh it. A job longer than one
   hour keeps an expired token. The alternative is to refresh the
   token for up to 24 hours, which is GitHub's self-hosted
   `GITHUB_TOKEN` bound, and still discard it when the job exits.
5. **What the job token may request.** Recommend no change to the
   App registration. The job token may request Contents read only.
   Checks write and Commit statuses write stay on the post token.
   `write` stays rejected. If one write is later authorized,
   recommend Contents write only. An omitted `permissions` key
   becomes Contents read when this is implemented. `read-all`
   becomes Contents read. The alternative is to let the job token
   request any scope the App already grants, including Checks write
   and Commit statuses write, and to keep an omitted permissions
   key as no token.
6. **A worker started with `--docker-socket`.** Recommend that this
   worker mint no job token and inject no secret. When a workflow
   would receive either, refuse and name the socket. The host-control
   warning stays. The alternative is to inject the values and keep
   only that warning.
7. **Same-repository pull requests.** Forks stay excluded. Recommend
   that a pull request whose head repository is the configured
   repository receive the same secrets and the same job token as a
   push. The alternative is to withhold both from every pull
   request, including a same-repository pull request.

## What an implementation would prove

This design proves none of these. An implementation that follows the
owner's answers would prove them. Tests would point `HOME` at a
temporary directory and use a fixture key and a local HTTP stub.
No test would read `~/Secrets/github-app/rookrunner-app/` and no test
would contact `api.github.com`.

1. A workflow that references no secret and needs no token runs as
   it does today. `security-events: write` and `actions: write`
   still fail planning.
2. A secret value is absent from the plan, the run record, the
   state directory, and stored log pages. The log shows `***`.
3. The value is in the environment of a step that references it in
   `env` or `with`, and in the environment when a `run` script
   references it. The stored script contains `$NAME` and does not
   contain the value. A step that does not reference the name does
   not receive it.
4. The job token is discarded when the container exits. It is not
   the check-run post token. The key path rules in
   [check runs](check-runs.md) still hold.
5. The owner's answer on `--docker-socket` holds: either the run
   refuses and names the socket, or the warning is the only control.
6. A fork pull request still creates no run and reads no secret.
7. `.github/workflows/check.yml` does not contain a secret, a token,
   or `--app-key`. The capability version stays 12.

## What this design does not do

No engine change. No secret directory is created. No token is
minted. No sign-in is started. No file under
`~/Secrets/github-app/rookrunner-app/` is read. The App is not
installed by this document. Its App ID is 5201333. No App
permission is added. The installation and the private key remain
pending. `write` stays rejected. `GITHUB_TOKEN` stays unset.
