# SPDX-License-Identifier: MIT

"""Test-only private RSA keys and fixed RFC 7638 SHA-256 thumbprints.

The DER keys were generated with OpenSSL. Each thumbprint was checked with
`openssl dgst -sha256 -binary` over the public JWK's literal e, kty, n members
in that order, without whitespace, then base64url encoded without padding.
No expected value is derived through execution_core.
"""

from pathlib import Path
import subprocess


KEY_IDS = {
    "a": "yr3K9sngVDBSFmNDI0aHEKclmB7EPwDHqgyELwgY4Aw",
    "b": "ibRhHKlZqRDAhRpyKSEKnk4DlMUupYsAqnykK00V-4w",
    "small": "nsnkst76pFqLwGAfN4PU0T07ijKZ-zPj3TEtUgVZEX0",
}


def write_key(path, name="a"):
    """Decode a fixed fixture into a temporary PEM file for the real key loader."""

    fixture = Path(__file__).parent / "fixtures" / "oidc" / f"{name}.der"
    subprocess.run(
        ["openssl", "pkey", "-inform", "DER", "-in", str(fixture), "-out", str(path)],
        check=True,
        capture_output=True,
    )
