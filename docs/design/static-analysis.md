# Static analysis lockdown

Status: accepted, 2026-10-06. MB2090 signed off in
https://github.com/moonbase2090/rookrunner/pull/81#issuecomment-6025512879.
The answers are 1A, 2A, 3B, 4A, 5A, 6A, 7A, 8A, and 9A.
This pull request is docs only. It does not merge itself.
Implementation pull requests wait until this one has merged.

The model is lunatui's `docs/conventions.md`, `docs/lints.md`, and
pull request 6 on `moonbase2090/lunatui`: one canonical way for each
recurring thing, no second way that a reviewer is asked to notice,
and a check that fails the pull request when the rule is broken.
Lunatui enforces that with rustfmt, denied lints, an `#[expect]`
inventory, a dependency allowlist, CODEOWNERS, and a sign-off
workflow that requires the label `mb2090-signoff`. This plan is that
shape for Python. It does not copy Rust rules that have no Python
subject.

## What is already true

Measured on `7996755b04b58f8a80b00e1565c371e2c7a79fef` with ruff
0.12.12, the version pinned in `pyproject.toml`.

`pyproject.toml` sets ruff `line-length` 100 and `target-version`
py311. It selects no extra rule families, so ruff's default set
runs. `.github/workflows/check.yml` runs `ruff check src tests`,
`ruff format --check src tests`, and `unittest discover -s tests`.
The workflow pins uv 0.12.18 and Python 3.12. `requires-python` is
`>=3.11`. That uv has two relevant commands, `uv audit` and
`uv check`. `uv check` type-checks with ty and takes `--ty-version`.
`uv audit` looks up the lockfile in the OSV service.

The runtime dependency list is `pyyaml==6.0.3`. The dev group is
`jsonschema==4.25.1` and `ruff==0.12.12`. `uv.lock` also records
the transitive dev distributions `jsonschema-specifications`,
`referencing`, `attrs`, `rpds-py`, and `typing-extensions`.
`docs/development-dependencies.md` is the license inventory of
that set.

`src/execution_core` is 27 modules and 19345 lines. `tests` is 39
files and 22527 lines. Thirteen source files are over 400 lines.
The largest are `run.py` (4577), `plan.py` (2319), `worker.py`
(2085), and `expr.py` (1794). Twenty-two functions are 80 lines or
longer. The longest is `run_job` at 692 lines. There is no
`# noqa`, no `# type: ignore`, and no CODEOWNERS file. Source
files contain 222 `#` comments. Tests contain 16. They are
constraint notes, security notes, and links to GitHub's docs.
There is no required license header. Choosing one belongs to M4,
not to this plan.

Ruff family counts on `src` and `tests` together, under the
repo `target-version` of py311. That is the config CI runs.
An `--isolated` run uses ruff's default py39 target and
undercounts B, UP, and SIM.

| Family | Findings | What they are |
| --- | --- | --- |
| B | 9 | bugbear; one is B905 |
| S | 257 | bandit; 226 are subprocess partial-path checks |
| UP | 35 | pyupgrade; 18 are UP038 and 14 are UP017 |
| SIM | 43 | simplify; four are SIM110 and two are SIM108 |
| C90 | 73 | mccabe, all in `src`, ruff's default limit of 10 |
| PL | 294 | pylint; 90 are magic-value comparisons |
| ANN | 4129 | missing annotations |
| D | 1280 | pydocstyle; 807 are undocumented public methods |
| ERA | 0 | commented-out code |
| T20 | 19 | `print`; 18 in `cli.py`, one stderr warning in `worker.py` |
| PT | 3693 | pytest-style rewrites of unittest assertions |
| RUF | 9 | ruff-specific |
| PGH | 0 | blanket `noqa` or `type: ignore` |

`cli.py` and `dashboard.py` import each other. `cli.py` and
`mcp.py` import each other. `protocol` imports no other
`execution_core` module. `worker` imports the engine. The engine
does not import `dashboard` or `mcp`.

