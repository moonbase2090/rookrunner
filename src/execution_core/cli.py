import argparse
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time

from .checks import mint_list_token, post_check_flow, revoke_installation_token
from .poll import PollError, accept_place, dispatch_once, poll_once
from .protocol import MAX_MESSAGE, TERMINAL, canonical, strict_json
from .status import DEFAULT_API_BASE, StatusError, post_status, read_credential
from .worker import serve
from .snapshot import CaptureError, SourceCapture

# Pause while a followed run is still open. This is client pacing, not an Actions limit.
POLL_SECONDS = 0.2
# The remote client uses the same socket bound. This covers the ssh process.
_SSH_CALL_SECONDS = 5
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:@[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")


def _parse_worker_reply(raw):
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
        return _parse_worker_reply(raw)


def _trigger_flags(args):
    return (
        args.activity_type is not None
        or args.changed_file
        or args.commit_count is not None
        or args.diff_unavailable
    )


def submit_params(parser, args):
    workflow = (args.workflow, args.job_id, args.event)
    if any(value is not None for value in (*workflow, args.image)):
        if any(value is None for value in workflow):
            parser.error("workflow submit requires --workflow, --job-id, and --event")
        if args.exit_code is not None or args.delay_ms is not None or args.output is not None:
            parser.error("fixture options cannot be combined with a workflow submit")
        try:
            event = strict_json(args.event)
        except (ValueError, RecursionError, UnicodeError):
            parser.error("--event must be one JSON value")
        event_name = args.event_name
        if event_name is not None and (
            event_name == ""
            or len(event_name) > 128
            or "\0" in event_name
            or "\n" in event_name
            or "\r" in event_name
        ):
            parser.error("--event-name must be 1 to 128 characters without a newline")
        # Version 1 has no backend field. --backend is accepted so the
        # fixture-shaped command can add the workflow flags.
        params = {
            "version": 1,
            "submission_key": args.key,
            "workflow": args.workflow,
            "job_id": args.job_id,
            "event": event,
        }
        if args.image is not None:
            params["image"] = args.image
        if event_name is not None:
            params["event_name"] = event_name
        if args.activity_type is not None:
            if (
                args.activity_type == ""
                or len(args.activity_type) > 128
                or "\0" in args.activity_type
                or "\n" in args.activity_type
                or "\r" in args.activity_type
            ):
                parser.error("--activity-type must be 1 to 128 characters without a newline")
            params["activity_type"] = args.activity_type
        if args.changed_file:
            params["changed_files"] = args.changed_file
        if args.commit_count is not None:
            if args.commit_count < 0:
                parser.error("--commit-count must be an integer from 0")
            params["commit_count"] = args.commit_count
        if args.diff_unavailable:
            params["diff_unavailable"] = True
        return params
    if args.event_name is not None:
        parser.error("--event-name is only accepted with a workflow submit")
    if _trigger_flags(args):
        parser.error("trigger options are only accepted with a workflow submit")
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


def _one_credential(app_key, credential_file):
    if app_key and credential_file:
        raise StatusError("INVALID_PARAMS", "one post accepts one credential")
    if not app_key and not credential_file:
        raise StatusError("INVALID_PARAMS", "one post needs one credential")


def _status_params(args, record=None):
    params = {
        "run_id": args.run_id,
        "tested_commit": args.tested_commit,
        "status_sha": args.status_sha,
        "context": args.context,
    }
    if record is not None:
        params["record"] = record
    elif args.app_key:
        params["checks"] = True
    return params


def _record_check(args, decision, posted):
    if posted.check_id is None and not posted.status_posted:
        return None
    params = {
        "run_id": args.run_id,
        "tested_commit": args.tested_commit,
        "status_sha": args.status_sha,
        "context": args.context,
    }
    if posted.status_posted:
        params["record"] = decision["state"]
    if posted.check_id is not None:
        params["check_run_id"] = posted.check_id
        params["check_status"] = decision["check_status"]
        if decision.get("check_conclusion") is not None:
            params["check_conclusion"] = decision["check_conclusion"]
    return call(args.state, "run.status", params)


def _print_status_error(error):
    print(
        canonical(
            {
                "error": {
                    "kind": error.kind,
                    "message": str(error),
                    "retryable": error.retryable,
                }
            }
        ),
        file=sys.stderr,
    )
    sys.exit(1)


def _worker_error(reply):
    data = reply["error"].get("data") or {}
    message = reply["error"].get("message") or "worker refused the poll request"
    raise PollError(data.get("kind") or "WORKER_ERROR", message)


def _worker_caller(state):
    def caller(method, params):
        reply = call(state, method, params)
        if "error" in reply:
            _worker_error(reply)
        return reply["result"]

    return caller


def _ssh_target(host, remote_state):
    if not isinstance(host, str) or not _SSH_HOST.fullmatch(host):
        raise PollError("INVALID_PARAMS", "ssh host is not accepted")
    if (
        not isinstance(remote_state, str)
        or remote_state == ""
        or remote_state.startswith("-")
        or any(character in remote_state for character in "\0\r\n")
    ):
        raise PollError("INVALID_PARAMS", "remote state is not accepted")
    return host, remote_state


def ssh_caller(host, remote_state):
    """Send worker methods through ssh. The remote command is the local client.

    No listen port is opened. The command is `ssh -o BatchMode=yes` and the
    remote argv is `python3 -m execution_core --state <dir> call`.
    """

    host, remote_state = _ssh_target(host, remote_state)

    def caller(method, params):
        request = (
            canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
        ).encode()
        if len(request) > MAX_MESSAGE:
            raise PollError("INVALID_PARAMS", "request exceeds 1 MiB")
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            "--",
            "python3",
            "-m",
            "execution_core",
            "--state",
            remote_state,
            "call",
        ]
        try:
            completed = subprocess.run(
                command,
                input=request,
                capture_output=True,
                timeout=_SSH_CALL_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise PollError("WORKER_ERROR", "worker ssh call timed out") from None
        except OSError:
            raise PollError("WORKER_ERROR", "worker ssh call failed") from None
        stdout = completed.stdout
        if stdout.count(b"\n") != 1 or not stdout.endswith(b"\n"):
            raise PollError("WORKER_ERROR", "worker ssh call failed")
        try:
            reply = _parse_worker_reply(stdout)
        except (ValueError, RecursionError, UnicodeError):
            raise PollError("WORKER_ERROR", "worker ssh call failed") from None
        if "error" in reply:
            _worker_error(reply)
        if completed.returncode != 0:
            raise PollError("WORKER_ERROR", "worker ssh call failed")
        return reply["result"]

    return caller


def _poll_caller(args):
    host = args.ssh_host
    remote = args.remote_state
    if host is None and remote is None:
        return _worker_caller(args.state)
    if host is None or remote is None:
        raise PollError("INVALID_PARAMS", "ssh host and remote state are set together")
    return ssh_caller(host, remote)


def _place_caller_unused(_method, _params):
    raise PollError("WORKER_ERROR", "worker ssh call failed")


def _place_from_json(text):
    if (
        not isinstance(text, str)
        or text == ""
        or text.startswith("-")
        or any(character in text for character in "\0\r\n")
    ):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    try:
        parsed = strict_json(text)
    except (ValueError, UnicodeError, RecursionError):
        raise PollError("INVALID_PARAMS", "place is not accepted") from None
    if not isinstance(parsed, dict):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    allowed = {"name", "state", "cap", "image", "ssh"}
    if not {"name", "state", "cap", "image"} <= set(parsed) or set(parsed) - allowed:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    checked = accept_place({**parsed, "caller": _place_caller_unused})
    if checked["ssh"] is None:
        checked["caller"] = _worker_caller(checked["state"])
    else:
        checked["caller"] = ssh_caller(checked["ssh"], checked["state"])
    return checked


def _places_from_args(args):
    raw = getattr(args, "place", None)
    if not raw:
        return None
    if (
        getattr(args, "ssh_host", None) is not None
        or getattr(args, "remote_state", None) is not None
    ):
        raise PollError("INVALID_PARAMS", "place and ssh host are set separately")
    places = []
    seen = set()
    for item in raw:
        place = _place_from_json(item)
        if place["name"] in seen:
            raise PollError("INVALID_PARAMS", "place is not accepted")
        seen.add(place["name"])
        places.append(place)
    return places


def _call_stdio(state):
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n") or sys.stdin.buffer.read(1):
        raise ValueError("invalid or oversized worker request")
    try:
        request = strict_json(raw)
    except RecursionError:
        raise ValueError("worker request exceeds JSON nesting limit") from None
    if (
        not isinstance(request, dict)
        or request.get("jsonrpc") != "2.0"
        or type(request.get("id")) is not int
        or not isinstance(request.get("method"), str)
        or not isinstance(request.get("params", {}), dict)
    ):
        raise ValueError("invalid worker request envelope")
    reply = call(state, request["method"], request.get("params") or {})
    print(canonical(reply))
    sys.exit(1 if "error" in reply else 0)


def _run_poll(args, caller, mint, revoke, places=None):
    """Mint a list token when ``--app-key`` is set, run one pass, then revoke."""

    if places is not None and (
        getattr(args, "ssh_host", None) is not None
        or getattr(args, "remote_state", None) is not None
    ):
        raise PollError("INVALID_PARAMS", "place and ssh host are set separately")
    token = None
    try:
        if args.app_key and not args.credential_file:
            token = mint(args.app_key, args.state, args.clone, args.repository, args.api_base)
        return poll_once(
            repository=args.repository,
            clone=args.clone,
            jobs=args.job,
            image=args.image,
            credential_file=args.credential_file,
            app_key=args.app_key,
            api_base=args.api_base,
            state=args.state,
            caller=caller,
            list_token=token,
            ssh_host=getattr(args, "ssh_host", None),
            places=places,
            allow_untrusted=getattr(args, "allow_untrusted", ()) or (),
        )
    finally:
        if isinstance(token, str):
            revoke(args.api_base, token)
        token = None


def report_dispatch(args):
    """Submit one workflow on the default-branch tip and exit."""

    try:
        result = dispatch_once(
            repository=args.repository,
            clone=args.clone,
            workflow=args.workflow,
            caller=_worker_caller(args.state),
            image=args.image,
        )
    except PollError as error:
        print(
            canonical({"error": {"kind": error.kind, "message": str(error)}}),
            file=sys.stderr,
        )
        sys.exit(1)
    print(canonical(result))


def report_poll(args):
    """Run one pass and exit. A rate-limit stop is a finished pass."""

    try:
        places = _places_from_args(args)
        caller = None if places is not None else _poll_caller(args)
        result = _run_poll(
            args,
            caller,
            mint_list_token,
            revoke_installation_token,
            places,
        )
    except PollError as error:
        print(
            canonical({"error": {"kind": error.kind, "message": str(error)}}),
            file=sys.stderr,
        )
        sys.exit(1)
    except StatusError as error:
        _print_status_error(error)
    else:
        for warning in result.get("warnings") or []:
            message = warning.get("message") if isinstance(warning, dict) else None
            if isinstance(message, str) and message != "":
                print(message, file=sys.stderr)
        print(canonical(result))


def report_status(args):
    """Post one status after the worker accepts the run. Skip a repeated terminal state."""

    try:
        _one_credential(args.app_key, args.credential_file)
    except StatusError as error:
        _print_status_error(error)
    reply = call(args.state, "run.status", _status_params(args))
    if "error" in reply:
        print(canonical(reply))
        sys.exit(1)
    decision = reply["result"]
    if decision["action"] == "skip":
        print(canonical(reply))
        return
    if decision["action"] != "post":
        raise ValueError("worker status decision was not post or skip")
    described = call(args.state, "worker.describe", {})
    if "error" in described:
        print(canonical(described))
        sys.exit(1)
    if args.app_key:
        _report_app_status(args, decision, described["result"]["repository"])
        return
    try:
        token = read_credential(args.credential_file, args.state, described["result"]["repository"])
        try:
            post_status(
                args.api_base,
                args.repository,
                args.status_sha,
                decision["state"],
                args.context,
                token,
            )
        finally:
            token = None
    except StatusError as error:
        _print_status_error(error)
    recorded = call(args.state, "run.status", _status_params(args, decision["state"]))
    print(canonical(recorded))
    sys.exit(1 if "error" in recorded else 0)


def _report_app_status(args, decision, repository_root):
    try:
        posted = post_check_flow(
            api_base=args.api_base,
            repository=args.repository,
            sha=args.status_sha,
            context=args.context,
            run_id=args.run_id,
            check_status=decision["check_status"],
            check_conclusion=decision.get("check_conclusion"),
            check_summary_text=decision["check_summary"],
            check_run_id=decision.get("check_run_id"),
            status_state=decision["state"],
            post_status_request=decision.get("status_recorded") is not True,
            app_key=args.app_key,
            state_dir=args.state,
            repository_root=repository_root,
        )
    except StatusError as error:
        _print_status_error(error)
    recorded = _record_check(args, decision, posted)
    if posted.error is not None:
        if recorded is not None:
            print(canonical(recorded))
        _print_status_error(posted.error)
    print(canonical(recorded))
    sys.exit(1 if recorded is None or "error" in recorded else 0)


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
            "Off by default. When set, a private Docker volume is mounted at "
            "that volume's mountpoint and TMPDIR, TEMP, and TMP default to "
            "it. Host credential directories stay unmounted. "
            "Startup warns that the exposure includes "
            "~/Secrets/github-app/rookrunner-app/ and "
            "~/Secrets/rookrunner-secrets/. Combined with --app-key or "
            "--secrets, startup refuses unless --runner-image is set and "
            "the ~/Secrets probe exits 0."
        ),
    )
    worker.add_argument(
        "--secrets",
        action="store_true",
        help=(
            "Takes no path. Requires --github-repository owner/name. "
            "When the allowlist matches, a step env or with expression "
            "that is exactly secrets.NAME receives that file's value. "
            "Does not mint a token and does not rewrite run. "
            "Combined with --docker-socket, startup refuses unless "
            "--runner-image is set and the ~/Secrets probe exits 0."
        ),
    )
    worker.add_argument(
        "--github-repository",
        help=(
            "GitHub repository as owner/name. Required with --secrets. "
            "Not a filesystem path. The secret directory is "
            "~/Secrets/rookrunner-secrets/<owner>/<repo>/."
        ),
    )
    worker.add_argument(
        "--app-key",
        help=(
            "private key path for the Rookrunner GitHub App. "
            "The file must be ~/Secrets/github-app/rookrunner-app/private-key.pem. "
            "worker --app-key opens that file only when a job mints a Contents-read token. "
            "Combined with --docker-socket, startup refuses unless "
            "--runner-image is set and the ~/Secrets probe exits 0."
        ),
    )
    worker.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"GitHub API origin (default {DEFAULT_API_BASE})",
    )
    worker.add_argument(
        "--secret-ref",
        action="append",
        default=[],
        help=(
            "one full ref, such as refs/heads/main. Repeat for each ref. "
            "Stored for the allowlist. No secret is read."
        ),
    )
    worker.add_argument(
        "--secret-pusher",
        action="append",
        default=[],
        help=(
            "one GitHub login. Repeat for each login. "
            "Stored for the allowlist and compared with ASCII case folding. "
            "No secret is read."
        ),
    )
    worker.add_argument(
        "--node24",
        help=(
            "unpacked Node.js 24 Linux distribution for the image architecture. "
            "Mounted read-only at /opt/node24 and not added to PATH. "
            "The worker does not download Node."
        ),
    )
    worker.add_argument(
        "--runner-image",
        help=(
            "digest-pinned image used when a submit or poll omits --image "
            "and every selected job has runs-on ubuntu-latest. "
            "The worker does not pull, build, or publish it."
        ),
    )
    commands.add_parser("describe")
    commands.add_parser("mcp", help="stdio MCP adapter for describe, get, list, and logs")
    commands.add_parser(
        "dashboard",
        help="terminal view of runs, logs, artifacts, and cancel",
    )
    commands.add_parser(
        "status-page",
        help="read-only page on 127.0.0.1:8765",
    )
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
    submit.add_argument(
        "--event-name",
        help=(
            "optional event name for a version 1 workflow submit. "
            "It is part of the submission. Omit it to leave github.event_name unset. "
            "push, pull_request, schedule, and workflow_dispatch check on before a run is stored"
        ),
    )
    submit.add_argument(
        "--activity-type",
        help="pull_request activity type. Required when that event is listed in on",
    )
    submit.add_argument(
        "--changed-file",
        action="append",
        help="one path from the caller-supplied diff. Repeat for each file. The first 3000 count",
    )
    submit.add_argument(
        "--commit-count",
        type=int,
        help="commits in a push. More than 1000 skips path filters",
    )
    submit.add_argument(
        "--diff-unavailable",
        action="store_true",
        help="the diff is unavailable, so path filters are skipped",
    )
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
    status = commands.add_parser("status", help="post one commit status for one workflow run")
    status.add_argument("run_id")
    status.add_argument("--repository", required=True, help="GitHub repository as owner/name")
    status.add_argument("--status-sha", required=True, help="commit that receives the status")
    status.add_argument(
        "--tested-commit",
        required=True,
        help="commit the snapshot base must equal; the pull-request merge commit",
    )
    status.add_argument("--context", required=True)
    status.add_argument(
        "--credential-file",
        help="operator token file read only when a status is posted",
    )
    status.add_argument(
        "--app-key",
        help=(
            "private key path for the Rookrunner GitHub App. "
            "The file must be ~/Secrets/github-app/rookrunner-app/private-key.pem. "
            "Omit this flag to post a commit status from --credential-file. "
            "Passing both refuses before any HTTP request."
        ),
    )
    status.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"GitHub API origin (default {DEFAULT_API_BASE})",
    )
    dispatch = commands.add_parser(
        "dispatch",
        help="submit one workflow on the default-branch tip and exit",
    )
    dispatch.add_argument("--repository", required=True, help="GitHub repository as owner/name")
    dispatch.add_argument("--clone", required=True, help="dedicated clone; the worker repository")
    dispatch.add_argument("--workflow", required=True, help="workflow path inside the clone")
    dispatch.add_argument(
        "--image",
        help=(
            "digest-pinned image id or name@sha256 pin. "
            "Omit it to use the worker --runner-image digest"
        ),
    )
    poll = commands.add_parser("poll", help="run one poll pass for one owner repository")
    poll.add_argument("--repository", required=True, help="GitHub repository as owner/name")
    poll.add_argument("--clone", required=True, help="dedicated clone; the worker repository")
    poll.add_argument(
        "--job",
        action="append",
        nargs=2,
        metavar=("WORKFLOW", "JOB_ID"),
        help="workflow path inside the clone and the job to run; repeat for each job",
    )
    poll.add_argument(
        "--image",
        help=(
            "digest-pinned image id or name@sha256 pin. "
            "Omit it to use the worker --runner-image digest"
        ),
    )
    poll.add_argument(
        "--credential-file",
        help="operator token file read only when a status is posted",
    )
    poll.add_argument(
        "--app-key",
        help=(
            "private key path for the Rookrunner GitHub App. "
            "The file must be ~/Secrets/github-app/rookrunner-app/private-key.pem. "
            "Omit this flag to post a commit status from --credential-file. "
            "Passing both refuses before any HTTP request."
        ),
    )
    poll.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"GitHub API origin (default {DEFAULT_API_BASE})",
    )
    poll.add_argument(
        "--place",
        action="append",
        help=(
            "one worker in placement order, as a JSON object with name, state, "
            "cap, and image, and an optional ssh. Repeat for each worker. "
            "Omit it to keep one local socket or one --ssh-host."
        ),
    )
    poll.add_argument(
        "--allow-untrusted",
        action="append",
        default=[],
        help=(
            "SHA of one untrusted pull-request head or push tip that may run. "
            "Repeat for each run. Omit it to refuse untrusted code."
        ),
    )
    poll.add_argument(
        "--ssh-host",
        help=(
            "reach the worker with ssh -o BatchMode=yes. "
            "The remote command is the local client. No listen port is opened."
        ),
    )
    poll.add_argument(
        "--remote-state",
        help="worker state directory on the ssh host. Set only with --ssh-host.",
    )
    commands.add_parser(
        "call",
        help="send one JSON-RPC request from stdin to the local worker socket",
    )
    args = parser.parse_args()
    if args.command == "poll" and not args.job:
        parser.error("poll requires at least one --job WORKFLOW JOB_ID")
    try:
        if args.command == "mcp":
            from .mcp import serve_stdio

            serve_stdio(args.state)
            return
        if args.command == "dashboard":
            from .dashboard import serve_terminal

            serve_terminal(args.state)
            return
        if args.command == "status-page":
            from .status_page import serve as serve_page

            serve_page(args.state)
            return
        if args.command == "snapshot":
            result = SourceCapture(args.repository, args.state).capture(args.workflow, args.include)
            print(canonical({"snapshot": result}))
            return
        if args.command == "status":
            report_status(args)
            return
        if args.command == "dispatch":
            report_dispatch(args)
            return
        if args.command == "poll":
            report_poll(args)
            return
        if args.command == "call":
            _call_stdio(args.state)
            return
        if args.command == "worker":
            serve(
                args.repository,
                args.state,
                args.disk_budget_bytes,
                args.network,
                args.docker_socket,
                node24=args.node24,
                runner_image=args.runner_image,
                secrets=args.secrets,
                github_repository=args.github_repository,
                app_key=args.app_key,
                secret_refs=args.secret_ref,
                secret_pushers=args.secret_pusher,
                api_base=args.api_base,
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
