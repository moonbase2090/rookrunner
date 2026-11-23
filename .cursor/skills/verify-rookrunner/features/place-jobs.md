# Route jobs between places

Verify the optional `jobs` list in `poll --place` using isolated local Git,
HTTP, and worker fixtures. The recipe checks admission and saved poll state.

## Sub-features

- `place-job-routing` admits listed IDs, routes unlisted IDs to the next eligible
  place, and preserves unrestricted placement when the key is absent.
- `place-job-capacity` admits a waiting job only to a place that allows it.
- `place-job-validation` refuses invalid lists and unknown configured IDs.
- `place-job-claims` holds an excluded recorded claim without resubmitting or
  redirecting an uncertain attempt.

## How to get to it (user POV)

Configure jobs with `poll --job WORKFLOW JOB_ID` and repeat `--place JSON` for
the ordered workers. A place may include `"jobs":["build"]` when `build` is a
configured job ID. Omit `jobs` for an unrestricted place. Use these local
fixtures for verification rather than changing a deployed poller.

## Driving it with the execution_core CLI

Preconditions: run `uv sync --locked --group dev` in the checkout under test.
The fixture owns its repositories, workers, sockets, and HTTP endpoint. It
needs no operator credentials or running container engine.

- Run the placement recipe through the skill's Evidence `capture` function:

  ```sh
  PYTHONPATH=src:tests capture place-jobs .venv/bin/python -m unittest test_placement -v
  ```

  Require exit 0 and every case to pass. The JSON routing case uses the CLI
  parser and real worker admission, and checks each worker's recorded job ID.
  The capacity case frees a restricted place while an unlisted job waits, then
  requires the allowed job there and the waiting job on the eligible worker.
- Check the real CLI startup case in that output. An unknown allow-list entry
  must produce `INVALID_PARAMS` about `jobs` before an App key is read.
- Preserve the command, stdout, stderr, and exit code under the evidence
  directory. The negative tests must reject null, empty, repeated, non-string,
  and unconfigured IDs at both parser and poll startup boundaries.

## Gotchas

- List job IDs as given to `--job`, not rendered names or workflow paths.
- A missing key permits all jobs; an empty list is invalid.
- Recorded claims retain their place. Changing the list cannot safely move an
  uncertain submission to another worker.
- The fixture stops worker schedulers and supplies external SSH and image
  inspection boundaries. It proves queued admission, not successful container
  execution or live multi-host failover.
