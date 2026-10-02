# Development execution contract v0

`contract.schema.json` is the machine-readable contract for the M1 development
backend. It uses JSON Schema draft 2020-12. Its default root validates a supported
request. All references are local; validation requires no schema downloads.

Select another entry by replacing the root `$ref`:

| Entry in `$defs` | Purpose |
| --- | --- |
| `Request` | Any supported request |
| `<method>.request` | One specific method's request |
| `<method>.response` | That method's success response or a protocol error |
| `Response` | Any success response or protocol error |
| `ErrorResponse` | Protocol errors, including transport rejection with null ID |
| `Run` | Durable development run record, with state-specific result constraints |

The methods are `worker.describe`, `run.submit`, `run.get`, `run.list`,
`run.logs`, and `run.cancel`. Unknown methods and unsupported capabilities are
not valid supported requests, but their rejection responses conform to the schema.
`examples.json` contains labeled valid and invalid examples. The example digest
is a placeholder, not execution evidence.

## Validation rules outside ordinary JSON Schema

The normative [development contract](../../docs/design/development-contract.md)
also specifies wire and stateful rules:

- Require UTF-8 newline framing within 1 MiB; reject duplicate object keys,
  non-finite numbers, and more than 64 nested containers before dispatch.
- Enable format assertions, including the custom `utf8-65536` format: a fixture
  output string must encode to at most 65536 UTF-8 bytes. `maxLength` alone counts
  characters and is insufficient for this limit. Unpaired surrogates are invalid.
- Integers include integral JSON numbers such as `0.0`; booleans do not qualify.
  Accepted fixture numeric fields normalize to integer values before hashing.
- Validate cursor ownership, offsets, current queue capacity, idempotency, and
  worker readiness against live state. These are not schema-only properties.
- A run's `input.snapshot_id` equals its `input.digest`, and the digest must match
  the captured normalized development input. The schema validates digest shape;
  it cannot prove input correspondence. Timestamp ordering is checked separately.

The independent validation helper is `tests/schema_support.py`. Worker request
validation uses standard-library code; tests compare its behavior and actual
responses with these schemas using pinned `jsonschema`. The schema's state-specific
rules prevent a successful record with a nonzero/null exit code or missing attempt
and finish evidence. Persistent format migrations are outside M1.

The format-assertion behavior follows the
[JSON Schema validation specification](https://json-schema.org/draft/2020-12/json-schema-validation)
and [jsonschema validation documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/).

## Reproduce the checks

```bash
uv sync --locked --group dev
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

Changing an implemented v0 field, error assignment, or limit requires updating
the contract and its tests together. Future workflow submission is a separately
negotiated extension/version; it must not silently reinterpret development input.
