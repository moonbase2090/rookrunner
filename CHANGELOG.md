# Changelog

The format is Keep a Changelog. Versions follow semantic versioning.
The current package version is 0.1.11. A published release section
names the wheel and tarball hashes once those files are published.
This file does not reconstruct earlier milestones.

## [Unreleased]

## [0.1.11] - 2026-10-19

- The worker enforces the per-job disk budget during the run, not
  just at admission. A submission that would exceed the budget is
  refused, and a running job that breaches the budget is stopped.
  The breach is latched on the lease for the run, and the budget
  watch runs past startup until the container is confirmed stopped.
- A per-job OIDC token mint fails closed when its audit record
  cannot be written: no token is issued unless the mint is recorded
  in `oidc-audit.jsonl`. Each mint and each refusal stays one line
  in that file, without the token.
- Secret and token eligibility comes only from the authenticated
  actor, never the commit author. A pull request run withholds actor
  eligibility, so a fork, an override run, and a run the secret
  allowlist does not match receive no secret and no token.
- Passwordless sudo still drops every capability. It then keeps
  `SETUID`, `SETGID`, `AUDIT_WRITE`, `CHOWN`, `DAC_OVERRIDE`, and
  `FOWNER`. `DAC_READ_SEARCH` is no longer kept; the apt, unlink,
  and workspace flows from 0.1.7 still work. A job without that
  opt-in still sets no-new-privileges and adds no capabilities.
- A low-severity hardening batch is included: state files are
  owner-only, an empty or missing run database fails closed instead
  of resetting, an unreachable engine is reported as not-ready, a
  full disk during capture reports storage exhaustion, deeply nested
  input is refused with a structured error naming the limit, the
  status page answers under a bounded budget, and the worker service
  file bounds its start loop with delayed restarts.
- The package version is 0.1.11. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.10] - 2026-10-19

- The `seccomp-personality.json` profile shipped for
  `worker --job-privileges` is regenerated from the current
  Docker default profile. The prior profile had no `clone3`
  entry, so a job container on a `--job-privileges` worker could
  fail during container setup on current Linux Docker, where
  `clone3` needs an ENOSYS fallback when `CAP_SYS_ADMIN` is
  absent. The regenerated profile allows `clone3` with
  `CAP_SYS_ADMIN` and returns ENOSYS (38) without it, and adds
  no `pivot_root` allowance. `worker --job-privileges` is off
  by default. The `personality` allowance stays restricted to
  the values `setarch` needs: 0, 262144, and 0xffffffff, with
  no unconditional entry. The capability version stays 12.
- The package version is 0.1.10. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.9] - 2026-10-19

- `worker --job-privileges` is off by default. A job container on a
  worker started with it keeps `--cap-drop ALL` and adds only
  `CAP_SYS_CHROOT`, plus a seccomp profile that allows
  `personality` on top of the Docker default profile. That
  allowance is restricted to the values `setarch` needs: 0,
  262144, and 0xffffffff. The profile ships as
  `seccomp-personality.json`. It is derived from the Docker
  default profile. Without the flag, the job container
  arguments are unchanged. The flag composes with
  `--passwordless-sudo`. A step running as the job user gets the
  `personality` allowance directly. `chroot` takes effect for
  steps running as root, for example under
  `--passwordless-sudo`. That serves a chroot smoke step and a
  bench step that runs Callgrind under `setarch`. The capability
  version stays 12.
- The package version is 0.1.9. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.8] - 2026-10-19

- A pre-commit hook rejects an unformatted staged Python file under
  `src/` or `tests/`. It checks the staged blob, not the working
  tree. `scripts/install-hooks.sh` points a clone at the hook. CI
  still runs `ruff format --check`.
- A design for OIDC identity tokens records Rookrunner as an OIDC
  issuer. A job can trade a short-lived signed token for a cloud
  credential without a long-lived cloud secret. Every cloud trust
  policy names the new issuer.
- Signing keys are RSA files of at least 2048 bits under `oidc/` in
  the key directory. Each file is named by its RFC 7638 thumbprint,
  and `oidc/active` names the key that signs. Every key is
  published, and only the active key signs. `oidc publish` writes
  `.well-known/openid-configuration` and `.well-known/jwks.json`
  with only the public key members. It refuses a non-https issuer,
  an issuer with a query, a fragment, user info, or a trailing
  slash, and an output directory inside the key directory.
- Token claims hold the issuer, the audience, the subject, the
  repository, the ref, the commit, the run, and the workflow. The
  subject is `repo:<owner>/<repo>:ref:<ref>`, or
  `repo:<owner>/<repo>:pull_request` for a same-repository pull
  request. A token lives for five minutes. The mint decision
  refuses a fork, an override run, and a run the secret allowlist
  does not match. A same-repository pull request also needs the
  pull-request opt-in.
- A per-job token endpoint runs only when the worker is started
  with `worker --oidc-issuer`. That flag is off by default. A job
  must grant `id-token: write`, and the run must be trusted. A job
  that passes gets `ACTIONS_ID_TOKEN_REQUEST_URL` and
  `ACTIONS_ID_TOKEN_REQUEST_TOKEN` in every step. A job that does
  not pass runs without them, and the refusal is audited. A passing
  job on the `none` network fails before its first step and names
  `--network bridge`. Each mint and each refusal is one line in
  `oidc-audit.jsonl`, without the token. The capability version
  stays 12.
- The package version is 0.1.8. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.7] - 2026-10-18

- Passwordless sudo still drops every capability. It then keeps
  `SETUID`, `SETGID`, `AUDIT_WRITE`, `CHOWN`, `DAC_OVERRIDE`,
  `DAC_READ_SEARCH`, and `FOWNER`. Root in that job can chown and
  chmod the apt partial directories, unlink a file in a mode-0700
  directory owned by another user, and stat a mode-0700 workspace
  owned by the job user. A job without that opt-in still sets
  no-new-privileges and adds no capabilities.
- The package version is 0.1.7. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.6] - 2026-10-17

- A step `continue-on-error` may be true, false, or an expression that
  evaluates to a boolean. When the step fails and the value is true,
  the outcome is failure, the conclusion is success, and the job
  continues. A later step sees `success()` as true. False, or an
  omitted key, still fails the job. A non-boolean is rejected. Job
  `continue-on-error` and a composite step's `continue-on-error` stay
  unsupported. The capability version stays 12.
- A poll can record the pending status it already posted when the run
  has since finished. That record is not a terminal state. A later pass
  still posts the finished state. A terminal state that does not match
  the run is still refused.
- The package version is 0.1.6. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.5] - 2026-10-17

- A same-repository pull request with a listed association goes on
  to the per-commit write check instead of being refused for the
  association alone. A fork, a missing association, and an
  unrecognized association are still refused.
- A commit authored by a GitHub App passes the author check only
  when GitHub reports the type as Bot and GitHub committed and
  signed it as web-flow.
- `--allow-pr-workflow` still accepts only OWNER, MEMBER, and
  COLLABORATOR.
- The package version is 0.1.5. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

## [0.1.4] - 2026-10-14

- A poll pass keeps going when an older claimed run has no readable
  snapshot. That run is skipped and its submission stays. Later runs
  still post. Any other status refusal stops the pass.
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
- The project license is the MIT License.
- `SECURITY.md` states the private reporting path, the socket and state
  modes, and that local mode is not a sandbox for a hostile repository.
- The README records M3 as implemented, the license, the PyYAML 6.0.3
  startup requirement, and the untrusted-code refusal.
- The package version is 0.1.4. A wheel and a source tarball can be
  built from this tree. This change does not create a tag.

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
