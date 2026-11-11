# SPDX-License-Identifier: MIT

"""Build and verify immutable engine release identity."""

import hashlib
import json
from pathlib import Path
import re

from . import __version__
from .capabilities import CAPABILITIES
from .plan import CAPABILITY_VERSION

MANIFEST_NAME = "engine-manifest.json"
STATE_FORMAT_VERSION = 1
PROTOCOL_VERSIONS = [0, 1]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def source_tree_digest(package_dir):
    """Hash installed package files in stable path order, excluding bytecode and this manifest."""

    root = Path(package_dir)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("engine package contains a symlink")
        if not path.is_file() or path.name == MANIFEST_NAME or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        files.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    if not files:
        raise ValueError("engine package has no source files")
    digest = hashlib.sha256()
    for relative, file_digest in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def make_manifest(source_commit, source_archive_sha256, package_dir):
    """Describe one exact source archive and its installed Python package."""

    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("source commit must be a full Git object id")
    if not isinstance(source_archive_sha256, str) or not _SHA256.fullmatch(source_archive_sha256):
        raise ValueError("source archive digest must be a lowercase SHA-256")
    return {
        "manifest_version": 1,
        "source_commit": source_commit,
        "package_version": __version__,
        "source_archive_sha256": source_archive_sha256,
        "source_tree_sha256": source_tree_digest(package_dir),
        "state_format_version": STATE_FORMAT_VERSION,
        "protocol_versions": list(PROTOCOL_VERSIONS),
        "planner_capability_version": CAPABILITY_VERSION,
        "supported_workflow_features": sorted(item.name for item in CAPABILITIES),
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("engine manifest has duplicate keys")
        result[key] = value
    return result


def verify_manifest(package_dir, manifest):
    """Validate the release contract against this installed package."""

    expected_keys = {
        "manifest_version",
        "source_commit",
        "package_version",
        "source_archive_sha256",
        "source_tree_sha256",
        "state_format_version",
        "protocol_versions",
        "planner_capability_version",
        "supported_workflow_features",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise ValueError("engine manifest fields are invalid")
    if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
        raise ValueError("engine manifest version is unsupported")
    if not isinstance(manifest["source_commit"], str) or not _COMMIT.fullmatch(
        manifest["source_commit"]
    ):
        raise ValueError("engine manifest commit is invalid")
    for key in ("source_archive_sha256", "source_tree_sha256"):
        if not isinstance(manifest[key], str) or not _SHA256.fullmatch(manifest[key]):
            raise ValueError(f"engine manifest {key} is invalid")
    if manifest["package_version"] != __version__:
        raise ValueError("engine manifest package version does not match installed code")
    if (
        type(manifest["state_format_version"]) is not int
        or manifest["state_format_version"] != STATE_FORMAT_VERSION
    ):
        raise ValueError("engine manifest state format is unsupported")
    if (
        not isinstance(manifest["protocol_versions"], list)
        or manifest["protocol_versions"] != PROTOCOL_VERSIONS
    ):
        raise ValueError("engine manifest protocol versions do not match installed code")
    if (
        type(manifest["planner_capability_version"]) is not int
        or manifest["planner_capability_version"] != CAPABILITY_VERSION
    ):
        raise ValueError("engine manifest planner capability does not match installed code")
    features = sorted(item.name for item in CAPABILITIES)
    if (
        not isinstance(manifest["supported_workflow_features"], list)
        or any(not isinstance(value, str) for value in manifest["supported_workflow_features"])
        or manifest["supported_workflow_features"] != features
    ):
        raise ValueError("engine manifest workflow features do not match installed code")
    if manifest["source_tree_sha256"] != source_tree_digest(package_dir):
        raise ValueError("engine manifest source digest does not match installed code")
    return manifest


def installed_manifest():
    """Return a verified release manifest, or None for an unpackaged checkout."""

    package_dir = Path(__file__).resolve().parent
    path = package_dir / MANIFEST_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not path.is_file() or path.is_symlink() or info.st_size > 64 * 1024:
        raise ValueError("engine manifest is not a bounded regular file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("engine manifest is invalid") from None
    return verify_manifest(package_dir, manifest)
