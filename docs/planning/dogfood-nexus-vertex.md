# Nexus and Vertex day-to-day dogfood

Status: proposed. This document is a plan. It does not install a
unit, load a timer, enable linger, copy a key, build an image, or
start the status page. The host install is a later pull request.

Capability version stays 12.
`.github/workflows/check.yml` is unchanged.

## Accepted facts this plan uses

The worker unit is
[deploy/rookrunner-worker.service](../../deploy/rookrunner-worker.service).
It is a file in this repository. [Worker unit](../design/worker-unit.md)
records that the file is not installed on a host by the change that
added it. Nexus install is a later letter. The Vertex install
procedure is [Vertex install](../design/vertex-install.md). Linger
stays off until that procedure's linger command is run on the host.
This plan does not run it.

One worker binds one repository clone. The unit's
`WorkingDirectory` is `/var/lib/rookrunner/engine`. `PYTHONPATH` is
`/var/lib/rookrunner/engine/src`. `ExecStart` is `/usr/bin/python3 -m execution_core`
with `--state /var/lib/rookrunner/state`, the `worker` command, and
`--repository /var/lib/rookrunner/clone`.
`Restart=always`. `NoNewPrivileges=yes`. `WantedBy=default.target`.
The unit has no `User=` line, no home directory, and no `%h`. It has
no private-key path, no `--app-key`, no `--docker-socket`, and no
`ListenStream`. It has no image digest. A host digest is appended
only on the copy installed on that host. The worker binds
`worker.sock` in the state directory. The socket mode is `0600`.
The state directory mode is `0700`.

The Lightwell x86 configuration is
[Lightwell poll](../design/lightwell-poll.md). The repository is
`moonbase2090/lightwell`. The workflow is
`.github/workflows/rookrunner.yml`. The jobs are `checks`,
`linux-cli-x86_64`, and `tauri-linux-artifact`. Places, in order,
are Nexus at cap 4, then Vertex at cap 2. There is no Mac place for
those jobs. The interval is 900 seconds. `--image` is omitted, so
each place image must equal that worker's runner image. That image
is the lightwell image recorded on the host. It is not the lunatui
image. Lightwell has no `CODEOWNERS` file. The poller records
`signoff_absent`. `ci.yml` stays in Lightwell until cutover. macOS
jobs are not in this configuration. `linux-cli-aarch64` is a
separate Mac place at cap 1. The two passes keep separate state
directories, and their job lists do not overlap.

The process that posts checks is the Mac launchd poller in that
document. It passes the App key flag. The key file stays on the
Mac. A host does not receive a copy. Linux hosts have no poll
timer in the accepted unit documents.

The status page is implemented. The command is `status-page`. It
binds `127.0.0.1:8765` and calls `status.view` only. The design is
[status page](../design/status-page.md). Those pull requests do not
start the page on a host.

