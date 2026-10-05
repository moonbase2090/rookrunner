# Secrets, GITHUB_TOKEN, and write permissions

Status: designed. p7-mask and p7-trust-gate are implemented. The
other four pull requests in the plan are not. MB2090 accepted the
amended answers for all seven questions on 2026-10-05. The source of
those answers is the review comment on pull request 63:

https://github.com/moonbase2090/rookrunner/pull/63#issuecomment-6001650970

This document records the accepted answers and the implementation
plan. p7-mask changes the job mask. p7-trust-gate compares
pull-request repositories by numeric id and stores the allowlist
fields on the event. The capability version stays 12. A
version 11 plan is not migrated. No plan field and no capability
entry are added. The plan schema is unchanged.
`.github/workflows/check.yml` is unchanged. `write` and `write-all`
stay rejected. `GITHUB_TOKEN` and `github.token` stay unset. The
`secrets` context stays withheld. No job token is minted. This is
not a GitHub-equivalence claim.

The seven answers below are accepted direction. Masking from answer
3 is implemented. The fork comparison and the allowlist match from
answer 7 are implemented. The secret files, the worker flags, and
the job token are not. Deploy workflows and
macOS jobs stay deferred until the implementation plan below has
landed.

The Rookrunner GitHub App exists. Its name is Rookrunner-App. Its
App ID is 5201333. It is not the moonbase2090-agents App. The
installation and the private key are still pending. This document
does not install the App, does not start a sign-in, and does not
read a key. It contains no client id, installation id, or key
material. Permissions on the App stay Checks write, Commit statuses
write, and Contents read. GitHub sets Metadata to read on every App.
That is not an extra permission.

The key directory is `~/Secrets/github-app/rookrunner-app/`. Pull
request 64 recorded that path in the code and the docs. The secret
root below is a different directory. This document does not move
the key.

https://docs.github.com/en/actions/concepts/security/github_token
https://docs.github.com/en/actions/reference/security/secrets
https://docs.github.com/en/actions/reference/security/secure-use
https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types

## What the engine does today

These are implemented facts. This design leaves them in place until
the pull requests in the implementation plan change them.

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
is omitted. Reusable-workflow `secrets` and `secrets: inherit` stay
rejected. The snapshot excludes `.secrets`. Filename exclusions are
not a secret system.

`github.token` and `GITHUB_TOKEN` stay unset. The check-run post
mints a separate installation token when `--app-key` is set. That
token requests Checks write and Commit statuses write only. It is
discarded before the post command returns. It is not exported to the
job and it is not `GITHUB_TOKEN`. The key path rules in
[check runs](check-runs.md) stay. The key directory is not a job
mount.

Stdout `add-mask` registers a value for later log text in the same
job. The mask replaces that value, each of its whitespace-separated
words, and the base64, JSON, and percent-encoded forms of the value,
with `***`. A form shorter than 4 characters is not registered.
`ghp_`, `gho_`, `ghu_`, `ghs_`, and `ghr_` followed by ASCII letters
or digits, and `github_pat_` followed by ASCII letters, digits, or
underscores, are masked together with that prefix. A partial line is
held until the next read or the end of the stream. A value split
across two lines is not joined. Annotation text is masked. A stored
step summary and a stored copy of an env, output, or state file are
masked. The live files the next step reads are not. A step output
keeps the raw value. A job output that contains an injected secret
or one of those prefixes is omitted, and the omission is recorded
without the value. An `add-mask` value in a job output is stored as
masked text. A registered value or word shorter than 4 characters
logs a warning that does not include the value. The check-run post
applies the prefix rules to its request body. An empty or
whitespace-only value is not a mask. The mask list is not copied
into the next job. stderr is masked with the masks registered while
that step's stdout was read. This does not open the secret directory
and does not mint a token.

The poll compares a pull request's head and base repository ids.
The ids are integers and match when they are equal. A bool is not
an id. A missing head repository, including a null `head.repo`, is
a fork. A fork is recorded and runs nothing. A same-repository
pull request still runs. The comparison does not use the full name.

