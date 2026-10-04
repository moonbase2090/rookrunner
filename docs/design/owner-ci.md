# Owner CI

Status: design, 2026-10-04. This slice writes this document and does not
change the engine. The capability version stays 12. A version 11 plan
is not migrated. NS-40 through NS-43 implement the path. This is not a
GitHub-equivalence claim.

The scope is the operator's own repositories. It is outbound HTTPS
only. There is no listener, no runner registration, no Terraform, and
no hosted service.

## Trust

Only a repository the operator configured runs, and that repository is
trusted code. A pull request whose head repository full name differs
from the configured repository is a fork. The poll records it and runs
nothing. `pull_request_target` is not evaluated.

The PRD trust model is unchanged. The initial release supports trusted
repositories owned by the local user, and a local Docker run is not a
sandbox for hostile code. GitHub's secure-use guidance says a
self-hosted runner should almost never run a pull request from a public
fork.

https://docs.github.com/en/actions/reference/security/secure-use

## Trigger

A one-shot poll pass, started by the operating system's scheduler
(cron, launchd, or a systemd timer). It is not a resident service.
Webhooks need an inbound listener. The PRD lists a network listener as
a non-goal, so webhooks stay deferred. Registering as a GitHub
self-hosted runner stays deferred. That path speaks GitHub's job
protocol, not this engine.

The poll interval is the operator's schedule. It is not a number in
this engine. A pass follows GitHub's rate-limit response headers and
stops when nothing remains. The next pass resumes.

## Source

Each configured repository has one dedicated clone. A run captures a
clean work tree at one commit, with no included files.

For `push`, that commit is the ref tip. `github.sha` is that tip when
the capture is clean and `included` is empty, which is the NS-30 rule.
GitHub documents `GITHUB_SHA` for `push` as the tip commit pushed to
the ref.

https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push

For `pull_request`, the tested commit is the merge commit on
`refs/pull/<number>/merge`. GitHub documents `GITHUB_SHA` for this
event as the last merge commit on that ref, and `GITHUB_REF` as
`refs/pull/<number>/merge`. The same page says a `pull_request`
workflow does not run while the pull request has a merge conflict.
When the merge ref is absent, the pass records that and runs nothing.

The commit status for a pull request is posted on the head SHA,
`pull_request.head.sha`. GitHub's pull request page shows statuses on
the commits in the pull request. The head SHA is that commit. The
tested commit and the status SHA differ. Testing the head branch
instead of the merge commit is not this design.

https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request
https://docs.github.com/en/rest/commits/statuses

NS-40 posts one status. It refuses, with no HTTP request, unless the
run is a workflow run, the snapshot is clean, `included` is empty, and
`base_commit` equals the tested commit. For a push, the tested commit
and the status SHA are the same. For a pull request, the tested commit
is the merge commit and the status SHA is the head SHA.

## Event payload

The event is built from REST responses. The fields are `ref`,
`before`, `after`, `repository.full_name`, and, for a pull request,
the number, head, and base. No other field is added.

Push:

| Field | Source |
| --- | --- |
| `ref` | The updated ref, `refs/heads/<branch>` |
| `before` | The tip this poll stored for that ref. A ref with no stored tip uses forty `0` characters. That first observation is a local poll rule. The push payload's `before` is the SHA of the most recent commit on the ref before the push |
| `after` | The tip after the poll's fetch. This is the tested commit |
| `repository.full_name` | The configured repository |

Pull request:

| Field | Source |
| --- | --- |
| `ref` | `refs/pull/<number>/merge` |
| `before` | `base.sha`. The pull request object has no push-style `before`. Using `base.sha` is a local mapping |
| `after` | The merge commit this poll fetched. This is the tested commit |
| `repository.full_name` | The configured repository |
| `number` | The pull request number |
| `pull_request.head.sha` | The head SHA. This is the status SHA |
| `pull_request.head.ref` | The head ref name |
| `pull_request.head.repo.full_name` | The head repository. A difference from the configured repository marks a fork |
| `pull_request.base.sha` | The base SHA |
| `pull_request.base.ref` | The base ref name |

https://docs.github.com/en/webhooks/webhook-events-and-payloads#push
https://docs.github.com/en/rest/pulls/pulls

`event_name` is `push` or `pull_request`. NS-41 evaluates `on` only
when that name is sent. `workflow_dispatch` and `schedule` stay stored
and are not evaluated.

## Reporting

Commit statuses first. Any credential that can write a commit status
can create one. Creating a check run is limited to GitHub Apps, so
check runs wait until the credential decision below is a GitHub App.
The context is `rookrunner/` plus the workflow file name plus `/` plus
the job id. The state mapping is NS-40: `queued` and `running` post
`pending`, `succeeded` with exit code 0 posts `success`, `failed`
posts `failure`, and `cancelled`, `lost`, or any other state posts
`error`. Nothing else posts `success`.

