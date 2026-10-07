"""Versioned plan for one selected job and the jobs it needs.

Parsing does not pull images, start containers, or accept a run. It fetches
an action only through a caller-supplied store. Capability version 12 records
declared fields, including step and job
`if` text, job output expressions, local composite actions read from a
snapshot, a literal job matrix, a local reusable workflow, service
containers, an owned checkout of the captured files for
`uses: actions/checkout@v` plus digits and for `actions/checkout` pinned
by a full commit SHA, an owned upload for `actions/upload-artifact` and
`github/codeql-action/upload-sarif` pinned by a full commit SHA, and a
read-only `permissions` value.
That checkout does not fetch a ref, replace
those files, or persist a credential. The tag and the SHA are stored and
are not resolved or verified. `fetch-depth` accepts only the YAML integer
0. An omitted `fetch-depth` leaves the checkout as it is.
The owned upload stores its `uses` string and does not fetch the action,
read an action file, or run its program. It checks that expressions can be
parsed and does not evaluate them. `run`, `env`, `with`, and step and
job `name` are checked, including mixed text. The plan stores the
source. An exact `secrets.NAME` is accepted in step `env` and step
`with` and stored as that text. An exact `secrets.NAME` in step `run`
is accepted when the shell is bash or sh and the lexer proves the
context, and the plan stores that text. Any other `secrets` read in
those fields is refused. `secrets` stays withheld in names, `if`,
job `env`, workflow `env`, and concurrency. Workflow `name`, service `env`, and
action output `value` that is not one whole expression stay unchecked.
`concurrency` on a workflow or job is stored, including mixed text in
the group. `queue: max` with `cancel-in-progress: true` is rejected.
`hashFiles` is unsupported.
Status functions are not accepted there. Matrix `include` and `exclude` are expanded here. A matrix value that
is itself an expression is rejected. A called workflow is read from the
snapshot. A remote workflow reference is rejected. Secrets are not passed
to a called workflow. A service image must be pinned by digest. GitHub
accepts a tag or registry name there. `credentials`, `volumes`, `options`,
and `ports` are rejected. A selected job includes the jobs it needs. A
dependency that is not defined in the workflow is rejected. Anything this
slice cannot describe is rejected. A remote action pinned by a
40-character lowercase commit SHA is read from the caller-supplied store.
The planner does not fetch one when that store is absent. A remote
action with ``runs.using: node24`` and ``main`` is one step. ``post``
is recorded on that step. ``post-if`` defaults to ``always()`` when it
is omitted. ``node20``, Docker, ``pre``, and ``pre-if`` are rejected
by name. A version 11 plan is not migrated.
"""

import hashlib
import json
import math
import re
import shlex
from pathlib import Path, PurePosixPath

import yaml
from yaml.constructor import SafeConstructor
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .actions import ActionStorageFull, ActionUnavailable, full_sha, parse_remote_uses
from .expr import (
    ExprError,
    check_call_default,
    check_call_output,
    check_call_with,
    check_job_env,
    check_job_if,
    check_job_name,
    check_job_output,
    check_action_default,
    check_step_if,
    check_job_concurrency,
    check_step_run,
    check_step_secret_value,
    check_step_text,
    check_workflow_concurrency,
    check_workflow_env,
)
from .protocol import canonical

CAPABILITY_VERSION = 12
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
INT_TAG = "tag:yaml.org,2002:int"
MERGE_TAG = "tag:yaml.org,2002:merge"
SCALAR_TAGS = {
    STR_TAG,
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    BOOL_TAG,
    "tag:yaml.org,2002:null",
}
# These names change execution or hide work. Reject them wherever they appear.
# `strategy` is accepted only on a job. `matrix` is accepted only inside that
# strategy. A matrix generates at most 256 jobs per workflow run, on
# GitHub-hosted and self-hosted runners.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
# https://docs.github.com/en/actions/reference/limits
MAX_MATRIX_JOBS = 256
# The top-level caller workflow plus up to nine called workflows is ten
# levels. Exactly ten is allowed. An eleventh level does not start a run.
# https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#nesting-reusable-workflows
MAX_WORKFLOW_LEVELS = 10
REUSE_WORKFLOWS = "https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows"
# Unique reusable workflows called from the top-level workflow file,
# including nested trees. Exactly 50 is allowed.
# https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#limitations-of-reusable-workflows
MAX_CALLED_WORKFLOWS = 50
REUSE_CONFIGURATIONS = (
    "https://docs.github.com/en/actions/reference/workflows-and-actions/"
    "reusing-workflow-configurations"
)
# `workflow_call` is accepted only under `on`. `secrets` stays rejected, so
# a called workflow does not receive secrets implicitly or by name.
# `services` is accepted only on a concrete job. A caller job has no service
# block of its own; the called workflow's jobs may.
FORBIDDEN = {
    "matrix",
    "secrets",
    "privileged",
    "container",
}
WORKFLOW_KEYS = {"name", "on", "jobs", "defaults", "env", "permissions", "concurrency"}
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
    "strategy",
    "services",
    "permissions",
    "concurrency",
}
# jobs.<job_id>.services.<service_id>. `credentials` would carry a registry
# login. `volumes` can bind a host path. `options` is passed to
# `docker create`, and GitHub warns that `--network` is not supported there.
# `ports` publishes a host port. Those four stay unsupported. A container
# job reaches the service by its label on a user-defined bridge network.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
SERVICE_KEYS = {"image", "env", "command", "entrypoint"}
# The service label is the hostname on that network. One DNS label, so the
# name can be used as a hostname. This is not a count of services. GitHub
# documents no service-count limit, and this planner adds none.
_SERVICE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# Same digest pin as a job image. GitHub allows a Docker Hub name or a
# registry name, including a floating tag. This engine does not pull one.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
_SERVICE_IMAGE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64})$"
)
STRATEGY_KEYS = {"fail-fast", "max-parallel", "matrix"}
# A job that calls a reusable workflow. GitHub also allows secrets, strategy,
# permissions, and cache-mode. Those stay unsupported. `concurrency` is
# stored. Secrets are not passed, including `secrets: inherit`.
# https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#supported-keywords-for-jobs-that-call-a-reusable-workflow
CALL_JOB_KEYS = {"name", "uses", "with", "needs", "if", "concurrency"}
CALL_TRIGGER_KEYS = {"inputs", "outputs"}
CALL_INPUT_KEYS = {"description", "required", "default", "type"}
CALL_OUTPUT_KEYS = {"description", "value"}
CALL_INPUT_TYPES = {"boolean", "number", "string"}
# Same-repository reusable workflows live directly in `.github/workflows`.
# Subdirectories are not supported.
# https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows
_CALLED_WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
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
# Owned checkout of files already in the workspace. A major tag is
# `actions/checkout@v` plus one or more digits. `actions/checkout@` plus 40
# lowercase hex digits is the same step. The tag and the SHA are stored
# and are not fetched, resolved, or verified. This does not read an action
# file or run the JavaScript action. An omitted `clean`,
# `persist-credentials`, or `fetch-depth` does not mean the upstream default.
# https://docs.github.com/en/actions/reference/security/secure-use
# https://github.com/actions/checkout
CHECKOUT_USES = "actions/checkout@v4"
_CHECKOUT_TAG = re.compile(r"actions/checkout@v[0-9]+")
_CHECKOUT_SHA = re.compile(r"actions/checkout@[0-9a-f]{40}")
CHECKOUT_WITH = {"clean", "persist-credentials", "fetch-depth"}


