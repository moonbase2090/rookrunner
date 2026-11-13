#!/usr/bin/env python3
"""Verify and unpack an operator-produced, credential-free pod payload."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from urllib.parse import urlsplit


REQUIRED = (
    "bin/pmux",
    "bin/pmuxd",
    "bin/pmux-mcp",
    "toolchains/node/bin/node",
    "toolchains/node/bin/npm",
    "toolchains/rust/bin/cargo",
    "toolchains/rust/bin/rustc",
)
ELF = tuple(path for path in REQUIRED if not path.endswith("/npm"))
GROUPS = {"mux", "node", "rust", "clients", "cache"}
FORBIDDEN = {
    ".aws",
    ".ssh",
    ".gnupg",
    ".config",
    ".git",
    ".env",
    ".netrc",
    ".npmrc",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "auth.json",
    "boot-token",
}


def executable_member(name, members):
    visited = set()
    while name not in visited:
        visited.add(name)
        item = members.get(name)
        if item is None:
            return None
        if item.isfile():
            return item if item.mode & 0o111 else None
        if not item.issym():
            return None
        name = os.path.normpath(str(PurePosixPath(name).parent / item.linkname))
    return None


def verify(archive, digest):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("payload SHA-256 mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        members = {}
        for item in bundle.getmembers():
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("payload path escapes its root")
            name = str(path)
            if name in members:
                raise ValueError("duplicate payload path")
            if path.parts[0] not in {
                "bin",
                "toolchains",
                "clients",
                "cache",
                "licenses",
                "manifest.json",
            }:
                raise ValueError("unexpected payload root")
            if any(part in FORBIDDEN or part.endswith((".pem", ".key")) for part in path.parts):
                raise ValueError("credential or machine-state path in payload")
            if not (item.isfile() or item.isdir() or item.issym()):
                raise ValueError("payload contains a special file or hard link")
            if item.mode & 0o7000:
                raise ValueError("privileged file mode in payload")
            if item.issym():
                target = os.path.normpath(str(path.parent / item.linkname))
                if item.linkname.startswith("/") or target == ".." or target.startswith("../"):
                    raise ValueError("payload symlink escapes its root")
            members[name] = item
        for name in members:
            if any(
                members.get(str(parent), tarfile.TarInfo()).issym()
                for parent in PurePosixPath(name).parents
            ):
                raise ValueError("payload writes through a symlink")
        for name in ("manifest.json", *REQUIRED):
            if name not in members:
                raise ValueError(f"payload is missing {name}")
        if not executable_member("toolchains/node/bin/npm", members):
            raise ValueError("npm must resolve to an executable file in the payload")
        manifest_item = members["manifest.json"]
        if not manifest_item.isfile() or manifest_item.size > 1024 * 1024:
            raise ValueError("invalid payload manifest")
        manifest = json.load(bundle.extractfile(manifest_item))
        if manifest.get("version") != 1 or manifest.get("architecture") != "x86_64":
            raise ValueError("unsupported payload version or architecture")
        components = manifest.get("components", [])
        if {item.get("group") for item in components} != GROUPS:
            raise ValueError(
                "manifest must identify mux, node, rust, clients, and cache provenance"
            )
        for component in components:
            if not all(
                isinstance(component.get(key), str) and component[key].strip()
                for key in ("name", "version", "source", "license", "notice")
            ):
                raise ValueError("component provenance is incomplete")
            url = urlsplit(component["source"])
            if url.scheme != "https" or not url.netloc or url.username or url.password or url.query:
                raise ValueError("component source must be a public HTTPS URL without credentials")
            notice = members.get(component["notice"])
            if (
                not component["notice"].startswith("licenses/")
                or not notice
                or not notice.isfile()
                or not notice.size
            ):
                raise ValueError("component license notice is missing")
        for name in ELF:
            item = members[name]
            if not item.isfile() or not item.mode & 0o111:
                raise ValueError(f"{name} must be an executable file")
            header = bundle.extractfile(item).read(20)
            if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
                raise ValueError(f"{name} is not a Linux x86_64 ELF executable")
        if not any(
            name.startswith("clients/bin/") and executable_member(name, members) for name in members
        ):
            raise ValueError("payload has no installed client CLI")
        for prefix in (
            "cache/cargo/registry/cache/",
            "cache/cargo/registry/index/",
            "cache/npm/_cacache/",
        ):
            if not any(
                name.startswith(prefix) and item.isfile() and item.size
                for name, item in members.items()
            ):
                raise ValueError(f"payload cache is cold: {prefix}")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "install"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("sha256")
    parser.add_argument("--destination", type=Path, default=Path("/opt/pod"))
    args = parser.parse_args()
    try:
        manifest = verify(args.archive, args.sha256)
        if args.action == "install":
            args.destination.mkdir(mode=0o755)  # Refuse a partially installed or existing tree.
            with tarfile.open(args.archive, "r:gz") as bundle:
                bundle.extractall(args.destination, filter="data")
            for path in args.destination.rglob("*"):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        sys.stdout.write(
            json.dumps({"verified": True, "sha256": args.sha256, "manifest": manifest}) + "\n"
        )
    except (ValueError, OSError, tarfile.TarError, KeyError, TypeError) as error:
        parser.exit(1, f"payload rejected: {error}\n")


if __name__ == "__main__":
    main()
