#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ruff: noqa: T201

"""Build and install immutable, manifest-verified Rookrunner engine bundles."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "engine-manifest.json"
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _run(args, *, cwd=ROOT, env=None, capture=False):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _source_archive(commit):
    source_tar = _run(
        ["git", "archive", "--format=tar", commit, "src/execution_core", "LICENSE"],
        capture=True,
    ).stdout
    return gzip.compress(source_tar, compresslevel=9, mtime=0)


def _canonical_tar(files):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(archive.getvalue(), compresslevel=9, mtime=0)


def build(output_dir):
    dirty = _run(["git", "status", "--porcelain"], capture=True).stdout
    if dirty:
        raise ValueError("build the engine release from a clean commit")
    commit = _run(["git", "rev-parse", "HEAD"], capture=True).stdout.decode().strip()
    if not _COMMIT.fullmatch(commit):
        raise ValueError("Git did not return a full commit id")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    source_archive = _source_archive(commit)
    source_digest = hashlib.sha256(source_archive).hexdigest()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    code = (
        "import json,sys; "
        "from execution_core.release_manifest import make_manifest; "
        "print(json.dumps(make_manifest(sys.argv[1], sys.argv[2], sys.argv[3]), "
        "sort_keys=True, separators=(',', ':')))"
    )
    manifest = _run(
        [sys.executable, "-c", code, commit, source_digest, str(ROOT / "src/execution_core")],
        env=env,
        capture=True,
    ).stdout
    # Verify that the generated descriptor names this source archive before packaging it.
    parsed = json.loads(manifest)
    if parsed["source_commit"] != commit or parsed["source_archive_sha256"] != source_digest:
        raise ValueError("generated engine manifest does not match the source archive")
    bundle = _canonical_tar({"manifest.json": manifest + b"\n", "source.tar.gz": source_archive})
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"rookrunner-engine-{version}-{commit}.tar.gz"
    if destination.exists():
        if (
            hashlib.sha256(destination.read_bytes()).hexdigest()
            != hashlib.sha256(bundle).hexdigest()
        ):
            raise ValueError("refusing to replace a different artifact for this engine commit")
    else:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(bundle)
        os.replace(temporary, destination)
    print(destination)
    print(f"sha256 {hashlib.sha256(bundle).hexdigest()}")
    print(f"source_sha256 {source_digest}")
    return destination


def _members(tar):
    found = {}
    for member in tar.getmembers():
        name = PurePosixPath(member.name)
        if (
            name.is_absolute()
            or ".." in name.parts
            or not member.isfile()
            or len(name.parts) != 1
            or name.name not in {"manifest.json", "source.tar.gz"}
        ):
            raise ValueError("engine bundle contains an unsupported entry")
        if name.name in found:
            raise ValueError("engine bundle contains a duplicate entry")
        if member.size > 256 * 1024 * 1024:
            raise ValueError("engine bundle entry is too large")
        stream = tar.extractfile(member)
        if stream is None:
            raise ValueError("engine bundle entry is unreadable")
        found[name.name] = stream.read()
    if set(found) != {"manifest.json", "source.tar.gz"}:
        raise ValueError("engine bundle is incomplete")
    return found


def _read_bundle(bundle_path):
    with tarfile.open(bundle_path, mode="r:gz") as tar:
        files = _members(tar)
    try:
        manifest = json.loads(files["manifest.json"])
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("engine bundle manifest is invalid") from None
    source = files["source.tar.gz"]
    if manifest.get("source_archive_sha256") != hashlib.sha256(source).hexdigest():
        raise ValueError("engine bundle source digest does not match the manifest")
    commit = manifest.get("source_commit")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("engine bundle commit is invalid")
    if manifest.get("manifest_version") != 1:
        raise ValueError("engine bundle manifest version is unsupported")
    version = manifest.get("package_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("engine bundle package version is invalid")
    return manifest, source


def _extract_source(source_archive, destination):
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(source_archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("source archive contains an unsupported entry")
            if member.name != "LICENSE" and name.parts[:2] != ("src", "execution_core"):
                raise ValueError("source archive contains an unexpected path")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("source archive contains an unsupported entry")
            if member.size > 32 * 1024 * 1024:
                raise ValueError("source archive entry is too large")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError("source archive entry is unreadable")
            with target.open("xb") as output:
                shutil.copyfileobj(stream, output)


def _verify_staged(staging, manifest):
    package = staging / "src/execution_core"
    manifest_path = package / MANIFEST_NAME
    if manifest_path.exists():
        installed = json.loads(manifest_path.read_text(encoding="utf-8"))
        if installed != manifest:
            raise ValueError("installed engine manifest does not match the bundle")
    else:
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(staging / "src")
    code = (
        "import json; "
        "from execution_core.release_manifest import installed_manifest; "
        "print(json.dumps(installed_manifest(), sort_keys=True))"
    )
    _run([sys.executable, "-c", code], cwd=staging, env=env, capture=True)


def _make_immutable(root):
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValueError("release tree contains a symlink")
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(root, 0o555)


def install(bundle_path, root):
    manifest, source = _read_bundle(bundle_path)
    commit = manifest["source_commit"]
    root.mkdir(parents=True, exist_ok=True)
    releases = root / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    destination = releases / commit
    if destination.exists():
        _verify_staged(destination, manifest)
        print(destination)
        return destination
    with tempfile.TemporaryDirectory(prefix=".engine-install-", dir=releases) as temporary:
        staging = Path(temporary) / "release"
        _extract_source(source, staging)
        _verify_staged(staging, manifest)
        _make_immutable(staging)
        os.rename(staging, destination)
    print(destination)
    return destination


def activate(root, commit):
    if not _COMMIT.fullmatch(commit):
        raise ValueError("engine commit must be a full Git object id")
    release = root / "releases" / commit
    if not release.is_dir() or release.is_symlink():
        raise ValueError("requested engine release is not installed")
    _verify_staged(
        release, json.loads((release / "src/execution_core" / MANIFEST_NAME).read_text())
    )
    active = root / "engine"
    if active.exists() and not active.is_symlink():
        raise ValueError("active engine path is not a symlink; migrate it before activation")
    temporary = root / f".engine-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(Path("releases") / commit, target_is_directory=True)
    os.replace(temporary, active)
    print(release)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    install_command = commands.add_parser("install")
    install_command.add_argument("bundle", type=Path)
    install_command.add_argument("--root", type=Path, required=True)
    activate_command = commands.add_parser("activate")
    activate_command.add_argument("commit")
    activate_command.add_argument("--root", type=Path, required=True)
    inspect_command = commands.add_parser("inspect")
    inspect_command.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.output_dir)
        elif args.command == "install":
            install(args.bundle, args.root)
        elif args.command == "activate":
            activate(args.root, args.commit)
        elif args.command == "inspect":
            sys.path.insert(0, str(ROOT / "src"))
            from execution_core.engine_fleet import inspect_fleet

            report = inspect_fleet(args.inventory)
            print(json.dumps(report, sort_keys=True, separators=(",", ":")))
            return 1 if any(unit["status"] != "ok" for unit in report) else 0
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"engine-release: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