GitHub allows 1,000 statuses per SHA and context. NS-40 records each
posted state and sends nothing when the same terminal state is posted
again.

https://docs.github.com/en/rest/commits/statuses
https://docs.github.com/en/rest/checks/runs

## Credential

Open owner decision. The two candidates are a GitHub App installation
token and a fine-grained personal access token. This design does not
choose. Either candidate can create a commit status if it has that
write. A check run still requires a GitHub App.

The credential is read at call time from an operator file outside the
repository. It never enters the state directory, a job container, a
snapshot, or a log. Job secrets stay disabled. `GITHUB_TOKEN` stays
unset. A `write` permissions value stays rejected.

https://docs.github.com/en/rest/commits/statuses

## Limits

No number is added here. The limits already cited for this track:

| Limit | Value | Source | Used by |
| --- | --- | --- | --- |
| Self-hosted job execution time | 5 days | https://docs.github.com/en/actions/reference/limits | Existing job bound |
| Self-hosted job queue time | 24 hours | same | NS-42 |
| Cache storage | 10 GB per repository | same | Existing disk budget |
| REST primary limit, unauthenticated | 60 requests per hour | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api | Existing fetch |
| REST primary limit, authenticated user | 5,000 requests per hour | same | NS-42 |
| REST primary limit, GitHub App installation | 5,000 per hour minimum, 12,500 maximum outside Enterprise Cloud | same | NS-42 |
| Content-generating requests | 80 per minute and 500 per hour | same | NS-40, NS-42 |
| Commit statuses | 1,000 per SHA and context | https://docs.github.com/en/rest/commits/statuses | NS-40 |
| Creating check runs | GitHub Apps only | https://docs.github.com/en/rest/checks/runs | Stays deferred |
| `paths` filter diff | 3,000 files. More than 1,000 commits, or a diff timeout, always runs | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax | NS-41 |
| Pending runs in a concurrency group | 100, with `queue: max` | same | Provisional P1 |

## Coexistence

GitHub-hosted checks keep running. A Rookrunner status is another
context on the same SHA. Making a context required is an owner
decision and stays open.

## Gap inventory

Read on 2026-10-04. The repositories are Rookrunner, Scorecard, and
the website repositories cairn-website, lightwell-website,
moonbase2090-website, prismattyc-website, rookrunner-website, and
scorecard-website. Every workflow whose `on` includes `push` or
`pull_request` was planned with `plan_workflow` at capability version
12. `action_root` was the repository. `action_store` was omitted, so
a remote `uses` that is not the owned checkout stops the planner
before the action file is read. A 40-character lowercase SHA is the
pin NS-34 fetches when a store is supplied. `node20`, Docker, and
`pre` stay rejected once that file is read.

The planner returns one error. A second walk records every field
outside the planner's allow-lists, every permissions value other than
`read` or `none`, every `uses` that is not the owned checkout and not
`./` or `$/`, and every owned-checkout `with` key other than
`clean: false` or `persist-credentials: false`. Eighteen workflows
are in the inventory.

Rookrunner `check.yml` is the dogfood workflow. Its checkout pin is
the owned checkout. `astral-sh/setup-uv` is pinned by a 40-character
SHA. With no store, the planner reports
`jobs.check.steps.1.uses` as `CAPABILITY_UNSUPPORTED`. The dogfood run
supplied a store and that pin ran. That uses is not a new gap.

### Ranked by workflows blocked

| Rank | Field | Workflows | Disposition |
| --- | --- | --- | --- |
| 1 | `concurrency` | 17 | The planner rejects the workflow before any job body. This is the first engine gap after NS-43 |
| 2 | a permissions value of `write` | 10 | Stays rejected until a reviewed secrets design |
| 3 | `actions/upload-artifact` pinned by a full SHA | 8 | The artifact HTTP service is deferred |
| 4 | `github/codeql-action/upload-sarif` pinned by a full SHA | 6 | Same deferral. These workflows also set `security-events: write` |
| 5 | `actions/checkout@v5` | 5 | Not a 40-character SHA. All five are deploy workflows, which are out of scope |
| 6 | `runs-on: macos-14` | 2 | The planner accepts the label. macOS jobs stay deferred |
| 7 | `actions/download-artifact` pinned by a full SHA | 1 | Scorecard `release.yml`, which is out of scope |
| 8 | `with.fetch-depth: 0` on the owned checkout | 1 | Scorecard `scorecard.yml` |

Sixteen of the eighteen workflows contain `${{ }}`. The two that do
not are Rookrunner `check.yml` and prismattyc-website `deploy.yml`.
The planner stores that text. Inside `run`, and in any mixed string,
the braces stay literal. That is the existing expression rule. It
does not fail planning, so it ranks behind `concurrency`.

