# M1 foundation validation

Historical foundation record. Current completion evidence is in
[M1 completion](m1-completion.md); the status and commands below describe the earlier revision.

Date: 2026-09-22 US/Mountain (2026-09-23 UTC).
Scope: initial executable contract working tree; repository has no initial commit.
M1 remains in progress. This is not release acceptance or workflow compatibility evidence.

## Environment and checks

- Linux 7.0.12-arch1-1, x86_64; Python 3.14.6.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`:
  **13 tests passed**, 4.221 seconds, process exit 0.
- `git diff --check`: passed, exit 0.
- `PYTHONPATH=src python3 -m execution_core --help`: passed, exit 0.

The integration suite starts actual worker processes using disposable private
state directories and Unix sockets. It verifies durable acceptance and reconnect,
normalization/idempotency conflicts, rejection without work creation, terminal
success and nonzero failure, binary-safe paged logs, empty active logs, cursor
scope, list pagination, private permissions, competing worker rejection,
repository binding, queued/active cancellation, terminal-result authority, CLI
JSON/error exit status, malformed/oversized requests, and SIGKILL recovery.
The crash test verifies an interrupted run becomes lost with a null exit code,
queued work completes, and retrying the old key does not create another run.

The initial suite exposed SQLite returning NULL for an empty BLOB substring.
The log reader now treats that as zero bytes. The full suite above passed after
the correction. No other platform or Python version was tested.

## Identified synthetic run

A separate process/socket smoke check submitted a fixture, retrieved its terminal
result, and asserted exit zero and unchanged input digest. Worker and disposable
state were removed after the check; the non-secret result identifiers are recorded
here as evidence, not as a retained live run.

| Field | Observed value |
| --- | --- |
| Run ID | `d6c9f404-422f-4d0f-b1c7-84b1bb86e6a6` |
| Attempt ID | `e869e7b2-d7c1-4c39-87cb-6589396a6899` |
| Input kind | `development_fixture` |
| SHA-256 snapshot/input digest | `9f0c862eb606492a34986dcdc0d904bc23e2471ff4bd400c3a29c60ac5b0b07a` |
| Backend | `development`, version `0.0.1` |
| Terminal state | `succeeded` |
| Exit code | `0` |
| Finished (UTC) | `2026-09-23T00:04:23.259046+00:00` |
| Cleanup | `confirmed_no_external_resources` |

Fixture: exit code 0, delay 0, output `Rookrunner development contract verified\n`.
This digest identifies stored synthetic input, not a captured source tree.

## Not validated or implemented

- Published machine-readable schemas, protocol compatibility guarantees, doctor,
  storage migrations, disk exhaustion recovery, retention budgets.
- Git source capture, act/Docker workflows, external process/container cleanup,
  workflow artifacts, secrets provisioning.
- MCP, dashboard, installed distribution, cold/warm performance measurements,
  clean-environment release acceptance, external-user acceptance.
- Public license/name selection, publishing, remote workers, managed customers.

See the [development contract](../design/development-contract.md) for exact
prototype limits. Subsequent work should finish M1 schema/contract checks before
claiming M1 completion, then implement M2 captured workflow execution.
