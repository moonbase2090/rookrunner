# Dogfood `check.yml` validation

Date: 2026-10-04. Scope: NS-38. This slice adds no capability. The
capability version stays 12. A version 11 plan is not migrated.
`.github/workflows/check.yml` is unchanged.

The identified run did not succeed. Only `succeeded` with exit code 0
for that snapshot would count. It ended `failed` with exit code 137.
The disposable ruff run ended `failed` with exit code 1. Queued,
running, cancelled, lost, and unknown outcomes are not reported as
success.

## Checks

| Run | Commit | State | Exit | Failing step |
| --- | --- | --- | --- | --- |
| Identified `main` | `bb634af7a211540ab54070179b7c6c873cf9c7d4` | `failed` | 137 | Unit tests (step 4) |
| Disposable ruff violation | `4648b3e05fb080d0cdfe51d65b65f634cc879a80` | `failed` | 1 | Ruff (step 3) |

The ruff run is the failure criterion. The success criterion is not
met. The next slice is the gap below, not NS-40.

## Gap

`check.yml`'s unit tests start job containers through the Docker client
in the job. With `--docker-socket`, that client is the host engine.
Those tests bind-mount temporary directories created inside the job.
The host engine does not have those paths. They also remove every
container whose name matches `rookrunner-`, and the job container's
name is `rookrunner-` plus 16 hexadecimal characters. The cleanup kills
the job.

The recorded unit-test stderr is 196 `.` characters and one `E`. The
step then exits 137 and the traceback is not in the step log. Sorted
discovery order places test 197 at
`test_run.DockerRunTests.test_always_runs_after_a_failed_step`. A probe
of that test, in the same image with the socket mounted and a container
name the cleanup filter does not match, raised `container setup failed`.
The daemon reported:

```
invalid mount config for type "bind": bind source path does not exist: /host_mnt/private/tmp/run-docker-7txs14qa/state/attempts/run-1
```

`DockerRunTests.tearDown` runs `docker rm -f` on `docker ps -aq --filter
name=rookrunner-`. That matches the job container, so the recorded step
is killed before unittest prints the traceback.

A path that exists on the host and is mounted into the container at the
same path can be used as a nested bind. A path that exists only in the
job cannot. The worker does not start the job privileged, so an
in-container engine is not available. This slice does not change that.

The image includes `python3` because the suite's Docker wrapper starts
with `#!/usr/bin/env python3`. An earlier image without it reached the
same terminal state and is not the recorded run.

## Identified run

Clean clone of `bb634af7a211540ab54070179b7c6c873cf9c7d4` on `main`.
The clone was detached, so `git.json` `head` is null. `dirty` is false.
The snapshot digest is the SHA-256 of the canonical manifest.

| Field | Value |
| --- | --- |
| Run | `f271e3b2-e92b-47d8-9751-063402fbf7e6` |
| Attempt | `a824bca8-29b0-4c95-8586-21c37dd0d4ae` |
| Snapshot | `c41cf9f2-cdef-49ca-97fd-ed54baea3de5` |
| Snapshot and manifest SHA-256 | `d8d7e0d72af601444ef1ec7ce990a9e12368438d43d905ef7584b2e61ab12d85` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `eb7fd7245b33a392d7ddc13910e63dcbe5cbe9894f740e042d69d1cbe2cfd26f` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` (the CLI default; `--network` was omitted) |
| Started | `2026-10-04T16:52:37.595458+00:00` |
| Finished | `2026-10-04T16:52:55.472932+00:00` |
| `follow` exit | 1 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | succeeded | 0 |
| 4 | Unit tests | failed | 137 |
| 5 | Install uv and Python | skipped | none |

Step 1 is the setup-uv `main`. It installed uv 0.12.18. Step 2 used
the image's CPython 3.12.3 and installed the lockfile, including ruff
0.12.12. Step 3 printed `All checks passed!` and `34 files already
formatted`. Step 5 is that action's `post`. It was skipped. The main
step had failed, and the action's `post-if` is `success()`.

The push event's `after` is the identified commit and its `before` is
`06dba059f6ed889b4b7751256f1d2236a56a588a`. `event-name` is `push`.

## Ruff run

Disposable local commit, parent `bb634af7a211540ab54070179b7c6c873cf9c7d4`.
It is not on `main` and it was not pushed. The only change is an unused
`import os` in `src/execution_core/__init__.py`. `dirty` is false.

| Field | Value |
| --- | --- |
| Run | `5770752f-7e8f-447e-97a1-cb5f910d53c5` |
| Attempt | `06a85889-1767-487b-b722-979af669f7f6` |
| Snapshot | `01f6c0c0-593b-437d-9504-3d1d0a617d9b` |
| Snapshot and manifest SHA-256 | `4298f1c77b2833cf56703d0d08bd602752de5dbc57821da5b61c53726e3516b4` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `9750808bb3f9c72dd1e5f583398ae995d070af37d2f60a72b75884f33d17a561` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` |
| Started | `2026-10-04T16:53:35.630833+00:00` |
| Finished | `2026-10-04T16:53:38.806008+00:00` |
| `follow` exit | 1 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | failed | 1 |
| 4 | Unit tests | skipped | none |
| 5 | Install uv and Python | skipped | none |

