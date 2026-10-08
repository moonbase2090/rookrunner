# Rookrunner

A local tool, worker agent, and execution protocol for software builds and tests.

**Status:** M1 complete; M2 started. The worker runs synthetic development fixtures
and executes one captured workflow job in a caller-pinned container. Planning,
snapshot verification, and attempt materialization exist as libraries.

**Accepted direction:** Rookrunner will own its workflow execution engine and
incrementally match GitHub Actions behavior. The earlier act backend selection
is superseded. See the [engine decision](docs/decisions/0003-owned-execution-engine.md)
and [compatibility plan](docs/design/execution-engine.md).

Developers and coding agents will submit jobs, follow output, and retrieve results through the same execution service.
The first release targets one local worker. Later designs can extend execution to private machines and managed capacity.

## Start here

- [Product requirements](docs/prd.md): users, scope, acceptance criteria, and open decisions.
- [Technical design](docs/design/architecture.md): components, execution boundaries, and recovery.
- [Execution protocol draft](docs/design/protocol.md): client and worker contract.
- [Build milestones](docs/roadmap.md): implementation order and completion evidence.
- [Epics and stories](docs/planning/project-plan.md): Waypoint work breakdown, owners, and acceptance criteria.

## Schedule

A poll of the default-branch tip runs a workflow that lists `on.schedule`. Each entry is `{cron: "..."}` with five POSIX fields, evaluated in UTC. When both the day-of-month and the day-of-week are restricted, the minute matches if either field matches. `7` and `SUN` are Sunday. An unusable cron is rejected.

The first time a workflow is seen, the poll records the latest due minute and does not run it. A later poll runs at most the newest minute that is due, including one catch-up after downtime. That minute is not run again after a restart. The run checks out the default-branch tip. `github.event_name` is `schedule` and `github.event.schedule` is the cron text. File-backed secrets follow the same rule as a push to the default branch. The check context is `rookrunner/<workflow file>/<job>/schedule`.

A due minute runs on the first poll at or after that minute. The engine does not choose the interval. The owner poll is every 5 minutes, so a shorter cron still runs at most once per poll.

## Project boundaries

This is an independent repository under Moonbase2090. `execution-platform` is a temporary directory name.
Rookrunner is the working title. The final public name, CLI name, package identifier, domain, and project license remain undecided.

Local Actions is a reference implementation outside this repository. No source or configuration has been imported from it.
Implementation choices in these documents are proposals unless explicitly marked as accepted.

The intended distribution is open source. A project license must be selected before public distribution.

## Try the development contract

Requires Linux and Python 3.11+ (validated on Python 3.14.6). No third-party
dependencies or Docker are needed for synthetic fixtures. From this checkout:

```bash
export PYTHONPATH="$PWD/src"
python3 -m execution_core --state "$PWD/.execution-state" worker --repository "$PWD"
```

Job containers use Docker network `bridge` by default, so a job can reach the
public internet. GitHub-hosted runners have that access by default
(https://docs.github.com/en/actions/concepts/runners/private-networking).
Start the worker with `--network none` to turn it off. The Docker socket
stays unmounted unless you add `--docker-socket`. That flag gives the job
the host Docker service, which container-dependent tests need. The job
keeps the caller user. When the socket is mounted, a private Docker
volume is mounted into the job at that volume's mountpoint, and
`TMPDIR`, `TEMP`, and `TMP` default to it. A workflow env value can
replace those three. The volume is removed with the job container. The
host `/tmp` is not mounted. The image must already contain the Docker
client. Combining `--docker-socket` with `--app-key` or `--secrets`
refuses at startup unless `--runner-image` is set and a startup probe
shows `~/Secrets` is not a usable directory in that image. A worker
that starts with `--docker-socket` warns that the exposure includes
`~/Secrets/github-app/rookrunner-app/` and
`~/Secrets/rookrunner-secrets/`.
GitHub requires that service to be installed and running on a self-hosted runner
(https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners).

In another terminal, from the same checkout:

```bash
export PYTHONPATH="$PWD/src"
python3 -m execution_core --state "$PWD/.execution-state" describe
python3 -m execution_core --state "$PWD/.execution-state" submit --backend development --key example-1 --delay-ms 500
python3 -m execution_core --state "$PWD/.execution-state" list
python3 -m execution_core --state "$PWD/.execution-state" get RUN_ID
python3 -m execution_core --state "$PWD/.execution-state" logs RUN_ID --limit 1024
python3 -m execution_core --state "$PWD/.execution-state" cancel RUN_ID
python3 -m execution_core --state "$PWD/.execution-state" submit \
  --key example-workflow --workflow .github/workflows/test.yml --job-id build \
  --event '{}' --image rust@sha256:<64 lowercase hex digits>
python3 -m execution_core --state "$PWD/.execution-state" follow RUN_ID
```

Replace `RUN_ID` with the submission's `result.run_id`. Reuse a submission key to
retry; use a new key for a new run. Commands return JSON. A version 1 submit
needs `--workflow`, `--job-id`, and `--event`. `--image` is optional when the worker was started with `--runner-image`. `--event-name` is
optional. When it is set, that name is part of the submission, and a retry
with a different name conflicts. `--backend development`
may be included on that command and is not sent. `follow` prints one JSON
envelope per state change and per nonempty log page, and exits zero only when
the run is `succeeded` with exit code 0. `get` and `logs` stay one-shot.
Successful submission only means acceptance. Stop the worker with Ctrl-C.
State remains on disk for restart. It contains synthetic fixture input and output;
do not place credentials in fixtures or commit state directories.

Install the pinned development-only tools and run contract/integration checks
(requires `uv`; the worker itself still has no third-party runtime dependencies):

```bash
uv sync --locked --group dev
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

`tests/test_run.py` needs a local Docker daemon. It builds a disposable image
and passes that image id. It does not select a project default image.

M2 now provides preparatory source capture (requires Git):

```bash
PYTHONPATH=src python3 -m execution_core --state "$PWD/.execution-state" snapshot \
  --repository /absolute/path/to/repository --workflow .github/workflows/test.yml
```

Use `--include relative/path` for each required untracked file. This creates a
snapshot, not a run. See the [capture rules and limitations](docs/design/source-capture.md).

See the [implemented contract](docs/design/development-contract.md),
[implementation choice](docs/decisions/0001-executable-foundation.md), and
[M1 completion evidence](docs/validation/m1-completion.md), and
[machine-readable schemas](schemas/v0/README.md). M1 covers the development
backend. Version 1 accepts one workflow job and the worker executes it after
recording the attempt. Packaging, MCP, and dashboard work remain on
the roadmap. Development dependency provenance is [recorded here](docs/development-dependencies.md).
