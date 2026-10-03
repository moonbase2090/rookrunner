"""Evaluator for step `if`, job `if`, and job output expressions.

Operators, literals, coercion, and functions follow the GitHub Actions
expression reference, including `case`. Context names follow the
context-availability table for the workflow key being checked. An
unavailable context or function is an error. A missing property of an
available value is an empty string. `hashFiles` is not implemented.

`case` evaluates predicates in order and does not evaluate later branches.
A property name may contain `-`, which is the contexts reference rule for
property dereference. `env.MY-VAR` is the property `MY-VAR`. The operators
table does not list arithmetic, and `-` is not subtraction here.

The contexts table lists no special functions for a job output. A function
listed in that column is available only on the keys that name it, so
`success`, `failure`, `always`, and `cancelled` are not accepted in an
output. Ordinary functions are.

This is not a GitHub equivalence claim.
https://docs.github.com/en/actions/reference/workflows-and-actions/expressions
https://docs.github.com/en/actions/reference/workflows-and-actions/contexts

Python eval is not used.
"""

import json
import math
import re

_NUMBER = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
_STATUS = {"success", "failure", "always", "cancelled"}
STEP_IF_CONTEXTS = frozenset(
    {
        "github",
        "needs",
        "strategy",
        "matrix",
        "job",
        "runner",
        "env",
        "vars",
        "steps",
        "inputs",
    }
)
STEP_IF_FUNCTIONS = frozenset(
    {
        "contains",
        "startsWith",
        "endsWith",
        "format",
        "join",
        "toJSON",
        "fromJSON",
        "case",
        "success",
        "failure",
        "always",
        "cancelled",
    }
)
# jobs.<job_id>.if — https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
JOB_IF_CONTEXTS = frozenset({"github", "needs", "vars", "inputs"})
# jobs.<job_id>.outputs.<output_id>
# Special functions: None. Status functions are listed only on `if`.
# https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
OUTPUT_CONTEXTS = frozenset(
    {
        "github",
        "needs",
        "strategy",
        "matrix",
        "job",
        "runner",
        "env",
        "vars",
        "secrets",
        "steps",
        "inputs",
    }
)
OUTPUT_FUNCTIONS = STEP_IF_FUNCTIONS - _STATUS
# jobs.<job_id>.with.<input_id>
# https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
CALL_WITH_CONTEXTS = frozenset({"github", "needs", "strategy", "matrix", "inputs", "vars"})
# on.workflow_call.outputs.<output_id>.value
CALL_OUTPUT_CONTEXTS = frozenset({"github", "jobs", "vars", "inputs"})
# on.workflow_call.inputs.<input_id>.default
CALL_DEFAULT_CONTEXTS = frozenset({"github", "inputs", "vars"})
# The contexts table lists no special functions for these keys.
CALL_FUNCTIONS = OUTPUT_FUNCTIONS


class ExprError(Exception):
    pass


def _reject():
    raise ExprError("expression is not accepted")


def unwrap_expression(source):
    """Return the expression inside one `${{ }}` wrapper, if that is the whole text."""

    if not isinstance(source, str):
        _reject()
    text = source.strip()
    if text.startswith("${{") and text.endswith("}}") and "${{" not in text[3:-2]:
        return text[3:-2].strip()
    return source


def check_step_if(source):
    """Reject a step `if` the subset cannot evaluate. Does not run it."""

    _check(_parse(unwrap_expression(source)), STEP_IF_CONTEXTS, STEP_IF_FUNCTIONS)


def check_job_if(source):
    """Reject a job `if` the subset cannot evaluate. Does not run it."""

    _check(_parse(unwrap_expression(source)), JOB_IF_CONTEXTS, STEP_IF_FUNCTIONS)


def check_job_output(source):
    """Reject a job output expression the subset cannot evaluate."""

    _check(_parse(unwrap_expression(source)), OUTPUT_CONTEXTS, OUTPUT_FUNCTIONS)


