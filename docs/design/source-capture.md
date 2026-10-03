# M2 source capture: first implemented slice

Status: implemented preparatory capture, 2026-09-23. M2 is in progress.
This component does not submit a run, interpret YAML, execute workflows, or establish
workflow compatibility. The M1 development RPC/schema contract is unchanged.

## Use

From the Rookrunner checkout, with Git installed:

```bash
PYTHONPATH=src python3 -m execution_core --state "$PWD/.execution-state" snapshot \
  --repository /absolute/path/to/repository \
  --workflow .github/workflows/test.yml \
  --include optional-untracked-input.txt
```

Omit `--include` when unnecessary; repeat it for additional explicit files.
The repository argument must be the Git working-tree root. Regular Git worktrees
and an unborn repository with staged inputs are supported. `--workflow` identifies
a regular file to hash, not a validated workflow. An untracked workflow must be
explicitly included. No Git identity is configured or commit created by capture.

Success prints `{"snapshot": {...}}` with a snapshot ID, manifest digest,
workflow digest, base commit, dirty flag, file count, and byte count. Exit zero
means capture completed, not that a job ran. There is no run ID or execution state.
Errors are JSON on stderr with nonzero exit status.

## Selection and manifest

Capture uses Git's index to select tracked **working-file bytes**, including
uncommitted changes; it does not export the index's older blobs. Explicit input
paths add otherwise untracked files, including ordinary ignored files. Paths must
be canonical repository-relative UTF-8 paths without `.`/`..` components. There
is no recursive untracked-directory inclusion or glob expansion.

`STATE/snapshots/ID/files/` contains copies, not hardlinks to original files.
`manifest.json` uses sorted compact JSON and records:

- `format_version: 1`, base commit or null, and Git object format.
- Selected-input `dirty`: whether included working bytes/modes differ from the
  equivalently filtered HEAD tree. Excluded credential changes and omitted
  untracked files do not influence this flag. It is not an unfiltered Git status.
- Workflow path and SHA-256 digest of its exact captured bytes.
- Explicit includes, excluded tracked paths, staged/unstaged deletions.
- Sorted entries with path, type, executable mode, byte count, and SHA-256;
  symlinks also record their literal relative target.

The returned digest is SHA-256 of the exact manifest bytes, including selection
and base-commit metadata. Identical capture manifests have identical digests;
each successful capture has a distinct storage ID. The digest does not identify
a workflow execution result or guarantee reproducibility of external dependencies.

Executable files become mode 0755, other regular files 0644. Setuid, group/other
write permissions, ownership, timestamps, extended attributes, and empty
directories are not preserved. Internal file symlinks and chains are supported;
targets must use canonical relative paths, with any `..` traversal confined to
the leading components and contained within the snapshot. Absolute, cyclic,
dangling, directory, and excluded-target links are rejected. Parents of captured
paths cannot be symlinks. No source symlink is followed while copying bytes.

## Exclusions and unsupported inputs

Known credential/execution paths are excluded even when tracked. An explicit
include cannot override these exclusions. Current rules exclude any component
named `.git`, `.execution-state`, `.cache`, `.aws`, `.ssh`, `.gnupg`, `.docker`,
`.secrets`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `credentials`,
`credentials.json`, `id_rsa`, or `id_ed25519`; `.env` and `.env.*`; and components
ending in `.pem`, `.key`, `.p12`, or `.pfx` (case-insensitive suffixes). A custom
state directory inside the repository is also excluded. These conservative
filename rules are not a universal secret detector; trusted source may contain
embedded secrets under other names.

Index conflicts, submodules, sparse checkout, LFS filter attributes or pointer
contents, special files, and unsupported symlinks fail explicitly. Capturing
materialized LFS data is not silently treated as supported LFS behavior.

Git commands inspect index/tree/attribute metadata. Inherited Git redirection
variables and global/system configuration are disabled, as are hooks, fsmonitor,
optional index locks, prompts, and lazy fetching. No local `.git` configuration,
objects, credentials, hooks, or remote URLs are copied. Consequently Git-dependent
workflows and `actions/checkout` are **not yet supported or validated**. The
[sanitized metadata design](git-metadata.md) names the only fields a later PR
may copy. This slice does not copy them, and checkout actions stay rejected.

## Stability and storage

Capture reads entries twice, comparing content digests, modes, inode/device,
size, and nanosecond modification/change timestamps. It rechecks index/HEAD and
filter attributes before publication. Detected changes cause `SOURCE_UNSTABLE`
with no automatic retry. Later checkout edits cannot change the captured copies.
This is observed-change detection, not an atomic filesystem snapshot or a lock
that prevents arbitrary repository writers.

The state and snapshots directories require owner-only mode 0700. Capture writes
to a private `.preparing-*` directory, fsyncs files/manifest/directories, then
atomically renames it to a UUID directory and fsyncs the parent before replying.
Handled failures remove staging. A crash can leave unreferenced staging or a
completed snapshot whose reply was lost; recovery/retention will be integrated
with durable worker submission later. A snapshot is never an executable queue
entry in this slice. Local owners can still intentionally modify state; execution
must verify the manifest and use a separate attempt workspace before consuming it.

Limits per capture: 10,000 selected files, 64 MiB per regular file, and 256 MiB
total file/link bytes. The total check can temporarily stage one additional
file before rejecting. Git metadata output and the number of retained
snapshots are not bounded by capture. The worker applies a configured disk
budget to the state directory after capture and refuses a submission that
would exceed it. Pruning retained snapshots remains later work.

| Error kind | Meaning |
| --- | --- |
| `SOURCE_INVALID` | Invalid root, path, workflow, metadata, or private directory |
| `SOURCE_EXCLUDED` | Explicit input/workflow conflicts with exclusions |
| `SOURCE_UNSTABLE` | Observed input/index/attribute change, missing explicit input, or Git timeout |
| `SOURCE_LIMIT` | File/count/aggregate capture limit exceeded |
| `CAPABILITY_UNSUPPORTED` | Unsupported Git input, file type, or link |
| `SOURCE_IO_ERROR` | Safe file access or persistence failed |

Path/setup failures before capture can use the CLI's `CLIENT_ERROR` envelope.
These are local capture errors, not additions to the finalized v0 RPC error set.

Selection uses the upstream [Git ls-files interface](https://git-scm.com/docs/git-ls-files);
no Git or other project's source code was imported.
