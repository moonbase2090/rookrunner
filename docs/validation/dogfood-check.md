# Dogfood `check.yml` validation

Date: 2026-10-04. The capability version stays 12. A version 11 plan
is not migrated. `.github/workflows/check.yml` is unchanged.

## Gap closed

NS-38 recorded a run of `bb634af7a211540ab54070179b7c6c873cf9c7d4` that
ended `failed` with exit code 137. That record is below and is
unchanged. The gap it names is closed.

The identified commit of this closure is
`21a3f5ad5058027fda62b2b9af6bbba9e336bf83`. The clone was detached, so
`git.json` `head` is null. `dirty` is false. The commit is not on
`main`. The run ended `succeeded` with exit code 0. `follow` exited 0.
The disposable ruff run ended `failed` with exit code 1 at the Ruff
step. `follow` exited 1. That commit was not pushed. Queued, running,
cancelled, lost, and unknown outcomes are not reported as success.

| Run | Commit | State | Exit | Failing step |
| --- | --- | --- | --- | --- |
| Identified | `21a3f5ad5058027fda62b2b9af6bbba9e336bf83` | `succeeded` | 0 | none |
| Disposable ruff violation | `3f967e925a6061346005942fa805ce6614b1181b` | `failed` | 1 | Ruff (step 3) |

This closure adds no capability. Test cleanup leaves the job container
in place when the process hostname is that container's id. With
`--docker-socket`, the job's temporary directory is a private Docker
volume mounted at that volume's mountpoint. `TMPDIR`, `TEMP`, and
`TMP` default to it. The volume is removed with the job container. The
host `/tmp` is not mounted. The job is not privileged.

### Identified run

Clean clone of `21a3f5ad5058027fda62b2b9af6bbba9e336bf83`. The snapshot
digest is the SHA-256 of the canonical manifest.

| Field | Value |
| --- | --- |
| Run | `e8c4f018-3e96-4b6a-827e-c25aec04052b` |
| Attempt | `058738ca-50b6-40a5-824c-d9ced1c85deb` |
| Snapshot | `272044bd-9575-4467-9b59-d3c9ca072b7b` |
| Snapshot and manifest SHA-256 | `7c9e0185f8d3c7a30ed086ec9681c5258e3e88f7188b06a65fafaa328e675e49` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `308d5f1932b60fd361c5f298d460bea188d9f3a44129926d52ecfe09c7175469` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` (the CLI default; `--network` was omitted) |
| Started | `2026-10-04T18:41:26.396777+00:00` |
| Finished | `2026-10-04T18:45:13.099998+00:00` |
| Worker | `6c018153-457f-4c34-b0c5-4a2274103230` |
| `follow` exit | 0 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | succeeded | 0 |
| 4 | Unit tests | succeeded | 0 |
| 5 | Install uv and Python | succeeded | 0 |

Step 1 is the setup-uv `main`. It installed uv 0.12.18. Step 2 used
the image's CPython 3.12.3. Step 3 printed `All checks passed!` and
`36 files already formatted`. Step 4 printed `Ran 340 tests in
222.234s` and `OK`. Step 5 is that action's `post`. It ran. Its stdout
says `UV_PYTHON_INSTALL_DIR` is already set.

The push event's `after` is the identified commit and its `before` is
`70361e946571815bc05d3c8419dd95d3cb0f4143`. `ref` is
`refs/heads/ns-38-gap`. `event-name` is `push`.

### Ruff run

Disposable local commit, parent
`21a3f5ad5058027fda62b2b9af6bbba9e336bf83`. It is not on `main` and it
was not pushed. The only change is an unused `import os` in
`src/execution_core/__init__.py`. `dirty` is false.

| Field | Value |
| --- | --- |
| Run | `1b7c1d8a-f7ef-4375-9f3f-50b7503cfc47` |
| Attempt | `b8a5f2f9-1944-43d8-825e-d0b6ff9eef48` |
| Snapshot | `888530d4-9ae3-43a2-bb73-55f33131c1d9` |
| Snapshot and manifest SHA-256 | `d14fdad336575ae908f69cc60616b7ab4d4efafc2058cc3a5ac32027b4a2229f` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `4ec56359b42e00b50f253c252796e324c1301edd581d911d0227b938bdaa85f8` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` |
| Started | `2026-10-04T18:45:59.686409+00:00` |
| Finished | `2026-10-04T18:46:04.003636+00:00` |
| Worker | `c113bdbd-5d33-48c4-8671-1f7f89b2f7b9` |
| `follow` exit | 1 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | failed | 1 |
| 4 | Unit tests | skipped | none |
| 5 | Install uv and Python | skipped | none |

