# Rookrunner-owned execution engine

Status: accepted direction, 2026-09-23, by explicit owner instruction:
“let's do the custom execution engine so we own everything, letting us match GH Actions as much as possible”.

## Decision

Rookrunner will implement and own workflow interpretation, planning, expression
evaluation, job/step scheduling, action execution, process/container supervision,
and result semantics. GitHub Actions workflow compatibility is a product goal,
developed incrementally with explicit evidence for each supported behavior.

This supersedes the act backend selection in decision 0002. Act will not be the
execution engine or a silent fallback. Its unused pin is retained only as a
historical provenance record. An optional act adapter is not part of this decision.
The completed worker/protocol and source-capture work remain applicable.

Owning execution semantics does not require rewriting Docker, Git, shells, or a
general-purpose YAML parser. Third-party libraries and runtime dependencies still
require provenance/license review. No new dependency or engine implementation is
introduced by this decision itself.

## Compatibility policy

- Keep GitHub Actions workflow syntax as the primary input format. Do not replace
  it with a new mandatory Rookrunner workflow language.
- Treat documented GitHub behavior and small reference fixtures as the specification.
  Do not make another emulator's behavior the compatibility authority.
- Track unsupported, implemented, locally validated, reference-validated, and
  intentionally different behavior separately. Local tests alone cannot establish
  equivalence to GitHub-hosted execution.
- Unsupported execution-affecting syntax must fail before executable acceptance
  with a field location and capability explanation. Never silently ignore it or
  report a partially executed workflow as successful.
- Version the supported capability set and execution plan; persist their identities
  with source/workflow/configuration/image digests and per-step evidence.
- Success still requires terminal `succeeded` and `exit_code=0` for identified
  inputs. Step outcomes, job conclusions, cancellation, and unknown cleanup must
  remain distinct.

Broad compatibility is the destination, not an assertion of current feature parity.
GitHub-hosted services, platform credentials, event delivery, runner-image contents,
and operating systems require separate integration or environment evidence.

## Implementation sequence

Follow the [owned-engine design and compatibility plan](../design/execution-engine.md).
M2 establishes a real, supervised run path for a declared initial subset. Subsequent
compatibility increments extend that same engine with expressions, dependencies,
action types, matrices, reusable workflows, and service integrations. They are not
permanently excluded just because the first executable slice is small.

## Current implementation boundary

Implemented: synthetic development runs, persistence, protocol, and preparatory
source capture. Not implemented: YAML workflow planning, native workflow execution,
action runtime, and GitHub reference conformance. This decision changes the accepted
architecture and roadmap; it does not turn prior synthetic tests into engine evidence.

## Checks on this change

`git diff --check` and local documentation-link checks passed. Implementation,
test, and schema file hashes still match the preceding M2 capture evidence; the
act pin was archived byte-for-byte. Runtime tests were not rerun for this
documentation/decision change. The next implementation is workflow parsing,
capability validation, and deterministic execution planning.
