# Owner CI validation for this repository

Date: 2026-10-04. The capability version stays 12. A version 11 plan
is not migrated. `.github/workflows/check.yml` is unchanged. This
slice adds no capability. This is not a GitHub-equivalence claim.

The poll pass ran against moonbase2090/rookrunner. Poll state already
stored the tip of every other branch head the list returned, including
`ns-43-pr`. It did not store `ns-43-ruff`. The pass submitted that
push and pull request 49. It did not submit the other branches. A
second pass submitted nothing. The repeated `failure` was skipped.
The pull request's `success` was posted on that second pass.

Only `succeeded` with exit code 0 posted `success`. The forced Ruff
failure posted `failure`. Queued, running, cancelled, lost, and
unknown outcomes are not reported as success. The credential was read
from an operator file outside the repository and the state directory.
It is not in this record. A scan of the state directory found no copy
of it. The state directory was mode `0700`.

| Event | SHA | Context | Posted states | Rookrunner | Hosted `check` |
| --- | --- | --- | --- | --- | --- |
| `push` `refs/heads/ns-43-ruff` | `ccfbef08d3a3bea8652c5a95c7b26abc72c006dd` | `rookrunner/check.yml/check` | `pending`, then `failure` | `failed`, exit 1 | `failure` |
| `pull_request` 49 | `a2fcf8e952797aab2622ffc5ec0fafc897ac75d1` | `rookrunner/check.yml/check` | `pending`, then `success` | `succeeded`, exit 0 | `success` |

The pull request status is on the head SHA. The tested commit is the
merge commit `a8991cc2f0d10395990961da29768edd422173f0`. The same head
SHA also has a GitHub-hosted `push` check, and that check ended
`success`. Rookrunner did not submit a push for that stored tip.

## Push

Branch `ns-43-ruff`, parent
`ca3cb1bb323c77f5ef9244b771317aaa78414d47`. The only change is an
unused `import os` in `src/execution_core/__init__.py`. The branch had
no stored tip, so `before` is forty `0` characters. `after` and the
status SHA are the branch tip. `follow` exited 1.

| Field | Value |
| --- | --- |
| Run | `ecc9c190-5f7f-4a63-9451-d67c914e6df4` |
| Attempt | `be3a4f9e-00ec-4d18-a722-529578d3731c` |
| Snapshot | `4079f328-1c06-41da-9680-d9735ea38c86` |
| Snapshot and manifest SHA-256 | `973b7cdb54abf456860454d00218997dd88fa1f8d88a0edbd7a63d5be3998928` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `e12d7a662094f8d2c94c67032417a23bfb26ec5faa86cdcbd2b77f0023ee098d` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` (the CLI default; `--network` was omitted) |
| Accepted | `2026-10-04T22:03:32.694453+00:00` |
| Started | `2026-10-04T22:03:32.701040+00:00` |
| Finished | `2026-10-04T22:03:36.073809+00:00` |
| Worker | `ed794708-79cf-443a-bd49-e78490d005c7` |
| `follow` exit | 1 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | failed | 1 |
| 4 | Unit tests | skipped | none |
| 5 | Install uv and Python | skipped | none |

Ruff printed `F401` for the unused `os` import. The unit-test step did
not run. The setup-uv `post` did not run. GitHub's hosted `check` for
this SHA completed `failure`:
https://github.com/moonbase2090/rookrunner/actions/runs/37238491765/job/111542272079

## Pull request

Pull request 49, head `ns-43-pr`, base `main` at
`ca3cb1bb323c77f5ef9244b771317aaa78414d47`. The pass fetched
`refs/pull/49/merge`. There was no stored observation for that pull
request. `before` is the base SHA. The tested commit is the merge
SHA. The status SHA is the head SHA. `follow` exited 0.

| Field | Value |
| --- | --- |
| Run | `7c355019-c502-45da-867e-8b06921dd5d7` |
| Attempt | `8a99683a-4365-4d8c-b50f-b39b33156d3d` |
| Snapshot | `850658c1-52c5-41f7-93e6-8b1f2911b3bc` |
| Snapshot and manifest SHA-256 | `0785ed184161ce77150b101d0049bf368921f14fb591f020b462b9dba3ac97ed` |
| Workflow SHA-256 | `10acd3ca2d7ede3567edb05b214da2c120db5f90bd2a9e62d76da23ed534d71f` |
| Plan SHA-256 | `b00428a2b65790adc933751211109c2ad1f86c62cb7e1c329a240a8c65aedf53` |
| Event SHA-256 | `2b24b4e3731e88ba91aa8611477a18cd5baac977c51bc89d542952f41a0c7efb` |
| Image | `sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8` |
| Node directory SHA-256 | `cda9ff6099e6eeb9fc394600bd930a1f75fa525cc93bbe7cea714471a8cdee10` |
| Node version | `v24.21.0` |
| setup-uv commit | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| setup-uv action SHA-256 | `0d06cc3d1548074d7b9bc86a682b10d13c2859157c27c16551eb904e9a130839` |
| Network | `bridge` |
| Accepted | `2026-10-04T22:03:35.876513+00:00` |
| Started | `2026-10-04T22:03:36.074381+00:00` |
| Finished | `2026-10-04T22:07:28.181072+00:00` |
| Worker | `ed794708-79cf-443a-bd49-e78490d005c7` |
| `follow` exit | 0 |

| Index | Name | Status | Exit |
| --- | --- | --- | --- |
| 0 | Checkout | succeeded | 0 |
| 1 | Install uv and Python | succeeded | 0 |
| 2 | Sync the lockfile | succeeded | 0 |
| 3 | Ruff | succeeded | 0 |
| 4 | Unit tests | succeeded | 0 |
| 5 | Install uv and Python | succeeded | 0 |

Step 1 installed uv 0.12.18. Step 3 printed `All checks passed!` and
`42 files already formatted`. Step 4 printed `Ran 374 tests in
228.302s` and `OK`. Step 5 is the setup-uv `post`. It ran.

GitHub's hosted `check` for the head SHA completed `success` for
`pull_request`:
https://github.com/moonbase2090/rookrunner/actions/runs/37238507131/job/111542313208

The hosted `push` check on that same SHA also completed `success`:
https://github.com/moonbase2090/rookrunner/actions/runs/37238493788/job/111542276928

Pull request 49 is closed. It is the observation, not this record.
https://github.com/moonbase2090/rookrunner/pull/49

## Commands

`$REPO` is the checkout that holds the project virtualenv. The working
directory was a checkout of
`ca3cb1bb323c77f5ef9244b771317aaa78414d47`, so `PYTHONPATH=src` is that
tree. `--network` was omitted. The credential flag named an operator
file outside the repository and the state directory. The file's path
is not recorded here.

```
PYTHONPATH=src $REPO/.venv/bin/python -m execution_core --state /private/tmp/rookrunner-ns43-state worker --repository /private/tmp/rookrunner-ns43-clone --docker-socket --node24 /private/tmp/rookrunner-ns38-build/node/node-v24.21.0-linux-arm64
```

```
PYTHONPATH=src $REPO/.venv/bin/python -m execution_core --state /private/tmp/rookrunner-ns43-state poll --repository moonbase2090/rookrunner --clone /private/tmp/rookrunner-ns43-clone --job .github/workflows/check.yml check --image sha256:b6a558fc0e0310b8579fbe31f0a09ee413daf422d586a7c3f8e66bdbbbd4bbb8 --credential-file <operator file>
```

`follow ecc9c190-5f7f-4a63-9451-d67c914e6df4` exited 1.
`follow 7c355019-c502-45da-867e-8b06921dd5d7` exited 0.
