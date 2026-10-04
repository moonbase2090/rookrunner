"""One commit-status post for one run.

The credential is read at call time from an operator file. It is not
returned, stored, or placed in an error message. GitHub's create-commit-status
call is one POST and is not retried.

https://docs.github.com/en/rest/commits/statuses
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
"""

import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from .protocol import canonical

# Client pacing for one POST. This is not a GitHub limit.
STATUS_TIMEOUT_SECONDS = 10
DEFAULT_API_BASE = "https://api.github.com"
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_REPOSITORY = re.compile(rf"{_NAME.pattern}/{_NAME.pattern}")
_GITHUB_STATES = frozenset({"pending", "success", "failure", "error"})
TERMINAL_STATUS = frozenset({"success", "failure", "error"})
# Long enough for `rookrunner/<workflow file>/<job>` and still one protocol field.
MAX_CONTEXT_LENGTH = 1024


class StatusError(Exception):
    def __init__(self, kind, message, retryable=False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def github_state(run_state, exit_code):
    """Map a run to one commit-status state. Only succeeded with exit 0 is success."""

    if run_state in {"queued", "running"}:
        return "pending"
    if run_state == "succeeded" and exit_code == 0:
        return "success"
    if run_state == "failed":
        return "failure"
    return "error"


def status_url(api_base, repository, sha):
    parsed = urllib.parse.urlsplit(api_base)
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise StatusError("INVALID_PARAMS", "api base must be an http or https origin")
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise StatusError("INVALID_PARAMS", "repository must be owner/name")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise StatusError("INVALID_PARAMS", "status SHA must be 40 or 64 lowercase hex characters")
    return f"{api_base.rstrip('/')}/repos/{repository}/statuses/{sha}"


def _inside(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def read_credential(path, state_dir, repository):
    """Return the token text. The caller must not store or log it."""

    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file is not readable")
    try:
        resolved = candidate.resolve(strict=True)
        state_root = Path(state_dir).resolve(strict=True)
        repo_root = Path(repository).resolve(strict=True)
    except OSError:
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file is not readable") from None
    if not resolved.is_file() or resolved.is_symlink():
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file is not readable")
    if _inside(resolved, state_root) or _inside(resolved, repo_root):
        raise StatusError(
            "CREDENTIAL_UNREADABLE",
            "credential file must stay outside the repository and the state directory",
        )
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file is not readable") from None
    try:
        chunks = []
        size = 0
        while block := os.read(fd, 4096):
            size += len(block)
            if size > 4096:
                raise StatusError(
                    "CREDENTIAL_UNREADABLE", "credential file must contain one token line"
                )
            chunks.append(block)
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeError:
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file is not readable") from None
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if text == "" or any(character.isspace() for character in text):
        raise StatusError("CREDENTIAL_UNREADABLE", "credential file must contain one token line")
    return text


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StatusError("STATUS_REJECTED", f"GitHub status request failed with HTTP {code}")


def _rate_limited(code, headers):
    if code == 429:
        return True
    if code != 403:
        return False
    remaining = headers.get("x-ratelimit-remaining") if headers is not None else None
    retry_after = headers.get("retry-after") if headers is not None else None
    return remaining == "0" or bool(retry_after)


def post_status(api_base, repository, sha, state, context, token):
    """POST one status. A rate-limit response is returned once and is not retried."""

    if state not in _GITHUB_STATES:
        raise StatusError("INVALID_PARAMS", "status state is not a commit-status state")
    url = status_url(api_base, repository, sha)
    body = canonical({"context": context, "state": state}).encode("ascii")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=STATUS_TIMEOUT_SECONDS) as response:
            code = response.status
            response.read(64)
    except StatusError:
        raise
    except urllib.error.HTTPError as error:
        code = error.code
        headers = error.headers
        try:
            error.read(64)
        except OSError:
            pass
        if _rate_limited(code, headers):
            raise StatusError(
                "RATE_LIMITED", "GitHub rate limit was not retried", retryable=True
            ) from None
        raise StatusError(
            "STATUS_REJECTED", f"GitHub status request failed with HTTP {code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise StatusError("STATUS_REJECTED", "GitHub status request failed") from None
    if code not in {200, 201}:
        raise StatusError("STATUS_REJECTED", f"GitHub status request failed with HTTP {code}")