def check_call_with(source):
    """Reject a reusable-workflow `with` value the subset cannot evaluate."""

    _check(_parse(unwrap_expression(source)), CALL_WITH_CONTEXTS, CALL_FUNCTIONS)


def check_call_output(source):
    """Reject a `workflow_call` output value the subset cannot evaluate."""

    _check(_parse(unwrap_expression(source)), CALL_OUTPUT_CONTEXTS, CALL_FUNCTIONS)


def check_call_default(source):
    """Reject a `workflow_call` input default the subset cannot evaluate."""

    _check(_parse(unwrap_expression(source)), CALL_DEFAULT_CONTEXTS, CALL_FUNCTIONS)


def mentions_context(source, name):
    """Return whether `name` appears as a context, including in a branch that is not taken."""

    return _mentions(_parse(unwrap_expression(source)), name)


def evaluate(source, values, prior=None, cancelled=False):
    """Evaluate one expression. `values` holds the available context objects."""

    return _eval(_parse(unwrap_expression(source)), values, _functions(prior or [], cancelled))


def step_is_enabled(source, values, prior, cancelled):
    """Return whether a step `if` is true.

    An omitted condition is `success()`. A condition that does not call a
    status function is combined with `success()`, which is the documented
    default for `if`.
    """

    if source is None:
        tree = _parse("success()")
    else:
        tree = _parse(unwrap_expression(source))
        if not _has_status(tree):
            tree = ("and", ("call", "success", ()), tree)
    return _truthy(_eval(tree, values, _functions(prior, cancelled)))


def job_is_enabled(source, values, needed_results, ancestor_failed, cancelled):
    """Return whether a job `if` is true.

    An omitted condition is `success()`. Direct needs that are not `success`
    make `success()` false, so a failed or skipped dependency skips this job.
    `failure()` is true when any transitive dependency failed. `always()`
    stays true. The same default combination as a step `if` applies when the
    expression does not call a status function.
    """

    if source is None:
        tree = _parse("success()")
    else:
        tree = _parse(unwrap_expression(source))
        if not _has_status(tree):
            tree = ("and", ("call", "success", ()), tree)
    functions = _functions([], cancelled)
    functions["success"] = lambda: all(item == "success" for item in needed_results)
    functions["failure"] = lambda: bool(ancestor_failed)

    def always():
        return True

    functions["always"] = always
    functions["cancelled"] = lambda: bool(cancelled)
    return _truthy(_eval(tree, values, functions))


def _check(node, contexts, functions):
    kind = node[0]
    if kind == "name":
        if node[1] not in contexts:
            raise ExprError(f"context is not available: {node[1]}")
        return
    if kind == "call":
        name = node[1]
        if name == "hashFiles":
            raise ExprError("function is not available: hashFiles")
        if name not in functions:
            _reject()
        for arg in node[2]:
            _check(arg, contexts, functions)
        return
    if kind == "lit":
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            _check(child, contexts, functions)
        elif isinstance(child, list):
            for item in child:
                _check(item, contexts, functions)


def _mentions(node, name):
    kind = node[0]
    if kind == "name":
        return node[1] == name
    if kind == "lit":
        return False
    for child in node[1:]:
        if isinstance(child, tuple) and _mentions(child, name):
            return True
        if isinstance(child, list) and any(_mentions(item, name) for item in child):
            return True
    return False


def _has_status(node):
    kind = node[0]
    if kind == "call" and node[1] in _STATUS:
        return True
    if kind == "lit":
        return False
    for child in node[1:]:
        if isinstance(child, tuple) and _has_status(child):
            return True
        if isinstance(child, list) and any(_has_status(item) for item in child):
            return True
    return False


def _parse(source):
    if not isinstance(source, str) or source.strip() == "" or "\0" in source:
        _reject()
    try:
        return _Parser(source).parse()
    except RecursionError:
        _reject()


