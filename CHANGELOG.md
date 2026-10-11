# Changelog

The format is Keep a Changelog. Versions follow semantic versioning.
The current package version is 0.1.1. A published release section
names the wheel and tarball hashes once those files are published.
This file does not reconstruct earlier milestones.

## [Unreleased]

- A job container is created with `--security-opt no-new-privileges`
  and `--cap-drop ALL`. A service container keeps the engine's default
  capability set, including `CAP_CHOWN`, and sets no-new-privileges.
  It does not receive `--cap-drop` or `--cap-add`. The account-setup
  container stays root so it can write passwd and sudoers. A step
  cannot gain privileges through sudo.
- The App private key directory is configurable. `--key-dir` overrides
  `ROOKRUNNER_KEY_DIR`. The default is `$XDG_CONFIG_HOME/rookrunner/keys`,
  or `~/.config/rookrunner/keys` when that variable is unset. The
  directory stays mode 0700 and `private-key.pem` stays mode 0600.
  Migration: set `ROOKRUNNER_KEY_DIR` or move keys. The file store is
  `~/.config/rookrunner/files/<owner>/<repo>/`, or the same path under
  `$XDG_CONFIG_HOME`.
- Release text is checked before publish. The check rejects private
  infrastructure, hostnames, personal paths, and internal tooling in
  the changelog, the release notes, the docs, the title, the tag
  message, and the built artifacts. Example domains are allowed, as
  is a noreply address at github.com.
- The worker unit keeps the state directory and the clone writable
  and mounts the rest of the filesystem read-only. It drops
  capabilities and limits address families to Unix sockets and IP.
- Job and service containers are created with a memory limit of 4g,
  no swap beyond that limit, a process limit of 1024, and a CPU limit
  of 2. The default network remains bridge.
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
