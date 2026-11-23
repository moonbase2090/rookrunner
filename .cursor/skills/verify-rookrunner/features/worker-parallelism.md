# Execute runs in parallel

Verify worker execution capacity with isolated workers and actual containers.
Controlled socket gates prove overlap without elapsed-time assertions.

## Sub-features

- `worker-slots` runs two jobs concurrently and keeps a third queued until a slot frees.
- `worker-isolation` preserves each captured input and private attempt workspace.
- `worker-shared-budget` refuses a second workspace when the worker can fit only one.
- `worker-group-slots` serializes a group while another group uses the spare slot.
- `worker-slot-cancel` cancels one lease without stopping its peer.
- `worker-slot-close` joins every slot before the database closes.
- `worker-slot-failure` keeps inspection available after execution or thread startup fails.

## How to get to it (user POV)

Start a worker with `worker --repository REPO --cap 2` and submit selected
workflow jobs through its state directory. The default execution capacity is
one. A poll place's `cap` counts queued plus running admissions separately.

## Driving it with the execution_core CLI

Preconditions: `uv sync --locked --group dev`, Git, and a reachable Linux Docker
engine. The proof builds a uniquely tagged image from `python:3.12-slim`, so the
base image must be cached or downloadable. It owns every worker, socket,
repository, image tag, and container it uses. It needs no operator credentials.

- For the CLI route, use a fresh Launch session with `--cap 2`, then Doctor.
  Submit two development fixtures with different keys and `--delay-ms 5000`.
  Capture `get` or `list --filter-state running` until both submitted IDs are
  running at the same time, then cancel both and capture their cancelled states.
  Stop that owned worker before running the independent container proof below.
- Capture proof with the checkout's interpreter:

  ```sh
  .venv/bin/python scripts/verify-worker-parallelism.py --output /tmp/worker-parallel-proof
  ```

  For an explicitly selected engine, add `--docker-context CONTEXT`. This
  sets the context only for the proof process and its children. Omitting it
  uses `ROOKRUNNER_DOCKER_CONTEXT` or `default`, matching worker selection;
  inherited `DOCKER_CONTEXT` and `DOCKER_HOST` do not choose the proof engine.
- Require exit 0 and aggregate `status` `succeeded` in `result.json`. Require
  all twelve named cases to pass in `stderr`.
  The proof saves the command, source digest, stdout, stderr, and terminal
  result outside the checkout. The `runs` directory holds observed overlap
  records and successful terminal records with their captured-source digests
  and actual persisted log output.
- In the overlap case, two different containers must be running at their step
  gates before either is released. A third run stays queued, then enters when
  one slot frees. The first two read different captured sources, and a marker
  placed in the first workspace cannot appear in the second.
- All successful workflow cases must end `succeeded` with exit code 0 and the
  expected persisted log bytes. The cancellation case requires `cancelled` for
  its target and a successful peer. The shutdown case reads the durable rows
  after the database closes. No queued or running result counts as success.

## Gotchas

- The transparent Docker CLI wrapper pauses job-step execution and forwards
  every command to the real engine. It does not replace the worker or backend.
- The container proof calls the real Worker API; the separate CLI drive checks
  positive `--cap 2` forwarding. The test module also checks CLI startup rejection.
- The proof awaits socket messages and database commit signals. It uses no
  fixed sleeps or timing thresholds to establish overlap.
- A worker cap does not change deployed workers or poll place configuration.
- Cleanup removes only resources owned by the fixture. Evidence stays.
