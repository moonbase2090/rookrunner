"""Versioned plan for one selected job and the jobs it needs.

Parsing does not fetch actions, pull images, start containers, or accept a
run. Capability version 5 records declared fields, including step and job
`if` text, job output expressions, and local composite actions read from a
snapshot. It checks that those expressions can be parsed and does not
evaluate them. A selected job includes the jobs it needs. A dependency that
is not defined in the workflow is rejected. Anything this slice cannot
describe is rejected.
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

CAPABILITY_VERSION = 5
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
STEP_KEYS = {
    "id",
    "name",
    "if",
    "run",
    "shell",
    "working-directory",
    "env",
    "timeout-minutes",
    "uses",
    "with",
}
DEFAULT_KEYS = {"run"}
RUN_DEFAULT_KEYS = {"shell", "working-directory"}
# Local composite metadata only. JavaScript and Docker runtimes are rejected.
# https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax
ACTION_KEYS = {"name", "description", "author", "branding", "inputs", "outputs", "runs"}
COMPOSITE_RUN_KEYS = {"using", "steps"}
COMPOSITE_STEP_KEYS = {"run", "shell", "if", "name", "id", "env", "working-directory"}
ACTION_INPUT_KEYS = {"description", "required", "default", "deprecationMessage"}
ACTION_OUTPUT_KEYS = {"description", "value"}
# inputs.<input_id> and outputs.<output_id> start with a letter or `_` and
# contain only alphanumeric characters, `-`, or `_`.
# https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax
_ACTION_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
METADATA_SYNTAX = (
    "https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax"
)


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


def _whole_expression(text):
    """True when `${{ }}` is the entire stripped string and is not nested."""

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith("${{") and stripped.endswith("}}") and "${{" not in stripped[3:-2]


class _Planner:
    def __init__(self, action_root=None):
        self.seen = set()
        self.constructor = SafeConstructor()
        self.action_root = None if action_root is None else Path(action_root)

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

    def _mapping(self, node, field, *, allow_uses=False, forbid=True):
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
            if key == "uses" and not allow_uses:
                _unsupported(path)
            if forbid and key in FORBIDDEN:
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
            body = self._mapping(child, step_field, allow_uses=True)
            self._allow(body, step_field, STEP_KEYS)
            has_run = "run" in body
            has_uses = "uses" in body
            if has_run and has_uses:
                _invalid(f"{step_field}: step must be run or uses", step_field)
            if not has_run and not has_uses:
                _invalid(f"{field} is not sequential run steps", step_field)
            if has_run and "with" in body:
                _invalid(
                    f"{step_field}: with requires uses",
                    _join(step_field, "with"),
                )
            recorded = {
                "index": index,
                "location": _location(child),
                "id": self._optional_string(body, step_field, "id"),
                "name": self._optional_string(body, step_field, "name"),
                "shell": self._optional_string(body, step_field, "shell"),
                "working_directory": self._optional_string(body, step_field, "working-directory"),
                "env": self._env(body, step_field),
            }
            if has_run:
                recorded["run"] = self._string_scalar(body["run"][1], _join(step_field, "run"))
            else:
                uses_field = _join(step_field, "uses")
                uses_text = self._string_scalar(body["uses"][1], uses_field)
                action = self._composite(uses_text, uses_field)
                recorded["uses"] = uses_text
                recorded["action_path"] = action["path"]
                recorded["action_digest"] = action["digest"]
                recorded["with"] = self._action_with(body, step_field, action["inputs"])
                recorded["inputs"] = action["inputs"]
                recorded["outputs"] = action["outputs"]
                recorded["steps"] = action["steps"]
            condition = self._if_text(body, step_field, check_step_if)
            if condition is not None:
                recorded["if"] = condition
            timeout_minutes = self._step_timeout_minutes(body, step_field)
            if timeout_minutes is not None:
                recorded["timeout_minutes"] = timeout_minutes
            steps.append(recorded)
        return steps

    def _required_text(self, items, field, key):
        path = _join(field, key)
        if key not in items:
            _invalid(f"{path} is required", path)
        value = self._string_scalar(items[key][1], path)
        if value.strip() == "":
            _invalid(f"{path} must be a non-empty string", path)
        return value

    def _bool_scalar(self, node, field):
        self._enter(node, field)
        if not isinstance(node, ScalarNode) or node.tag != BOOL_TAG:
            _invalid(f"{field} must be a boolean", field)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{field} must be a boolean", field)
        if type(value) is not bool:
            _invalid(f"{field} must be a boolean", field)
        return value

    def _uses_relative(self, text, field):
        """Accept `./path` and `$/path`. Anything else stays unsupported.

        `$/` is the same-repository form. The snapshot is those bytes, so
        this does not fetch a ref. `..`, an absolute path, and an empty
        segment are rejected.
        """

        if (
            text == ""
            or "\\" in text
            or "@" in text
            or text.startswith("docker://")
            or text.startswith("/")
        ):
            _unsupported(field)
        if text.startswith("./"):
            raw = text[2:]
        elif text.startswith("$/"):
            raw = text[2:]
        else:
            _unsupported(field)
        if raw == "":
            return ""
        parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            _invalid(f"{field}: action path is not accepted", field)
        return "/".join(parts)

    def _composite(self, uses_text, field):
        """Inline one local composite action. The run executes this plan.

        The metadata page documents no size limit and no nesting limit for
        an action file, so this parser does not add one.
        """

        relative = self._uses_relative(uses_text, field)
        if self.action_root is None:
            _unsupported(field)
        directory = self._action_directory(relative, field)
        payload = self._action_bytes(directory, field)
        node = self._action_node(payload, field)
        body = self._mapping(node, field)
        self._allow(body, field, ACTION_KEYS)
        name = self._required_text(body, field, "name")
        description = self._required_text(body, field, "description")
        if "author" in body:
            self._string_scalar(body["author"][1], _join(field, "author"))
        if "branding" in body:
            self._data(body["branding"][1], _join(field, "branding"), 1)
        inputs = self._action_inputs(body, field)
        outputs = self._action_outputs(body, field)
        steps = self._action_steps(body, field)
        digested = []
        for step in steps:
            digested.append({key: value for key, value in step.items() if key != "location"})
        digest_body = {
            "path": relative,
            "name": name,
            "description": description,
            "inputs": inputs,
            "outputs": outputs,
            "steps": digested,
        }
        digest = hashlib.sha256(canonical(digest_body).encode("ascii")).hexdigest()
        return {
            "path": relative,
            "digest": digest,
            "inputs": inputs,
            "outputs": outputs,
            "steps": steps,
        }

    def _action_directory(self, relative, field):
        current = self.action_root
        if current.is_symlink():
            _invalid(f"{field}: action path is not a regular file", field)
        if relative == "":
            if not current.is_dir():
                _invalid(f"{field}: action file is missing", field)
            return current
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                _invalid(f"{field}: action path is not a regular file", field)
            if not current.is_dir():
                _invalid(f"{field}: action file is missing", field)
        return current

    def _action_bytes(self, directory, field):
        preferred = directory / "action.yml"
        alternate = directory / "action.yaml"
        for candidate in (preferred, alternate):
            if candidate.is_symlink():
                _invalid(f"{field}: action path is not a regular file", field)
        if preferred.is_file():
            chosen = preferred
        elif alternate.is_file():
            chosen = alternate
        else:
            _invalid(f"{field}: action file is missing", field)
        try:
            return chosen.read_bytes()
        except OSError:
            _invalid(f"{field}: action file is missing", field)

    def _action_node(self, payload, field):
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeError:
            _invalid(f"{field}: action file must be UTF-8", field)
        loader = WorkflowLoader(text)
        try:
            try:
                node = loader.get_single_node()
            except yaml.YAMLError:
                _invalid(f"{field}: action YAML is not accepted", field)
        finally:
            loader.dispose()
        if not isinstance(node, MappingNode):
            _invalid(f"{field}: action file must be a mapping", field)
        return node

    def _action_inputs(self, items, field):
        if "inputs" not in items:
            return {}
        path = _join(field, "inputs")
        body = self._mapping(items["inputs"][1], path, allow_uses=True, forbid=False)
        recorded = {}
        for key, (_, value) in body.items():
            item_field = _join(path, key)
            if not _ACTION_ID.fullmatch(key):
                _invalid(f"{item_field}: input id is not accepted", item_field)
            spec = self._mapping(value, item_field)
            self._allow(spec, item_field, ACTION_INPUT_KEYS)
            description_field = _join(item_field, "description")
            if "description" not in spec:
                _invalid(f"{description_field} is required", description_field)
            description = self._string_scalar(spec["description"][1], description_field)
            item = {"description": description, "required": False}
            if "required" in spec:
                item["required"] = self._bool_scalar(
                    spec["required"][1], _join(item_field, "required")
                )
            if "default" in spec:
                item["default"] = self._string_scalar(
                    spec["default"][1], _join(item_field, "default")
                )
            if "deprecationMessage" in spec:
                item["deprecation_message"] = self._string_scalar(
                    spec["deprecationMessage"][1],
                    _join(item_field, "deprecationMessage"),
                )
            recorded[key] = item
        return recorded

    def _action_outputs(self, items, field):
        if "outputs" not in items:
            return {}
        path = _join(field, "outputs")
        body = self._mapping(items["outputs"][1], path, allow_uses=True, forbid=False)
        recorded = {}
        for key, (_, value) in body.items():
            item_field = _join(path, key)
            if not _ACTION_ID.fullmatch(key):
                _invalid(f"{item_field}: output id is not accepted", item_field)
            spec = self._mapping(value, item_field)
            self._allow(spec, item_field, ACTION_OUTPUT_KEYS)
            description_field = _join(item_field, "description")
            if "description" not in spec:
                _invalid(f"{description_field} is required", description_field)
            description = self._string_scalar(spec["description"][1], description_field)
            value_field = _join(item_field, "value")
            if "value" not in spec:
                _invalid(f"{value_field} is required", value_field)
            text = self._string_scalar(spec["value"][1], value_field)
            if _whole_expression(text):
                self._check_expression(text, value_field, check_step_if)
            recorded[key] = {"description": description, "value": text}
        return recorded

    def _action_steps(self, items, field):
        runs_field = _join(field, "runs")
        if "runs" not in items:
            _invalid(f"{runs_field} is required", runs_field)
        body = self._mapping(items["runs"][1], runs_field)
        using_field = _join(runs_field, "using")
        if "using" not in body:
            _invalid(f"{using_field} is required", using_field)
        using = self._string_scalar(body["using"][1], using_field)
        if using != "composite":
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{using_field}: capability is unsupported ({METADATA_SYNTAX})",
                using_field,
            )
        self._allow(body, runs_field, COMPOSITE_RUN_KEYS)
        steps_field = _join(runs_field, "steps")
        if "steps" not in body:
            _invalid(f"{steps_field} is required", steps_field)
        node = body["steps"][1]
        self._enter(node, steps_field)
        if not isinstance(node, SequenceNode) or node.tag != "tag:yaml.org,2002:seq":
            _invalid(f"{steps_field} must be a sequence", steps_field)
        if not node.value:
            _invalid(f"{steps_field}: action has no steps", steps_field)
        steps = []
        seen_ids = set()
        for index, child in enumerate(node.value):
            step_field = _join(steps_field, index)
            step_body = self._mapping(child, step_field)
            self._allow(step_body, step_field, COMPOSITE_STEP_KEYS)
            if "run" not in step_body:
                _invalid(f"{step_field}: action step must be a run step", step_field)
            shell_field = _join(step_field, "shell")
            if "shell" not in step_body:
                _invalid(f"{shell_field} is required", shell_field)
            shell = self._string_scalar(step_body["shell"][1], shell_field)
            if shell.strip() == "":
                _invalid(f"{shell_field} is required", shell_field)
            step_id = self._optional_string(step_body, step_field, "id")
            if step_id:
                if step_id in seen_ids:
                    _invalid(f"{step_field}: duplicate step id", step_field)
                seen_ids.add(step_id)
            recorded = {
                "index": index,
                "location": _location(child),
                "id": step_id,
                "name": self._optional_string(step_body, step_field, "name"),
                "run": self._string_scalar(step_body["run"][1], _join(step_field, "run")),
                "shell": shell,
                "working_directory": self._optional_string(
                    step_body, step_field, "working-directory"
                ),
                "env": self._checked_env(step_body, step_field),
            }
            condition = self._if_text(step_body, step_field, check_step_if)
            if condition is not None:
                recorded["if"] = condition
            steps.append(recorded)
        return steps

    def _checked_env(self, items, field):
        recorded = self._env(items, field)
        path = _join(field, "env")
        for key, value in recorded.items():
            if _whole_expression(value):
                self._check_expression(value, _join(path, key), check_step_if)
        return recorded

    def _action_with(self, items, field, inputs):
        if "with" not in items:
            return {}
        path = _join(field, "with")
        body = self._mapping(items["with"][1], path, allow_uses=True, forbid=False)
        recorded = {}
        for key, (_, value) in body.items():
            key_field = _join(path, key)
            if key not in inputs:
                _invalid(f"{key_field}: input is not defined", key_field)
            text = self._string_scalar(value, key_field)
            if _whole_expression(text):
                self._check_expression(text, key_field, check_step_if)
            recorded[key] = text
        return recorded


def plan_workflow(workflow, job_id, action_root=None):
    """Plan workflow bytes for one job. The same bytes and job produce the same digest."""

    return _Planner(action_root).plan(workflow, job_id)


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
    return plan_workflow(workflow_bytes, job_id, action_root=root / "files")
