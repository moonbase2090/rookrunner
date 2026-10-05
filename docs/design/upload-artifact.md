# Owned upload of workspace files

Status: accepted. An owned `actions/upload-artifact` names files in the
artifact manifest, and an owned CodeQL SARIF upload records one local
file under the name `codeql-sarif`. The capability version stays 12.
A version 11 plan is not migrated. `.github/workflows/check.yml` is
unchanged. This is not a GitHub-equivalence claim.

The implementation follows this document. The next item is P4.

## Why this slice is a design

NS-12 already publishes a manifest of regular files a workflow attempt
writes under its workspace that differ from the snapshot. Each entry
has an id, a workspace-relative path, a size, and a SHA-256 digest.
`run.artifacts` pages that list 100 entries at a time.
`artifact.read` pages one file's bytes 65536 bytes at a time. Symlinks
are not followed. The bytes stay in the attempt workspace under the
disk budget. NS-12 does not build an upload-artifact zip, does not add
a second quota, and does not evict.

The owner-CI inventory deferred the artifact HTTP service for
`actions/upload-artifact`, and the same deferral covers
`github/codeql-action/upload-sarif`. The runner requirements note
ER-17 separates that HTTP service from the `GITHUB_ARTIFACTS` file
protocol. Supporting the file does not establish the HTTP API. This
design adds neither the file protocol nor the HTTP service.

