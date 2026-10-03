"""Shared v0 framing, limits, and errors; no backend or storage dependencies."""

import json
import math

MAX_MESSAGE = 1024 * 1024
MAX_LOG_PAGE = 64 * 1024
MAX_QUEUE = 100
MAX_LIST_PAGE = 100
MAX_CURSOR = 1024
MAX_OFFSET = 2**63 - 1
MAX_REQUEST_ID = 2**53 - 1
MAX_JSON_DEPTH = 64
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
METHODS = [
    "worker.describe",
    "run.submit",
    "run.get",
    "run.list",
    "run.logs",
    "run.artifacts",
    "artifact.read",
    "run.cancel",
]
ERROR_CODES = {
    "PARSE_ERROR": -32700,
    "INVALID_REQUEST": -32600,
    "METHOD_NOT_FOUND": -32601,
    "INVALID_PARAMS": -32602,
    "INTERNAL_ERROR": -32603,
    "VERSION_UNSUPPORTED": -32000,
    "CAPABILITY_UNSUPPORTED": -32000,
    "QUEUE_FULL": -32000,
    "RUN_NOT_FOUND": -32000,
    "CURSOR_EXPIRED": -32000,
    "IDEMPOTENCY_CONFLICT": -32000,
    "WORKER_NOT_READY": -32000,
    "STORAGE_FULL": -32000,
    "SOURCE_UNSTABLE": -32000,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Fault(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.code = ERROR_CODES[kind]


def error_response(request_id, fault):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": fault.code,
            "message": str(fault),
            "data": {"kind": fault.kind, "retryable": False},
        },
    }


def invalid(message):
    raise Fault("INVALID_PARAMS", message)


def fields(params, required=(), optional=()):
    if not isinstance(params, dict):
        invalid("params must be an object")
    if set(params) - set(required) - set(optional) or set(required) - set(params):
        invalid("missing or unknown parameters")


def is_integer(value):
    # JSON Schema integer semantics include 1.0; bool is not a JSON number.
    return type(value) is int or (
        type(value) is float and math.isfinite(value) and value.is_integer()
    )


def integer(value, low, high, name):
    if not is_integer(value) or not low <= value <= high:
        invalid(f"{name} must be an integer in [{low}, {high}]")
    return int(value)


def utf8_string(value, low, high):
    if not isinstance(value, str) or not low <= len(value) <= high:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def valid_id(value):
    return utf8_string(value, 1, 128) or (
        is_integer(value) and -MAX_REQUEST_ID <= value <= MAX_REQUEST_ID
    )


def strict_json(raw):
    def constant(_):
        raise ValueError("non-finite JSON number")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("JSON number exceeds finite range")
        return result

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    # Bound parser work consistently across Python versions. Brackets inside
    # strings do not contribute to structural depth.
    depth, quoted, escaped = 0, False, False
    for character in raw:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds 64 containers")
        elif character in "]}":
            depth -= 1
    return json.loads(raw, parse_constant=constant, parse_float=number, object_pairs_hook=pairs)
