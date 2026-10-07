"""Evaluator for step `if`, job `if`, job outputs, and text expressions.

Operators, literals, coercion, and functions follow the GitHub Actions
expression reference, including `case`. Context names follow the
context-availability table for the workflow key being checked. An
unavailable context or function is an error. A missing property of an
available value is an empty string. `hashFiles` is not implemented.

`run`, `env`, `with`, and step and job `name` accept mixed text. Each
`${{ }}` is inserted as text. A whole-string expression drops the
surrounding whitespace and is stringified the same way. The inserted
text is not scanned again. Step `env` and `with` accept an expression
that is exactly `secrets.NAME`. Step `run` rewrites that expression to
`${RR_SECRET_NAME}` when the shell is bash or sh and a small lexer
proves an unquoted or double-quoted context. `secrets.GITHUB_TOKEN`
and `github.token` are the same alias and rewrite to
`${RR_SECRET_GITHUB_TOKEN}`. `secrets` stays withheld
in names, `if`, job `env`, workflow `env`, and concurrency.
Status functions are not accepted there.

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
# jobs.<job_id>.steps.run, steps.env, steps.with, and steps.name.
# The table also lists secrets. This subset withholds that context.
# Special functions there are hashFiles only, which is not implemented.
# https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
STEP_TEXT_CONTEXTS = frozenset(
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
STEP_TEXT_FUNCTIONS = OUTPUT_FUNCTIONS
# jobs.<job_id>.env — github, needs, strategy, matrix, vars, secrets, inputs.
# secrets is withheld. No env, job, runner, or steps context.
JOB_ENV_CONTEXTS = frozenset({"github", "needs", "strategy", "matrix", "vars", "inputs"})
# jobs.<job_id>.name — github, needs, strategy, matrix, vars, inputs.
JOB_NAME_CONTEXTS = JOB_ENV_CONTEXTS
JOB_TEXT_FUNCTIONS = OUTPUT_FUNCTIONS
# Workflow env — github, secrets, inputs, vars. secrets is withheld.
# No needs, strategy, matrix, or env context.
WORKFLOW_ENV_CONTEXTS = frozenset({"github", "inputs", "vars"})
# Workflow concurrency — github, inputs, vars. secrets is withheld.
# Job concurrency also allows needs, strategy, and matrix.
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
CONCURRENCY_WORKFLOW_CONTEXTS = WORKFLOW_ENV_CONTEXTS
CONCURRENCY_JOB_CONTEXTS = JOB_NAME_CONTEXTS


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


def check_step_text(source):
    """Reject an expression in step `run`, `env`, `with`, or `name`."""

    check_text(source, STEP_TEXT_CONTEXTS, STEP_TEXT_FUNCTIONS)


_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _exact_secret_tree(tree):
    if (
        isinstance(tree, tuple)
        and len(tree) == 3
        and tree[0] == "prop"
        and isinstance(tree[1], tuple)
        and len(tree[1]) == 2
        and tree[1][0] == "name"
        and tree[1][1] == "secrets"
        and isinstance(tree[2], str)
        and _SECRET_NAME.fullmatch(tree[2])
    ):
        return tree[2]
    return None


def exact_secret_reference(source):
    """Return the name when `source` is exactly ``${{ secrets.NAME }}``."""

    tree = _whole_tree(source)
    if tree is None:
        return None
    return _exact_secret_tree(tree)


def _whole_tree(source):
    if not isinstance(source, str):
        return None
    text = source.strip()
    if not (text.startswith("${{") and text.endswith("}}") and "${{" not in text[3:-2]):
        return None
    try:
        return _parse(text[3:-2].strip())
    except ExprError:
        return None


def _exact_github_token_tree(tree):
    return (
        isinstance(tree, tuple)
        and len(tree) == 3
        and tree[0] == "prop"
        and isinstance(tree[1], tuple)
        and len(tree[1]) == 2
        and tree[1][0] == "name"
        and tree[1][1] == "github"
        and tree[2] == "token"
    )


def _tree_reads_github_token(node):
    if isinstance(node, list) or (
        isinstance(node, tuple) and (not node or not isinstance(node[0], str))
    ):
        return any(_tree_reads_github_token(child) for child in node)
    if not isinstance(node, tuple):
        return False
    if _exact_github_token_tree(node):
        return True
    if (
        len(node) == 3
        and node[0] == "index"
        and isinstance(node[1], tuple)
        and node[1][0] == "name"
        and node[1][1] == "github"
        and isinstance(node[2], tuple)
        and node[2][0] == "lit"
        and node[2][1] == "token"
    ):
        return True
    return any(_tree_reads_github_token(child) for child in node[1:])


def exact_job_token_reference(source):
    """Return ``GITHUB_TOKEN`` for an exact ``secrets.GITHUB_TOKEN`` or ``github.token``."""

    tree = _whole_tree(source)
    if tree is None:
        return None
    secret = _exact_secret_tree(tree)
    if isinstance(secret, str) and secret.upper() == "GITHUB_TOKEN":
        return "GITHUB_TOKEN"
    if _exact_github_token_tree(tree):
        return "GITHUB_TOKEN"
    return None


def text_reads_github_token(source):
    """Return whether any expression in `source` reads ``github.token``."""

    if not isinstance(source, str) or "${{" not in source:
        return False
    try:
        pieces = _text_pieces(source)
    except ExprError:
        return False
    if not pieces:
        return False
    for kind, body in pieces:
        if kind != "expr":
            continue
        try:
            tree = _parse(body)
        except ExprError:
            return False
        if _tree_reads_github_token(tree):
            return True
    return False


def text_needs_job_token(source):
    """Return whether env, with, or run text asks for the job token."""

    if not isinstance(source, str) or "${{" not in source:
        return False
    if exact_job_token_reference(source) == "GITHUB_TOKEN":
        return True
    if text_reads_github_token(source):
        return True
    try:
        pieces = _text_pieces(source)
    except ExprError:
        return False
    if not pieces:
        return False
    for kind, body in pieces:
        if kind != "expr":
            continue
        try:
            tree = _parse(body)
        except ExprError:
            continue
        name = _exact_secret_tree(tree)
        if isinstance(name, str) and name.upper() == "GITHUB_TOKEN":
            return True
    return False


def check_action_default(source):
    """Accept an action default that is exactly the job-token alias.

    Any other read of ``secrets`` or ``github.token`` is refused. The
    plan field names the input.
    """

    if exact_job_token_reference(source) == "GITHUB_TOKEN":
        return
    if text_reads_github_token(source):
        raise ExprError("github.token reference is not accepted")
    if _text_mentions_secrets(source):
        raise ExprError("secrets reference is not accepted")
    check_step_text(source)


def check_step_secret_value(source):
    """Accept an exact ``secrets.NAME`` in step `env` or `with`.

    Any other read of `secrets` in that text is refused. ``GITHUB_TOKEN``
    is the one ``GITHUB_`` name that is accepted. An exact ``github.token``
    is the same alias. Any other ``github.token`` read is refused. The
    value is not read.
    """

    if exact_job_token_reference(source) == "GITHUB_TOKEN":
        return
    if text_reads_github_token(source):
        raise ExprError("github.token reference is not accepted")
    name = exact_secret_reference(source)
    if name is not None:
        upper = name.upper()
        if upper.startswith("GITHUB_") and upper != "GITHUB_TOKEN":
            raise ExprError("secrets reference is not accepted")
        return
    if _text_mentions_secrets(source):
        raise ExprError("secrets reference is not accepted")
    check_step_text(source)


def _text_mentions_secrets(source):
    try:
        pieces = _text_pieces(source)
    except ExprError:
        return False
    if pieces is None:
        return False
    for kind, text in pieces:
        if kind != "expr":
            continue
        try:
            tree = _parse(text)
        except ExprError:
            return False
        if _mentions(tree, "secrets"):
            return True
    return False


def check_job_env(source):
    """Reject an expression in a job `env` value."""

    check_text(source, JOB_ENV_CONTEXTS, JOB_TEXT_FUNCTIONS)


def check_job_name(source):
    """Reject an expression in a job `name`."""

    check_text(source, JOB_NAME_CONTEXTS, JOB_TEXT_FUNCTIONS)


def check_workflow_env(source):
    """Reject an expression in a workflow `env` value."""

    check_text(source, WORKFLOW_ENV_CONTEXTS, JOB_TEXT_FUNCTIONS)


def check_workflow_concurrency(source):
    """Reject an expression in a workflow `concurrency` group or flag."""

    check_text(source, CONCURRENCY_WORKFLOW_CONTEXTS, JOB_TEXT_FUNCTIONS)


def check_job_concurrency(source):
    """Reject an expression in a job `concurrency` group or flag."""

    check_text(source, CONCURRENCY_JOB_CONTEXTS, JOB_TEXT_FUNCTIONS)


def check_text(source, contexts, functions):
    """Reject every `${{ }}` in `source`. Text without one is left alone."""

    pieces = _text_pieces(source)
    if pieces is None:
        return
    for kind, text in pieces:
        if kind == "expr":
            _check(_parse(text), contexts, functions)


def render_text(source, values):
    """Insert each `${{ }}` into `source`.

    A string with no expression is returned unchanged. One expression
    that is the whole stripped string is stringified without the
    surrounding whitespace. Mixed text keeps the literal characters.
    The inserted value is not scanned for another expression. `null` is
    empty. `true` and `false` are those words. An unclosed or nested
    expression is an error.
    """

    pieces = _text_pieces(source)
    if pieces is None:
        return source
    parts = []
    for kind, text in pieces:
        if kind == "lit":
            parts.append(text)
            continue
        parts.append(_insert_text(evaluate(text, values)))
    return "".join(parts)


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


def _text_pieces(source):
    """Split `source` into literal and expression pieces.

    `None` means the text has no `${{ }}`. One expression that occupies
    the stripped string is a single expression piece, so surrounding
    whitespace is not kept. Nested `${{` outside a string is an error.
    A `}` inside a single-quoted string is not the closer.
    """

    if not isinstance(source, str):
        _reject()
    if "${{" not in source:
        return None
    if "\0" in source:
        _reject()
    stripped = source.strip()
    if stripped.startswith("${{"):
        body, end = _scan_wrapper(stripped, 3)
        if end == len(stripped):
            return (("expr", body),)
    pieces = []
    index = 0
    length = len(source)
    while index < length:
        found = source.find("${{", index)
        if found < 0:
            pieces.append(("lit", source[index:]))
            break
        if found > index:
            pieces.append(("lit", source[index:found]))
        body, end = _scan_wrapper(source, found + 3)
        pieces.append(("expr", body))
        index = end
    return tuple(pieces)


def _scan_wrapper(source, index):
    """Return `(body, index_after)` for the expression that starts at `index`."""

    length = len(source)
    start = index
    in_string = False
    while index < length:
        if in_string:
            if source[index] == "'":
                if index + 1 < length and source[index + 1] == "'":
                    index += 2
                    continue
                in_string = False
            index += 1
            continue
        if source[index] == "'":
            in_string = True
            index += 1
            continue
        if source.startswith("${{", index):
            _reject()
        if source.startswith("}}", index):
            body = source[start:index].strip()
            if body == "":
                _reject()
            return body, index + 2
        index += 1
    _reject()


def _insert_text(value):
    """String form inserted for one expression. `null` is empty."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if "\0" in value:
            return ""
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    return ""


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
    if isinstance(value, int | float):
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
    if isinstance(value, int | float):
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
        if isinstance(parsed, bool) or not isinstance(parsed, int | float):
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