The paused list is in [the roadmap](../roadmap.md#planned-paused).
Paused until this dogfood:

- macOS jobs, such as Scorecard's `check-macos`.
- `actions/cache` and Docker actions.
- `actions/setup-node` and `hashFiles`.

Untrusted code is not on that list.

## What to install

The later pull request installs two processes on Nexus and the same
two processes on Vertex. This pull request does not install either
process.

### Worker

The worker is the unit file above, one service per box, one clone.
The clone remote for this plan is `moonbase2090/lightwell`. The
Vertex install document records a different clone remote. That
remote is not the clone this plan runs. The repository copy of the
unit stays free of a digest, a key path, `--app-key`,
`--docker-socket`, and a listen socket. The host copy appends that
host's lightwell image digest and still has none of those other
fields.

### Poller

The poller is a systemd user timer on that box. It is not a
resident service. Every 900 seconds it runs one `poll` pass and
exits. The pass uses a poll state directory separate from the
worker state directory, so `poll.json` is not the worker socket
directory. The place has no `ssh` field. The worker socket is
local. Nexus uses cap 4. Vertex uses cap 2. `--image` is omitted.
The place image is that host's lightwell runner image. The digest
is not written in this document.

Nexus:

```text
python3 -m execution_core
  --state /var/lib/rookrunner/poll
  poll
  --repository moonbase2090/lightwell
  --clone /var/lib/rookrunner/clone
  --job .github/workflows/rookrunner.yml checks
  --job .github/workflows/rookrunner.yml linux-cli-x86_64
  --job .github/workflows/rookrunner.yml tauri-linux-artifact
  --place {"name":"nexus","state":"/var/lib/rookrunner/state","cap":4,"image":"<nexus lightwell image>"}
```

Vertex uses the same command with `"name":"vertex"`, `"cap":2`, and
`"<vertex lightwell image>"`.

The pass does not pass `--allow-untrusted`, `--app-key`,
`--credential-file`, `--docker-socket`, or `--ssh-host`.

### What the later letter still has to keep

The accepted documents keep the posting poller on the Mac because
the App key stays on the Mac. This plan proposes a poller on each
box and proposes that poller with no key. The later letter can add
the timer only while the host still has no copy of the key and the
unit still has no `--app-key`. Listing and check posting that need
the App key stay on the Mac poller until a later decision names a
credential that is not a copy of that key. This document does not
name that credential.

Until that letter, day-to-day placement stays the Mac pass in
[Lightwell poll](../design/lightwell-poll.md): the three jobs,
Nexus then Vertex, caps 4 and 2, `--image` omitted, separate state
directories. The later letter starts the box timer only after that
Mac place for the same jobs has stopped, so two pollers do not
submit the same job. The aarch64 pass stays on the Mac.

The later letter also creates `/var/lib/rookrunner/engine`,
`/var/lib/rookrunner/state`, `/var/lib/rookrunner/poll`, and
`/var/lib/rookrunner/clone`, copies the unit, and appends the host
digest. Linger stays off until that letter runs the linger command
already recorded for the service user. This document does not run
that command.

## Repositories

Both boxes run one repository. One worker binds one clone, and the
accepted x86 places are Nexus and Vertex for the same jobs.

| | |
| --- | --- |
| Repository | `moonbase2090/lightwell` |
| Workflow | `.github/workflows/rookrunner.yml` |
| Jobs on these boxes | `checks`, `linux-cli-x86_64`, `tauri-linux-artifact` |
| Nexus cap | 4 |
| Vertex cap | 2 |
| Interval | 900 seconds |
| Sign-off | `signoff_absent` |

`linux-cli-aarch64` stays the Mac place at cap 1. It is not
installed on these boxes. macOS jobs in `ci.yml` are not in this
configuration.

`moonbase2090/rookrunner` `.github/workflows/check.yml` is this
product's own workflow. It has one job, `check`. Owner CI recorded
one push and one pull request for that repository. It is not
assigned to Nexus or Vertex. Assigning it would replace that box's
lightwell clone. This plan leaves that workflow off these boxes.

These boxes do not run lunatui for this dogfood. The lightwell
place image is not the lunatui image.

## Known code only

Day-to-day polling runs only `moonbase2090/lightwell`, and only the
three jobs above. The invocation does not pass
`--allow-untrusted`.

The poll refuses untrusted code with a warning that names the
reason, and it creates no run. The flag is not a default and it is
not stored. A run created with the flag would record
`untrusted_override` and `untrusted_reason`. This period does not
create that run.

The refusal reasons are:

- A pull request whose head and base repository ids are not equal
  integers. A missing head repository, including a null head
  repository, is a fork. The comparison does not use the full name.
  When both ids are integers and differ, the reason is that the
  head repository is not the owner repository. Any other mismatch
  is a fork.
- An author association other than `OWNER`, `MEMBER`, or
  `COLLABORATOR`.
- A committer association other than `OWNER`, `MEMBER`, or
  `COLLABORATOR`.

A missing association is not a refusal. `pull_request_target` is
not evaluated. `write` stays rejected. A local Docker run is not a
sandbox for hostile code. GitHub's secure-use guidance says a
self-hosted runner should almost never run a pull request from a
public fork.

https://docs.github.com/en/actions/reference/security/secure-use

## Status page

On each box the operator opens `http://127.0.0.1:8765/` in a
browser on that box. The command the later letter starts is:

```text
python3 -m execution_core --state /var/lib/rookrunner/state status-page
```

The command has no host flag, no port flag, and no repository flag.
It binds `127.0.0.1` port `8765` with `AF_INET` only. `GET /` and
`HEAD /` with `Host: 127.0.0.1:8765` call `status.view` through the
worker socket. A different path is HTTP 404. A different Host is
HTTP 400. A missing socket is HTTP 200 and the worker line is
`down`. A timeout is HTTP 200 and the worker line is `busy`. In
both of those cases the runs, the queue, and the groups say
`not available`.

The page does not open `runs.sqlite3` or `poll.json`. It does not
signal the worker. It has no cancel, retry, submit, or status
post. It is not the M3 dashboard. A second process exits 1. Its
stderr line is `status page could not bind 127.0.0.1:8765`.

While the Mac pass is the poller, `poll.json` stays in the Mac poll
state. After posts, and before the final save of that file, the
pass calls `poll.record` on each place whose origin was accepted.
The page reads that stamp from `status.view`. It does not read
`poll.json`. A place is stamped only when `remote_repository` is
set. A `poll_stamp` skip does not stop the pass from saving.

The page shows the worker line, the package version, ready, the
runner image, the last poll time, the poll repository, recent
runs, the queue, and the concurrency groups. The package version
is not the capability version. The capability version stays 12
and is not a field on the page. A run is successful only when its
state is `succeeded` and its exit code is 0. The check cell is a
link only when the stored check URL is a GitHub actions run URL.
Otherwise the cell is `not posted`. The page shows no filesystem
path, home path, secret, token, or submission key.

A page on the Mac that renders a Nexus or Vertex worker is a
non-goal. The later letter starts the page on the box. This pull
request does not start it.

## How long it runs

The worker, the poller, and the status page stay up as the
day-to-day processes. The end of the measurement window does not
stop them.

The measurement window is 14 days. That length is proposed. It is
not an engine constant, not a poll interval, and not a delivery
date. The poll interval stays 900 seconds.

The window starts at the first moment when both boxes show Worker
`up` and Poll repository `moonbase2090/lightwell` with a last-poll
stamp. It ends 14 days later. The operator writes the success
criteria then, including every criterion that failed. A failed
criterion ends nothing by itself and unlocks nothing by itself.

A run still queued 24 hours after acceptance is cancelled and
reported as `error` by the existing poll rule. That cancellation
is not a successful run and does not end the window early.

## Success criteria

All of these are observed on the box, or in the poll warning for
that pass. A queued, running, interrupted, or unknown outcome is
not a successful run.

1. On each box, `GET /` with `Host: 127.0.0.1:8765` returns HTTP
   200 and the worker line is `up`. Ready is `true` while that
   worker is not stopping. A page that says `down` or `busy` fails
   this observation. `Restart=always` may restart the worker. The
   observation that counts is the page after it is up again.
2. Last poll is a stamp, and Poll repository is
   `moonbase2090/lightwell`. The page shows no filesystem path,
   home path, secret, token, or submission key.
3. During the 14 days, each of `checks`, `linux-cli-x86_64`, and
   `tauri-linux-artifact` has a terminal run on Nexus and a
   terminal run on Vertex. The operator records the state and the
   exit code. `succeeded` with exit code 0 meets this criterion
   for that job and box. `failed` with an exit code and a check
   link also meets it, and the operator records the failure. A
   24-hour queue cancellation does not meet it.
4. A second `status-page` process on the same box exits 1, and
   stderr is `status page could not bind 127.0.0.1:8765`. One
   check during the window is enough.
5. The poll invocation does not pass `--allow-untrusted`. When an
   untrusted SHA is seen, the warning names the reason and no run
   is created for that SHA.
6. The queue and the concurrency groups render. The queue cap
   stays 100. The place caps stay 4 on Nexus and 2 on Vertex.
   Those caps are poll placement caps. They are not worker fields.
7. At the end of the window the engine still rejects macOS jobs,
   `actions/cache`, Docker actions, `actions/setup-node`, and
   `hashFiles`. The host unit still has no `--app-key`, no
   `--docker-socket`, and no listen socket. The App key was not
   copied onto the box.

## What a finding unlocks

A finding unlocks one paused item only when a configured job on
these boxes fails, or cannot be expressed, because that item is
missing. The finding names the repository, the job, the SHA, the
run state, and the exit code, plus the engine refusal when there
is one. It includes no filesystem path, home path, or secret.
Writing the finding does not implement the item. A later plan may
be written for an unlocked item. The same letter does not start
that work. These are the only unlocks:

- macOS jobs. A configured job is `runs-on` a macOS label, and the
  planner refuses that label, or the job cannot be placed on these
  x86 boxes because it is macOS.
- `actions/cache` and Docker actions. A configured step is
  `actions/cache` or a Docker action, and the engine reports that
  step unsupported.
- `actions/setup-node` and `hashFiles`. A configured step is
  `actions/setup-node`, or an expression calls `hashFiles`, and the
  engine reports that use unsupported.

Any other finding unlocks nothing on the paused list. These do
not unlock a paused item:

- An apt failure, a missing package, or a network failure.
- A step that runs the docker client and fails because the unit
  has no `--docker-socket`. That step is not a Docker action.
- A hung run, a 24-hour queue cancellation, or a worker restart.
- An untrusted refusal. Untrusted code stays refused. The
  override stays off for this period.
- `signoff_absent` on Lightwell.
- A check cell that says `not posted` because the pass had no
  check run.
- `linux-cli-aarch64`, or a macOS job that lives only in `ci.yml`.
  Those jobs are outside this configuration. Scorecard's
  `check-macos` is the named example of a paused macOS job, and
  it is not one of the three jobs.
- The owned checkout's `node20` special case. Other `node20`
  actions stay rejected.

These are deferred, not paused until this dogfood. No finding in
the window unlocks them:

- A webhook receiver.
- GitHub self-hosted runner registration.
- Deploy workflows and `write` permissions.
- An artifact HTTP service, `pre` entries, tag or branch action
  refs, and private action repositories.
- Windows jobs.
- M4 packaging.
- Remote workers and hosted service discovery.
- Terraform and hosted infrastructure.
- A status page on the Mac that renders another box.

The M3 dashboard and the MCP adapter are already implemented.
They are not paused, and this dogfood does not change them.
