import argparse
from pathlib import Path
import socket
import sqlite3
import sys
import time

from .protocol import MAX_MESSAGE, TERMINAL, canonical, strict_json
from .worker import serve
from .snapshot import CaptureError, SourceCapture

# Pause while a followed run is still open. This is client pacing, not an Actions limit.
POLL_SECONDS = 0.2


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


def submit_params(parser, args):
    workflow = (args.workflow, args.job_id, args.event, args.image)
    if any(value is not None for value in workflow):
        if any(value is None for value in workflow):
            parser.error("workflow submit requires --workflow, --job-id, --event, and --image")
        if args.exit_code is not None or args.delay_ms is not None or args.output is not None:
            parser.error("fixture options cannot be combined with a workflow submit")
        try:
            event = strict_json(args.event)
        except (ValueError, RecursionError, UnicodeError):
            parser.error("--event must be one JSON value")
        # Version 1 has no backend field. --backend is accepted so the
        # fixture-shaped command can add the workflow flags.
        return {
            "version": 1,
            "submission_key": args.key,
            "workflow": args.workflow,
            "job_id": args.job_id,
            "event": event,
            "image": args.image,
        }
    if args.backend is None:
        parser.error("fixture submit requires --backend development")
    return {
        "version": 0,
        "submission_key": args.key,
        "backend": args.backend,
        "fixture": {
            "exit_code": 0 if args.exit_code is None else args.exit_code,
            "delay_ms": 0 if args.delay_ms is None else args.delay_ms,
            "output": "development fixture completed\n" if args.output is None else args.output,
        },
    }


def follow_run(state, run_id):
    """Print status and log pages until the run is terminal and its log is consumed."""

    seen = None
    cursor = None
    while True:
        reply = call(state, "run.get", {"run_id": run_id})
        if "error" in reply:
            return reply
        state_name = reply["result"]["state"]
        if state_name != seen:
            print(canonical(reply), flush=True)
            seen = state_name
        params = {"run_id": run_id}
        if cursor is not None:
            params["cursor"] = cursor
        while True:
            page = call(state, "run.logs", params)
            if "error" in page:
                return page
            result = page["result"]
            if result.get("data_base64"):
                print(canonical(page), flush=True)
            cursor = result["next_cursor"]
            params["cursor"] = cursor
            if result["end_of_stream"]:
                if seen not in TERMINAL:
                    reply = call(state, "run.get", {"run_id": run_id})
                    if "error" in reply:
                        return reply
                    if reply["result"]["state"] != seen:
                        print(canonical(reply), flush=True)
                return reply
            if not result.get("data_base64"):
                break
        time.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="Rookrunner development execution contract")
    parser.add_argument("--state", required=True, help="private worker state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--repository", required=True)
    worker.add_argument(
        "--disk-budget-bytes",
        type=int,
        default=None,
        help=(
            "bytes allowed under the state directory "
            "(default 10*1024**3, GitHub's default 10 GB cache storage per repository; "
            "https://docs.github.com/en/actions/reference/limits)"
        ),
    )
    worker.add_argument(
        "--network",
        choices=["bridge", "none"],
        default="bridge",
        help=(
            "Docker network for job containers (default bridge). "
            "GitHub-hosted runners have public internet access by default "
            "(https://docs.github.com/en/actions/concepts/runners/private-networking). "
            "none turns that access off."
        ),
    )
    worker.add_argument(
        "--docker-socket",
        action="store_true",
        help=(
            "bind-mount the host Docker engine socket into the job container. "
            "The job keeps its user and is added to the groups that can open "
            "that socket. GitHub requires an active Docker service for "
            "container-dependent work on a self-hosted runner "
            "(https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot#troubleshooting-containers-in-self-hosted-runners). "
            "Off by default. Host credential directories stay unmounted."
        ),
    )
    commands.add_parser("describe")
    snapshot = commands.add_parser(
        "snapshot", help="capture Git inputs locally; does not submit a run"
    )
    snapshot.add_argument("--repository", required=True)
    snapshot.add_argument("--workflow", required=True)
    snapshot.add_argument("--include", action="append", default=[])
    submit = commands.add_parser("submit", help="submit a development fixture or a workflow job")
    submit.add_argument(
        "--backend",
        choices=["development"],
        help="required for a development fixture; accepted and not sent for a workflow job",
    )
    submit.add_argument("--key", required=True, help="persistent submission key; reuse to retry")
    submit.add_argument("--exit-code", type=int)
    submit.add_argument("--delay-ms", type=int)
    submit.add_argument("--output")
    submit.add_argument("--workflow", help="workflow path inside the worker's repository")
    submit.add_argument("--job-id", help="job to run from that workflow")
    submit.add_argument("--event", help="one JSON value stored as the version 1 event input")
    submit.add_argument("--image", help="digest-pinned image id or name@sha256 pin")
    follow = commands.add_parser(
        "follow", help="poll status and log pages until the run is terminal"
    )
    follow.add_argument("run_id")
    for name in ("get", "cancel", "logs", "artifacts"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name in {"logs", "artifacts"}:
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int)
    artifact_read = commands.add_parser("artifact-read")
    artifact_read.add_argument("artifact_id")
    artifact_read.add_argument("--offset", type=int)
    artifact_read.add_argument("--limit", type=int)
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
            serve(
                args.repository,
                args.state,
                args.disk_budget_bytes,
                args.network,
                args.docker_socket,
            )
            return
        if args.command == "describe":
            method, params = "worker.describe", {}
        elif args.command == "artifact-read":
            method = "artifact.read"
            params = {"artifact_id": args.artifact_id}
            for name in ("offset", "limit"):
                if getattr(args, name, None) is not None:
                    params[name] = getattr(args, name)
        elif args.command == "submit":
            method, params = "run.submit", submit_params(parser, args)
        elif args.command == "follow":
            reply = follow_run(args.state, args.run_id)
            if "error" in reply:
                print(canonical(reply))
                sys.exit(1)
            record = reply["result"]
            succeeded = record["state"] == "succeeded" and record["exit_code"] == 0
            sys.exit(0 if succeeded else 1)
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
