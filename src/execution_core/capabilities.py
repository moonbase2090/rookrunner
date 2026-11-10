# SPDX-License-Identifier: MIT

"""Capability floor of a deployed engine version.

The floor of a version is the workflow syntax that release accepts. A later
field is missing from an older engine: that engine rejects it while planning,
so the run never starts. ``check`` and workflow submission both compare a
planned job with the version that would run it, and name each missing
capability with the first release that accepts it.

``workflow.job`` is the owned job plan from 0.1.0, the first tagged release
that plans a job. It is not the ``worker.describe`` protocol list.
``step.continue-on-error`` is 0.1.6. ``permissions.id-token-write`` is 0.1.8;
earlier releases reject ``write``. ``upload.retention-days`` is 0.1.16.
The plan capability version stays 12.
"""

import re

from .plan import plan_workflow

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class Capability:
    def __init__(self, name, minimum_engine):
        self.name = name
        self.minimum_engine = minimum_engine


# First release whose planner accepts the syntax. Keep these at or below
# the package version that ships the planner, or submission of a current
# workflow would refuse itself.
CAPABILITIES = (
    Capability("workflow.job", "0.1.0"),
    Capability("step.continue-on-error", "0.1.6"),
    Capability("permissions.id-token-write", "0.1.8"),
    Capability("upload.retention-days", "0.1.16"),
)
_MINIMUM = {item.name: item.minimum_engine for item in CAPABILITIES}


def require_engine_version(value):
    """Return a dotted engine version, or raise ValueError."""

    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError("engine version must be a dotted numeric version such as 0.1.16")
    return value


def engine_version_from_describe(result):
    """Return the version ``worker.describe`` reported for the running engine."""

    if not isinstance(result, dict) or not isinstance(result.get("version"), str):
        raise ValueError("worker.describe did not report an engine version")
    return require_engine_version(result["version"])


def _numbers(version):
    return tuple(int(part) for part in version.split("."))


def _remember(found, capability, field):
    if capability not in found:
        found[capability] = field


def _take_permissions(permissions, field, found):
    if isinstance(permissions, dict) and permissions.get("id-token") == "write":
        _remember(found, "permissions.id-token-write", f"{field}.id-token")


def _take_steps(steps, field, found):
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        index = step.get("index")
        step_field = f"{field}.steps.{index}" if isinstance(index, int) else field
        if "continue_on_error" in step:
            _remember(found, "step.continue-on-error", f"{step_field}.continue-on-error")
        entered = step.get("with")
        if step.get("upload") and isinstance(entered, dict) and "retention-days" in entered:
            _remember(found, "upload.retention-days", f"{step_field}.with.retention-days")
        _take_steps(step.get("steps"), step_field, found)


def _take_job(job, field, found):
    _take_permissions(job.get("permissions"), f"{field}.permissions", found)
    _take_steps(job.get("steps"), field, found)
    call = job.get("call")
    if not isinstance(call, dict):
        return
    workflow = call.get("workflow")
    if isinstance(workflow, dict):
        _take_permissions(workflow.get("permissions"), f"{field}.call.permissions", found)
    nested = call.get("jobs")
    if not isinstance(nested, list):
        return
    for called in nested:
        if isinstance(called, dict) and isinstance(called.get("id"), str):
            _take_job(called, f"{field}.call.jobs.{called['id']}", found)


def capabilities_used(plan):
    """Return the capabilities a planned job uses, in capability name order."""

    if not isinstance(plan, dict) or not isinstance(plan.get("jobs"), list):
        raise ValueError("plan is not a workflow job")
    found = {"workflow.job": "jobs"}
    workflow = plan.get("workflow")
    if isinstance(workflow, dict):
        _take_permissions(workflow.get("permissions"), "permissions", found)
    for job in plan["jobs"]:
        if isinstance(job, dict) and isinstance(job.get("id"), str):
            _take_job(job, f"jobs.{job['id']}", found)
    return [{"capability": name, "field": found[name]} for name in sorted(found)]


def floor_message(gaps, engine_version):
    """Name each missing capability, its first release, and the deployed version."""

    if not gaps:
        return f"deployed engine {engine_version} covers this job"
    parts = [
        (
            f"missing capability {gap['capability']}; "
            f"minimum engine version {gap['minimum_engine']}; "
            f"field {gap['field']}"
        )
        for gap in gaps
    ]
    return "; ".join(parts) + f"; deployed engine {engine_version}"


def assess(plan, engine_version):
    """Compare one planned job with the floor of ``engine_version``."""

    version = require_engine_version(engine_version)
    used = capabilities_used(plan)
    gaps = []
    for item in used:
        minimum = _MINIMUM[item["capability"]]
        if _numbers(minimum) > _numbers(version):
            gaps.append(
                {
                    "capability": item["capability"],
                    "minimum_engine": minimum,
                    "field": item["field"],
                }
            )
    return {
        "ok": not gaps,
        "engine_version": version,
        "capabilities": [item["capability"] for item in used],
        "missing": gaps,
        "message": floor_message(gaps, version),
    }


def check_workflow(workflow, job_id, engine_version, action_root=None, action_store=None):
    """Plan one job and compare it with the floor of ``engine_version``.

    Planning still rejects syntax no release accepts. This comparison then
    refuses syntax the named engine is too old to accept. Nothing is launched.
    """

    version = require_engine_version(engine_version)
    planned = plan_workflow(workflow, job_id, action_root=action_root, action_store=action_store)
    return assess(planned["plan"], version)