def owned_checkout_uses(text):
    """Return whether `text` is the owned checkout reference."""

    return isinstance(text, str) and (
        text == CHECKOUT_USES
        or _CHECKOUT_TAG.fullmatch(text) is not None
        or _CHECKOUT_SHA.fullmatch(text) is not None
    )


# Owned upload of workspace files, and one local CodeQL SARIF file. The
# SHA is stored and is not fetched or verified. This does not read an
# action file or run the action program. Inventory pins are examples.
# https://github.com/actions/upload-artifact
# https://github.com/github/codeql-action
_UPLOAD_SHA = re.compile(r"actions/upload-artifact@[0-9a-f]{40}")
_SARIF_SHA = re.compile(r"github/codeql-action/upload-sarif@[0-9a-f]{40}")
_UPLOAD_FILE_KEYS = {
    "name",
    "path",
    "if-no-files-found",
    "include-hidden-files",
    "archive",
    "overwrite",
}
_UPLOAD_BOOL_KEYS = {"include-hidden-files", "archive", "overwrite"}
_UPLOAD_NO_FILES = {"warn", "error", "ignore"}
_SARIF_KEYS = {"sarif_file"}
_SARIF_REJECTED = {
    "token",
    "checkout_path",
    "ref",
    "sha",
    "matrix",
    "category",
    "wait-for-processing",
}
_REJECTED_USES = (
    "actions/download-artifact",
    "github/codeql-action/init",
    "github/codeql-action/analyze",
)


def owned_upload_kind(text):
    """Return ``files``, ``sarif``, or None for an owned upload reference."""

    if isinstance(text, str) and _UPLOAD_SHA.fullmatch(text):
        return "files"
    if isinstance(text, str) and _SARIF_SHA.fullmatch(text):
        return "sarif"
    return None


def _rejected_remote_uses(text):
    """Reject download and the other CodeQL entry points before any fetch."""

    if not isinstance(text, str):
        return False
    for prefix in _REJECTED_USES:
        if text == prefix or text.startswith(prefix + "@") or text.startswith(prefix + "/"):
            return True
    return False


# permissions scopes on the workflow syntax page, read 2026-10-04.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions
# The page lists `id-token` as `write|none` and `vulnerability-alerts` as
# `read|none`. This slice accepts `read` or `none` for every scope below
# and rejects `write`. No token is created from the recorded value.
PERMISSION_SCOPES = frozenset(
    {
        "actions",
        "artifact-metadata",
        "attestations",
        "checks",
        "code-quality",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "packages",
        "pages",
        "pull-requests",
        "security-events",
        "statuses",
        "vulnerability-alerts",
    }
)
PERMISSION_ACCESS = frozenset({"read", "none"})
DEFAULT_KEYS = {"run"}
RUN_DEFAULT_KEYS = {"shell", "working-directory"}
# Local composite metadata, plus a remote node24 action that has main.
# post is part of that action. node20, Docker, and pre stay rejected.
# https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax
ACTION_KEYS = {"name", "description", "author", "branding", "inputs", "outputs", "runs"}
COMPOSITE_RUN_KEYS = {"using", "steps"}
NODE24_RUN_KEYS = {"using", "main", "post", "post-if"}
_LIFECYCLE_KEYS = ("pre", "pre-if", "post", "post-if")
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


def _matrix_limit(path):
    raise PlanError(
        "CAPABILITY_UNSUPPORTED",
        f"{path}: a matrix will generate a maximum of 256 jobs per workflow run "
        f"({GITHUB_WORKFLOW_SYNTAX})",
        path,
    )


_MISSING = object()


def _lookup(mapping, name):
    folded = name.casefold()
    for key, value in mapping.items():
        if key.casefold() == folded:
            return value
    return _MISSING


def _product(axes):
    """Yield combinations. The last axis changes fastest.

    The workflow syntax example creates `{version: 10, os: ubuntu-latest}`
    before `{version: 10, os: windows-latest}` when `version` is declared
    first.
    https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-what-your-workflow-does/running-variations-of-jobs-in-a-workflow
    """

    if not axes:
        return
    lengths = [len(values) for _name, values in axes]
    if any(length == 0 for length in lengths):
        return
    indexes = [0] * len(axes)
    while True:
        yield {axes[pos][0]: axes[pos][1][indexes[pos]] for pos in range(len(axes))}
        pos = len(axes) - 1
        while pos >= 0:
            indexes[pos] += 1
            if indexes[pos] < lengths[pos]:
                break
            indexes[pos] = 0
            pos -= 1
        else:
            return


def _excluded(combo, rules):
    """True when any exclude object matches. A partial match is enough.

    https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
    """

    for rule in rules:
        if all(_lookup(combo, key) == value for key, value in rule.items()):
            return True
    return False


def _is_original(key, original_names):
    folded = key.casefold()
    return any(name.casefold() == folded for name in original_names)


def _can_merge(combo, extra, original_names):
    for key, value in extra.items():
        if _is_original(key, original_names) and _lookup(combo, key) != value:
            return False
    return True


def _merge_extra(combo, extra, original_names):
    for key, value in extra.items():
        if _is_original(key, original_names):
            continue
        folded = key.casefold()
        for existing in combo:
            if existing.casefold() == folded:
                combo[existing] = value
                break
        else:
            combo[key] = value


def _expand_matrix(axes, excludes, includes, path):
    """Apply exclude, then include, to the cartesian product.

    Include merges only into combinations from that product. A combination
    created by an earlier include is not a merge target. Original axis values
    are not overwritten. Added values can be overwritten.
    https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
    https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-what-your-workflow-does/running-variations-of-jobs-in-a-workflow
    """

    original_names = [name for name, _values in axes]
    originals = []
    for combo in _product(axes):
        if _excluded(combo, excludes):
            continue
        originals.append(combo)
        if len(originals) > MAX_MATRIX_JOBS:
            _matrix_limit(path)
    created = []
    for extra in includes:
        matched = [combo for combo in originals if _can_merge(combo, extra, original_names)]
        if matched:
            for combo in matched:
                _merge_extra(combo, extra, original_names)
            continue
        created.append(dict(extra))
        if len(originals) + len(created) > MAX_MATRIX_JOBS:
            _matrix_limit(path)
    return originals + created


