# Owned Git directory

Status: design, 2026-10-03. This slice does not write a `.git`
directory. The following PR may write one in the attempt workspace.
It does not copy the original `.git`. Remotes, credentials, and the
original commit stay excluded. This is not a GitHub-equivalence claim.

Decision 0002 still holds: the snapshot is plain working files and a
canonical manifest, and the original `.git` is not copied. This design
does not rewrite that decision. The directory is assembled from the
object store and `git.json` that capture already wrote.

## What the following PR may write

When the snapshot contains an `objects` directory,
`materialize_attempt` may add one `.git` directory in the attempt
workspace. When that store is absent, the following PR writes no
`.git`. An unborn repository and a base tree that contains an excluded
path already have no store. Those attempts still succeed, and they
still have no `.git`. An old snapshot without the store still
materializes, and it still has no `.git`.

The snapshot root gains no `.git`. `git.json` is not copied into the
workspace. `objects/` is not created at the workspace root. The
original checkout is not opened. The checkout step still does not
create `.git` and does not delete it ([checkout](checkout.md)).

The following PR reads the store and the sibling before it creates the
workspace. A missing sibling, a sibling `read_git_metadata` rejects, or
a null `base_commit` while the store is present is the existing
`SNAPSHOT_INVALID`. A `head` whose path is not safely inside `.git`
is the same kind. That includes an empty segment and a segment of `.`
or `..`. No workspace is created.

The store must contain exactly one commit, and every loose object must
hash to its path in the repository's recorded format. The commit
payload must be the fixed payload from
[synthesized commit](synthesized-commit.md), and its tree must be a
tree object in the same store. Any other store is the existing
`ATTEMPT_FAILED`. No workspace is created. The original commit is not
accepted in place of that payload. The following PR does not fetch a
replacement object.

`.git` is a real directory, mode 0700. It is not a gitfile and it is
not a symlink. The following PR copies each loose object byte for byte
into `.git/objects/<two hex digits>/<remaining hex>`. Those files are
mode 0600. The directories that hold them are mode 0700. It does not
copy `pack`, `info`, or `alternates`.

`refs` is created, mode 0700, even when HEAD is detached. Git does not
treat the directory as a repository without it
(https://git-scm.com/docs/gitrepository-layout).

When `head` is null, `.git/HEAD` is the synthesized id and a newline.
No branch file is written. When `head` is `refs/heads/<name>`,
`.git/HEAD` is `ref: refs/heads/<name>` and a newline, and
`.git/refs/heads/<name>` is the synthesized id and a newline. The name
comes from `git.json`. The original `base_commit` is not written into
`HEAD` or into a ref. `HEAD`, the ref file, and `config` are mode 0600.

The config is fixed ASCII. It is not read from the source repository,
from Git configuration, or from the host. `sha1` is:

```
[core]
	repositoryformatversion = 0
	filemode = true
	bare = false
	logallrefupdates = false
	ignorecase = false
```

`sha256` is the same, with `repositoryformatversion = 1`, plus:

```
[extensions]
	objectformat = sha256
```

https://git-scm.com/docs/git-config

Each of those files ends in a newline. The config sets no `user.name`,
no `user.email`, no remote, no credential helper, and no
`http.extraheader`. Reflogs stay off. Case folding stays off, because
capture treats paths as case-sensitive. Host keys such as
`precomposeunicode` are not copied.

After those files exist, the following PR may run one command,
`git read-tree HEAD`, with its working directory set to the attempt
workspace. The command uses the capture Git environment:
`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL` pointing at `/dev/null`,
`GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`,
`GIT_NO_REPLACE_OBJECTS=1`, `GIT_NO_LAZY_FETCH=1`,
`core.fsmonitor=false`, and `core.hooksPath=/dev/null`. It reads the
tree of `HEAD` into the index and does not update the work tree
(https://git-scm.com/docs/git-read-tree). A failure removes the
workspace and is `ATTEMPT_FAILED`.

The index then matches the synthesized commit. Captured files stay the
dirty working bytes. A clean tracked file is unchanged. A dirty tracked
file remains a work-tree modification. An explicitly included untracked
file stays untracked. The following PR does not run `git checkout`,
`git reset`, `git add`, `git commit`, `git status`, or `git init`.

No hook files are written. No sample hooks are written. The command
above sets `hooksPath` only for itself. The stored config does not set
`hooksPath`.

A failure after the workspace exists removes the partial workspace,
which is the existing `materialize_attempt` rule. The snapshot is
unchanged.

The submission and the run start already reserve `usage(snapshot)` for
the attempt workspace. That total includes the object store. The copy
into `.git/objects` uses that reserve. The following PR does not change
the reserve formula and does not add a byte cap.

`written_files` skips `.git` and every path under it, and it does not
follow a symlink of that name. Object bytes are not artifacts. Files
outside `.git` are unchanged.

`verify_snapshot` still does not walk the snapshot root and does not
require `.git`. The run record, the plan, and describe are unchanged.
The capability version stays 9. No new error kind, capability string,
or protocol field is added.

## What stays excluded

These stay excluded:

- the original `.git` directory, including its `HEAD`, config, refs,
  hooks, packs, alternates, replace refs, and reflogs
- the original commit object, its parents, its author, its committer,
  its timestamp, and its message
- tags, notes, and remote-tracking refs
- remote URLs, remote names, and credential helpers
- `user.name` and `user.email`
- `github.token` and `GITHUB_TOKEN`

`github.sha`, `github.ref`, `github.actor`, `github.repository`, and
`github.token` stay uninvented. The synthesized id is not
`github.sha`. A missing `github` property stays an empty string.
The checkout step still does not replace captured files.

## Acceptance for this design

- A written design names the one `.git` directory a later PR may write
  in the attempt workspace.
- That directory points `HEAD` at the synthesized commit. It does not
  copy the original `.git` or the original commit.
- Filling the `github` context remains unstarted.

## Out of scope

Filling the `github` context, fetching, and persisting a credential
each still need their own design. This design does not authorize those
behaviors. The following PR may write the directory named above. Until
that PR, a `run` step still has no Git repository.
