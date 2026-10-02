# Rookrunner

A local tool, worker agent, and execution protocol for software builds and tests.

**Status:** M1 complete; M2 started. The worker runs synthetic development fixtures
and can accept one captured workflow job onto the queue. It does not execute
that job. Planning, snapshot verification, attempt materialization, and
digest-pinned Bash execution exist as libraries.

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

In another terminal, from the same checkout:

```bash
export PYTHONPATH="$PWD/src"
python3 -m execution_core --state "$PWD/.execution-state" describe
python3 -m execution_core --state "$PWD/.execution-state" submit --backend development --key example-1 --delay-ms 500
python3 -m execution_core --state "$PWD/.execution-state" list
python3 -m execution_core --state "$PWD/.execution-state" get RUN_ID
python3 -m execution_core --state "$PWD/.execution-state" logs RUN_ID --limit 1024
python3 -m execution_core --state "$PWD/.execution-state" cancel RUN_ID
```

Replace `RUN_ID` with the submission's `result.run_id`. Reuse a submission key to
retry; use a new key for a new run. Commands return JSON. Successful submission
only means acceptance: execution success requires `state=succeeded` and
`exit_code=0` for the identified fixture digest. Stop the worker with Ctrl-C.
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
backend. Version 1 can accept one workflow job onto the queue, and `run_job`
can execute a planned job in a caller-pinned container. The worker does not
execute an accepted job. Packaging, MCP, and dashboard work remain on
the roadmap. Development dependency provenance is [recorded here](docs/development-dependencies.md).
