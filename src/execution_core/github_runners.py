# SPDX-License-Identifier: MIT
"""Prototype: GitHub runner registration requests and webhook intake.

Status: prototype, off by default, not accepted direction. See
``docs/design/github-runner-prototype.md``. Nothing else imports this module.

Every entry point refuses unless ``ROOKRUNNER_EXPERIMENTAL_GITHUB_RUNNERS=1``
is set (or the caller passes an ``environ`` mapping that sets it).

The registration client asks GitHub for a JIT runner configuration or a
registration token. Those values are secrets: they are wrapped in ``Secret``,
never logged, never placed in an error message, and never written to disk.

The webhook handler is a pure function over headers, raw body, secret, and a
durable ``WebhookStore``. A ``workflow_job`` notification is a hint about
demand. It never produces a local ``succeeded`` result.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

FLAG_ENV = "ROOKRUNNER_EXPERIMENTAL_GITHUB_RUNNERS"
DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
ACTIONS = frozenset({"queued", "in_progress", "completed"})
_ACTION_RANK = {"queued": 0, "in_progress": 1, "completed": 2}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_WORK_FOLDER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
_DELIVERY_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")
MAX_LABELS = 10


class FeatureDisabled(RuntimeError):
    """The experimental flag is off."""


class ValidationError(ValueError):
    """A caller-supplied value was refused before any request was made."""


class GitHubRunnerError(RuntimeError):
    """A request failed. The message never includes a token or a response body."""


def enabled(environ=None):
    env = os.environ if environ is None else environ
    return env.get(FLAG_ENV) == "1"


def require_enabled(environ=None):
    if not enabled(environ):
        raise FeatureDisabled(
            f"GitHub runner registration is an experimental prototype and is off; "
            f"set {FLAG_ENV}=1 to enable it"
        )


class Secret:
    """Holds a credential. ``repr`` and ``str`` never show it."""

    __slots__ = ("_value",)

    def __init__(self, value):
        if isinstance(value, Secret):
            value = value._value
        if not isinstance(value, str | bytes) or not value:
            raise ValidationError("a secret must be a non-empty string")
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a Secret cannot be serialized")

    def __eq__(self, other):
        return isinstance(other, Secret) and hmac.compare_digest(
            _as_bytes(self._value), _as_bytes(other._value)
        )

    __hash__ = None


def _as_bytes(value):
    return value if isinstance(value, bytes) else value.encode("utf-8")


# ---------------------------------------------------------------- registration


@dataclass(frozen=True)
class Scope:
    """A repository (``owner``, ``repo``) or organization (``owner`` only) scope."""

    owner: str
    repo: str | None = None

    def __post_init__(self):
        if not isinstance(self.owner, str) or not _OWNER.fullmatch(self.owner):
            raise ValidationError("invalid owner")
        if self.owner.endswith("."):
            raise ValidationError("invalid owner")
        if self.repo is not None and (
            not isinstance(self.repo, str)
            or not _OWNER.fullmatch(self.repo)
            or self.repo.endswith(".")
        ):
            raise ValidationError("invalid repository name")

    @property
    def path(self):
        owner = urllib.parse.quote(self.owner, safe="")
        if self.repo is None:
            return f"/orgs/{owner}/actions/runners"
        return f"/repos/{owner}/{urllib.parse.quote(self.repo, safe='')}/actions/runners"


@dataclass(frozen=True)
class JitConfig:
    runner_id: int
    runner_name: str
    encoded_jit_config: Secret = field(repr=False)


@dataclass(frozen=True)
class RegistrationToken:
    token: Secret = field(repr=False)
    expires_at: str


def validate_runner_name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValidationError("runner name must be 1-64 characters of letters, digits, . _ -")
    return name


def validate_labels(labels):
    if isinstance(labels, str) or not isinstance(labels, list | tuple):
        raise ValidationError("labels must be a list of strings")
    if not 1 <= len(labels) <= MAX_LABELS:
        raise ValidationError(f"labels must hold 1 to {MAX_LABELS} entries")
    for label in labels:
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise ValidationError("a label must be 1-64 characters of letters, digits, . _ : -")
    if len(set(labels)) != len(labels):
        raise ValidationError("labels must be unique")
    return list(labels)


def validate_runner_group_id(group_id):
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 1:
        raise ValidationError("runner_group_id must be a positive integer")
    return group_id


def validate_work_folder(folder):
    if not isinstance(folder, str) or not _WORK_FOLDER.fullmatch(folder) or folder in {".", ".."}:
        raise ValidationError("work folder must be a simple relative name")
    return folder


def _checked_base_url(base_url):
    parts = urllib.parse.urlsplit(base_url if isinstance(base_url, str) else "")
    if parts.query or parts.fragment or parts.username or parts.password or not parts.hostname:
        raise ValidationError("invalid API base URL")
    if not (parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(parts.hostname))):
        raise ValidationError("API base URL must be https (http only for loopback)")
    return base_url.rstrip("/")


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would forward the bearer token to another host. Refuse it."""

    def redirect_request(self, *args, **kwargs):
        return None