The repository is public. This plan takes the task's statement
that secret scanning, push protection, and Dependabot security
updates are off. It does not turn them on. A repo admin does
that, outside a pull request.

`check.yml` is also the workflow the product dogfoods. A new step
there is a product change as well as a CI change.
`docs/design/dogfood-check.md` describes the current steps. Pull
requests below that edit `check.yml` update that note in the same
pull request. Pull requests that can be a ruff select, a dev
dependency, or a unittest leave `check.yml` byte-identical.

## Decisions

Answered on 2026-10-06: 1A, 2A, 3B, 4A, 5A, 6A, 7A, 8A, 9A.
The letter that was not chosen stays listed as the alternative.

### 1. Which type checker?

Answered: A.

- A. mypy, strict, pinned in the dev group. Accepted.
- B. pyright, strict, pinned in the dev group.
- C. `uv check` with a pinned `--ty-version`.

A and B have a defined strict mode and a per-module way to turn
it on. C is already on the uv binary CI pins, so it adds no dev
distribution, but this plan has not shown that ty's strictness
matches A or B. The pyright wheel vendors a JavaScript runtime.
mypy is one Python distribution, locked and hashed the same way
ruff is. The ratchet below is the same for A or B.

### 2. Which ruff families?

Answered: A.

- A. Enable the families that are already small or that have a
  named per-file policy, and put complexity on a ceiling at
  today's maximum. Leave PT, the whole PL family, repo-wide ANN,
  and D off. Accepted.
- B. Enable every family listed in the task, including PT, PL,
  ANN, and D.
- C. Leave ruff on its default set.

PT rewrites unittest assertions into pytest form. There are 3693
of those findings. Unittest stays the test runner, so PT stays
off. ANN is 4129 missing annotations. Turning it on repo-wide is
the type-checker migration done twice. Docstring lint stays
off because question 3 is B. The whole PL family includes 90
magic-value findings that would
rename protocol numbers. Complexity uses C90 and the argument,
branch, return, and statement codes PLR0911, PLR0912, PLR0913,
and PLR0915, not the rest of PL.

### 3. What comments are allowed?

Answered: B.

- A. The lunatui rule, translated: no comments except docstrings
  on the public surface, plus a short list of approved forms.
  The 222 `#` comments in `src` would be deleted or moved.
  Many of them record a security restriction or a GitHub limit.
- B. Explanatory comments stay. Commented-out code stays banned.
  Docstring lint stays off. Accepted.
- C. A rule written later.

ERA still lands. It flags commented-out code rather than prose.
There is no comment scanner and no D-family rollout.

### 4. Where do repeat mistakes go?

Answered: A.

- A. `docs/lints.md` gains a rule when the same mistake has been
  seen twice, in that pull request or the next one. Every
  suppression is a row in the same file. Accepted.
- B. Reviewers remember.

A suppression without a row fails CI. A row whose suppression is
gone fails CI. This is lunatui's inventory, with `# noqa: CODE`
and `# type: ignore[code]` in place of `#[expect]`.

### 5. What runs for security?

Answered: A.

- A. `uv audit --locked` in the check job, a pinned CodeQL
  workflow for Python, and an admin enabling secret scanning and
  push protection. No Dependabot. Accepted.
- B. CodeQL's default setup, `pip-audit` as another dev
  dependency, and Dependabot security updates.
- C. No new security tooling.

