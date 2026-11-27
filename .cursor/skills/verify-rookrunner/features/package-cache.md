# Share package download caches

Verify that jobs of one repository share package download caches, and that a
job outside the trust boundary does not. The proof uses real containers.

## Sub-features

- `cache-namespaces` picks `default-branch`, `branches`, or no cache from the stored event and the operator's override.
- `cache-share` lets a later job read cargo, pip, npm, Go module and install-directory files an earlier job left.
- `cache-lock` makes two concurrent jobs wait on cargo's package cache lock.
- `cache-budget` counts the cache toward the disk budget and frees an idle cache before a run is refused.
- `cache-age` removes a cache unused for the maximum age, never one in use.

## How to get to it (user POV)

A worker has the cache on by default. `worker --no-package-cache` turns it off.
`--package-cache-max-age-days` and `--package-cache-max-bytes` change how long
an idle cache lives and how large it may grow. A workflow sees the default
variables (`PIP_CACHE_DIR`, `npm_config_cache`, `GOMODCACHE`, `CARGO_INSTALL_ROOT`
and the others) and may replace any in its `env`.

## Driving it with the execution_core CLI

Preconditions: `uv sync --locked --group dev` and a reachable Linux Docker
engine. The tests build uniquely tagged images from `python:3.12-slim`, so the
base image must be cached or downloadable.

- Run the module with the checkout's interpreter:

  ```sh
  PYTHONPATH=src:tests .venv/bin/python -m unittest test_package_cache
  ```

- Require `OK`. `JobCacheTests` run actual containers: a first job writes files
  into each cache and a second job, with a new workspace, reads them back; a
  job without the cache reads nothing; a second namespace reads nothing; a
  second job started while the first holds cargo's lock sees it held.
- To see it on a worker, start one with `--runner-image` set to an image that
  sets `CARGO_HOME`, submit two push-event jobs, and list
  `STATE/package-cache/default-branch/IMAGE_KEY/` between them.

## Gotchas

- The cache is chosen from the stored event. A development fixture has no
  event, so it never gets one.
- Build output is never cached. `target/`, `node_modules/` and the Go build cache
  stay in the job.
- Deleting a Go module cache needs the engine's removal, which makes read-only
  entries writable first. Do not remove it by hand while a job runs.
