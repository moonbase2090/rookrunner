# Checkout by major tag and fetch-depth 0

Status: design. This slice writes this document and does not change the
engine. The capability version stays 12. A version 11 plan is not
migrated. `.github/workflows/check.yml` is unchanged. This is not a
GitHub-equivalence claim.

The implementation follows this document. It is the next slice.

## Why this slice is a design

The owned checkout accepts `actions/checkout@v4` and
`actions/checkout@` plus 40 lowercase hexadecimal characters
([checkout](checkout.md)). The tag is not fetched. The step does not
replace captured files, read an action file, or run the action. Other
tags stay rejected, including `@v5`. `fetch-depth` stays rejected.

The owner-CI inventory ranks `actions/checkout@v5` on deploy workflows
and `fetch-depth: 0` on one Scorecard workflow
([owner CI](owner-ci.md)). Deploy workflows stay blocked by other
fields. Scorecard sets `fetch-depth` because it reads commit history.
The snapshot stores the trees and blobs of one commit and one
parentless synthesized commit
([git objects](git-objects.md),
[synthesized commit](synthesized-commit.md)).
The attempt `.git` requires that single commit
([Git directory](git-directory.md)). Parent commits are not walked.
The original commit object stays excluded on that path.

`fetch-depth: 0` on `actions/checkout` means all history for all
branches and tags
(https://github.com/actions/checkout). The default is 1. This worker
does not fetch. History has to be copied from the local repository
into the snapshot before the attempt starts. Doing that on every
capture would copy author, committer, timestamp, and message bytes
that the accepted object-store designs exclude. This document keeps
that exclusion for every capture that does not ask for history, and
states the one path that copies it.

## Uses

A major tag is `actions/checkout@v` plus one or more digits and nothing
else. `v4`, `v5`, `v3`, and `v10` match. The match is the whole `uses`
string. The planner records that string and sets `checkout` to
`captured`. It does not resolve the tag, fetch a ref, read an action
file, or run a program. A tag that GitHub does not publish is still
this step. The string is stored as written.

`actions/checkout@v4` stays the step it is today. A workflow that
already plans keeps the same plan shape when `fetch-depth` is absent.
The 40-character lowercase SHA form is unchanged. The SHA is stored
and is not verified.

These stay `CAPABILITY_UNSUPPORTED` and name the `uses` field:

- `actions/checkout` with no `@`
- a dotted tag, such as `v4.2.2` or `v5.0.0`
- `@v` with no digits
- an uppercase `V`
- a branch, a short SHA, or any other action

A checkout `uses` inside a local composite stays rejected and names
that field. A remote composite's steps stay `run` steps. This slice
does not fetch those actions.

## fetch-depth

`fetch-depth` joins `clean` and `persist-credentials` as an accepted
`with` key. The only accepted value is the YAML integer `0`.

An omitted `fetch-depth` leaves capture and `.git` as they are. It
does not mean a written `1`. GitHub's default of 1 is the single
commit this worker already stores, and a written integer other than
`0`, including `1`, is `CAPABILITY_UNSUPPORTED` and names
`with.fetch-depth`. A boolean, a string, a float, or any other node
is `WORKFLOW_INVALID` and names that field. The value is not
evaluated. `hashFiles` stays unsupported.

`clean: false` and `persist-credentials: false` may appear beside
`fetch-depth: 0`. `clean: true`, `persist-credentials: true`, and
every other `with` key stay `CAPABILITY_UNSUPPORTED` and name the
field, as [checkout](checkout.md) already says.

## When history is stored

History is stored only when the accepted plan contains an owned
checkout step with `fetch-depth: 0`. That includes a checkout on a job
the selected job needs, and a checkout in a called workflow that is
part of the plan. A step whose `if` is constant `false` still counts.
The condition can also be an expression, and capture cannot know the
later result. The attempt `.git` is written before steps run.

A plan that contains no such step stores the same objects as today.
The synthesized payload stays parentless. `git_objects_digest` for
that capture stays the same. The original commit stays excluded.

The snapshot is published by the existing atomic rename of the staging
directory. History objects are written into that staging store before
the rename. A rejected workflow leaves no snapshot and no run. A
published snapshot is not modified later.

The history decision uses the same planner as acceptance. One plan is
the accepted plan.

## What history contains

The copy is the ancestor closure of `base_commit`. It includes the
original commit object for `base_commit`, every parent reachable from
it, and the trees and blobs those commits reach that are not already
in the store. All parents are walked. The walk is not first-parent
only.

The copy omits other branches, tags, notes, reflogs, and
remote-tracking refs. GitHub's `fetch-depth: 0` fetches all branches
and tags. This slice does not. Scorecard's history of the captured
commit is the closure above.

Commit objects are copied as stored. The author, committer, timestamp,
parents, and message are the original bytes. Those bytes stay in the
state directory and count against the existing disk budget. They are
not redacted. A commit message that holds a secret is stored. That is
limited to this history path. The default capture still excludes the
original commit.

Capture reads the closure through the existing capture Git environment,
adding `git rev-list --objects <base_commit>` and reading new objects
with the existing `git cat-file --batch`. `GIT_NO_LAZY_FETCH` stays
set. Capture does not add `git fetch`, `git clone`, `git unshallow`,
or `git config`. A missing object, including a parent absent from a
shallow repository, is `SOURCE_INVALID`. It is not a fetch. No partial
snapshot is published.

An unborn repository has no `base_commit`. A plan that wants history
then fails capture with `SOURCE_INVALID`. A plan that fails for
another reason keeps that plan error.

No new quota is added. The existing disk budget counts the new
objects. A capture that would exceed it fails with the existing
storage error and leaves no run.

## The synthesized commit in history mode

The synthesized commit gains one `parent` line, the `base_commit` id.
The tree, author, committer, timestamp, and message stay the fixed
payload from [synthesized commit](synthesized-commit.md). The object
id changes only in history mode, because the payload changed. The
parentless object is not stored beside it.

`HEAD` in the attempt `.git` remains that synthesized id. `base_commit`
is not written into `HEAD` or into a branch. `github.sha` is unchanged:
it is `base_commit` only for a clean capture with an empty `included`
list, and it is unset when the capture is dirty. No tag refs are
written.

`git log` from `HEAD` walks the synthesized commit and then the copied
ancestors. On a dirty capture the first commit is still `captured tree`,
and its parent is the real base commit.

## Materialize

A store with exactly one parentless synthesized commit is unchanged.
`materialize_attempt` still rejects any other commit in that store.

A history store may contain more than one commit. It contains exactly
one synthesized commit, with the parent line and the captured root
tree. `base_commit` is present as a commit object. Every parent id
named by a stored commit is present in the store. Any other shape is
the existing `ATTEMPT_FAILED`, and no workspace is created.

Objects stay loose. Files are mode `0600`. Directories are mode
`0700`. Packs, alternates, remotes, and credentials are not written.
The checkout step still does not create `.git` and does not delete it.

## What the step does at run time

The step does not start a process, fetch, or modify the workspace.
History is already in `.git` when materialize finished. A false `if`
skips the step and leaves that directory. `shell`, `env`,
`working-directory`, and `timeout-minutes` start no process. The step
publishes no outputs and has no post step. The snapshot digest is
unchanged by the step.

The capability version stays 12. `fetch-depth` is an optional key on
the existing checkout step. A version 12 plan from before this slice
has no such key and still runs. A version 11 plan is not migrated. No
protocol field, error kind, or schema entry is added.

## What the implementation proves

1. `actions/checkout@v5` plans as an owned checkout of the captured
   files. A dirty file is still there after the step. The step does
   not open a network connection.
2. `actions/checkout@v4` with no `fetch-depth` keeps today's plan and
   today's single parentless commit.
3. `actions/checkout@v4.2.2`, `actions/checkout@V5`, and
   `actions/checkout` with no `@` are `CAPABILITY_UNSUPPORTED` and
   name the `uses` field.
4. A checkout `uses` inside a local composite stays rejected.
5. `fetch-depth: 0` on a repository with a parent stores the original
   base commit and that parent. `git log` from `HEAD` shows the
   synthesized commit, then those commits. No fetch runs.
6. A workflow with no `fetch-depth` stores one commit, the parentless
   synthesized payload.
7. `fetch-depth: 1` is `CAPABILITY_UNSUPPORTED` and names the field.
   The string `0` is `WORKFLOW_INVALID` and names the field.
8. An unborn repository with `fetch-depth: 0` is `SOURCE_INVALID` and
   publishes no snapshot.
9. A missing parent is `SOURCE_INVALID` and is not fetched.
10. The capability version stays 12. `.github/workflows/check.yml` is
    unchanged. `security-events: write` and `actions: write` still fail
    planning.

## What this slice does not do

No engine change. No fetch and no unshallow. No copy of other branches
or tags. No credential and no token. `GITHUB_TOKEN` stays unset.
`actions/setup-node` stays rejected. Deploy workflows that need it, or
that need a `write` permission, stay blocked. P5, P6, and P7 stay
unstarted. The default capture still excludes the original commit.
Decision 0002 still holds: the original `.git` directory is not copied.