`uv audit` is the pinned uv, so the allowlist does not gain a
distribution. It sends the lockfile's package set to OSV. `uv
sync` already reaches PyPI from this job. CodeQL as a workflow
file is reviewed like the rest of CI. The default setup is a
repo setting and can double-run if both are on. The repository
is public, so CodeQL does not need a paid security product.
Secret scanning and push protection are repo settings. This
plan does not change them. Dependabot version updates would
open lockfile pull requests that still need sign-off, and
security updates would do the same. The audit failure is the
gate. A person opens the bump.

### 6. What coverage floor?

Answered: A.

- A. The first coverage pull request measures line coverage and
  sets the floor to that integer. Later pull requests may raise
  it. Lowering it is a protected change. Accepted.
- B. Pick a percentage in this document.
- C. No coverage gate.

This document does not invent the percentage. The floor lives in
`pyproject.toml`, which is a protected path. The gate is line
coverage. Branch coverage can be a later decision after the line
floor has ratcheted. `coverage` is a dev distribution and an
allowlist change.

### 7. How is the dependency allowlist enforced?

Answered: A.

- A. CI fails if `uv.lock` contains a distribution name that is
  not listed, direct or transitive. The runtime list stays
  `pyyaml`. A lockfile change needs sign-off even when the name
  is already listed. Accepted.
- B. Pin direct dependencies and let new transitive
  distributions through.
- C. Trust the lockfile with no name check.

The current names are the starting allowlist: `pyyaml`,
`jsonschema`, `jsonschema-specifications`, `referencing`,
`attrs`, `rpds-py`, `typing-extensions`, and `ruff`. Adding
mypy, coverage, or any later tool is a sign-off pull request
that edits the list and `docs/development-dependencies.md`
together. The checker reads `uv.lock`. It does not resolve from
the network.

### 8. What size and boundary limits?

Answered: A.

- A. Files and functions already over the limit are listed and
  may not grow. A new file is at most 400 lines. A new function
  is at most 80 lines. The import graph is baselined, and a new
  edge fails. Accepted.
- B. Fail CI now on every file over 400 lines, which means
  splitting `run.py`, `plan.py`, `worker.py`, and `expr.py`
  before the other checks.
- C. Review only.

The 400 and 80 figures are the lunatui file scale and the size
at which this tree already has 22 functions over the line.
Thirteen source files are over 400 and would be listed, not
rewritten, in the first size pull request. Tests are not under
the file cap. One unittest module holds one module's behaviors,
and several of those files are already thousands of lines.
Growth of a listed source file or function fails. The two
import cycles, `cli` with `dashboard` and `cli` with `mcp`, are
listed. New cycles fail. `protocol` gaining an `execution_core`
import fails. An engine module gaining an import of `dashboard`
or `mcp` fails. Moving `call` out of `cli.py` so those cycles
disappear is a later behavior pull request, not a requirement
for the baseline.

### 9. What protects the config?

Answered: A.

- A. CODEOWNERS plus a sign-off workflow. A pull request that
  changes a protected path fails until it has the label
  `mb2090-signoff`. Accepted.
- B. CODEOWNERS with no label check.
- C. The label with no CODEOWNERS.

Protected paths, and the only list of them, live in
`.github/CODEOWNERS`:

- `pyproject.toml`
- `uv.lock`
- `.github/`
- `docs/lints.md`
- `docs/conventions.md`
- `docs/design/static-analysis.md`
- `docs/development-dependencies.md`

The owner entry is `@mb2090`. Anyone with triage access can add
a label, so the label is not the ownership gate. CODEOWNERS
review blocks a merge only after a repo admin turns on "Require
review from Code Owners" for `main`. A new workflow blocks a
merge only after an admin marks that check required, except for
steps added to the existing `check` job, which is already the
required job. This plan does not change those settings, and it
does not create the label.

## Conventions

`docs/conventions.md` is a later pull request. It records one
way for each row. A row marked CI has a mechanical check in the
pull request that introduces it, or it is marked CI (planned)
until that check exists. Review rows are Muse's until a check
exists. Routing around a row is a failed review. Changing a row
is a pull request that touches `docs/conventions.md` and needs
sign-off.

| Rule | Check |
| --- | --- |
| One module per file under `src/execution_core`. No new package until a signed-off boundary change. | CI (planned) |
| Tests are `unittest` modules under `tests/`. No pytest. | CI: PT stays off; a check rejects a pytest import |
| A test does not pass `dir="/private/tmp"` to `TemporaryDirectory`. GitHub-hosted runners have no `/private/tmp`. | CI |
| A test Git identity is `Fixture` and `fixture@example.invalid`. | Review |
| A test does not read `~/Secrets` and does not contact `api.github.com`. `HOME` is a temp directory only when the test touches a key path. | CI for the two banned strings |
| Runtime dependencies are `pyyaml` only. Workflow YAML uses a `SafeLoader` subclass. `yaml.load`, `yaml.unsafe_load`, and `CLoader` stay unused. | CI |
| User-facing JSON is `protocol.canonical`, printed from `cli.py`. | CI: T20, with `cli.py` as the listed print site |
| The worker may print the host-control warning to stderr. That call is one row in `docs/lints.md`. | CI |
| Socket mode stays `0600`. The state directory stays `0700`. | Review, already covered by tests |
| The capability version stays 12 until a signed-off pull request changes it. `write` stays rejected. | Review |
| Formatting is ruff, line length 100, target py311. | CI, already |
| Suppression is `# noqa: CODE` or `# type: ignore[code]` with a reason, and a row in `docs/lints.md`. A bare `# noqa` or a bare `# type: ignore` fails. | CI |
| Subprocess calls that are the engine's process spawn stay in the modules that already own them. A new module does not add one. | CI: S603 and S607 |
| Explanatory comments stay. Commented-out code stays banned. Docstring lint stays off. | CI: ERA. No comment scanner. |

