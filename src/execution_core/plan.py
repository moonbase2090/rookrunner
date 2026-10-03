"""Versioned plan for one selected job and the jobs it needs.

Parsing does not fetch actions, pull images, start containers, or accept a
run. Capability version 4 records declared fields, including step and job
`if` text and job output expressions. It checks that those expressions can
be parsed and does not evaluate them. A selected job includes the jobs it
needs. A dependency that is not defined in the workflow is rejected.
Anything this slice cannot describe is rejected.
"""

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

import yaml
from yaml.constructor import SafeConstructor
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .expr import ExprError, check_job_if, check_job_output, check_step_if
from .protocol import canonical

CAPABILITY_VERSION = 4
# https://docs.github.com/en/actions/reference/limits
GITHUB_ACTIONS_LIMITS = "https://docs.github.com/en/actions/reference/limits"
# Workflow file size: 500 KB per file (500 * 1024 bytes). A larger file does
# not start a run. GitHub documents no per-job step limit.
# https://docs.github.com/en/actions/reference/limits
MAX_WORKFLOW_BYTES = 500 * 1024
MAX_DEPTH = 64
# Job time bound is jobs.<job_id>.timeout-minutes. The default is 360 minutes,
# which is also the 6 hour GitHub-hosted job execution time. Self-hosted job
# execution time is 5 days, and that is the ceiling accepted here because this
# engine runs the job. run_job stops the owned container at that bound.
# https://docs.github.com/en/actions/reference/limits
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
DEFAULT_JOB_TIMEOUT_MINUTES = 360
MAX_JOB_TIMEOUT_MINUTES = 5 * 24 * 60
# jobs.<job_id>.steps[*].timeout-minutes maximum is 360 minutes on both
# GitHub-hosted and self-hosted runners. An omitted step timeout has no
# default; only the job bound applies. A longer step value is rejected here.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
GITHUB_WORKFLOW_SYNTAX = (
    "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax"
)
MAX_STEP_TIMEOUT_MINUTES = 360
STR_TAG = "tag:yaml.org,2002:str"
BOOL_TAG = "tag:yaml.org,2002:bool"
MERGE_TAG = "tag:yaml.org,2002:merge"
SCALAR_TAGS = {
    STR_TAG,
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    BOOL_TAG,
    "tag:yaml.org,2002:null",
}
# These names change execution or hide work. Reject them wherever they appear.
# `strategy` and `matrix` stay unsupported. GitHub's job matrix limit is 256
# jobs per workflow run, on GitHub-hosted and self-hosted runners.
# https://docs.github.com/en/actions/reference/limits
FORBIDDEN = {
    "uses",
    "strategy",
    "matrix",
    "secrets",
    "services",
    "privileged",
    "container",
    "workflow_call",
}
WORKFLOW_KEYS = {"name", "on", "jobs", "defaults", "env"}
JOB_KEYS = {
    "name",
    "runs-on",
    "needs",
    "if",
    "outputs",
    "steps",
    "defaults",
    "env",
    "timeout-minutes",
}
STEP_KEYS = {"id", "name", "if", "run", "shell", "working-directory", "env", "timeout-minutes"}
DEFAULT_KEYS = {"run"}
RUN_DEFAULT_KEYS = {"shell", "working-directory"}


class PlanError(Exception):
    def __init__(self, kind, message, field=None):
        super().__init__(message)
        self.kind = kind
        self.field = field


