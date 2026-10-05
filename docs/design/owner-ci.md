# Owner CI

Status: design, 2026-10-04. This slice writes this document and does not
change the engine. The capability version stays 12. A version 11 plan
is not migrated. NS-40 through NS-43 implement the path. NS-43 recorded one push and
one pull request for moonbase2090/rookrunner
([validation](../validation/owner-ci-rookrunner.md)). The credential
is the Rookrunner GitHub App recorded below. P5 posts one check run
when `--app-key` is set, then posts the commit status with that
installation token ([check runs](check-runs.md)). Omitting `--app-key`
keeps the NS-40 token file. The plan schema is unchanged. P7 designs
secrets, `GITHUB_TOKEN`, and `write` permissions for
owner-repository push and pull-request runs on this one local worker
([secrets](secrets.md)). p7-mask changes the job mask. p7-trust-gate
compares pull-request repository ids and stores the allowlist fields
on the event. p7-socket-lock adds the worker secret flags and refuses --docker-socket combined with --app-key or --secrets unless the ~/Secrets probe exits 0. The other P7 pull requests are not implemented. This is
not a GitHub-equivalence claim.

The scope is the operator's own repositories. It is outbound HTTPS
only. There is no listener, no runner registration, no Terraform, and
no hosted service.

## Trust

Only a repository the operator configured runs, and that repository is
trusted code. A pull request whose head and base repository ids are
not equal integers is a fork. A missing head repository, including a
null head repository, is a fork. The poll records it and runs nothing.
The comparison does not use the full name. `pull_request_target` is
not evaluated.

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

NS-40 posts one status with the CLI command `status`. It refuses, with no HTTP request, unless the
run is a workflow run, the snapshot is clean, `included` is empty, and
`base_commit` equals the tested commit. For a push, the tested commit
and the status SHA are the same. For a pull request, the tested commit
is the merge commit and the status SHA is the head SHA. NS-41 evaluates `on` for `push` and `pull_request` before a run is accepted. A non-match creates no run. A tag push skips the path filters. `tags` and `tags-ignore` under `pull_request` are ignored. NS-42's `poll` command is the caller that supplies that check for one repository and then exits.

## Event payload

The event is built from REST responses. The fields are `ref`,
`before`, `after`, `repository.full_name`, `repository.id`,
`repository.default_branch`, and the actor login. A pull request
also stores the number, head, and base. The head and base repository
ids are integers. A pull request whose head repository is missing, or
whose head id and base id are not equal integers, is a fork. The poll
records it and runs nothing. The full name is not the comparison.

Push:

| Field | Source |
| --- | --- |
| `ref` | The updated ref, `refs/heads/<branch>` |
| `before` | The tip this poll stored for that ref. A ref with no stored tip uses forty `0` characters. That first observation is a local poll rule. The push payload's `before` is the SHA of the most recent commit on the ref before the push |
| `after` | The tip after the poll's fetch. This is the tested commit |
| `repository.full_name` | The configured repository |
| `repository.id` | The integer id from the repository object |
| `repository.default_branch` | The `default_branch` string on that object. It is not the local clone's `HEAD` |
| `commits[-1].author.login` | The tip commit's `author.login` when that field is a non-empty string. Other commit fields are not copied |

Pull request:

| Field | Source |
| --- | --- |
| `ref` | `refs/pull/<number>/merge` |
| `before` | `base.sha`. The pull request object has no push-style `before`. Using `base.sha` is a local mapping |
| `after` | The merge commit this poll fetched. This is the tested commit |
| `repository.full_name` | The configured repository |
| `repository.id` | The integer id from the repository object |
| `repository.default_branch` | The `default_branch` string on that object |
| `number` | The pull request number |
| `pull_request.user.login` | `user.login` when that field is a non-empty string |
| `pull_request.head.sha` | The head SHA. This is the status SHA |
| `pull_request.head.ref` | The head ref name |
| `pull_request.head.repo.id` | The head repository id. Compared with the base id |
| `pull_request.head.repo.full_name` | The head repository name when the response includes one. It does not decide the fork |
| `pull_request.base.sha` | The base SHA |
| `pull_request.base.ref` | The base ref name |
| `pull_request.base.repo.id` | The base repository id |

https://docs.github.com/en/webhooks/webhook-events-and-payloads#push
https://docs.github.com/en/rest/pulls/pulls

`event_name` is `push` or `pull_request`. NS-41 evaluates `on` only
when that name is sent. `workflow_dispatch` and `schedule` stay stored
and are not evaluated.

## Reporting

Commit statuses first. Any credential that can write a commit status
can create one. Creating a check run is limited to GitHub Apps. The
credential below is that App. The posting contract is
[check runs](check-runs.md). P5 posts one check run when `--app-key`
is set. Omitting the flag keeps the commit status below.
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

