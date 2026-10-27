---
name: verify-rookrunner
description: "Drive the Rookrunner worker CLI on an isolated state directory: launch, doctor, submit a development fixture, and capture command evidence. Use when proving a user-visible CLI change or when /maintain-verification-skill audits this map."
---

# Verify Rookrunner

Rookrunner's primary surface is the local worker CLI, `.venv/bin/python -m execution_core`. A user starts one worker bound to a private state directory and a repository checkout, then submits work and reads JSON results. This skill drives that CLI. It does not drive the terminal dashboard, the read-only status page, workflow containers, or live GitHub posting. The [failed poll retry recipe](features/poll-retry.md) uses isolated local Git/HTTP/worker fixtures and drives the real CLI flag.

The CLI imports PyYAML, which is a project dependency. From the checkout, `uv sync --locked` creates `.venv` with that dependency. Use `.venv/bin/python` for every command below. Python 3.11 or newer is required. `PYTHONPATH=src` keeps the checkout's source ahead of an installed copy. Do not pass `--docker-socket`, `--app-key`, `--secrets`, or a credential file. Do not read secret directories.

Two verification runs must not share a state directory. A second worker bound to the same directory exits. Use a new `VERIFY_ROOT` per run. Do not point `--state` at `.execution-state` in a checkout someone else is using.

When a change alters how Rookrunner builds, starts, or is tested, run `/maintain-verification-skill` and keep this map aligned with the CLI.

## Launch

From the checkout under test:

```bash
uv sync --locked
export PYTHONPATH="$PWD/src"
RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
VERIFY_ROOT="/tmp/rookrunner-verify-${RUN_ID}"
STATE="$VERIFY_ROOT/state"
EVIDENCE="$VERIFY_ROOT/evidence"
REPO="$(pwd -P)"
mkdir -p "$EVIDENCE"
printf '%s\n' .venv/bin/python -m execution_core --state "$STATE" worker --repository "$REPO" --network none \
  >"$EVIDENCE/worker.command"
.venv/bin/python -m execution_core --state "$STATE" worker --repository "$REPO" --network none \
  >"$EVIDENCE/worker.stdout" 2>"$EVIDENCE/worker.stderr" &
WORKER_PID=$!
```

Ready means `doctor` below exits 0. The worker does not print a ready line. Retry `describe` for up to 10 seconds. `worker.sock` appears under `$STATE` only after the process is listening.

There is no long-lived server beyond that worker process. Each later CLI command is a short client of the same `--state` directory.

Use `/tmp` for `VERIFY_ROOT`. A long temporary directory makes `worker.sock` exceed the AF_UNIX path limit, and both the worker and `describe` then exit with `AF_UNIX path too long`.

## Doctor

Run this before the first drive and again after any failed drive. It is read-only. Define `capture` from Evidence before the block below.

```bash
kill -0 "$WORKER_PID"
capture describe .venv/bin/python -m execution_core --state "$STATE" describe
.venv/bin/python - "$REPO" "$EVIDENCE" <<'PY'
import json
import sys
from pathlib import Path

repository = sys.argv[1]
evidence = Path(sys.argv[2])
if (evidence / "describe.exit").read_text().strip() != "0":
    raise SystemExit("describe exit is not 0")
command = (evidence / "describe.command").read_text().splitlines()
if command[-1:] != ["describe"]:
    raise SystemExit("describe command was not recorded")
reply = json.loads((evidence / "describe.stdout").read_text())
result = reply["result"]
if result["ready"] is not True:
    raise SystemExit("worker is not ready")
if result["repository"] != repository:
    raise SystemExit("worker repository does not match this launch")
if "development.fixture" not in result["capabilities"]:
    raise SystemExit("development fixture capability is missing")
PY
```

A doctor failure means this instance is not worth driving. Do not point the same commands at another worker. A worker configured with `--runner-image` also checks local image availability. For that negative readiness branch, use [configured image readiness](features/describe-worker.md#configured-image-readiness) with a separate worker and state directory.

## Drive

The harness is the CLI itself. Commands are literal. The feature map lists the user-facing entries. Drive one feature from `.cursor/skills/verify-rookrunner/features/` unless the task asks for the whole map.

Set `PYTHONPATH`, `STATE`, `EVIDENCE`, `REPO`, and `WORKER_PID` from Launch before any command below. Clients print one JSON object on stdout. Exit 0 means the protocol call succeeded. A run can still finish with a non-zero fixture exit code. `follow` is the exception: it exits 0 only when the run state is `succeeded` and `exit_code` is 0.

Define `capture` from the Evidence section before any client call. Development fixture submit:

```bash
capture submit .venv/bin/python -m execution_core --state "$STATE" submit \
  --backend development \
  --key "verify-${RUN_ID}" \
  --delay-ms 200 \
  --output "verify-fixture-ok"
```

Read `result.run_id` from `$EVIDENCE/submit.stdout`. Then `capture get` that id until `result.state` is `succeeded` and `result.exit_code` is 0. The wait overwrites `get.command`, `get.stdout`, `get.stderr`, and `get.exit`. Decode `logs` `result.data_base64` and require the text `verify-fixture-ok`.

## Evidence

Write proof under `$EVIDENCE` (`$VERIFY_ROOT/evidence`). Every client call goes through `capture`, which stores the argument list, stdout, stderr, and exit code:

```bash
capture() {
  name="$1"
  shift
  printf '%s\n' "$@" >"$EVIDENCE/${name}.command"
  set +e
  "$@" >"$EVIDENCE/${name}.stdout" 2>"$EVIDENCE/${name}.stderr"
  status=$?
  set -e
  printf '%s\n' "$status" >"$EVIDENCE/${name}.exit"
  return "$status"
}
```

`name.command` is one argv entry per line. `name.exit` is the numeric status. `name.stderr` is kept even when it is empty. For a mutation, also keep a second read (`get` or `logs`) that shows the stored result. Do not treat a successful `submit` by itself as proof that the fixture ran.

The development fixture does not use a test-only HTTP endpoint. `--output` is the user-supplied fixture text, and the proof is that `logs` returns that text after `get` reports success.

## Cleanup

Stop only the worker this run started. Do not match on the process name.

```bash
kill "$WORKER_PID"
set +e
wait "$WORKER_PID"
status=$?
set -e
printf '%s\n' "$status" >"$EVIDENCE/worker.exit"
rm -rf "$STATE"
```

Leave `$EVIDENCE` in place. A later check must still find `describe.command`, `describe.stdout`, `describe.stderr`, and `describe.exit` after `$STATE` is gone.

## Helpers

This skill does not ship a helper script. The commands in Launch, Doctor, Drive, and Cleanup are the harness.
