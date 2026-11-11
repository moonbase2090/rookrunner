# Engine release and rollout design

Status: the requirements in RR-51 are accepted. The implementation choices
below are proposed and will be validated against the current worker and
release workflow before rollout.

## Artifact and identity

Build one release bundle from a clean, full Git commit. The bundle contains a
deterministic source archive and a JSON manifest. The manifest records the full
commit, package version, SHA-256 of the source archive, state format version,
protocol versions, planner capability version, and declared workflow features.
The manifest stays outside the source archive so its digest is not self
referential. The installed package also has a source-tree digest, allowing the
worker to verify its files at startup.

Install each bundle under a commit-addressed, immutable release directory. A
stable `engine` symlink selects the active release. Switching that symlink is
atomic; a running worker keeps its loaded code until it is restarted. Previous
release directories remain available for rollback.

An unpackaged checkout remains usable for development. A release install with
a manifest refuses worker startup when the manifest is malformed, has an
unsupported state format, or does not describe the installed package.

## State compatibility

The SQLite state format receives an explicit version. A database without a
version marker is the legacy format and is upgraded transactionally to the
current format after the existing schema migrations succeed. An unknown future
format refuses startup without rewriting the database.

## Fleet inspection and rollout

Fleet inspection reads a version-controlled inventory containing unit names,
SSH destinations, engine/state paths, and intended cohort identities. It asks
each running worker for `worker.describe`, then emits an allowlisted report of
the unit name, state directory, engine identity, capability set, and drift.
The inventory contains no credentials; SSH authentication remains an operator
environment concern. Inspection never restarts a service or writes state.

Rollout first installs the bundle without changing the active symlink. It then
switches and restarts one canary unit. The gate submits a real workflow through
the worker and accepts it only after terminal success, exit code zero, matching
input digest, final status disposition, and a clean second poll. Remaining
units switch only after that gate passes. A failed canary leaves other units on
their previous release. The prior release directory is retained for rollback.

CI builds the bundle from the exact checked-out commit, installs it into a
temporary host layout, upgrades a copy of pre-versioned state, and exercises
the same canary gate. The canary uses disposable state and execution resources.

## Implemented behavior

No release bundle, manifest verification, fleet inspection, or rollout behavior
has been implemented yet.