class _Planner:
    def __init__(self, action_root=None, action_store=None):
        self.seen_stack = [set()]
        self.constructor = SafeConstructor()
        self.action_root = None if action_root is None else Path(action_root)
        self.action_store = action_store
        self.call_stack = []
        self.called_workflows = set()
        self.workflow_level = 1
        self.workflow_shell = None

    def plan(self, workflow, job_id):
        if not isinstance(job_id, str) or job_id == "":
            _invalid("no selected job")
        root = self._root(workflow)
        body = self._mapping(root, "")
        self._allow(body, "", WORKFLOW_KEYS)
        workflow_plan = {
            "location": _location(root),
            "name": self._optional_string(body, "", "name"),
            "on": self._on(body) if "on" in body else None,
            "defaults": self._defaults(body, ""),
            "env": self._env(body, "", check_workflow_env),
        }
        permissions = self._permissions(body, "")
        if permissions is not None:
            workflow_plan["permissions"] = permissions
        concurrency = self._concurrency(body, "", check_workflow_concurrency)
        if concurrency is not None:
            workflow_plan["concurrency"] = concurrency
        self.workflow_shell = workflow_plan["defaults"]["shell"]
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
        if not isinstance(workflow, bytes | bytearray):
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
        seen = self.seen_stack[-1]
        identity = id(node)
        if identity in seen:
            _invalid(f"{field or 'workflow'}: YAML aliases are not accepted", field or None)
        seen.add(identity)

    def _mapping(
        self,
        node,
        field,
        *,
        allow_uses=False,
        allow_strategy=False,
        allow_workflow_call=False,
        forbid=True,
    ):
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
            if forbid and key == "strategy" and not allow_strategy:
                _unsupported(path)
            if forbid and key == "workflow_call" and not allow_workflow_call:
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
        if value is None or isinstance(value, str | bool | int):
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

    def _env(self, items, field, check=None, reserve_secret=False):
        if "env" not in items:
            return {}
        path = _join(field, "env")
        body = self._mapping(items["env"][1], path)
        recorded = {
            key: self._string_scalar(value, _join(path, key)) for key, (_, value) in body.items()
        }
        if reserve_secret:
            for key in recorded:
                if isinstance(key, str) and key.startswith("RR_SECRET_"):
                    key_field = _join(path, key)
                    _invalid(f"{key_field}: env key is not accepted", key_field)
        if check is not None:
            for key, value in recorded.items():
                self._check_expression(value, _join(path, key), check)
        return recorded

    def _services(self, items, field):
        """Record service containers. An omitted key is an empty list.

        `image` is required and must be digest-pinned. `env` is a string map.
        `command` replaces the image command and is split into arguments.
        `entrypoint` replaces the image entrypoint and stays one string.
        Expressions in these fields are not evaluated.
        """

        if "services" not in items:
            return []
        path = _join(field, "services")
        body = self._mapping(items["services"][1], path)
        services = []
        for key, (_, value) in body.items():
            child = _join(path, key)
            if not _SERVICE_ID.fullmatch(key):
                _invalid(f"{child} is not a service hostname", child)
            spec = self._mapping(value, child)
            self._allow(spec, child, SERVICE_KEYS)
            image_field = _join(child, "image")
            if "image" not in spec:
                _invalid(f"{image_field} must be pinned by digest", image_field)
            image = self._string_scalar(spec["image"][1], image_field)
            if not _SERVICE_IMAGE.fullmatch(image):
                _invalid(f"{image_field} must be pinned by digest", image_field)
            recorded = {"id": key, "image": image, "env": self._env(spec, child)}
            if "command" in spec:
                recorded["command"] = self._service_command(
                    spec["command"][1], _join(child, "command")
                )
            if "entrypoint" in spec:
                recorded["entrypoint"] = self._service_entrypoint(
                    spec["entrypoint"][1], _join(child, "entrypoint")
                )
            services.append(recorded)
        return services

    def _service_command(self, node, field):
        text = self._string_scalar(node, field)
        if text.strip() == "":
            _invalid(f"{field} must be a command", field)
        try:
            parts = shlex.split(text)
        except ValueError:
            _invalid(f"{field} must be a command", field)
        if not parts or any("\0" in part or part == "" for part in parts):
            _invalid(f"{field} must be a command", field)
        return parts

    def _service_entrypoint(self, node, field):
        text = self._string_scalar(node, field)
        if text.strip() == "" or "\0" in text or "\n" in text:
            _invalid(f"{field} must be an entrypoint", field)
        return text

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

    def _permissions(self, items, field):
        """Record a read-only permissions value. No token is created."""

        if "permissions" not in items:
            return None
        path = _join(field, "permissions")
        node = items["permissions"][1]
        if isinstance(node, ScalarNode) and node.tag == STR_TAG:
            self._enter(node, path)
            if node.value == "read-all":
                return "read-all"
            _unsupported(path)
        if isinstance(node, MappingNode):
            body = self._mapping(node, path, forbid=False)
            recorded = {}
            for key, (_, value) in body.items():
                child = _join(path, key)
                if key not in PERMISSION_SCOPES:
                    _unsupported(child)
                access = self._string_scalar(value, child)
                if access not in PERMISSION_ACCESS:
                    _unsupported(child)
                recorded[key] = access
            return recorded
        self._enter(node, path)
        _unsupported(path)

    def _job(self, jobs, job_id):
        job_node = jobs[job_id][1]
        job_field = f"jobs.{job_id}"
        job_body = self._mapping(job_node, job_field, allow_uses=True, allow_strategy=True)
        if "uses" in job_body:
            self._allow(job_body, job_field, CALL_JOB_KEYS)
            return self._reusable_job(job_body, job_node, job_field, job_id)
        self._allow(job_body, job_field, JOB_KEYS)
        defaults = self._defaults(job_body, job_field)
        recorded = {
            "id": job_id,
            "location": _location(job_node),
            "name": self._checked_name(job_body, job_field, check_job_name),
            "needs": self._needs(job_body, job_field, job_id),
            "runs_on": self._runs_on(job_body, job_field),
            "defaults": defaults,
            "env": self._env(job_body, job_field, check_job_env),
            "outputs": self._outputs(job_body, job_field),
            "timeout_minutes": self._timeout_minutes(job_body, job_field),
            "strategy": self._strategy(job_body, job_field),
            "services": self._services(job_body, job_field),
            "steps": self._steps(job_body, job_field, defaults["shell"]),
        }
        permissions = self._permissions(job_body, job_field)
        if permissions is not None:
            recorded["permissions"] = permissions
        concurrency = self._concurrency(job_body, job_field, check_job_concurrency)
        if concurrency is not None:
            recorded["concurrency"] = concurrency
        condition = self._if_text(job_body, job_field, check_job_if)
        if condition is not None:
            recorded["if"] = condition
        return recorded

    def _on(self, body):
        return self._on_data(body["on"][1], "on")

    def _on_data(self, node, field):
        """Store `on`. `workflow_call` is allowed here and nowhere else."""

        if isinstance(node, MappingNode):
            items = self._mapping(node, field, allow_workflow_call=True)
            values = {}
            for key, (_, value) in items.items():
                child = _join(field, key)
                if key == "workflow_call":
                    values[key] = self._workflow_call_data(value, child)
                else:
                    values[key] = self._data(value, child, 1)
            return values
        return self._data(node, field, 1)

    def _workflow_call_data(self, node, field):
        if isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:null":
            self._enter(node, field)
            return None
        spec = self._workflow_call_spec(node, field)
        stored = {"inputs": {}, "outputs": {}}
        for item in spec["inputs"]:
            entry = {"type": item["type"], "required": item["required"]}
            if item["default"] is not None:
                entry["default"] = item["default"]
            stored["inputs"][item["name"]] = entry
        for name, expression in spec["outputs"].items():
            stored["outputs"][name] = expression
        return stored

    def _reusable_job(self, items, node, field, job_id):
        uses_field = _join(field, "uses")
        relative = self._reusable_path(items["uses"][1], uses_field)
        passed = self._call_with(items, field)
        loaded = self._load_reusable(relative, uses_field)
        known = {item["name"]: item for item in loaded["inputs"]}
        for name in passed:
            if name not in known:
                _invalid(
                    f"{_join(field, 'with')}.{name}: input is not defined", f"{field}.with.{name}"
                )
        inputs = []
        for item in loaded["inputs"]:
            recorded = {
                "name": item["name"],
                "type": item["type"],
                "required": item["required"],
                "default": item["default"],
                "passed": passed.get(item["name"]),
            }
            if recorded["passed"] is None and item["required"]:
                _invalid(f"{field}.with.{item['name']} is required", f"{field}.with.{item['name']}")
            if recorded["passed"] is not None and "literal" in recorded["passed"]:
                self._require_input_type(
                    item["type"], recorded["passed"]["literal"], f"{field}.with.{item['name']}"
                )
            if item["default"] is not None and "literal" in item["default"]:
                self._require_input_type(
                    item["type"],
                    item["default"]["literal"],
                    f"{uses_field}.inputs.{item['name']}.default",
                )
            inputs.append(recorded)
        recorded = {
            "id": job_id,
            "location": _location(node),
            "name": self._checked_name(items, field, check_job_name),
            "needs": self._needs(items, field, job_id),
            "outputs": {},
            "call": {
                "path": relative,
                "inputs": inputs,
                "outputs": loaded["outputs"],
                "workflow": loaded["workflow"],
                "jobs": loaded["jobs"],
            },
        }
        concurrency = self._concurrency(items, field, check_job_concurrency)
        if concurrency is not None:
            recorded["concurrency"] = concurrency
        condition = self._if_text(items, field, check_job_if)
        if condition is not None:
            recorded["if"] = condition
        return recorded

    def _reusable_path(self, node, field):
        text = self._string_scalar(node, field)
        if "${{" in text:
            _invalid(f"{field}: expression is not accepted", field)
        relative = self._uses_relative(text, field)
        if not _CALLED_WORKFLOW.fullmatch(relative):
            _invalid(f"{field}: reusable workflow path is not accepted", field)
        return relative

    def _call_with(self, items, field):
        if "with" not in items:
            return {}
        path = _join(field, "with")
        body = self._mapping(items["with"][1], path, forbid=False)
        passed = {}
        for key, (_, value) in body.items():
            if key.strip() == "" or "\0" in key:
                _invalid(f"{path}: input is not defined", path)
            passed[key] = self._passed_input(value, _join(path, key))
        return passed

    def _passed_input(self, node, field):
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            self._enter(node, field)
            self._check_expression(node.value, field, check_call_with)
            return {"expression": node.value}
        if not isinstance(node, ScalarNode):
            self._enter(node, field)
            _invalid(f"{field} must match the input type", field)
        return {"literal": self._typed_scalar(node, field)}

    def _load_reusable(self, relative, field):
        """Read one called workflow from the snapshot and plan every job in it."""

        if self.workflow_level + 1 > MAX_WORKFLOW_LEVELS:
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{field}: a workflow can connect a maximum of {MAX_WORKFLOW_LEVELS} levels "
                f"({REUSE_WORKFLOWS}#nesting-reusable-workflows)",
                field,
            )
        if relative in self.call_stack:
            _invalid(f"{field}: reusable workflow loop", field)
        if (
            relative not in self.called_workflows
            and len(self.called_workflows) >= MAX_CALLED_WORKFLOWS
        ):
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{field}: a workflow can call a maximum of {MAX_CALLED_WORKFLOWS} unique "
                f"reusable workflows ({REUSE_CONFIGURATIONS}#limitations-of-reusable-workflows)",
                field,
            )
        self.called_workflows.add(relative)
        self.call_stack.append(relative)
        self.workflow_level += 1
        # Each document has its own node set. A later parse can reuse the
        # id of a node from a document that has already been released.
        self.seen_stack.append(set())
        try:
            return self._parse_reusable(relative, field)
        finally:
            self.seen_stack.pop()
            self.workflow_level -= 1
            self.call_stack.pop()

    def _parse_reusable(self, relative, field):
        payload = self._workflow_bytes(relative, field)
        root = self._root(payload)
        body = self._mapping(root, "")
        self._allow(body, "", WORKFLOW_KEYS)
        if "on" not in body:
            _invalid(f"{field}: workflow_call is required", field)
        spec = self._called_trigger(body["on"][1], "on")
        if "jobs" not in body:
            _invalid(f"{field}: no jobs", field)
        jobs = self._mapping(body["jobs"][1], "jobs")
        if not jobs:
            _invalid(f"{field}: no jobs", field)
        previous_shell = self.workflow_shell
        try:
            called_defaults = self._defaults(body, "")
            self.workflow_shell = called_defaults["shell"]
            parsed = {key: self._job(jobs, key) for key in jobs}
            planned = [parsed[key] for key in self._order_all(parsed)]
            return {
                "inputs": spec["inputs"],
                "outputs": spec["outputs"],
                "workflow": self._called_workflow(body, called_defaults),
                "jobs": planned,
            }
        finally:
            self.workflow_shell = previous_shell

    def _called_workflow(self, body, defaults):
        recorded = {
            "name": self._optional_string(body, "", "name"),
            "env": self._env(body, "", check_workflow_env),
            "defaults": defaults,
        }
        permissions = self._permissions(body, "")
        if permissions is not None:
            recorded["permissions"] = permissions
        concurrency = self._concurrency(body, "", check_workflow_concurrency)
        if concurrency is not None:
            recorded["concurrency"] = concurrency
        return recorded

    def _called_trigger(self, node, field):
        if isinstance(node, ScalarNode) and node.tag == STR_TAG:
            self._enter(node, field)
            if node.value != "workflow_call":
                _invalid(f"{field}: workflow_call is required", field)
            return {"inputs": [], "outputs": {}}
        if isinstance(node, SequenceNode):
            self._enter(node, field)
            names = []
            for index, child in enumerate(node.value):
                names.append(self._string_scalar(child, _join(field, index)))
            if "workflow_call" not in names:
                _invalid(f"{field}: workflow_call is required", field)
            return {"inputs": [], "outputs": {}}
        if not isinstance(node, MappingNode):
            self._enter(node, field)
            _invalid(f"{field}: workflow_call is required", field)
        items = self._mapping(node, field, allow_workflow_call=True)
        if "workflow_call" not in items:
            _invalid(f"{field}: workflow_call is required", field)
        for key, (_, value) in items.items():
            if key != "workflow_call":
                self._data(value, _join(field, key), 1)
        return self._workflow_call_spec(items["workflow_call"][1], _join(field, "workflow_call"))

    def _workflow_call_spec(self, node, field):
        if isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:null":
            self._enter(node, field)
            return {"inputs": [], "outputs": {}}
        if not isinstance(node, MappingNode):
            self._enter(node, field)
            _invalid(f"{field} must be a mapping", field)
        body = self._mapping(node, field)
        self._allow(body, field, CALL_TRIGGER_KEYS)
        return {
            "inputs": self._call_inputs(body, field),
            "outputs": self._call_outputs(body, field),
        }

    def _call_inputs(self, items, field):
        if "inputs" not in items:
            return []
        path = _join(field, "inputs")
        body = self._mapping(items["inputs"][1], path, forbid=False)
        recorded = []
        for key, (_, value) in body.items():
            if key.strip() == "" or "\0" in key:
                _invalid(f"{path}: input is not accepted", path)
            item_field = _join(path, key)
            if not isinstance(value, MappingNode):
                _invalid(f"{item_field} must be a mapping", item_field)
            spec = self._mapping(value, item_field, forbid=False)
            self._allow(spec, item_field, CALL_INPUT_KEYS)
            if "type" not in spec:
                _invalid(f"{item_field}.type is required", f"{item_field}.type")
            kind = self._string_scalar(spec["type"][1], _join(item_field, "type"))
            if kind not in CALL_INPUT_TYPES:
                _invalid(
                    f"{item_field}.type must be boolean, number, or string",
                    f"{item_field}.type",
                )
            required = False
            if "required" in spec:
                required = self._bool_scalar(spec["required"][1], _join(item_field, "required"))
            if "description" in spec:
                self._string_scalar(spec["description"][1], _join(item_field, "description"))
            default = None
            if "default" in spec:
                default = self._input_default(spec["default"][1], _join(item_field, "default"))
            recorded.append({"name": key, "type": kind, "required": required, "default": default})
        return recorded

    def _input_default(self, node, field):
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            self._enter(node, field)
            self._check_expression(node.value, field, check_call_default)
            return {"expression": node.value}
        if not isinstance(node, ScalarNode):
            self._enter(node, field)
            _invalid(f"{field} must match the input type", field)
        return {"literal": self._typed_scalar(node, field)}

    def _call_outputs(self, items, field):
        if "outputs" not in items:
            return {}
        path = _join(field, "outputs")
        body = self._mapping(items["outputs"][1], path, forbid=False)
        recorded = {}
        for key, (_, value) in body.items():
            if key.strip() == "" or "\0" in key:
                _invalid(f"{path}: output is not accepted", path)
            item_field = _join(path, key)
            if not isinstance(value, MappingNode):
                _invalid(f"{item_field} must be a mapping", item_field)
            spec = self._mapping(value, item_field, forbid=False)
            self._allow(spec, item_field, CALL_OUTPUT_KEYS)
            if "description" in spec:
                self._string_scalar(spec["description"][1], _join(item_field, "description"))
            if "value" not in spec:
                _invalid(f"{item_field}.value is required", f"{item_field}.value")
            value_field = _join(item_field, "value")
            text = self._string_scalar(spec["value"][1], value_field)
            if not _whole_expression(text):
                _invalid(f"{value_field}: expression is not accepted", value_field)
            self._check_expression(text, value_field, check_call_output)
            recorded[key] = text
        return recorded

    def _require_input_type(self, kind, value, field):
        """Reject a literal that is not the declared workflow_call input type.

        The syntax page allows boolean, number, or string. A boolean is not a
        number. An omitted default is applied at runtime: false, 0, or "".
        https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
        """

        if kind == "boolean" and type(value) is bool:
            return
        if kind == "number" and type(value) is int:
            return
        if kind == "number" and type(value) is float and math.isfinite(value):
            return
        if kind == "string" and isinstance(value, str):
            return
        _invalid(f"{field} must be a {kind}", field)

    def _workflow_bytes(self, relative, field):
        if self.action_root is None:
            _unsupported(field)
        current = self.action_root
        if current.is_symlink():
            _invalid(f"{field}: workflow file is missing", field)
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                _invalid(f"{field}: workflow file is missing", field)
        if not current.is_file():
            _invalid(f"{field}: workflow file is missing", field)
        try:
            payload = current.read_bytes()
        except OSError:
            _invalid(f"{field}: workflow file is missing", field)
        if len(payload) > MAX_WORKFLOW_BYTES:
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"workflow file exceeds 500 KB per file ({GITHUB_ACTIONS_LIMITS})",
                field,
            )
        return payload

    def _order_all(self, parsed):
        """Return every job in the called workflow, needs first."""

        for job_id, job in parsed.items():
            for need in job["needs"]:
                if need not in parsed or need == job_id:
                    _invalid(
                        f"jobs.{job_id}.needs: dependency is outside the selection",
                        f"jobs.{job_id}.needs",
                    )
        order = []
        pending = set(parsed)
        while pending:
            ready = [
                job_id
                for job_id in parsed
                if job_id in pending
                and all(need not in pending for need in parsed[job_id]["needs"])
            ]
            if not ready:
                _invalid("jobs: dependency cycle", "jobs")
            order.append(ready[0])
            pending.remove(ready[0])
        return order

    def _concurrency(self, items, field, check):
        """Store a concurrency group. The source is evaluated when the run is accepted."""

        if "concurrency" not in items:
            return None
        path = _join(field, "concurrency")
        node = items["concurrency"][1]
        if isinstance(node, ScalarNode) and node.tag == STR_TAG:
            self._enter(node, path)
            if node.value == "" or "\0" in node.value:
                _invalid(f"{path}: group is not accepted", path)
            self._check_expression(node.value, path, check)
            return {"group": node.value, "cancel_in_progress": False, "queue": "single"}
        if not isinstance(node, MappingNode):
            self._enter(node, path)
            _invalid(f"{path} must be a string or a mapping", path)
        body = self._mapping(node, path, forbid=False)
        self._allow(body, path, {"group", "cancel-in-progress", "queue"})
        if "group" not in body:
            _invalid(f"{path}: group is required", path)
        group_field = _join(path, "group")
        group = self._string_scalar(body["group"][1], group_field)
        if group == "" or "\0" in group:
            _invalid(f"{group_field}: group is not accepted", group_field)
        self._check_expression(group, group_field, check)
        cancel = False
        if "cancel-in-progress" in body:
            cancel = self._cancel_in_progress(
                body["cancel-in-progress"][1], _join(path, "cancel-in-progress"), check
            )
        queue = "single"
        if "queue" in body:
            queue_field = _join(path, "queue")
            queue = self._string_scalar(body["queue"][1], queue_field)
            if queue not in {"single", "max"}:
                _invalid(f"{queue_field}: queue is not accepted", queue_field)
        if queue == "max" and cancel is True:
            _invalid(
                f"{path}: queue max cannot be combined with cancel-in-progress",
                path,
            )
        return {"group": group, "cancel_in_progress": cancel, "queue": queue}

    def _cancel_in_progress(self, node, path, check):
        self._enter(node, path)
        if isinstance(node, ScalarNode) and node.tag == BOOL_TAG:
            try:
                value = self.constructor.construct_object(node, deep=False)
            except yaml.YAMLError:
                _invalid(f"{path} must be a boolean", path)
            if type(value) is not bool:
                _invalid(f"{path} must be a boolean", path)
            return value
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            if node.value.strip() == "" or "\0" in node.value:
                _invalid(f"{path}: expression is not accepted", path)
            self._check_expression(node.value, path, check)
            return node.value
        _invalid(f"{path} must be a boolean", path)

    def _strategy(self, items, field):
        if "strategy" not in items:
            return None
        path = _join(field, "strategy")
        body = self._mapping(items["strategy"][1], path, forbid=False)
        self._allow(body, path, STRATEGY_KEYS)
        fail_fast = True
        if "fail-fast" in body:
            fail_fast = self._bool_value(body["fail-fast"][1], _join(path, "fail-fast"))
        max_parallel = None
        if "max-parallel" in body:
            max_parallel = self._max_parallel(body["max-parallel"][1], _join(path, "max-parallel"))
        combinations = None
        if "matrix" in body:
            combinations = self._matrix(body["matrix"][1], _join(path, "matrix"))
        return {
            "fail_fast": fail_fast,
            "max_parallel": max_parallel,
            "combinations": combinations,
        }

    def _bool_value(self, node, path):
        self._enter(node, path)
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            _unsupported(path)
        if not isinstance(node, ScalarNode) or node.tag != BOOL_TAG:
            _invalid(f"{path} must be a boolean", path)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{path} must be a boolean", path)
        if type(value) is not bool:
            _invalid(f"{path} must be a boolean", path)
        return value

    def _max_parallel(self, node, path):
        """Record the declared concurrency. GitHub publishes no numeric ceiling."""

        self._enter(node, path)
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            _unsupported(path)
        if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:int":
            _invalid(f"{path} must be a positive integer", path)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{path} must be a positive integer", path)
        if type(value) is not int or value < 1:
            _invalid(f"{path} must be a positive integer", path)
        return value

    def _matrix(self, node, path):
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            self._enter(node, path)
            _unsupported(path)
        if not isinstance(node, MappingNode):
            self._enter(node, path)
            _invalid(f"{path} must be a mapping", path)
        body = self._mapping(node, path, forbid=False)
        axes = []
        seen = []
        includes = []
        excludes = []
        for key, (_, value) in body.items():
            key_field = _join(path, key)
            if key.casefold() in {"include", "exclude"} and key not in {"include", "exclude"}:
                _invalid(f"{key_field}: matrix variable is not accepted", key_field)
            if key == "include":
                includes = self._matrix_objects(value, key_field)
                continue
            if key == "exclude":
                excludes = self._matrix_objects(value, key_field)
                continue
            if any(key.casefold() == prior for prior in seen):
                _invalid(f"{key_field}: duplicate matrix variable", key_field)
            seen.append(key.casefold())
            axes.append((key, self._axis(value, key_field)))
        return _expand_matrix(axes, excludes, includes, path)

    def _axis(self, node, path):
        self._enter(node, path)
        if isinstance(node, ScalarNode) and node.tag == STR_TAG and _whole_expression(node.value):
            _unsupported(path)
        if not isinstance(node, SequenceNode) or node.tag != "tag:yaml.org,2002:seq":
            _invalid(f"{path} must be a sequence", path)
        values = []
        for index, child in enumerate(node.value):
            values.append(self._data(child, _join(path, index), 1))
        return values

    def _matrix_objects(self, node, path):
        self._enter(node, path)
        if not isinstance(node, SequenceNode) or node.tag != "tag:yaml.org,2002:seq":
            _invalid(f"{path} must be a sequence", path)
        objects = []
        for index, child in enumerate(node.value):
            item_field = _join(path, index)
            if not isinstance(child, MappingNode):
                _invalid(f"{item_field} must be a mapping", item_field)
            body = self._mapping(child, item_field, forbid=False)
            recorded = {}
            for key, (_, value) in body.items():
                if any(key.casefold() == existing.casefold() for existing in recorded):
                    _invalid(f"{item_field}: duplicate matrix variable", item_field)
                recorded[key] = self._data(value, _join(item_field, key), 1)
            objects.append(recorded)
        return objects

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

        return self._condition_text(items, field, "if", check)

    def _condition_text(self, items, field, key, check):
        """Store one condition. Parsing checks the shape and does not evaluate it."""

        if key not in items:
            return None
        path = _join(field, key)
        node = items[key][1]
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
                if message == "context is not available: secrets" and check in (
                    check_job_env,
                    check_workflow_env,
                ):
                    message += "; move this reference to the step env"
                raise PlanError("WORKFLOW_INVALID", f"{path}: {message}", path) from None
            if message in {
                "secrets reference is not accepted",
                "run is not accepted",
                "github.token reference is not accepted",
                "token reference is not accepted",
            }:
                raise PlanError("WORKFLOW_INVALID", f"{path}: {message}", path) from None
            raise PlanError(
                "WORKFLOW_INVALID", f"{path}: expression is not accepted", path
            ) from None

    def _run_shell(self, step_shell, job_shell):
        if step_shell is not None:
            return step_shell
        if job_shell is not None:
            return job_shell
        return self.workflow_shell

    def _steps(self, items, field, job_shell=None):
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
                "name": self._checked_name(body, step_field, check_step_text),
                "shell": self._optional_string(body, step_field, "shell"),
                "working_directory": self._optional_string(body, step_field, "working-directory"),
                "env": self._env(body, step_field, check_step_secret_value, reserve_secret=True),
            }
            if has_run:
                run_field = _join(step_field, "run")
                recorded["run"] = self._string_scalar(body["run"][1], run_field)
                chosen = self._run_shell(recorded["shell"], job_shell)
                self._check_expression(
                    recorded["run"],
                    run_field,
                    lambda source, shell=chosen: check_step_run(source, shell),
                )
            else:
                uses_field = _join(step_field, "uses")
                uses_text = self._string_scalar(body["uses"][1], uses_field)
                if owned_checkout_uses(uses_text):
                    recorded.update(self._checkout(body, step_field, uses_text))
                elif owned_upload_kind(uses_text) is not None:
                    recorded.update(self._owned_upload(body, step_field, uses_text))
                elif _rejected_remote_uses(uses_text):
                    _unsupported(uses_field)
                else:
                    action = self._composite(uses_text, uses_field)
                    recorded["uses"] = uses_text
                    recorded["action_path"] = action["path"]
                    recorded["action_digest"] = action["digest"]
                    recorded["with"] = self._action_with(body, step_field, action["inputs"])
                    recorded["inputs"] = action["inputs"]
                    recorded["outputs"] = action["outputs"]
                    if action.get("javascript") == "node24":
                        recorded["javascript"] = "node24"
                        recorded["main"] = action["main"]
                        if "post" in action:
                            recorded["post"] = action["post"]
                        if "post_if" in action:
                            recorded["post_if"] = action["post_if"]
                    else:
                        recorded["steps"] = action["steps"]
                    if "content_digest" in action:
                        recorded["action_owner"] = action["owner"]
                        recorded["action_repository"] = action["repository"]
                        recorded["action_commit"] = action["commit"]
                        recorded["content_digest"] = action["content_digest"]
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

    def _checkout(self, body, step_field, uses_text):
        """Record an owned checkout of the captured files.

        `actions/checkout` fetches a ref and, by default, persists a
        credential and resets the work tree
        (https://github.com/actions/checkout). This step does neither. The
        `uses` string is stored as written. An omitted key does not mean
        the upstream default of true.
        """

        recorded = {"uses": uses_text, "checkout": "captured"}
        if "with" not in body:
            return recorded
        path = _join(step_field, "with")
        items = self._mapping(body["with"][1], path, allow_uses=True, forbid=False)
        accepted = {}
        for key, (_, value) in items.items():
            field = _join(path, key)
            if key not in CHECKOUT_WITH:
                _unsupported(field)
            if key == "fetch-depth":
                accepted[key] = self._fetch_depth(value, field)
                continue
            if self._bool_scalar(value, field) is not False:
                _unsupported(field)
            accepted[key] = False
        recorded["with"] = accepted
        return recorded

    def _fetch_depth(self, node, field):
        """Accept only the YAML integer 0. The value is not evaluated."""

        self._enter(node, field)
        if not isinstance(node, ScalarNode) or node.tag != INT_TAG:
            _invalid(f"{field} must be an integer", field)
        try:
            value = self.constructor.construct_object(node, deep=False)
        except yaml.YAMLError:
            _invalid(f"{field} must be an integer", field)
        if type(value) is not int:
            _invalid(f"{field} must be an integer", field)
        if value != 0:
            _unsupported(field)
        return 0

    def _owned_upload(self, body, step_field, uses_text):
        """Record an owned upload. The action file is not read."""

        kind = owned_upload_kind(uses_text)
        return {
            "uses": uses_text,
            "upload": kind,
            "with": self._upload_with(body, step_field, kind),
        }

    def _upload_with(self, body, step_field, kind):
        path = _join(step_field, "with")
        required = "sarif_file" if kind == "sarif" else "path"
        required_field = _join(path, required)
        if "with" not in body:
            _invalid(f"{required_field} is required", required_field)
        items = self._mapping(body["with"][1], path, allow_uses=True, forbid=False)
        allowed = _SARIF_KEYS if kind == "sarif" else _UPLOAD_FILE_KEYS
        rejected = _SARIF_REJECTED if kind == "sarif" else set()
        accepted = {}
        for key, (_, value) in items.items():
            field = _join(path, key)
            if key in rejected or key not in allowed:
                _unsupported(field)
            if key in _UPLOAD_BOOL_KEYS:
                flag = self._bool_scalar(value, field)
                if key == "overwrite" and flag is True:
                    _unsupported(field)
                accepted[key] = flag
                continue
            text = self._string_scalar(value, field)
            self._check_expression(text, field, check_step_secret_value)
            if key == "if-no-files-found" and "${{" not in text and text not in _UPLOAD_NO_FILES:
                _invalid(f"{field}: if-no-files-found is not accepted", field)
            accepted[key] = text
        if required not in accepted:
            _invalid(f"{required_field} is required", required_field)
        return accepted

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

        remote = parse_remote_uses(uses_text)
        if remote is not None:
            return self._remote_composite(remote, field)
        relative = self._uses_relative(uses_text, field)
        if self.action_root is None:
            _unsupported(field)
        directory = self._action_directory(relative, field)
        payload = self._action_bytes(directory, field)
        node = self._action_node(payload, field)
        self.seen_stack.append(set())
        try:
            body = self._mapping(node, field)
            return self._composite_body(body, field, relative)
        finally:
            self.seen_stack.pop()

    def _remote_composite(self, remote, field):
        """Inline one fetched composite. The store is supplied by the caller.

        A tag, branch, short SHA, or 64-character pin is not fetched. A
        40-character lowercase SHA is the only full commit pin this slice
        accepts. Without a store, planning still launches no fetch.
        """

        if not full_sha(remote.ref):
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                (
                    f"{field}: capability is unsupported; pin the action with a "
                    f"full 40-character lowercase commit SHA ({GITHUB_WORKFLOW_SYNTAX})"
                ),
                field,
            )
        if self.action_store is None:
            _unsupported(field)
        try:
            stored = self.action_store.resolve(
                remote.owner, remote.repository, remote.path, remote.ref
            )
        except ActionUnavailable as exc:
            raise PlanError(
                "ACTION_UNAVAILABLE",
                f"{field}: action repository could not be fetched",
                field,
            ) from exc
        except ActionStorageFull as exc:
            raise PlanError(
                "STORAGE_FULL",
                "worker storage is full; free space before retrying",
                field,
            ) from exc
        payload = self._action_bytes(stored.path, field)
        node = self._action_node(payload, field)
        self.seen_stack.append(set())
        try:
            body = self._mapping(node, field)
            parsed = self._composite_body(body, field, remote.path, javascript=True)
        finally:
            self.seen_stack.pop()
        parsed["owner"] = remote.owner
        parsed["repository"] = remote.repository
        parsed["commit"] = remote.ref
        parsed["content_digest"] = stored.digest
        return parsed

    def _composite_body(self, body, field, relative, javascript=False):
        self._allow(body, field, ACTION_KEYS)
        name = self._required_text(body, field, "name")
        description = self._required_text(body, field, "description")
        if "author" in body:
            self._string_scalar(body["author"][1], _join(field, "author"))
        if "branding" in body:
            self._data(body["branding"][1], _join(field, "branding"), 1)
        runtime = self._action_steps(body, field, javascript=javascript)
        node24 = isinstance(runtime, dict)
        inputs = self._action_inputs(body, field)
        outputs = self._action_outputs(body, field, require_value=not node24)
        if node24:
            main = runtime["main"]
            digest_body = {
                "path": relative,
                "name": name,
                "description": description,
                "inputs": inputs,
                "outputs": outputs,
                "javascript": "node24",
                "main": main,
            }
            returned = {
                "path": relative,
                "inputs": inputs,
                "outputs": outputs,
                "javascript": "node24",
                "main": main,
            }
            if "post" in runtime:
                digest_body["post"] = runtime["post"]
                returned["post"] = runtime["post"]
            if "post_if" in runtime:
                digest_body["post_if"] = runtime["post_if"]
                returned["post_if"] = runtime["post_if"]
            returned["digest"] = hashlib.sha256(canonical(digest_body).encode("ascii")).hexdigest()
            return returned
        steps = runtime
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
                default_field = _join(item_field, "default")
                item["default"] = self._string_scalar(spec["default"][1], default_field)
                self._check_expression(item["default"], default_field, check_action_default)
            if "deprecationMessage" in spec:
                item["deprecation_message"] = self._string_scalar(
                    spec["deprecationMessage"][1],
                    _join(item_field, "deprecationMessage"),
                )
            recorded[key] = item
        return recorded

    def _action_outputs(self, items, field, require_value=True):
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
                if require_value:
                    _invalid(f"{value_field} is required", value_field)
                recorded[key] = {"description": description}
                continue
            text = self._string_scalar(spec["value"][1], value_field)
            if not require_value:
                recorded[key] = {"description": description}
                continue
            if _whole_expression(text):
                self._check_expression(text, value_field, check_step_if)
            recorded[key] = {"description": description, "value": text}
        return recorded

    def _reject_lifecycle(self, runs, runs_field):
        self._reject_named(runs, runs_field, _LIFECYCLE_KEYS)

    def _reject_named(self, runs, runs_field, keys):
        for key in keys:
            if key not in runs:
                continue
            path = _join(runs_field, key)
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{path}: capability is unsupported ({METADATA_SYNTAX})",
                path,
            )

    def _relative_file(self, text, field):
        if text == "" or "\\" in text or "\0" in text or text.startswith("/"):
            _invalid(f"{field}: action path is not accepted", field)
        parts = text.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            _invalid(f"{field}: action path is not accepted", field)
        return "/".join(parts)

    def _node24_main(self, runs, runs_field):
        """Accept one remote node24 main and its post. pre stays rejected."""

        self._reject_named(runs, runs_field, ("pre", "pre-if"))
        self._allow(runs, runs_field, NODE24_RUN_KEYS)
        main_field = _join(runs_field, "main")
        if "main" not in runs:
            _invalid(f"{main_field} is required", main_field)
        if "post-if" in runs and "post" not in runs:
            post_field = _join(runs_field, "post")
            _invalid(f"{post_field} is required", post_field)
        recorded = {
            "javascript": "node24",
            "main": self._relative_file(
                self._string_scalar(runs["main"][1], main_field), main_field
            ),
        }
        if "post" in runs:
            post_field = _join(runs_field, "post")
            recorded["post"] = self._relative_file(
                self._string_scalar(runs["post"][1], post_field), post_field
            )
        condition = self._condition_text(runs, runs_field, "post-if", check_step_if)
        if condition is not None:
            recorded["post_if"] = condition
        return recorded

    def _composite_upload(self, step_body, step_field, child, index, seen_ids, uses_text):
        """Record one owned upload inside a local composite. Shell is optional."""

        step_id = self._optional_string(step_body, step_field, "id")
        if step_id:
            if step_id in seen_ids:
                _invalid(f"{step_field}: duplicate step id", step_field)
            seen_ids.add(step_id)
        shell = self._optional_string(step_body, step_field, "shell")
        shell_field = _join(step_field, "shell")
        if shell is not None and shell.strip() == "":
            _invalid(f"{shell_field} is required", shell_field)
        recorded = {
            "index": index,
            "location": _location(child),
            "id": step_id,
            "name": self._checked_name(step_body, step_field, check_step_text),
            "shell": shell,
            "working_directory": self._optional_string(step_body, step_field, "working-directory"),
            "env": self._env(step_body, step_field, check_step_secret_value, reserve_secret=True),
        }
        recorded.update(self._owned_upload(step_body, step_field, uses_text))
        condition = self._if_text(step_body, step_field, check_step_if)
        if condition is not None:
            recorded["if"] = condition
        return recorded

    def _action_steps(self, items, field, javascript=False):
        runs_field = _join(field, "runs")
        if "runs" not in items:
            _invalid(f"{runs_field} is required", runs_field)
        body = self._mapping(items["runs"][1], runs_field)
        using_field = _join(runs_field, "using")
        if "using" not in body:
            _invalid(f"{using_field} is required", using_field)
        using = self._string_scalar(body["using"][1], using_field)
        if using == "node24" and javascript:
            return self._node24_main(body, runs_field)
        if using != "composite":
            raise PlanError(
                "CAPABILITY_UNSUPPORTED",
                f"{using_field}: capability is unsupported ({METADATA_SYNTAX})",
                using_field,
            )
        self._reject_lifecycle(body, runs_field)
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
            step_body = self._mapping(child, step_field, allow_uses=not javascript)
            allowed = COMPOSITE_STEP_KEYS
            if not javascript:
                allowed = COMPOSITE_STEP_KEYS | {"uses", "with"}
            self._allow(step_body, step_field, allowed)
            if not javascript and "uses" in step_body:
                if "run" in step_body:
                    _invalid(f"{step_field}: step must be run or uses", step_field)
                uses_field = _join(step_field, "uses")
                uses_text = self._string_scalar(step_body["uses"][1], uses_field)
                if owned_upload_kind(uses_text) is None:
                    _unsupported(uses_field)
                steps.append(
                    self._composite_upload(step_body, step_field, child, index, seen_ids, uses_text)
                )
                continue
            if "with" in step_body:
                _unsupported(_join(step_field, "with"))
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
            run_field = _join(step_field, "run")
            recorded = {
                "index": index,
                "location": _location(child),
                "id": step_id,
                "name": self._checked_name(step_body, step_field, check_step_text),
                "run": self._string_scalar(step_body["run"][1], run_field),
                "shell": shell,
                "working_directory": self._optional_string(
                    step_body, step_field, "working-directory"
                ),
                "env": self._env(
                    step_body, step_field, check_step_secret_value, reserve_secret=True
                ),
            }
            self._check_expression(
                recorded["run"],
                run_field,
                lambda source, shell=shell: check_step_run(source, shell),
            )
            condition = self._if_text(step_body, step_field, check_step_if)
            if condition is not None:
                recorded["if"] = condition
            steps.append(recorded)
        return steps

    def _checked_name(self, items, field, check):
        name = self._optional_string(items, field, "name")
        if name is not None:
            self._check_expression(name, _join(field, "name"), check)
        return name

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
            self._check_expression(text, key_field, check_step_secret_value)
            recorded[key] = text
        return recorded


