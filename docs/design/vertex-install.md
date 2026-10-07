# Vertex install

Status: this is the install letter. This pull request does not run
these commands. User linger stays off until an operator runs the
letter on the host.

The worker uses
[deploy/rookrunner-worker.service](../../deploy/rookrunner-worker.service).
There is no second unit file. The engine tree is
`2d0b3ca36c27daec1f91d2bf0be1af53ef3a7207`. The service account is
the SSH account that can already run docker. The host name in the
commands is `vertex`.

The letter copies no App key, opens no listen port, and does not
pass a Docker socket flag. SSH is `ssh -o BatchMode=yes vertex` with
no tunnel flags.

## Commands

1. Directories. State is mode 0700, owned by the service account,
   and not a symlink.

```
sudo install -d -o "$USER" -g "$USER" -m 0755 /var/lib/rookrunner /var/lib/rookrunner/engine /var/lib/rookrunner/clone /var/lib/rookrunner/image
sudo install -d -o "$USER" -g "$USER" -m 0700 /var/lib/rookrunner/state
sudo chmod 0700 /var/lib/rookrunner/state
```

Run those in a shell on the host. `"$USER"` is that account.

2. Engine tree, from a checkout of that SHA. The host does not clone
   GitHub.

```
git archive --format=tar 2d0b3ca36c27daec1f91d2bf0be1af53ef3a7207 | ssh -o BatchMode=yes vertex -- tar -x -C /var/lib/rookrunner/engine
```

3. Import path. The poller runs `python3 -m execution_core --state /var/lib/rookrunner/state call` with no `PYTHONPATH` in that argv.

```
ssh -o BatchMode=yes vertex -- python3 -c 'import site, pathlib; p = pathlib.Path(site.getusersitepackages()); p.mkdir(parents=True, exist_ok=True); (p / "rookrunner.pth").write_text("/var/lib/rookrunner/engine/src\n")'
```

4. Runtime dependency on that same interpreter. The required version
   is 6.0.3. If pip refuses an externally managed environment, rerun
   the same command with `--break-system-packages`.

```
ssh -o BatchMode=yes vertex -- python3 -m pip install --user 'pyyaml==6.0.3'
ssh -o BatchMode=yes vertex -- python3 -c 'import execution_core, yaml; assert yaml.__version__ == "6.0.3"'
```

5. Worker repository. One clone. The origin is
   `https://github.com/moonbase2090/lunatui.git` with no credentials
   in the URL.

```
ssh -o BatchMode=yes vertex -- git init -b main /var/lib/rookrunner/clone
ssh -o BatchMode=yes vertex -- git -C /var/lib/rookrunner/clone remote add origin https://github.com/moonbase2090/lunatui.git
```

6. Image. Build on Vertex so the digest is x86_64. Do not use the
   documented arm64 parent as the base, and do not commit the new
   digest. Place a linux/amd64 docker client binary at
   `/var/lib/rookrunner/image/docker` before the parent build. The
   parent packages are bash, git, ca-certificates, curl, and
   python3. The child layer adds sudo, gcc, and libc6-dev.

```
ssh -o BatchMode=yes vertex -- python3 -c 'import pathlib; root = pathlib.Path("/var/lib/rookrunner/image"); root.mkdir(parents=True, exist_ok=True); (root / "Dockerfile.parent").write_text("FROM ubuntu:24.04\nRUN apt-get update \\\n && apt-get install -y --no-install-recommends bash git ca-certificates curl python3 \\\n && rm -rf /var/lib/apt/lists/*\nCOPY docker /usr/local/bin/docker\nRUN chmod 755 /usr/local/bin/docker\n"); (root / "Dockerfile").write_text("FROM rookrunner-ns38-check:py3\nRUN apt-get update \\\n && apt-get install -y --no-install-recommends sudo gcc libc6-dev \\\n && rm -rf /var/lib/apt/lists/*\n")'
ssh -o BatchMode=yes vertex -- docker build -t rookrunner-ns38-check:py3 -f /var/lib/rookrunner/image/Dockerfile.parent /var/lib/rookrunner/image
ssh -o BatchMode=yes vertex -- docker build -t rookrunner-lunatui-ci:local -f /var/lib/rookrunner/image/Dockerfile /var/lib/rookrunner/image
```

7. Installed unit. Copy the template into the user unit directory
   and append only the runner image flag plus the local image id.

```
digest=$(ssh -o BatchMode=yes vertex -- docker image inspect --format '{{.Id}}' rookrunner-lunatui-ci:local)
ssh -o BatchMode=yes vertex -- python3 -c 'import pathlib, sys; digest = sys.argv[1]; home = pathlib.Path.home(); dest = home / ".config/systemd/user"; dest.mkdir(parents=True, exist_ok=True); text = pathlib.Path("/var/lib/rookrunner/engine/deploy/rookrunner-worker.service").read_text(); needle = " --repository /var/lib/rookrunner/clone\n"; text = text.replace(needle, needle[:-1] + " --runner-image " + digest + "\n", 1); (dest / "rookrunner-worker.service").write_text(text)' "$digest"
ssh -o BatchMode=yes vertex -- python3 -c 'import pathlib; text = (pathlib.Path.home() / ".config/systemd/user/rookrunner-worker.service").read_text(); assert "--runner-image " in text; assert "app-key" not in text; assert "docker-socket" not in text; assert "Listen" not in text'
```

8. Linger, before the user service starts. On the host the command
   is:

```
sudo loginctl enable-linger "$USER"
```

From the operator machine, the account name is read on Vertex:

```
ssh -o BatchMode=yes vertex -- python3 -c 'import pwd, os, subprocess; user = pwd.getpwuid(os.getuid()).pw_name; raise SystemExit(subprocess.call(["sudo", "loginctl", "enable-linger", user]))'
ssh -o BatchMode=yes vertex -- python3 -c 'import pwd, os, subprocess; user = pwd.getpwuid(os.getuid()).pw_name; raise SystemExit(subprocess.call(["loginctl", "show-user", user, "-p", "Linger"]))'
```

The second command reports `Linger=yes`.

9. Start the user service.

```
ssh -o BatchMode=yes vertex -- python3 -c 'import os; os.environ["XDG_RUNTIME_DIR"] = "/run/user/" + str(os.getuid()); os.execvp("systemctl", ["systemctl", "--user", "daemon-reload"])'
ssh -o BatchMode=yes vertex -- python3 -c 'import os; os.environ["XDG_RUNTIME_DIR"] = "/run/user/" + str(os.getuid()); os.execvp("systemctl", ["systemctl", "--user", "enable", "--now", "rookrunner-worker.service"])'
ssh -o BatchMode=yes vertex -- python3 -c 'import os; os.environ["XDG_RUNTIME_DIR"] = "/run/user/" + str(os.getuid()); os.execvp("systemctl", ["systemctl", "--user", "is-active", "rookrunner-worker.service"])'
```

10. Check. The socket is `/var/lib/rookrunner/state/worker.sock`.

```
ssh -o BatchMode=yes vertex -- test -S /var/lib/rookrunner/state/worker.sock
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"worker.describe","params":{}}' | ssh -o BatchMode=yes vertex -- python3 -m execution_core --state /var/lib/rookrunner/state call
```

The reply names ready true and the runner image equal to the local
image id. The Mac poller uses the same `python3` command over
`ssh -o BatchMode=yes vertex`.

## Left out

Capability version stays 12. `.github/workflows/check.yml` is
unchanged. The poller stays on the Mac.
