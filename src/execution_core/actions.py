"""Fetch one SHA-pinned action into the state directory.

The documented forms are ``{owner}/{repo}@{sha}`` and
``{owner}/{repo}/{path}@{sha}``. Only a 40-character lowercase commit SHA
is fetched. The Git command asks for that commit and does not read host
Git configuration or send a credential. The REST archive API is not used.

The action directory is stored under its content digest. A later resolve
reuses that directory only when the bytes still hash to the recorded
digest. A mismatch is not returned. Tests pass an absolute directory in
place of ``https://github.com``.
"""

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import uuid

from .disk import usage
from .protocol import canonical

DEFAULT_ACTION_REMOTE = "https://github.com"


class ActionUnavailable(Exception):
    """The pin could not be fetched. No stored bytes are returned."""


class ActionStorageFull(Exception):
    """Copying the fetched tree into the state directory would exceed the budget."""


class RemoteUse:
    def __init__(self, owner, repository, path, ref):
        self.owner = owner
        self.repository = repository
        self.path = path
        self.ref = ref


class StoredAction:
    def __init__(self, path, digest):
        self.path = path
        self.digest = digest


def _owner_ok(text):
    if len(text) > 39:
        return False
    if text.startswith("-") or text.endswith("-"):
        return False
    for char in text:
        if char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-":
            return False
    return text != ""


def _segment_ok(text):
    if text in {".", ".."} or text.endswith(".git") or len(text) > 100:
        return False
    for char in text:
        if char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-":
            return False
    return text != ""


def _full_sha(text):
    return len(text) == 40 and all(char in "0123456789abcdef" for char in text)


def parse_remote_uses(text):
    """Return a remote use, or None when `text` is not `owner/repo[/path]@ref`."""

    if (
        not isinstance(text, str)
        or "@" not in text
        or "\\" in text
        or text.startswith(("./", "$/", "/", "docker://"))
    ):
        return None
    left, ref = text.rsplit("@", 1)
    parts = left.split("/")
    if len(parts) < 2 or not _owner_ok(parts[0]) or not _segment_ok(parts[1]):
        return None
    if any(not _segment_ok(part) for part in parts[2:]):
        return None
    return RemoteUse(parts[0], parts[1], "/".join(parts[2:]), ref)


def full_sha(ref):
    return _full_sha(ref)


def _git_env():
    # Same clean environment capture uses. HOME and host Git config are absent.
    # credential.helper and http.extraheader are cleared on the command line.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }


def _git(repository, *arguments):
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-c",
                "http.extraheader=",
                "-C",
                str(repository),
                *arguments,
            ],
            capture_output=True,
            env=_git_env(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionUnavailable("action repository could not be fetched") from exc


def _link_escapes(relative, target):
    if target.startswith("/") or "\\" in target or "\0" in target:
        return True
    parent = PurePosixPath(relative).parent
    resolved = PurePosixPath(os.path.normpath(str(parent / target)))
    return resolved.is_absolute() or ".." in resolved.parts


def _iter_tree(root):
    found = []

    def walk(current, prefix):
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise ActionUnavailable("action tree is not accepted") from exc
        for entry in children:
            relative = entry.name if prefix == "" else f"{prefix}/{entry.name}"
            if "\\" in relative or "\0" in relative or ".." in PurePosixPath(relative).parts:
                raise ActionUnavailable("action tree is not accepted")
            try:
                symlink = entry.is_symlink()
            except OSError as exc:
                raise ActionUnavailable("action tree is not accepted") from exc
            if symlink:
                try:
                    target = os.readlink(entry.path)
                except OSError as exc:
                    raise ActionUnavailable("action tree is not accepted") from exc
                if _link_escapes(relative, target):
                    raise ActionUnavailable("action tree is not accepted")
                found.append((relative, "120000", target.encode("utf-8")))
                continue
            if entry.is_dir(follow_symlinks=False):
                found.append((relative, "040000", b""))
                walk(Path(entry.path), relative)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ActionUnavailable("action tree is not accepted")
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
                payload = Path(entry.path).read_bytes()
            except OSError as exc:
                raise ActionUnavailable("action tree is not accepted") from exc
            kind = "100755" if stat.S_IXUSR & mode else "100644"
            found.append((relative, kind, payload))

    walk(root, "")
    found.sort()
    return found


def tree_digest(root):
    """SHA-256 of sorted paths, kinds, and bytes. Symlinks are not followed."""

    hasher = hashlib.sha256()
    for relative, kind, payload in _iter_tree(root):
        hasher.update(f"{kind} {relative}".encode())
        hasher.update(b"\0")
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)
    return hasher.hexdigest()


