# M1 executable contract completion

Date: 2026-09-23. Verdict: **M1 complete for the deterministic development backend**.
This does not establish real workflow compatibility or satisfy the M4 release gate.

Historical completion record. The later [owned-engine decision](../decisions/0003-owned-execution-engine.md)
supersedes the act plan mentioned in this record's original remaining milestones.

## Implemented scope

| M1 requirement | Delivered evidence |
| --- | --- |
| Select core language | [Decision 0001](../decisions/0001-executable-foundation.md): Python, standard-library runtime |
| Finalize v0 schemas, limits, errors, and key retention | [Contract](../design/development-contract.md), [schemas and valid/invalid examples](../../schemas/v0/README.md); state-specific run invariants; indefinite key retention with no pruning |
| Worker startup, binding, private socket, single ownership | Real-process tests for mode 0700/0600, repository mismatch, symlink/non-socket protection, second-worker rejection |
| Durable runs and deterministic execution | SQLite acceptance-before-reply, concurrent duplicate protection, success/failure, bounded logs, queue saturation, cancellation races, SIGKILL/clean restart checks |
| CLI discovery/status/logs/cancel/JSON | Worker/CLI process tests and live-response schema validation; malformed replies and corrupt startup storage fail with nonzero status |

The roadmap's specific exit conditions are covered by
`test_disconnect_after_send_and_reconnect`,
`test_concurrent_retries_remain_one_run`,
`test_invalid_requests_never_create_work`, and
`test_socket_permissions_and_single_owner` in `tests/test_contract.py`.
The disconnect test observes the accepted run before retrying, so a retry alone
cannot make the test pass by creating previously unaccepted work.

Additional boundary checks cover malformed UTF-8, duplicate JSON keys, finite
numbers, integer normalization, depth/request/page/queue limits, opaque cursor
scope and integer overflow, and exact UTF-8 output size. Failure injection verifies
that a stopped scheduler refuses new work, SQLite full rolls back acceptance
without consuming the key, database errors are sanitized, and a corrupt database
is preserved while startup fails.

## Final checks

Environment: Linux 7.0.12-arch1-1 x86_64, glibc 2.43, Python 3.14.6,
jsonschema 4.25.1, Ruff 0.12.12. Development dependencies came from the recorded
lockfile contents; see [provenance](../development-dependencies.md).

| Command | Result |
| --- | --- |
| `uv sync --locked --group dev --python /usr/bin/python3` | Passed; isolated `.venv`, Python 3.14.6 |
| `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | **32 tests passed**, 7.525 seconds, exit 0 |
| `.venv/bin/ruff check src tests` | Passed, exit 0 |
| `.venv/bin/ruff format --check src tests` | 9 files already formatted, exit 0 |
| `git diff --check` | Passed, exit 0 |

Earlier development checks passed 31 tests on Python 3.12.13 before the final
startup-error regression test was added. Only the 32-test run on Python 3.14.6
is the final revision evidence. Python 3.11 and other operating systems were not
tested; release support requires separate evidence.

The repository still has no initial commit. Existing staged documentation was
preserved, and changes remain local on `main`. Rather than claiming a commit SHA,
[machine-readable evidence](m1-completion-evidence.json) records SHA-256 values
for all implementation, test, schema JSON, project configuration, and lock files.
The aggregate is SHA-256 of that path-to-hash map encoded as sorted, compact JSON:

`e66d1e6438118e8ac298be3230b15812bec55697ebaf20cfd057284d0817e323`

Documentation is excluded from this aggregate so the evidence report can refer
to it without a circular hash. No implementation changes followed this check.

## Identified execution result

A separate real worker/socket smoke check submitted the following normalized
input and independently hashed these exact ASCII bytes (no trailing newline):

```json
{"backend":"development","fixture":{"delay_ms":0,"exit_code":0,"output":"M1 contract complete\n"}}
```

| Field | Observed value |
| --- | --- |
| Run ID | `6db1eab9-7bcb-4060-a416-26f3ac13c6c4` |
| Input kind | `development_fixture` |
| Snapshot/input SHA-256 | `b19cd7d45720c8084faf5c2fcfa078d82017acdd5ec3503a2b7ea760364f1207` |
| Backend | `development`, version `0.0.1` |
| Terminal state | `succeeded` |
| Exit code | `0` |
| Finished (UTC) | `2026-09-23T15:18:23.588368+00:00` |

The retrieved record passed the published run schema; its digest matched the
independently computed expected value. Temporary workers and state were cleaned
up. The evidence JSON retains the non-secret result, not a live worker or database.

## Remaining milestones

M2 owns Git source capture, pinned act execution, backend/image evidence, external
process/container supervision, timeout/cleanup reconciliation, artifacts, disk
budgets, and backend diagnostics. M3 owns MCP and runnable dashboard design options.
M4 owns packaging, licensing, public names, and clean-environment/external-user
release acceptance. There is no automatic source capture, workflow execution,
package publication, remote worker, or multi-customer service in M1.

Retention is intentionally indefinite and storage use is not yet bounded. The
development backend owns no external processes or containers, so its cancellation
and restart proof must not be reused as evidence for an external backend.
