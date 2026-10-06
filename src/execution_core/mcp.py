"""Stdio MCP adapter for the read and artifact tools.

Each tool call opens one worker socket through the CLI client. The
process binds no port, takes no key, and writes only MCP messages
on stdout. A read that times out is not sent again.
"""

import sys

from . import __version__
from .protocol import MAX_CURSOR, MAX_MESSAGE, canonical, strict_json, utf8_string

PROTOCOL_VERSION = "2025-11-25"

# Submit, run.status, fixtures, poll, and the App key stay on the CLI.
_TOOLS = (
    {
        "name": "describe",
        "description": "Read the worker description.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get",
        "description": "Read one run.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list",
        "description": "Read one page of runs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
                "state": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "logs",
        "description": "Read one page of a run log.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "artifacts",
        "description": "Read one page of a run's artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "artifact_read",
        "description": (
            "Read one page of an artifact by id. There is no path argument. "
            'The message "artifact bytes are not available" also covers '
            "other internal failures."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
)

_FORWARD = {
    "describe": ("worker.describe", (), ()),
    "get": ("run.get", ("run_id",), ()),
    "list": ("run.list", (), ("cursor", "limit", "state")),
    "logs": ("run.logs", ("run_id",), ("cursor", "limit")),
    "artifacts": ("run.artifacts", ("run_id",), ("cursor", "limit")),
    "artifact_read": ("artifact.read", ("artifact_id",), ("offset", "limit")),
}


def serve_stdio(state):
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return
        if line in {b"\n", b"\r\n"}:
            continue
        try:
            response = _response(state, line)
        except Exception:
            response = _rpc_error(None, -32603, "internal error")
        if response is not None:
            _emit(response)


def _emit(message):
    sys.stdout.buffer.write(canonical(message).encode() + b"\n")
    sys.stdout.buffer.flush()


def _response(state, line):
    if len(line) > MAX_MESSAGE:
        return _rpc_error(None, -32600, "invalid request")
    try:
        message = strict_json(line)
    except (ValueError, RecursionError, UnicodeError):
        return _rpc_error(None, -32700, "parse error")
    if (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and isinstance(message.get("method"), str)
        and "id" not in message
    ):
        return None
    if not _is_request(message):
        return _rpc_error(_id_of(message), -32600, "invalid request")
    request_id = message["id"]
    method = message["method"]
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rookrunner", "version": __version__},
            },
        )
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": list(_TOOLS)})
    if method == "tools/call":
        return _ok(request_id, _call_tool(state, message.get("params", {})))
    return _rpc_error(request_id, -32601, "method not found")


def _is_request(message):
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and isinstance(message.get("method"), str)
        and "id" in message
        and _valid_id(message["id"])
    )


def _valid_id(value):
    return value is None or type(value) is int or isinstance(value, str)


def _id_of(message):
    if isinstance(message, dict) and "id" in message and _valid_id(message["id"]):
        return message["id"]
    return None


def _ok(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _call_tool(state, params):
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _failure("INVALID_PARAMS", "tools/call requires a tool name", False)
    name = params["name"]
    if name not in _FORWARD:
        return _failure("INVALID_PARAMS", "unknown tool", False)
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _failure("INVALID_PARAMS", "tool arguments must be an object", False)
    method, required, optional = _FORWARD[name]
    if set(arguments) - set(required) - set(optional) or set(required) - set(arguments):
        return _failure("INVALID_PARAMS", "missing or unknown parameters", False)
    forwarded = {}
    if "run_id" in arguments:
        run_id = arguments["run_id"]
        if not utf8_string(run_id, 1, 128):
            return _failure(
                "INVALID_PARAMS",
                "run_id must be a nonempty UTF-8 string of at most 128 characters",
                False,
            )
        forwarded["run_id"] = run_id
    if "cursor" in arguments:
        cursor = arguments["cursor"]
        if not utf8_string(cursor, 1, MAX_CURSOR):
            return _failure(
                "INVALID_PARAMS",
                "cursor must be a nonempty UTF-8 string of at most 1024 characters",
                False,
            )
        forwarded["cursor"] = cursor
    if "artifact_id" in arguments:
        artifact_id = arguments["artifact_id"]
        if not utf8_string(artifact_id, 1, 128):
            return _failure(
                "INVALID_PARAMS",
                "artifact_id must be a nonempty UTF-8 string of at most 128 characters",
                False,
            )
        forwarded["artifact_id"] = artifact_id
    for key in ("limit", "state", "offset"):
        if key in arguments:
            forwarded[key] = arguments[key]
    return _forward(state, method, forwarded)


def _forward(state, method, params):
    # Imported here so loading the adapter does not cycle through the CLI.
    from .cli import call

    try:
        reply = call(state, method, params)
    except TimeoutError:
        return _failure("WORKER_TIMEOUT", "worker did not answer within 5 seconds", True)
    except OSError:
        return _failure("WORKER_UNAVAILABLE", "worker socket is not available", True)
    except (ValueError, RecursionError, UnicodeError):
        return _failure("INTERNAL_ERROR", "worker response was not a protocol result", False)
    if "error" in reply:
        error = reply["error"] if isinstance(reply["error"], dict) else {}
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        kind = data.get("kind")
        if not isinstance(kind, str) or kind == "":
            kind = "INTERNAL_ERROR"
        message = error.get("message")
        if not isinstance(message, str) or message == "":
            message = "worker refused the request"
        retryable = data.get("retryable") is True
        return _failure(kind, message, retryable)
    result = reply.get("result")
    if not isinstance(result, dict):
        return _failure("INTERNAL_ERROR", "worker response was not a protocol result", False)
    return _success(result)


def _success(result):
    return {
        "content": [{"type": "text", "text": canonical(result)}],
        "structuredContent": result,
        "isError": False,
    }


def _failure(kind, message, retryable):
    body = {"kind": kind, "message": message, "retryable": retryable}
    return {
        "content": [{"type": "text", "text": canonical(body)}],
        "structuredContent": body,
        "isError": True,
    }
