# Copy of the captured base tree

Status: accepted, 2026-10-03. Capture copies the trees and blobs of the
single commit named by the captured `base_commit`. Commit objects,
remotes, credentials, and the `.git` directory stay excluded. This is
not a GitHub-equivalence claim.

Decision 0002 still holds: the snapshot is plain working files and a
canonical manifest, and `.git` is not copied. This design does not
rewrite that decision and does not authorize copying that directory.
Objects are read through the existing capture Git environment, not by
copying `.git/objects`.

## What capture copies

When `base_commit` is a stored commit id, capture copies the root tree
of that commit and the trees and blobs reachable from it. When
`base_commit` is null, the repository is unborn, capture stores
nothing, and capture still succeeds.

Parent commits are not walked. Tags, notes, and reflogs are not walked.
Commit objects are not copied. A commit object carries an author, a
committer, and a message. Copying one belongs to synthesizing a commit,
and that design is not started.

The copied blobs are the committed bytes of that one tree. Captured
files stay the dirty working bytes. The object copy does not reset the
workspace. The two copies may differ. That difference is intentional.

## How the objects are read

Capture adds only these commands, and only through the
capture Git environment that already exists:

- `git rev-parse --verify <base>^{tree}`
- `git ls-tree -r -t -z <base>`
- `git cat-file --batch`

`rev-parse` yields the root tree id. `ls-tree -r` lists the reachable
entries and does not list that root. `-t` adds the intermediate trees
(https://git-scm.com/docs/git-ls-tree). `cat-file --batch` yields each
object's type, size, and payload
(https://git-scm.com/docs/git-cat-file). The size is the payload
length. The newline after the payload is a record separator and is not
part of the object.

The capture environment already sets `GIT_CONFIG_NOSYSTEM`,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_OPTIONAL_LOCKS=0`,
`GIT_TERMINAL_PROMPT=0`, `GIT_NO_REPLACE_OBJECTS=1`,
`GIT_NO_LAZY_FETCH=1`, `core.fsmonitor=false`, and
`core.hooksPath=/dev/null`. Those stay. A missing object is
`SOURCE_INVALID`. It is not a fetch.
https://git-scm.com/docs/git

Capture does not add `git config --list`, `git remote -v`,
fetch, or clone. It does not copy pack files. A pack can contain
objects that this commit does not reach.

The root tree id joins the existing two-read inventory comparison. A
change between those reads is the existing `SOURCE_UNSTABLE` result.
No new error kind is added.

## Where the objects live

Capture stores loose objects in the standard loose form, so
the Git object id matches the stored bytes. The stored bytes are zlib
applied to the type, a space, the decimal size, a NUL, and the payload
(https://git-scm.com/book/en/v2/Git-Internals-Git-Objects). The path is
`objects/<two hex digits>/<remaining hex>` next to `manifest.json` and
`git.json`. The directory is not inside `files/`. It is not named
`.git`. It is not a repository. It has no `HEAD`, no config, no refs,
no hooks, and no `info/alternates`.

Each object is a regular file, mode 0600. The directories that hold
them follow the existing owner-only staging directory. Capture writes
them in the same private staging directory as `git.json`, fsyncs them,
and publishes them only with that atomic rename. A failure publishes
no partial snapshot. If the id of the stored loose object does not
match the id Git reported, the result is `SOURCE_INVALID` and nothing
is published.

The snapshot command returns `git_objects_digest`: the SHA-256 of
`protocol.canonical` applied to the sorted list of those object ids.
The field is null when the store is absent. It is not part of the
manifest digest, the plan, the run record, or describe. The worker
still stores only `snapshot_id`, `digest`, and `workflow_digest`.

`verify_snapshot` opens `manifest.json` and `files/` and does not walk
the snapshot root, so `objects/` is ignored the same way `git.json` is.
A missing store still verifies. The run path does not read the store.
`materialize_attempt` still copies only manifest entries. The workspace
receives neither `.git`, nor `git.json`, nor `objects/`.

## What stays excluded

If the base tree contains any path the existing capture exclusions
would reject, capture stores no objects and still succeeds. It does
not drop entries out of a tree. Dropping entries
would write a new tree object, and that is synthesizing. The
working-file capture is unchanged, including its record of excluded
tracked paths. A symlink in the tree is a blob. It is copied with the
other blobs and is not followed.

Capture opens `.git/objects/info/alternates` with
`O_NOFOLLOW` before `cat-file`. A missing path or an empty regular
file is accepted. A non-empty regular file is `CAPABILITY_UNSUPPORTED`
and the error names that situation. A symlink, a directory, or any
other type is `SOURCE_INVALID`. The file is not followed and is not
copied. A linked worktree stores its objects through a gitfile.
Capture classifies alternates in that common directory and still does
not follow a symlink. The same observation joins the two inventory
reads. A change between those reads is `SOURCE_UNSTABLE`.

Replace refs are not copied. The capture environment already sets
`GIT_NO_REPLACE_OBJECTS`.

A gitlink, a submodule, and an LFS pointer stay the existing capture
failures. The object copy is not reached. Capture does not
smudge and does not download LFS objects. An `ls-tree` entry whose
type is `commit` is that existing submodule rejection, and no objects
are stored. Any other type that is not `tree` or `blob` is
`SOURCE_INVALID`, and nothing is published.

These stay excluded:

- the `.git` directory, including config, hooks, packs, alternates,
  replace refs, packed-refs, and worktree pointers
- remote URLs, remote names, and remote-tracking refs
- credential helpers, tokens, and `http.extraheader`
- `user.name` and `user.email`
- commit objects, and therefore authors, committers, and messages
- tags, notes, and reflogs
- `github.token` and `GITHUB_TOKEN`

`github.sha`, `github.ref`, `github.actor`, `github.repository`, and
`github.token` stay uninvented. A missing `github` property stays an
empty string. Checkout behavior is unchanged
([checkout](checkout.md)). The capability version stays 9. No new
error kind, capability string, or protocol field is added to the plan,
the run record, or describe.

## Limits

This copy adds no numeric limit and no object-count cap. GitHub's
Actions limits page does not publish an object-count cap
(https://docs.github.com/en/actions/reference/limits).

Existing capture limits stay, and they are not an object-count cap:

- `MAX_FILES` is 10000 working files.
- `MAX_BYTES` is `256 * 1024 * 1024` captured file bytes.
- `MAX_FILE_BYTES` is `64 * 1024 * 1024`. A blob payload over that size
  fails with the existing `SOURCE_LIMIT` kind, and nothing is
  published.

Object-store bytes live under the state directory, so the existing
disk budget counts them. The default budget stays
`10 * 1024 * 1024 * 1024` bytes.

## Acceptance

- The trees and blobs of the captured base commit are stored as loose
  objects. The commit object is not stored.
- An unborn repository, and a base tree that contains an excluded path,
  store no objects. Capture still succeeds.
- Dirty captured files stay the working bytes. The copied blob stays
  the committed bytes.
- A non-empty alternates file fails capture and publishes nothing.
- The workspace has no `.git`, no `git.json`, and no `objects`.
- Synthesizing a commit, creating a `.git` directory, and filling the
  `github` context remain unstarted.

## Out of scope

Synthesizing a commit, creating a `.git` directory, fetching,
persisting a credential, and filling the `github` context each still
need their own design. A later `run` step still has no Git repository.
This design does not authorize those behaviors.
