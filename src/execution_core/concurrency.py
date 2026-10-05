"""Concurrency groups for one worker.

The plan stores the source. This module evaluates it when a run is
accepted and decides which queued or running runs in the same group
are cancelled. Group names match case-insensitively. `queue: max`
keeps at most 100 pending runs in a group. A 101st run is cancelled
and the pending runs stay. This is not a GitHub-equivalence claim.

https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
"""

from .expr import ExprError, evaluate, render_text

# Pending runs in one concurrency group when queue is max.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
MAX_PENDING = 100
_MAX_GROUP = 1024


def group_keys(record):
    """Return the casefolded group names stored on a run."""

    found = set()
    items = record.get("concurrency") if isinstance(record, dict) else None
    if not isinstance(items, list):
        return found
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("group"), str):
            found.add(item["group"].casefold())
    return found


def eligible(queued, running):
    """Return the first queued run whose groups are not already running."""

    blocked = set()
    for record in running:
        blocked |= group_keys(record)
    for record in queued:
        if group_keys(record) & blocked:
            continue
        return record
    return None


def decide(peers, new_run):
    """Return run ids to cancel, and whether the new run is past the cap.

    `peers` are queued or running runs. The new run is not one of them.
    A `single` queue cancels other queued runs in that group. A `max`
    queue cancels the new run once 100 are already pending. Cancelling
    the running run requires `cancel_in_progress`. If any group rejects
    the new run, no peer is cancelled.
    """

    groups = new_run.get("concurrency") if isinstance(new_run, dict) else None
    if not isinstance(groups, list) or not groups:
        return set(), False
    cancel = set()
    for spec in groups:
        if not isinstance(spec, dict) or not isinstance(spec.get("group"), str):
            continue
        key = spec["group"].casefold()
        queued = []
        running = []
        for peer in peers:
            if not isinstance(peer, dict) or peer.get("run_id") == new_run.get("run_id"):
                continue
            if peer.get("state") not in {"queued", "running"}:
                continue
            if key not in group_keys(peer):
                continue
            if peer["state"] == "queued":
                queued.append(peer["run_id"])
            else:
                running.append(peer["run_id"])
        if spec.get("queue") == "max" and len(queued) >= MAX_PENDING:
            return set(), True
        if spec.get("queue") != "max":
            cancel.update(queued)
        if spec.get("cancel_in_progress") is True:
            cancel.update(running)
    return cancel, False


def resolve_groups(plan, event, event_name, workflow_path):
    """Evaluate the workflow and selected-job groups for one submission."""

    if not isinstance(plan, dict):
        return []
    workflow = plan.get("workflow") if isinstance(plan.get("workflow"), dict) else {}
    job = plan.get("job") if isinstance(plan.get("job"), dict) else {}
    label = workflow.get("name")
    if not isinstance(label, str) or label == "":
        label = workflow_path
    specs = []
    if isinstance(workflow.get("concurrency"), dict):
        specs.append((workflow["concurrency"], False))
    if isinstance(job.get("concurrency"), dict):
        specs.append((job["concurrency"], True))
    call = job.get("call") if isinstance(job.get("call"), dict) else {}
    called = call.get("workflow") if isinstance(call.get("workflow"), dict) else {}
    if isinstance(called.get("concurrency"), dict):
        specs.append((called["concurrency"], False))
    resolved = []
    for spec, job_level in specs:
        values = _values(event, event_name, label, job_level=job_level)
        group = render_text(spec.get("group"), values)
        if not isinstance(group, str) or group == "" or "\0" in group or len(group) > _MAX_GROUP:
            raise ExprError("concurrency group is not accepted")
        cancel = _cancel_flag(spec.get("cancel_in_progress"), values)
        queue = spec.get("queue") or "single"
        if queue not in {"single", "max"}:
            raise ExprError("expression is not accepted")
        if queue == "max" and cancel:
            raise ExprError("queue max cannot be combined with cancel-in-progress")
        resolved.append({"group": group, "cancel_in_progress": cancel, "queue": queue})
    return resolved


def _cancel_flag(flag, values):
    if isinstance(flag, bool):
        return flag
    if not isinstance(flag, str):
        raise ExprError("expression is not accepted")
    value = evaluate(flag, values)
    if type(value) is not bool:
        raise ExprError("expression is not accepted")
    return value


def _values(event, event_name, workflow_label, *, job_level):
    """Contexts for a concurrency expression.

    `github.ref` and `github.ref_name` come from the submission event.
    Step expressions still leave those properties unset. `needs`,
    `strategy`, and `matrix` are empty at acceptance. `vars` is empty.
    `secrets` is not provided.
    """

    github = {}
    if isinstance(event, (dict, list, str, bool)) or event is None:
        github["event"] = event
    elif isinstance(event, int) and not isinstance(event, bool):
        github["event"] = event
    if isinstance(event, dict):
        ref = event.get("ref")
        if isinstance(ref, str) and ref != "":
            github["ref"] = ref
            parts = ref.split("/")
            if len(parts) >= 3 and parts[0] == "refs" and parts[1] in {"heads", "tags", "pull"}:
                github["ref_name"] = "/".join(parts[2:])
            else:
                github["ref_name"] = ref
        repository = event.get("repository")
        if isinstance(repository, dict):
            full_name = repository.get("full_name")
            if isinstance(full_name, str) and full_name != "":
                github["repository"] = full_name
        pull = event.get("pull_request")
        if isinstance(pull, dict):
            head = pull.get("head")
            base = pull.get("base")
            if isinstance(head, dict) and isinstance(head.get("ref"), str) and head["ref"] != "":
                github["head_ref"] = head["ref"]
            if isinstance(base, dict) and isinstance(base.get("ref"), str) and base["ref"] != "":
                github["base_ref"] = base["ref"]
    if isinstance(event_name, str) and event_name != "":
        github["event_name"] = event_name
    if isinstance(workflow_label, str) and workflow_label != "":
        github["workflow"] = workflow_label
    inputs = {}
    if isinstance(event, dict) and isinstance(event.get("inputs"), dict):
        inputs = event["inputs"]
    values = {"github": github, "inputs": inputs, "vars": {}}
    if job_level:
        values["needs"] = {}
        values["strategy"] = {}
        values["matrix"] = {}
    return values
