# SPDX-License-Identifier: MIT

"""Signing keys for job identity tokens, the JWKS, and the discovery document.

Keys live in the key directory under ``oidc/``, one RSA private key per
file named ``<kid>.pem``. The ``kid`` is the RFC 7638 JWK thumbprint of
the public key, so it is derived and never chosen. ``oidc/active`` names
the one ``kid`` that signs. Every other key is published but does not
sign. The directory is mode 0700 and each file is mode 0600, opened with
the same ``O_NOFOLLOW`` and ``fstat`` checks as the App key.

``publish`` writes the two public files an issuer host serves. It never
writes private key material and refuses a JWKS without the active key.

https://www.rfc-editor.org/rfc/rfc7638
https://www.rfc-editor.org/rfc/rfc7517
https://openid.net/specs/openid-connect-discovery-1_0.html
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import urllib.parse

from .checks import (
    _b64url,
    _load_rsa_key,
    _one_line,
    _open_checked,
    _read_file,
    _rs256,
    _symlink_component,
)
from .protocol import canonical
from .socket_lock import resolve_key_dir
from .status import StatusError

OIDC_DIR = "oidc"
ACTIVE_FILE = "active"
WELL_KNOWN = ".well-known"
DISCOVERY_FILE = "openid-configuration"
JWKS_FILE = "jwks.json"
MIN_BITS = 2048
# Rotation needs at most three keys: previous, current, and next.
MAX_KEYS = 8
_MAX_PEM = 65536
_MAX_ACTIVE = 128
_MAX_ISSUER = 255
_KID = re.compile(r"[A-Za-z0-9_-]{43}")
_KEY_MESSAGE = "oidc key is not readable"
_ACTIVE_MESSAGE = "oidc active key is not readable"

CLAIMS_SUPPORTED = (
    "iss",
    "sub",
    "aud",
    "exp",
    "iat",
    "nbf",
    "jti",
    "repository",
    "repository_id",
    "repository_owner",
    "ref",
    "ref_type",
    "sha",
    "run_id",
    "run_attempt",
    "workflow",
    "workflow_ref",
    "job_workflow_ref",
    "event_name",
    "actor",
    "environment",
    "runner_environment",
)


class OidcError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _refuse(message=_KEY_MESSAGE):
    raise OidcError("OIDC_KEY_UNREADABLE", message)


def _uint(value):
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def rsa_jwk(modulus, exponent):
    """The public JWK members RFC 7638 hashes for an RSA key."""

    return {"e": _uint(exponent), "kty": "RSA", "n": _uint(modulus)}


def jwk_thumbprint(jwk):
    """RFC 7638 SHA-256 thumbprint: required members, sorted, no whitespace."""

    required = {name: jwk[name] for name in ("e", "kty", "n")}
    text = json.dumps(required, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _b64url(hashlib.sha256(text.encode("ascii")).digest())


class OidcKey:
    """One RSA key. The private exponent is not part of the repr or the JWK."""

    __slots__ = ("_exponent", "_modulus", "_private", "bits", "kid")

    def __init__(self, modulus, exponent, private):
        self._modulus = modulus
        self._exponent = exponent
        self._private = private
        self.bits = modulus.bit_length()
        self.kid = jwk_thumbprint(rsa_jwk(modulus, exponent))

    def __repr__(self):
        return f"OidcKey(kid={self.kid!r}, bits={self.bits})"

    def public_jwk(self):
        jwk = rsa_jwk(self._modulus, self._exponent)
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid, **jwk}

    def sign(self, message):
        return _rs256(self._modulus, self._private, message)


class OidcKeySet:
    """Every configured key and the one that signs."""

    def __init__(self, keys, active_kid):
        by_kid = {key.kid: key for key in keys}
        if len(by_kid) != len(keys):
            _refuse("two oidc keys have the same kid")
        if active_kid not in by_kid:
            raise OidcError("OIDC_ACTIVE_MISSING", f"oidc active key {active_kid} has no key file")
        self.keys = tuple(sorted(keys, key=lambda key: key.kid))
        self.active = by_kid[active_kid]

    @property
    def kids(self):
        return [key.kid for key in self.keys]

    def jwks(self):
        """The JWKS. The active key comes first, then the rest by kid."""

        rest = [key.public_jwk() for key in self.keys if key is not self.active]
        return {"keys": [self.active.public_jwk(), *rest]}

    def sign_jwt(self, claims):
        """Sign one RS256 JWT with the active key only."""

        header = {"alg": "RS256", "typ": "JWT", "kid": self.active.kid}
        encoded = [
            _b64url(json.dumps(part, separators=(",", ":")).encode("ascii"))
            for part in (header, claims)
        ]
        signing = ".".join(encoded).encode("ascii")
        return ".".join([*encoded, _b64url(self.active.sign(signing))])


def _checked(call, message):
    try:
        return call()
    except StatusError:
        _refuse(message)


def oidc_dir(key_dir=None):
    try:
        return resolve_key_dir(key_dir) / OIDC_DIR
    except ValueError:
        _refuse()


def load_key_set(key_dir=None):
    """Read every ``oidc/<kid>.pem`` and ``oidc/active``. Refuse before any use."""

    directory = oidc_dir(key_dir)
    if _symlink_component(directory):
        _refuse()
    for path in (directory.parent, directory):
        fd = _checked(
            lambda path=path: _open_checked(path, True, 0o700, _KEY_MESSAGE), _KEY_MESSAGE
        )
        os.close(fd)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        _refuse()
    pems = [name for name in names if name.endswith(".pem")]
    if not pems:
        _refuse("oidc key directory has no key")
    if len(pems) > MAX_KEYS:
        _refuse(f"oidc key directory has more than {MAX_KEYS} keys")
    keys = []
    for name in pems:
        path = directory / name
        pem = _checked(lambda path=path: _read_file(path, _MAX_PEM, _KEY_MESSAGE), _KEY_MESSAGE)
        try:
            key = OidcKey(*_load_rsa_key(pem))
        except ValueError:
            _refuse(f"oidc key {name} is not an RSA key of at least {MIN_BITS} bits")
        if name != f"{key.kid}.pem":
            _refuse(f"oidc key {name} has kid {key.kid}; name the file {key.kid}.pem")
        keys.append(key)
    raw = _checked(
        lambda: _read_file(directory / ACTIVE_FILE, _MAX_ACTIVE, _ACTIVE_MESSAGE),
        _ACTIVE_MESSAGE,
    )
    active = _checked(lambda: _one_line(raw, _KID, _ACTIVE_MESSAGE), _ACTIVE_MESSAGE)
    return OidcKeySet(keys, active)


def require_issuer(issuer):
    """An HTTPS URL with a host, no query, fragment, or user info, and no trailing slash."""

    message = "oidc issuer must be an https URL with no query, fragment, or trailing slash"
    if (
        not isinstance(issuer, str)
        or not 0 < len(issuer) <= _MAX_ISSUER
        or not all("!" <= character <= "~" for character in issuer)
        or "?" in issuer
        or "#" in issuer
        or issuer.endswith("/")
    ):
        raise OidcError("INVALID_PARAMS", message)
    parts = urllib.parse.urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        raise OidcError("INVALID_PARAMS", message)
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:
        raise OidcError("INVALID_PARAMS", message)
    return issuer


def discovery_document(issuer):
    issuer = require_issuer(issuer)
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/{WELL_KNOWN}/{JWKS_FILE}",
        "subject_types_supported": ["public"],
        "response_types_supported": ["id_token"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid"],
        "claims_supported": list(CLAIMS_SUPPORTED),
    }


def _write_public(path, document):
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="ascii") as handle:
        handle.write(canonical(document) + "\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def publish(key_set, issuer, out_dir, key_dir=None):
    """Write ``.well-known/openid-configuration`` and ``.well-known/jwks.json``.

    Only public members are written. A JWKS without the active key is refused,
    and an output directory inside the key directory is refused.
    """

    document = discovery_document(issuer)
    jwks = key_set.jwks()
    published = [key.get("kid") for key in jwks["keys"]]
    if key_set.active.kid not in published:
        raise OidcError("OIDC_ACTIVE_MISSING", "the JWKS does not contain the active key")
    if any(set(key) - {"kty", "use", "alg", "kid", "n", "e"} for key in jwks["keys"]):
        raise OidcError("OIDC_KEY_UNREADABLE", "the JWKS holds a member that is not public")
    out = Path(out_dir).expanduser().absolute()
    keys_root = oidc_dir(key_dir).parent
    for root in (keys_root, out):
        if _symlink_component(root):
            raise OidcError("INVALID_PARAMS", "oidc output directory has a symlink component")
    try:
        out.relative_to(keys_root)
    except ValueError:
        pass
    else:
        raise OidcError("INVALID_PARAMS", "oidc output directory is inside the key directory")
    target = out / WELL_KNOWN
    target.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(target).st_mode):
        raise OidcError("INVALID_PARAMS", "oidc output directory is not a directory")
    _write_public(target / JWKS_FILE, jwks)
    _write_public(target / DISCOVERY_FILE, document)
    return {
        "issuer": document["issuer"],
        "active": key_set.active.kid,
        "kids": published,
        "files": [f"{WELL_KNOWN}/{DISCOVERY_FILE}", f"{WELL_KNOWN}/{JWKS_FILE}"],
    }