def _extract_tar(payload, prefix, destination):
    head = "" if prefix == "" else prefix.rstrip("/") + "/"
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                name = member.name.replace("\\", "/")
                while name.startswith("./"):
                    name = name[2:]
                if prefix == "":
                    relative = name
                elif name == prefix.rstrip("/"):
                    continue
                elif name.startswith(head):
                    relative = name[len(head) :]
                else:
                    raise ActionUnavailable("action path is not in the fetched commit")
                if relative == "" or relative.endswith("/"):
                    continue
                path = PurePosixPath(relative)
                if path.is_absolute() or ".." in path.parts or "." in path.parts:
                    raise ActionUnavailable("action tree is not accepted")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    if _link_escapes(relative, member.linkname or ""):
                        raise ActionUnavailable("action tree is not accepted")
                    os.symlink(member.linkname, target)
                    continue
                if member.isdir():
                    target.mkdir(mode=0o700, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ActionUnavailable("action tree is not accepted")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ActionUnavailable("action tree is not accepted")
                data = extracted.read()
                mode = 0o755 if member.mode & 0o111 else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                os.chmod(target, mode)
    except (tarfile.TarError, OSError) as exc:
        raise ActionUnavailable("action repository could not be fetched") from exc


def _copy_tree(source, destination):
    destination.mkdir(mode=0o700)
    for relative, kind, payload in _iter_tree(source):
        target = destination.joinpath(*PurePosixPath(relative).parts)
        if kind == "040000":
            target.mkdir(mode=0o700, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "120000":
            os.symlink(payload.decode("utf-8"), target)
            continue
        mode = 0o755 if kind == "100755" else 0o644
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.chmod(target, mode)


class ActionStore:
    """Content-addressed action trees under ``state/actions``."""

    def __init__(self, state, remote, budget):
        self.state = Path(state)
        self.remote = remote
        self._budget = budget
        self.root = self.state / "actions"
        self.objects = self.root / "objects"
        self.index_path = self.root / "pins.json"
        self.fetches = 0
        self.guard = threading.RLock()
        self._open = False
        self._created = []
        self._index_before = None
        self._index_existed = False

    @property
    def budget(self):
        return self._budget() if callable(self._budget) else self._budget

    @property
    def is_open(self):
        return self._open

    def begin(self):
        with self.guard:
            existed = self.index_path.is_file() and not self.index_path.is_symlink()
            before = self.index_path.read_bytes() if existed else None
            self._open = True
            self._created = []
            self._index_existed = existed
            self._index_before = before

    def commit(self):
        with self.guard:
            self._open = False
            self._created = []

    def rollback(self):
        with self.guard:
            self._rollback()

    def _rollback(self):
        if not self._open:
            return
        self._open = False
        for path in reversed(self._created):
            if path.is_symlink() or not path.is_dir():
                continue
            shutil.rmtree(path)
        self._created = []
        if not self._index_existed:
            if self.index_path.is_file() and not self.index_path.is_symlink():
                self.index_path.unlink()
            return
        self.index_path.write_bytes(self._index_before)
        os.chmod(self.index_path, 0o600)

    def resolve(self, owner, repository, path, commit):
        with self.guard:
            return self._resolve(owner, repository, path, commit)

    def _resolve(self, owner, repository, path, commit):
        cached = self._cached(owner, repository, path, commit)
        if cached is not None:
            return cached
        self.fetches += 1
        with tempfile.TemporaryDirectory(prefix="rookrunner-action-") as temp:
            fetched = self._fetch(owner, repository, path, commit, Path(temp))
            return self._publish(owner, repository, path, commit, fetched)

    def _remote_url(self, owner, repository):
        if self.remote == DEFAULT_ACTION_REMOTE:
            return f"https://github.com/{owner}/{repository}.git"
        root = Path(self.remote)
        if not root.is_absolute():
            raise ActionUnavailable("action repository could not be fetched")
        return str(root / owner / f"{repository}.git")

    def _fetch(self, owner, repository, path, commit, temp):
        url = self._remote_url(owner, repository)
        repo = temp / "repo"
        repo.mkdir(mode=0o700)
        if _git(repo, "init", "--quiet").returncode:
            raise ActionUnavailable("action repository could not be fetched")
        fetched = _git(repo, "fetch", "--no-tags", "--depth", "1", "--", url, commit)
        if fetched.returncode:
            fetched = _git(repo, "fetch", "--no-tags", "--", url, commit)
        if fetched.returncode:
            raise ActionUnavailable("action repository could not be fetched")
        verified = _git(repo, "rev-parse", "--verify", "FETCH_HEAD")
        if verified.returncode or verified.stdout.decode().strip() != commit:
            raise ActionUnavailable("action repository could not be fetched")
        archive_args = ["archive", "--format=tar", commit]
        if path:
            archive_args.append(path)
        archived = _git(repo, *archive_args)
        if archived.returncode:
            raise ActionUnavailable("action repository could not be fetched")
        tree = temp / "tree"
        tree.mkdir(mode=0o700)
        _extract_tar(archived.stdout, path, tree)
        return tree

    def _pins(self):
        if not self.index_path.exists():
            return []
        if self.index_path.is_symlink() or not self.index_path.is_file():
            raise ActionUnavailable("action store is not accepted")
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ActionUnavailable("action store is not accepted") from exc
        if not isinstance(data, list):
            raise ActionUnavailable("action store is not accepted")
        pins = []
        for item in data:
            if not isinstance(item, dict):
                raise ActionUnavailable("action store is not accepted")
            if set(item) != {"owner", "repository", "path", "commit", "digest"}:
                raise ActionUnavailable("action store is not accepted")
            if not all(isinstance(item[key], str) for key in item):
                raise ActionUnavailable("action store is not accepted")
            if not _full_sha(item["commit"]) or len(item["digest"]) != 64:
                raise ActionUnavailable("action store is not accepted")
            if any(char not in "0123456789abcdef" for char in item["digest"]):
                raise ActionUnavailable("action store is not accepted")
            pins.append(item)
        return pins

    def _write_pins(self, pins):
        if self.root.is_symlink() or self.objects.is_symlink():
            raise ActionUnavailable("action store is not accepted")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        payload = (canonical(pins) + "\n").encode("ascii")
        temporary = self.root / f".pins-{uuid.uuid4().hex}"
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.index_path)

    def _cached(self, owner, repository, path, commit):
        for item in self._pins():
            if (
                item["owner"] == owner
                and item["repository"] == repository
                and item["path"] == path
                and item["commit"] == commit
            ):
                return self._verified(item["digest"])
        return None

    def _verified(self, digest):
        if self.objects.is_symlink():
            raise ActionUnavailable("action store is not accepted")
        dest = self.objects / digest
        if dest.is_symlink():
            raise ActionUnavailable("action store is not accepted")
        if not dest.is_dir():
            return None
        try:
            actual = tree_digest(dest)
        except ActionUnavailable:
            shutil.rmtree(dest)
            return None
        if actual != digest:
            shutil.rmtree(dest)
            return None
        return StoredAction(dest, digest)

    def _publish(self, owner, repository, path, commit, tree):
        digest = tree_digest(tree)
        if self.objects.is_symlink() or self.root.is_symlink():
            raise ActionUnavailable("action store is not accepted")
        dest = self.objects / digest
        if dest.is_symlink():
            raise ActionUnavailable("action store is not accepted")
        if dest.is_dir():
            if tree_digest(dest) != digest:
                shutil.rmtree(dest)
            else:
                self._remember(owner, repository, path, commit, digest)
                return StoredAction(dest, digest)
        incoming = usage(tree)
        try:
            used = usage(self.state)
        except OSError as exc:
            raise ActionStorageFull() from exc
        if used + incoming > self.budget:
            raise ActionStorageFull()
        self.objects.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.objects, 0o700)
        temporary = self.objects / f".tmp-{uuid.uuid4().hex}"
        try:
            _copy_tree(tree, temporary)
            if tree_digest(temporary) != digest:
                raise ActionUnavailable("action store is not accepted")
            os.replace(temporary, dest)
        except Exception:
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise
        os.chmod(dest, 0o700)
        if self._open:
            self._created.append(dest)
        self._remember(owner, repository, path, commit, digest)
        return StoredAction(dest, digest)

    def _remember(self, owner, repository, path, commit, digest):
        pins = [
            item
            for item in self._pins()
            if not (
                item["owner"] == owner
                and item["repository"] == repository
                and item["path"] == path
                and item["commit"] == commit
            )
        ]
        pins.append(
            {
                "owner": owner,
                "repository": repository,
                "path": path,
                "commit": commit,
                "digest": digest,
            }
        )
        pins.sort(
            key=lambda item: (item["owner"], item["repository"], item["path"], item["commit"])
        )
        self._write_pins(pins)


