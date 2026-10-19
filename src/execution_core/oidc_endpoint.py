# SPDX-License-Identifier: MIT

"""The per-job token request endpoint and the OIDC audit log.

A job whose resolved permissions grant ``id-token: write``, on a worker
started with ``--oidc-issuer``, and whose run passes ``mint_refusal``
gets one HTTP listener. It is bound to one host address on an ephemeral
port, and the container reaches it as ``host.docker.internal``. Every
step of that job receives the two names ``@actions/core`` reads:

- ``ACTIONS_ID_TOKEN_REQUEST_URL``: ``http://host.docker.internal:<port>/token?job=<id>``
- ``ACTIONS_ID_TOKEN_REQUEST_TOKEN``: 256 random bits, unique to the job

A request is ``GET <url>&audience=<aud>`` with ``Authorization: Bearer
<request token>``. The answer is ``{"value": "<jwt>"}``. A wrong bearer
token, an unknown or finished job, and a missing or invalid audience
are refused with no token in the body. A job gets at most 60 requests.
Redirects are not followed and nothing is proxied.

The request token and every minted JWT are registered on the job mask
before either can reach a log line. Each mint and each refusal appends
one JSON line to ``oidc-audit.jsonl`` in the state directory. That line
never holds the JWT or the request token. When the mint line cannot be
appended, the mint fails closed: the endpoint answers 500 and no token
is issued.

https://docs.github.com/en/actions/reference/security/oidc
"""

import base64
from datetime import datetime, UTC
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse

from .commands import register_mask
from .oidc_claims import OidcRefusal, build_claims, mint, mint_refusal
from .oidc_keys import OidcKeySet, require_issuer

REQUEST_URL_ENV = "ACTIONS_ID_TOKEN_REQUEST_URL"
REQUEST_TOKEN_ENV = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
CONTRACT_NAMES = frozenset({REQUEST_URL_ENV, REQUEST_TOKEN_ENV})
CONTAINER_HOST = "host.docker.internal"
MAX_REQUESTS = 60
AUDIT_FILE = "oidc-audit.jsonl"
_MAX_REQUEST_LINE = 4096
NETWORK_MESSAGE = (
    "job requests id-token: write, but the worker network is none; "
    "OIDC tokens need worker --network bridge"
)


def _random(size):
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).rstrip(b"=").decode("ascii")


def id_token_write_granted(permissions):
    """Only an explicit ``id-token: write`` grants it, as on GitHub.

    An omitted key, ``read-all``, and ``permissions: {}`` do not.
    """

    return isinstance(permissions, dict) and permissions.get("id-token") == "write"


class AuditLog:
    """Append-only JSON lines, mode 0600. A write failure does not raise."""

    def __init__(self, path):
        self.path = path
        self._guard = threading.Lock()

    def append(self, entry):
        line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with self._guard:
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError:
                return False
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    return False
                os.write(fd, line)
                return True
            except OSError:
                return False
            finally:
                os.close(fd)


class OidcSettings:
    """Worker-level OIDC settings. Built once at worker start."""

    def __init__(
        self,
        issuer,
        key_set,
        *,
        repository,
        state_dir,
        refs=(),
        pushers=(),
        pull_requests=False,
    ):
        if not isinstance(key_set, OidcKeySet):
            raise ValueError("oidc key set is not accepted")
        if type(pull_requests) is not bool:
            raise ValueError("oidc pull request flag must be a boolean")
        self.issuer = require_issuer(issuer)
        self.key_set = key_set
        self.repository = repository
        self.refs = tuple(refs or ())
        self.pushers = tuple(pushers or ())
        self.pull_requests = pull_requests
        self.audit = AuditLog(os.path.join(os.fspath(state_dir), AUDIT_FILE))


class OidcRun:
    """One run's identity inputs. Nothing here comes from the workflow file."""

    def __init__(self, settings, *, run_id, attempt, untrusted_override=False):
        if not isinstance(settings, OidcSettings):
            raise ValueError("oidc settings are not accepted")
        self.settings = settings
        self.run_id = run_id
        self.attempt = attempt
        self.untrusted_override = untrusted_override is not False

    def _audit(self, job_id, **fields):
        entry = {
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": self.run_id,
            "attempt": str(self.attempt),
            "job_id": job_id,
        }
        entry.update({key: value for key, value in fields.items() if value is not None})
        return self.settings.audit.append(entry)

    def refusal(self, event, event_name, job_id):
        """Return the mint refusal for this run, and audit it. None means allowed."""

        settings = self.settings
        reason = mint_refusal(
            event,
            event_name,
            repository=settings.repository,
            refs=settings.refs,
            pushers=settings.pushers,
            pull_requests=settings.pull_requests,
            untrusted_override=self.untrusted_override,
        )
        if reason is not None:
            self._audit(job_id, reason=reason)
        return reason