## Lints file

`docs/lints.md` has three tables, updated in the pull request
that changes them.

- Rules. A mistake seen twice. The enforcer, the mistake, and
  the pull request that added it.
- Suppressions. Every `# noqa` and `# type: ignore` in `src`
  and `tests`: location, code, reason, pull request. The same
  table lists per-file ignores.
- Checks. Each mechanical check, what runs it, and what it
  rejects.

Known rows the first suppression pull request will have, unless
the fix is smaller than the exception:

- `cli.py` may print. It is the CLI's stdout.
- `worker.py` may print the host-control warning to stderr.
- `S603` and `S607` are ignored on the current process-spawning
  modules and on `tests`, because those call sites are the
  spawn path. A finding in any other module fails.
- `S105` on fixture token strings in tests, and on
  `TOKEN_EXPIRY_WARNING` in `job_token.py`. Those strings are
  not a credential to delete.
- Modules not yet on the strict type-check list.

`S101` has five findings. They get fixed or listed. They do not
get a blanket test ignore while the count is that small.

## Ruff rollout

Config lives in `pyproject.toml`. `check.yml` already runs
`ruff check src tests`, so a select change does not edit the
workflow.

Order, each its own pull request, each green at the tip:

1. ERA, PGH, RUF, B, and UP. ERA and PGH are already at zero.
   RUF is 9, B is 9, UP is 35. That is 53 findings. UP017
   and UP012 are autofixable. UP038 and UP031 are manual
   and small.
2. SIM. Forty-three findings. Some join branches. The tests
   cover the touched modules.
3. T20, with the print rows above.
4. The narrow S codes `S101`, `S103`, `S108`, `S110`, and
   `S310`, plus `S105` with the listed fixtures. Then `S603`
   and `S607` with the per-file spawn policy.
5. Complexity ceilings. The pull request measures the current
   maximum C90 score and the current maximum for each PLR091*
   code, sets ruff's limits to those maxima, and records the
   numbers in `docs/lints.md`. A function that exceeds them
   fails. Lowering a maximum is a later pull request that
   splits the function. This plan does not split `run_job`.

Per-file test policy, in `pyproject.toml` and copied into
`docs/lints.md`: tests are not held to ANN or D. They are held
to S except the spawn and fixture rows. They are held to ERA,
PGH, RUF, B, UP, SIM, and T20.

