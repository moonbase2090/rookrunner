# Sanitized Git metadata

Status: design, 2026-10-03. Capture copies the fields below into a
sibling `git.json`. Checkout actions remain an explicit rejection. This
is not a GitHub-equivalence claim.

Decision 0002 still holds for the implemented capture: the snapshot is
plain working files and a canonical manifest, and `.git` is not copied.
This design is the subsequent step that decision names. It does not
authorize copying the `.git` directory.

## Where the metadata lives

The manifest stays `format_version` 1. Its digest stays the SHA-256 of
the exact manifest bytes. Those bytes stay the identity of the submitted
working files.

Capture writes one sibling, `git.json`, next to
`manifest.json` in the same private staging directory, then fsync it
before the atomic rename. The file mode is 0600. It is a regular file.
The sibling is not a manifest field, so verification of an existing
snapshot digest does not read it and does not require it. A snapshot
that has no sibling still verifies and still materializes.

The sibling has its own SHA-256, returned on the snapshot command as
`git_metadata_digest`. That field is not part of the manifest digest,
the plan, the run record, or describe. No capability string is added.
The capability version stays 8. No plan digest changes.

`materialize_attempt` keeps copying only manifest entries. The attempt
workspace does not receive `git.json` and does not receive a `.git`
directory. A job step that runs Git still has no repository. The disk
budget already counts files under the state directory. This design adds
no byte cap and no other numeric limit.

## Fields capture copies

The sibling is canonical JSON with exactly these keys:

| Field | Value |
| --- | --- |
| `format_version` | `1` |
| `base_commit` | The manifest's `base_commit`, unchanged |
| `dirty` | The manifest's `dirty`, unchanged |
| `git_object_format` | The manifest's `git_object_format`, unchanged |
| `head` | `refs/heads/<name>`, or null |

`base_commit`, `dirty`, and `git_object_format` are copies of the values
the manifest already stores. They are not read a second time.
`base_commit` is stored only when it is null or lowercase hex whose
length matches the object format: 40 characters for `sha1`, 64 for
`sha256`. Any other value fails capture with the existing
`SOURCE_INVALID` kind. An unborn repository keeps a null base, as it
does today.

`head` is the only new fact. Capture runs `git symbolic-ref --quiet HEAD`
and `git check-ref-format` through the capture Git environment that
already discards inherited configuration, hooks, prompts, and network
fetch. Exit 0 is stored only when the output is a single line,
`git check-ref-format` accepts it
(https://git-scm.com/docs/git-check-ref-format), and it begins with
`refs/heads/`. The name must not contain `@`. Exit 1 with empty output
stores null. That is a detached HEAD. An unborn repository still has a
symbolic ref; its base commit is null, so the sibling stores `head` null.
A name that is not an accepted local branch stores null rather than the
ref. Any other `symbolic-ref` result is `SOURCE_INVALID`. A null `head`
does not fail a capture that succeeds today.

A change to HEAD or to that symbolic ref between the two inventory
reads is the existing `SOURCE_UNSTABLE` result. No new error kind is
added.

## What stays excluded

Capture does not copy the items below. The base commit's trees and
blobs are copied separately ([git objects](git-objects.md)).

- `.git` itself, including config, hooks, objects, packs, alternates,
  replace refs, packed-refs, and worktree pointers
- remote URLs, remote names, and remote-tracking refs
- credential helpers, tokens, and `http.extraheader`
- `user.name` and `user.email`, from configuration or from a commit
- commit objects, and therefore authors, committers, and messages
- tags, notes, and reflogs
- `github.token` or `GITHUB_TOKEN`

`github.token` is a credential the Actions runner sets for a job step
(https://docs.github.com/en/actions/reference/workflows-and-actions/contexts).
This engine does not create it. `github.sha` is the commit that
triggered a GitHub workflow run. The stored `base_commit` is not that
property, and this design does not invent `github.sha`, `github.ref`,
`github.actor`, or `github.repository`. A missing `github` property
stays an empty string.

`actions/checkout` fetches the ref that triggered the run and, by
default, persists a token for later Git commands
(https://github.com/actions/checkout). Its default clean step resets
the work tree to HEAD. That would drop the dirty and untracked bytes
this capture stores. The owned checkout accepts only
`uses: actions/checkout@v4` and does not do that reset, does not fetch,
and does not persist a credential
([checkout](checkout.md)). Other checkout `uses` strings, including
`actions/checkout` without `@`, stay `CAPABILITY_UNSUPPORTED`.
Capture does not fetch, does not persist a credential, and does not
reset the workspace.

No capture command may print configuration or remotes.
`git config --list` and `git remote -v` are not allowed.
`symbolic-ref` and `check-ref-format` read the branch name. The base
tree is read with `rev-parse`, `ls-tree`, and `cat-file`
([git objects](git-objects.md)).

## Checkout fixture

Tests prove the captured digest against a checkout, and still reject
checkout actions.

1. Capture a repository that has a local branch. Record the manifest
   digest and `git_metadata_digest`.
2. After the reply, edit that checkout: add a remote whose URL is
   `https://example.invalid/fixture.git`, and write a fixture-only
   credential string under `.git`. The test identity stays the existing
   fixture name and `fixture@example.invalid`.
3. Read the snapshot again. The manifest digest is unchanged. `git.json`
   does not contain the remote URL, the credential string, `user.email`,
   or `fixture@example.invalid`. Its `base_commit`, `dirty`, and
   `git_object_format` equal the manifest. `head` is `refs/heads/` plus
   the local branch name.
4. A detached HEAD and an unborn repository store `head` null and still
   capture.
5. A remote-tracking symbolic ref stores `head` null rather than the
   ref.
6. `verify_snapshot` accepts the digest when `git.json` is absent,
   present, or replaced with other bytes. A separate reader rejects a
   sibling that is not the canonical five-field object or that
   disagrees with the manifest. The run path does not call that reader.
7. `uses: actions/checkout@v4` plans as an owned checkout of the captured
   files and does not read `git.json`. `actions/checkout@v7` produces no
   plan (`CAPABILITY_UNSUPPORTED`).
8. Materializing the attempt leaves the workspace without `.git` and
   without `git.json`.

An old snapshot without `git.json` still verifies, still materializes,
and reports no metadata digest.

## Out of scope

The trees and blobs of the captured base commit are copied
([git objects](git-objects.md)). Commit objects stay excluded.
Synthesizing a commit, filling the `github` context, and publishing a
GitHub-equivalence claim are later work. Each of those still needs its
own design. The owned checkout in [checkout](checkout.md) does not
authorize them.
