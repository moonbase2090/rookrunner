# Worker unit template

Status: implemented as a file. The unit is in this repository and is
not installed on a host.

The signed-off LAN worker plan accepts this template. Installing it
on Nexus is a later letter. That letter also builds the host's
x86_64 image and records the digest in the copy installed on the
host. This pull request does not install the unit, does not build
an image, and does not enable user linger.

The file is
[deploy/rookrunner-worker.service](../../deploy/rookrunner-worker.service).

## Implemented

The file is a systemd user unit. `Type=simple` runs the worker as
the service process. `Restart=always` starts it again after it
exits. `WorkingDirectory` is `/var/lib/rookrunner/engine`, the
durable engine checkout. `PYTHONPATH` is
`/var/lib/rookrunner/engine/src`, the import root inside that
checkout. `ExecStart` is `/usr/bin/python3 -m execution_core` with
`--state /var/lib/rookrunner/state`, the `worker` command, and
`--repository /var/lib/rookrunner/clone`. `NoNewPrivileges=yes` is
set. `WantedBy=default.target` is the user-session install target.
The unit has no `User=` line. The user who enables it is the
service user.

The worker binds `worker.sock` inside the state directory. The unit
does not declare a `ListenStream` or any other listen socket.

The template contains no home directory and no `%h`. It contains no
private-key path, no `--app-key`, and no `--docker-socket`. It
contains no image digest. The documented parent digest stays where
the runner-image document already records it. A host digest is
added only in the unit copied onto that host.

## Accepted, and not done here

The Nexus install letter creates `/var/lib/rookrunner/engine`,
`/var/lib/rookrunner/state`, and `/var/lib/rookrunner/clone`, copies
this file into the user unit directory, and appends that host's
image digest to `ExecStart`. The Vertex install letter uses the
same unit, turns on user linger, and records Vertex's own image
digest. Linger on Vertex stays off until that letter.

The poller stays a Mac launchd agent. Linux hosts still have no
poll timer. Capability version stays 12.
`.github/workflows/check.yml` is unchanged.