class RunnerClient:
    """Requests JIT configs and registration tokens. Persists and logs nothing."""

    def __init__(
        self,
        base_url,
        token_provider: Callable[[], str],
        *,
        environ=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    ):
        require_enabled(environ)
        self._base = _checked_base_url(base_url)
        if not callable(token_provider):
            raise ValidationError("token_provider must be callable")
        self._token_provider = token_provider
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self):
        return f"RunnerClient(base_url={self._base!r})"

    def generate_jit_config(self, scope, *, name, runner_group_id, labels, work_folder=None):
        body = {
            "name": validate_runner_name(name),
            "runner_group_id": validate_runner_group_id(runner_group_id),
            "labels": validate_labels(labels),
        }
        if work_folder is not None:
            body["work_folder"] = validate_work_folder(work_folder)
        data = self._post(scope.path + "/generate-jitconfig", body)
        runner = data.get("runner")
        encoded = data.get("encoded_jit_config")
        if (
            not isinstance(runner, dict)
            or not isinstance(runner.get("id"), int)
            or not isinstance(runner.get("name"), str)
            or not isinstance(encoded, str)
            or not encoded
        ):
            raise GitHubRunnerError("unexpected generate-jitconfig response shape")
        return JitConfig(runner["id"], runner["name"], Secret(encoded))

    def create_registration_token(self, scope):
        data = self._post(scope.path + "/registration-token", None)
        token = data.get("token")
        expires = data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubRunnerError("unexpected registration-token response shape")
        return RegistrationToken(Secret(token), expires)

    def _post(self, path, body):
        credential = self._token_provider()
        if not isinstance(credential, str | Secret) or not credential:
            raise GitHubRunnerError("token provider returned no credential")
        credential = Secret(credential).reveal()
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        request = urllib.request.Request(
            self._base + path,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {credential}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "rookrunner-prototype",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            # Status only: the body and headers are never copied into the message.
            raise GitHubRunnerError(f"POST {path} failed with HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise GitHubRunnerError(f"POST {path} failed: network error") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GitHubRunnerError("response too large")
        try:
            data = json.loads(raw)
        except ValueError:
            raise GitHubRunnerError("response was not JSON") from None
        if not isinstance(data, dict):
            raise GitHubRunnerError("response was not a JSON object")
        return data


# --------------------------------------------------------------------- webhook


@dataclass(frozen=True)
class Allowlist:
    """A delivery must match an allowed installation and an allowed repository.

    Empty sets deny everything. Repository names compare case-insensitively.
    """

    installation_ids: frozenset = frozenset()
    repositories: frozenset = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "installation_ids", frozenset(self.installation_ids))
        object.__setattr__(self, "repositories", frozenset(r.lower() for r in self.repositories))

    def allows(self, installation_id, full_name):
        return (
            installation_id in self.installation_ids
            and isinstance(full_name, str)
            and full_name.lower() in self.repositories
        )


@dataclass(frozen=True)
class WebhookResult:
    """``status`` is the HTTP status a server wrapper should answer with."""

    status: int
    outcome: str
    reason: str = ""
    delivery_id: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL,
    event TEXT NOT NULL,
    action TEXT NOT NULL,
    job_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY,
    installation_id INTEGER NOT NULL,
    repository TEXT NOT NULL,
    run_id INTEGER,
    labels TEXT NOT NULL,
    reported_action TEXT NOT NULL,
    reported_conclusion TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS demand (
    job_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('pending', 'cleared')),
    created_at REAL NOT NULL
);
"""


class WebhookStore:
    """Durable sqlite state under a caller-given directory.

    ``deliveries`` records delivery identity. ``jobs`` records the latest GitHub
    report per job, including any conclusion, as a hint. ``demand`` is the
    queue a controller would reconcile; it has no success state.
    """

    def __init__(self, state_dir, *, environ=None):
        require_enabled(environ)
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "github-webhooks.sqlite3"
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        self._conn.executescript(_SCHEMA)
        os.chmod(self.path, 0o600)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def record(self, delivery_id, event, action, job):
        """Atomically record one delivery. Return True if it was new."""

        now = time.time()
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, ?, ?)",
                (delivery_id, now, event, action, job["id"]),
            )
            if cursor.rowcount == 0:
                conn.execute("COMMIT")
                return False
            previous = conn.execute(
                "SELECT reported_action FROM jobs WHERE job_id = ?", (job["id"],)
            ).fetchone()
            # Events can arrive late or out of order. A report never moves a job
            # backwards (queued < in_progress < completed), so a late in_progress
            # cannot erase a completed conclusion, and a late queued is not demand.
            stale = previous is not None and _ACTION_RANK[action] < _ACTION_RANK[previous[0]]
            if not stale:
                conn.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET reported_action = excluded.reported_action, "
                    "reported_conclusion = excluded.reported_conclusion, "
                    "updated_at = excluded.updated_at",
                    (
                        job["id"],
                        job["installation_id"],
                        job["repository"],
                        job["run_id"],
                        json.dumps(job["labels"]),
                        action,
                        job["conclusion"] if action == "completed" else None,
                        now,
                    ),
                )
            if action == "queued" and not previous:
                conn.execute(
                    "INSERT OR IGNORE INTO demand VALUES (?, 'pending', ?)", (job["id"], now)
                )
            elif action != "queued":
                conn.execute("UPDATE demand SET state = 'cleared' WHERE job_id = ?", (job["id"],))
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def pending_demand(self):
        rows = self._conn.execute(
            "SELECT j.job_id, j.installation_id, j.repository, j.labels FROM demand d "
            "JOIN jobs j USING (job_id) WHERE d.state = 'pending' ORDER BY d.created_at, j.job_id"
        ).fetchall()
        return [
            {
                "job_id": r[0],
                "installation_id": r[1],
                "repository": r[2],
                "labels": json.loads(r[3]),
            }
            for r in rows
        ]

    def job(self, job_id):
        row = self._conn.execute(
            "SELECT reported_action, reported_conclusion FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else {"reported_action": row[0], "reported_conclusion": row[1]}

    def delivery_count(self):
        return self._conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]


def verify_signature(secret, body, header):
    """Check ``X-Hub-Signature-256`` over the raw body in constant time."""

    if not isinstance(header, str):
        return False
    match = _SIGNATURE.fullmatch(header)
    if not match:
        return False
    key = _as_bytes(Secret(secret).reveal())
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), match.group(1).encode("ascii"))


def handle_delivery(
    headers,
    body,
    secret,
    store,
    allowlist,
    *,
    max_body_bytes=DEFAULT_MAX_BODY_BYTES,
    environ=None,
):
    """Authenticate, filter, dedupe, and durably record one webhook delivery.

    No socket is involved. The body is untrusted until the signature matches,
    and it is not parsed before then.
    """

    require_enabled(environ)
    if not isinstance(body, bytes):
        raise TypeError("body must be the raw request bytes")
    lowered = {str(k).lower(): v for k, v in headers.items()}
    if len(body) > max_body_bytes:
        return WebhookResult(413, "rejected", "body too large")
    if not verify_signature(secret, body, lowered.get("x-hub-signature-256")):
        return WebhookResult(401, "rejected", "signature missing or invalid")
    delivery_id = lowered.get("x-github-delivery")
    if not isinstance(delivery_id, str) or not _DELIVERY_ID.fullmatch(delivery_id):
        return WebhookResult(400, "rejected", "missing or malformed delivery id")
    event = lowered.get("x-github-event")
    if event != "workflow_job":
        return WebhookResult(200, "ignored", "event not handled", delivery_id)
    try:
        payload = json.loads(body)
    except ValueError:
        return WebhookResult(400, "rejected", "body is not JSON", delivery_id)
    if not isinstance(payload, dict):
        return WebhookResult(400, "rejected", "payload is not an object", delivery_id)
    action = payload.get("action")
    if action not in ACTIONS:
        return WebhookResult(200, "ignored", "action not handled", delivery_id)
    job = _extract_job(payload)
    if job is None:
        return WebhookResult(400, "rejected", "payload is missing job fields", delivery_id)
    if not allowlist.allows(job["installation_id"], job["repository"]):
        return WebhookResult(403, "rejected", "installation or repository not allowed", delivery_id)
    if not store.record(delivery_id, event, action, job):
        return WebhookResult(200, "duplicate", "delivery already recorded", delivery_id)
    return WebhookResult(202, "accepted", "", delivery_id)


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _extract_job(payload):
    workflow_job = payload.get("workflow_job")
    repository = payload.get("repository")
    installation = payload.get("installation")
    if not all(isinstance(x, dict) for x in (workflow_job, repository, installation)):
        return None
    labels = workflow_job.get("labels")
    full_name = repository.get("full_name")
    if (
        not _is_id(workflow_job.get("id"))
        or not _is_id(installation.get("id"))
        or not isinstance(full_name, str)
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
    ):
        return None
    conclusion = workflow_job.get("conclusion")
    return {
        "id": workflow_job["id"],
        "installation_id": installation["id"],
        "repository": full_name.lower(),
        "run_id": workflow_job.get("run_id") if _is_id(workflow_job.get("run_id")) else None,
        "labels": labels,
        "conclusion": conclusion if isinstance(conclusion, str) else None,
    }


__all__ = [
    "FLAG_ENV",
    "Allowlist",
    "FeatureDisabled",
    "GitHubRunnerError",
    "JitConfig",
    "RegistrationToken",
    "RunnerClient",
    "Scope",
    "Secret",
    "ValidationError",
    "WebhookResult",
    "WebhookStore",
    "enabled",
    "handle_delivery",
    "require_enabled",
    "verify_signature",
]