def _workflow_loader():
    """SafeLoader that keeps YAML 1.1 yes/no/on/off values as strings.

    GitHub workflow files use an unquoted `on` key. PyYAML's default resolver
    would turn that key into a boolean before the planner can see it.
    """

    class WorkflowLoader(yaml.SafeLoader):
        pass

    copied = {key: list(values) for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    bool_pattern = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
    for key, values in copied.items():
        copied[key] = [
            (BOOL_TAG, bool_pattern) if tag == BOOL_TAG else (tag, pattern)
            for tag, pattern in values
        ]
    WorkflowLoader.yaml_implicit_resolvers = copied
    return WorkflowLoader


WorkflowLoader = _workflow_loader()


def _unsupported(field):
    raise PlanError("CAPABILITY_UNSUPPORTED", f"{field}: capability is unsupported", field)


def _invalid(message, field=None):
    raise PlanError("WORKFLOW_INVALID", message, field)


def _location(node):
    mark = node.start_mark
    return {"line": mark.line + 1, "column": mark.column + 1}


def _join(field, part):
    text = str(part)
    return text if not field else f"{field}.{text}"


class _Planner:
    def __init__(self):
        self.seen = set()
        self.constructor = SafeConstructor()

    def plan(self, workflow, job_id):
        if not isinstance(job_id, str) or job_id == "":
            _invalid("no selected job")
        root = self._root(workflow)
        body = self._mapping(root, "")
        self._allow(body, "", WORKFLOW_KEYS)
        workflow_plan = {
            "location": _location(root),
            "name": self._optional_string(body, "", "name"),
            "on": self._data(body["on"][1], "on", 1) if "on" in body else None,
            "defaults": self._defaults(body, ""),
            "env": self._env(body, ""),
        }
        if "jobs" not in body:
            _invalid("no selected job", "jobs")
        jobs = self._mapping(body["jobs"][1], "jobs")
        if job_id not in jobs:
            _invalid("no selected job", "jobs")
        parsed = {key: self._job(jobs, key) for key in jobs}
        planned_jobs = [parsed[key] for key in self._order(parsed, job_id)]
        plan = {
            "capability_version": CAPABILITY_VERSION,
            "workflow": workflow_plan,
            "job": planned_jobs[-1],
            "jobs": planned_jobs,
        }
        encoded = canonical(plan).encode("ascii")
        return {"plan": plan, "digest": hashlib.sha256(encoded).hexdigest()}

    def _root(self, workflow):
        if not isinstance(workflow, (bytes, bytearray)):
            _invalid("workflow must be bytes")
        if len(workflow) > MAX_WORKFLOW_BYTES:
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"workflow file exceeds 500 KB per file ({GITHUB_ACTIONS_LIMITS})",
                "workflow",
            )
        try:
            text = bytes(workflow).decode("utf-8")
        except UnicodeError:
            _invalid("workflow must be UTF-8")
        loader = WorkflowLoader(text)
        try:
            try:
                node = loader.get_single_node()
            except yaml.YAMLError:
                _invalid("workflow YAML is not accepted")
        finally:
            loader.dispose()
        if not isinstance(node, MappingNode):
            _invalid("workflow must be a mapping")
        return node

    def _enter(self, node, field):
        identity = id(node)
        if identity in self.seen:
            _invalid(f"{field or 'workflow'}: YAML aliases are not accepted", field or None)
        self.seen.add(identity)

    def _mapping(self, node, field):
        self._enter(node, field)
        if not isinstance(node, MappingNode) or node.tag != "tag:yaml.org,2002:map":
            _invalid(f"{field or 'workflow'} must be a mapping", field or None)
        items = {}
        for key_node, value_node in node.value:
            if key_node.tag == MERGE_TAG:
                self._enter(key_node, field)
                _invalid(f"{field or 'workflow'}: YAML merge keys are not accepted", field or None)
            key = self._string_scalar(key_node, _join(field, "key"))
            path = _join(field, key)
            if key in items:
                _invalid(f"duplicate YAML key {key!r} at {path}", path)
            if key in FORBIDDEN:
                _unsupported(path)
            items[key] = (key_node, value_node)
        return items

    def _allow(self, items, field, allowed):
        for key in items:
            if key not in allowed:
                _unsupported(_join(field, key))

    def _string_scalar(self, node, field):
        self._enter(node, field)
        if not isinstance(node, ScalarNode) or node.tag != STR_TAG:
            _invalid(f"{field} must be a string", field)
        return node.value

    def _optional_string(self, items, field, key):
        if key not in items:
            return None
        return self._string_scalar(items[key][1], _join(field, key))

    def _data(self, node, field, depth):
        if depth > MAX_DEPTH:
            _invalid(f"{field}: workflow nesting exceeds {MAX_DEPTH}", field)
        if isinstance(node, MappingNode):
            items = self._mapping(node, field)
            values = {}
            for key, (_, value) in items.items():
                values[key] = self._data(value, _join(field, key), depth + 1)
            return values
        if isinstance(node, SequenceNode):
            self._enter(node, field)
            if node.tag != "tag:yaml.org,2002:seq":
                _invalid(f"{field} must be a sequence", field)
            values = []
            for index, child in enumerate(node.value):
                values.append(self._data(child, _join(field, index), depth + 1))
            return values
        return self._typed_scalar(node, field)

    def _typed_scalar(self, node, field):
        self._enter(node, field)
        if not isinstance(node, ScalarNode) or node.tag not in SCALAR_TAGS:
            _invalid(f"{field}: YAML value is not accepted", field)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{field}: YAML value is not accepted", field)
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        _invalid(f"{field}: YAML value is not accepted", field)

    def _defaults(self, items, field):
        declared = {"shell": None, "working_directory": None}
        if "defaults" not in items:
            return declared
        path = _join(field, "defaults")
        body = self._mapping(items["defaults"][1], path)
        self._allow(body, path, DEFAULT_KEYS)
        if "run" not in body:
            return declared
        run_field = _join(path, "run")
        run_body = self._mapping(body["run"][1], run_field)
        self._allow(run_body, run_field, RUN_DEFAULT_KEYS)
        declared["shell"] = self._optional_string(run_body, run_field, "shell")
        declared["working_directory"] = self._optional_string(
            run_body, run_field, "working-directory"
        )
        return declared

    def _env(self, items, field):
        if "env" not in items:
            return {}
        path = _join(field, "env")
        body = self._mapping(items["env"][1], path)
        return {
            key: self._string_scalar(value, _join(path, key)) for key, (_, value) in body.items()
        }

    def _runs_on(self, items, field):
        if "runs-on" not in items:
            return None
        path = _join(field, "runs-on")
        node = items["runs-on"][1]
        if isinstance(node, SequenceNode):
            self._enter(node, path)
            values = []
            for index, child in enumerate(node.value):
                values.append(self._string_scalar(child, _join(path, index)))
            return values
        return self._string_scalar(node, path)

    def _positive_minutes(self, node, path):
        self._enter(node, path)
        if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:int":
            _invalid(f"{path} must be a positive integer number of minutes", path)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{path} must be a positive integer number of minutes", path)
        if type(value) is not int or value < 1:
            _invalid(f"{path} must be a positive integer number of minutes", path)
        return value

    def _timeout_minutes(self, items, field):
        """Record the job time bound. An omitted value is the 360 minute default."""

        if "timeout-minutes" not in items:
            return DEFAULT_JOB_TIMEOUT_MINUTES
        path = _join(field, "timeout-minutes")
        value = self._positive_minutes(items["timeout-minutes"][1], path)
        if value > MAX_JOB_TIMEOUT_MINUTES:
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{path} is above the 5 day self-hosted job execution time "
                f"({GITHUB_ACTIONS_LIMITS})",
                path,
            )
        return value

    def _step_timeout_minutes(self, items, field):
        """Record a step timeout. An omitted value stays omitted; there is no step default."""

        if "timeout-minutes" not in items:
            return None
        path = _join(field, "timeout-minutes")
        value = self._positive_minutes(items["timeout-minutes"][1], path)
        if value > MAX_STEP_TIMEOUT_MINUTES:
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{path} is above the 360 minute step timeout maximum ({GITHUB_WORKFLOW_SYNTAX})",
                path,
            )
        return value

    def _order(self, parsed, selected):
        """Return the selected job after each job it needs, in workflow order."""

        pending = set()
        visiting = set()

        def walk(job_id, field):
            if job_id in pending:
                return
            if job_id in visiting:
                _invalid(f"{field}: dependency cycle", field)
            if job_id not in parsed:
                _invalid(f"{field}: dependency is outside the selection", field)
            visiting.add(job_id)
            for need in parsed[job_id]["needs"]:
                walk(need, f"jobs.{job_id}.needs")
            visiting.remove(job_id)
            pending.add(job_id)

        walk(selected, f"jobs.{selected}")
        order = []
        remaining = set(pending)
        while remaining:
            ready = [
                job_id
                for job_id in parsed
                if job_id in remaining
                and all(need not in remaining for need in parsed[job_id]["needs"])
            ]
            if not ready:
                _invalid(f"jobs.{selected}.needs: dependency cycle", f"jobs.{selected}.needs")
            order.append(ready[0])
            remaining.remove(ready[0])
        return order

    def _job(self, jobs, job_id):
        job_node = jobs[job_id][1]
        job_field = f"jobs.{job_id}"
        job_body = self._mapping(job_node, job_field)
        self._allow(job_body, job_field, JOB_KEYS)
        recorded = {
            "id": job_id,
            "location": _location(job_node),
            "name": self._optional_string(job_body, job_field, "name"),
            "needs": self._needs(job_body, job_field, job_id),
            "runs_on": self._runs_on(job_body, job_field),
            "defaults": self._defaults(job_body, job_field),
            "env": self._env(job_body, job_field),
            "outputs": self._outputs(job_body, job_field),
            "timeout_minutes": self._timeout_minutes(job_body, job_field),
            "steps": self._steps(job_body, job_field),
        }
        condition = self._if_text(job_body, job_field, check_job_if)
        if condition is not None:
            recorded["if"] = condition
        return recorded

    def _needs(self, items, field, job_id):
        if "needs" not in items:
            return []
        return self._needs_value(items["needs"][1], _join(field, "needs"), job_id, enter=True)

    def _needs_value(self, node, path, job_id, enter):
        if enter:
            self._enter(node, path)
        if isinstance(node, ScalarNode) and node.tag == STR_TAG:
            names = [node.value]
        elif isinstance(node, SequenceNode) and node.tag == "tag:yaml.org,2002:seq":
            if enter:
                names = []
                for index, child in enumerate(node.value):
                    names.append(self._string_scalar(child, _join(path, index)))
            else:
                names = []
                for child in node.value:
                    if not isinstance(child, ScalarNode) or child.tag != STR_TAG:
                        _invalid(f"{path} must name a job", path)
                    names.append(child.value)
        else:
            _invalid(f"{path} must name a job", path)
        seen = []
        for name in names:
            if not isinstance(name, str) or name.strip() == "" or "\0" in name:
                _invalid(f"{path} must name a job", path)
            if name == job_id or name in seen:
                _invalid(f"{path}: dependency cycle", path)
            seen.append(name)
        return seen

    def _outputs(self, items, field):
        if "outputs" not in items:
            return {}
        path = _join(field, "outputs")
        body = self._mapping(items["outputs"][1], path)
        recorded = {}
        for key, (_, value) in body.items():
            if key.strip() == "" or "\0" in key:
                _invalid(f"{path}: expression is not accepted", path)
            output_field = _join(path, key)
            self._enter(value, output_field)
            if not isinstance(value, ScalarNode) or value.tag not in SCALAR_TAGS:
                _invalid(f"{output_field}: expression is not accepted", output_field)
            text = value.value
            if not isinstance(text, str) or text.strip() == "" or "\0" in text:
                _invalid(f"{output_field}: expression is not accepted", output_field)
            self._check_expression(text, output_field, check_job_output)
            recorded[key] = text
        return recorded

    def _if_text(self, items, field, check):
        """Store `if` text. Parsing checks the shape and does not evaluate it."""

        if "if" not in items:
            return None
        path = _join(field, "if")
        node = items["if"][1]
        self._enter(node, path)
        if not isinstance(node, ScalarNode) or node.tag not in SCALAR_TAGS:
            _invalid(f"{path}: expression is not accepted", path)
        text = node.value
        if not isinstance(text, str) or text.strip() == "" or "\0" in text:
            _invalid(f"{path}: expression is not accepted", path)
        self._check_expression(text, path, check)
        return text

    def _check_expression(self, text, path, check):
        try:
            check(text)
        except ExprError as exc:
            message = str(exc)
            if message == "function is not available: hashFiles":
                raise PlanError(
                    "CAPABILITY_UNSUPPORTED",
                    f"{path}: hashFiles: capability is unsupported",
                    path,
                ) from None
            if message.startswith("context is not available:"):
                raise PlanError("WORKFLOW_INVALID", f"{path}: {message}", path) from None
            raise PlanError(
                "WORKFLOW_INVALID", f"{path}: expression is not accepted", path
            ) from None

    def _steps(self, items, field):
        if "steps" not in items:
            _invalid(f"{field} is not sequential run steps", _join(field, "steps"))
        path = _join(field, "steps")
        node = items["steps"][1]
        self._enter(node, path)
        if not isinstance(node, SequenceNode) or node.tag != "tag:yaml.org,2002:seq":
            _invalid(f"{path} must be a sequence", path)
        if not node.value:
            _invalid(f"{field} is not sequential run steps", path)
        steps = []
        for index, child in enumerate(node.value):
            step_field = _join(path, index)
            body = self._mapping(child, step_field)
            self._allow(body, step_field, STEP_KEYS)
            if "run" not in body:
                _invalid(f"{field} is not sequential run steps", step_field)
            recorded = {
                "index": index,
                "location": _location(child),
                "id": self._optional_string(body, step_field, "id"),
                "name": self._optional_string(body, step_field, "name"),
                "run": self._string_scalar(body["run"][1], _join(step_field, "run")),
                "shell": self._optional_string(body, step_field, "shell"),
                "working_directory": self._optional_string(body, step_field, "working-directory"),
                "env": self._env(body, step_field),
            }
            condition = self._if_text(body, step_field, check_step_if)
            if condition is not None:
                recorded["if"] = condition
            timeout_minutes = self._step_timeout_minutes(body, step_field)
            if timeout_minutes is not None:
                recorded["timeout_minutes"] = timeout_minutes
            steps.append(recorded)
        return steps


def plan_workflow(workflow, job_id):
    """Plan workflow bytes for one job. The same bytes and job produce the same digest."""

    return _Planner().plan(workflow, job_id)


def plan_snapshot(snapshot_dir, job_id):
    """Plan the workflow bytes stored in a capture snapshot. Does not verify hashes."""

    root = Path(snapshot_dir)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        workflow = manifest["workflow"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        _invalid("snapshot workflow is not readable")
    if not isinstance(workflow, str):
        _invalid("snapshot workflow is not readable")
    relative = PurePosixPath(workflow)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        _invalid("snapshot workflow path is not accepted")
    target = root / "files" / Path(*relative.parts)
    try:
        if target.is_symlink() or not target.is_file():
            _invalid("snapshot workflow is not a regular file")
        workflow_bytes = target.read_bytes()
    except OSError:
        _invalid("snapshot workflow is not readable")
    return plan_workflow(workflow_bytes, job_id)
