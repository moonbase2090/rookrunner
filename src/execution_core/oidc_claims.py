# SPDX-License-Identifier: MIT

"""Job identity token claims and the mint decision.

Both are pure functions of the stored event, the run, and the clock.
Nothing from the workflow file chooses ``iss``, ``sub``, or
``repository``. A claim Rookrunner cannot fill truthfully is omitted.

The mint is allowed only for a trusted run of the worker repository: a
push, schedule, or workflow_dispatch event that ``allowlist_matches``
accepts, or a same-repository pull request when the worker allows pull
requests. A fork, a null head repository, a run with an untrusted
override, and an event without the stored repository fields are refused
here, not only by the step environment.

https://docs.github.com/en/actions/reference/security/oidc
"""

import base64
import re
import secrets

from .oidc_keys import require_issuer
from .poll import _integer_id, _stored_login, allowlist_matches

TOKEN_LIFETIME_SECONDS = 300
MAX_AUDIENCE = 256
RUNNER_ENVIRONMENT = "self-hosted"
_EVENTS = frozenset({"push", "pull_request", "schedule", "workflow_dispatch"})
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")


class OidcRefusal(Exception):
    """The mint was refused. The message is public and names no secret."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def require_audience(audience):
    """One printable ASCII string of 1 to 256 characters, without spaces."""

    if (
        not isinstance(audience, str)
        or not 0 < len(audience) <= MAX_AUDIENCE
        or not all("!" <= character <= "~" for character in audience)
    ):
        raise OidcRefusal("audience must be 1 to 256 printable ASCII characters")
    return audience


def _repository_id(event):
    repository = event.get("repository") if isinstance(event, dict) else None
    if not isinstance(repository, dict):
        return None
    return _integer_id(repository.get("id"))


def _pull_repo_id(pull, side):
    part = pull.get(side) if isinstance(pull, dict) else None
    repo = part.get("repo") if isinstance(part, dict) else None
    if not isinstance(repo, dict):
        return None
    return _integer_id(repo.get("id"))


def mint_refusal(
    event,
    event_name,
    *,
    repository,
    refs=(),
    pushers=(),
    pull_requests=False,
    untrusted_override=False,
):
    """Return why this run may not receive a token, or None when it may.

    ``repository`` is the worker's ``owner/name``. ``refs`` and ``pushers``
    are the file-secret allowlist. ``pull_requests`` is the worker's
    ``--oidc-pull-request``.
    """

    if untrusted_override is not False:
        return "run was started by an untrusted override"
    if event_name not in _EVENTS:
        return "event is not push, pull_request, schedule, or workflow_dispatch"
    if not isinstance(event, dict):
        return "run has no stored event"
    stored = event.get("repository")
    if (
        not isinstance(repository, str)
        or not isinstance(stored, dict)
        or stored.get("full_name") != repository
    ):
        return "event repository is not the worker repository"
    if not isinstance(event.get("ref"), str) or event["ref"] == "":
        return "event has no ref"
    if event_name != "workflow_dispatch" and _repository_id(event) is None:
        return "event has no stored repository id"
    if event_name == "pull_request":
        pull = event.get("pull_request")
        head = _pull_repo_id(pull, "head")
        base = _pull_repo_id(pull, "base")
        if head is None:
            return "pull request head repository is missing"
        if base is None or head != base or head != _repository_id(event):
            return "pull request is from a fork"
        if pull_requests is not True:
            return "pull requests do not receive tokens on this worker"
        return None
    if not allowlist_matches(event, list(refs), list(pushers), event_name):
        return "ref and actor are not on the allowlist"
    return None


def subject(repository, event, event_name):
    """GitHub's default ``sub`` template for the events Rookrunner mints for."""

    if event_name == "pull_request":
        return f"repo:{repository}:pull_request"
    return f"repo:{repository}:ref:{event['ref']}"


def new_jti():
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def build_claims(
    *,
    issuer,
    audience,
    event,
    event_name,
    repository,
    sha,
    run_id,
    run_attempt,
    workflow,
    workflow_path,
    issued_at,
    job_workflow_path=None,
    jti=None,
):
    """Claims for one token. The caller has already passed ``mint_refusal``.

    ``workflow`` is ``github.workflow``: the workflow name, or its path when
    unnamed. ``workflow_path`` is ``.github/workflows/FILE``.
    ``job_workflow_path`` is the called local reusable workflow, if any.
    """

    issuer = require_issuer(issuer)
    audience = require_audience(audience)
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise OidcRefusal("repository must be owner/name")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise OidcRefusal("commit is not a full SHA")
    for path in (workflow_path, job_workflow_path or workflow_path):
        if not isinstance(path, str) or _WORKFLOW_PATH.fullmatch(path) is None:
            raise OidcRefusal("workflow path is not under .github/workflows")
    if not isinstance(workflow, str) or workflow == "":
        raise OidcRefusal("workflow name is empty")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise OidcRefusal("mint time is not an integer")
    ref = event["ref"]
    owner = repository.split("/", 1)[0]
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject(repository, event, event_name),
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + TOKEN_LIFETIME_SECONDS,
        "jti": new_jti() if jti is None else jti,
        "repository": repository,
        "repository_owner": owner,
        "ref": ref,
        "ref_type": "tag" if ref.startswith("refs/tags/") else "branch",
        "sha": sha,
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "workflow": workflow,
        "workflow_ref": f"{repository}/{workflow_path}@{ref}",
        "job_workflow_ref": f"{repository}/{job_workflow_path or workflow_path}@{ref}",
        "event_name": event_name,
        "runner_environment": RUNNER_ENVIRONMENT,
    }
    repository_id = _repository_id(event)
    if repository_id is not None:
        claims["repository_id"] = str(repository_id)
    actor = _stored_login(event, event_name)
    if actor is not None:
        claims["actor"] = actor
    return claims


def mint(key_set, claims):
    """Sign ``claims`` with the active key."""

    return key_set.sign_jwt(claims)