`# noqa` without a code fails (PGH004). `# type: ignore` without
a code fails (PGH003). Ruff's unused-noqa check fails a code
that no longer fires. The inventory check fails a code that
ruff accepts but `docs/lints.md` does not list, and it fails a
row that no longer matches a line.

## Type checking

The checker from question 1 runs strict. CI does not run it on
the whole tree on the first day. `docs/lints.md` lists every
module under `src/execution_core` that is not strict yet. CI
type-checks the modules that are absent from that list. A new
module is strict in the pull request that adds it. An existing
module leaves the list only in a pull request whose Proof
section shows a clean strict run of that module.

The first type-checking pull request adds the pinned checker,
the strict config, the inventory, and the smallest modules that
are already clean. It records the strict error count of the
remaining modules in the Proof section and does not silence
them with `# type: ignore`. Later pull requests take one module
each, smallest remaining file first. `expr.py`, `plan.py`,
`worker.py`, and `run.py` are last. Tests stay off the strict
list until `src` is empty of exemptions. A strict error is
fixed in the module. A suppression is a typed code, a reason,
and a row.

mypy config lives in `pyproject.toml`. The checker is mypy.

The check job gains one command for the type checker. That is
an intentional `check.yml` edit, with the dogfood note updated
in the same pull request. `check.yml` is under `.github/`, so
the pull request needs the sign-off label.

## Coverage

`coverage` joins the dev group and the allowlist. The check
job's unittest command becomes a coverage run of the same
discovery. The floor in `pyproject.toml` is the integer the
Proof section measured. A pull request whose coverage is below
the floor fails. A pull request may raise the floor in the
same change as the tests that earn it. A pull request that
lowers the floor needs sign-off, which it already needs
because `pyproject.toml` is protected.

## Security tooling

`uv audit --locked` is a step in the `check` job, next to
`uv sync --locked`. A vulnerability fails the job. An ignore
is `--ignore GHSA-...` only together with a row in
`docs/lints.md`: the id, the distribution, the reason, and
whether a fix exists. `--ignore-until-fixed` is allowed for
an id that has no fix, and the row says so. The dogfood note
gains this step because dogfood runs `check.yml`. The job
already reaches PyPI. The audit also reaches OSV.

CodeQL is `.github/workflows/codeql.yml`. The action is pinned
by commit SHA. The language list is Python. It runs on
`pull_request` and on a weekly schedule. Permissions are
`contents: read` and `security-events: write`. The default
setup stays off so two analyses do not run. Enabling the
workflow does not by itself make the check required. The
admin note below says so.

Secret scanning and push protection stay admin actions. Push
protection is what stops a secret at push time. Scanning is
what finds one already committed. Neither replaces the rule
that the private key stays out of the repository. This plan
does not read `~/Secrets` and does not start a sign-in.

Dependabot stays off under the recommendation. The alternative
in question 5 is security updates only, with auto-merge left
off, and those pull requests still need the sign-off label
because they edit `uv.lock`.

## Size, boundaries, and the allowlist check

These three checks are unittest modules. They read the tree.
They do not use the network. They ride the existing unittest
step, so `check.yml` stays byte-identical for them.

The size check parses `src/execution_core` with the standard
library `ast` module. It fails when an unlisted file is over
400 lines, an unlisted function is over 80 lines, or a listed
file or function is longer than the length recorded in
`docs/lints.md`.

The import check fails when the set of `execution_core`
imports is not the baselined set. That baseline is the
measured graph: `cli` imports `dashboard` and `mcp`, those
two import `cli`, and no other module imports either of
them. `protocol` imports no other `execution_core` module.
A new edge fails, including `mcp` importing `dashboard`.
The baseline is generated in the pull request and reviewed
as a fixture. Adding an import edits the fixture and is
visible in the diff.

