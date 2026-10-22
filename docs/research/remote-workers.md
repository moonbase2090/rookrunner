# Private remote workers: requirements and open questions

Status: proposed research, 2026-10-22. Author: rookrunner-redteam. Not reviewed. Still not accepted scope.

This note is not accepted scope. It does not change the [roadmap](../roadmap.md), the [PRD](../prd.md), or the [architecture](../design/architecture.md). The roadmap lists "Authenticated workers on private remote machines" under [later investigations](../roadmap.md#later-investigations), and says each investigation needs its own requirements and evidence before it becomes implementation scope. This note sets out those requirements and the questions MB2090 must answer. It does not design the transport.

## Question

What must be true before a Rookrunner worker on a machine other than the coordinator can accept captured work over a network, and which parts of that are not yet decided?

## What exists today

- **No listener.** The worker exposes no network listener. The worker socket is a Unix socket readable only by its owning user, and the state directory uses restrictive permissions ([architecture](../design/architecture.md)).
- **SSH transport, operator-initiated.** `poll --ssh-host HOST --remote-state DIR` runs the same worker methods through `ssh -o BatchMode=yes`. The remote command is `python3 -m execution_core --state DIR call`, which reads one JSON-RPC request from stdin and talks only to the Unix socket in `DIR`. No listen port is opened ([development contract](../design/development-contract.md)). The host does not fetch from GitHub. The coordinator sends captured objects over the same SSH client.
- **Placement list.** `poll --place` takes a list of places. A place with `ssh` uses the BatchMode client. A place without `ssh` uses a local socket. A new job goes to the first place whose image matches ([development contract](../design/development-contract.md)).
- **Installation by hand.** The second dogfood host is set up with SSH commands run by an operator, and it uses the same service unit as the local worker. User linger is off ([Linux host worker](../design/linux-host-worker.md), [Linux host install](../design/linux-host-install.md)).
- **Secrets.** Secrets are designed for one local worker. The design lists remote workers and webhook registration as out of scope ([secrets](../design/secrets.md)).
- **Discovery only.** The PRD places private remote workers and managed capacity under discovery, and says neither blocks the local preview ([PRD](../prd.md)).

So the current remote path is an operator-run SSH pipe to a worker that already trusts the SSH account. It is not an authenticated worker service.

## Requirements to decide

1. **Authentication of the coordinator.** Which identity may submit work, how it is issued, rotated, and revoked. The SSH account today is the only check, and that check sits outside Rookrunner.
2. **Authentication of the worker.** How the coordinator knows it is talking to the intended worker, pinned by host key or by something Rookrunner records.
3. **Transport security.** SSH is the current answer. Any other transport needs its own threat model, including replay and downgrade.
4. **Authorization scope.** Whether a coordinator may submit to any project, or only to projects and places it was granted.
5. **Leases.** A remote run needs a lease that expires if the coordinator disappears, and cancel and restart rules that do not leave a container running. Restart reconciliation exists only for the local worker ([roadmap](../roadmap.md)).
6. **Snapshot integrity.** Captured inputs must be verified on the remote side against the identified snapshot, as the local path does. The remote worker must not trust a coordinator-supplied digest without checking it.
7. **Secrets leaving the coordinator.** Secret values must not be included in request envelopes, snapshots, or stored results on the remote host. The current secret design covers one local worker only, so this is open.
8. **Logs and artifacts.** Remote logs and artifacts are read through the protocol, not by opening worker paths. Bounds on size and retention need stating for each host.
9. **Operations.** Who installs and updates the remote engine, how its prerequisites and images are pinned, and how a host is removed from the placement list.
10. **Isolation.** A remote host runs the same Docker trust model as the local worker. Hosts shared with other users or customers need stronger isolation. That belongs to [discovery for hosted service](../roadmap.md), not this item.

## Evidence needed before scope

- A written threat model for the SSH path as it runs now, and for any listener.
- A test that shows a submission from an unauthorized identity is refused. The test must fail when the check is removed.
- A test that shows a snapshot whose digest does not match is refused on the remote side.
- A decision on whether the remote lease can expire, and what happens to a running container when it does.

## Questions for MB2090

1. Is the SSH transport the intended answer for the next milestone, or does a network protocol need to be designed?
2. Which identities may submit to a remote worker, and who grants them?
3. Is per-project and per-place authorization in scope for this item, or later?
4. Does this item block the dogfood or owner CI work, or can it wait until after the local preview?

## Non-goals

- No listener, daemon, or open port.
- No change to the protocol version, the plan schema, or the capability version.
- No change to `poll`, `--ssh-host`, or `--place` behavior.
- No host names, addresses, or credentials in this document.
