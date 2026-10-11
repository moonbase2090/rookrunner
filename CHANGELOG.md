# Changelog

The format is Keep a Changelog. Versions follow semantic versioning.
The current package version is 0.1.3. A published release section
names the wheel and tarball hashes once those files are published.
This file does not reconstruct earlier milestones.

## [Unreleased]

- The source distribution omits `tests` and `scripts`. The published
  package keeps the same private-key header check without a header
  line in `checks.py`.
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
  of 2.
- The project license is the Mozilla Public License, v. 2.0.
- `SECURITY.md` states the private reporting path, the socket and state
  modes, and that local mode is not a sandbox for a hostile repository.
- The README records M3 as implemented, the license, the PyYAML 6.0.3
  startup requirement, and the untrusted-code refusal.

## [0.1.3] - 2026-10-11

- A host copy of the worker unit must set `NoNewPrivileges=yes`.
  The repository unit sets that directive. A copy that omits it
  is reported by systemd as `NoNewPrivileges=no` and must be set
  to `yes`. This note does not edit a running unit.
- A host that upgrades onto a tree where the job network defaults
  to `none` adds `--network bridge` on that host's unit when jobs
  need egress. Cargo and pip need it. The repository unit omits
  the flag.
- Job containers use Docker network `none` by default.
  `worker --network bridge` opts in to egress. Service containers start
  only with that opt-in.
- Every engine call uses the Docker context named `default` unless
  `worker --docker-context` or `ROOKRUNNER_DOCKER_CONTEXT` names one.
  An inherited `DOCKER_CONTEXT` or `DOCKER_HOST` does not select the
  engine. The scheduler thread uses that same context.
- A bash or sh `run` script quotes each `${{ }}` value. An unquoted
  value is single-quoted. A value already inside double quotes is
  escaped. A value already inside single quotes escapes `'`. Another
  shell, or a context the script cannot prove, is refused. An
  expression in `eval` or in a shell `-c` command is refused.
  A brace group or an `if`, `while`, or `for` body does not hide it.
  A backslash in that command name does not hide it either.
  A backslash before a newline is not part of that name.
  `$'eval'`, `$"eval"`, and a command substitution used as the
  command name are refused when that command contains an expression.
  An unquoted `*`, `?`, or `[` in that command name is refused the
  same way. The `[` command stays accepted. An unquoted `{` in that
  command name is refused the same way. `builtin eval` is refused the
  same way.
- A missing or unrecognized pull request author association is
  refused. A pushed tip commit, and every commit on a pull request,
  needs an author and a committer with `write` or `admin` permission
  on the repository, as GitHub reports it. A commit without a GitHub
  account, or one whose permission GitHub does not return, is refused.
  Without `--app-key` that request has no credential and the commit is
  refused. The `web-flow` committer passes only when GitHub verified
  the signature. `--allow-untrusted` still runs one named SHA.
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
- The package version is 0.1.3. A wheel and a source tarball can be
  built from this tree. They are not published. This change does
  not create a tag.

## [0.1.1] - 2026-10-10

- The release workflow fetches the annotated tag before `git verify-tag`.
  Checkout had replaced the tag ref with the commit.
- The package version is 0.1.1. A wheel and a source tarball can be
  built from this tree. They are not published.
