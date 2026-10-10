# SPDX-License-Identifier: MPL-2.0

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

import base64
import json
import re
from urllib.parse import quote

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
# A run of letters and digits after the four-character token markers.
# github_pat_ also takes underscores. The marker alone is not a mask.
_TOKEN_PREFIX = re.compile(r"gh(?:p|o|u|s|r)_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+")


class MaskList(list):
    """Exact registered values, plus secret and warning records.

    Encoded forms are derived while masking. They are not stored here,
    so equality with a list of `add-mask` values stays the same.
    """

    def __init__(self):
        super().__init__()
        self.secrets = []
        self.warnings = []
        self.omissions = []


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


def register_mask(masks, value, *, secret=False, name=None):
    """Register one value. An empty or whitespace-only value is not a mask.

    A secret is omitted from a job output that contains it. `add-mask`
    is not a secret. A value or word shorter than 4 characters warns.
    The warning names `name` when the caller passes one, and it does
    not include the value.
    """

    if not isinstance(value, str) or value.strip() == "":
        return
    _warn_short(masks, value, name)
    if secret:
        secrets = getattr(masks, "secrets", None)
        if isinstance(secrets, list) and value not in secrets:
            secrets.append(value)
    masks.append(value)


def mask_prefixes(text):
    """Replace token-prefix runs. The marker with no tail is left as-is."""

    if not isinstance(text, str) or text == "":
        return text
    return _TOKEN_PREFIX.sub(_MASK, text)


def process_stdout(text, masks):
    """Return the stored stdout and register masks from complete command lines.

    `masks` is a list of exact `add-mask` values for this job. The command
    line itself is not logged. A partial line is held until the stream
    finishes, and it is not treated as a command. A line before
    `add-mask` stays unmasked.
    """

    if text == "":
        return ""
    stream = LogStream(masks, commands=True)
    return stream.feed(text) + stream.finish()


def mask_stored_copy(text, masks):
    """Mask a stored copy of a file or a log. Lines are not joined."""

    if text == "":
        return ""
    stream = LogStream(masks, commands=False)
    return stream.feed(text) + stream.finish()


def mask_text(text, masks):
    """Replace registered values, their words, encoded forms, and token prefixes.

    The longest match at each index wins and is replaced by `***`. The
    workflow commands page shows that replacement for `Mona The Octocat`.
    An encoded form shorter than 4 characters is not used.
    """

    if text == "":
        return text
    needles = _needles(masks)
    parts = []
    index = 0
    length = len(text)
    while index < length:
        matched = _match_at(text, index, needles)
        if matched is None:
            parts.append(text[index])
            index += 1
            continue
        parts.append(_MASK)
        index += len(matched)
    return "".join(parts)


def job_output_value(text, masks, name):
    """Return masked job-output text, or None when the output is omitted.

    An injected secret or a token prefix omits the output. The omission
    record names the output and does not include the value. An `add-mask`
    value is stored as masked text.
    """

    if _contains_secret(text, masks) or _TOKEN_PREFIX.search(text):
        _record_omission(masks, name)
        return None
    return mask_text(text, masks)


