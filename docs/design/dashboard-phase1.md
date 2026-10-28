# Dashboard phase 1 preparation

## Accepted direction

The owner approved a web dashboard on 2026-10-23 and revised the access
model on 2026-10-24. Remote access uses Cloudflare Tunnel with Cloudflare
Access sign-in. The tunnel connects outbound; the dashboard listener stays
bound to loopback on the host. Start read-only, keep the existing TUI, and
defer browser cancel and rerun. Present runnable TUI and web options over
the same Rookrunner-owned data before choosing the visual direction.
Web visual work is assigned separately from the data and TUI preparation.

This acceptance supersedes the pending surface/listener decision described
in the separate CI dashboard proposal. It does not accept its proposed APIs,
streaming, controls, trends, or every visual detail. The existing
[worker architecture](architecture.md) and owned-engine direction remain.

## Bounded slice and API gaps

Prepare one shared synthetic fixture bundle and an existing-TUI replay.
The separately authored web option consumes that bundle unchanged, then can
be connected to allowlisted read methods. No worker schema, engine progress,
history service, or control subsystem is needed for this comparison.

Use only `status.view`, `run.list`, `run.get`, `run.logs`, `run.artifacts`,
and `artifact.read`. The worker socket owns the live values. Neither client
reads SQLite or obtains evidence from GitHub. A missing source label,
timestamp, or step remains unknown. Worker readiness is not idle capacity.

Current logs are saved after workflow execution. The fixture's empty running
pages preserve `end_of_stream=false`; they do not establish live streaming.
The existing TUI treats an empty log page as the end even when that marker is
false. Its interactive writer also emits LF in raw mode without CRLF
normalization. These are recorded gaps, not fixed by this comparison harness.
History filters, incremental steps, remote worker discovery, and retained-input
rerun remain outside this bounded slice.

## Shared bundle and runnable TUI

[phase1.json](../../tests/fixtures/dashboard/phase1.json) contains synthetic
worker-protocol examples, not observed workflow executions. `fixture_version`
versions this comparison file only, not the worker protocol. `observed_at` and
`last_observed_at` use RFC3339 UTC. `mode` is `current`, `stale`, or
`unavailable`, and describes client observation, not run state.

Each `responses` entry contains `method`, exact `params`, and a JSON-RPC
`reply` with either `result` or `error`. Match the method and parameters;
never fall back to another run. Read clients use copies of those replies.
The bundle includes queued, running, failed, lost, and succeeded-with-exit-zero
runs. The run listed in `negative_run_ids` deliberately violates success
semantics and must never be presented as success. Its presence makes the
combined list intentionally schema-invalid. Excluding that negative record,
the read replies conform to the existing protocol schemas.

Failed logs have two populated pages, including script-like text, ANSI SGR,
and a bell byte. Running logs have empty nonterminal pages. The failed run
also has a synthetic artifact; a separate missing-run request returns
`RUN_NOT_FOUND`. Placeholder digests, images, and artifacts establish no
execution or availability claim.

Run the existing terminal renderer against the bundle:

```sh
PYTHONPATH=src python3 scripts/dashboard-comparison.py
PYTHONPATH=src python3 scripts/dashboard-comparison.py --mode stale
PYTHONPATH=src python3 scripts/dashboard-comparison.py --mode unavailable
PYTHONPATH=src python3 scripts/dashboard-comparison.py --run 00000000-0000-4000-8000-000000000003
PYTHONPATH=src python3 scripts/dashboard-comparison.py --run 00000000-0000-4000-8000-000000000003 --next-log
PYTHONPATH=src python3 scripts/dashboard-comparison.py --interactive
```

The replay opens no worker socket or network listener and never executes
commands from fixture text. Its controls cannot mutate a worker. The existing
TUI's cancel key returns a fixture-method error. Control bytes are shown
visibly at the comparison rendering boundary. This does not repair production
terminal rendering. Web rendering must escape the original text, use safe
text nodes for logs, and refuse browser mutations.

## Access decisions and proof still required

Keep the runnable web preview and deployed dashboard listener bound to
loopback. Do not bind LAN, wildcard, or public interfaces. The owner configures
the outbound Cloudflare Tunnel and Access sign-in separately. Authentication
and tunnel implementation are outside this preparation slice. This accepted
deployment direction does not claim that either service is configured or
validated.

Listener checks must prove that non-loopback binds are refused. Deployment
proof must separately establish that remote access requires Access sign-in
and reaches the loopback listener through the tunnel. That deployment proof
remains pending with the owner.

Visual selection follows runnable comparison. Runtime changes require focused
behavior checks and clean macOS VM/live proof in the PR before exact-head
review. Listener exposure, safe log rendering, unavailable/stale observation,
cursor navigation, terminal restoration, and success semantics need direct
evidence. This preparation does not deploy the tunnel or enable browser controls.
