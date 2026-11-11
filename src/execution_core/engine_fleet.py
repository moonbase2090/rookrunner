# SPDX-License-Identifier: MIT

"""Read-only inspection of workers in an operator-supplied SSH inventory."""

import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

_COHORT_FIELDS = {
    "source_commit",
    "package_version",
    "source_archive_sha256",
    "source_tree_sha256",
    "state_format_version",
    "protocol_versions",
    "planner_capability_version",
    "supported_workflow_features",
}
_UNIT_FIELDS = {"name", "host", "python", "engine_root", "state_dir", "cohort"}
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SSH_DESTINATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\[\]-]*$")


def _clean_text(value, *, name, max_length=256):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError(f"fleet inventory {name} is invalid")
    return value


def _absolute_path(value, *, name):
    path = _clean_text(value, name=name, max_length=4096)
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError(f"fleet inventory {name} must be an absolute path without '..'")
    return path


def _load_inventory(path):
    try:
        inventory = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("fleet inventory is not readable JSON") from None
    if not isinstance(inventory, dict) or set(inventory) != {"cohorts", "units"}:
        raise ValueError("fleet inventory fields are invalid")
    cohorts = inventory["cohorts"]
    units = inventory["units"]
    if not isinstance(cohorts, dict) or not cohorts or not isinstance(units, list) or not units:
        raise ValueError("fleet inventory needs cohorts and units")
    for name, expected in cohorts.items():
        _clean_text(name, name="cohort name")
        if not isinstance(expected, dict) or set(expected) != _COHORT_FIELDS:
            raise ValueError(f"fleet cohort {name} fields are invalid")
        if not isinstance(expected["source_commit"], str) or not _COMMIT.fullmatch(
            expected["source_commit"]
        ):
            raise ValueError(f"fleet cohort {name} commit is invalid")
        _clean_text(expected["package_version"], name="package version")
        for key in ("source_archive_sha256", "source_tree_sha256"):
            if not isinstance(expected[key], str) or not _SHA256.fullmatch(expected[key]):
                raise ValueError(f"fleet cohort {name} {key} is invalid")
        if (
            type(expected["state_format_version"]) is not int
            or expected["state_format_version"] < 1
        ):
            raise ValueError(f"fleet cohort {name} state format is invalid")
        if (
            type(expected["planner_capability_version"]) is not int
            or expected["planner_capability_version"] < 1
        ):
            raise ValueError(f"fleet cohort {name} capability version is invalid")
        for key in ("protocol_versions", "supported_workflow_features"):
            values = expected[key]
            if not isinstance(values, list) or any(
                not isinstance(value, str | int) for value in values
            ):
                raise ValueError(f"fleet cohort {name} {key} is invalid")
    names = set()
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != _UNIT_FIELDS:
            raise ValueError("fleet unit fields are invalid")
        name = _clean_text(unit["name"], name="unit name")
        if name in names:
            raise ValueError("fleet unit names must be unique")
        names.add(name)
        host = _clean_text(unit["host"], name="SSH destination")
        if not _SSH_DESTINATION.fullmatch(host):
            raise ValueError("fleet SSH destination contains unsupported characters")
        python = _clean_text(unit["python"], name="Python executable")
        if not python.startswith("/"):
            raise ValueError("fleet Python executable must be an absolute path")
        unit["engine_root"] = _absolute_path(unit["engine_root"], name="engine root")
        unit["state_dir"] = _absolute_path(unit["state_dir"], name="state directory")
        if unit["cohort"] not in cohorts:
            raise ValueError(f"fleet unit {name} names an unknown cohort")
    return cohorts, units


def _engine_identity(description):
    manifest = description.get("engine_release")
    if not isinstance(manifest, dict):
        return None
    return {key: manifest.get(key) for key in sorted(_COHORT_FIELDS)}


def _inspect_unit(unit, expected, ssh=subprocess.run):
    command = [
        "env",
        f"PYTHONPATH={unit['engine_root']}/engine/src",
        unit["python"],
        "-m",
        "execution_core",
        "--state",
        unit["state_dir"],
        "describe",
    ]
    remote = shlex.join(command)
    try:
        result = ssh(
            ["ssh", "-T", "-o", "BatchMode=yes", unit["host"], remote],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise ValueError("remote describe failed")
        if len(result.stdout) > 64 * 1024:
            raise ValueError("remote describe response is too large")
        envelope = json.loads(result.stdout)
        description = envelope.get("result")
        if not isinstance(description, dict) or "error" in envelope:
            raise ValueError("remote describe returned an invalid result")
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {
            "unit": unit["name"],
            "cohort": unit["cohort"],
            "state_dir": unit["state_dir"],
            "status": "unavailable",
            "engine": None,
            "capabilities": [],
            "drift": ["unavailable"],
        }
    identity = _engine_identity(description)
    drift = []
    if identity is None:
        drift.append("engine_release")
    else:
        drift.extend(key for key, value in expected.items() if identity.get(key) != value)
    capabilities = description.get("capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(value, str) for value in capabilities
    ):
        capabilities = []
        drift.append("capabilities")
    features = None if identity is None else identity["supported_workflow_features"]
    if (
        features != expected["supported_workflow_features"]
        and "supported_workflow_features" not in drift
    ):
        drift.append("supported_workflow_features")
    return {
        "unit": unit["name"],
        "cohort": unit["cohort"],
        "state_dir": unit["state_dir"],
        "status": "ok" if not drift else "drift",
        "engine": identity,
        "capabilities": sorted(capabilities),
        "drift": sorted(set(drift)),
    }


def inspect_fleet(inventory_path, *, ssh=subprocess.run):
    """Return a credential-free status report without changing any unit."""

    cohorts, units = _load_inventory(inventory_path)
    return [
        _inspect_unit(unit, cohorts[unit["cohort"]], ssh=ssh)
        for unit in sorted(units, key=lambda item: item["name"])
    ]
