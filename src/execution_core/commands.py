"""Environment files and stdout workflow commands for one job.

`GITHUB_ENV`, `GITHUB_OUTPUT`, and `GITHUB_PATH` are read after a step
finishes and apply to later steps in that job. The writing step does not
see its own env or PATH update. Stdout commands are the lines documented
for workflow commands. stderr is masked and is not scanned for commands.

`set-env` and `add-path` are recognized and ignored. The current workflow
commands page does not list them. Percent-escapes stay literal because
that page does not specify a decoding table.

This is not a GitHub equivalence claim.
https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands
https://docs.github.com/en/actions/reference/workflows-and-actions/variables
"""

import re

# Same pattern the planner uses for an environment name.
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A property name may start with a letter or "_", then letters, digits, "_", or "-".
# https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
OUTPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_COMMAND = re.compile(r"^::([A-Za-z0-9][A-Za-z0-9_-]*)(?: ([^:\n]*))?::(.*)$")
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands
# Default names GITHUB_* and RUNNER_* are ignored. NODE_OPTIONS is a
# documented security restriction. ROOKRUNNER_* is reserved by this engine.
_IGNORED_PREFIXES = ("GITHUB_", "RUNNER_", "ROOKRUNNER_")
_IGNORED_NAMES = {"NODE_OPTIONS"}
_DROPPED = {"set-env", "add-path", "debug", "endgroup", "echo", "setcommandecho"}
_MESSAGES = {"notice", "warning", "error", "group"}
_MASK = "***"


def parse_env(text):
    """Return env assignments from one `GITHUB_ENV` file.

    A line containing `<<` is a heredoc. The body is joined with newlines
    and has no trailing newline. An unclosed heredoc is not stored. A value
    that contains NUL is not stored. Invalid names are skipped.
    """

    return _parse_assignments(text, _accept_env)


def parse_output(text):
    """Return step outputs from one `GITHUB_OUTPUT` file.

    Names follow the expression property rule, so a hyphen is allowed.
    A value that contains NUL is not stored.
    """

    return _parse_assignments(text, _accept_output)


def parse_path(text):
    """Return PATH directories from one `GITHUB_PATH` file, in file order."""

    found = []
    for line in _lines(text):
        if line == "" or "\0" in line:
            continue
        found.append(line)
    return found


def process_stdout(text, masks):
    """Return the stored stdout and register masks from complete command lines.

    `masks` is a list of exact `add-mask` values for this job. The command
    line itself is not logged. An incomplete final line is logged and is
    not treated as a command. A line before `add-mask` stays unmasked.
    """

    if text == "":
        return ""
    pieces = text.split("\n")
    if text.endswith("\n"):
        complete = pieces[:-1]
        incomplete = None
    else:
        complete = pieces[:-1]
        incomplete = pieces[-1]
    kept = []
    stopped = None
    for line in complete:
        rendered = _one_line(line, masks, stopped)
        if rendered is None and stopped is not None and line == f"::{stopped}::":
            stopped = None
            continue
        if isinstance(rendered, tuple):
            kind, payload = rendered
            if kind == "stop":
                stopped = payload
            continue
        if rendered is not None:
            kept.append(rendered)
    if incomplete is not None:
        kept.append(mask_text(incomplete, masks))
    log = "\n".join(kept)
    if incomplete is None and text.endswith("\n") and kept:
        log += "\n"
    return log


def mask_text(text, masks):
    """Replace registered mask values and their whitespace-separated words.

    The longest match at each index wins and is replaced by `***`. The
    workflow commands page shows that replacement for `Mona The Octocat`.
    """

    needles = []
    seen = set()
    for item in masks:
        if not isinstance(item, str) or item.strip() == "":
            continue
        for candidate in (item, *item.split()):
            if candidate and candidate not in seen:
                seen.add(candidate)
                needles.append(candidate)
    if not needles or text == "":
        return text
    needles.sort(key=len, reverse=True)
    parts = []
    index = 0
    length = len(text)
    while index < length:
        matched = None
        for needle in needles:
            if text.startswith(needle, index):
                matched = needle
                break
        if matched is None:
            parts.append(text[index])
            index += 1
            continue
        parts.append(_MASK)
        index += len(matched)
    return "".join(parts)


def _parse_assignments(text, accept):
    stored = {}
    lines = _lines(text)
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == "":
            index += 1
            continue
        marker = line.find("<<")
        if marker >= 0:
            name = line[:marker]
            delimiter = line[marker + 2 :]
            body = []
            index += 1
            closed = False
            while index < len(lines):
                if lines[index] == delimiter:
                    closed = True
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            if closed:
                _store(stored, name, "\n".join(body), accept)
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            _store(stored, name, value, accept)
        index += 1
    return stored


def _lines(text):
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _store(stored, name, value, accept):
    if "\0" in name or "\0" in value:
        return
    if accept(name):
        stored[name] = value


def _accept_env(name):
    if not ENV_NAME.fullmatch(name):
        return False
    if name in _IGNORED_NAMES or name.startswith(_IGNORED_PREFIXES):
        return False
    return True


def _accept_output(name):
    return OUTPUT_NAME.fullmatch(name) is not None


def _one_line(line, masks, stopped):
    """Return text to keep, None to drop, or ('stop', token)."""

    if stopped is not None:
        if line == f"::{stopped}::":
            return None
        return mask_text(line, masks)
    matched = _COMMAND.fullmatch(line)
    if matched is None:
        return mask_text(line, masks)
    name = matched.group(1).lower()
    value = matched.group(3)
    if name == "add-mask":
        if value.strip() != "":
            masks.append(value)
        return None
    if name == "stop-commands":
        if value != "":
            return ("stop", value)
        return None
    if name in _DROPPED:
        return None
    if name in _MESSAGES:
        return mask_text(value, masks)
    return mask_text(line, masks)
