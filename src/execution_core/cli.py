import argparse
from pathlib import Path
import socket
import sqlite3
import sys

from .protocol import MAX_MESSAGE, canonical, strict_json
from .worker import serve
from .snapshot import CaptureError, SourceCapture


def call(state, method, params):
    request = (
        canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    ).encode()
    if len(request) > MAX_MESSAGE:
        raise ValueError("request exceeds 1 MiB")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(Path(state).absolute() / "worker.sock"))
        client.sendall(request)
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
            raise ValueError("invalid or oversized worker response")
        try:
            reply = strict_json(raw)
        except RecursionError:
            raise ValueError("worker response exceeds JSON nesting limit") from None
        if (
            not isinstance(reply, dict)
            or reply.get("jsonrpc") != "2.0"
            or type(reply.get("id")) is not int
            or reply["id"] != 1
            or set(reply) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
            or not isinstance(reply.get("result", reply.get("error")), dict)
        ):
            raise ValueError("invalid worker response envelope")
        return reply


def main():
    parser = argparse.ArgumentParser(description="Rookrunner development execution contract")
    parser.add_argument("--state", required=True, help="private worker state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--repository", required=True)
    commands.add_parser("describe")
    snapshot = commands.add_parser(
        "snapshot", help="capture Git inputs locally; does not submit a run"
    )
    snapshot.add_argument("--repository", required=True)
    snapshot.add_argument("--workflow", required=True)
    snapshot.add_argument("--include", action="append", default=[])
    submit = commands.add_parser("submit", help="submit a synthetic development fixture")
    submit.add_argument("--backend", choices=["development"], required=True)
    submit.add_argument("--key", required=True, help="persistent submission key; reuse to retry")
    submit.add_argument("--exit-code", type=int, default=0)
    submit.add_argument("--delay-ms", type=int, default=0)
    submit.add_argument("--output", default="development fixture completed\n")
    for name in ("get", "cancel", "logs"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "logs":
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int)
    listing = commands.add_parser("list")
    listing.add_argument("--cursor")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--filter-state", dest="state_filter")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = SourceCapture(args.repository, args.state).capture(args.workflow, args.include)
            print(canonical({"snapshot": result}))
            return
        if args.command == "worker":
            serve(args.repository, args.state)
            return
        if args.command == "describe":
            method, params = "worker.describe", {}
        elif args.command == "submit":
            method, params = (
                "run.submit",
                {
                    "version": 0,
                    "submission_key": args.key,
                    "backend": args.backend,
                    "fixture": {
                        "exit_code": args.exit_code,
                        "delay_ms": args.delay_ms,
                        "output": args.output,
                    },
                },
            )
        else:
            method = "run." + args.command
            params = {"run_id": args.run_id} if hasattr(args, "run_id") else {}
            for name in ("cursor", "limit"):
                if getattr(args, name, None) is not None:
                    params[name] = getattr(args, name)
            if args.command == "cancel":
                params["version"] = 0
            if args.command == "list" and args.state_filter is not None:
                params["state"] = args.state_filter
        reply = call(args.state, method, params)
        print(canonical(reply))
        # Exit zero means this protocol operation succeeded, not that a run succeeded.
        sys.exit(1 if "error" in reply else 0)
    except CaptureError as error:
        print(canonical({"error": {"kind": error.kind, "message": str(error)}}), file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error:
        print(
            canonical(
                {
                    "error": {
                        "kind": "CLIENT_ERROR",
                        "message": "worker storage could not be opened or initialized",
                    }
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    except (OSError, ValueError) as error:
        print(
            canonical({"error": {"kind": "CLIENT_ERROR", "message": str(error)}}), file=sys.stderr
        )
        sys.exit(1)