class LogStream:
    """Mask complete lines and hold a trailing partial line.

    The held text is the unfinished line, so a value split across two
    reads of that line is masked before it is stored. When the unfinished
    line is at least as long as the longest mask, the hold is too. A
    value split across two lines is not joined.
    """

    def __init__(self, masks, commands=True):
        self.masks = masks
        self.commands = commands
        self.held = ""
        self.stopped = None

    def feed(self, text):
        if text == "":
            return ""
        data = self.held + text
        if data.endswith("\n"):
            lines = data.split("\n")[:-1]
            self.held = ""
        else:
            parts = data.split("\n")
            self.held = parts[-1]
            lines = parts[:-1]
        kept = []
        for line in lines:
            rendered = self._one(line)
            if rendered is not None:
                kept.append(rendered)
        if not kept:
            return ""
        return "\n".join(kept) + "\n"

    def finish(self):
        if self.held == "":
            return ""
        line = self.held
        self.held = ""
        return mask_text(line, self.masks)

    def _one(self, line):
        if not self.commands:
            return mask_text(line, self.masks)
        if self.stopped is not None:
            if line == f"::{self.stopped}::":
                self.stopped = None
                return None
            return mask_text(line, self.masks)
        matched = _COMMAND.fullmatch(line)
        if matched is None:
            return mask_text(line, self.masks)
        name = matched.group(1).lower()
        value = matched.group(3)
        if name == "add-mask":
            register_mask(self.masks, value)
            return None
        if name == "stop-commands":
            if value != "":
                self.stopped = value
            return None
        if name in _DROPPED:
            return None
        if name in _MESSAGES:
            return mask_text(value, self.masks)
        return mask_text(line, self.masks)


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
    return not (name in _IGNORED_NAMES or name.startswith(_IGNORED_PREFIXES))


def _accept_output(name):
    return OUTPUT_NAME.fullmatch(name) is not None


def _warn_short(masks, value, name):
    short = len(value) < 4 or any(len(word) < 4 for word in value.split())
    if not short:
        return
    if name:
        message = f"secret {name} is shorter than 4 characters"
    else:
        message = "registered value is shorter than 4 characters"
    warnings = getattr(masks, "warnings", None)
    if isinstance(warnings, list) and message not in warnings:
        warnings.append(message)


def _encoded_forms(value):
    """Base64 at three byte offsets, the JSON string contents, and the URI form.

    The value itself is registered with padding and with trailing `=`
    removed. Each offset keeps the form with trailing `=` removed.
    A form shorter than 4 characters is dropped.
    """

    raw = value.encode("utf-8")
    forms = []
    full = base64.b64encode(raw).decode("ascii")
    forms.append(full)
    trimmed = full.rstrip("=")
    if trimmed != full:
        forms.append(trimmed)
    for offset in (1, 2):
        if len(raw) <= offset:
            continue
        forms.append(base64.b64encode(raw[offset:]).decode("ascii").rstrip("="))
    forms.append(_json_contents(value))
    forms.append(quote(value, safe="-._~"))
    kept = []
    seen = set()
    for item in forms:
        if not item or len(item) < 4 or item in seen:
            continue
        seen.add(item)
        kept.append(item)
    return kept


def _json_contents(value):
    """JSON string contents, without the surrounding quotation marks."""

    try:
        dumped = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    if len(dumped) < 2 or not dumped.startswith('"') or not dumped.endswith('"'):
        return ""
    return dumped[1:-1]


def _needles(masks):
    found = []
    seen = set()
    for item in masks:
        if not isinstance(item, str) or item.strip() == "":
            continue
        for candidate in (item, *item.split(), *_encoded_forms(item)):
            if candidate and candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    found.sort(key=len, reverse=True)
    return found


def _match_at(text, index, needles):
    prefix = _TOKEN_PREFIX.match(text, index)
    matched = prefix.group(0) if prefix is not None else None
    matched_len = len(matched) if matched is not None else 0
    for needle in needles:
        if len(needle) <= matched_len:
            break
        if text.startswith(needle, index):
            return needle
    return matched


def _contains_secret(text, masks):
    secrets = getattr(masks, "secrets", None)
    if not isinstance(secrets, list):
        return False
    for secret in secrets:
        if not isinstance(secret, str) or secret.strip() == "":
            continue
        if secret in text:
            return True
        for word in secret.split():
            if word and word in text:
                return True
    return False


def _record_omission(masks, name):
    omissions = getattr(masks, "omissions", None)
    if isinstance(omissions, list) and name not in omissions:
        omissions.append(name)
    warnings = getattr(masks, "warnings", None)
    message = f"output {name} omitted"
    if isinstance(warnings, list) and message not in warnings:
        warnings.append(message)
