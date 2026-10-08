# Development dependency provenance

Workflow planning is the first runtime feature with a third-party library.
PyYAML parses workflow text for the planner. The worker, the v0 protocol, and
source capture still use only Python's standard library. No third-party source
or binaries are copied into the repository or bundled into a distribution.

Runtime and development packages are installed into the ignored `.venv`
directory with `uv sync --locked --group dev`. The same development pins are
also the `dev` extra, because Scorecard 0.1.6 runs its tests with
`uv run --extra dev`. `uv.lock` records their PyPI artifacts and hashes. No
Local Actions resources were used.

PyYAML's installed wheel metadata and `licenses/LICENSE` were inspected on
2026-10-02. The wheel reports license MIT, home page https://pyyaml.org/, and
source https://github.com/yaml/pyyaml. The license file copyright is Ingy döt
Net (2017-2021) and Kirill Simonov (2006-2016). Version 6.0.3 is the current
maintained release. The planner builds its tree with a `SafeLoader` subclass.
It does not call `yaml.load`, `yaml.unsafe_load`, or the libyaml `CLoader`.
Published wheels may include libyaml bindings; those bindings are not on the
planning path.

The development-tool wheels below were inspected during M1 completion on
2026-09-23:

| Distribution | Locked version | Declared license | Upstream source |
| --- | --- | --- | --- |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| jsonschema | 4.25.1 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| rpds-py | 2026.6.3 | MIT | https://github.com/crate-py/rpds |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| ruff | 0.12.12 | MIT | https://github.com/astral-sh/ruff |

MIT notices must accompany redistributed copies of PyYAML and of the MIT
development tools. The PSF license and applicable notices must likewise be
retained if distributing typing-extensions. jsonschema, Ruff, and their
transitive libraries are development tools, not runtime requirements. This
inventory is not a license choice for Rookrunner or a license audit for a
future bundled interpreter, action runtime, runner image, or release artifact.
Those require a separate M4 review.