Ruff printed `F401` for the unused `os` import in
`src/execution_core/__init__.py`. The unit-test step was skipped. The
setup-uv `post` was skipped because the main step had failed and the
action's `post-if` is `success()`.

The push event's `ref` is `refs/heads/disposable`. Its `before` is the
identified commit and its `after` is
`3f967e925a6061346005942fa805ce6614b1181b`.

### Command lines

Worker, from the clean clone, state mode `0700`. `--network` omitted.
`$REPO` is the checkout root:

```
PYTHONPATH=src $REPO/.venv/bin/python -m execution_core --state /tmp/rookrunner-gap-state-ok worker --repository /tmp/rookrunner-gap-src --docker-socket --node24 /private/tmp/rookrunner-ns38-build/node/node-v24.21.0-linux-arm64
```

Submit and follow used that state directory, submission key
`ns-gap-check-21a3f5a`, workflow `.github/workflows/check.yml`, job
`check`, event name `push`, and image
`sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8`.
`follow e8c4f018-3e96-4b6a-827e-c25aec04052b` exited 0.

The ruff worker used state directory `/tmp/rookrunner-gap-state-ruff`
and repository `/tmp/rookrunner-gap-ruff`. The same image, Node
directory, socket flag, and default network were used. Submission key
`ns-gap-ruff-3f967e9`. `follow 1b7c1d8a-f7ef-4375-9f3f-50b7503cfc47`
exited 1.

The image and Node tree are the ones named in the NS-38 record. This
commit is not on `main`, so this closure has no GitHub-hosted run.
The hosted result recorded below is for the NS-38 commit.

## NS-38 record

Scope: NS-38. This slice adds no capability.

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

The ruff run is the failure criterion. The success criterion of this
NS-38 run is not met. The gap is below. The closure is the run
recorded above. NS-39 designs owner CI. NS-40 posts one commit status.
NS-41 evaluates `on` for push and pull request. A tag push skips the path filters. Tags under `pull_request` are ignored. NS-42 polls one owner repository. NS-43 recorded this repository's statuses. The record is [owner CI validation](owner-ci-rookrunner.md). NS-44 evaluates expressions in `run`, `env`, `with`, and step and job `name`, including mixed text. NS-45 evaluates `concurrency` and `cancel-in-progress` on this one worker. With `queue: max`, at most 100 runs can be pending in a group. NS-46 names selected workspace files in the artifact manifest and records one local CodeQL SARIF file ([upload artifact](../design/upload-artifact.md)). The capability version stays 12. P4 accepts `actions/checkout@v` plus digits as an owned checkout and copies ancestor history when that plan sets `fetch-depth` to the YAML integer `0` ([checkout tag](../design/checkout-tag.md)). Omitting `fetch-depth` keeps one parentless synthesized commit and still excludes the original commit. `.github/workflows/check.yml` is unchanged. P6 runs the operator-built image when `worker --runner-image` is set and `image` is omitted and every selected job is literal `runs-on: ubuntu-latest` ([runner image](../design/runner-image.md)). An explicit `image` still wins. Any other image stays a caller pin. `run_job` still requires a digest and still does not select a default. P5 posts one check run through the Rookrunner GitHub App when `--app-key` is set, then posts the commit status with that installation token. Omitting `--app-key` keeps the NS-40 token file. The plan schema is unchanged. P7 designs secrets, `GITHUB_TOKEN`, and `write` permissions for owner-repository push and pull-request runs on this one local worker . p7-mask, p7-trust-gate, p7-socket-lock, and p7-secret-env are implemented. p7-secret-run and p7-job-token are not ([secrets](../design/secrets.md)). MB2090 accepted the amended answers on 2026-10-05. The rest of the provisional list is not numbered yet.

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

Worker, from the clean clone, state mode `0700`. `--network` omitted.
`$REPO` is the checkout root:

```
PYTHONPATH=src $REPO/.venv/bin/python -m execution_core --state /tmp/rookrunner-ns38-state worker --repository /tmp/rookrunner-ns38-src --docker-socket --node24 /private/tmp/rookrunner-ns38-build/node/node-v24.21.0-linux-arm64
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
