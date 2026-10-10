# Changelog

The format is Keep a Changelog. Versions follow semantic versioning.
The current package version is 0.1.1. A published release section
names the wheel and tarball hashes once those files are published.
This file does not reconstruct earlier milestones.

## [Unreleased]

- The project license is the Mozilla Public License, v. 2.0.
- `SECURITY.md` states the private reporting path, the socket and state
  modes, and that local mode is not a sandbox for a hostile repository.
- The README records M3 as implemented, the license, the PyYAML 6.0.3
  startup requirement, and the untrusted-code refusal.

## [0.1.1] - 2026-10-10

- The release workflow fetches the annotated tag before `git verify-tag`.
  Checkout had replaced the tag ref with the commit.
- The package version is 0.1.1. A wheel and a source tarball can be
  built from this tree. They are not published.
