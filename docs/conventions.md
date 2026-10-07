# Conventions

Status: accepted with the static analysis plan on 2026-10-06.
The source is `docs/design/static-analysis.md`.

One way for each row. Routing around a row is a failed review.
Changing a row changes this file and needs the label
`mb2090-signoff`.

A row marked CI has a mechanical check in the pull request that
introduces it. Until that check exists, the row says CI (planned).
Review rows stay with Muse until a check exists.

| Rule | Check |
| --- | --- |
| One module per file under `src/execution_core`. No new package until a signed-off boundary change. | CI (planned) |
| Tests are `unittest` modules under `tests/`. No pytest. | CI (planned): PT stays off; a check rejects a pytest import |
| A test does not pass `dir="/private/tmp"` to `TemporaryDirectory`. GitHub-hosted runners have no `/private/tmp`. | CI (planned) |
| A test Git identity is `Fixture` and `fixture@example.invalid`. | Review |
| A test does not read `~/Secrets` and does not contact `api.github.com`. `HOME` is a temp directory only when the test touches a key path. | CI (planned) for the two banned strings |
| Runtime dependencies are `pyyaml` only. Workflow YAML uses a `SafeLoader` subclass. `yaml.load`, `yaml.unsafe_load`, and `CLoader` stay unused. | CI (planned) |
| User-facing JSON is `protocol.canonical`, printed from `cli.py`. | CI (planned): T20, with `cli.py` as the listed print site |
| The worker may print the host-control warning to stderr. That call is one row in `docs/lints.md`. | CI (planned) |
| Socket mode stays `0600`. The state directory stays `0700`. | Review, already covered by tests |
| The capability version stays 12 until a signed-off pull request changes it. `write` stays rejected. | Review |
| Formatting is ruff, line length 100, target py311. | CI, already. `check.yml` runs `ruff check src tests` and `ruff format --check src tests`. |
| Suppression is `# noqa: CODE` or `# type: ignore[code]` with a reason, and a row in `docs/lints.md`. A bare `# noqa` or a bare `# type: ignore` fails. | CI (planned) |
| Subprocess calls that are the engine's process spawn stay in the modules that already own them. A new module does not add one. | CI (planned): S603 and S607 |
| Explanatory comments stay. Commented-out code stays banned. Docstring lint stays off. | CI (planned): ERA. No comment scanner. |
