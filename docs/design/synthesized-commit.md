# Synthesized commit for the captured tree

Status: design, 2026-10-03. This slice does not write a commit object.
The following PR may write one new commit whose tree is the captured
base tree. It does not copy the original commit. Remotes, credentials,
and the `.git` directory stay excluded. This is not a
GitHub-equivalence claim.

Decision 0002 still holds: the snapshot is plain working files and a
canonical manifest, and `.git` is not copied. This design does not
rewrite that decision and does not authorize copying that directory.

## What the following PR may write

When the object store is present, the following PR may add one loose
commit object. Its tree is the root tree already stored for
`base_commit`. When the store is absent, the following PR writes no
commit. An unborn repository and a base tree that contains an excluded
path already store nothing. Those captures still succeed, and they
still store no commit.

The original commit object is not read and is not copied. A commit
object carries an author, a committer, a timestamp, parents, and a
message. Copying it would copy those fields. The stored `base_commit`
stays the original commit id. `git.json` stays the same five fields.
The synthesized id does not replace `base_commit`.

The copied blobs stay the committed bytes. Captured files stay the
dirty working bytes. The synthesized commit points at the committed
tree, not at a new tree of the dirty files. The following PR does not
reset the workspace and does not write a second tree. The two copies
may differ. That difference is intentional.

## The commit payload

The payload is ASCII. It has no parent line. The name, email,
timestamp, timezone, and message are fixed. They are not read from the
repository, from the original commit, from Git configuration, or from
the process environment.

```
tree <root-tree-id>
author Rookrunner <rookrunner@example.invalid> 0 +0000
committer Rookrunner <rookrunner@example.invalid> 0 +0000

captured tree
```

Each header line ends in a newline. A blank line separates the headers
from the message. The message is the single line `captured tree` and
ends in a newline. The email domain is the reserved `.invalid` domain
(https://www.rfc-editor.org/rfc/rfc2606). The timestamp is Unix time 0
and the timezone is `+0000`. A clock reading would change the object
id between captures of the same tree. The original commit's timestamp
is not copied.

The object id is the repository's existing object format, SHA-1 or
SHA-256, over `commit`, a space, the decimal size, a NUL, and that
payload. The following PR stores it in the same loose form as the
trees and blobs
(https://git-scm.com/book/en/v2/Git-Internals-Git-Objects). The path is
`objects/<two hex digits>/<remaining hex>` in the existing store. The
file is mode 0600. It is written in the same private staging directory,
fsynced, and published only with that atomic rename. A failure
publishes no partial snapshot. If the stored id does not match the
computed id, the result is `SOURCE_INVALID` and nothing is published.

The following PR adds no Git command. It does not run `git commit`,
`git commit-tree`, `git config`, `git log`, `git var`, or
`git cat-file` on the original commit. It does not add the base commit
id to the existing cat-file batch. The capture Git environment stays as it is. A missing
tree or blob is still `SOURCE_INVALID` and is not a fetch.

The synthesized id is included in the sorted list already hashed for
`git_objects_digest`. That field stays null when the store is absent.
No new snapshot field is added. The digest is not part of the manifest
digest, the plan, the run record, or describe. The worker still stores
only `snapshot_id`, `digest`, and `workflow_digest`.

The root tree id is already compared across the two inventory reads.
The commit id is a pure function of that tree id and the fixed
payload, so the following PR computes it once after those reads agree.
A tree change remains the existing `SOURCE_UNSTABLE` result.

## What stays excluded

These stay excluded:

- the original commit object, its parents, its author, its committer,
  its timestamp, and its message
- tags, notes, and reflogs
- the `.git` directory, including `HEAD`, config, refs, hooks, packs,
  alternates, and replace refs
- remote URLs, remote names, and remote-tracking refs
- credential helpers, tokens, and `http.extraheader`
- `user.name` and `user.email`
- `github.token` and `GITHUB_TOKEN`

`github.sha`, `github.ref`, `github.actor`, `github.repository`, and
`github.token` stay uninvented. The synthesized id is not
`github.sha`. A missing `github` property stays an empty string.
Checkout behavior is unchanged ([checkout](checkout.md)). The
capability version stays 9. The plan does not change. No new error
kind, capability string, or protocol field is added to the plan, the
run record, or describe. No numeric limit is added.

`verify_snapshot` still does not walk the snapshot root. A missing
store still verifies. The run path does not read the store.
`materialize_attempt` still copies only manifest entries. The
workspace receives neither `.git`, nor `git.json`, nor `objects/`.

## Acceptance for this design

- A written design names the one commit a later PR may write.
- That commit does not copy the original author, committer, message,
  timestamp, or parents.
- Creating a `.git` directory and filling the `github` context remain
  unstarted.

## Out of scope

Creating a `.git` directory, fetching, persisting a credential, and
filling the `github` context each still need their own design. A later
`run` step still has no Git repository. This design does not authorize
those behaviors.
