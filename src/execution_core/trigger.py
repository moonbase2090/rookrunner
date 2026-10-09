"""Decide whether a push, pull request, or schedule matches a workflow's `on`.

The pattern rules are the workflow syntax filter cheat sheet. `*` does not
match `/`. `**/` matches zero or more directories. `?` and `+` quantify the
preceding atom. A leading `!` negates, and the last matching pattern wins.
This is not a GitHub-equivalence claim.

https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
"""

import re

from .cron import parse_cron
from .protocol import invalid

# A match past this prefix of the caller-supplied diff does not count.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
PATH_WINDOW = 3000
# A push with more commits than this skips path filters. 1000 still evaluates them.
COMMIT_PATH_LIMIT = 1000
# Local request bounds. They are not GitHub limits.
MAX_CHANGED_FILES = 10000
MAX_PATH_LENGTH = 4096
MAX_PATTERN_LENGTH = 1024
DEFAULT_PULL_REQUEST_TYPES = ("opened", "synchronize", "reopened")
_RANGE = (("a", "z"), ("A", "Z"), ("0", "9"))


def schedule_expressions(on):
    """Return the cron strings listed under ``on.schedule``.

    A workflow with no schedule key returns an empty list. A schedule
    value that is not a list of ``{cron: "<five fields>"}`` entries is
    ``INVALID_PARAMS``, as is a cron this parser rejects. ``timezone``
    and any other key are rejected. This evaluator is UTC only.
    """

    if not isinstance(on, dict) or "schedule" not in on:
        return []
    value = on["schedule"]
    if not isinstance(value, list) or not value:
        invalid("on.schedule cron is not accepted")
    found = []
    for item in value:
        if not isinstance(item, dict):
            invalid("on.schedule cron is not accepted")
        extra = set(item) - {"cron"}
        if "timezone" in extra:
            invalid("on.schedule timezone is not accepted")
        if extra:
            invalid("on.schedule key is not accepted")
        if "cron" not in item:
            invalid("on.schedule cron is not accepted")
        parse_cron(item["cron"])
        found.append(item["cron"])
    return found


def submission_triggered(
    on,
    event_name,
    event,
    activity_type,
    changed_files,
    commit_count,
    diff_unavailable,
):
    """Return whether this event matches `on`. A missing event is false.

    More than 1,000 commits on a push, or an unavailable diff, skips path
    filters only. Branch, tag, and activity-type filters still apply.
    A schedule event matches when ``event.schedule`` is one of the cron
    strings under ``on.schedule``. ``workflow_dispatch`` matches when
    that event is listed as a string, a list member, null, or an empty
    mapping. ``inputs`` and any other key are rejected. Any other event
    name still matches, after a present schedule has been checked for a
    usable cron.
    """

    expressions = schedule_expressions(on)
    if event_name == "schedule":
        if not isinstance(event, dict):
            return False
        listed = event.get("schedule")
        return isinstance(listed, str) and listed in expressions
    if event_name == "workflow_dispatch":
        return _workflow_dispatch(on)
    if event_name not in ("push", "pull_request"):
        return True
    config = _config(on, event_name)
    if config is None:
        return False
    if event_name == "push":
        return _push(config, event, changed_files, commit_count, diff_unavailable)
    return _pull_request(config, event, activity_type, changed_files, diff_unavailable)


def _workflow_dispatch(on):
    """Return whether ``on`` lists workflow_dispatch with no extra keys."""

    config = _config(on, "workflow_dispatch")
    if config is None:
        return False
    if not config:
        return True
    if "inputs" in config:
        invalid("on.workflow_dispatch inputs is not accepted")
    invalid("on.workflow_dispatch key is not accepted")


