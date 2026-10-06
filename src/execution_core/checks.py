"""One check run and one commit status for one run.

The operator passes the App private-key path. This module reads that
key only when a post is about to be sent. It signs one JWT, exchanges
it for an installation token, posts the check run, then posts the
commit status with the same token. The token is not cached and is
discarded before the call returns. Nothing here writes the key, the
JWT, or the token to disk.

https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
https://docs.github.com/en/rest/checks/runs
https://docs.github.com/en/rest/commits/statuses
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .commands import mask_prefixes
from .protocol import canonical, is_integer, strict_json
from .status import (
    STATUS_TIMEOUT_SECONDS,
    StatusError,
    _rate_limited,
    api_origin,
    post_status,
    require_repository,
    require_sha,
)

# A GitHub App private key is a PEM of a few kilobytes. A larger file is refused.
_MAX_PEM = 65536
_MAX_ID_FILE = 256
_MAX_RESPONSE = 65536
_CLIENT_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})")
_INSTALLATION_ID = re.compile(r"[1-9][0-9]{0,18}")
_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_OID = bytes.fromhex("2a864886f70d010101")
_KEY_MESSAGE = "app key is not readable"
_INSTALL_MESSAGE = "app installation is not readable"
_PEM_MESSAGE = "app key could not be loaded"


class AppPost:
    """What one attempt managed to send. The token is not a field."""

    def __init__(self):
        self.check_id = None
        self.status_posted = False
        self.status_remaining = None
        self.error = None


class _AppKey:
    def __init__(self, pem, client_id, installation_id):
        self.pem = bytearray(pem)
        self.client_id = client_id
        self.installation_id = installation_id

    def clear(self):
        if self.pem is not None:
            self.pem[:] = b"\x00" * len(self.pem)
        self.pem = None
        self.client_id = None
        self.installation_id = None


def check_mapping(run_state, exit_code):
    """Map a run to a check status and conclusion. Only exit 0 is success."""

    if run_state == "queued":
        return "queued", None
    if run_state == "running":
        return "in_progress", None
    if run_state == "succeeded" and exit_code == 0:
        return "completed", "success"
    if run_state == "cancelled":
        return "completed", "cancelled"
    return "completed", "failure"


def check_summary(run_state, exit_code):
    """The check output summary: the run state and, when present, the exit code."""

    if not isinstance(run_state, str) or run_state == "" or len(run_state) > 64:
        text = "unknown"
    elif exit_code is None:
        text = run_state
    else:
        text = f"{run_state} exit_code {exit_code}"
    return mask_prefixes(text)[:256]


def _refuse(message=_KEY_MESSAGE):
    raise StatusError("APP_KEY_UNREADABLE", message)


def _inside(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _symlink_component(path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _open_checked(path, directory, mode, message):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        _refuse(message)
    try:
        info = os.fstat(fd)
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or not kind(info.st_mode)
        ):
            _refuse(message)
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_limited(fd, limit, message):
    chunks = []
    size = 0
    while block := os.read(fd, 4096):
        size += len(block)
        if size > limit:
            _refuse(message)
        chunks.append(block)
    return b"".join(chunks)


def _read_file(path, limit, message):
    fd = _open_checked(path, False, 0o600, message)
    try:
        return _read_limited(fd, limit, message)
    finally:
        os.close(fd)


def _one_line(blob, pattern, message):
    try:
        text = blob.decode("utf-8")
    except UnicodeError:
        _refuse(message)
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if pattern.fullmatch(text) is None:
        _refuse(message)
    return text


def _der_tlv(data, offset):
    if offset >= len(data):
        raise ValueError("der")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("der")
    first = data[offset]
    offset += 1
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 3 or offset + count > len(data):
            raise ValueError("der")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise ValueError("der")
    return tag, offset, end


def _der_sequence(data):
    tag, start, end = _der_tlv(data, 0)
    if tag != 0x30 or end != len(data):
        raise ValueError("der")
    items = []
    cursor = start
    while cursor < end:
        item_tag, item_start, item_end = _der_tlv(data, cursor)
        items.append((item_tag, data[item_start:item_end]))
        cursor = item_end
    if cursor != end:
        raise ValueError("der")
    return items


def _der_integer(blob):
    if not blob or blob[0] & 0x80:
        raise ValueError("der")
    return int.from_bytes(blob, "big")


def _rsa_from_pkcs1(items):
    if len(items) < 4:
        raise ValueError("der")
    numbers = []
    for tag, blob in items:
        if tag != 0x02:
            raise ValueError("der")
        numbers.append(_der_integer(blob))
    if numbers[0] != 0:
        raise ValueError("der")
    modulus, _exponent, private = numbers[1], numbers[2], numbers[3]
    if modulus.bit_length() < 2048 or private.bit_length() < 2:
        raise ValueError("der")
    return modulus, private


def _der_items(data):
    items = []
    cursor = 0
    while cursor < len(data):
        tag, start, end = _der_tlv(data, cursor)
        items.append((tag, data[start:end]))
        cursor = end
    return items


def _load_rsa(pem):
    try:
        text = pem.decode("ascii")
    except UnicodeError:
        raise ValueError("pem") from None
    header = None
    body = []
    ended = False
    for line in text.splitlines():
        if line.startswith("-----BEGIN ") and line.endswith("-----"):
            if header is not None:
                raise ValueError("pem")
            header = line
            continue
        if line.startswith("-----END ") and line.endswith("-----"):
            ended = True
            break
        if header is not None and line.strip() != "":
            body.append(line.strip())
    if (
        header not in {"-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----"}
        or not ended
    ):
        raise ValueError("pem")
    try:
        der = base64.b64decode("".join(body), validate=True)
    except ValueError:
        raise ValueError("pem") from None
    items = _der_sequence(der)
    if header.endswith("RSA PRIVATE KEY-----"):
        return _rsa_from_pkcs1(items)
    if (
        len(items) < 3
        or items[0][0] != 0x02
        or _der_integer(items[0][1]) != 0
        or items[1][0] != 0x30
        or items[2][0] != 0x04
    ):
        raise ValueError("pem")
    algorithm = _der_items(items[1][1])
    if not algorithm or algorithm[0][0] != 0x06 or algorithm[0][1] != _RSA_OID:
        raise ValueError("pem")
    return _rsa_from_pkcs1(_der_sequence(items[2][1]))


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _rs256(modulus, private, message):
    digest = hashlib.sha256(message).digest()
    info = _DIGEST_INFO + digest
    width = (modulus.bit_length() + 7) // 8
    if len(info) + 11 > width:
        raise ValueError("rsa")
    padded = b"\x00\x01" + (b"\xff" * (width - len(info) - 3)) + b"\x00" + info
    signature = pow(int.from_bytes(padded, "big"), private, modulus)
    return signature.to_bytes(width, "big")


def sign_app_jwt(pem, client_id, moment):
    """Sign one RS256 JWT. iat is 60 seconds before moment. exp is 10 minutes after iat."""

    if moment.tzinfo is None:
        raise StatusError("INVALID_PARAMS", "check time is not valid")
    modulus, private = _load_rsa(pem)
    issued = int(moment.timestamp()) - 60
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {"iat": issued, "exp": issued + 600, "iss": client_id},
            separators=(",", ":"),
        ).encode("ascii")
    )
    signing = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_b64url(_rs256(modulus, private, signing))}"


def _redirect(label):
    kind = "TOKEN_REJECTED" if label == "token" else "CHECK_REJECTED"

    class Handler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise StatusError(kind, f"GitHub {label} request failed with HTTP {code}")

    return Handler


def _call(url, method, body, token, label):
    kind = "TOKEN_REJECTED" if label == "token" else "CHECK_REJECTED"
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(_redirect(label))
    try:
        with opener.open(request, timeout=STATUS_TIMEOUT_SECONDS) as response:
            code = response.status
            raw = response.read(_MAX_RESPONSE + 1)
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
        raise StatusError(kind, f"GitHub {label} request failed with HTTP {code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise StatusError(kind, f"GitHub {label} request failed") from None
    if code not in {200, 201} or len(raw) > _MAX_RESPONSE:
        raise StatusError(kind, f"GitHub {label} request failed with HTTP {code}")
    return code, raw


def post_installation_token(api_base, installation_id, jwt, payload):
    """POST one installation access token. The caller chooses the body.

    The check-run post passes Checks write and Commit statuses write, and
    does not pass ``repositories``. A job token passes both ``repositories``
    and Contents read. Redirects are refused.
    """

    origin = api_origin(api_base)
    url = f"{origin}/app/installations/{installation_id}/access_tokens"
    body = canonical(payload).encode("ascii")
    code, raw = _call(url, "POST", body, jwt, "token")
    try:
        parsed = strict_json(raw)
        token = parsed.get("token") if isinstance(parsed, dict) else None
        if (
            not isinstance(token, str)
            or token == ""
            or len(token) > 4096
            or any(character.isspace() for character in token)
        ):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError, TypeError, AttributeError):
        raise StatusError(
            "TOKEN_REJECTED", f"GitHub token request failed with HTTP {code}"
        ) from None
    return token


def _exchange(api_base, installation_id, jwt):
    """Mint the check-run post token. The body has no ``repositories`` field."""

    return post_installation_token(
        api_base,
        installation_id,
        jwt,
        {"permissions": {"checks": "write", "statuses": "write"}},
    )


def revoke_installation_token(api_base, token):
    """DELETE /installation/token. True only for HTTP 204.

    ``_call`` accepts only 200 and 201, so this request has its own path.
    A failure returns False and does not raise. Redirects are refused, so
    the token is not sent to another host.
    """

    if not isinstance(token, str) or token == "":
        return False
    try:
        origin = api_origin(api_base)
    except StatusError:
        return False
    request = urllib.request.Request(f"{origin}/installation/token", method="DELETE")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(_redirect("token"))
    try:
        with opener.open(request, timeout=STATUS_TIMEOUT_SECONDS) as response:
            code = response.status
            response.read(64)
    except StatusError:
        return False
    except urllib.error.HTTPError as error:
        try:
            error.read(64)
        except OSError:
            pass
        return False
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return code == 204


def _github_time(moment):
    if moment.tzinfo is None:
        raise StatusError("INVALID_PARAMS", "check time is not valid")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_check(
    api_base,
    repository,
    sha,
    context,
    run_id,
    check_status,
    check_conclusion,
    summary,
    check_run_id,
    token,
    moment,
):
    origin = api_origin(api_base)
    require_repository(repository)
    require_sha(sha)
    if not isinstance(summary, str):
        summary = ""
    summary = mask_prefixes(summary)
    payload = {
        "name": context,
        "external_id": run_id,
        "status": check_status,
        "output": {"title": context, "summary": summary},
    }
    if check_run_id is None:
        payload["head_sha"] = sha
        url = f"{origin}/repos/{repository}/check-runs"
        method = "POST"
    else:
        url = f"{origin}/repos/{repository}/check-runs/{int(check_run_id)}"
        method = "PATCH"
    if check_status == "in_progress":
        payload["started_at"] = _github_time(moment)
    if check_conclusion is not None:
        payload["conclusion"] = check_conclusion
        payload["completed_at"] = _github_time(moment)
    body = mask_prefixes(canonical(payload)).encode("ascii")
    code, raw = _call(url, method, body, token, "check")
    try:
        parsed = strict_json(raw)
        identifier = parsed.get("id") if isinstance(parsed, dict) else None
        if not is_integer(identifier) or isinstance(identifier, bool):
            raise ValueError
        value = int(identifier)
        if not 1 <= value <= 2**63 - 1:
            raise ValueError
    except (ValueError, UnicodeError, RecursionError, TypeError, AttributeError):
        raise StatusError(
            "CHECK_REJECTED", f"GitHub check request failed with HTTP {code}"
        ) from None
    return value


def load_app_key(path, state_dir, repository):
    """Read the key, client id, and installation id. Refuse before any HTTP."""

    candidate = Path(path).expanduser()
    allowed = Path.home() / "Secrets" / "github-app" / "rookrunner-app"
    if not candidate.is_absolute() or candidate.name != "private-key.pem":
        _refuse()
    if _symlink_component(candidate) or _symlink_component(allowed):
        _refuse()
    try:
        resolved = candidate.resolve(strict=True)
        allowed_resolved = allowed.resolve(strict=True)
        state_root = Path(state_dir).resolve(strict=True)
        repo_root = Path(repository).resolve(strict=True)
    except OSError:
        _refuse()
    if resolved.parent != allowed_resolved:
        _refuse()
    if _inside(resolved, state_root) or _inside(resolved, repo_root):
        _refuse()
    attempts = state_root / "attempts"
    try:
        if attempts.exists() and _inside(resolved, attempts.resolve()):
            _refuse()
    except OSError:
        _refuse()
    directory = _open_checked(allowed_resolved, True, 0o700, _KEY_MESSAGE)
    os.close(directory)
    pem = _read_file(resolved, _MAX_PEM, _KEY_MESSAGE)
    try:
        client_id = _one_line(
            _read_file(allowed_resolved / "client-id", _MAX_ID_FILE, _INSTALL_MESSAGE),
            _CLIENT_ID,
            _INSTALL_MESSAGE,
        )
        installation_id = _one_line(
            _read_file(allowed_resolved / "installation-id", _MAX_ID_FILE, _INSTALL_MESSAGE),
            _INSTALLATION_ID,
            _INSTALL_MESSAGE,
        )
        if int(installation_id) > 2**63 - 1:
            _refuse(_INSTALL_MESSAGE)
        _load_rsa(pem)
    except ValueError:
        _refuse(_PEM_MESSAGE)
    return _AppKey(pem, client_id, installation_id)


def _prefer(current, new):
    if new is None:
        return current
    if current is not None and current.retryable:
        return current
    if new.retryable or current is None:
        return new
    return current


def post_check_flow(
    *,
    api_base,
    repository,
    sha,
    context,
    run_id,
    check_status,
    check_conclusion,
    check_summary_text,
    check_run_id,
    status_state,
    post_status_request,
    app_key,
    state_dir,
    repository_root,
    clock=None,
):
    """Mint a token, post the check, then the commit status. Discard the token."""

    if clock is None:
        clock = datetime.now(timezone.utc)
    posted = AppPost()
    material = load_app_key(app_key, state_dir, repository_root)
    jwt = None
    token = None
    try:
        try:
            jwt = sign_app_jwt(bytes(material.pem), material.client_id, clock)
        except ValueError:
            raise StatusError("APP_KEY_UNREADABLE", _PEM_MESSAGE) from None
        material.pem[:] = b"\x00" * len(material.pem)
        material.pem = None
        material.client_id = None
        try:
            token = _exchange(api_base, material.installation_id, jwt)
        finally:
            jwt = None
        try:
            posted.check_id = _post_check(
                api_base,
                repository,
                sha,
                context,
                run_id,
                check_status,
                check_conclusion,
                check_summary_text,
                check_run_id,
                token,
                clock,
            )
        except StatusError as error:
            if error.retryable or error.kind != "CHECK_REJECTED":
                raise
            posted.error = error
        if post_status_request:
            try:
                posted.status_remaining = post_status(
                    api_base, repository, sha, status_state, context, token
                )
                posted.status_posted = True
            except StatusError as error:
                posted.error = _prefer(posted.error, error)
        return posted
    finally:
        token = None
        jwt = None
        material.clear()
