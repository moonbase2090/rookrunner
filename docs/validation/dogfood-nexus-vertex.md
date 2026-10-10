# Nexus and Vertex dogfood start

Date: 2026-10-10. This is the host install named by
[Nexus and Vertex day-to-day dogfood](../planning/dogfood-nexus-vertex.md).
It records what is running. It does not change the engine. The
capability version stays 12. `.github/workflows/check.yml` is
unchanged.

The 14-day window is open. Both boxes show Worker `up` and Poll
repository `moonbase2090/lightwell` with a last-poll stamp. Success
criteria are written when the window ends. This record leaves them
open. Paused items stay paused. No finding here unlocks one.

## What is running

On Nexus and on Vertex:

- The worker is the repository unit. The host copy appends the
  lightwell image digest. The unit has no `--app-key`, no
  `--docker-socket`, and no `ListenStream`.
- The status page command is `status-page` with state
  `/var/lib/rookrunner/state`. It binds `127.0.0.1:8765`.
- The poll timer runs one pass every 900 seconds and exits. The
  place is local. Nexus cap is 4. Vertex cap is 2. `--image` is
  omitted. The place image is the digest below.
- The repository is `moonbase2090/lightwell`. The workflow is
  `.github/workflows/rookrunner.yml`. The jobs are `checks`,
  `linux-cli-x86_64`, and `tauri-linux-artifact`.
- The poll command passes `--app-key`. It omits
  `--allow-untrusted`, `--credential-file`, `--docker-socket`, and
  `--ssh-host`.

The installed `src/execution_core` Python sources match
`1b686e71990ec24c28d6141e715145ff3dca9650` on both boxes. The
runner image on both boxes is
`sha256:5cfe135b33b53e7c7bb9d445521308369ec2e24b8c2e1c73ec09a0068d947d23`.

Each clone's `origin/main` is
`3d394a9a82fc5be49aae8127f75c96630201f374`. The worktree was clean.
`HEAD` was detached at
`5d6de8b61114982e8944c35410aacfe06f5a1882`. The runs below are for
`3d394a9a82fc5be49aae8127f75c96630201f374`.

The Mac launchd poller for these three jobs is stopped. The
aarch64 place stays on the Mac. These boxes run lightwell for this
dogfood.

The worker, the poll timer, and the status page were active on
both boxes. Both poll timers were enabled.

## Credential

The plan left check posting on the Mac until a decision named a
credential other than a copy of the Mac key. That decision is in
effect. Each box has its own App private key at
`~/Secrets/github-app/rookrunner-app/private-key.pem`, mode
`0600`, owned by the service user. The keys differ. The Mac key
stays on the Mac. The worker unit has no `--app-key`.

## Pages

A read on 2026-10-10 returned HTTP 200 for `GET /` with
`Host: 127.0.0.1:8765` on each box. The page text had no filesystem
path, home path, secret, or token.

| Box | Worker | Ready | Version | Last poll | Repository |
| --- | --- | --- | --- | --- | --- |
| Nexus | `up` | `true` | `0.0.1` | `2026-10-10T03:42:58.018587+00:00` | `moonbase2090/lightwell` |
| Vertex | `up` | `true` | `0.0.1` | `2026-10-10T03:42:54.508801+00:00` | `moonbase2090/lightwell` |

The runner image on both pages is the digest above. The package
version is the page's version. The capability version stays 12
and is not a field on the page.

A second `status-page` process on each box exited 1. Its stderr
was `status page could not bind 127.0.0.1:8765`.

The queue and the concurrency groups rendered. At the Vertex read,
queued was 0 and running was 0.

## Runs for `3d394a9a`

A run is successful only when its state is `succeeded` and its
exit code is 0. These runs use the image digest above.

### Nexus

| Job | Run | Accepted | Finished | State | Exit | Check |
| --- | --- | --- | --- | --- | --- | --- |
| `checks` | `672f1bfd-46f3-436c-bf31-e25c3c95aaf0` | `2026-10-10T03:09:45.837211+00:00` | `2026-10-10T03:11:32.382573+00:00` | `succeeded` | 0 | [114117380951](https://github.com/moonbase2090/lightwell/runs/114117380951) `success` |
| `linux-cli-x86_64` | `3fda883b-67dc-49b8-a3ee-941ed220a7a9` | `2026-10-10T03:09:49.481070+00:00` | `2026-10-10T03:12:14.612144+00:00` | `succeeded` | 0 | [114117392228](https://github.com/moonbase2090/lightwell/runs/114117392228) `success` |
| `tauri-linux-artifact` | `b395bc9e-e750-48e2-abe1-1a8f3c6faeee` | `2026-10-10T03:09:56.416099+00:00` | `2026-10-10T03:15:13.996817+00:00` | `succeeded` | 0 | [114117411937](https://github.com/moonbase2090/lightwell/runs/114117411937) `success` |

### Vertex

| Job | Run | Accepted | Finished | State | Exit | Check |
| --- | --- | --- | --- | --- | --- | --- |
| `checks` | `6b854b90-027b-482c-89de-bfc4b8db96de` | `2026-10-10T03:12:13.584073+00:00` | `2026-10-10T03:14:57.473649+00:00` | `succeeded` | 0 | [114117829065](https://github.com/moonbase2090/lightwell/runs/114117829065) `success` |
| `linux-cli-x86_64` | `2a0baf4e-7c96-4eda-8aeb-4b9b311550b2` | `2026-10-10T03:12:16.614111+00:00` | `2026-10-10T03:16:03.112495+00:00` | `succeeded` | 0 | [114117839063](https://github.com/moonbase2090/lightwell/runs/114117839063) `success` |

Vertex has no `tauri-linux-artifact` run for this commit. The
place cap is 2. That job stays inside the open window.

## Install checks before those posts

These three runs used the same commit and image. The page shows
their check cell as `not posted`.

| Box | Run | Job | Accepted | Finished | State | Exit |
| --- | --- | --- | --- | --- | --- | --- |
| Nexus | `2e4bb543-52ce-4a8a-b2c3-74f6bfb0b587` | `checks` | `2026-10-10T02:23:57.804784+00:00` | `2026-10-10T02:24:00.242408+00:00` | `failed` | none |
| Nexus | `2c381e52-a1ad-4f71-bca8-f2ebe59aeea7` | `checks` | `2026-10-10T02:28:30.157343+00:00` | `2026-10-10T02:30:19.711941+00:00` | `succeeded` | 0 |
| Vertex | `98e6cf3f-83e2-44fd-925e-6a2a08744331` | `checks` | `2026-10-10T02:23:56.727876+00:00` | `2026-10-10T02:26:28.676746+00:00` | `succeeded` | 0 |

`2e4bb543` ended `SETUP_FAILED` with `image digest will not
resolve`. The worker's Docker socket is
`unix:///var/run/docker.sock`. The image was absent from that
socket's context, then loaded there. The retry `2c381e52` ended
`succeeded` with exit code 0. That failure unlocks nothing on the
paused list. The step is not a Docker action.

## Paused items

These stay unimplemented:

- macOS jobs, such as Scorecard's `check-macos`.
- `actions/cache` and Docker actions.
- `actions/setup-node` and `hashFiles`.

No configured job on these boxes has failed because one of those
items is missing. This record does not implement them.

## Still open

- The window's end, and the success-criteria writeup.
- A terminal `tauri-linux-artifact` run on Vertex for a commit in
  the window.
- A later finding that meets the plan's unlock rule. The same
  record does not start that work.