Deploy and publish workflows need secrets or a `write` permission.
They stay out of scope: Scorecard `release.yml`, and every website
`deploy.yml`, plus prismattyc-website `cloudflare.yml`. Their fields
are still listed below.

`workflow_dispatch` and `schedule` appear on some of these files.
NS-41 does not evaluate them.

### Rookrunner

`check.yml`, events `push` and `pull_request`, job `check`. Planner
result: `CAPABILITY_UNSUPPORTED` at `jobs.check.steps.1.uses` because
no action store was supplied. The pin is a full SHA and the dogfood
run fetched it. No other field is outside the allow-lists.

### Scorecard

`ci.yml`, events `pull_request`, `push`, and `workflow_dispatch`. Jobs
`check-ubuntu`, `check-macos`, `deny`, `msrv`, `coverage`, and
`dogfood`. Every job's planner result is `CAPABILITY_UNSUPPORTED` at
`concurrency`.

Other fields:

- `jobs.coverage.permissions.actions` is `write`
- `jobs.dogfood.permissions.actions` is `write`
- `jobs.check-macos` uses `runs-on: macos-14`
- Full SHA uses, rejected in this run because no store was supplied:
  `dtolnay/rust-toolchain@6bed0761d98439e5a578e2877258200ad565ba87`,
  `Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6`,
  `actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9`,
  `shivammathur/setup-php@f3e473d116dcccaddc5834248c87452386958240`,
  `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`
- The checkout steps are the owned checkout

`docs.yml`, events `pull_request`, `push`, and `workflow_dispatch`.
Job `docs`. Planner result: `concurrency`. Other fields are the
rust-toolchain and rust-cache pins above.

`release.yml`, events `push`. Jobs `apple`, `linux`, and `publish`.
Out of scope. Planner result: `concurrency`. Other fields:
`jobs.publish.permissions.contents` is `write`, `runs-on: macos-14`
on `apple`, and full SHA uses of rust-toolchain,
`actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`,
and
`actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`.

`scorecard.yml`, events `pull_request` and `push`. Job `scorecard`.
Planner result: `concurrency`. Other fields:
`permissions.security-events` is `write`,
`jobs.scorecard.steps.0.with.fetch-depth` is `0`, and full SHA uses of
rust-toolchain and rust-cache. `uses: ./action` is a local composite.
The planner stopped at `concurrency` and did not read that file.

### Websites

The six `scorecard.yml` files, events `pull_request` and `push`, job
`scorecard`, are the same shape. Planner result for each:
`concurrency`. Other fields on each:
`permissions.security-events` is `write`,
`actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`,
and
`github/codeql-action/upload-sarif@2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2`.
The checkout step is the owned checkout.

Deploy workflows, out of scope:

| Repository | Workflow | Events | Planner field | Other fields |
| --- | --- | --- | --- | --- |
| cairn-website | `deploy.yml` | `push`, `workflow_dispatch` | `concurrency` | `actions/checkout@v5`, `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020` |
| lightwell-website | `deploy.yml` | `push`, `workflow_dispatch` | `concurrency` | `actions/checkout@v5`, the same setup-node pin |
| moonbase2090-website | `deploy.yml` | `push`, `schedule`, `workflow_dispatch` | `concurrency` | `actions/checkout@v5`, the same setup-node pin |
| prismattyc-website | `cloudflare.yml` | `push`, `pull_request`, `workflow_dispatch` | `concurrency` | `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`. Checkout is the owned checkout |
| prismattyc-website | `deploy.yml` | `push` | `concurrency` | `permissions.id-token` is `write`. `aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd`. Checkout is the owned checkout |
| rookrunner-website | `deploy.yml` | `push`, `workflow_dispatch` | `concurrency` | `actions/checkout@v5`, the same setup-node pin |
| scorecard-website | `deploy.yml` | `push`, `workflow_dispatch`, `schedule` | `concurrency` | `actions/checkout@v5`, the same setup-node pin. Jobs `release` and `deploy` |

## Order after this design

The provisional list in the next steps was read on 2026-10-03, before
this inventory. Reordered by the counts above:

1. `concurrency`. It blocks 17 workflows at plan time.
2. Expressions inside `run` and mixed strings. Sixteen workflows
   contain `${{ }}`, and the text stays literal.
3. `actions/upload-artifact` and the CodeQL SARIF upload.
4. `actions/checkout` by tag, and `fetch-depth`. The tag form is on
   deploy workflows. `fetch-depth` is one Scorecard workflow.
5. Check runs, if the owner chooses a GitHub App.
6. A runner image for `ubuntu-latest` jobs that use `sudo` and apt.
7. `write` permissions and `GITHUB_TOKEN`. These wait for a reviewed
   secrets design. macOS jobs stay deferred with them.

## What this slice does not do

No code changes. No credential file. No poll, no status POST, and no
evaluation of `on`. NS-40, NS-41, and NS-42 do that work, citing the
sections above.
