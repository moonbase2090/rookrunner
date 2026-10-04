# Dogfood check

Status: design, 2026-10-03. This slice writes this document and does not
change the engine. NS-31 through NS-38 implement the path for this
repository's `.github/workflows/check.yml`. The capability version stays
9. This is not a GitHub-equivalence claim.

`check.yml` was read on 2026-10-03. It has one job, `check`. The
checkout step is `actions/checkout` at
`11d5960a326750d5838078e36cf38b85af677262`, whose action file declares
`runs.using: node20`. The setup step is `astral-sh/setup-uv` at
`c18668ad3cf93ea998bef934396af7bb5c839dc7`, whose action file declares
`runs.using: node24`, `main`, `post`, and `post-if: success()`. Its
`working-directory` default is `${{ github.workspace }}` and its
`github-token` default is `${{ github.token }}`. `enable-cache` defaults
to `auto`. The remaining steps are `uv sync --locked --group dev`,
ruff, and unittest.

## What each element uses

| Element | Behavior | Slice |
| --- | --- | --- |
| `name: check` | The workflow name is stored on the plan | Existing |
| `on: push` and `on: pull_request` | The trigger is stored and is not evaluated | Existing. Evaluation is NS-41, after dogfood |
| `permissions: contents: read` | A read-only permissions map is accepted. It grants no token | NS-31 |
| `jobs.check` | One concrete job runs in the caller-pinned container | Existing |
| `runs-on: ubuntu-latest` | The label is accepted. The caller pins the image by digest | Existing |
| Checkout `uses` pinned by 40 hex digits | The step is the owned checkout. The SHA is stored and is not fetched. The action file is not read and its JavaScript does not run | NS-32 |
| `astral-sh/setup-uv` pinned by 40 hex digits | The action is fetched and stored under its content digest | NS-34 |
| `runs.using: node24` | Node comes from the operator-supplied directory | NS-35 |
| `main: dist/setup/index.cjs` | Node runs that file | NS-36 |
| `with.version` and `with.python-version` | Each input arrives as `INPUT_<NAME>` | NS-36 |
| Defaults `${{ github.workspace }}` and `${{ github.token }}` | The contexts below supply those values | NS-33 |
| `enable-cache: auto` | `RUNNER_ENVIRONMENT` is `self-hosted`, so the action's GitHub-hosted cache stays off | NS-33 |
| `post` and `post-if: success()` | The post entry runs after the main steps when the job succeeded | NS-37 |
| `uv sync`, ruff, and unittest | `run` steps. `uv` arrives through the `GITHUB_PATH` write from setup-uv | Existing (NS-15) |
| Default `bridge` network | A job can reach the public internet. `--network none` turns that off | Existing |
| Checkout `runs.using: node20` | That action is the owned checkout, so its `node20` entry is not executed. Any other `node20` action stays rejected | NS-32 and the `node20` answer below |

https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
https://github.com/actions/checkout/blob/11d5960a326750d5838078e36cf38b85af677262/action.yml
https://github.com/astral-sh/setup-uv/blob/c18668ad3cf93ea998bef934396af7bb5c839dc7/action.yml

## `github.sha`

`github.sha` and `GITHUB_SHA` are the manifest `base_commit` when
`dirty` is false, `included` is empty, and `base_commit` is a commit
id. A null `base_commit`, a dirty capture, or a non-empty `included`
list leaves both unset. The synthesized commit id is not used.

GitHub documents `GITHUB_SHA` for `push` as the tip commit pushed to
the ref.
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push
https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context

Intentional local difference: the value comes from the captured
snapshot, not from a push payload. A clean capture with an empty
include list is that commit. Any other snapshot is not named as that
commit. When the object store is present, workspace `HEAD` is the
synthesized commit from [Git directory](git-directory.md). That id and
`github.sha` differ. `git rev-parse HEAD` is not a way to read
`github.sha`.

## Where each `github` property comes from

A property is set from the plan, from the attempt, or from the caller
event, or it stays unset. An unset property reads as an empty string,
which is the existing missing-property rule. Nothing is read from the
user's Git configuration, from the host credential store, or from the
environment of the worker.

| Property | Source |
| --- | --- |
| `github.event` | The caller event object. This is the existing value |
| `github.event_name` | The submit `event_name` when the caller sends one. An absent `event_name` leaves the property unset. The event body is not mined for a name |
| `github.event_path` | The existing event file, `/run/rookrunner/event.json` |
| `github.workspace` | `/workspace`, the attempt workspace in the job container |
| `github.job` | The current job id on the plan |
| `github.workflow` | The workflow `name` on the plan. When `name` is absent, the workflow path |
| `github.sha` | The rule in the previous section |
| `github.action_path` | The existing composite-action path, and only while those inner steps run |
| `github.token` | Unset |
| Every other `github` property | Unset. That includes `actor`, `ref`, `repository`, `base_ref`, `head_ref`, `run_id`, `server_url`, `api_url`, `workflow_ref`, and `workflow_sha` |