class _Parser:
    def __init__(self, source):
        self.tokens = list(_tokenize(source))
        self.index = 0

    def parse(self):
        node = self._or()
        if self.tokens[self.index][0] != "eof":
            _reject()
        return node

    def _or(self):
        node = self._and()
        while self._eat("||"):
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._eq()
        while self._eat("&&"):
            node = ("and", node, self._eq())
        return node

    def _eq(self):
        node = self._rel()
        while self.tokens[self.index][0] in {"==", "!="}:
            op = self.tokens[self.index][0]
            self.index += 1
            node = ("cmp", op, node, self._rel())
        return node

    def _rel(self):
        node = self._unary()
        while self.tokens[self.index][0] in {"<", "<=", ">", ">="}:
            op = self.tokens[self.index][0]
            self.index += 1
            node = ("cmp", op, node, self._unary())
        return node

    def _unary(self):
        if self._eat("!"):
            return ("not", self._unary())
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        while True:
            if self._eat("."):
                token = self.tokens[self.index]
                if token[0] == "*":
                    self.index += 1
                    name = "*"
                elif token[0] == "ident":
                    self.index += 1
                    name = token[1]
                else:
                    _reject()
                node = ("prop", node, name)
            elif self._eat("["):
                index = self._or()
                if not self._eat("]"):
                    _reject()
                node = ("index", node, index)
            elif self._eat("("):
                if node[0] != "name":
                    _reject()
                args = []
                if not self._eat(")"):
                    args.append(self._or())
                    while self._eat(","):
                        args.append(self._or())
                    if not self._eat(")"):
                        _reject()
                node = ("call", node[1], tuple(args))
            else:
                return node

    def _primary(self):
        token = self.tokens[self.index]
        kind, value = token
        if kind == "number":
            self.index += 1
            return ("lit", _number(value))
        if kind == "string":
            self.index += 1
            return ("lit", value)
        if kind == "ident":
            self.index += 1
            lowered = value.lower()
            if lowered == "true":
                return ("lit", True)
            if lowered == "false":
                return ("lit", False)
            if lowered == "null":
                return ("lit", None)
            return ("name", value)
        if kind == "(":
            self.index += 1
            node = self._or()
            if not self._eat(")"):
                _reject()
            return node
        _reject()

    def _eat(self, kind):
        if self.tokens[self.index][0] == kind:
            self.index += 1
            return True
        return False


def _tokenize(source):
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "'":
            index += 1
            chars = []
            while index < length:
                if source[index] == "'":
                    if index + 1 < length and source[index + 1] == "'":
                        chars.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                chars.append(source[index])
                index += 1
            else:
                _reject()
            yield ("string", "".join(chars))
            continue
        if char == '"':
            _reject()
        matched = _NUMBER.match(source, index)
        if matched and (index == 0 or source[index - 1] not in {".", "]"}):
            text = matched.group(0)
            # A sign starts a number only at the beginning of a token, which
            # this match already is. Do not treat a second dot as part of it.
            if not _bad_number(text):
                yield ("number", text)
                index = matched.end()
                continue
        for symbol in ("&&", "||", "==", "!=", "<=", ">="):
            if source.startswith(symbol, index):
                yield (symbol, symbol)
                index += len(symbol)
                break
        else:
            if char in "()[].,!*<>":
                yield (char, char)
                index += 1
                continue
            if char.isalpha() or char == "_":
                start = index
                index += 1
                # Property dereference allows "-" in the name. The operators
                # table does not list subtraction, so this is not arithmetic.
                # https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
                while index < length and (source[index].isalnum() or source[index] in "_-"):
                    index += 1
                yield ("ident", source[start:index])
                continue
            _reject()
    yield ("eof", "")


def _bad_number(text):
    body = text[1:] if text.startswith("-") else text
    if len(body) > 1 and body[0] == "0" and body[1] not in ".eExX":
        return True
    return False


