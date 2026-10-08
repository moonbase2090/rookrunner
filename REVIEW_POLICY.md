# Review policy

This policy applies to every pull request, whether a person or an agent wrote it.

## 1. Classify every PR

**Trunk**: the change touches shared code that other parts depend on:

- core libraries and shared modules
- auth, secrets, and permissions
- data models, schemas, and migrations
- CI, release, and signing workflows; build configuration
- public APIs, CLI flags, config formats
- anything with 5 or more dependents

In rookrunner, trunk includes:

- Shared modules under `src/execution_core/` with 5 or more files in `src/` and `tests/` that import them: `protocol.py` (38), `worker.py` (24), `cli.py` (23; CLI flags), `plan.py` (14), `run.py` (12), `snapshot.py` (10), `expr.py` (10), `status.py` (9), `poll.py` (9), `checks.py` (8), `actions.py` (8), `verify.py` (6), `attempt.py` (6), `artifacts.py` (6), `disk.py` (5), `commands.py` (5).
- Secrets, auth, and permissions: `secrets.py`, `job_token.py`, output masking in `commands.py`, the worker flags `--network` and `--docker-socket`, and the lockdown sign-off paths below.
- Contracts and APIs: `schemas/v0/` (`contract.schema.json`, `examples.json`), `docs/design/protocol.md`, and `mcp.py` (the MCP tool surface).
- Runner image and worker deployment: `images/ubuntu-runner/Dockerfile`, `deploy/rookrunner-worker.service`.
- CI and the sign-off paths in `.github/CODEOWNERS`: `.github/` (workflows, `signoff.py`, CODEOWNERS), `pyproject.toml`, `uv.lock`, `docs/lints.md`, `docs/conventions.md`, `docs/design/static-analysis.md`, `docs/development-dependencies.md`. A pull request that changes one of these also needs the `mb2090-signoff` label.

**Leaf**: everything else. Examples: a dashboard-only change in `dashboard.py` with no API or protocol change, one test file, `docs/validation/`, `docs/research/`, or a design doc that is not a sign-off path.

A PR that is partly trunk is trunk. When unsure, call it trunk. Label trunk PRs `trunk` and leaf PRs `leaf`.

## 2. Proof (every PR)

The PR body has a **Proof** section with real evidence: test output, a CI run link, a screenshot or recording for UI changes, or a before/after for behavior changes. "Tested locally" without output is not proof.

## 3. Leaf PRs

- CI green and proof present.
- One review by anyone other than the author (person or agent).
- Merge once green.

## 4. Trunk PRs

- CI green, and the proof shows the change running, not just compiling.
- Agentic validation: an agent other than the author builds and exercises the change.
- Independent review, preferably by a different model than the author.
- Every finding is fixed or explicitly waived in the PR thread.
- Then merge.

## 5. Feature gating

New behavior in trunk code ships behind a flag or setting that is off by default, unless it is a pure fix. Turning a flag on by default is its own PR, and that PR is trunk.

A risky capability stays off unless the operator turns it on. `--docker-socket` works that way: the worker does not mount the Docker socket unless that flag is set.

## 6. Tests

For every test, ask: would it fail if the behavior it names broke? If not, reject it. Reject tests that:

- assert a value against itself, or a constant against the same constant
- compute the expected value with the code under test or a copy of its logic
- mock or stub the unit under test, then assert what the mock returns
- only check that something ran, was called, or did not raise, with no assertion on the result
- compare against a snapshot or golden file regenerated from current output without review
- still pass when the implementation is deleted or replaced with a stub or default

These are the no-tautological-tests rules in `AGENTS.md`.

## 7. Merging

- Merge commits only. No admin overrides.
- Code-scanning threads are resolved only when they are report-only or addressed, never just to unblock a merge.
- Releases and tags need owner approval.
- A pull request that changes a path in `.github/CODEOWNERS` also needs the `mb2090-signoff` label. `.github/signoff.py` enforces it.
- A successful run is a terminal succeeded result with `exit_code=0` for the identified input snapshot. Queued, running, interrupted, and unknown outcomes are not successful runs (`AGENTS.md`).
