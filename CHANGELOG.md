# Changelog

The format is Keep a Changelog. Versions follow semantic versioning.
The current package version is 0.1.1. A published release section
names the wheel and tarball hashes once those files are published.
This file does not reconstruct earlier milestones.

## [Unreleased]

- A bash or sh `run` script quotes each `${{ }}` value. An unquoted
  value is single-quoted. A value already inside double quotes is
  escaped. A value already inside single quotes escapes `'`. Another
  shell, or a context the script cannot prove, is refused. An
  expression in `eval` or in a shell `-c` command is refused.
  A brace group or an `if`, `while`, or `for` body does not hide it.
  A backslash in that command name does not hide it either.
  A backslash before a newline is not part of that name.
- A pull request that changes a workflow file is refused. The pass
  reads the diff from the base to the head and does not run
  `refs/pull/<number>/merge` for that pull request unless
  `--allow-pr-workflow` names the author association (`OWNER`,
  `MEMBER`, or `COLLABORATOR`). A missing association does not match.
  A diff that cannot be read is refused the same way. The fetched
  merge commit is checked out only when its first parent is the
  listed base and its second parent is the listed head. The flag is
  not stored.
- A job container is created with `--security-opt no-new-privileges`
  and `--cap-drop ALL`. A service container keeps the engine's default
  capability set, including `CAP_CHOWN`, and sets no-new-privileges.
  It does not receive `--cap-drop` or `--cap-add`. The account-setup
  container stays root so it can write passwd. Passwordless sudo is
  off unless the worker is started with `--passwordless-sudo`. That
  flag writes a sudoers file, omits no-new-privileges, keeps only
  `SETUID`, `SETGID`, and `AUDIT_WRITE`, and warns that the job can
  become root. With `--docker-socket` that is control of the host
  engine. By default a step cannot gain privileges through sudo.
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