def _number(text):
    body = text[1:] if text.startswith("-") else text
    try:
        if body.lower().startswith("0x"):
            return int(text, 16)
        if any(char in body for char in ".eE"):
            value = float(text)
            if not math.isfinite(value):
                _reject()
            return value
        return int(text)
    except ValueError:
        _reject()


def _functions(prior, cancelled):
    conclusions = [_conclusion(record) for record in prior]

    def success():
        return all(item != "failure" for item in conclusions)

    def failure():
        return any(item == "failure" for item in conclusions)

    def always():
        return True

    def was_cancelled():
        return bool(cancelled)

    return {
        "contains": _contains,
        "startsWith": _starts,
        "endsWith": _ends,
        "format": _format,
        "join": _join,
        "toJSON": _to_json,
        "fromJSON": _from_json,
        "success": success,
        "failure": failure,
        "always": always,
        "cancelled": was_cancelled,
    }


def _conclusion(record):
    status = record.get("status")
    if status == "succeeded":
        return "success"
    if status == "failed":
        return "failure"
    if status == "skipped":
        return "skipped"
    _reject()


def _eval(node, values, functions):
    try:
        return _eval_node(node, values, functions)
    except RecursionError:
        _reject()


def _eval_node(node, values, functions):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "name":
        if node[1] not in values:
            raise ExprError(f"context is not available: {node[1]}")
        return values[node[1]]
    if kind == "not":
        return not _truthy(_eval(node[1], values, functions))
    if kind == "and":
        left = _eval(node[1], values, functions)
        if not _truthy(left):
            return left
        return _eval(node[2], values, functions)
    if kind == "or":
        left = _eval(node[1], values, functions)
        if _truthy(left):
            return left
        return _eval(node[2], values, functions)
    if kind == "cmp":
        return _compare(
            node[1], _eval(node[2], values, functions), _eval(node[3], values, functions)
        )
    if kind == "prop":
        return _property(_eval(node[1], values, functions), node[2])
    if kind == "index":
        return _index(_eval(node[1], values, functions), _eval(node[2], values, functions))
    if kind == "call":
        return _call(node[1], node[2], values, functions)
    _reject()


def _call(name, args, values, functions):
    if name == "case":
        return _case(args, values, functions)
    if name == "hashFiles":
        raise ExprError("function is not available: hashFiles")
    if name not in functions:
        _reject()
    if name in _STATUS and args:
        _reject()
    if name == "format" and len(args) < 2:
        _reject()
    if name == "join" and len(args) not in {1, 2}:
        _reject()
    if name in {"contains", "startsWith", "endsWith"} and len(args) != 2:
        _reject()
    if name in {"toJSON", "fromJSON"} and len(args) != 1:
        _reject()
    evaluated = [_eval(arg, values, functions) for arg in args]
    function = functions[name]
    if name == "format":
        return function(evaluated[0], evaluated[1:])
    if name == "join":
        return function(*evaluated)
    if name in _STATUS:
        return function()
    return function(*evaluated)


def _case(args, values, functions):
    if len(args) < 1 or len(args) % 2 == 0:
        _reject()
    index = 0
    while index + 1 < len(args):
        if _truthy(_eval(args[index], values, functions)):
            return _eval(args[index + 1], values, functions)
        index += 2
    return _eval(args[-1], values, functions)


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def _truthy(value):
    # Documented falsy values are false, 0, -0, "", and null.
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _as_number(value):
    kind = _kind(value)
    if kind == "null":
        return 0.0
    if kind == "bool":
        return 1.0 if value else 0.0
    if kind == "number":
        return float(value)
    if kind == "string":
        if value == "":
            return 0.0
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return math.nan
        if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
            return math.nan
        return float(parsed)
    return math.nan


def _number_text(value):
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return str(value)


def _as_string(value):
    kind = _kind(value)
    if kind == "null":
        return ""
    if kind == "bool":
        return "true" if value else "false"
    if kind == "number":
        return _number_text(value)
    if kind == "string":
        return value
    _reject()