Recorded on 2026-10-05. The credential is a new GitHub App. Its name
is Rookrunner-App. It is not the moonbase2090-agents App. The App
exists. Its App ID is 5201333. The installation and the private key
are still pending. This slice does not install it, sign in, or read
a key.

Repository permissions are Checks write, Commit statuses write, and
Contents read. The private key lives only in
`~/Secrets/github-app/rookrunner-app/` on the operator's Mac. The operator
passes that key's path. An installation token is minted at post time.
The key never enters this repository, a CI job, a log, or a run record.
The posting contract is [check runs](check-runs.md).

NS-40 still reads one token line from an operator file outside the
repository. This slice does not change that command. The token still
never enters the state directory, a job container, a snapshot, or a
log. Job secrets stay disabled. `GITHUB_TOKEN` stays unset. A `write`
permissions value stays rejected.

https://docs.github.com/en/rest/commits/statuses
https://docs.github.com/en/rest/checks/runs

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
| Creating check runs | GitHub Apps only | https://docs.github.com/en/rest/checks/runs | P5 |
| `paths` filter diff | 3,000 files. More than 1,000 commits, or a diff timeout, always runs | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax | NS-41 |
| Pending runs in a concurrency group | 100, with `queue: max` | same | NS-45 |

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
| 2 | a permissions value of `write` | 10 | Stays rejected. The secrets design does not change the engine ([secrets](secrets.md)) |
| 3 | `actions/upload-artifact` pinned by a full SHA | 8 | The artifact HTTP service is deferred |
| 4 | `github/codeql-action/upload-sarif` pinned by a full SHA | 6 | Same deferral. These workflows also set `security-events: write` |
| 5 | `actions/checkout@v5` | 5 | Not a 40-character SHA. All five are deploy workflows, which are out of scope |
| 6 | `runs-on: macos-14` | 2 | The planner accepts the label. macOS jobs stay deferred |
| 7 | `actions/download-artifact` pinned by a full SHA | 1 | Scorecard `release.yml`, which is out of scope |
| 8 | `with.fetch-depth: 0` on the owned checkout | 1 | Scorecard `scorecard.yml` |

Sixteen of the eighteen workflows contain `${{ }}`. The two that do
not are Rookrunner `check.yml` and prismattyc-website `deploy.yml`.
The planner stores that text. NS-44 evaluates expressions in `run`,
`env`, `with`, and step and job `name`, including mixed text.
`secrets` stays unavailable and `hashFiles` stays unsupported. NS-45
accepts `concurrency` on a workflow or job and evaluates the group
when the run is accepted. The inventory rank below is the 2026-10-04
reading, when that field failed planning. NS-46 names selected
workspace files in the artifact manifest and records one local CodeQL
SARIF file ([upload artifact](upload-artifact.md)). The capability
version stays 12. P4 accepts `actions/checkout@v` plus digits as an
owned checkout and copies ancestor history when that plan sets
`fetch-depth` to the YAML integer `0`
([checkout tag](checkout-tag.md)). Omitting `fetch-depth` keeps one
parentless synthesized commit and still excludes the original commit.
`.github/workflows/check.yml` is unchanged. P6 runs the
operator-built image when `worker --runner-image` is set and `image`
is omitted and every selected job is literal `runs-on: ubuntu-latest`
([runner image](runner-image.md)). An explicit `image` still wins. Any
other image stays a caller pin. `run_job` still requires a digest and
still does not select a default. P5 posts one check run through the
Rookrunner GitHub App when `--app-key` is set, then posts the commit
status with that installation token. Omitting `--app-key` keeps the
NS-40 token file ([check runs](check-runs.md)). The plan schema is
unchanged. P7 designs secrets, `GITHUB_TOKEN`, and `write`
permissions for owner-repository push and pull-request runs on this
one local worker ([secrets](secrets.md)). p7-mask changes the job
mask. p7-trust-gate compares pull-request repository ids and stores
the allowlist fields on the event. p7-socket-lock adds the worker
secret flags and refuses --docker-socket combined with --app-key or
--secrets unless the ~/Secrets probe exits 0. The other P7 pull
requests are not implemented. MB2090 accepted the amended answers on
2026-10-05.
The rank table above stays the 2026-10-04 reading.

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
7. `write` permissions and `GITHUB_TOKEN`. Designed in
   [secrets](secrets.md). p7-mask, p7-trust-gate, and p7-socket-lock
   are implemented.
   MB2090 accepted the amended answers on 2026-10-05. macOS jobs stay
   deferred.

## What this slice does not do

No code changes. No credential file. No poll, no status POST, and no
evaluation of `on`. NS-40, NS-41, and NS-42 do that work, citing the
sections above.
