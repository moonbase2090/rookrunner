# M2 capture boundary and backend pin

Status: source-capture decision retained; backend selection superseded by
[decision 0003](0003-owned-execution-engine.md), 2026-09-23.
The act selection and adapter plan below are historical, not current direction.
The workflow subset below remains proposed until adapter and integration evidence exist.

## Implemented decisions

Start M2 with a reusable working-file capture component and a local `snapshot`
preparation command. Preserve v0's development run semantics; a new negotiated
workflow submission contract will reference captured source before durable run
acceptance. The [capture contract](../design/source-capture.md) defines current
selection, exclusions, stability checks, limits, and failure behavior.

Retain plain source files and a canonical manifest. Do not copy `.git` metadata
or use the original checkout as an execution directory. Preparing safe Git
metadata for Git-dependent workflows is a subsequent step, not an assumed
benefit of copying a working tree.

Pin the future Linux x86_64 adapter to upstream **act v0.2.89**, commit
`4f411281417e88660bea1c1a1749aa71ae0bd60f`. The
[archived machine-readable pin](history/act.lock.json) records the upstream archive
URL and release-reported SHA-256:

`0191d6f1f3b716b5c55820032605d05fc3c1cdbf581ebeff655019e5dd1524c0`

The [release metadata](https://github.com/nektos/act/releases/tag/v0.2.89),
annotated tag's commit, and [tagged MIT license](https://github.com/nektos/act/blob/v0.2.89/LICENSE)
were inspected through GitHub's API. Any redistributed copy must retain the
upstream copyright and license notice. No act code/archive/binary was imported,
downloaded, bundled, or executed in this slice. Archive-byte verification is
required when the adapter's isolated backend resource is downloaded. An existing
host act installation is not the provenance authority for this project.

## Proposed first workflow subset

- An explicitly selected job with sequential Bash `run` steps.
- Linux x86_64 Docker execution with an explicitly selected runner image pinned
  by digest. Runner-image selection and provenance remain pending.
- Explicit workflow file, job ID, event, timeout, and resource limits; defaults
  and unsupported keys must be validated before acceptance.
- No secrets, reusable workflows, matrix, service containers, host execution,
  privileged execution, or arbitrary action downloads in the first adapter proof.
- Git-dependent steps and checkout actions only after sanitized repository
  metadata and checkout behavior have dedicated integration evidence.

This is a conservative intended adapter subset, not a statement that upstream
act lacks those features. No real-workflow capability is currently advertised.

## Next implementation boundary

Connect validated capture to durable workflow submission, build the pinned
adapter in disposable execution environments, and record backend/image identities.
Add attempt workspaces, process/container ownership, timeout/cancellation and
restart reconciliation, bounded logs/artifacts, and disk budgets. Only then can
M2's representative success/failure and cleanup exit evidence be claimed.
