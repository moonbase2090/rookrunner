# M4 downloadable preview

Status: proposed. This document is a plan. It does not build a
wheel, choose a license, sign a tag, or publish a release. It
does not change the dogfood hosts. Paused items stay paused.

Grok signs off on this plan before a later pull request starts.
Capability version stays 12. `.github/workflows/check.yml` is
unchanged.

## What the preview is

The preview is version 0.1.0. It is a GitHub release marked as a
preview. The release carries two files built from the tagged
commit:

- a wheel that `pip` and `uv` can install
- a source tarball

`SHA256SUMS` lists both files. The tag is annotated and signed.
A release workflow builds the files and attaches them.

The current package is `execution-core` at `0.0.1`. The import
package is `execution_core`. `__version__` in
`src/execution_core/__init__.py` is the string the status page
shows. Both hosts showed `0.0.1` in the dogfood start record.
There is no `[build-system]` table, so this checkout does not
build a wheel. There is no `LICENSE`, `NOTICE`, `SECURITY.md`,
or `CHANGELOG`.

Publishing the preview on GitHub is not a PyPI registration.
The public name stays the working title. The distribution name
stays `execution-core`.

## Accepted facts

The roadmap's M4 exit is still the packaged artifact, P01–P12
on a clean machine, and one external install-to-result run.
This preview is the first artifact. It does not claim that exit.

The only runtime dependency is `pyyaml==6.0.3`. Development
tools stay out of the wheel. The notice inventory is
[development dependencies](../development-dependencies.md).
That inventory is not a project license.

The worker unit is
[deploy/rookrunner-worker.service](../../deploy/rookrunner-worker.service).
`WorkingDirectory` is `/var/lib/rookrunner/engine`.
`PYTHONPATH` is `/var/lib/rookrunner/engine/src`. `ExecStart`
is `/usr/bin/python3 -m execution_core` with the state directory
and the clone. The repository copy has no image digest, no key
path, no `--app-key`, no `--docker-socket`, and no listen
socket. A host copy appends that host's image digest and still
has none of those other fields.

The second dogfood host letter installs an engine tree with `git archive`,
writes a user `.pth`, and installs PyYAML for that interpreter.
The dogfood hosts are past that letter. Their running sources
match `1b686e71990ec24c28d6141e715145ff3dca9650`. The record is
[Dogfood start](../validation/dogfood.md).
The 14-day window is open. This plan leaves that install in
place.

