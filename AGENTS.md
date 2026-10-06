# Project instructions

The working title is Rookrunner. Treat `execution-platform` as a temporary directory name.

Read README.md, docs/prd.md, docs/design/architecture.md, and docs/roadmap.md before implementation.
Document proposed decisions separately from accepted direction and implemented behavior.

This repository is independent of Local Actions. Do not modify that checkout as part of this project.
Do not copy its machine configuration, credentials, project identity, mailbox registration, or historical operating procedures.
Check source provenance and license obligations before importing code or bundled binaries.

Use neutral internal identifiers until naming is settled. Do not publish packages or create remote repositories from placeholders.
Keep secrets and execution state out of version control.

A successful run requires a terminal succeeded result and exit_code=0 for the identified input snapshot.
Queued, running, interrupted, and unknown outcomes are not successful runs.
Use focused checks for each implementation change and record what was actually validated.

Write no tautological tests. A test must fail when the behavior it covers breaks.
Do not assert a value against itself, or a constant against the same constant.
Do not compute the expected value with the code under test or a copy of its logic.
Do not mock or stub the unit under test and then assert what the mock returns.
Do not write a test whose only check is that something ran, was called, or did not raise.
Do not regenerate a snapshot or golden file from current output without reviewing it.
Reject any test that still passes when the implementation is deleted or replaced with a stub or default.
For every test, reviewers ask: would it fail if the behavior it names broke?

For UI implementation, prepare runnable design options before choosing the visual direction.
Treat remote execution and serving unrelated customers as separate design milestones.