def _config(on, event_name):
    if isinstance(on, str):
        return {} if on == event_name else None
    if isinstance(on, list):
        if event_name not in on:
            return None
        if not all(isinstance(item, str) for item in on):
            invalid("on must list event names")
        return {}
    if isinstance(on, dict):
        if event_name not in on:
            return None
        value = on[event_name]
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        invalid(f"on.{event_name} must be a mapping")
    return None


def _push(config, event, changed_files, commit_count, diff_unavailable):
    _reject_pair(config, "push", "branches", "branches-ignore")
    _reject_pair(config, "push", "tags", "tags-ignore")
    _reject_pair(config, "push", "paths", "paths-ignore")
    branch_filter = "branches" in config or "branches-ignore" in config
    tag_filter = "tags" in config or "tags-ignore" in config
    path_filter = "paths" in config or "paths-ignore" in config
    kind = None
    name = None
    if branch_filter or tag_filter or path_filter:
        kind, name = _push_ref(event)
        if kind is None:
            return False
    if kind == "branch":
        if branch_filter:
            if not _name_filter(config, "push", "branches", "branches-ignore", name):
                return False
        elif tag_filter:
            return False
    elif kind == "tag":
        if tag_filter:
            if not _name_filter(config, "push", "tags", "tags-ignore", name):
                return False
        elif branch_filter:
            return False
        # Path filters are not evaluated for a tag push.
        return True
    elif kind is None:
        return True
    return _paths(config, "push", changed_files, commit_count, diff_unavailable, push=True)


def _pull_request(config, event, activity_type, changed_files, diff_unavailable):
    _reject_pair(config, "pull_request", "branches", "branches-ignore")
    _reject_pair(config, "pull_request", "paths", "paths-ignore")
    if not isinstance(activity_type, str) or activity_type == "":
        invalid("activity_type is required for pull_request")
    types = (
        DEFAULT_PULL_REQUEST_TYPES
        if "types" not in config
        else _string_list(config["types"], "on.pull_request.types")
    )
    if activity_type not in types:
        return False
    if ("branches" in config or "branches-ignore" in config) and not _name_filter(
        config,
        "pull_request",
        "branches",
        "branches-ignore",
        _base_ref(event),
    ):
        return False
    return _paths(
        config,
        "pull_request",
        changed_files,
        None,
        diff_unavailable,
        push=False,
    )


def _push_ref(event):
    if not isinstance(event, dict) or not isinstance(event.get("ref"), str):
        invalid("event.ref is required")
    ref = event["ref"]
    if ref == "" or "\0" in ref or "\n" in ref or "\r" in ref or len(ref) > 256:
        invalid("event.ref is required")
    if ref.startswith("refs/heads/"):
        name = ref[len("refs/heads/") :]
        kind = "branch"
    elif ref.startswith("refs/tags/"):
        name = ref[len("refs/tags/") :]
        kind = "tag"
    else:
        return None, None
    if name == "":
        invalid("event.ref is required")
    return kind, name


