# Runner image for ubuntu-latest

Status: implemented. `worker --runner-image` supplies one operator-built
digest when a submit or poll omits `image` and every selected job has
the literal `runs-on: ubuntu-latest`. An explicit `image` still wins.
The image contract is [images/ubuntu-runner/Dockerfile](../../images/ubuntu-runner/Dockerfile).
The capability version stays 12. A version 11 plan is not migrated.
No plan field is added. `.github/workflows/check.yml` is unchanged.
`run_job` still requires a digest and still does not select a default.
This is not a GitHub-equivalence claim. P5's credential is the
dedicated Rookrunner GitHub App recorded in
[check runs](check-runs.md). This document does not create that App.

The implementation follows this document.

## Why this slice is a design

The local-worker answer is the implemented behavior above. Workflow
execution needs an image pinned by digest. `run_job` requires the
caller to pass one and does not select a default. The worker supplies
the operator digest from `--runner-image` when `image` is omitted.
Publishing stays rejected. Decision 0002 still marks provenance
pending for every other image
([PRD](../prd.md),
[decision 0002](../decisions/0002-m2-capture-and-backend.md)).

NS-4 accepts that caller digest. `runs-on` does not select an image.
`runs-on: ubuntu-latest` is an accepted label. The job container is
created with `--user` set to the caller uid and gid. The entrypoint is
`sh`. The container is not privileged. Host credential directories are
not mounted. The Docker socket stays unmounted unless the worker was
started with `--docker-socket`. When that socket is mounted, the image
must already contain the Docker client. This engine does not install
packages and does not pull a floating tag. The run record stores the
image digest with its `sha256:` prefix.

