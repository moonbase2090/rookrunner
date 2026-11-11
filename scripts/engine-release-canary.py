#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ruff: noqa: T201

"""Run an operator-local poller canary against one SSH worker."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None):
    from execution_core.cli import ssh_caller
    from execution_core.engine_canary import run_poller_canary
    from execution_core.poll import PollError, poll_once
    from execution_core.status import StatusError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository as owner/name")
    parser.add_argument("--workflow", required=True, help="workflow path inside the clone")
    parser.add_argument("--job", required=True, help="job id in the workflow")
    parser.add_argument("--clone", required=True, type=Path, help="operator-local repository clone")
    parser.add_argument("--state", required=True, type=Path, help="operator-local poll state")
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--ssh-host", required=True, help="canary worker SSH destination")
    parser.add_argument("--remote-state", required=True, help="canary worker state directory")
    parser.add_argument("--expected-engine-commit", required=True)
    parser.add_argument("--api-base", default="https://api.github.com")
    parser.add_argument("--image")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.timeout > 7200:
        parser.error("--timeout must be between 1 and 7200 seconds")

    caller = ssh_caller(args.ssh_host, args.remote_state)

    def poll():
        return poll_once(
            repository=args.repo,
            clone=args.clone,
            jobs=[(args.workflow, args.job)],
            image=args.image,
            credential_file=args.credential_file,
            api_base=args.api_base,
            state=args.state,
            caller=caller,
            ssh_host=args.ssh_host,
        )

    try:
        evidence = run_poller_canary(
            poll,
            caller,
            expected_engine_commit=args.expected_engine_commit,
            timeout=args.timeout,
        )
    except (OSError, PollError, StatusError, ValueError, RuntimeError) as error:
        print(f"engine-release-canary: {error}", file=sys.stderr)
        return 2
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
