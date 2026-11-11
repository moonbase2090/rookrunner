# Install

The release workflow publishes a wheel, a source distribution, and one immutable
engine bundle for the tagged commit. A bundle is named
`rookrunner-engine-<version>-<full-commit>.tar.gz`. Its manifest records the
commit, package version, source archive digest, state format, protocol versions,
planner capability version, and declared workflow features.

## Developer machine

Install the wheel into a new virtual environment. The wheel depends on PyYAML
6.0.3 and does not vendor it.

```sh
uv venv
uv pip install ./execution_core-0.1.19-py3-none-any.whl
python -m execution_core --help
```

Do not set `PYTHONPATH`. The checkout procedure in the README remains the way
to work on this tree.

## Worker host

Build the bundle from a clean tagged commit. The repository release workflow
does this as part of the signed-tag release:

```sh
uv sync --locked
.venv/bin/python scripts/engine-release.py build --output-dir dist
```

Copy the bundle and `scripts/engine-release.py` to the host. Install it into a
commit-addressed directory, then atomically select it through the `engine`
symlink:

```sh
python3 engine-release.py install rookrunner-engine-<version>-<commit>.tar.gz \
  --root /var/lib/rookrunner
python3 engine-release.py activate <commit> --root /var/lib/rookrunner
systemctl restart rookrunner-worker.service
```

`install` verifies the source archive digest and installed source tree before it
creates `/var/lib/rookrunner/releases/<commit>`. The release directory is
read-only. Reinstalling identical bytes is idempotent; a different artifact for
the same commit is refused. `activate` refuses to replace a real directory, so
an existing mutable `/var/lib/rookrunner/engine` must be migrated once while
the worker is stopped:

```sh
systemctl stop rookrunner-worker.service
mkdir -p /var/lib/rookrunner/releases
mv /var/lib/rookrunner/engine /var/lib/rookrunner/releases/legacy
ln -s releases/legacy /var/lib/rookrunner/engine
```

The legacy tree is preserved. Activating a release only replaces the symlink;
it does not edit the selected release or remove previous versions. To roll
back, activate the previous full commit and restart the worker. The worker
verifies a packaged manifest before opening state and refuses startup if the
manifest or installed source does not match. An unpackaged development
checkout has no manifest and remains usable.

The worker service remains bound to its configured repository and state
directory. Keep the state directory at mode `0700`; the worker socket is mode
`0600`. A host copy of the worker unit must set `NoNewPrivileges=yes`. The
repository unit sets that directive. A copy that omits it is reported by
systemd as `NoNewPrivileges=no` and must be set to `yes`.

Add `--network bridge` to the host copy's `ExecStart` when jobs need egress.
Cargo and pip need it. The repository unit omits the flag, so jobs have no
egress until the host adds it. Do not copy a private key into a release tree or
mount the Docker socket unless the operator explicitly starts the worker with
`--docker-socket`.
