# Security

## Reporting

Report a vulnerability privately through GitHub private vulnerability
reporting for `moonbase2090/rookrunner`:

https://github.com/moonbase2090/rookrunner/security/advisories/new

Do not open a public issue that contains a secret, a private key, a
token, or a credential. A public issue is for a defect that does not
need those values.

## Local access

The worker listens on a Unix socket owned by the user that started it.
The state directory is mode `0700`. The socket is mode `0600`. Both
modes are tested. Another account on the machine cannot submit through
that socket.

The worker exposes no network listener for remote execution or for
unrelated customers. A webhook receiver and GitHub runner registration
are not part of this program. Two host-local listeners still exist.
The status page is a separate read-only process on a host-local
loopback listener.
A job whose permissions grant `id-token: write`, on a worker started
with `--oidc-issuer`, gets one per-job token endpoint on a host-local
listener on an ephemeral port for that job only. It is reachable only
from that job's container. It answers bearer-token
requests for that job and closes when the job ends.

The Docker socket is not mounted into a job unless the worker is
started with `--docker-socket`. Every engine call uses the Docker
context named `default` unless `worker --docker-context` or
`ROOKRUNNER_DOCKER_CONTEXT` names one. An inherited `DOCKER_CONTEXT`
or `DOCKER_HOST` does not select the engine. The scheduler
thread uses that same context.

A job container and a service container are created with a memory
limit of 4g, no swap beyond that limit, a process limit of 1024, and
a CPU limit of 2. Job containers use Docker network `none` by default.
`worker --network bridge` opts in to egress. Service containers start
only with that opt-in. A job container is created with
`--security-opt no-new-privileges` and `--cap-drop ALL`. A service
container is created with `--security-opt no-new-privileges` and keeps
the engine's default capability set, including `CAP_CHOWN`. It does
not receive `--cap-drop` or `--cap-add`. There is no workflow field
that changes that set. The account-setup container still runs as root
to write passwd, and it does not receive those flags. Passwordless
sudo is off unless the worker is started with `--passwordless-sudo`.
That flag writes a sudoers file for the caller, omits
no-new-privileges on the job, and keeps `SETUID`, `SETGID`,
`AUDIT_WRITE`, `CHOWN`, `DAC_OVERRIDE`, `DAC_READ_SEARCH`, and
`FOWNER`. It warns that the job can become root in its container. With `--docker-socket`, that is control of the host
engine. A step cannot gain privileges through sudo.
`--job-privileges` is off by default. It adds only `CAP_SYS_CHROOT` and a `personality` seccomp allowance restricted to arg0 values 0 (PER_LINUX), 262144 (PER_LINUX|ADDR_NO_RANDOMIZE, the `setarch -R` value), and 0xffffffff (reading the current personality) for job containers, on top of Docker's default seccomp profile. It does not add other capabilities. It is intended for chroot smoke steps and setarch or callgrind bench steps. The shipped profile `seccomp-personality.json` is derived from the moby default seccomp profile (Apache-2.0) from `github.com/moby/profiles/seccomp` v0.2.3 `seccomp/default.json` (vendored in moby `docker-v29.7.2`), with `personality` restricted to those values. The `chroot` entry stays gated on `CAP_SYS_CHROOT`.

A bash or sh `run` script does not insert a `${{ }}` value as raw
text. An unquoted value is single-quoted. A value already inside
double quotes is escaped. A value already inside single quotes
escapes `'`. Another shell, or a context the script cannot prove,
is refused. An expression in `eval` or in a shell `-c` command is
refused. A brace group or an `if`, `while`, or `for` body does not
hide it. A backslash in that command name does not hide it either.
A backslash before a newline is not part of that name.
`$'eval'`, `$"eval"`, and a command substitution used as the command
name are refused when that command contains an expression.
An unquoted `*`, `?`, or `[` in that command name is refused the same
way. The `[` command stays accepted. An unquoted `{` in that command
name is refused the same way. `builtin eval` is refused the same way.

A poll refuses untrusted code and creates no run. A pull request
from a fork is refused whatever its author association.
A missing or unrecognized association is refused. A same-repository
pull request with another listed association is judged by its
commits alone. A commit authored by a GitHub App (`type` `Bot`)
passes only when GitHub committed and signed it as `web-flow`. A pushed tip
commit, and every commit on a pull request, needs an author and a
committer with `write` or `admin` permission on the repository. A
commit without a GitHub account is refused, and so is a permission
GitHub does not return. Without `--app-key` the poll sends no
credential, so GitHub returns no permission and the commit is
refused. The `web-flow` committer passes only on a commit whose
signature GitHub verified. `--allow-untrusted` with the commit SHA
runs that one commit, and the run records why.

A pull request is tested from `refs/pull/<number>/merge`. When that
pull request changes a workflow file, or that diff cannot be read,
the pass refuses it unless `--allow-pr-workflow` names the pull
request's author association. The accepted values are `OWNER`,
`MEMBER`, and `COLLABORATOR`. A missing association does not match.
The flag is not stored. The fetched merge commit is checked out
only when its first parent is the listed base and its second parent
is the listed head. A different commit is not checked out and
creates no run.

## What this is not

Local mode is not a sandbox for arbitrary hostile repositories.
Capture excludes known credential paths. That exclusion is not
universal secret detection. Workflow output can contain sensitive
data. This project does not promise universal log redaction.
