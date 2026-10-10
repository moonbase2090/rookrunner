# Install

Version 0.1.0 can be installed in two ways. Neither way is a
published download yet. `uv build` in a checkout writes
`execution_core-0.1.0-py3-none-any.whl` and
`execution_core-0.1.0.tar.gz`. A later release will attach those
files. This page does not install anything on Nexus or Vertex.

## A developer machine

Install the wheel into a new virtualenv. The wheel depends on
PyYAML 6.0.3 and does not vendor it.

```sh
uv venv
uv pip install ./execution_core-0.1.0-py3-none-any.whl
python -m execution_core --help
```

Do not set `PYTHONPATH`. `pip install` of the same wheel is the
same install. The checkout procedure in the README remains the
way to work on this tree.

## A host that follows the Nexus and Vertex layout

This procedure matches
[deploy/rookrunner-worker.service](../deploy/rookrunner-worker.service).
It does not change that file. A host copy of the unit appends
only that host's image digest. The copy still has no key path,
no `--app-key`, no `--docker-socket`, and no listen socket.

1. Create `/var/lib/rookrunner`, `/var/lib/rookrunner/engine`,
   `/var/lib/rookrunner/clone`, and `/var/lib/rookrunner/state`.
   The state directory is mode `0700`. The socket the worker
   creates is mode `0600`.
2. Unpack the source tarball so `src/execution_core` lands at
   `/var/lib/rookrunner/engine/src/execution_core`.

```sh
tar -xzf execution_core-0.1.0.tar.gz -C /var/lib/rookrunner/engine --strip-components=1
```

3. Install PyYAML for `/usr/bin/python3`. The unit runs that
   interpreter, not the wheel.

```sh
/usr/bin/python3 -m pip install 'pyyaml==6.0.3'
```

4. Do not write a `.pth` file. The unit already sets
   `PYTHONPATH=/var/lib/rookrunner/engine/src`.
5. The clone, the image, and starting the user service stay
   operator steps. Copy the repository unit as it is, and append
   only that host's image digest on the `ExecStart` line.

Do not copy a private key into the engine tree. Do not mount the
Docker socket unless the worker is started with `--docker-socket`.
The repository unit does not pass that flag.
