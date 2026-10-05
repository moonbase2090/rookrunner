# Checkout of captured files

Status: accepted, 2026-10-03. The planner accepts
`uses: actions/checkout@v4` as an owned step that does not replace the
captured files. NS-32 also accepts `actions/checkout` pinned by 40
lowercase hexadecimal characters. That SHA is stored and is not
verified. The step does not read an action file, fetch a ref, or run the
JavaScript action. This is not a GitHub-equivalence claim.

`actions/checkout@v` plus one or more digits is the same owned checkout.
`fetch-depth` accepts only the YAML integer `0`. Both are implemented in
[checkout tag](checkout-tag.md). The capability version stays 12.
`.github/workflows/check.yml` is unchanged. Dotted tags, a bare
`actions/checkout`, and every other `fetch-depth` value stay rejected.

The workspace already holds the captured working files before the first
step. Those bytes include dirty files and explicitly included untracked
files. `actions/checkout` fetches a commit and, by default, persists a
token and resets the work tree
(https://github.com/actions/checkout). That reset would drop the
captured dirty bytes. This engine does not do that.

## What is accepted

The accepted `uses` strings are `actions/checkout@v4`,
`actions/checkout@v` plus one or more digits, and
`actions/checkout@` followed by 40 lowercase hexadecimal characters.
The planner records that string as an owned checkout of the captured
files and sets `checkout` to `captured`. It does not read an action
file, does not fetch a ref, and does not run the JavaScript action.
The SHA is not verified against a remote.

Short SHAs, branches, and dotted tags stay rejected, including a
39-character SHA, `@main`, and `@v4.2.2`. A major tag such as `@v5`
is the owned checkout ([checkout tag](checkout-tag.md)).

`with` may be omitted. The only accepted keys are:

| Key | Accepted value |
| --- | --- |
| `clean` | boolean `false` |
| `persist-credentials` | boolean `false` |
| `fetch-depth` | YAML integer `0` |

An omitted `clean` does not mean the upstream default `true`. An
omitted `persist-credentials` does not mean the upstream default
`true`. Both omissions mean the step does not change captured files and
does not write a credential. That is an intentional difference. An
omitted `fetch-depth` does not mean the upstream default `1`. It leaves
the parentless synthesized commit and still excludes the original
commit.

`clean: true` and `persist-credentials: true` are
`CAPABILITY_UNSUPPORTED` and name the field. A `clean` or
`persist-credentials` value that is not a boolean is
`WORKFLOW_INVALID`. A `fetch-depth` that is not an integer is
`WORKFLOW_INVALID` and names the field. The value is not evaluated. A
written integer other than `0` is `CAPABILITY_UNSUPPORTED` and names
`with.fetch-depth`. Any other `with` key is
`CAPABILITY_UNSUPPORTED` and names the field. These stay rejected:
`token`, `ssh-key`, `ssh-known-hosts`, `ssh-strict`, `ssh-user`,
`repository`, `ref`, `path`, `fetch-tags`, `submodules`,
`lfs`, `sparse-checkout`, `sparse-checkout-cone-mode`, `filter`,
`set-safe-directory`, `github-server-url`, and `show-progress`.

Every other `uses` string stays rejected. That includes
`actions/checkout` without `@`, a dotted tag, an uppercase `V`, and any
other `actions/...` reference. `actions/checkout@v3`,
`actions/checkout@v5`, and `actions/checkout@v7` are the owned checkout
([checkout tag](checkout-tag.md)). JavaScript and Docker actions stay
rejected.
Local composite `./` and `$/` uses are unchanged.

The step may still carry the existing step keys: `id`, `name`, `if`,
`shell`, `working-directory`, `env`, and `timeout-minutes`. This
acceptance adds no step key. A false `if` skips the step. `shell`,
`env`, `working-directory`, and `timeout-minutes` do not start a
process, because the step does no work.

## What the step does

The plan stores the accepted `uses` string and `checkout` as
`captured`. It does not store an action path, an action digest, or
inner steps. The capability version is 11. The runner accepts only that
version. Plans from version 10 are not migrated. Every plan
digest changes because the version field changes.

When the step runs, the runner does not start a process, does not
modify the workspace, does not create `.git`, does not delete a `.git`
that materialize already wrote, does not read `git.json`,
and does not contact a network. The step succeeds with exit code 0. It
publishes no outputs and has no post step. The snapshot digest is
unchanged. Describe capabilities are unchanged. The compatibility note
is not lengthened. No error kind, protocol field, or numeric limit is
added.

The checkout step does not invent `github.sha`. A clean capture sets
`github.sha` to the manifest base commit, as NS-33 describes. A dirty
capture, or a capture with a non-empty `included` list, leaves it
unset. `github.ref`, `github.actor`, `github.repository`, and
`github.token` stay unset. No `GITHUB_TOKEN` is created.

## Proof

1. A first step `uses: actions/checkout@v4` leaves a captured dirty
   file in place. A later `run` step still sees those bytes. The
   snapshot digest is unchanged.
2. The same holds for `clean: false` and `persist-credentials: false`.
3. `clean: true`, `persist-credentials: true`, `token`, `repository`,
   `ref`, `ssh-key`, and `submodules` each fail planning
   with `CAPABILITY_UNSUPPORTED` and create no run. `fetch-depth`
   other than the YAML integer `0` fails the same way and names the
   field. The integer `0` is specified in
   [checkout tag](checkout-tag.md).
4. `actions/checkout` without `@` stays rejected. A major tag such as
   `actions/checkout@v7` is the owned checkout.
5. After the step, the workspace has no `git.json` and no `objects`.
   When the store is present, `.git` is already there and the step
   leaves it. When the store is absent, `.git` is absent.
6. On a dirty capture, `github.sha` and `github.token` are still empty.
   The checkout step does not set them. A clean capture sets
   `github.sha` from the manifest base commit.
7. The capability version is 9. A workflow of only `run` steps still
   runs.

## Out of scope

The trees and blobs of the captured base commit are copied
([git objects](git-objects.md)). One synthesized commit for that tree
is stored ([synthesized commit](synthesized-commit.md)). When the store is present, materialize writes an owned `.git`
([Git directory](git-directory.md)). The checkout step does not create
it. `github.sha` follows the NS-33 rule. Fetching an
action and persisting a credential still need their implementing
slices. This design does not authorize those behaviors.