The pinned upload program is `node24` with `main: dist/upload/index.js`
and no post step
(https://github.com/actions/upload-artifact/blob/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/action.yml).
The pinned SARIF program is `node24` with a main and a post
(https://github.com/github/codeql-action/blob/2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2/upload-sarif/action.yml).
Both would talk to GitHub. The owned step does not run either program.

Scorecard's coverage and dogfood jobs set `actions: write`. The
website `scorecard.yml` files set `security-events: write`. NS-31
rejects `write`. A SARIF upload that needs a token cannot be
implemented by creating one. That rejection stays.

What was unspecified is which inputs are accepted, when paths are
chosen, what the manifest stores, and that the SARIF step stays on
this worker. This document settles that.

## Accepted uses

The implementation accepts these `uses` strings:

- `actions/upload-artifact@` followed by 40 lowercase hexadecimal
  characters.
- `github/codeql-action/upload-sarif@` followed by 40 lowercase
  hexadecimal characters.

The plan stores that string. The SHA is not verified. The step does
not read an action file, does not fetch a ref, and does not run the
action program. Any 40-character lowercase SHA of those two paths is
accepted. The inventory pins are examples, not the only SHAs:

- `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`
- `github/codeql-action/upload-sarif@2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2`

These stay `CAPABILITY_UNSUPPORTED` and name the field: a tag (`v4`,
`v5`, `v7`), a short SHA, a branch, `actions/upload-artifact` without
`@`, `github/codeql-action/init`, `github/codeql-action/analyze`,
`actions/download-artifact` at any pin, and every other `uses` string.

The implementation recognizes the two strings on a workflow step, which
is where owned checkout is recognized today. It also recognizes them on
a step of a local composite loaded from the snapshot (`./` and `$/`).
A local composite step is a `run` step today and must name `shell`.
The implementation accepts these two uses there, allows `with`, and
does not require `shell`. It does not fetch them. A remote composite's
steps stay `run` steps. Nested remote `uses` stays unimplemented.

The step may still carry `id`, `name`, `if`, `shell`,
`working-directory`, `env`, and `timeout-minutes`. A false `if` skips
the step and records no files. `shell`, `env`, `working-directory`,
and `timeout-minutes` start no process. `working-directory` does not
change the path root below.

## upload-artifact inputs

`path` is required. Omitting it is `WORKFLOW_INVALID`.

`name` may be omitted. An omitted name is `artifact`, which is the
default in the action file at the pin above. The step does not read
that file to learn the default. An empty name, or a name that contains
NUL or a newline, fails the step. This design adds no character class
and no name-length limit.

| Key | Accepted value |
| --- | --- |
| `name` | A string, or omitted |
| `path` | A string of newline-separated patterns |
| `if-no-files-found` | `warn`, `error`, or `ignore`. Omitted means `warn` |
| `include-hidden-files` | A boolean. Omitted means false |
| `archive` | A boolean. Omitted means the files are recorded and not zipped |
| `overwrite` | Boolean `false`, or omitted |

`retention-days` and `compression-level`, when present, are
`CAPABILITY_UNSUPPORTED` and name the field. The action file allows
retention of 1 to 90 days, with `0` meaning the repository default,
and a compression level of 0 to 9. This worker does not expire
artifacts and does not compress them. Scorecard's upload steps omit
both keys.

`overwrite: true` is `CAPABILITY_UNSUPPORTED` and names the field.
Omitted `overwrite` and `overwrite: false` mean a second use of the
same name fails the step. The action file's default is false.

`archive: true` and an omitted `archive` are an intentional
difference. The action file defaults `archive` to true and zips the
files. This worker does not zip. It records each selected regular
file under `name`. `archive: false` means exactly one regular file,
and the artifact name is that file's name. The `name` input is
ignored. More than one file fails the step.

A value that is not a boolean for `include-hidden-files`, `archive`,
or `overwrite` is `WORKFLOW_INVALID`. A literal
`if-no-files-found` other than the three words is `WORKFLOW_INVALID`.
Any other `with` key is `CAPABILITY_UNSUPPORTED` and names the field.

`with` text is evaluated by NS-44 before the step, including mixed
text. `secrets` stays unavailable. `hashFiles` stays
`CAPABILITY_UNSUPPORTED`. The path match does not call `hashFiles`.

## Paths

`path` is a subset of the patterns the action README describes. It is
not `@actions/glob`. Patterns are separated by newlines. An empty line
is ignored.

A pattern is a relative path. A literal file selects that file when it
is a regular file. A literal directory selects the regular files under
it. `*` and `**` are accepted only as a whole path segment. A line
that starts with `!` excludes matches. `**` may cross directories.
`*` is one segment.

These fail the step: `..`, an absolute path, `~`, `?`, and `[`.
`?` and `[` do not match a literal character. The step does not
shell-expand `$`. It does not read `HOME`. The action README roots a
relative path at the current working directory and expands `~` as
`HOME`. Both are intentional differences. Every pattern is rooted at
the workspace. A path that leaves the workspace is rejected.

Symlinks are not followed. A hidden path is one with a segment that
begins with `.`, which is the action README's rule for a file or a
directory segment. Hidden paths are skipped unless
`include-hidden-files` is true, including when the pattern names that
path. `.` as the whole pattern is the workspace directory.

When the match is empty, `if-no-files-found` applies. Omitted means
`warn`. `warn` finishes the step with exit code 0, writes one step-log
line, and records no entries. `error` fails the step. `ignore`
finishes with exit code 0 and writes no log line. An empty match does
not consume a name. The implementation adds no compatibility note.

## Names

Names are unique across the attempt. The attempt has one manifest, and
the selected job and the jobs it needs share that workspace. The
second step that uses a name already recorded in the attempt fails.
`queue` and job order do not create a second manifest.

The action README at the pin above limits one job to 500 artifacts
(https://github.com/actions/upload-artifact/blob/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/README.md).
That cap counts names created by that job, not files. The 501st name
fails the step. Unnamed NS-12 files do not count. An empty match does
not count. This design adds no file-count cap. GitHub's limits page
states no per-file count, and NS-12 already cites that page. The disk
budget remains the byte limit. `run.artifacts` still pages 100 entries
at a time. That page size is not a cap.

## What the step records

The step runs after earlier steps have written files. It records the
artifact name and the workspace-relative paths it selected. It does
not copy the bytes, does not zip them, and does not open a network
connection. It publishes no outputs. A missing step output reads as
empty. `artifact-id`, `artifact-url`, and `artifact-digest` are GitHub
API values. The manifest id and the file digest stay on
`run.artifacts`.

The finish scan remains the source of bytes. A selected path that is
still a regular file under the workspace is one manifest entry with
that name, including a file that matches the snapshot. It is not a
second entry. A selected path that is missing, or that is no longer a
regular file, fails the job. A path selected for two names fails the
second step. Unselected files that differ from the snapshot stay in
the manifest with no name. Unselected files that match the snapshot
stay omitted.

A plan that contains none of these steps publishes the NS-12 manifest
unchanged.

## Manifest

`ArtifactEntry` requires `id`, `path`, `size`, and `digest`, and sets
`additionalProperties` to false. `name` is optional. Old manifests omit
it. Selected files from one upload share one name. The capability
version stays 12. An optional entry field does not change the plan
version. A version 11 plan is not migrated.

## CodeQL SARIF

`github/codeql-action/upload-sarif@` plus 40 lowercase hexadecimal
characters records one SARIF file in the same manifest. It does not
upload it.

`sarif_file` is required in the workflow. Omitting it is
`WORKFLOW_INVALID`. The action file defaults it to `../results`, which
is outside the workspace. This worker does not apply that default and
does not read the action file.

The value is one relative regular file under the workspace. A
directory, a glob, `..`, an absolute path, and `~` fail the step.
There is no newline-separated list in this design. The action file
also accepts a directory. That is an intentional difference. A missing
file fails the step. A file that is gone at the finish scan fails the
job, by the same rule as a selected upload path.

The entry uses the reserved name `codeql-sarif`. A same-attempt
`actions/upload-artifact` of that name fails whichever step comes
second, under the uniqueness rule. The SARIF name counts toward that
job's 500.

These keys, when present, are `CAPABILITY_UNSUPPORTED` and name the
field: `token`, `checkout_path`, `ref`, `sha`, `matrix`, `category`,
and `wait-for-processing`. The action file defaults `token` to
`github.token` and marks `wait-for-processing` required with default
`"true"`. An omitted `wait-for-processing` means the step does not
wait and does not POST. The step does not read `github.token`. It
does not publish `sarif-id` or `sarif-ids`. It does not rewrite paths
inside the file. It does not gzip the file. It does not contact
`api.github.com`.

Code scanning rejects a SARIF upload larger than 10 MB
(https://docs.github.com/en/code-security/code-scanning/troubleshooting-sarif-uploads/file-too-large).
This worker does not upload, so it does not add that limit. The disk
budget is the only size limit.

The Scorecard coverage and dogfood jobs, and the local composite
`./action`, are the shapes this mapping covers. Those upload steps set
`name`, `path` as one relative file or a newline-separated list of
relative files, and `if-no-files-found: warn`. They omit
`retention-days`, `compression-level`, `overwrite`,
`include-hidden-files`, and `archive`. The SARIF step sets only
`sarif_file` to one relative file. Those jobs also set `actions: write`
or `security-events: write`.

## Permissions stay rejected

`write`, `write-all`, and an unknown permissions scope stay
`CAPABILITY_UNSUPPORTED` and name the field. `security-events: write`
and `actions: write` stay rejected. `GITHUB_TOKEN` stays unset. No
credential is created. The Scorecard coverage and dogfood jobs, and
the website `scorecard.yml` files, stay blocked on that rejection
after the implementation lands. This design does not unblock them.
`actions/download-artifact` stays rejected. P7 remains the reviewed
secrets design. macOS stays deferred.

## What the implementation proves

1. A Scorecard-shaped upload step plans: a full SHA, `name`, `path`,
   and `if-no-files-found: warn`. A newline-separated `path` plans.
   The same step inside a local composite plans. The step starts no
   process and contacts no network.
2. An omitted `archive` records each regular file and does not zip.
   `archive: false` with one file uses the file name. More than one
   file fails the step.
3. `retention-days`, `compression-level`, and `overwrite: true` are
   `CAPABILITY_UNSUPPORTED` and name the field.
4. `token` on the SARIF uses is `CAPABILITY_UNSUPPORTED` and names the
   field. An in-workspace `sarif_file` is labeled `codeql-sarif`.
5. `..` fails the step. A second use of the same name fails the step.
6. `if-no-files-found: error` fails the step. `warn` succeeds with
   exit code 0, one step-log line, and no entries.
7. The capability version stays 12. A plan without these steps
   publishes the NS-12 manifest. `security-events: write` and
   `actions: write` still fail planning. `actions/download-artifact`
   stays rejected. `.github/workflows/check.yml` stays unchanged.

## What this slice does not do

No zip, no HTTP artifact service, and no `GITHUB_ARTIFACTS` file. No
token. No credential, no poll, and no status POST. Permissions are
unchanged. `security-events: write` and `actions: write` stay rejected.
No `hashFiles`. No `download-artifact`. The owned step runs no `node24`
program and has no post step.
