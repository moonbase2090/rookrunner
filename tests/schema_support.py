"""Independent JSON Schema checks against the published v0 contract."""

from datetime import datetime
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/v0/contract.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
FORMATS = FormatChecker()


@FORMATS.checks("utf8-65536", raises=UnicodeError)
def utf8_output(value):
    return not isinstance(value, str) or len(value.encode("utf-8")) <= 65536


@FORMATS.checks("date-time", raises=ValueError)
def utc_timestamp(value):
    if not isinstance(value, str):
        return True
    offset = datetime.fromisoformat(value).utcoffset()
    return offset is not None and offset.total_seconds() == 0


def validator(definition):
    return Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{definition}"}, format_checker=FORMATS)


def validate_response(method, reply):
    validator(f"{method}.response").validate(reply)
    if "result" not in reply:
        return
    result = reply["result"]
    runs = result["runs"] if method == "run.list" else [result] if "run_id" in result else []
    for run in runs:
        # Cross-field invariants cannot be expressed by portable JSON Schema.
        kind = run["input"]["kind"]
        if kind == "development_fixture":
            assert run["input"]["snapshot_id"] == run["input"]["digest"]
        elif kind == "workflow_job":
            assert run["input"]["snapshot_id"] != run["input"]["digest"]
            for name in (
                "digest",
                "workflow_digest",
                "plan_digest",
                "event_digest",
            ):
                assert len(run["input"][name]) == 64
            image_digest = run["input"]["image_digest"]
            assert image_digest.startswith("sha256:") and len(image_digest) == 71
        else:
            raise AssertionError(kind)
        times = [run[field] for field in ("accepted_at", "started_at", "finished_at") if run[field]]
        assert times == sorted(times)