def _equal(left, right):
    left_kind = _kind(left)
    right_kind = _kind(right)
    if left_kind != right_kind:
        left_number = _as_number(left)
        right_number = _as_number(right)
        if math.isnan(left_number) or math.isnan(right_number):
            return False
        return left_number == right_number
    if left_kind == "string":
        return left.casefold() == right.casefold()
    if left_kind == "number":
        if isinstance(left, float) and math.isnan(left):
            return False
        if isinstance(right, float) and math.isnan(right):
            return False
        return float(left) == float(right)
    if left_kind in {"array", "object"}:
        return left is right
    return left_kind in {"null", "bool"} and left == right


def _compare(op, left, right):
    if op == "==":
        return _equal(left, right)
    if op == "!=":
        return not _equal(left, right)
    left_kind = _kind(left)
    right_kind = _kind(right)
    if left_kind == right_kind == "string":
        pair = (left.casefold(), right.casefold())
    elif left_kind == right_kind == "number":
        if isinstance(left, float) and math.isnan(left):
            return False
        if isinstance(right, float) and math.isnan(right):
            return False
        pair = (float(left), float(right))
    elif left_kind == right_kind == "bool":
        pair = (1.0 if left else 0.0, 1.0 if right else 0.0)
    elif left_kind == right_kind:
        return False
    else:
        left_number = _as_number(left)
        right_number = _as_number(right)
        if math.isnan(left_number) or math.isnan(right_number):
            return False
        pair = (left_number, right_number)
    left_value, right_value = pair
    if op == "<":
        return left_value < right_value
    if op == "<=":
        return left_value <= right_value
    if op == ">":
        return left_value > right_value
    if op == ">=":
        return left_value >= right_value
    _reject()


def _property(value, name):
    if isinstance(value, dict):
        if name == "*":
            return list(value.values())
        if name in value:
            return value[name]
        return ""
    if isinstance(value, list):
        if name == "*":
            return list(value)
        return [_property(item, name) for item in value]
    return ""


def _index(value, key):
    if isinstance(value, dict):
        if isinstance(key, str) and key in value:
            return value[key]
        if isinstance(key, int) and not isinstance(key, bool) and str(key) in value:
            return value[str(key)]
        return ""
    if isinstance(value, list):
        if isinstance(key, bool) or isinstance(key, float):
            if isinstance(key, float) and math.isfinite(key) and key.is_integer() and key >= 0:
                key = int(key)
            else:
                return ""
        if isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(value):
            return value[key]
        return ""
    return ""


def _contains(search, item):
    if isinstance(search, list):
        return any(_equal(element, item) for element in search)
    return _as_string(item).casefold() in _as_string(search).casefold()


def _starts(search, prefix):
    return _as_string(search).casefold().startswith(_as_string(prefix).casefold())


def _ends(search, suffix):
    return _as_string(search).casefold().endswith(_as_string(suffix).casefold())


def _format(template, replacements):
    text = _as_string(template)
    parts = []
    index = 0
    while index < len(text):
        if text.startswith("{{", index):
            parts.append("{")
            index += 2
            continue
        if text.startswith("}}", index):
            parts.append("}")
            index += 2
            continue
        if text[index] == "{":
            end = text.find("}", index)
            if end < 0:
                _reject()
            slot = text[index + 1 : end]
            if not slot.isdigit():
                _reject()
            number = int(slot)
            if number >= len(replacements):
                _reject()
            parts.append(_as_string(replacements[number]))
            index = end + 1
            continue
        if text[index] == "}":
            _reject()
        parts.append(text[index])
        index += 1
    return "".join(parts)


def _join(value, separator=","):
    if isinstance(value, list):
        return _as_string(separator).join(_as_string(item) for item in value)
    return _as_string(value)


def _to_json(value):
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        _reject()


def _from_json(value):
    if not isinstance(value, str):
        _reject()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        _reject()