The poll copies `repository.id` and `repository.default_branch`
from the repository object into the event it already stores. The
event's `repository.full_name` stays the configured repository.
For a push, a non-empty commit `author.login` is stored as the tip
commit's `author.login`. For a pull request, a non-empty
`user.login` is stored on the pull request. `allowlist_matches` is
a pure function of that event, the ref list, the pusher list, and
the event name. With both lists empty, the only match is a push
whose ref is `refs/heads/` plus `repository.default_branch`. A
listed ref matches exactly. A listed login matches with ASCII case
folding. The default push stays a match when a list is non-empty.
A missing login does not match a pusher entry. A local submit that
does not carry those fields does not match the default rule. No
secret is read and no token is minted. `worker --secrets`,
`worker --secret-ref`, and `worker --secret-pusher` are not added
yet.

## Threat model

These boundaries are already accepted. This design does not reopen
them.

Only a repository the operator configured runs. That repository is
trusted code. `pull_request_target` is not evaluated. A local Docker
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
value to the network. Masking a log does not stop either copy.

A socket worker can also mount the host. Docker Desktop shares
`/Users` into its VM by default, so a step can
`docker run -v ~/Secrets:/x`. That mount reaches the App private key
and every file in the secret root, including files the workflow
never names. Withholding injection does not close that mount.
Answer 6 is the accepted control. The host-control warning stays,
and it names the key directory and the secret root.

On GitHub, events caused by `GITHUB_TOKEN` do not start a new
workflow run, with the exceptions GitHub lists. This engine's poll
does not apply that rule. A push to a configured owner repository is
trusted code on the next poll. The accepted job token therefore
receives no write permission. If a later change authorizes Contents
write on the job token, the poll must skip commits pushed by the
Rookrunner-App bot identity before that write can be used. This
design records that requirement. It does not add the write, and it
does not add the skip.

https://docs.github.com/en/actions/concepts/security/github_token

## Where secret values live

Accepted answer 1. The store is one fixed root, with one folder per
repository:

`~/Secrets/rookrunner-secrets/<owner>/<repo>/`

`<owner>` and `<repo>` are the two halves of the GitHub repository
name. The root is outside the repository, the state directory, every
attempt workspace, and `~/Secrets/github-app/`. The key reader and
the secret reader are different directories.

`worker --secrets` takes no path. It requires
`worker --github-repository owner/name`. That flag is `owner/name`
with one slash. It is not a filesystem path. The secret directory is
derived from it. Any other shape is a refusal at startup, and the
error names the flag. The process resolves `~` for the user running
it and refuses a directory outside that root. `poll --repository`
must be the same `owner/name`. A mismatch refuses the job before a
step runs.