Untrusted code is implemented. A fork, a head repository other
than the owner repository, or an author or committer who is not
`OWNER`, `MEMBER`, or `COLLABORATOR` is refused. The warning
names the reason. No run is created. `--allow-untrusted` with
that commit's SHA allows one invocation. The flag is not stored
and it is not the default. The run record stores
`untrusted_override` and `untrusted_reason`. A missing
association is not a rejection. The comparison uses integer
repository ids. A null head repository is a fork. The rule is
in [the roadmap](../roadmap.md#planned-paused) and in
[next steps](next-steps.md). It is not in `README.md`.

Reviews require a Proof section. A release or a tag needs owner
approval. A pull request that changes `pyproject.toml`,
`uv.lock`, or `.github/` also needs the `mb2090-signoff` label.
This seat does not add that label.

## Proposed decisions

These are proposals. Sign-off accepts or changes them. They are
not decisions until then.

1. Version `0.1.0` replaces `0.0.1` in `pyproject.toml` and in
   `__init__.py` in one later pull request. The two strings
   stay equal. The capability version stays 12.
2. The wheel and the tarball are built from the tagged commit
   with `uv`. The wheel does not vendor PyYAML, Python, Node,
   or a runner image. An install of the wheel pulls
   `pyyaml==6.0.3`.
3. The project license stays unselected. The PRD's position is
   a permissive license, not a selection. No later pull request
   adds `LICENSE` until MB2090 names the license. The preview
   is not published without that file.
4. `NOTICE` names PyYAML and points at its MIT terms. It does
   not list development tools, because the wheel does not
   redistribute them. Updating
   `docs/development-dependencies.md` is a protected path and
   stays out of the `NOTICE` pull request.
5. MB2090 signs the annotated tag `v0.1.0`. This seat does not
   sign. The private key stays out of the repository. The
   release workflow runs `git verify-tag` and fails closed when
   verification fails. That step needs the public half of the
   signing key, supplied by MB2090, in the workflow pull
   request. This plan does not choose GPG or SSH signing.
6. The GitHub release is a prerelease named `v0.1.0` preview.
   Its body is the release notes. Assets are the wheel, the
   tarball, and `SHA256SUMS`.
7. Clean-environment acceptance is a fresh `ubuntu:24.04`
   `linux/amd64` container. The checkout is not mounted. The
   only install is the wheel into a new virtualenv. The
   acceptance starts the worker with `--network none`, runs
   `describe`, and stops the worker. The record names the
   platform, the wheel hash, the version string `0.1.0`, and
   the exit code. Exit code 0 and a JSON result are the pass.
   That is install evidence. It is not a P01–P12 claim and it
   is not an external-user run.
8. Install documentation has two procedures. A developer
   machine installs the wheel and runs `python -m execution_core`
   with no `PYTHONPATH`. A host that follows the dogfood host layout
   unpacks the tarball into
   `/var/lib/rookrunner/engine` so the existing unit's
   `PYTHONPATH` still finds `src`. That host still installs
   `pyyaml==6.0.3` for `/usr/bin/python3`, keeps state mode
   `0700`, and keeps the socket mode `0600`. The unit file in
   the repository stays as it is. Neither procedure runs on
   the dogfood hosts in the pull request that adds the doc.

## Readiness

Checked against `origin/main` at
`21d1e950415c4eb90e8709fba02e260e03579852` while drafting this
plan. Nothing in this list is rewritten here.

`README.md` still matches the checkout command:
`PYTHONPATH=src python3 -m execution_core`. Three claims are
stale:

- The status says M1 is complete and M2 has started. The
  roadmap records M3 implemented.
- The closing paragraph says packaging, MCP, and the dashboard
  remain on the roadmap. MCP and the dashboard are implemented.
  Packaging is this plan.
- The fixture paragraph says no third-party dependency is
  required. Importing the worker imports `plan.py`, which
  imports `yaml`. Python 3.14 with this tree on `sys.path` and
  without PyYAML raises `ModuleNotFoundError` at that import.

`SECURITY.md` is absent. The later file states the local model
already in the architecture: the socket is mode `0600`, the
state directory is mode `0700`, and the worker has no network
listener. It tells a reporter to use GitHub private
vulnerability reporting for `moonbase2090/rookrunner` and to
keep secrets out of public issues. It repeats the PRD line that
local mode is not a sandbox for a hostile repository.

`CHANGELOG` is absent. The later file uses Keep a Changelog
headings. The `0.1.0` section is written with the release
notes, when the artifact hash exists. It does not invent a
backlog of earlier milestones.

The untrusted refusal is documented in the roadmap and in next
steps. A later `README` section quotes that rule: the warning
names the reason, no run is created, and `--allow-untrusted`
with the SHA is the only override.

## History scan

The scan used `gitleaks detect` with `--log-opts=--all` and
`trufflehog git` with `--no-verification` over the same
history. History was not rewritten.

Commit authors are `MB2090` at
`322824348+mb2090@users.noreply.github.com` and `GitHub` at
`noreply@github.com`.

The current tree has no home-directory path. Two historical
commits of the dogfood record still show a checkout path in
`git log -p`: `eb2426ae24810814ea417404218dd172fccf8333` added
it, and `92b09d53610d83217fb4b0923084ace812ca3de8` removed it
from the files. The finding is reported to grok with this
plan, before any rewrite. This plan leaves those commits as
they are.

The dogfood evidence also quotes the setup-uv log. That log
names the Actions runner tool directory. It is not a person's
home directory.

Gitleaks reported four hits. All four are still explained by
the current tree:

- `private-key` at `src/execution_core/checks.py` is the PEM
  header text the parser accepts. The file contains no key
  material.
- `generic-api-key` on three `submission_key` strings in
  `docs/validation/dogfood-check-evidence.json` and
  `docs/validation/m1-completion-evidence.json`. Those values
  are fixture submission keys.

Trufflehog reported zero verified secrets and one unverified
hit. The hit is the fixture URL in `tests/test_status.py` that
`status_url` rejects because it contains a user and a
password. The URL is not a credential.

No credential is rotated and no file is rewritten for these
hits.

## Later pull requests

Each one starts from fresh `origin/main` after the previous
one has merged. Each has a Proof section. This seat does not
merge, does not force-push, and does not add
`mb2090-signoff`.

| Order | Change | Class |
| --- | --- | --- |
| 1 | Correct the three stale `README` claims and document the untrusted refusal | Leaf |
| 2 | Add `SECURITY.md` | Leaf |
| 3 | Add `CHANGELOG` with the preview section | Leaf |
| 4 | Add `LICENSE` and `NOTICE` after the license is named | Leaf |
| 5 | Add a build backend, set version `0.1.0`, and build the wheel and the tarball | Trunk. `pyproject.toml` and, if the lockfile changes, `uv.lock` |
| 6 | Add install documentation for the wheel and for the host layout | Leaf |
| 7 | Add the release workflow and `SHA256SUMS` generation | Trunk. `.github/` |
| 8 | Record the clean-container acceptance | Leaf |
| 9 | Sign `v0.1.0` and publish the prerelease | Owner. This seat does not sign |

Pull requests 4, 7, and 9 wait on the sign-off answers below.
Pull request 1 can start as soon as this plan is accepted.

The release workflow triggers on the tag `v0.1.0`. The build
job uses `contents: read`. A separate publish job uses
`contents: write` and only uploads the three assets to the
prerelease. The workflow does not request `actions: write`.
It does not run a macOS job, `actions/cache`, a Docker action,
`actions/setup-node`, or `hashFiles`. `git verify-tag` is a
required step.

`SHA256SUMS` is two lines in `sha256sum` format, one for the
wheel and one for the tarball. The acceptance record quotes
those lines.

## Release notes

The notes that ship with `0.1.0` say at least this:

- The artifact is a preview. It is not a claim of GitHub
  Actions equivalence.
- The version string is `0.1.0`. The capability version is 12.
- The acceptance platform is Linux x86_64. Other platforms are
  not claimed. The acceptance interpreter is Python 3.12. The
  package requires Python 3.11 or newer.
- PyYAML 6.0.3 is installed with the wheel and is not vendored.
- There is no doctor command. P02 remains unmet.
- The acceptance run is install, worker start, `describe`, and
  stop. P03–P12 are not re-measured on the wheel in this
  preview.
- macOS jobs, `actions/cache`, Docker actions,
  `actions/setup-node`, and `hashFiles` stay paused during the
  initial dogfood period.
- Untrusted code is refused unless that poll invocation passes
  `--allow-untrusted` with the SHA.
- The worker has no listen socket. Write permissions stay
  rejected. A webhook receiver and GitHub runner registration
  stay out.
- The self-hosted Linux dogfood install is unchanged. The page
  there can keep showing `0.0.1` until a later host change,
  which this preview does not include.
- The App private key is not in the artifact.
- The public package name and a package-index upload stay
  undecided.

## Sign-off answers this plan needs

1. The license name, before pull request 4.
2. The signing key's public half, and whether GPG or SSH
   signing is the one `git verify-tag` will check.
3. Confirmation that history stays as it is. The checkout path
   remains visible in `git log -p` for `eb2426a` and `92b09d5`.
4. Confirmation that the four gitleaks hits need no rotation
   and no rewrite.
5. Confirmation that the dogfood hosts stay on the recorded
   install for the rest of the window.
