# Implementation status

This is the only record of what the current tree implements.
The [README](../README.md), the [product requirements](prd.md), and the
[roadmap](roadmap.md) point here and do not restate it.

`tests/test_implementation_status.py` reads the fenced blocks on this page.
The check fails when a listed command is not a CLI command, when a listed
protocol method is missing from `execution_core.protocol.METHODS`, when a
listed capability is missing from the unconditional `worker.describe` list,
or when a listed feature's module attribute is missing. It also fails when
the CLI has a command, method, or unconditional capability this page does not
list. The package block must match `execution_core.__version__`, the project
name and build backend in `pyproject.toml`, and
`execution_core.plan.CAPABILITY_VERSION`.

## Package

```status-package
version: 0.1.16
distribution: execution-core
build_backend: hatchling.build
capability_version: 12
```

The build backend is the one that writes a wheel and a source tarball from
this tree. This page does not record whether a download has been published.

## Commands

Each line is one command of `python -m execution_core`.
A second word is that command's subcommand.

```status-commands
worker
storage-plan
describe
mcp
dashboard
status-page
snapshot
submit
follow
get
cancel
logs
artifacts
artifact-read
list
status
dispatch
reconcile
poll
call
oidc
oidc publish
```

Names in the next block are checked to be absent from the CLI.

```status-absent
doctor
```

## Protocol methods

```status-methods
worker.describe
run.submit
run.get
run.list
run.logs
run.artifacts
artifact.read
run.cancel
run.status
status.view
poll.record
```

## Describe capabilities

These are the capabilities `worker.describe` always reports.
A runner image or a Node 24 mount is an optional field on that reply, not an
extra name in this list.

```status-capabilities
development.fixture
run.cancel
run.logs
workflow.job
```

## Features

Each line names a feature and the module attribute that implements it.
The check imports the attribute and does not call it.

```status-features
development worker | execution_core.worker:serve
mcp adapter | execution_core.mcp:serve_stdio
terminal dashboard | execution_core.dashboard:serve_terminal
status page | execution_core.status_page:serve
source capture | execution_core.snapshot:SourceCapture
snapshot verification | execution_core.verify:verify_snapshot
workflow planner | execution_core.plan:plan_workflow
workflow runner | execution_core.run:run_job
runner image readiness | execution_core.run:runner_image_readiness
owner poll | execution_core.poll:poll_once
workflow dispatch | execution_core.poll:dispatch_once
untrusted code refusal | execution_core.poll:untrusted_reason
commit status | execution_core.status:post_status
check run | execution_core.checks:post_check_flow
schedule | execution_core.cron:parse_cron
event trigger | execution_core.trigger:submission_triggered
expressions | execution_core.expr:evaluate
run expression quoting | execution_core.expr:quote_run_expressions
file secrets | execution_core.secrets:load_job_secrets
job token | execution_core.job_token:mint_job_token
node 24 distribution | execution_core.node24:inspect_node24
remote action pin | execution_core.actions:parse_remote_uses
node 24 action staging | execution_core.actions:stage_node24_actions
artifact manifest | execution_core.artifacts:named_manifest
upload selection | execution_core.upload:select_files
concurrency | execution_core.concurrency:decide
disk budget | execution_core.disk:DEFAULT_DISK_BUDGET
status reconcile | execution_core.reconcile:reconcile
oidc discovery publish | execution_core.oidc_keys:publish
```

## Reading the milestones

The command list includes the development-contract commands (`worker`,
`describe`, `submit`, `get`, `list`, `logs`, `cancel`, `follow`) and the
workflow, poll, dashboard, MCP, and status-page commands. The feature list
includes capture, the planner, `run_job`, and the owner-repository poll path.
The package block is the version and build backend in this tree.
`doctor` is an absent command.
