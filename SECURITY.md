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

The worker has no network listener. Remote execution is a separate
threat model. A webhook receiver and GitHub runner registration are
not part of this program.

The Docker socket is not mounted into a job unless the worker is
started with `--docker-socket`.

A job container and a service container are created with a memory
limit of 4g, no swap beyond that limit, a process limit of 1024, and
a CPU limit of 2. The default network remains `bridge`. `network none`
still turns egress off. A job container is created with
`--security-opt no-new-privileges` and `--cap-drop ALL`. A service
container is created with `--security-opt no-new-privileges` and keeps
the engine's default capability set, including `CAP_CHOWN`. It does
not receive `--cap-drop` or `--cap-add`. There is no workflow field
that changes that set. The account-setup container still runs as root
to write passwd and sudoers, and it does not receive those flags. A
step cannot gain privileges through sudo.

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