def _node24_steps(job, found):
    if not isinstance(job, dict):
        return
    call = job.get("call")
    if isinstance(call, dict):
        for inner in call.get("jobs") or []:
            _node24_steps(inner, found)
        return
    for step in job.get("steps") or []:
        if isinstance(step, dict) and step.get("javascript") == "node24":
            found.append(step)


def _mkdir_private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def stage_node24_actions(plan, store, destination, over_budget):
    """Copy each node24 action into ``destination``.

    The content store is not modified and is not the mount. The same pin is
    copied once. ``over_budget(size)`` runs before any copy. A true result
    copies nothing.
    """

    found = []
    for job in plan.get("jobs") or []:
        _node24_steps(job, found)
    if not found:
        return None
    copies = []
    seen = set()
    total = 0
    for step in found:
        owner = step.get("action_owner")
        repository = step.get("action_repository")
        path = step.get("action_path")
        commit = step.get("action_commit")
        digest = step.get("content_digest")
        if not all(isinstance(item, str) and item != "" for item in (owner, repository, commit)):
            raise ActionUnavailable("action tree is not accepted")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ActionUnavailable("action tree is not accepted")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise ActionUnavailable("action tree is not accepted")
        key = (owner, repository, path, commit)
        if key in seen:
            continue
        seen.add(key)
        stored = store.resolve(owner, repository, path, commit)
        if stored.digest != digest:
            raise ActionUnavailable("action directory changed")
        try:
            total += usage(stored.path)
        except OSError as exc:
            raise ActionUnavailable("action tree is not accepted") from exc
        dest = Path(destination) / owner / repository / commit
        if path:
            dest = dest.joinpath(*relative.parts)
        copies.append((dest, stored.path))
    if over_budget(total):
        raise ActionStorageFull()
    root = Path(destination)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ActionUnavailable("action tree is not accepted")
    _mkdir_private(root)
    for dest, source in copies:
        if dest.exists():
            continue
        _mkdir_private(dest.parent)
        _copy_tree(source, dest)
        os.chmod(dest, 0o700)
    return root
