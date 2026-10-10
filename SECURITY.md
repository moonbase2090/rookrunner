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
still turns egress off.

## What this is not

Local mode is not a sandbox for arbitrary hostile repositories.
Capture excludes known credential paths. That exclusion is not
universal secret detection. Workflow output can contain sensitive
data. This project does not promise universal log redaction.