One file per secret. The file name is the secret name. The root, the
owner directory, and the repo directory are mode `0700`. Each file
is mode `0600`. None of them is a symlink. The owner uid is the user
running the process. The checks run on open file descriptors, not on
a path that is looked up again. The root is opened with
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`. `fstat` requires a directory,
mode `0700`, and the caller uid. `openat` walks `owner` and `repo`
the same way. The file is opened with `O_RDONLY|O_NOFOLLOW`, and
`fstat` requires a regular file, mode `0600`, and the caller uid.
The bytes are read from that descriptor. A missing directory, a
symlink, a mode that is group- or world-readable, or a path outside
the root is a refusal before a step runs. The error does not include
a path or the file bytes.

A secret name contains only ASCII letters, digits, and underscores,
does not start with a digit, and does not start with `GITHUB_`.
File names on disk are uppercase. A workflow reference is uppercased
before lookup, so `secrets.npm_token` reads `NPM_TOKEN`. A file
whose name is not that uppercase form is a refusal, and the error
names the file name. A value larger than 48 KB after the newline
rule below is refused. The value is not parsed as JSON, XML, or
YAML. GitHub says structured data makes redaction fail.

An empty file is refused. The refusal names the secret. A file whose
bytes are only a newline is empty after the strip below, and that is
the same refusal. A reference to a name that has no file resolves to
an empty string. The log names that secret. The name is not
sensitive, and the log does not include a path or a value. An empty
string is not registered as a mask.

Exactly one trailing `\n` is stripped. A second trailing `\n` stays.
A trailing `\r` stays. The check applies to the bytes read from the
descriptor. `echo value > FILE` therefore yields `value`.

`secrets.GITHUB_TOKEN` is not a file. The `GITHUB_` prefix means a
file cannot shadow the job token. That name is an alias for the job
token, in answer 2. Any other reference whose uppercase form starts
with `GITHUB_` is a refusal and names the field. A file in the
directory whose name starts with `GITHUB_` is a refusal and names
the file name.

When `--secrets` is set and answer 7 allows secrets for the run, the
directory is checked before the first step. Every directory entry
must be a legal file. Bytes are read only for names the job
references. An unreferenced file's bytes are not read. A bad entry
refuses the job before the first step. When the allowlist does not
match, the directory is not opened. When `--secrets` is omitted, the
directory is not opened, and a `secrets` reference stays the
withheld-context error the engine returns today.

The store is not the GitHub secrets API. The App has no Secrets
permission, and this design does not ask for one. The store is not
the worker process environment. Values are not copied into the
snapshot, the plan, or the run record.

https://docs.github.com/en/actions/reference/security/secrets

## How a value reaches a step

Accepted answer 2. A file-backed secret is delivered only to the
step whose `env`, `with`, or `run` text references that name.

`env` and `with` substitute the value as text. An expression that is
exactly `secrets.NAME` becomes the file value. An expression that is
exactly `secrets.GITHUB_TOKEN` becomes the job token. Any other
expression that reads `secrets` or `github.token` is a refusal and
names the field. The stored plan keeps the expression text. It does
not keep the value. The value is not written into the workspace, the
live `GITHUB_ENV` file, or a file the artifact manifest scans. A
step that does not reference the name does not receive that file's
value.

A `run` script is not given the spliced secret. GitHub splices the
value into the script. This engine rewrites the expression and sets
an environment variable. The rewrite uses the `${NAME}` form, under
the reserved prefix `RR_SECRET_`. The expression
`${{ secrets.API }}` becomes `${RR_SECRET_API}`. The characters that
follow stay put, so `${{ secrets.API }}_v2` becomes
`${RR_SECRET_API}_v2`. The engine sets `RR_SECRET_API` in that
step's environment and nowhere else. The script file contains the
rewritten text and does not contain the value.

`github.token` and `secrets.GITHUB_TOKEN` are the same alias. In
`run` they become `${RR_SECRET_GITHUB_TOKEN}`. On a job that minted
a token, the engine also sets `GITHUB_TOKEN` to that token on every
step. A file secret stays on the referencing step only.

A step `env` key that starts with `RR_SECRET_` is a refusal and
names the key. The engine is the only writer of that prefix. A
secret named `PATH`, `HOME`, `IFS`, `BASH_ENV`, `ENV`,
`LD_PRELOAD`, or `SHELLOPTS` therefore cannot replace the step's own
environment through the rewrite.

The rewrite applies only when the chosen shell is `bash` or `sh`.
That includes the omitted shell, which is bash with a fallback to
sh, and a custom shell whose command is bash or sh. Any other shell
is a refusal and names `run`. The script is not rewritten and the
step does not run.

The expression is rewritten only when a small lexer can prove it
sits in an unquoted or double-quoted context. Single quotes, ANSI-C
quotes (`$'...'`), a quoted heredoc, a comment, and any context the
lexer cannot prove are a refusal and name `run`. The lexer is not a
full bash parser. Doubt refuses the step.

`secrets` stays withheld from step `if`, job `if`, concurrency, job
`name`, step `name`, job outputs, job `env`, and workflow `env`.
The refusal names the field. For job `env` and workflow `env` it
tells the author to move the reference to the step's `env`. GitHub
allows `secrets` in job `env`, workflow `env`, job outputs, and
step `name`, and rejects them in step `if`, job `if`, concurrency,
and job `name`. Withholding job `env` and workflow `env` is a known
compatibility gap. It is not a permanent difference. This plan does
not close that gap.

An action input default that is exactly `github.token` or
`secrets.GITHUB_TOKEN` is evaluated when the step's `with` omits
that input. The value is the job token. Any other expression in an
action default that reads `secrets` or `github.token` is a refusal
and names the input. A step that uses one of those two defaults is
a reason to mint, in answer 4.

## Masking in logs and run records

Accepted answer 3. The mask uses the existing job mask list, with
the additions below. Omitting an output that contains a secret is
GitHub's behavior. The runner skips that output and logs a warning.
This engine does the same for an injected secret and for the job
token. The omission is recorded without the value. That rule is
compatible behavior. `add-mask` for a value that is not a secret
and not the job token still stores an output that contains the
masked text.

Before the first step of a job that will receive a secret or a job
token, the engine registers each value that will be injected and
the job token, when one was minted. `mask_text` replaces the value,
and each whitespace-separated word of it, with `***` in that job's
stdout, stderr, stored log pages, and error text. An empty value is
not registered. The mask list stays in memory for that job. It is
not stored in the run record, and the next job does not inherit it.

Registering the four characters `ghp_` would replace only those
four characters. A run of one or more ASCII letters and digits after
`ghp_`, `gho_`, `ghu_`, `ghs_`, or `ghr_` is masked together with
that prefix. A run of one or more ASCII letters, digits, and
underscores after `github_pat_` is masked together with that
prefix. The same rule masks a token that is not one of the injected
values. It also masks unrelated text that uses the prefix.

Each injected value and the job token also register encoded forms.
The base64 forms follow the three alignments the Actions runner
registers, plus the padding trim in the runner's base64-masking
note. Standard base64, no line breaks, of the UTF-8 bytes:

- the value, both with `=` padding and with trailing `=` removed
- the value with its first byte removed, then the same encoding
  with trailing `=` removed, when any bytes remain
- the value with its first two bytes removed, then the same
  encoding with trailing `=` removed, when any bytes remain

https://github.com/actions/runner/blob/main/docs/adrs/0297-base64-masking-trailing-characters.md

The JSON form is the contents of a JSON string for that value,
without the surrounding quotation marks. Quotation marks, reverse
solidus, and the control characters U+0000 through U+001F use the
escapes JSON requires. The URI form percent-encodes every byte
outside the unreserved set `A-Z a-z 0-9 - _ . ~`. Space is `%20`.
Hex digits are uppercase. A form shorter than 4 characters is not
registered. A transformation other than these forms still bypasses
the mask. Base64 of a larger wrapping, a split across commands, or
any other change of the bytes will not match. GitHub says redaction
is not guaranteed for the same reason. A step that prints a
transformed secret has exposed it. The operator rotates that secret.

Stdout and stderr are masked on complete lines. A trailing partial
line is held until the next read or the end of the stream. When a
read is not split on line boundaries, the hold is at least as long
as the longest registered mask. A value split across two reads of
one line is masked before it is stored. A value split across two
lines is not joined.

The same mask covers workflow-command annotation text (`::error::`
and `::warning::`), any stored copy of `GITHUB_STEP_SUMMARY`, and
any copy of `GITHUB_ENV`, `GITHUB_OUTPUT`, or `GITHUB_STATE` that
is written into the run record or a log. The live files the next
step reads keep the real bytes. The engine does not implement
`GITHUB_STEP_SUMMARY` today. A later writer of that file uses this
mask before the text is stored or sent.

The check-run post runs in another process and does not receive the
secret values or the job token. Text is masked before it is stored
in the run record, so a summary, an annotation, or a log excerpt
that the post sends is already masked. The post also applies the
prefix rules above to the request body. A proof item sends that
body to the local HTTP stub and shows `***` where the fixture value
was.

A secret, or one of its whitespace-separated words, shorter than 4
characters logs a warning at load time. The warning names the
secret and does not include the value. The value is still masked.
GitHub has the same short-value weakness. The warning is for the
operator.

This design does not scan artifact file bytes. `artifact.read` can
return a workspace file that contains a value.

https://docs.github.com/en/actions/reference/security/secrets
https://docs.github.com/en/actions/reference/security/secure-use

## Token minting, scope, and lifetime

Accepted answers 4 and 5. The job token is not the check-run post
token.

The mint is one installation access token for Rookrunner-App,
limited to the repository named by `worker --github-repository`.
The worker mints it only when `--app-key` is set, answer 6 allows
the worker to hold the key, the job's resolved permissions allow
Contents read, and at least one step needs the token. A step needs
the token when its `env`, `with`, or `run` text references
`github.token` or `secrets.GITHUB_TOKEN`, or an action input default
is exactly one of those two expressions. A job that never asks gets
no token and no key read.

The mint happens before the first step. A mint failure fails the
job before the first step and names the cause. The cause does not
include the token, the key path, or the key bytes. The job does not
run with the token missing in that case.

The request body always contains both of these fields. Neither is
omitted. `repositories` is a one-element array of the
`owner/name` from `--github-repository`. `permissions` is exactly
`{"contents": "read"}`. The test assertion is that body. Leaving
either field off would grant every App permission on every
installed repository.

https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app

The token is held in memory. It is not written to disk, the state
directory, the workspace, or a log. It is not cached across jobs.
Redirects are refused, as they are for the check-run post, so the
token is not sent to another host.

The token is revoked when the job ends, on success, failure, setup
failure, and cancel. Revocation is `DELETE /installation/token`
with that token. The run record says revocation was attempted, and
whether the call was accepted. It never stores the token. A revoke
failure does not change the job result. A job that minted nothing
records that revocation was not needed.

An installation access token expires after one hour. Its prefix is
`ghs_`. The token is not refreshed. When a job that holds a token
reaches 55 minutes after the mint, the worker logs one warning that
the token expires one hour after mint. The warning does not include
the token.

The App registration grants Checks write, Commit statuses write,
and Contents read. The post token keeps Checks write and Commit
statuses write and is minted only for that post. The job token does
not receive those two write scopes. `write` in a workflow stays
`CAPABILITY_UNSUPPORTED`. This design adds no App permission.

Permission resolution, once the pull request that mints has landed:

- An omitted `permissions` key mints Contents read when a step
  needs the token. That matches GitHub's restricted default.
- `read-all` mints Contents read only. A call to the checks,
  statuses, or actions read API with that token receives 403.
  That narrowing is accepted.
- A mapping that includes `contents: read` mints Contents read
  only, even when the mapping also grants other read scopes.
- `permissions: {}`, a mapping that sets every scope to `none`,
  and a mapping that does not grant `contents: read` mint no
  token. `GITHUB_TOKEN` stays unset. The job runs. GitHub issues
  a metadata-only token in the empty and all-none cases. That
  difference is accepted.
- `contents: write` and every other `write` stay
  `CAPABILITY_UNSUPPORTED` at plan time. No mint.

`worker --app-key` on this path is the same file rule as the post:
`~/Secrets/github-app/rookrunner-app/private-key.pem`. `poll` and
`status` keep their own `--app-key` for the post token. The two
mints stay separate.

https://docs.github.com/en/actions/concepts/security/github_token
https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types

## A worker started with `--docker-socket`

Accepted answer 6. `--docker-socket` is mutually exclusive with
`worker --app-key` and with `worker --secrets`. Startup refuses the
combination and names both flags.

The exception is a startup probe that shows `~/Secrets` is not
shared into the Docker VM. The probe uses `worker --runner-image`.
Without that image the probe cannot run, and the combination is
refused. The container's only host mount is `~/Secrets`, read-only.
It does not receive the Docker socket. Its command is a fixed
existence test. It prints no file names and no file bytes. The
probe passes when the mount is rejected or the path is not a
usable directory inside the container. The probe fails when the
path is a usable directory. The container is removed before the
worker serves. If the daemon cannot be contacted, or the image
fails for a reason other than the mount, startup refuses the
combination. On a Docker engine that can mount
any host path, the probe fails and the flags stay mutually
exclusive.

A worker that sets `--docker-socket` and sets neither `--app-key`
nor `--secrets` still starts. The host-control warning stays. The
warning says the exposure includes
`~/Secrets/github-app/rookrunner-app/` and
`~/Secrets/rookrunner-secrets/`.

## Which runs receive secrets

Accepted answer 7. File-backed secrets follow the ref and the
pusher.

The fork test compares numeric repository ids. The head repository
id and the base repository id are integers. They match when they
are equal. A missing head repository, including a deleted fork
whose `head.repo` is null, is a fork. A fork is recorded and runs
nothing. A same-repository pull request still runs.

A same-repository pull request, and a push whose ref is not the
default branch, may receive the Contents-read job token when the
other token gates pass. They receive file-backed secrets only when
the ref or the pusher is on the operator allowlist.

The allowlist is `worker --secret-ref` and `worker --secret-pusher`.
Each flag may be repeated. A ref entry is a full ref, such as
`refs/heads/main`. A pusher entry is a GitHub login, compared
ASCII-case-insensitively. A ref is compared exactly. With both
lists empty, the only match is a `push` event whose ref is
`refs/heads/` plus `event.repository.default_branch`. Adding a ref
or a pusher adds a match. The default push stays a match. Omitting
`--secrets` is how the operator turns file secrets off.

`poll` copies `repository.id`, `repository.default_branch`, and the
actor login into the event it already stores. For a push, the login
is the commit `author.login` when that field is a string. For a
pull request, the login is `user.login` on the pull request. A
missing login does not match a pusher entry. The default branch is
the `default_branch` string on the repository object. It is not the
local clone's `HEAD`. A local submit that does not carry those
event fields does not match the default rule.

The match is computed from the stored event. It is not a new plan
field. The plan schema stays unchanged.

## What stays rejected

These stay out of this design.

- A fork pull request, any other untrusted code, and
  `pull_request_target`.
- Mounting `~/Secrets/github-app/rookrunner-app/` or
  `~/Secrets/rookrunner-secrets/` into the job, and putting the
  post token or the private key in the job.
- A new App permission, a Secrets API permission, and any use of
  the GitHub secrets API.
- `write-all`, `id-token` as a minted OIDC token, `security-events:
  write`, `actions: write`, packages write, and pull-requests write.
- Contents write, and the poller skip for the Rookrunner-App bot.
  The skip is recorded above and is not implemented.
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
- Refreshing the job token, and writing it to disk.
- Creating the secret directory, minting a token, installing the
  App, starting a sign-in, or reading a key. The App ID is
  5201333. The installation and the private key are still pending,
  so an implementation cannot mint against the live App until the
  operator finishes those steps.

## Decided answers

Each answer is accepted. The sections above are the behavior. This
list is the record of the choice.

1. **Where secret values live.** One fixed root,
   `~/Secrets/rookrunner-secrets/<owner>/<repo>/`, outside the key
   directory. `worker --secrets` takes no path and requires
   `--github-repository owner/name`. One uppercase file per secret.
   Mode `0700` on each directory and `0600` on each file. No
   symlinks. Owned by the caller. Checks use `O_NOFOLLOW`, `fstat`,
   and `openat` on the descriptor that is read. Names are letters,
   digits, and underscores, and do not start with a digit or
   `GITHUB_`. Lookup uppercases the reference. A missing name is an
   empty string, and the log names the secret. An empty file is
   refused. Exactly one trailing newline is stripped. A value over
   48 KB is refused. `secrets.GITHUB_TOKEN` is the job token, not
   a file.
2. **Which fields receive a value.** Step `env`, step `with`, and
   step `run` on the step that names the secret. `env` and `with`
   receive the value. A `run` script is rewritten to
   `${RR_SECRET_NAME}`. The engine sets that variable. A step `env`
   key with that prefix is refused. The rewrite is bash and sh
   only, and only in an unquoted or double-quoted context a small
   lexer can prove. `secrets.GITHUB_TOKEN` aliases the job token in
   `env`, `with`, and `run`. An action input default that is exactly
   `github.token` or `secrets.GITHUB_TOKEN` is evaluated when `with`
   omits that input. Job `env` and workflow `env` stay withheld.
   The refusal names the field and points the author at the step's
   `env`. That gap is recorded, and this plan does not close it.
3. **Masking.** Register each injected value and the job token
   before the first step. Also mask `github_pat_` and the existing
   `ghp_`, `gho_`, `ghu_`, `ghs_`, and `ghr_` prefixes. Also
   register the base64 forms at three byte offsets, the JSON
   escape, and the percent-encoded form. Mask on complete lines,
   and hold a partial line long enough for the longest mask.
   Annotations, a stored step summary, stored copies of
   `GITHUB_ENV`, `GITHUB_OUTPUT`, and `GITHUB_STATE`, and the
   check-run post body are covered. An output that contains a
   value is omitted, with a warning and without the value. A
   secret or word shorter than 4 characters warns at load time.
4. **Token lifetime.** One installation token per job, minted
   before the first step only when a step needs it, held in
   memory, and revoked with `DELETE /installation/token` when the
   job ends. The record says revocation was attempted and does not
   store the token. A mint failure fails the job before the first
   step. The token is not refreshed. A job that holds one logs one
   warning at 55 minutes.
5. **What the job token may request.** Contents read only. Checks
   write and Commit statuses write stay on the post token. The
   mint body always names the repository and
   `permissions: {"contents": "read"}`. An omitted permissions key
   and `read-all` mint that scope. `permissions: {}` and an
   all-none mapping mint nothing, and `GITHUB_TOKEN` stays unset.
   `write` stays rejected. A future Contents write also requires
   the poller to skip the Rookrunner-App bot. That skip is not in
   this plan.
6. **A worker started with `--docker-socket`.** Mutually exclusive
   with `worker --app-key` and with `worker --secrets`, and the
   error names both flags. The exception is the startup probe that
   shows `~/Secrets` is not shared. The host-control warning names
   the key directory and the secret root.
7. **Which runs receive secrets.** Secrets are injected only when
   the ref or the pusher is on the allowlist. The default is a
   push to the default branch. Same-repository pull requests and
   other pushes still receive the Contents-read job token when the
   other token gates pass. Repositories are compared by numeric
   id. A null head repository is a fork. Forks run nothing.

## Implementation plan

Six pull requests, in this order, one at a time. Each merges before
the next branch is opened. Each is limited to the behavior named
here. The capability version stays 12. A version 11 plan is not
migrated. No plan field is added. The plan schema is unchanged.
`.github/workflows/check.yml` is unchanged. `write` stays rejected.
Tests point `HOME` at a temporary directory and use a fixture key
and a local HTTP stub. No test reads
`~/Secrets/github-app/rookrunner-app/` and no test contacts
`api.github.com`. No pull request installs the App, starts a
sign-in, or reads a real key.

1. **p7-mask.** Extend the job mask. Add the `github_pat_` prefix
   and keep the existing token prefixes. Register the base64,
   JSON, and percent-encoded forms above. Hold a partial line
   across reads. Mask annotation text. Mask stored copies of the
   env, output, and state files without changing the live files
   the next step reads. Mask check-run summary text before it is
   stored, and apply the prefix rules to the post body. Omit a job
   output that contains an injected secret or a prefix token, and
   record the omission without the value. Warn when a registered
   value or one of its words is shorter than 4 characters. This
   pull request does not open the secret directory and does not
   mint a token. The proof is a fixture value in stdout, in a
   split read, in an annotation, in base64 and the other two
   encodings, and in the JSON body the local check-run stub
   receives.
2. **p7-trust-gate.** The poll compares head and base repository
   ids. A null head repository is a fork and runs nothing. A
   same-repository pull request still runs. The poll copies
   `repository.id`, `repository.default_branch`, and the actor
   login into the event it already stores. The allowlist match is
   a pure function of that event plus the ref and pusher lists:
   the default push matches, another ref does not, a listed ref or
   login matches, and a missing login does not. This pull request
   does not read a secret and does not mint a token. The proof
   uses the local HTTP stub.
3. **p7-socket-lock.** Add `worker --secrets`,
   `worker --github-repository`, `worker --app-key`,
   `worker --secret-ref`, and `worker --secret-pusher`.
   `--secrets` takes no path. `--app-key` accepts only
   `~/Secrets/github-app/rookrunner-app/private-key.pem`.
   `--docker-socket` combined with `--app-key` or `--secrets`
   refuses at startup and names both flags, unless the `~/Secrets`
   probe exits 0. The host-control warning names the key directory
   and the secret root. This pull request does not inject a secret
   and does not mint a job token. `poll` and `status` keep the
   post credential they have today. The proof uses a temporary
   `HOME` and does not read the real key directory.
4. **p7-secret-env.** Depends on the three pull requests above.
   When `--secrets` is set, the socket lock passed, and the
   allowlist matches, a step `env` or `with` expression that is
   exactly `secrets.NAME` receives the file value, on that step
   only. A missing name is an empty string and the log names it.
   An empty file, a bad mode, a symlink, a lowercase file name, a
   second repository's folder, and a `GITHUB_` file are refusals.
   One trailing newline is stripped. The value is registered on
   the mask list before the step writes a log line. The plan, the
   run record, and stored log pages do not contain it.
   `secrets.GITHUB_TOKEN` is recognized and stays unset until the
   token pull request. A reference with `--secrets` omitted stays
   today's withheld-context error. Job `env`, workflow `env`,
   outputs, names, `if`, and concurrency stay withheld, and the
   error names the field. A step `env` key that starts with
   `RR_SECRET_` is refused.
5. **p7-secret-run.** Depends on p7-secret-env. A `run` expression
   that is exactly `secrets.NAME` becomes `${RR_SECRET_NAME}` when
   the shell is bash or sh and the lexer proves the context. The
   engine sets that variable on that step. The script file does
   not contain the value. `${{ secrets.API }}_v2` becomes
   `${RR_SECRET_API}_v2`. Another shell, single quotes, `$'...'`,
   a quoted heredoc, a comment, or an unproven context refuses and
   names `run`, and the step does not run.
6. **p7-job-token.** Depends on p7-socket-lock, p7-secret-env, and
   p7-secret-run. The worker mints one installation token when a
   step needs it and the resolved permissions allow Contents read.
   The mint body is the assertion in the token section. The mint
   runs before the first step. A failure fails the job and names
   the cause. `permissions: {}` and an all-none mapping mint
   nothing. The token is revoked on every end path, and the record
   stores the attempt, not the token. One warning is logged at 55
   minutes. `secrets.GITHUB_TOKEN` and `github.token` deliver that
   token in `env`, `with`, and `run`. The two action-input
   defaults are evaluated when `with` omits the input. The
   check-run post token is unchanged. If the run record schema
   rejects an unknown property, the optional revocation field is
   added the same way the check-run fields were added. It is not a
   plan field. The Contents-write poller skip is not implemented.
   The proof uses a fixture key under the temporary `HOME` and the
   local HTTP stub.

## What an implementation would prove

This design proves none of these. The pull request named beside
each item proves it.

1. A workflow that references no secret and needs no token runs as
   it does today. `security-events: write` and `actions: write`
   still fail planning. p7-secret-env and p7-job-token.
2. A secret value is absent from the plan, the run record, the
   state directory, and stored log pages. The log shows `***`,
   including a value split across two reads, a base64 form, a
   JSON escape, a percent-encoded form, an annotation, and the
   check-run body. p7-mask and p7-secret-env.
3. The value is in the environment of a step that references it in
   `env` or `with`. The stored script contains `${RR_SECRET_NAME}`
   and does not contain the value. A step that does not reference
   the name does not receive the file secret. A missing name is
   empty. An empty file is a refusal. p7-secret-env and
   p7-secret-run.
4. The job token is revoked when the job ends. The record does not
   contain the token. The token is not the check-run post token.
   The mint body names the repository and Contents read.
   `permissions: {}` mints nothing. A mint failure fails the job
   before the first step. The key path rules in
   [check runs](check-runs.md) still hold. p7-job-token.
5. A worker that combines `--docker-socket` with `--app-key` or
   `--secrets` refuses and names both flags, unless the
   `~/Secrets` probe passes. p7-socket-lock.
6. A fork pull request still creates no run and reads no secret,
   including a pull request whose head repository is null. The
   comparison uses numeric ids. A push that is not the default
   branch receives no file secret under the default allowlist.
   p7-trust-gate and p7-secret-env.
7. `.github/workflows/check.yml` does not contain a secret, a
   token, or `--app-key`. The capability version stays 12. Every
   pull request in the plan.

## What this design does not do

p7-mask and p7-trust-gate change the engine as the plan names.
No secret directory is created. No token is minted. No sign-in is
started. No file under
`~/Secrets/github-app/rookrunner-app/` is read. The App is not
installed by this document. Its App ID is 5201333. No App
permission is added. The installation and the private key remain
pending. `write` stays rejected. `GITHUB_TOKEN` stays unset.
