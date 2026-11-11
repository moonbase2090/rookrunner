# SPDX-License-Identifier: MIT

"""Install one engine bundle across a cohort after a verified canary gate."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from .engine_fleet import _load_inventory

_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _run(command, *, runner=subprocess.run, **kwargs):
    result = runner(command, check=False, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise ValueError("engine rollout command failed")
    return result


def _remote(unit, args, *, runner):
    command = ["ssh", "-T", "-o", "BatchMode=yes", unit["host"], shlex.join(args)]
    _run(command, runner=runner, timeout=120)


def _gate_evidence(output, expected_engine_commit):
    try:
        evidence = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("rollout gate returned invalid JSON evidence") from None
    required = {
        "engine_commit",
        "run_id",
        "commit_sha",
        "run_state",
        "exit_code",
        "input_digest",
        "input_digest_verified",
        "status_disposition",
        "second_poll",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise ValueError("rollout gate evidence fields are invalid")
    if (
        evidence["engine_commit"] != expected_engine_commit
        or not isinstance(evidence["run_id"], str)
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            evidence["run_id"],
        )
        or not isinstance(evidence["commit_sha"], str)
        or not _SHA.fullmatch(evidence["commit_sha"])
        or evidence["run_state"] != "succeeded"
        or type(evidence["exit_code"]) is not int
        or evidence["exit_code"] != 0
        or not isinstance(evidence["input_digest"], str)
        or not _DIGEST.fullmatch(evidence["input_digest"])
        or evidence["input_digest_verified"] is not True
        or evidence["status_disposition"] != "posted"
        or evidence["second_poll"] != "clean"
    ):
        raise ValueError("rollout gate did not prove the canary acceptance criteria")
    return evidence


def rollout(
    bundle_path,
    installer_path,
    inventory_path,
    *,
    cohort,
    canary_unit,
    engine_commit,
    gate_command,
    runner=subprocess.run,
):
    """Install everywhere, restart the canary, then gate the rest of a cohort.

    ``gate_command`` runs on the operator machine. Its stdout is one JSON object
    with the fields validated by :func:`_gate_evidence`.
    """

    if not isinstance(engine_commit, str) or not _SHA.fullmatch(engine_commit):
        raise ValueError("engine commit is invalid")
    if (
        not isinstance(gate_command, list)
        or not gate_command
        or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in gate_command)
    ):
        raise ValueError("rollout gate command is invalid")
    artifact = Path(bundle_path)
    installer = Path(installer_path)
    if not artifact.is_file() or not installer.is_file():
        raise ValueError("engine bundle or installer is not readable")
    bundle_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    cohorts, inventory_units = _load_inventory(inventory_path)
    if cohort not in cohorts:
        raise ValueError("rollout cohort is unknown")
    units = sorted(
        (unit for unit in inventory_units if unit["cohort"] == cohort),
        key=lambda unit: unit["name"],
    )
    if not units or canary_unit not in {unit["name"] for unit in units}:
        raise ValueError("canary unit must belong to the rollout cohort")
    canary = next(unit for unit in units if unit["name"] == canary_unit)
    remote_bundle = f"/tmp/rookrunner-engine-{engine_commit}.tar.gz"
    remote_installer = f"/tmp/rookrunner-engine-install-{engine_commit}.py"

    for unit in units:
        destination = f"{unit['host']}:{remote_bundle}"
        _run(["scp", "-q", str(artifact), destination], runner=runner, timeout=120)
        _run(
            ["scp", "-q", str(installer), f"{unit['host']}:{remote_installer}"],
            runner=runner,
            timeout=120,
        )
        _remote(
            unit,
            [
                unit["python"],
                remote_installer,
                "install",
                remote_bundle,
                "--root",
                unit["engine_root"],
            ],
            runner=runner,
        )

    def activate(unit):
        _remote(
            unit,
            [
                unit["python"],
                remote_installer,
                "activate",
                engine_commit,
                "--root",
                unit["engine_root"],
            ],
            runner=runner,
        )
        _remote(unit, ["systemctl", "restart", "rookrunner-worker.service"], runner=runner)

    activate(canary)
    environment = os.environ.copy()
    environment.update(
        {
            "ROOKRUNNER_RELEASE_COMMIT": engine_commit,
            "ROOKRUNNER_RELEASE_BUNDLE_SHA256": bundle_sha256,
            "ROOKRUNNER_CANARY_HOST": canary["host"],
            "ROOKRUNNER_CANARY_REMOTE_STATE": canary["state_dir"],
        }
    )
    completed = _run(
        gate_command,
        runner=runner,
        timeout=3600,
        env=environment,
    )
    evidence = _gate_evidence(completed.stdout, engine_commit)

    for unit in units:
        if unit["name"] != canary_unit:
            activate(unit)
    return {
        "cohort": cohort,
        "engine_commit": engine_commit,
        "bundle_sha256": bundle_sha256,
        "canary_unit": canary_unit,
        "activated_units": [unit["name"] for unit in units],
        "canary": evidence,
    }
