# Checkout of captured files

Status: accepted, 2026-10-03. The planner accepts
`uses: actions/checkout@v4` as an owned step that does not replace the
captured files. It does not read an action file, fetch a ref, or run the
JavaScript action. This is not a GitHub-equivalence claim.

The workspace already holds the captured working files before the first
step. Those bytes include dirty files and explicitly included untracked
files. `actions/checkout` fetches a commit and, by default, persists a
token and resets the work tree
(https://github.com/actions/checkout). That reset would drop the
captured dirty bytes. This engine does not do that.

## What is accepted

The only accepted `uses` string is `actions/checkout@v4`. The planner
records it as an owned checkout of the captured files. It does not read
an action file, does not fetch a ref, and does not run the JavaScript
action.

`with` may be omitted. The only accepted keys are:

| Key | Accepted value |
| --- | --- |
| `clean` | boolean `false` |
| `persist-credentials` | boolean `false` |

An omitted `clean` does not mean the upstream default `true`. An
omitted `persist-credentials` does not mean the upstream default
`true`. Both omissions mean the step does not change captured files and
does not write a credential. That is an intentional difference.

`clean: true` and `persist-credentials: true` are
`CAPABILITY_UNSUPPORTED` and name the field. A value that is not a
boolean is `WORKFLOW_INVALID`. Any other `with` key is
`CAPABILITY_UNSUPPORTED` and names the field. These stay rejected:
`token`, `ssh-key`, `ssh-known-hosts`, `ssh-strict`, `ssh-user`,
`repository`, `ref`, `path`, `fetch-depth`, `fetch-tags`, `submodules`,
`lfs`, `sparse-checkout`, `sparse-checkout-cone-mode`, `filter`,
`set-safe-directory`, `github-server-url`, and `show-progress`.

Every other `uses` string stays rejected. That includes
`actions/checkout` without `@`, `actions/checkout@v3`,
`actions/checkout@v5`, `actions/checkout@v7`, and any other
`actions/...` reference. JavaScript and Docker actions stay rejected.
Local composite `./` and `$/` uses are unchanged.

The step may still carry the existing step keys: `id`, `name`, `if`,
`shell`, `working-directory`, `env`, and `timeout-minutes`. This
acceptance adds no step key. A false `if` skips the step. `shell`,
`env`, `working-directory`, and `timeout-minutes` do not start a
process, because the step does no work.

## What the step does

The plan stores `uses` as `actions/checkout@v4` and `checkout` as
`captured`. It does not store an action path, an action digest, or
inner steps. The capability version is 9. The runner accepts only that
version. In-flight plans from version 8 are not migrated. Every plan
digest changes because the version field changes.

When the step runs, the runner does not start a process, does not
modify the workspace, does not create `.git`, does not read `git.json`,
and does not contact a network. The step succeeds with exit code 0. It
publishes no outputs and has no post step. The snapshot digest is
unchanged. Describe capabilities are unchanged. The compatibility note
is not lengthened. No error kind, protocol field, or numeric limit is
added.

`github.sha`, `github.ref`, `github.actor`, `github.repository`, and
`github.token` stay uninvented. No `GITHUB_TOKEN` is created.

## Proof

1. A first step `uses: actions/checkout@v4` leaves a captured dirty
   file in place. A later `run` step still sees those bytes. The
   snapshot digest is unchanged.
2. The same holds for `clean: false` and `persist-credentials: false`.
3. `clean: true`, `persist-credentials: true`, `token`, `repository`,
   `ref`, `fetch-depth`, `ssh-key`, and `submodules` each fail planning
   with `CAPABILITY_UNSUPPORTED` and create no run.
4. `actions/checkout@v7` and `actions/checkout` without `@` stay
   rejected.
5. After the step, the workspace has no `.git` and no `git.json`.
6. `github.sha` and `github.token` are still empty.
7. The capability version is 9. A workflow of only `run` steps still
   runs.

## Out of scope

The trees and blobs of the captured base commit are copied
([git objects](git-objects.md)). Synthesizing a
commit, creating a `.git` directory, fetching, persisting a credential,
and filling the `github` context each need their own design. A later
`run` step still has no Git repository. This design does not authorize
those behaviors.