A step that runs `sudo` or `apt` needs both a Debian package manager
and a uid that `sudo` can resolve. The caller uid is chosen when the
container is created. It is not known when an image is built, and it
is not in the image's passwd. `sudo` then fails before `apt` runs.
GitHub-hosted `ubuntu-latest` avoids that because its `runner` user
has passwordless sudo. That image is a virtual-machine image from
[actions/runner-images](https://github.com/actions/runner-images), not
an OCI digest this engine can pin. Tool parity with it is a separate
environment milestone
([runner requirements](../research/actions-runner-requirements.md),
ER-23).

On 2026-10-05, GitHub's `ubuntu-latest` label is Ubuntu 24.04. GitHub's
changelog of 2026-09-17 says the label migrates to Ubuntu 26.04 between
2026-10-19 and 2026-11-19
(https://github.blog/changelog/2026-09-17-ubuntu-26-generally-available-and-latest-migration).
This engine does not follow that label. `.github/workflows/check.yml`
uses `ubuntu-latest` and does not run `sudo` or `apt`. Its identified
Rookrunner run already succeeded with a caller-pinned image. This
design does not change that workflow.

P5 designs check runs through the Rookrunner GitHub App and does not
change the engine ([check runs](check-runs.md)). The App is not
created yet. This document does not create it.

## Options

### A. A written image contract. The caller pin stays required

The repository documents the packages an image needs. The operator
builds it and passes the digest to `--image`, as today. `ubuntu-latest`
stays a label. The engine gains no flag and no default.

This keeps provenance with the operator and changes no submit path.
It does not answer the PRD's request for a project default. Every
submit and every poll still carries a digest.

### B. An operator digest on the worker. Recommended

The operator builds the image in option A and starts the worker with
that digest. A submit or poll that omits `image` uses the digest when
every job in the selected plan has `runs-on: ubuntu-latest`. An
explicit `image` still wins. The engine does not pull, build, or
publish. A missing local image is the existing setup failure
`image digest will not resolve`.

This is the same shape as `worker --node24`: the operator supplies the
bytes, and the worker does not download them. The plan shape does not
change. The run record stores whichever digest actually ran.

### C. The engine pulls or publishes a registry image

A later milestone could publish one digest and have the worker pull
it. Publishing packages is a separate milestone from this repository's
rules. The worker does not download Node. It should not download a
runner image either. This option is not the recommendation.

### D. GitHub's ubuntu-latest image, or a community mirror of it

`actions/runner-images` publishes virtual-machine images. They are
large, they gain and drop tools on a weekly cadence, and the
`ubuntu-latest` label moves on GitHub's calendar. Using one would
claim tool parity this project does not claim. This option is not the
recommendation.

### E. Run the job as root

Root can run `apt` without `sudo`. The accepted container is created
as the caller uid and is not privileged. Root steps would change that
model, and a root job that also has the Docker socket can drive the
host engine. This option is not the recommendation.

## Recommendation

Option B. The image contract is option A. Options C, D, and E are
recorded so a later slice does not reopen them.

The implementation does the following.

1. **Image contract.** One Dockerfile, built by the operator, not by
   this engine and not by `.github/workflows/check.yml`. The base is
   one Ubuntu LTS digest. The open decision below names the release.
   The packages are `bash`, `git`, `ca-certificates`, `sudo`, and the
   Docker client. `apt` is the base image's package manager. The image
   does not install language toolchains, a GitHub tool cache, or the
   `actions/runner` agent. The operator's build is the provenance.
   The base digest is recorded in the Dockerfile. Ubuntu is not
   vendored. A built image is not published.

2. **Worker flag.** `worker --runner-image DIGEST` stores one
   digest-pinned reference, the same forms `run_job` already accepts
   (`sha256:` plus 64 hex characters, or `name@sha256:` plus 64 hex
   characters). The worker does not pull it. `worker.describe` reports
   the digest only when the flag is set. A digest that does not match
   those forms fails worker start.

3. **Which image runs.** An explicit submit or poll `image` is the
   image, as today. When `image` is omitted and the flag is set, the
   flag's digest is used only if every job in the accepted plan has
   the literal `runs-on: ubuntu-latest`. Any other label, including
   `ubuntu-24.04`, `ubuntu-22.04`, `ubuntu-26.04`, and `macos-14`, is
   `CAPABILITY_UNSUPPORTED` and names `runs-on`. No run is created.
   When `image` is omitted and the flag is unset, the result is the
   existing rejection `image is not pinned by digest`, and no run is
   created. Service-container images stay caller pins. They are not
   this image.

4. **Local image only.** The job container is created only after the
   existing inspect path resolves the digest. A missing image is
   `SETUP_FAILED` with `image digest will not resolve`. There is no
   `docker pull`, `docker build`, or registry request on that path.

5. **Caller uid and sudo.** Steps stay `--user` of the caller uid and
   gid. The container stays unprivileged. When the resolved digest is
   the flag's digest, the passwd and sudoers entries live as two host
   files in a new attempt directory, `runner-account`, mode 0700,
   beside `home`, `runner-temp`, and `tool-cache`. That directory is
   outside the workspace, so it is not in the artifact manifest.
   `run_job` first runs one short root container, `--user 0:0`,
   `--rm`, entrypoint `sh`, the same shape as the socket-volume chown.
   The container bind-mounts `runner-account` read-write at
   `/runner-account` and mounts nothing else: not the workspace, not
   the Docker socket, and not host credentials. It copies the image's
   `/etc/passwd` to `/runner-account/passwd`, mode 0644, owned by uid
   0. When the caller uid is missing from that copy, it appends one
   line. The line's name is `runner-` plus the decimal uid, its uid
   and gid are the caller's, its home is `/github/home`, and its
   shell is `/bin/sh`. The password field is empty, so `sudo` does
   not treat the account as locked. A name that is already present
   fails setup. It
   writes `/runner-account/sudoers` as uid 0, mode 0440, for that uid
   only: `<name> ALL=(ALL) NOPASSWD: ALL`, which is decision 2. If `sudo` is
   missing or either write fails, setup fails and no step runs.
   `--rm` deletes that container and not the host files. The job
   container bind-mounts `runner-account/passwd` read-only at
   `/etc/passwd` and `runner-account/sudoers` read-only at
   `/etc/sudoers.d/rookrunner`. Those mounts are how the job sees the
   entries. The worker does not write the files. sudo ignores a
   sudoers file that is not owned by uid 0, and the worker is not
   root. A different caller image does not get that command or those
   mounts. Existing images keep today's behavior.

6. **What stays put.** The capability version stays 12. A version 11
   plan is not migrated. No plan field and no protocol version are
   added. The plan schema is unchanged. `worker.describe` gained an
   optional `runner_image`, reported only when the flag is set, because
   that result rejects unknown properties. Version 1 submit `image`
   stays in the schema and is no longer required. The run record
   already stores the image digest. `.github/workflows/check.yml` stays
   unchanged. The dogfood caller still passes `--image`. `GITHUB_TOKEN`
   stays unset. `security-events: write`
   and `actions: write` stay rejected. The P5 implementation and P7
   stay unstarted.
   macOS stays deferred.

## Open owner decisions

The implementation takes each recommendation below.

1. **Base release.** Recommend Ubuntu 24.04, pinned by digest in the
   Dockerfile. On 2026-10-05 that is the release behind GitHub's
   `ubuntu-latest` label. Ubuntu 26.04 is generally available and
   becomes that label during 2026-10-19 through 2026-11-19. The
   implementation does not track the label. The owner can name 26.04
   instead.
2. **sudoers scope.** Recommend `NOPASSWD` for every command, for the
   caller uid only. That matches the hosted `runner` user. The owner
   can limit the command list to `apt` and `apt-get`. All-command
   `NOPASSWD` plus the Docker client in this image composes with
   `--docker-socket` into control of the host engine. The credential
   choice in P5 inherits that warning.
3. **Docker client.** Recommend installing it. Socket jobs already
   require the client, and the unit tests in `check.yml` use it. The
   owner can omit it and keep a second image for socket jobs.
4. **Omitting `image`.** Recommend allowing the omission when
   `--runner-image` is set and the plan is all `ubuntu-latest`. The
   owner can keep `image` required on every submit and poll. The
   Dockerfile then stays a contract the operator pins by hand, which
   is option A.

## What the implementation proves

1. A worker started with `--runner-image` accepts a submit that omits
   `image` for a job whose `runs-on` is `ubuntu-latest`. The run
   record stores that digest with the `sha256:` prefix.
2. An explicit `image` is the digest that runs. The flag does not
   replace it.
3. An omitted `image` with no flag creates no run.
4. An omitted `image` and `runs-on: macos-14` is
   `CAPABILITY_UNSUPPORTED`, names `runs-on`, and creates no run.
5. A digest that is not present locally is `SETUP_FAILED`. The engine
   does not pull it.
6. On the runner image, the step uid is the caller uid, `sudo -n true`
   exits 0, and `sudo apt-get --version` exits 0. The step is not uid
   0. The container is not privileged. The passwd and sudoers the step
   uses are the host files under `runner-account`, bind-mounted
   read-only. Removing the root container does not remove them.
7. A different caller image does not gain a sudoers entry.
8. The capability version stays 12. `.github/workflows/check.yml` is
   unchanged. `security-events: write` and `actions: write` still fail
   planning.

## What this implementation does not do

The engine does not build, pull, or publish the image. The operator
builds the Dockerfile. No root job. No privileged container. No host
credential mount. No change to `runs-on` for a caller who passes
`image`. No macOS image. No `actions/runner` agent. No `GITHUB_TOKEN`.
The P5 implementation and P7 stay unstarted. The default capture
still excludes the original commit. Decision 0002 still holds for every image that is
not this operator digest: the caller passes the pin.
