# Engine release and rollout design

Status: RR-51's artifact, inspection, and canary requirements are accepted.
The implementation choices below define the current implementation. The
production cohort rollout invocation remains proposed pending the owner's
choice of how the existing poller should be triggered.

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

The release workflow builds and publishes one deterministic bundle per tagged
commit. The installer verifies the archive and installed source tree, places
each release under its full commit, and atomically changes the active symlink.
Worker startup validates the manifest before touching state. Legacy SQLite
state receives format version 1; unknown versions are refused. `worker.describe`
reports the release identity, and `scripts/engine-release.py inspect` reads a
credential-free SSH inventory and emits an allowlisted per-unit drift report.

CI installs the exact commit bundle into a temporary host layout and runs a
Docker-backed canary with a pre-d8 state copy and local GitHub API fixture. It
checks terminal success and exit code, snapshot digest, final success status,
and an idempotent clean poll. Production cohort activation after a canary is
not yet automated.