The allowlist check parses `uv.lock` package names and
compares them to the names in `docs/lints.md`.

## Sign-off

The sign-off pull request adds CODEOWNERS, adds
`.github/workflows/signoff.yml`, and adds a stdlib script that
the workflow runs. The script reads the protected paths from
CODEOWNERS and the changed files from the pull request. If the
intersection is non-empty and the label `mb2090-signoff` is
absent, it exits non-zero. The path list is not copied into
the script. The workflow has `contents: read` and
`pull-requests: read`. It does not post a comment and it does
not change the label.

The pull request that adds the workflow will be red until the
label is applied, which is the same shape as lunatui pull
request 6. Creating and applying the label is a person's
action. This plan's author does not create it.

A repo admin, and not this pull request, does three things
when they want the gates to bind merges:

1. Require a review from code owners on `main`.
2. Mark the sign-off workflow as a required check.
3. Enable secret scanning and push protection. Mark the
   CodeQL workflow required if that pull request has merged
   and the default `check` job does not already cover it.

Until those settings exist, the workflows still run and still
report. They do not by themselves block the merge button.
CODEOWNERS without the branch protection setting does not
block it either.

## Pull requests

The answers above are accepted. This pull request changes no
code, no lockfile, and no workflow. Implementation pull
requests start only after this pull request has merged. Each
one is small, has a Proof section that names the commands it
ran and their results, and does not carry feature work. A pull
request that touches a protected path waits for the sign-off
label once that mechanism exists. The agent does not merge.

1. **Sign-off.** CODEOWNERS, the sign-off workflow, and the
   script. Proof: a fixture diff of `pyproject.toml` fails
   without the label and passes with it. `check.yml` is
   unchanged.
2. **Conventions and the lints skeleton.** `docs/conventions.md`
   and `docs/lints.md` as specified above, with empty rule and
   suppression tables except the rows this plan already names.
   Docs only.
3. **Ruff, clean families.** ERA, PGH, RUF, B, UP. Select and
   the small fixes. Unittest of the touched modules, then the
   full suite if the fixes are not comment-level.
4. **Ruff, simplify.** SIM only.
5. **Ruff, print.** T20 and the two print rows.
6. **Ruff, bandit.** Narrow S codes, then the subprocess
   per-file policy.
7. **Complexity ceilings.** Measured maxima, limits set to
   those maxima, numbers written into `docs/lints.md`.
8. **Allowlist.** The unittest over `uv.lock`, seeded with
   the eight current names.
9. **Audit.** `uv audit --locked` in the check job, dogfood
   note updated, `check.yml` changed only by that step.
10. **Types.** The checker, the strict config, the exemption
    list, and the first clean modules. Then one module per
    pull request. `check.yml` gains the checker command in
    the first of these.
11. **Coverage.** Dev dependency, allowlist row, measured
    floor, unittest command runs under coverage.
12. **Size.** The 400 and 80 gates, with the current over-limit
    files and functions listed at their current lengths.
13. **Imports.** The baseline fixture and the cycle and leaf
    rules.
14. **CodeQL.** The pinned workflow. `check.yml` unchanged.

Question 3 is B, so there is no comment-scanner pull request.
Pull request 10 adds mypy. Pull requests 9 and 14 are
`uv audit --locked` and the pinned CodeQL workflow. Question 8
is A, so `run.py`, `plan.py`, `worker.py`, and `expr.py` stay
listed at their current lengths. They are not split ahead of
pull request 12.

## What this plan leaves as it is

The capability version stays 12. `write` stays rejected.
`pyyaml==6.0.3` stays the only runtime dependency. This pull
request does not edit `check.yml`, `pyproject.toml`, or
`uv.lock`. The private key stays out of the repository. No
file under `~/Secrets` is read. No sign-in is started. No
repo setting is changed. M4 still owns the project license,
public names, and the preview package.
