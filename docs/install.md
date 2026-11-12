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
uv pip install ./execution_core-0.1.21-py3-none-any.whl
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
symlink. The host's `/usr/bin/python3` needs PyYAML 6.0.3 for the installer
verification and worker runtime.

```sh
/usr/bin/python3 -m pip install 'pyyaml==6.0.3'
/usr/bin/python3 engine-release.py install rookrunner-engine-<version>-<commit>.tar.gz \
  --root /var/lib/rookrunner
/usr/bin/python3 engine-release.py activate <commit> --root /var/lib/rookrunner
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

## Roll out to a cohort

Keep a credential-free JSON fleet inventory on the operator machine. The
default canary adapter uses the local poller and credential file, and reaches
the selected worker over SSH. `--state` is the operator-local poll state; the
remote worker state comes from the canary unit's inventory entry.

```sh
.venv/bin/python scripts/engine-release.py rollout dist/rookrunner-engine-<version>-<commit>.tar.gz \
  --inventory fleet.json \
  --cohort stable \
  --canary-unit worker-a \
  --repo owner/repository \
  --workflow .github/workflows/check.yml \
  --job check \
  --clone /path/to/operator-local/clone \
  --state /path/to/operator-local/poll-state \
  --credential-file /path/to/operator-local/status-token
```

The command installs the bundle on every unit in the selected cohort without
activating it. It activates and restarts the canary first. The local poller
adapter submits one real workflow, waits for `succeeded` with exit code zero,
posts the final success status (which verifies the worker's snapshot digest),
then requires a clean poll with no repeated status. Only then does it activate
and restart the remaining units. A custom `--gate-command` may replace the
adapter; it must write one JSON evidence object with the same verified fields.
If the gate fails, rollout stops before activating other units. Previous
release directories remain installed for operator rollback.

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
