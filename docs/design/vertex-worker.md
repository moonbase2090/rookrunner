# Vertex worker

Status: the install procedure is recorded. User linger stays off.
This pull request does not run a command on Vertex and does not
install the unit.

Vertex uses the same unit as any other host,
[deploy/rookrunner-worker.service](../../deploy/rookrunner-worker.service).
There is no second unit file. `Restart=always`, the engine checkout
`/var/lib/rookrunner/engine`, and `PYTHONPATH` set to that
checkout's `src` stay as that file writes them.

## Linger

User linger on Vertex is off. The install runs this command for the
service user before it enables the user service:

    sudo loginctl enable-linger "$USER"

Linger is not a directive in the unit file. After the command,
`loginctl show-user "$USER" -p Linger` reports `Linger=yes`. The
user service then stays up when the login session ends.

## Image

The install builds Vertex's own x86_64 image and appends that
digest to `ExecStart` on the host copy. The digest stays on the
host. The unit in this repository has no digest.

## Left out

The host copy has no private-key path, no `--app-key`, no
`--docker-socket`, and no listen socket. The poller stays on the
Mac. Capability version stays 12. `.github/workflows/check.yml`
is unchanged.