class JobListener:
    """One job's token endpoint. ``close`` ends it on every job end path."""

    def __init__(
        self,
        run,
        *,
        job_id,
        event,
        event_name,
        sha,
        workflow,
        workflow_path,
        masks,
        address,
        job_workflow_path=None,
        clock=time.time,
    ):
        self.run = run
        self.job_id = job_id
        self.masks = masks
        self.notes = []
        self.step_index = None
        self.requests = 0
        self.closed = False
        self._guard = threading.Lock()
        self._clock = clock
        self._claims = {
            "issuer": run.settings.issuer,
            "event": event,
            "event_name": event_name,
            "repository": run.settings.repository,
            "sha": sha,
            "run_id": run.run_id,
            "run_attempt": run.attempt,
            "workflow": workflow,
            "workflow_path": workflow_path,
            "job_workflow_path": job_workflow_path,
        }
        # Fail before any listener exists when a claim cannot be built.
        build_claims(audience="probe", issued_at=0, jti="probe", **self._claims)
        self.opaque = _random(16)
        self.request_token = _random(32)
        register_mask(masks, self.request_token, secret=True, name=REQUEST_TOKEN_ENV)
        self.server = ThreadingHTTPServer((address, 0), _handler(self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def env(self):
        return {
            REQUEST_URL_ENV: f"http://{CONTAINER_HOST}:{self.port}/token?job={self.opaque}",
            REQUEST_TOKEN_ENV: self.request_token,
        }

    def drain_notes(self):
        with self._guard:
            notes, self.notes = self.notes, []
        return notes

    def close(self):
        with self._guard:
            if self.closed:
                return
            self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def answer(self, path, authorization):
        """Return ``(status, body)`` for one request. The body is a dict."""

        parts = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        jobs = query.get("job", [])
        audiences = query.get("audience", [])
        step = self.step_index
        with self._guard:
            known = (
                not self.closed
                and parts.path == "/token"
                and len(jobs) == 1
                and hmac.compare_digest(jobs[0].encode("utf-8"), self.opaque.encode("ascii"))
            )
            if not known:
                reason = "job is unknown or finished"
            elif not isinstance(authorization, str) or not hmac.compare_digest(
                authorization.encode("utf-8"), f"Bearer {self.request_token}".encode("ascii")
            ):
                reason = "request token is not accepted"
            elif self.requests >= MAX_REQUESTS:
                reason = f"job reached {MAX_REQUESTS} token requests"
            else:
                reason = None
                self.requests += 1
        if reason is not None:
            self.run._audit(self.job_id, step_index=step, reason=reason)
            return (429 if reason.startswith("job reached") else 401), {"message": reason}
        if len(audiences) != 1:
            reason = "audience is required once"
            self.run._audit(self.job_id, step_index=step, reason=reason)
            return 400, {"message": reason}
        try:
            claims = build_claims(
                audience=audiences[0], issued_at=int(self._clock()), **self._claims
            )
        except OidcRefusal as exc:
            self.run._audit(self.job_id, step_index=step, reason=exc.reason)
            return 400, {"message": exc.reason}
        audited = self.run._audit(
            self.job_id,
            step_index=step,
            aud=claims["aud"],
            sub=claims["sub"],
            jti=claims["jti"],
            kid=self.run.settings.key_set.active.kid,
            exp=claims["exp"],
        )
        if not audited:
            return 500, {"message": "audit log is not writable"}
        token = mint(self.run.settings.key_set, claims)
        # Masked before the step can receive it, so no log line can carry it.
        register_mask(self.masks, token, secret=True, name="OIDC token")
        with self._guard:
            self.notes.append(f"oidc token minted for aud {claims['aud']} sub {claims['sub']}")
        return 200, {"value": token}


def _handler(listener):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rookrunner-oidc"
        sys_version = ""

        def log_message(self, format, *args):
            return

        def _send(self, code, body):
            raw = json.dumps(body, separators=(",", ":")).encode("ascii")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if len(self.path) > _MAX_REQUEST_LINE:
                self._send(400, {"message": "request is too long"})
                return
            code, body = listener.answer(self.path, self.headers.get("Authorization"))
            self._send(code, body)

        def _refuse_method(self):
            self._send(405, {"message": "method is not allowed"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _refuse_method

    return Handler