`github.token` stays unset on purpose. The contexts reference describes
it as a token for the GitHub App installed on the repository.
https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context
Secrets and `GITHUB_TOKEN` stay deferred. setup-uv's `github-token`
input therefore receives an empty string. Its `download-from-astral-mirror`
default is true, so the dogfood download does not need that token. A
run that fails closed on the empty token is a gap for NS-38 to name.

The same values are the default variables `GITHUB_WORKSPACE`,
`GITHUB_JOB`, `GITHUB_WORKFLOW`, `GITHUB_EVENT_NAME`,
`GITHUB_EVENT_PATH`, and `GITHUB_SHA`. `GITHUB_EVENT_PATH` keeps the
existing event-file path. The variables reference example is
`/github/workflow/event.json`.
https://docs.github.com/en/actions/reference/workflows-and-actions/variables

Intentional local difference: the event file stays where the engine
already writes it.

`GITHUB_*` and `RUNNER_*` names still cannot be overwritten. `CI` can
be overwritten. `HOME` can be overwritten. Those two are not in the
reserved set.

## `GITHUB_ACTIONS`

`GITHUB_ACTIONS` stays unset. The variables reference says it is always
`true` when GitHub Actions is running the workflow.
https://docs.github.com/en/actions/reference/workflows-and-actions/variables

Intentional local difference: this engine is not GitHub Actions, so it
does not set that variable. It does not set the variable to `false`.
The name is not reserved. `check.yml` does not read it. setup-uv's
action file does not read it. NS-38 names a gap if the action's
JavaScript requires it.

## Runner directories

NS-33 sets these, both as context properties and as variables:

| Name | Value |
| --- | --- |
| `runner.os`, `RUNNER_OS` | `Linux` |
| `runner.arch`, `RUNNER_ARCH` | From the image platform: `linux/amd64` is `X64`, `linux/arm64` is `ARM64`, `linux/386` is `X86`, and `linux/arm` is `ARM` |
| `runner.environment`, `RUNNER_ENVIRONMENT` | `self-hosted` |
| `runner.temp`, `RUNNER_TEMP` | `/github/runner-temp` |
| `runner.tool_cache`, `RUNNER_TOOL_CACHE` | `/github/tool-cache` |
| `HOME` | `/github/home` |
| `CI` | `true` |

An image platform outside that table fails setup with the existing
setup failure. No architecture string is invented.

`runner.os` and `runner.environment` use the documented values. This
job container is Linux, and the worker is not a GitHub-hosted runner.
https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#runner-context
https://docs.github.com/en/actions/reference/workflows-and-actions/variables

`CI` follows the variables reference, which says it is always `true`.

`HOME` follows the path GitHub mounts for a job container. The
self-hosted runner mounts `_temp/_github_home` at `/github/home`,
read-write.
https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/customizing-the-containers-used-by-jobs

Intentional local difference: GitHub's container paths for the temp
directory and the tool cache are `/__w/_temp` and `/__w/_tool`. This
engine's workspace is `/workspace` and does not use the `/__w` layout.
The temp directory and the tool cache use `/github/runner-temp` and
`/github/tool-cache`.

On the host, the three directories are siblings of the attempt
workspace: `home`, `runner-temp`, and `tool-cache`, each mode 0700.
They are created for the attempt and removed with the attempt. They
sit outside the workspace, so they are not in the artifact manifest.
`usage(state)` includes them. This design adds no byte cap. The
existing disk budget is the only limit.

`RUNNER_TEMP` is emptied at the start of each job in the attempt. A
file the job user cannot delete stays, which is the variables-reference
rule. `HOME` and `RUNNER_TOOL_CACHE` are not emptied between jobs.
They do not survive the attempt. The tool cache starts empty. It is
not the GitHub-hosted tool cache, and it does not contain Node.
Node 24 is the separate read-only mount below.

setup-uv's `enable-cache: auto` enables caching on GitHub-hosted
runners. `RUNNER_ENVIRONMENT=self-hosted` leaves that cache off. The
actions cache service stays deferred.

## Fetching a SHA-pinned action

