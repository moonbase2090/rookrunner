#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

"""Capture reproducible source and real-container evidence for worker capacity."""

import argparse
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from execution_core.socket_lock import resolve_docker_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker-context")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("the evidence directory must be outside the checkout")
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).split(b"\0")
    source = hashlib.sha256()
    for name in sorted(set(filter(None, names))):
        path = root / os.fsdecode(name)
        content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        source.update(name + b"\0" + hashlib.sha256(content).digest())
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "tests")))
    environment["TMPDIR"] = "/tmp"
    try:
        context = resolve_docker_context(args.docker_context)
    except ValueError as error:
        parser.error(str(error))
    environment.pop("DOCKER_HOST", None)
    environment["DOCKER_CONTEXT"] = context
    environment["ROOKRUNNER_DOCKER_CONTEXT"] = context
    environment["ROOKRUNNER_PARALLEL_PROOF"] = str(output / "runs")
    command = [sys.executable, "-m", "unittest", "test_worker_parallelism", "-v"]
    output.mkdir(parents=True, exist_ok=True)
    receipt = {
        "command": command,
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "source_sha256": source.hexdigest(),
        "source_files": len(set(filter(None, names))),
        "started_at": datetime.now(UTC).isoformat(),
        "docker_context": context,
    }
    (output / "command.json").write_text(json.dumps(receipt, indent=2) + "\n")
    with (output / "stdout").open("w") as stdout, (output / "stderr").open("w") as stderr:
        result = subprocess.run(command, cwd=root, env=environment, stdout=stdout, stderr=stderr)
    log = (output / "stderr").read_text()
    receipt.update(
        finished_at=datetime.now(UTC).isoformat(),
        exit_code=result.returncode,
        status="succeeded" if result.returncode == 0 else "failed",
        summary=re.findall(r"^(?:Ran \d+ tests? in .+|OK|FAILED .+)$", log, re.MULTILINE),
    )
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    sys.stdout.write(json.dumps(receipt) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
