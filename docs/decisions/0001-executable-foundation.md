# Executable foundation

Status: selected core implementation decision for M1, completed 2026-09-23.
Python 3.11+ is the core language for this local worker/CLI. This engineering
decision completes M1's language selection under the instruction to finish M1;
it does not settle distribution format, license, or public executable name.

## Accepted product direction

The accepted direction remains the PRD's local tool, independent worker, and
documented protocol in an independent repository. Rookrunner is the working title.

## Implementation choice

Use Python 3.11+ and its standard library for the first executable contract.
SQLite, Unix sockets, file locking, JSON, hashing, and subprocess-based tests
need no third-party runtime dependencies. One scheduler thread owns synthetic
execution; protocol operations and scheduler transitions share a lock.
SQLite transactions commit accepted inputs and results before replies.

The neutral internal module is `execution_core`. Invocation currently uses
`PYTHONPATH=src python3 -m execution_core`; it is not a published package or an
installed public CLI. Linux is the implementation target. Python 3.14.6 on the
development Linux host was validated for the foundation; the final M1 contract
suite is recorded separately in [M1 completion evidence](../validation/m1-completion.md).
The minimum Python version and other platforms still require separate checks.

All new source was authored here. No source, binaries, machine configuration,
identity, or operational procedures were imported from Local Actions or another
project. Only Python standard-library modules are used at runtime. Pinned
development-only schema/lint dependencies are inventoried in
[dependency provenance](../development-dependencies.md). Nothing is bundled for
redistribution. Project license selection and distribution notices remain M4 work.

## Implemented behavior

- Explicit state directory, private directory/socket permissions, advisory
  single-owner state lock, persistent canonical repository-path binding.
- Synthetic fixtures only, selected explicitly as `development`.
- Fixture content stored atomically with a SHA-256 input identity; this is not
  repository source capture or a workflow digest.
- Durable keys retained indefinitely with their runs. No pruning, expiry, or
  automatic retries; keys cannot silently become available again.
- Interrupted active fixtures become `lost`; queued fixtures survive restart.
  There are no external processes or containers to reconcile for this backend.
- Bounded queue, request, fixture output, and log/list page sizes.

## Still proposed or pending

Published v0 schemas and error/retention contracts now complete the M1 scope.
The owned workflow engine, captured Git source, external execution ownership, disk budgets,
and backend readiness diagnostics remain M2 work. Packaging may motivate a
separate future runtime decision; Python remains the selected core until then.
No production or workflow compatibility is claimed.
