# Lints

Status: skeleton, accepted with the static analysis plan on
2026-10-06. The source is `docs/design/static-analysis.md`.

Three tables. The pull request that changes a table updates it
here. Ruff's select is unchanged. These rows are not ruff
configuration yet.

On `bbc0cb8b17522cebfb2b212da54243d7d4a3f434`, `src` and `tests`
contain no `# noqa` and no `# type: ignore`.

## Rules

A mistake seen twice adds a row: the enforcer, the mistake, and
the pull request that added it.

| Enforcer | Mistake | Pull request |
| --- | --- | --- |

No rows yet.

## Suppressions

Every `# noqa` and `# type: ignore` in `src` and `tests` is a
row: location, code, reason, and pull request. The same table
lists per-file ignores. A suppression without a row fails CI
once the inventory check exists. A row whose suppression is gone
fails that check too.

The rows below are the ones the plan already names. The spawn
modules are the `src/execution_core` modules that call
`subprocess.run` on the commit named above.

| Location | Code | Reason | Pull request |
| --- | --- | --- | --- |
| `src/execution_core/cli.py` | T20 | `cli.py` may print. It is the CLI's stdout. | The T20 pull request |
| `src/execution_core/worker.py` | T20 | The worker may print the host-control warning to stderr. | The T20 pull request |
| `src/execution_core/actions.py`, `attempt.py`, `poll.py`, `run.py`, `snapshot.py`, `socket_lock.py`, and `tests/` | S603, S607 | These modules own the current process spawn. A finding in any other module fails. | The bandit pull request |
| Fixture token strings in `tests/`, and `TOKEN_EXPIRY_WARNING` in `src/execution_core/job_token.py` | S105 | Those strings are not a credential to delete. | The bandit pull request |
| `src/execution_core` modules not yet on the strict type-check list | type check | Tests stay off until the `src` exemptions are empty. A module leaves this row in its own pull request. | The type-check pull requests |
| `tests/` | ANN, D | Tests are not held to ANN or D. They are held to S except the spawn and fixture rows, and to ERA, PGH, RUF, B, UP, SIM, and T20. | The pull requests that select those families |

`S101` has five findings on the plan's measurement. They are
fixed or listed one by one. They do not get a blanket test
ignore.

## Checks

| Check | Runs | Rejects |
| --- | --- | --- |
| Ruff | `check.yml` runs `ruff check src tests` and `ruff format --check src tests` | Default ruff findings on `src` and `tests`, and format drift. Line length is 100. Target is py311. |
| Unit tests | `check.yml` runs `PYTHONPATH=src python -m unittest discover -s tests` | A failing test. |

Checks marked CI (planned) in `docs/conventions.md` are absent
from this table until the pull request that adds them.