The run exit code is 1, the same as the Ruff step. Ruff 0.12.12
reported `F401` for the unused import. The unit-test step did not run.
The setup-uv post was skipped.

## GitHub-hosted reference

The hosted `check` workflow for `bb634af7a211540ab54070179b7c6c873cf9c7d4`
completed with conclusion `success`. This run did not dispatch it.

<https://github.com/moonbase2090/rookrunner/actions/runs/37217343292>

Job: <https://github.com/moonbase2090/rookrunner/actions/runs/37217343292/job/111480431867>

## Command lines

Worker, from the clean clone, state mode `0700`. `--network` omitted:

```
PYTHONPATH=src /Users/brandan/Projects/Moonbase2090/Rookrunner/.venv/bin/python -m execution_core --state /tmp/rookrunner-ns38-state worker --repository /tmp/rookrunner-ns38-src --docker-socket --node24 /private/tmp/rookrunner-ns38-build/node/node-v24.21.0-linux-arm64
```

Submit and follow used that state directory, submission key
`ns38-check-bb634af-py3`, workflow `.github/workflows/check.yml`, job
`check`, event name `push`, and image
`sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8`.
`follow f271e3b2-e92b-47d8-9751-063402fbf7e6` exited 1.

The ruff worker used state directory `/tmp/rookrunner-ns38-state-ruff`
and repository `/tmp/rookrunner-ns38-ruff`. The same image, Node
directory, socket flag, and default network were used. Submission key
`ns38-ruff-violation`. `follow 5770752f-7e8f-447e-97a1-cb5f910d53c5`
exited 1.

`--docker-socket` is set because the unit tests call `docker`. The
README documents that flag for container-dependent tests. The default
network stays `bridge`, so setup-uv and `uv sync` can download. The
worker did not download Node. The operator unpacked the official
linux-arm64 tarball outside the repository.

## Image and Node

Host machine `arm64`. Docker server linux/aarch64 29.8.0. CLI Python is
the repository's `.venv` (CPython 3.12.14).

The image is local and is not pushed. It is
`ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55`
plus `bash`, `git`, `ca-certificates`, `curl`, `python3` 3.12.3, and the
static linux-arm64 Docker 29.8.0 client at `/usr/local/bin/docker`.
Archive SHA-256
`1462a696be6029bd478d7d60d7f3c31cdd15affd1178a4a278aaf4a1d1b7f8b5`.
Built with `--provenance=false --sbom=false`. The recipe is in the
evidence JSON. It is not a product Dockerfile.

Node is `v24.21.0` from
`node-v24.21.0-linux-arm64.tar.xz`, SHA-256
`6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2`.
The `--node24` directory is the unpacked `node-v24.21.0-linux-arm64`
tree. The run recorded mount `/opt/node24` and version `v24.21.0`.

[Evidence JSON](dogfood-check-evidence.json) holds the digests, the
event documents, the step text, the image recipe, and the command
lines. Execution state and the Node and image trees are not in git.
