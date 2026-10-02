# M2 source-capture validation

Date: 2026-09-23. Scope: the first M2 implementation slice. **M2 remains in progress.**
This validates repository capture, not a successful workflow execution.

Historical capture evidence: the subsequent
[owned-engine decision](../decisions/0003-owned-execution-engine.md) supersedes
the act selection recorded here. The pin was moved unchanged from
`backends/act.lock.json` to `docs/decisions/history/act.lock.json`; the evidence
JSON retains its original path/hash map and aggregate rather than rewriting history.

## Checks performed

Environment: Linux 7.0.12-arch1-1 x86_64, Python 3.14.6, Git 2.54.0.

| Check | Result |
| --- | --- |
| `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | **55 passed**, 7.971 seconds, exit 0 |
| `.venv/bin/ruff check src tests` | Passed, exit 0 |
| `.venv/bin/ruff format --check src tests` | 11 files already formatted, exit 0 |
| `git diff --check` | Passed, exit 0 |

The 23 new capture tests use disposable Git repositories and private state. They
cover capture digests and independence from later checkout edits; stable repeat
digests with separate storage IDs; working changes, modes, internal links, and
explicit untracked files; staged/unstaged deletions; credential/custom-state
exclusions; unsupported links, parent-link traversal, special files, LFS,
submodules, sparse checkout; observed content/index/attribute mutations; capture
limits and cleanup; inherited Git-variable isolation; CLI output distinct from
run acceptance; unborn repositories; and Git worktrees without metadata copying.
All 32 M1 tests also passed; the M1 worker protocol and schema were not extended.

## Identified capture proof

A separate disposable fixture captured a workflow file and `source.txt`, edited
the original source, and captured again. The first captured source still read
`original\n`; its manifest/file digests were independently checked by the test
helper. The second capture had a new digest and `dirty=true`.

| Field | Observed value |
| --- | --- |
| First snapshot ID | `7f7e2e7f-e72c-478b-ba23-f178079ee56e` |
| First manifest SHA-256 | `855a72ad62241b2b679581d1ce75472d3822ff836f7bd0a5b7ab965e7c4361bb` |
| Second manifest SHA-256 | `d574e264f0cefc30fd396ba6b8d00553a813b1a3ef651976715f3df0f59ca9fb` |
| Workflow SHA-256 (unchanged) | `4308436ba532c03595ba34976d792b7bb8df259d8c6c2e7d76de6a0db0c3ef88` |
| Original snapshot preserved | Confirmed |

[Evidence JSON](m2-capture-evidence.json) contains the captured manifest, source
identities, and an implementation file-hash map. The aggregate SHA-256 of the
sorted compact path-to-hash map is:

`59d3eb5053632f4af7ba121053df5a3b10693bfcc66eb5231fd0398afacdef54`

The map covers source, tests, v0 schema JSON, act pin, project configuration, and
dependency lock. Documentation is excluded to avoid circular hashes. Test
repositories and execution state were cleaned up; the JSON is evidence, not a
live snapshot store. There is no real-workflow run ID, terminal result, or backend
exit code to report for this slice.

## Backend provenance and environment probe

Docker's read-only version probe reported server 29.7.2. No containers were
created or workflows executed. Upstream act v0.2.89 release metadata, the tag's
commit, and tagged MIT license were inspected; the Linux x86_64 archive URL and
release-reported checksum are recorded in [the archived backend pin](../decisions/history/act.lock.json).
The archive itself was not downloaded or verified. No existing host act binary,
Local Actions configuration, code, state, or identity was imported.

## Still required for M2

- Manifest verification when consuming snapshots; separate attempt workspaces.
- Negotiated workflow submission and durable capture-before-acceptance integration.
- The owned workflow engine and a runner image selected by digest; the earlier
  planned act download is superseded by decision 0003.
- Validated workflow subset, backend/image/configuration evidence, actual
  success/failure workflows, and Git-dependent/checkout compatibility evidence.
- External process/container ownership, timeout/cancellation cleanup, interrupted
  attempt reconciliation, storage budgets, orphan recovery, logs, and artifacts.

The current capture rules deliberately reject some legitimate inputs and do not
provide atomic filesystem snapshots, arbitrary secret detection, sanitized Git
metadata, or a sandbox. See the [implemented capture contract](../design/source-capture.md).