def _base_ref(event):
    pull = event.get("pull_request") if isinstance(event, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    ref = base.get("ref") if isinstance(base, dict) else None
    if (
        not isinstance(ref, str)
        or ref == ""
        or "\0" in ref
        or "\n" in ref
        or "\r" in ref
        or len(ref) > 256
    ):
        invalid("pull_request.base.ref is required")
    return ref


def _reject_pair(config, event_name, left, right):
    if left in config and right in config:
        invalid(f"on.{event_name} defines both {left} and {right}")


def _name_filter(config, event_name, positive, ignore, name):
    if positive in config:
        patterns = _string_list(config[positive], f"on.{event_name}.{positive}")
        return _filter_matches(patterns, name, f"on.{event_name}.{positive}")
    patterns = _string_list(config[ignore], f"on.{event_name}.{ignore}")
    return not _filter_matches(patterns, name, f"on.{event_name}.{ignore}")


def _paths(config, event_name, changed_files, commit_count, diff_unavailable, *, push):
    if "paths" not in config and "paths-ignore" not in config:
        return True
    if diff_unavailable or (push and commit_count is not None and commit_count > COMMIT_PATH_LIMIT):
        return True
    files = list(changed_files)[:PATH_WINDOW]
    if not files:
        return False
    if "paths" in config:
        patterns = _string_list(config["paths"], f"on.{event_name}.paths")
        return any(_filter_matches(patterns, path, f"on.{event_name}.paths") for path in files)
    patterns = _string_list(config["paths-ignore"], f"on.{event_name}.paths-ignore")
    return any(
        not _filter_matches(patterns, path, f"on.{event_name}.paths-ignore") for path in files
    )


def _string_list(value, field):
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = list(value)
    else:
        invalid(f"{field} must be a string or a list of strings")
    for item in items:
        if (
            item == ""
            or "\0" in item
            or "\n" in item
            or "\r" in item
            or len(item) > MAX_PATTERN_LENGTH
        ):
            invalid(f"{field} pattern is not accepted")
    return items


def _filter_matches(patterns, text, field):
    if not any(not _is_negative(pattern) for pattern in patterns):
        invalid(f"{field} must include a pattern without !")
    matched = False
    for pattern in patterns:
        negative, body = _split_negative(pattern)
        try:
            ok = _glob_match(body, text)
        except ValueError:
            invalid(f"{field} pattern is not accepted")
        if ok:
            matched = not negative
    return matched


def _is_negative(pattern):
    return pattern.startswith("!") and not pattern.startswith("\\!")


def _split_negative(pattern):
    if _is_negative(pattern):
        return True, pattern[1:]
    return False, pattern


def _glob_match(pattern, text):
    return re.fullmatch(_compile(pattern), text) is not None


def _compile(pattern):
    parts = ["^"]
    last = None
    index = 0

    def emit(atom):
        nonlocal last
        if last is not None:
            parts.append(last)
        last = atom

    while index < len(pattern):
        if pattern.startswith("**/", index):
            emit("(?:[^/]+/)*")
            index += 3
            continue
        if pattern.startswith("**", index):
            emit(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            emit("[^/]*")
            index += 1
            continue
        if char == "?":
            if last is None:
                raise ValueError("quantifier")
            last = f"(?:{last})?"
            index += 1
            continue
        if char == "+":
            if last is None:
                raise ValueError("quantifier")
            last = f"(?:{last})+"
            index += 1
            continue
        if char == "[":
            atom, index = _class(pattern, index)
            emit(atom)
            continue
        if char == "\\":
            if index + 1 >= len(pattern):
                raise ValueError("escape")
            emit(re.escape(pattern[index + 1]))
            index += 2
            continue
        emit(re.escape(char))
        index += 1
    if last is not None:
        parts.append(last)
    parts.append("$")
    return "".join(parts)


def _class(pattern, index):
    index += 1
    body = []
    first = True
    while index < len(pattern):
        char = pattern[index]
        if char == "]" and not first:
            if not body:
                raise ValueError("class")
            return _class_pattern(body), index + 1
        if char == "\\" and index + 1 < len(pattern):
            body.append(("lit", pattern[index + 1]))
            index += 2
            first = False
            continue
        if (
            char == "-"
            and body
            and body[-1][0] == "lit"
            and index + 1 < len(pattern)
            and pattern[index + 1] != "]"
        ):
            end = pattern[index + 1]
            start = body[-1][1]
            if not any(low <= start <= end <= high for low, high in _RANGE):
                raise ValueError("range")
            body[-1] = ("range", start, end)
            index += 2
            first = False
            continue
        body.append(("lit", char))
        index += 1
        first = False
    raise ValueError("class")


def _class_pattern(body):
    chars = []
    for item in body:
        if item[0] == "lit":
            chars.append(re.escape(item[1]))
        else:
            chars.append(re.escape(item[1]) + "-" + re.escape(item[2]))
    return "[" + "".join(chars) + "]"