def plan_workflow(workflow, job_id, action_root=None, action_store=None):
    """Plan workflow bytes for one job. The same bytes and job produce the same digest."""

    return _Planner(action_root, action_store).plan(workflow, job_id)


def snapshot_workflow_bytes(snapshot_dir):
    """Return the workflow bytes stored in a capture snapshot."""

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
        return target.read_bytes()
    except OSError:
        _invalid("snapshot workflow is not readable")


def workflow_on(workflow):
    """Return the stored `on` value without planning jobs."""

    planner = _Planner()
    root = planner._root(workflow)
    body = planner._mapping(root, "", forbid=False)
    if "on" not in body:
        return None
    return planner._on(body)


def plan_snapshot(snapshot_dir, job_id, action_store=None):
    """Plan the workflow bytes stored in a capture snapshot. Does not verify hashes."""

    root = Path(snapshot_dir)
    return plan_workflow(
        snapshot_workflow_bytes(snapshot_dir),
        job_id,
        action_root=root / "files",
        action_store=action_store,
    )


def _checkout_wants_history(step):
    if not isinstance(step, dict) or step.get("checkout") != "captured":
        return False
    raw = step.get("with")
    if not isinstance(raw, dict):
        return False
    return raw.get("fetch-depth") == 0


def _jobs_want_history(jobs):
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if _checkout_wants_history(step):
                return True
        call = job.get("call")
        if isinstance(call, dict) and _jobs_want_history(call.get("jobs")):
            return True
    return False


def plan_needs_history(plan):
    """Return whether the accepted plan has an owned checkout with fetch-depth 0.

    A constant-false `if` still counts. The condition can be an expression,
    and capture cannot know the later result. Composite steps do not carry
    an owned checkout; that uses string is rejected while planning.
    """

    if not isinstance(plan, dict):
        return False
    return _jobs_want_history(plan.get("jobs"))


def remote_action_records(plan):
    """Owner, repository, path, commit, and content digest recorded on the plan."""

    found = []
    seen = set()
    for job in plan.get("jobs", []):
        for step in job.get("steps", []):
            if "content_digest" not in step:
                continue
            record = {
                "owner": step["action_owner"],
                "repository": step["action_repository"],
                "path": step["action_path"],
                "commit": step["action_commit"],
                "digest": step["content_digest"],
            }
            key = tuple(
                record[name] for name in ("owner", "repository", "path", "commit", "digest")
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(record)
    found.sort(key=lambda item: (item["owner"], item["repository"], item["path"], item["commit"]))
    return found