NS-34 fetches `uses: {owner}/{repo}@{sha}` and
`{owner}/{repo}/{path}@{sha}` with Git over HTTPS and no credential.
The URL is `https://github.com/{owner}/{repo}.git`. The fetch asks for
that one full-length commit. The Git environment matches capture:
`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL` pointing at `/dev/null`,
`GIT_TERMINAL_PROMPT=0`, no credential helper, and no
`http.extraheader`. Host Git configuration is not read.

The REST archive endpoint is not used. An unauthenticated REST client
has a primary limit of 60 requests per hour.
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api

This design adds no request counter and no numeric limit. A refused or
failed fetch is the NS-34 fetch failure: no run, and the submission
key is not consumed. There is no fallback to the archive API. Tests
use a local repository in place of github.com, as NS-34 already
requires. The fetched tree is stored by content digest and is not
copied into the workspace `.git`. The synthesized commit stays the
only commit in that directory.

## Node 24

`worker --node24 DIR` names an unpacked Node.js 24 distribution for
the image architecture. The worker mounts that directory read-only at
`/opt/node24`. The binary is `/opt/node24/bin/node`. It is not placed
on `PATH` for `run` steps. NS-36 invokes it by that path.

The worker does not download Node and does not bundle it. Packaging
revisits bundling. `worker.describe` reports the directory digest only
when the flag is set. A run that uses the mount records that digest
and the version `node --version` prints in the job container.

## `runs.using: node20`

`node20` stays rejected by name. Planning fails with the existing
`CAPABILITY_UNSUPPORTED` and names `runs.using`. The 2026-09-23
changelog says Node 20 is no longer available in GitHub Actions.
https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/

The changelog does not say which runtime executes an action that still
declares `node20`. Until a cited rule says how that action runs, this
engine does not choose a runtime for it.

The only `node20` action in `check.yml` is `actions/checkout`. NS-32
accepts a 40-hex pin of that action as the owned checkout and does not
run its JavaScript. A different action that declares `node20` still
creates no run.

## Action files in the container

NS-34 keeps the fetched action in a content-addressed store. NS-36
copies that directory into the attempt and mounts the copy read-write
at `/actions/{owner}/{repo}/{sha}`, with the action path appended when
`uses` has one. The store itself is not mounted into the job. A write
next to the action files stays in the copy. It is not written back to
the store.

GitHub mounts the runner's `_actions` directory read-write at
`/__w/_actions`.
https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/customizing-the-containers-used-by-jobs

The copy is removed with the attempt, sits outside the workspace, and
is omitted from the artifact manifest. `usage(state)` includes it. No
new byte cap is added.

`github.action_path` stays limited to composite actions, matching the
contexts reference. NS-36 runs a JavaScript `main` file by its path in
this copy and does not set `github.action_path` for that process.

## What stays out

These stay out of NS-31 through NS-38:

- `github.token`, `GITHUB_TOKEN`, secrets, and `write` permissions
- A webhook listener and runner registration
- Docker actions, `pre` entries, tag or branch action refs, and private
  action repositories
- The actions cache and artifact HTTP services
- Downloading or bundling Node
- Setting `GITHUB_ACTIONS`
- Using the synthesized commit id as `github.sha`
- macOS and Windows jobs

## Limits

This design adds no numeric limit. The limits the later slices already
cite stay the ones in
[next steps](../planning/next-steps.md#limits-used). The disk budget
remains the existing 10 GB repository cache limit. The unauthenticated
REST primary limit is the reason the fetch uses Git rather than a new
local quota.

## Acceptance

- Each element of `check.yml` maps to one of NS-31 through NS-37, or
  to behavior the engine already has. `on` stays stored and unevaluated
  until NS-41.
- `github.sha` is `base_commit` only for a clean capture with an empty
  `included` list. The synthesized commit is not that value. Workspace
  `HEAD` remains the synthesized commit when the store is present.
- `github.token` stays unset. No property is read from host Git
  configuration.
- `GITHUB_ACTIONS` stays unset.
- `HOME`, `RUNNER_TEMP`, and `RUNNER_TOOL_CACHE` have the paths above.
  They are removed with the attempt and count toward the existing disk
  budget.
- A SHA-pinned remote action is fetched with Git over HTTPS and no
  credential.
- Node 24 is the operator-supplied directory mounted read-only at
  `/opt/node24`.
- `node20` stays rejected by name. SHA-pinned `actions/checkout` is the
  owned checkout and does not run that JavaScript.
- Action files are copied into the attempt. The store is not mounted
  into the job.
- No code changes. The capability version stays 9.

## Out of scope

NS-39 designs owner CI. This document does not settle the credential,
the poll pass, or commit statuses. NS-38 is the run of `check.yml`.
If that run finds a gap this map does not cover, the next slice is
that gap.