def _shell_accepts_rewrite(shell):
    """Bash and sh can expand ``${RR_SECRET_NAME}``. Other shells cannot."""

    if shell is None or shell == "bash" or shell == "sh":
        return True
    if not isinstance(shell, str) or shell == "" or "\0" in shell:
        return False
    parts = shell.split()
    if len(parts) < 2 or parts.count("{0}") != 1:
        return False
    return parts[0] in {"bash", "sh"}


def check_step_run(source, shell):
    """Reject a step `run` whose secrets cannot be rewritten. Does not keep the text."""

    rewrite_run_secrets(source, shell)


def rewrite_run_secrets(source, shell):
    """Return ``(text, file_names)`` for one step `run`.

    An exact ``secrets.NAME`` in a proven unquoted or double-quoted context
    becomes ``${RR_SECRET_NAME}``. ``file_names`` is the uppercase file names
    in first-seen order. ``GITHUB_TOKEN`` and an exact ``github.token`` are
    rewritten and are not files. A script with no secret or token read is
    returned unchanged, including when the shell cannot expand the variable.
    Any other shell, or a context the lexer cannot prove, raises
    ``run is not accepted`` and does not return text. A ``secrets`` read
    that is not an exact name raises ``secrets reference is not accepted``.
    A ``github.token`` read that is not exact raises
    ``github.token reference is not accepted``.
    """

    if not isinstance(source, str) or "\0" in source:
        _reject()
    sites = _RunLexer(source).scan()
    classified = []
    saw_secret = False
    for start, end, body, context in sites:
        tree = _parse(body)
        name = _exact_secret_tree(tree)
        github_exact = _exact_github_token_tree(tree)
        mentions_token = _tree_reads_github_token(tree)
        if name is not None or github_exact or _walk_mentions_secrets(tree) or mentions_token:
            saw_secret = True
            classified.append((start, end, context, name, github_exact, mentions_token))
        else:
            _check(tree, STEP_TEXT_CONTEXTS, STEP_TEXT_FUNCTIONS)
    if saw_secret and not _shell_accepts_rewrite(shell):
        raise ExprError("run is not accepted")
    names = []
    replacements = []
    for start, end, context, name, github_exact, mentions_token in classified:
        if context not in {"unquoted", "double"}:
            raise ExprError("run is not accepted")
        if github_exact:
            upper = "GITHUB_TOKEN"
        elif mentions_token:
            raise ExprError("github.token reference is not accepted")
        elif name is None:
            raise ExprError("secrets reference is not accepted")
        else:
            upper = name.upper()
            if upper.startswith("GITHUB_") and upper != "GITHUB_TOKEN":
                raise ExprError("secrets reference is not accepted")
        if upper != "GITHUB_TOKEN" and upper not in names:
            names.append(upper)
        replacements.append((start, end, "${RR_SECRET_" + upper + "}"))
    parts = []
    cursor = 0
    for start, end, replacement in replacements:
        if start < cursor:
            raise ExprError("run is not accepted")
        parts.append(source[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(source[cursor:])
    rewritten = "".join(parts)
    if _rewritten_mentions_secrets(rewritten) or text_reads_github_token(rewritten):
        raise ExprError("run is not accepted")
    return rewritten, names


def _walk_mentions_secrets(node):
    """Return whether any node reads the `secrets` context.

    Argument lists are tuples of nodes, including the empty tuple, so this
    walk does not treat every tuple as a node.
    """

    if isinstance(node, list) or (
        isinstance(node, tuple) and (not node or not isinstance(node[0], str))
    ):
        return any(_walk_mentions_secrets(child) for child in node)
    if not isinstance(node, tuple):
        return False
    if node[0] == "name" and len(node) > 1 and node[1] == "secrets":
        return True
    return any(_walk_mentions_secrets(child) for child in node[1:])


def _rewritten_mentions_secrets(text):
    try:
        pieces = _text_pieces(text)
    except ExprError:
        return False
    if pieces is None:
        return False
    for kind, body in pieces:
        if kind != "expr":
            continue
        try:
            tree = _parse(body)
        except ExprError:
            return False
        if _walk_mentions_secrets(tree):
            return True
    return False


class _RunLexer:
    """Classify ``${{ }}`` in a bash or sh script.

    Rewrite is allowed only for unquoted and double-quoted sites. A heredoc
    body, a comment, a quote, an escape, and any context this lexer cannot
    prove are recorded as something else. A pending heredoc belongs to the
    frame that saw ``<<``. Its body starts at the next newline in that same
    frame. A closer that arrives while a heredoc is still pending fails the
    rest of the script.
    """

    def __init__(self, source):
        self.source = source
        self.n = len(source)
        self.i = 0
        self.sites = []

    def scan(self):
        self._read_unquoted(None)
        return self.sites

    def _record(self, context):
        body, end = _scan_wrapper(self.source, self.i + 3)
        self.sites.append((self.i, end, body, context))
        self.i = end

    def _fail(self):
        while self.i < self.n:
            if self.source.startswith("${{", self.i):
                try:
                    body, end = _scan_wrapper(self.source, self.i + 3)
                except ExprError:
                    break
                self.sites.append((self.i, end, body, "unproven"))
                self.i = end
                continue
            self.i += 1
        self.i = self.n

    def _reclassify(self, opened, context):
        for index in range(opened, len(self.sites)):
            start, end, body, _old = self.sites[index]
            self.sites[index] = (start, end, body, context)

    def _read_unquoted(self, closer):
        pending = []
        depth = 0
        word_start = True
        while self.i < self.n:
            char = self.source[self.i]
            if char == "\n":
                self.i += 1
                word_start = True
                if pending and not self._consume_heredocs(pending):
                    self._fail()
                    return
                pending.clear()
                continue
            if char == "\\":
                if self.i + 1 < self.n and self.source[self.i + 1] == "\n":
                    self.i += 2
                    continue
                if self.i + 1 < self.n and self.source.startswith("${{", self.i + 1):
                    self.i += 1
                    self._record("literal")
                    word_start = False
                    continue
                self.i += 2 if self.i + 1 < self.n else 1
                word_start = False
                continue
            if char == "`":
                if closer == "backtick":
                    if pending:
                        self._fail()
                        return
                    self.i += 1
                    return
                self.i += 1
                self._read_unquoted("backtick")
                word_start = False
                continue
            if self.source.startswith("${{", self.i):
                self._record("unquoted")
                word_start = False
                continue
            if self.source.startswith("$'", self.i):
                self.i += 2
                self._read_ansi()
                word_start = False
                continue
            if self.source.startswith('$"', self.i):
                self.i += 2
                self._read_double("unproven")
                word_start = False
                continue
            if self.source.startswith("$((", self.i):
                self._read_arith()
                word_start = False
                continue
            if self.source.startswith("$(", self.i):
                self.i += 2
                self._read_unquoted("paren")
                word_start = False
                continue
            if self.source.startswith("${", self.i):
                self._read_param()
                word_start = False
                continue
            if char == "'":
                self.i += 1
                self._read_single()
                word_start = False
                continue
            if char == '"':
                self.i += 1
                self._read_double("double")
                word_start = False
                continue
            if char == "#" and word_start:
                self._read_comment()
                word_start = True
                continue
            if self.source.startswith("<<<", self.i):
                self.i += 3
                word_start = True
                continue
            if self.source.startswith("<<", self.i):
                queued = self._queue_heredoc()
                if queued is None:
                    self._fail()
                    return
                pending.append(queued)
                word_start = True
                continue
            if char == ")" and closer == "paren" and depth == 0:
                if pending:
                    self._fail()
                    return
                self.i += 1
                return
            if char == "(":
                depth += 1
                self.i += 1
                word_start = True
                continue
            if char == ")":
                if depth > 0:
                    depth -= 1
                self.i += 1
                word_start = True
                continue
            if char in "|&;<>" or char.isspace():
                self.i += 1
                word_start = True
                continue
            self.i += 1
            word_start = False
        if pending:
            self._fail()

    def _read_double(self, context):
        opened = len(self.sites)
        while self.i < self.n:
            char = self.source[self.i]
            if char == "\\":
                if self.i + 1 < self.n and self.source[self.i + 1] == "\n":
                    self.i += 2
                    continue
                if self.i + 1 < self.n and self.source.startswith("${{", self.i + 1):
                    self.i += 1
                    self._record("literal")
                    continue
                self.i += 2 if self.i + 1 < self.n else 1
                continue
            if char == '"':
                self.i += 1
                return
            if self.source.startswith("${{", self.i):
                self._record(context)
                continue
            if char == "`":
                self.i += 1
                self._read_unquoted("backtick")
                continue
            if self.source.startswith("$((", self.i):
                self._read_arith()
                continue
            if self.source.startswith("$(", self.i):
                self.i += 2
                self._read_unquoted("paren")
                continue
            if self.source.startswith("${", self.i):
                self._read_param()
                continue
            self.i += 1
        self._reclassify(opened, "unproven")

    def _read_single(self):
        while self.i < self.n:
            if self.source[self.i] == "'":
                self.i += 1
                return
            if self.source.startswith("${{", self.i):
                self._record("single")
                continue
            self.i += 1

    def _read_ansi(self):
        while self.i < self.n:
            char = self.source[self.i]
            if char == "\\":
                if self.i + 1 < self.n and self.source.startswith("${{", self.i + 1):
                    self.i += 1
                    self._record("ansi")
                    continue
                self.i += 2 if self.i + 1 < self.n else 1
                continue
            if char == "'":
                self.i += 1
                return
            if self.source.startswith("${{", self.i):
                self._record("ansi")
                continue
            self.i += 1

    def _read_comment(self):
        while self.i < self.n and self.source[self.i] != "\n":
            if self.source.startswith("${{", self.i):
                self._record("comment")
                continue
            self.i += 1

    def _read_param(self):
        if self.source.startswith("${{", self.i):
            self._record("unproven")
            return
        self.i += 2
        depth = 1
        while self.i < self.n and depth > 0:
            if self.source.startswith("${{", self.i):
                self._record("unproven")
                continue
            if self.source.startswith("$((", self.i):
                self._read_arith()
                continue
            if self.source.startswith("$(", self.i):
                self.i += 2
                self._read_unquoted("paren")
                continue
            char = self.source[self.i]
            if (
                char in "'\""
                or self.source.startswith("$'", self.i)
                or self.source.startswith('$"', self.i)
            ):
                self._fail()
                return
            if char == "{":
                depth += 1
                self.i += 1
                continue
            if char == "}":
                depth -= 1
                self.i += 1
                continue
            if char == "\\":
                self.i += 2 if self.i + 1 < self.n else 1
                continue
            self.i += 1
        if depth != 0:
            self._fail()

    def _read_arith(self):
        self.i += 3
        depth = 0
        closers = 2
        while self.i < self.n:
            if self.source.startswith("${{", self.i):
                self._record("unproven")
                continue
            if self.source.startswith("$(", self.i):
                self._fail()
                return
            char = self.source[self.i]
            if char in "'\"`":
                self._fail()
                return
            if char == "(":
                depth += 1
                self.i += 1
                continue
            if char == ")":
                if depth > 0:
                    depth -= 1
                    self.i += 1
                    continue
                self.i += 1
                closers -= 1
                if closers == 0:
                    return
                if self.i >= self.n or self.source[self.i] != ")":
                    self._fail()
                    return
                continue
            self.i += 1
        self._fail()

    def _queue_heredoc(self):
        self.i += 2
        strip_tabs = False
        if self.i < self.n and self.source[self.i] == "-":
            # `<<<` is a here-string and is handled before this method.
            strip_tabs = True
            self.i += 1
        while self.i < self.n and self.source[self.i] in " \t":
            self.i += 1
        delimiter = self._heredoc_word()
        if delimiter is None:
            return None
        return (delimiter, strip_tabs)

    def _heredoc_word(self):
        if self.i >= self.n or self.source[self.i] in " \t\n|&;()<>":
            return None
        parts = []
        while self.i < self.n:
            if self.source.startswith("${{", self.i):
                self._record("unproven")
                return None
            char = self.source[self.i]
            if char in " \t\n|&;()<>":
                break
            if char == "\\":
                if self.i + 1 >= self.n:
                    return None
                parts.append(self.source[self.i + 1])
                self.i += 2
                continue
            if char == "'":
                chunk = self._delimiter_quotes("'", "single")
                if chunk is None:
                    return None
                parts.append(chunk)
                continue
            if char == '"':
                chunk = self._delimiter_quotes('"', "double")
                if chunk is None:
                    return None
                parts.append(chunk)
                continue
            if self.source.startswith("$'", self.i):
                chunk = self._delimiter_ansi()
                if chunk is None:
                    return None
                parts.append(chunk)
                continue
            if self.source.startswith('$"', self.i):
                self.i += 1
                chunk = self._delimiter_quotes('"', "unproven")
                if chunk is None:
                    return None
                parts.append(chunk)
                continue
            parts.append(char)
            self.i += 1
        return "".join(parts)

    def _delimiter_quotes(self, quote, context):
        self.i += 1
        chars = []
        while self.i < self.n:
            if self.source.startswith("${{", self.i):
                self._record(context)
                return None
            if self.source[self.i] == "\\" and quote == '"':
                if self.i + 1 >= self.n:
                    return None
                chars.append(self.source[self.i + 1])
                self.i += 2
                continue
            if self.source[self.i] == quote:
                self.i += 1
                return "".join(chars)
            chars.append(self.source[self.i])
            self.i += 1
        return None

    def _delimiter_ansi(self):
        self.i += 2
        chars = []
        while self.i < self.n:
            if self.source.startswith("${{", self.i):
                self._record("ansi")
                return None
            char = self.source[self.i]
            if char == "\\":
                if self.i + 1 >= self.n:
                    return None
                nxt = self.source[self.i + 1]
                if nxt == "n":
                    chars.append("\n")
                elif nxt == "t":
                    chars.append("\t")
                else:
                    chars.append(nxt)
                self.i += 2
                continue
            if char == "'":
                self.i += 1
                return "".join(chars)
            chars.append(char)
            self.i += 1
        return None

    def _consume_heredocs(self, pending):
        for delimiter, strip_tabs in pending:
            if not self._consume_one(delimiter, strip_tabs):
                return False
        return True

    def _consume_one(self, delimiter, strip_tabs):
        body_start = self.i
        while self.i < self.n:
            line_start = self.i
            newline = self.source.find("\n", self.i)
            if newline < 0:
                line = self.source[self.i :]
                line_end = self.n
                has_nl = False
            else:
                line = self.source[self.i : newline]
                line_end = newline
                has_nl = True
            compare = line.lstrip("\t") if strip_tabs else line
            if compare == delimiter:
                after = self._record_between(body_start, line_start, "heredoc")
                self.i = after if after > line_start else line_end + (1 if has_nl else 0)
                return True
            if not has_nl:
                self._record_between(body_start, self.n, "heredoc")
                self.i = self.n
                return False
            self.i = line_end + 1
        return False

    def _record_between(self, start, end, context):
        index = start
        while index < end:
            found = self.source.find("${{", index)
            if found < 0 or found >= end:
                return end
            self.i = found
            self._record(context)
            index = self.i
            if index > end:
                return index
        return end
