# SPDX-License-Identifier: MPL-2.0

"""One Contents-read installation token for one job.

This is not the check-run post token. The mint body always names the
repository and ``permissions: {"contents": "read"}``. Leaving either
field off would grant every App permission on every installed
repository. The token stays in memory. The revocation record stores
the attempt and never the token.
"""

from .checks import (
    load_app_key,
    post_installation_token,
    revoke_installation_token,
    sign_app_jwt,
)
from .expr import exact_job_token_reference, text_needs_job_token
from .status import StatusError, require_repository

TOKEN_EXPIRY_WARNING = "job token expires one hour after mint"
TOKEN_WARNING_AFTER_SECONDS = 55 * 60
_JOB_PERMISSIONS = {"contents": "read"}


class JobTokenConfig:
    """Where to mint. This object does not hold a token or a key."""

    def __init__(self, app_key, api_base, *, state_dir, repository_root, github_repository):
        self.app_key = app_key
        self.api_base = api_base
        self.state_dir = state_dir
        self.repository_root = repository_root
        self.github_repository = github_repository


class TokenExpiry:
    """One warning, the first time a held token reaches 55 minutes."""

    def __init__(self):
        self.warned = False

    def observe(self, minted_at, now):
        if self.warned:
            return None
        if isinstance(minted_at, bool) or isinstance(now, bool):
            return None
        if not isinstance(minted_at, int | float) or not isinstance(now, int | float):
            return None
        if now - minted_at >= TOKEN_WARNING_AFTER_SECONDS:
            self.warned = True
            return TOKEN_EXPIRY_WARNING
        return None


def contents_read_granted(permissions):
    """Whether resolved permissions allow the Contents-read mint.

    An omitted key and ``read-all`` grant it. An empty mapping does not.
    ``contents: write`` does not: write is rejected at plan time, and a
    stored write is not a reason to mint.
    """

    if permissions is None or permissions == "read-all":
        return True
    return bool(isinstance(permissions, dict) and permissions.get("contents") == "read")


def resolved_permissions(job, workflow):
    """Job permissions replace workflow permissions when the job key is present."""

    if isinstance(job, dict) and "permissions" in job:
        return job.get("permissions")
    if isinstance(workflow, dict) and "permissions" in workflow:
        return workflow.get("permissions")
    return None


def job_needs_token(job):
    """Whether any step asks via env, with, run, or an omitted action default.

    Step ``if`` and step ``name`` are not reasons to mint. ``github.token``
    stays off the github context so those conditions still read empty.
    """

    def walk(step):
        if not isinstance(step, dict):
            return False
        env = step.get("env")
        if isinstance(env, dict):
            for value in env.values():
                if text_needs_job_token(value):
                    return True
        raw_with = step.get("with")
        provided = set(raw_with) if isinstance(raw_with, dict) else set()
        if isinstance(raw_with, dict):
            for value in raw_with.values():
                if text_needs_job_token(value):
                    return True
        if text_needs_job_token(step.get("run")):
            return True
        spec = step.get("inputs")
        if isinstance(spec, dict):
            for key, item in spec.items():
                if key in provided or not isinstance(item, dict):
                    continue
                if exact_job_token_reference(item.get("default")) == "GITHUB_TOKEN":
                    return True
        return any(walk(inner) for inner in step.get("steps") or [])

    if not isinstance(job, dict):
        return False
    return any(walk(step) for step in job.get("steps") or [])


def mint_job_token(config, clock=None):
    """Mint one token. A bad repository is refused before the key is read."""

    from datetime import UTC, datetime

    if clock is None:
        clock = datetime.now(UTC)
    repository = require_repository(getattr(config, "github_repository", None))
    material = load_app_key(config.app_key, config.state_dir, config.repository_root)
    jwt = None
    try:
        try:
            jwt = sign_app_jwt(bytes(material.pem), material.client_id, clock)
        except ValueError:
            raise StatusError("APP_KEY_UNREADABLE", "app key could not be loaded") from None
        material.pem[:] = b"\x00" * len(material.pem)
        material.pem = None
        payload = {"repositories": [repository], "permissions": dict(_JOB_PERMISSIONS)}
        return post_installation_token(config.api_base, material.installation_id, jwt, payload)
    finally:
        jwt = None
        material.clear()


def finish_job_token(runtime, job_id, revocations, config):
    """Revoke a held token. A revoke failure does not raise."""

    if not isinstance(job_id, str) or job_id == "":
        job_id = "job"
    token = getattr(runtime, "job_token", None)
    if not isinstance(token, str) or token == "":
        revocations.append({"job_id": job_id, "attempted": False})
        return
    runtime.job_token = None
    accepted = False
    try:
        api_base = None if config is None else getattr(config, "api_base", None)
        accepted = revoke_installation_token(api_base, token)
    except Exception:
        accepted = False
    finally:
        token = None
    revocations.append({"job_id": job_id, "attempted": True, "accepted": bool(accepted)})


def public_token_revocation(records):
    """Copy revocation records. Drop every key except the three stored fields."""

    if not isinstance(records, list):
        return []
    stored = []
    for item in records:
        if not isinstance(item, dict):
            continue
        job_id = item.get("job_id")
        attempted = item.get("attempted")
        if (
            not isinstance(job_id, str)
            or job_id == ""
            or len(job_id) > 128
            or any(0xD800 <= ord(char) <= 0xDFFF for char in job_id)
        ):
            continue
        if attempted is not True and attempted is not False:
            continue
        entry = {"job_id": job_id, "attempted": attempted}
        if attempted:
            entry["accepted"] = item.get("accepted") is True
        stored.append(entry)
    return stored
