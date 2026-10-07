"""One poll pass for one configured repository.

The operating system's scheduler starts this command. The pass lists
branch heads and open pull requests, fetches into the dedicated clone,
submits each new SHA, and posts commit statuses. It then exits. It is
not a resident service, a listener, or a runner registration.

List requests are conditional. On the credential-file path they send
no credential, and that file is read only when a status is posted.
With ``--app-key``, the key is read once to mint an installation
token for the GETs in the pass, and again when a post is about to be
sent. That list token is scoped to the repository, carries the
permissions the installation currently grants, and is revoked when
the pass ends. It is not written to state.
A response whose rate-limit header reports nothing remaining is
not applied, and the pass makes no further HTTP request. The next pass
starts from the last saved checkpoint.

Local rules, not GitHub-equivalence claims:

- The first observation of a branch uses forty ``0`` characters as
  ``before`` and reports the diff unavailable.
- A later push diffs the stored tip against the new tip.
- A pull request's activity is ``opened`` the first time it is seen
  and ``synchronize`` when its head or merge SHA changes.
- A missing ``refs/pull/<number>/merge`` is recorded and runs nothing.
- A selected workflow that is not a tracked file in the tested commit
  is recorded as skipped and the pass continues. The tip is still
  stored. A workflow that is not a regular file, and any other capture
  failure, stops the pass.
- A fork is recorded and runs nothing. The head and base repository
  ids match only when both are integers and equal. A null head
  repository is a fork. The full name is not the comparison.
- A repository with ``.github/CODEOWNERS`` at the pull base or head
  gets a ``rookrunner/signoff`` status on the head SHA. Patterns are
  the union of the two blobs. A protected path without
  ``mb2090-signoff`` is a failure. No CODEOWNERS blob is skipped and
  recorded. The pass does not apply the label and does not run
  ``signoff.yml``.
- The submission key is ``poll-`` plus the SHA-256 of the repository,
  event, tested SHA, workflow, and job.
- An ssh host checks the worker repository ``origin`` owner/name. The
  host is written into ``poll.json`` before submit, and the tested SHA
  is checked out there from this clone. A key recorded for another host
  is left where it is.
- ``--place`` selects a worker in the given order. The place image
  matches ``--image`` when that flag is set, and the worker runner image
  when ``--image`` is omitted. A place that is not ready, full,
  unreachable, or a different repository is named in the summary. A job
  with no matching place is left unrecorded. A recorded place stays,
  including when its run is lost.
- A queued run is cancelled 24 hours after acceptance and reported as
  ``error``. An older SHA is not cancelled because a newer push arrived.
- This pass lists branches and open pull requests. It does not list tags.

https://docs.github.com/en/actions/reference/limits
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
https://docs.github.com/en/rest/commits/statuses
"""

import contextlib
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, UTC

from .checks import post_check_flow
from .protocol import MAX_LIST_PAGE, canonical, is_integer, strict_json
from .status import (
    StatusError,
    github_state,
    header_remaining,
    post_status,
    read_credential,
    status_url,
)
from .trigger import COMMIT_PATH_LIMIT, MAX_CHANGED_FILES

# Self-hosted job queue time.
# https://docs.github.com/en/actions/reference/limits
QUEUE_LIMIT = timedelta(hours=24)
# One response page. GitHub's maximum page size is 100.
# https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api
PAGE_SIZE = 100
# Local bounds. They are not GitHub limits.
_GET_TIMEOUT_SECONDS = 10
_GIT_TIMEOUT_SECONDS = 30
_MAX_BODY = 1024 * 1024
_ZERO = "0" * 40
_WORKFLOW_ABSENT = "workflow must be tracked or explicitly included"
_SIGNOFF_CONTEXT = "rookrunner/signoff"
_GATE = None
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_REPOSITORY = re.compile(rf"{_NAME.pattern}/{_NAME.pattern}")
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:@[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class PollError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class _Budget:
    def __init__(self):
        self.stopped = False

    def observe(self, remaining):
        if remaining == 0:
            self.stopped = True


def queue_expired(accepted_at, clock):
    """Return whether a queued run has waited 24 hours since acceptance."""

    if not isinstance(accepted_at, str):
        return False
    try:
        accepted = datetime.fromisoformat(accepted_at)
    except ValueError:
        return False
    if accepted.tzinfo is None:
        return False
    return clock - accepted >= QUEUE_LIMIT


def submission_key(repository, event, sha, workflow, job):
    """Key built from the repository, event, tested SHA, workflow, and job."""

    material = "\0".join((repository, event, sha, workflow, job)).encode()
    return "poll-" + hashlib.sha256(material).hexdigest()


def _integer_id(value):
    """Return a JSON integer, or None. A bool is not an id."""

    if not is_integer(value):
        return None
    return int(value)


def _login(value):
    """Return a non-empty login string, or None."""

    if not isinstance(value, dict):
        return None
    login = value.get("login")
    if not isinstance(login, str) or login == "":
        return None
    return login


def _ascii_fold(value):
    """Fold A-Z to a-z. Other characters, including non-ASCII letters, stay."""

    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character for character in value
    )


def _same_repository(head, base):
    """Return whether the head and base repository ids are equal integers.

    A missing head repository, including a null ``head.repo``, is not
    the same repository. The full name is not compared.
    """

    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        return False
    head_id = _integer_id(head_repo.get("id"))
    base_id = _integer_id(base_repo.get("id"))
    if head_id is None or base_id is None:
        return False
    return head_id == base_id


def _event_repository(configured, body):
    """Copy the configured full name plus id and default branch from the body."""

    copied = {"full_name": configured}
    if not isinstance(body, dict):
        return copied
    ident = _integer_id(body.get("id"))
    if ident is not None:
        copied["id"] = ident
    branch = body.get("default_branch")
    if isinstance(branch, str) and branch != "":
        copied["default_branch"] = branch
    return copied


def _event_repo(repo):
    """Copy the integer id and, when present, the full name."""

    copied = {"id": _integer_id(repo.get("id"))}
    name = repo.get("full_name")
    if isinstance(name, str) and name != "":
        copied["full_name"] = name
    return copied


def _stored_login(event, event_name):
    if not isinstance(event, dict):
        return None
    if event_name == "push":
        commits = event.get("commits")
        if not isinstance(commits, list) or not commits or not isinstance(commits[-1], dict):
            return None
        return _login(commits[-1].get("author"))
    if event_name == "pull_request":
        pull = event.get("pull_request")
        if not isinstance(pull, dict):
            return None
        return _login(pull.get("user"))
    return None


def _default_push(event, event_name):
    if event_name != "push" or not isinstance(event, dict):
        return False
    ref = event.get("ref")
    repository = event.get("repository")
    if not isinstance(ref, str) or not isinstance(repository, dict):
        return False
    branch = repository.get("default_branch")
    if not isinstance(branch, str) or branch == "":
        return False
    return ref == "refs/heads/" + branch


def allowlist_matches(event, refs, pushers, event_name):
    """Return whether this stored event may receive file-backed secrets.

    ``refs`` and ``pushers`` are the operator lists. With both empty, the
    only match is a push whose ``ref`` is ``refs/heads/`` plus
    ``repository.default_branch``. A listed ref matches that ``ref``
    exactly. A listed login matches the actor login with ASCII case
    folding. The default push stays a match when a list is non-empty.
    For a push, the login is the last commit's ``author.login`` when that
    field is a non-empty string. For a pull request, the login is
    ``pull_request.user.login`` on the same condition. A missing login
    does not match a pusher entry. An event that lacks these fields does
    not match the default rule. This does not read a secret.
    """

    if _default_push(event, event_name):
        return True
    ref = event.get("ref") if isinstance(event, dict) else None
    if (
        isinstance(ref, str)
        and isinstance(refs, list | tuple)
        and any(item == ref for item in refs)
    ):
        return True
    login = _stored_login(event, event_name)
    if login is None or not isinstance(pushers, list | tuple):
        return False
    folded = _ascii_fold(login)
    return any(
        isinstance(item, str) and item != "" and _ascii_fold(item) == folded for item in pushers
    )


def _repository_name(value):
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise PollError("INVALID_PARAMS", "repository must be owner/name")
    return value


def _sha(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise PollError("API_REJECTED", "GitHub response did not include a commit SHA")
    return value


def _ref_name(clone, kind, name):
    if not isinstance(name, str) or name == "" or name.startswith("/") or ".." in name.split("/"):
        raise PollError("API_REJECTED", "GitHub response did not include a ref name")
    ref = f"refs/{kind}/{name}"
    result = _git(clone, "check-ref-format", ref, check=False)
    if result.returncode != 0:
        raise PollError("API_REJECTED", "GitHub response did not include a ref name")
    return ref


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PollError("API_REJECTED", f"GitHub request failed with HTTP {code}")


def _remaining_stop(code, headers):
    if code == 429:
        return True
    if code != 403:
        return False
    return header_remaining(headers) == 0


def _get_json(api_base, path, etag, token=None):
    """GET one list. 304 returns a null body.

    ``token`` is the installation token for an ``--app-key`` pass.
    The credential-file path passes none.
    """

    # status_url validates the origin. The SHA is discarded with the path.
    origin = status_url(api_base, "owner/name", "ab" * 20).rsplit("/repos/", 1)[0]
    url = origin + path
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token is not None:
        if not isinstance(token, str) or token == "" or any(char.isspace() for char in token):
            raise PollError("INVALID_PARAMS", "list token is not usable")
        request.add_header("Authorization", "Bearer " + token)
    if etag:
        request.add_header("If-None-Match", etag)
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=_GET_TIMEOUT_SECONDS) as response:
            code = response.status
            headers = response.headers
            body = response.read(_MAX_BODY + 1)
    except PollError:
        raise
    except urllib.error.HTTPError as error:
        code = error.code
        headers = error.headers
        with contextlib.suppress(OSError):
            error.read(256)
        if code == 304:
            return {
                "modified": False,
                "body": None,
                "etag": etag,
                "remaining": header_remaining(headers),
            }
        if _remaining_stop(code, headers):
            return {
                "modified": False,
                "body": None,
                "etag": None,
                "remaining": 0,
                "exhausted": True,
            }
        raise PollError("API_REJECTED", f"GitHub request failed with HTTP {code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PollError("API_REJECTED", "GitHub request failed") from None
    if code == 304:
        return {
            "modified": False,
            "body": None,
            "etag": etag,
            "remaining": header_remaining(headers),
        }
    if code != 200:
        raise PollError("API_REJECTED", f"GitHub request failed with HTTP {code}")
    if len(body) > _MAX_BODY:
        raise PollError("API_REJECTED", "GitHub list response is too large")
    try:
        parsed = strict_json(body)
    except (ValueError, UnicodeError, RecursionError):
        raise PollError("API_REJECTED", "GitHub list response is not JSON") from None
    return {
        "modified": True,
        "body": parsed,
        "etag": headers.get("ETag"),
        "remaining": header_remaining(headers),
    }


def _git(clone, *args, check=True, input_bytes=None):
    try:
        result = subprocess.run(
            ["git", "-C", str(clone), *args],
            input=input_bytes,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PollError("GIT_FAILED", "git command failed") from None
    if check and result.returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    return result


def _stdout(result):
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        raise PollError("GIT_FAILED", "git command failed") from None


def _checkout(clone, sha):
    _git(clone, "checkout", "--detach", "--force", "--quiet", sha)
    _git(clone, "clean", "-fd", "--quiet")
    head = _stdout(_git(clone, "rev-parse", "HEAD")).strip()
    if head != sha or _git(clone, "status", "--porcelain").stdout != b"":
        raise PollError("GIT_FAILED", "dedicated clone is not a clean checkout of the fetched SHA")


def _accept_ssh_host(host):
    if not isinstance(host, str) or _SSH_HOST.fullmatch(host) is None:
        raise PollError("INVALID_PARAMS", "ssh host is not accepted")
    return host


def _accept_remote_path(path):
    if (
        not isinstance(path, str)
        or path == ""
        or path.startswith("-")
        or any(character in path for character in "\0\r\n")
    ):
        raise PollError("CLONE_MISMATCH", "worker repository is not accepted")
    return path


def _origin_repository(url):
    if not isinstance(url, str):
        raise PollError("CLONE_MISMATCH", "worker repository must be the polled repository")
    text = url.strip()
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@") and ":" in text.split("/", 1)[0]:
        text = text.split(":", 1)[1]
    elif "://" in text:
        text = urllib.parse.urlsplit(text).path
    else:
        raise PollError("CLONE_MISMATCH", "worker repository must be the polled repository")
    text = text.strip("/")
    if _REPOSITORY.fullmatch(text) is None:
        raise PollError("CLONE_MISMATCH", "worker repository must be the polled repository")
    return text


def _ssh_run(host, args, input_bytes=None):
    command = ["ssh", "-o", "BatchMode=yes", host, "--", *args]
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PollError("GIT_FAILED", "git command failed") from None
    if completed.returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    return completed.stdout


def _ssh_text(host, args):
    try:
        return _ssh_run(host, args).decode("utf-8")
    except UnicodeError:
        raise PollError("GIT_FAILED", "git command failed") from None


def _fetch_sha(clone, ref):
    result = _git(clone, "fetch", "--quiet", "origin", ref, check=False)
    if result.returncode != 0:
        return None
    sha = _stdout(_git(clone, "rev-parse", "FETCH_HEAD")).strip()
    if _SHA.fullmatch(sha) is None:
        raise PollError("GIT_FAILED", "git fetch did not resolve a commit")
    return sha


def _digest(value):
    if isinstance(value, str) and _IMAGE_DIGEST.fullmatch(value):
        return value
    if isinstance(value, str) and "@sha256:" in value:
        digest = "sha256:" + value.rsplit("sha256:", 1)[1]
        if _IMAGE_DIGEST.fullmatch(digest):
            return digest
    return None


def _place_state_accepted(path):
    return (
        isinstance(path, str)
        and path != ""
        and not path.startswith("-")
        and not any(character in path for character in "\0\r\n")
    )


def accept_place(place):
    """Return one place. ``caller`` talks to that worker and is not invoked here."""

    if not isinstance(place, dict):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    internal = {"skip", "reachable", "connected", "remote_repository", "active"}
    public = {key: value for key, value in place.items() if key not in internal}
    allowed = {"name", "ssh", "state", "cap", "image", "caller"}
    required = {"name", "state", "cap", "image", "caller"}
    if not required <= set(public) or set(public) - allowed:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    name = public["name"]
    ssh = public.get("ssh")
    if not isinstance(name, str) or _SSH_HOST.fullmatch(name) is None:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    if ssh is not None and (not isinstance(ssh, str) or _SSH_HOST.fullmatch(ssh) is None):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    if not _place_state_accepted(public["state"]):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    cap = public["cap"]
    if type(cap) is not int or not 1 <= cap <= MAX_LIST_PAGE:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    if not isinstance(public["image"], str) or _IMAGE_DIGEST.fullmatch(public["image"]) is None:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    if not callable(public["caller"]):
        raise PollError("INVALID_PARAMS", "place is not accepted")
    return {
        "name": name,
        "ssh": ssh,
        "state": public["state"],
        "cap": cap,
        "image": public["image"],
        "caller": public["caller"],
        "skip": None,
        "reachable": False,
        "connected": False,
        "remote_repository": None,
        "active": 0,
    }


def accept_places(places):
    """Return the placement order. An empty list or a repeated name is refused."""

    if not isinstance(places, list) or not places:
        raise PollError("INVALID_PARAMS", "place is not accepted")
    accepted = []
    seen = set()
    for place in places:
        item = accept_place(place)
        if item["name"] in seen:
            raise PollError("INVALID_PARAMS", "place is not accepted")
        seen.add(item["name"])
        accepted.append(item)
    return accepted


def _diff(clone, before, after):
    """Return changed paths and commit count, or None when the diff is unavailable."""

    files = _git(
        clone,
        "diff",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        before,
        after,
        check=False,
    )
    count = _git(clone, "rev-list", "--count", f"{before}..{after}", check=False)
    if files.returncode != 0 or count.returncode != 0:
        return None
    try:
        commits = int(_stdout(count).strip())
    except ValueError:
        return None
    text = _stdout(files)
    names = [] if text == "" else text.split("\n")
    if names and names[-1] == "":
        names.pop()
    if len(names) > MAX_CHANGED_FILES:
        return None
    return names, commits


def _signoff_gate():
    """Load the repository sign-off script. It does not import this package."""

    global _GATE
    if _GATE is not None:
        return _GATE
    path = Path(__file__).resolve().parents[2] / ".github" / "signoff.py"
    spec = importlib.util.spec_from_file_location("_rookrunner_signoff_gate", path)
    if spec is None or spec.loader is None:
        raise PollError("INVALID_PARAMS", "sign-off gate could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _GATE = module
    return module


def _codeowners_blob(clone, sha):
    """Return the CODEOWNERS text, or None when that commit has no such file."""

    result = _git(clone, "show", f"{sha}:.github/CODEOWNERS", check=False)
    if result.returncode == 0:
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            raise PollError("GIT_FAILED", "git command failed") from None
    err = result.stderr.decode("utf-8", "replace")
    if "does not exist" in err or "exists on disk, but not" in err:
        return None
    raise PollError("GIT_FAILED", "git command failed")


def _signoff_paths(clone, base, head):
    """Return both sides of a rename. Too many paths is a git failure."""

    result = _git(
        clone,
        "diff",
        "--name-status",
        "--find-renames",
        base,
        head,
        check=False,
    )
    if result.returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    paths = []
    for line in _stdout(result).splitlines():
        parts = line.split("\t")
        for path in parts[1:]:
            if path != "" and path not in paths:
                paths.append(path)
    if len(paths) > MAX_CHANGED_FILES:
        raise PollError("GIT_FAILED", "git command failed")
    return paths


def _signoff_labels(item):
    """Label names from the pulls list. A missing field is unlabeled."""

    if "labels" not in item:
        return []
    labels = item.get("labels")
    if not isinstance(labels, list):
        raise PollError("API_REJECTED", "GitHub pull request list is not usable")
    names = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        name = label.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


class Pass:
    def __init__(
        self,
        *,
        repository,
        clone,
        jobs,
        image,
        credential_file,
        api_base,
        state,
        caller,
        clock,
        app_key=None,
        list_token=None,
        ssh_host=None,
        places=None,
    ):
        self.repository = _repository_name(repository)
        self.clone = Path(clone)
        self.jobs = list(jobs)
        self.image = image
        self.credential_file = credential_file
        self.app_key = app_key
        self.list_token = list_token
        self.api_base = api_base
        self.state_dir = Path(state)
        self.caller = caller
        self.clock = clock
        self.ssh_host = ssh_host
        self.places = places
        self.remote_repository = None
        self.budget = _Budget()
        self.state = None
        self.lock_fd = None
        self.repository_body = None
        self.signoff_seen = False
        self.signoff_present = False
        self.result = {"stopped": False, "submitted": [], "statuses": [], "skipped": []}

    def run(self):
        if not self.jobs:
            raise PollError("INVALID_PARAMS", "poll requires at least one workflow job")
        if self.app_key and self.credential_file:
            raise PollError("INVALID_PARAMS", "one post accepts one credential")
        if not self.app_key and not self.credential_file:
            raise PollError("INVALID_PARAMS", "one post needs one credential")
        if self.places is not None:
            self.places = accept_places(self.places)
            if self.ssh_host is not None:
                raise PollError("INVALID_PARAMS", "place and ssh host are set separately")
        elif self.ssh_host is not None:
            _accept_ssh_host(self.ssh_host)
        self._lock()
        try:
            if self.places is None:
                described = self.caller("worker.describe", {})
                if self.ssh_host is None:
                    if Path(described["repository"]).resolve() != self.clone.resolve():
                        raise PollError(
                            "CLONE_MISMATCH", "dedicated clone must be the worker repository"
                        )
                else:
                    remote = _accept_remote_path(described.get("repository"))
                    origin = _ssh_text(
                        self.ssh_host, ["git", "-C", remote, "remote", "get-url", "origin"]
                    )
                    if _origin_repository(origin) != self.repository:
                        raise PollError(
                            "CLONE_MISMATCH", "worker repository must be the polled repository"
                        )
                    self.remote_repository = remote
            else:
                self._probe_places()
            self.state = _load_state(self.state_dir / "poll.json", self.repository)
            if self.places is None:
                self._expire_queued()
            else:
                self._note_unknown_hosts()
                self._connect_claimed()
                self._expire_placed()
            self._branches()
            self._pulls()
            if self.places is None:
                self._post_terminal()
            else:
                self._post_placed()
            self.result["stopped"] = self.budget.stopped
            _save_state(self.state_dir / "poll.json", self.state)
            return self.result
        finally:
            self._unlock()

    def _lock(self):
        path = self.state_dir / "poll.lock"
        if path.is_symlink():
            raise PollError("STATE_INVALID", "poll lock must not be a symlink")
        try:
            self.lock_fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PollError("POLL_BUSY", "another poll pass holds this state directory") from None
        except OSError:
            raise PollError("STATE_INVALID", "poll state could not be locked") from None

    def _unlock(self):
        if self.lock_fd is None:
            return
        os.close(self.lock_fd)
        self.lock_fd = None

    def _probe_places(self):
        for place in self.places:
            self._classify(place, force_connect=False)

    def _connect_claimed(self):
        claimed = {
            entry.get("host")
            for entry in self.state["submissions"]
            if isinstance(entry.get("host"), str)
        }
        for place in self.places:
            if place["name"] in claimed and not place["connected"]:
                self._classify(place, force_connect=True)

    def _classify(self, place, *, force_connect):
        if place["connected"]:
            return
        if not force_connect and self.image is not None and _digest(self.image) != place["image"]:
            self._mark(place, "image")
            return
        place["connected"] = True
        try:
            described = place["caller"]("worker.describe", {})
            if not isinstance(described, dict):
                raise PollError("WORKER_ERROR", "worker describe was not an object")
        except (PollError, OSError):
            # A down socket is a skipped host. It must not fail the pass.
            self._mark(place, "unreachable")
            return
        place["reachable"] = True
        mismatched = (
            described.get("runner_image") != place["image"]
            if self.image is None
            else _digest(self.image) != place["image"]
        )
        if mismatched:
            self._mark(place, "image")
        elif described.get("ready") is not True:
            self._mark(place, "not_ready")
        self._remember_repository(place, described.get("repository"))
        if place["skip"] is not None:
            return
        try:
            place["active"] = self._count_active(place)
        except (PollError, OSError):
            place["reachable"] = False
            self._mark(place, "unreachable")
            return
        if place["active"] >= place["cap"]:
            self._mark(place, "host_full")

    def _remember_repository(self, place, raw):
        try:
            remote = _accept_remote_path(raw)
            if place["ssh"] is not None:
                origin = _ssh_text(
                    place["ssh"], ["git", "-C", remote, "remote", "get-url", "origin"]
                )
            else:
                origin = _stdout(_git(remote, "remote", "get-url", "origin"))
            if _origin_repository(origin) != self.repository:
                raise PollError("CLONE_MISMATCH", "worker repository must be the polled repository")
        except PollError:
            if place["skip"] is None:
                self._mark(place, "repository")
            return
        place["remote_repository"] = remote

    def _count_active(self, place):
        total = 0
        for state in ("queued", "running"):
            listed = place["caller"]("run.list", {"state": state, "limit": place["cap"]})
            runs = listed.get("runs") if isinstance(listed, dict) else None
            if not isinstance(runs, list):
                raise PollError("WORKER_ERROR", "worker run list was not a page")
            total += len(runs)
        return total

    def _mark(self, place, reason):
        if place["skip"] is None or reason == "unreachable":
            place["skip"] = reason
        row = {"reason": reason, "host": place["name"]}
        if row not in self.result["skipped"]:
            self.result["skipped"].append(row)

    def _note_unknown_hosts(self):
        names = {place["name"] for place in self.places}
        seen = []
        for entry in self.state["submissions"]:
            host = entry.get("host")
            if host in names or host in seen:
                continue
            seen.append(host)
            self.result["skipped"].append({"reason": "host_unknown", "host": host})

    def _choose_place(self):
        for place in self.places:
            if place["skip"] == "host_full":
                self._mark(place, "host_full")
                continue
            if place["skip"] is not None or not place["reachable"]:
                continue
            if place["active"] >= place["cap"]:
                self._mark(place, "host_full")
                continue
            return place
        return None

    def _place_for(self, name):
        for place in self.places:
            if place["name"] == name:
                return place
        return None

    def _bind(self, place):
        self.caller = place["caller"]
        self.ssh_host = place["ssh"]
        self.remote_repository = place.get("remote_repository")

    def _place_checkout(self, sha):
        if self.ssh_host is not None:
            self._remote_checkout(sha)
            return
        remote = self.remote_repository
        if remote is None or _SHA.fullmatch(sha) is None:
            raise PollError("GIT_FAILED", "git command failed")
        try:
            same = Path(remote).resolve() == self.clone.resolve()
        except OSError:
            same = False
        if same:
            return
        pack = _git(
            self.clone,
            "pack-objects",
            "--revs",
            "--stdout",
            input_bytes=(sha + "\n").encode(),
        ).stdout
        _git(remote, "unpack-objects", "-q", input_bytes=pack)
        _checkout(remote, sha)

    def _expire_placed(self):
        for entry in self.state["submissions"]:
            if self._placed_caller(entry) is None or "run_id" not in entry:
                continue
            record = self.caller("run.get", {"run_id": entry["run_id"]})
            if record["state"] == "queued" and queue_expired(record.get("accepted_at"), self.clock):
                self.caller("run.cancel", {"version": 0, "run_id": entry["run_id"]})

    def _post_placed(self):
        for entry in self.state["submissions"]:
            if self.budget.stopped:
                return
            if self._placed_caller(entry) is None or "run_id" not in entry:
                continue
            self._post_record(entry)

    def _placed_caller(self, entry):
        place = self._place_for(entry.get("host"))
        if place is None or not place.get("reachable"):
            return None
        self._bind(place)
        return place

    def _checkpoint(self):
        _save_state(self.state_dir / "poll.json", self.state)

    def _object(self, path):
        """GET one JSON object. No credential is sent.

        A response whose rate-limit header reports nothing remaining is
        not applied. The caller retries that event on the next pass.
        """

        if self.budget.stopped:
            return None
        got = _get_json(self.api_base, path, None, self.list_token)
        self.budget.observe(got["remaining"])
        if got.get("exhausted") or self.budget.stopped:
            self.budget.stopped = True
            return None
        if not isinstance(got["body"], dict):
            raise PollError("API_REJECTED", "GitHub response is not an object")
        return got["body"]

    def _repository_body(self):
        if self.repository_body is not None:
            return self.repository_body
        body = self._object(f"/repos/{self.repository}")
        if body is None:
            return None
        self.repository_body = body
        return body

    def _commit_body(self, sha):
        quoted = urllib.parse.quote(sha, safe="")
        return self._object(f"/repos/{self.repository}/commits/{quoted}")

    def _list(self, kind, path):
        """Return (body, etag) when a new list should be applied.

        A 304 keeps the stored validator. A list whose rate-limit header
        reports nothing remaining is left unapplied so the next pass
        requests it again.
        """

        if self.budget.stopped:
            return None
        listed = _get_json(self.api_base, path, self.state["etags"].get(kind), self.list_token)
        self.budget.observe(listed["remaining"])
        if listed.get("exhausted") or (listed["modified"] and self.budget.stopped):
            self.budget.stopped = True
            return None
        if not listed["modified"]:
            return None
        if not isinstance(listed["body"], list):
            raise PollError("API_REJECTED", "GitHub list response is not a list")
        return listed["body"], listed["etag"]

    def _branches(self):
        listed = self._list("branches", f"/repos/{self.repository}/branches?per_page={PAGE_SIZE}")
        if listed is None:
            return
        body, etag = listed
        for item in body:
            if not isinstance(item, dict):
                raise PollError("API_REJECTED", "GitHub branch list is not usable")
            name = item.get("name")
            commit = item.get("commit")
            sha = _sha(commit.get("sha") if isinstance(commit, dict) else None)
            ref = _ref_name(self.clone, "heads", name)
            if not self._push(ref, sha):
                return
        self.state["etags"]["branches"] = etag
        self._checkpoint()

    def _pulls(self):
        listed = self._list(
            "pulls", f"/repos/{self.repository}/pulls?state=open&per_page={PAGE_SIZE}"
        )
        if listed is None:
            return
        body, etag = listed
        for item in body:
            if not isinstance(item, dict):
                raise PollError("API_REJECTED", "GitHub pull request list is not usable")
            if not self._pull(item):
                return
        if self.signoff_seen and not self.signoff_present:
            self.result["skipped"].append({"reason": "signoff_absent"})
        self.state["etags"]["pulls"] = etag
        self._checkpoint()

    def _push(self, ref, sha):
        previous = self.state["tips"].get(ref)
        if previous == sha:
            return True
        fetched = _fetch_sha(self.clone, ref)
        if fetched != sha:
            raise PollError("GIT_FAILED", "fetched branch tip does not match the listed SHA")
        _checkout(self.clone, sha)
        repo = self._repository_body()
        if repo is None:
            return False
        commit = self._commit_body(sha)
        if commit is None:
            return False
        before = _ZERO if previous is None else previous
        changed = None if previous is None else _diff(self.clone, previous, sha)
        event = {
            "ref": ref,
            "before": before,
            "after": sha,
            "repository": _event_repository(self.repository, repo),
        }
        login = _login(commit.get("author"))
        if login is not None:
            event["commits"] = [{"author": {"login": login}}]
        if not self._submit_jobs(
            "push",
            ref,
            sha,
            sha,
            event,
            None,
            changed,
            previous is None,
        ):
            return False
        self.state["tips"][ref] = sha
        self._checkpoint()
        return True

    def _pull(self, item):
        number = item.get("number")
        if type(number) is not int or number < 1:
            raise PollError("API_REJECTED", "GitHub pull request list is not usable")
        head = item.get("head")
        base = item.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise PollError("API_REJECTED", "GitHub pull request list is not usable")
        head_sha = _sha(head.get("sha"))
        base_sha = _sha(base.get("sha"))
        head_ref = head.get("ref")
        base_ref = base.get("ref")
        if (
            not isinstance(head_ref, str)
            or not isinstance(base_ref, str)
            or head_ref == ""
            or base_ref == ""
        ):
            raise PollError("API_REJECTED", "GitHub pull request list is not usable")
        head_repo = head.get("repo")
        if not _same_repository(head, base):
            full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
            if not isinstance(full_name, str):
                full_name = None
            self._record_fork(number, head_sha, full_name)
            return True
        seen = self.state["pulls"].get(str(number))
        if isinstance(seen, dict) and seen.get("head") == head_sha and seen.get("base") == base_sha:
            return self._signoff(number, head_sha, base_sha, item)
        ref = f"refs/pull/{number}/merge"
        merge = _fetch_sha(self.clone, ref)
        if merge is None:
            self._record_absent(number, head_sha)
            return True
        _checkout(self.clone, merge)
        repo = self._repository_body()
        if repo is None:
            return False
        activity = "opened" if seen is None else "synchronize"
        changed = _diff(self.clone, base_sha, head_sha)
        pull_request = {
            "head": {
                "sha": head_sha,
                "ref": head_ref,
                "repo": _event_repo(head_repo),
            },
            "base": {
                "sha": base_sha,
                "ref": base_ref,
                "repo": _event_repo(base.get("repo")),
            },
        }
        login = _login(item.get("user"))
        if login is not None:
            pull_request["user"] = {"login": login}
        event = {
            "ref": ref,
            "before": base_sha,
            "after": merge,
            "repository": _event_repository(self.repository, repo),
            "number": number,
            "pull_request": pull_request,
        }
        submitted = self._submit_jobs(
            "pull_request", ref, merge, head_sha, event, activity, changed, False
        )
        if submitted:
            self.state["pulls"][str(number)] = {
                "head": head_sha,
                "base": base_sha,
                "merge": merge,
            }
            self._checkpoint()
        if not self._signoff(number, head_sha, base_sha, item):
            return False
        return submitted

    def _signoff(self, number, head_sha, base_sha, item):
        """Post rookrunner/signoff from the two CODEOWNERS blobs and the label."""

        if self.budget.stopped:
            return False
        names = _signoff_labels(item)
        head_text = _codeowners_blob(self.clone, head_sha)
        base_text = _codeowners_blob(self.clone, base_sha)
        self.signoff_seen = True
        if head_text is None and base_text is None:
            return True
        self.signoff_present = True
        gate = _signoff_gate()
        labeled = gate.LABEL in names
        patterns = []
        if head_text is not None:
            patterns.extend(gate.parse_patterns(head_text))
        if base_text is not None:
            patterns.extend(gate.parse_patterns(base_text))
        changed = _signoff_paths(self.clone, base_sha, head_sha)
        code, hits = gate.decision(changed, patterns, names)
        state = "failure" if code == 1 else "success"
        if code == 1:
            summary = "protected path changed without the sign-off label"
        elif hits:
            summary = "sign-off label present"
        else:
            summary = "no protected path changed"
        key = str(number)
        signoffs = self.state.get("signoffs")
        stored = signoffs.get(key) if isinstance(signoffs, dict) else None
        if (
            isinstance(stored, dict)
            and stored.get("head") == head_sha
            and stored.get("base") == base_sha
            and stored.get("labeled") == labeled
            and stored.get("state") == state
        ):
            return True
        posted, check_id = self._post_signoff(number, head_sha, state, summary, stored)
        if not posted:
            return False
        record = {"head": head_sha, "base": base_sha, "labeled": labeled, "state": state}
        if check_id is not None:
            record["check_run_id"] = check_id
        if not isinstance(self.state.get("signoffs"), dict):
            self.state["signoffs"] = {}
        self.state["signoffs"][key] = record
        self._checkpoint()
        self.result["statuses"].append(
            {
                "context": _SIGNOFF_CONTEXT,
                "state": state,
                "action": "posted",
                "sha": head_sha,
            }
        )
        return True

    def _post_signoff(self, number, sha, state, summary, stored):
        """Post the sign-off status. There is no worker run to record."""

        if self.budget.stopped:
            return False, None
        described = self.caller("worker.describe", {})
        root = self._credential_repository(described)
        if self.app_key:
            return self._post_signoff_app(number, sha, state, summary, stored, root)
        try:
            token = read_credential(self.credential_file, self.state_dir, root)
            try:
                remaining = post_status(
                    self.api_base,
                    self.repository,
                    sha,
                    state,
                    _SIGNOFF_CONTEXT,
                    token,
                )
            finally:
                token = None
        except StatusError as error:
            if error.kind == "RATE_LIMITED" and error.retryable:
                self.budget.stopped = True
                return False, None
            raise
        self.budget.observe(remaining)
        return True, None

    def _post_signoff_app(self, number, sha, state, summary, stored, root):
        check_run_id = None
        if isinstance(stored, dict) and is_integer(stored.get("check_run_id")):
            check_run_id = int(stored["check_run_id"])
        conclusion = "success" if state == "success" else "failure"
        try:
            posted = post_check_flow(
                api_base=self.api_base,
                repository=self.repository,
                sha=sha,
                context=_SIGNOFF_CONTEXT,
                run_id=f"signoff-{number}",
                check_status="completed",
                check_conclusion=conclusion,
                check_summary_text=summary,
                check_run_id=check_run_id,
                status_state=state,
                post_status_request=True,
                app_key=self.app_key,
                state_dir=self.state_dir,
                repository_root=root,
                clock=self.clock,
            )
        except StatusError as error:
            if error.kind == "RATE_LIMITED" and error.retryable:
                self.budget.stopped = True
                return False, None
            raise
        if (
            posted.error is not None
            and posted.error.kind == "RATE_LIMITED"
            and posted.error.retryable
        ):
            self.budget.stopped = True
            return False, None
        if posted.error is not None:
            raise posted.error
        if posted.status_posted:
            self.budget.observe(posted.status_remaining)
        return True, posted.check_id

    def _record_fork(self, number, head_sha, full_name):
        row = {"number": number, "head_sha": head_sha, "repository": full_name}
        if row not in self.state["forks"]:
            self.state["forks"].append(row)
            self.result["skipped"].append({"reason": "fork", "number": number})
            self._checkpoint()

    def _record_absent(self, number, head_sha):
        row = {"number": number, "head_sha": head_sha}
        if row not in self.state["merge_absent"]:
            self.state["merge_absent"].append(row)
            self.result["skipped"].append({"reason": "merge_ref_absent", "number": number})
            self._checkpoint()

    def _submit_jobs(self, event_name, ref, tested, status_sha, event, activity, changed, first):
        """Submit each configured job. Return false when a pending post did not finish."""

        if self.places is not None:
            return self._submit_placed(
                event_name, ref, tested, status_sha, event, activity, changed, first
            )
        for workflow, job_id in self.jobs:
            key = submission_key(self.repository, event_name, tested, workflow, job_id)
            existing = next(
                (item for item in self.state["submissions"] if item.get("key") == key), None
            )
            if existing is not None and not self._claims_this_host(existing):
                continue
            if existing is not None and "run_id" in existing:
                if not existing["pending"]:
                    if not self._post_pending(existing):
                        return False
                    existing["pending"] = True
                    self._checkpoint()
                    self.result["submitted"].append(
                        {"event": event_name, "ref": ref, "run_id": existing["run_id"]}
                    )
                continue
            params = {
                "version": 1,
                "submission_key": key,
                "workflow": workflow,
                "job_id": job_id,
                "event": event,
                "event_name": event_name,
            }
            if self.image is not None:
                params["image"] = self.image
            if activity is not None:
                params["activity_type"] = activity
            if first or changed is None:
                params["diff_unavailable"] = True
            else:
                files, commits = changed
                params["changed_files"] = files
                if event_name == "push":
                    params["commit_count"] = commits
                    if commits > COMMIT_PATH_LIMIT:
                        params.pop("changed_files", None)
            claimed = existing
            if claimed is None and self.ssh_host is not None:
                claimed = {
                    "key": key,
                    "host": self.ssh_host,
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": f"rookrunner/{Path(workflow).name}/{job_id}",
                    "pending": False,
                }
                self.state["submissions"].append(claimed)
                self._checkpoint()
            if self.ssh_host is not None:
                self._remote_checkout(tested)
            try:
                submitted = self.caller("run.submit", params)
            except PollError as exc:
                if exc.kind == "INVALID_PARAMS" and str(exc) == _WORKFLOW_ABSENT:
                    if claimed is not None and "run_id" not in claimed:
                        self._drop_unstarted(claimed)
                    self.result["skipped"].append(
                        {
                            "reason": "workflow_absent",
                            "event": event_name,
                            "ref": ref,
                            "workflow": workflow,
                            "job_id": job_id,
                        }
                    )
                    continue
                raise
            if submitted.get("triggered") is False and "run_id" not in submitted:
                if claimed is not None and "run_id" not in claimed:
                    self._drop_unstarted(claimed)
                self.result["submitted"].append(
                    {"event": event_name, "ref": ref, "triggered": False}
                )
                continue
            if claimed is None:
                claimed = {
                    "key": key,
                    "run_id": submitted["run_id"],
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": f"rookrunner/{Path(workflow).name}/{job_id}",
                    "pending": False,
                }
                self.state["submissions"].append(claimed)
            else:
                claimed["run_id"] = submitted["run_id"]
            entry = claimed
            self._checkpoint()
            if not self._post_pending(entry):
                return False
            entry["pending"] = True
            self._checkpoint()
            self.result["submitted"].append(
                {"event": event_name, "ref": ref, "run_id": submitted["run_id"]}
            )
        return not self.budget.stopped

    def _submit_placed(self, event_name, ref, tested, status_sha, event, activity, changed, first):
        """Place each new job. A recorded place is submitted again on that place."""

        for workflow, job_id in self.jobs:
            key = submission_key(self.repository, event_name, tested, workflow, job_id)
            existing = next(
                (item for item in self.state["submissions"] if item.get("key") == key), None
            )
            if existing is not None:
                place = self._place_for(existing.get("host"))
                if place is None or not place.get("reachable"):
                    continue
                if "run_id" in existing:
                    self._bind(place)
                    if not existing["pending"]:
                        if not self._post_pending(existing):
                            return False
                        existing["pending"] = True
                        self._checkpoint()
                        self.result["submitted"].append(
                            {
                                "event": event_name,
                                "ref": ref,
                                "run_id": existing["run_id"],
                                "host": place["name"],
                            }
                        )
                    continue
                if place.get("skip") not in (None, "host_full"):
                    continue
                claimed = existing
            else:
                place = self._choose_place()
                if place is None:
                    self.result["skipped"].append(
                        {
                            "reason": "no_host",
                            "event": event_name,
                            "ref": ref,
                            "workflow": workflow,
                            "job_id": job_id,
                        }
                    )
                    continue
                claimed = {
                    "key": key,
                    "host": place["name"],
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": f"rookrunner/{Path(workflow).name}/{job_id}",
                    "pending": False,
                }
                self.state["submissions"].append(claimed)
                self._checkpoint()
            self._bind(place)
            params = {
                "version": 1,
                "submission_key": key,
                "workflow": workflow,
                "job_id": job_id,
                "event": event,
                "event_name": event_name,
            }
            if self.image is not None:
                params["image"] = self.image
            if activity is not None:
                params["activity_type"] = activity
            if first or changed is None:
                params["diff_unavailable"] = True
            else:
                files, commits = changed
                params["changed_files"] = files
                if event_name == "push":
                    params["commit_count"] = commits
                    if commits > COMMIT_PATH_LIMIT:
                        params.pop("changed_files", None)
            self._place_checkout(tested)
            try:
                submitted = self.caller("run.submit", params)
            except PollError as exc:
                if exc.kind == "INVALID_PARAMS" and str(exc) == _WORKFLOW_ABSENT:
                    if "run_id" not in claimed:
                        self._drop_unstarted(claimed)
                    self.result["skipped"].append(
                        {
                            "reason": "workflow_absent",
                            "event": event_name,
                            "ref": ref,
                            "workflow": workflow,
                            "job_id": job_id,
                        }
                    )
                    continue
                raise
            if submitted.get("triggered") is False and "run_id" not in submitted:
                if "run_id" not in claimed:
                    self._drop_unstarted(claimed)
                self.result["submitted"].append(
                    {"event": event_name, "ref": ref, "triggered": False, "host": place["name"]}
                )
                continue
            claimed["run_id"] = submitted["run_id"]
            place["active"] += 1
            if place["active"] >= place["cap"]:
                place["skip"] = "host_full"
            self._checkpoint()
            if not self._post_pending(claimed):
                return False
            claimed["pending"] = True
            self._checkpoint()
            self.result["submitted"].append(
                {
                    "event": event_name,
                    "ref": ref,
                    "run_id": submitted["run_id"],
                    "host": place["name"],
                }
            )
        return not self.budget.stopped

    def _claims_this_host(self, entry):
        return entry.get("host") == self.ssh_host

    def _drop_unstarted(self, entry):
        self.state["submissions"] = [
            item for item in self.state["submissions"] if item is not entry
        ]
        self._checkpoint()

    def _remote_checkout(self, sha):
        if self.remote_repository is None or _SHA.fullmatch(sha) is None:
            raise PollError("GIT_FAILED", "git command failed")
        pack = _git(
            self.clone,
            "pack-objects",
            "--revs",
            "--stdout",
            input_bytes=(sha + "\n").encode(),
        ).stdout
        remote = self.remote_repository
        host = self.ssh_host
        _ssh_run(host, ["git", "-C", remote, "unpack-objects", "-q"], input_bytes=pack)
        _ssh_run(host, ["git", "-C", remote, "checkout", "--detach", "--force", "--quiet", sha])
        _ssh_run(host, ["git", "-C", remote, "clean", "-fd", "--quiet"])
        head = _ssh_text(host, ["git", "-C", remote, "rev-parse", "HEAD"]).strip()
        porcelain = _ssh_run(host, ["git", "-C", remote, "status", "--porcelain"])
        if head != sha or porcelain != b"":
            raise PollError(
                "GIT_FAILED", "dedicated clone is not a clean checkout of the fetched SHA"
            )

    def _expire_queued(self):
        for entry in self.state["submissions"]:
            if not self._claims_this_host(entry) or "run_id" not in entry:
                continue
            record = self.caller("run.get", {"run_id": entry["run_id"]})
            if record["state"] == "queued" and queue_expired(record.get("accepted_at"), self.clock):
                self.caller("run.cancel", {"version": 0, "run_id": entry["run_id"]})

    def _post_terminal(self):
        for entry in self.state["submissions"]:
            if self.budget.stopped:
                return
            if not self._claims_this_host(entry) or "run_id" not in entry:
                continue
            self._post_record(entry)

    def _post_record(self, entry):
        record = self.caller("run.get", {"run_id": entry["run_id"]})
        mapped = github_state(record["state"], record["exit_code"])
        if mapped == "pending":
            return
        params = {
            "run_id": entry["run_id"],
            "tested_commit": entry["tested_commit"],
            "status_sha": entry["status_sha"],
            "context": entry["context"],
        }
        if self.app_key:
            params["checks"] = True
        decision = self.caller("run.status", params)
        if decision["action"] == "skip":
            self.result["statuses"].append(
                {"run_id": entry["run_id"], "state": decision["state"], "action": "skip"}
            )
            return
        if decision["action"] != "post":
            raise PollError("WORKER_ERROR", "worker status decision was not post or skip")
        if self._post(entry, decision["state"], decision if self.app_key else None):
            self.result["statuses"].append(
                {"run_id": entry["run_id"], "state": decision["state"], "action": "posted"}
            )

    def _status_params(self, entry):
        return {
            "run_id": entry["run_id"],
            "tested_commit": entry["tested_commit"],
            "status_sha": entry["status_sha"],
            "context": entry["context"],
        }

    def _post_pending(self, entry):
        if not self.app_key:
            return self._post(entry, "pending")
        decision = self.caller("run.status", {**self._status_params(entry), "checks": True})
        if decision["action"] == "skip":
            return True
        if decision["action"] != "post":
            raise PollError("WORKER_ERROR", "worker status decision was not post or skip")
        return self._post(entry, decision["state"], decision)

    def _post(self, entry, state, decision=None):
        if self.budget.stopped:
            return False
        if self.app_key:
            return self._post_app(entry, state, decision)
        described = self.caller("worker.describe", {})
        try:
            token = read_credential(
                self.credential_file, self.state_dir, self._credential_repository(described)
            )
            try:
                remaining = post_status(
                    self.api_base,
                    self.repository,
                    entry["status_sha"],
                    state,
                    entry["context"],
                    token,
                )
            finally:
                token = None
        except StatusError as error:
            if error.kind == "RATE_LIMITED" and error.retryable:
                self.budget.stopped = True
                return False
            raise
        self.caller(
            "run.status",
            {
                "run_id": entry["run_id"],
                "tested_commit": entry["tested_commit"],
                "status_sha": entry["status_sha"],
                "context": entry["context"],
                "record": state,
            },
        )
        self.budget.observe(remaining)
        return True

    def _post_app(self, entry, state, decision):
        if not isinstance(decision, dict):
            raise PollError("WORKER_ERROR", "worker status decision was not post or skip")
        described = self.caller("worker.describe", {})
        try:
            posted = post_check_flow(
                api_base=self.api_base,
                repository=self.repository,
                sha=entry["status_sha"],
                context=entry["context"],
                run_id=entry["run_id"],
                check_status=decision["check_status"],
                check_conclusion=decision.get("check_conclusion"),
                check_summary_text=decision["check_summary"],
                check_run_id=decision.get("check_run_id"),
                status_state=state,
                post_status_request=decision.get("status_recorded") is not True,
                app_key=self.app_key,
                state_dir=self.state_dir,
                repository_root=self._credential_repository(described),
            )
        except StatusError as error:
            if error.kind == "RATE_LIMITED" and error.retryable:
                self.budget.stopped = True
                return False
            raise
        self._record_app(entry, state, decision, posted)
        if (
            posted.error is not None
            and posted.error.kind == "RATE_LIMITED"
            and posted.error.retryable
        ):
            self.budget.stopped = True
            return False
        if posted.error is not None:
            raise posted.error
        if posted.status_posted:
            self.budget.observe(posted.status_remaining)
        return True

    def _credential_repository(self, described):
        if self.ssh_host is not None:
            return str(self.clone)
        return described["repository"]

    def _record_app(self, entry, state, decision, posted):
        if posted.check_id is None and not posted.status_posted:
            return
        params = self._status_params(entry)
        if posted.status_posted:
            params["record"] = state
        if posted.check_id is not None:
            params["check_run_id"] = posted.check_id
            params["check_status"] = decision["check_status"]
            if decision.get("check_conclusion") is not None:
                params["check_conclusion"] = decision["check_conclusion"]
        self.caller("run.status", params)


def _empty_state(repository):
    return {
        "version": 1,
        "repository": repository,
        "etags": {},
        "tips": {},
        "pulls": {},
        "submissions": [],
        "forks": [],
        "merge_absent": [],
    }


def _load_state(path, repository):
    if not path.exists():
        return _empty_state(repository)
    if path.is_symlink():
        raise PollError("STATE_INVALID", "poll state must not be a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PollError("STATE_INVALID", "poll state could not be read") from None
    try:
        blob = os.read(fd, _MAX_BODY + 1)
    finally:
        os.close(fd)
    if len(blob) > _MAX_BODY:
        raise PollError("STATE_INVALID", "poll state could not be read")
    try:
        parsed = strict_json(blob)
    except (ValueError, UnicodeError, RecursionError):
        raise PollError("STATE_INVALID", "poll state could not be read") from None
    if not isinstance(parsed, dict) or parsed.get("version") != 1:
        raise PollError("STATE_INVALID", "poll state could not be read")
    if parsed.get("repository") != repository:
        raise PollError("STATE_INVALID", "poll state is bound to another repository")
    return parsed


def _save_state(path, payload):
    if path.is_symlink():
        raise PollError("STATE_INVALID", "poll state must not be a symlink")
    temporary = path.with_name(".poll.json.preparing")
    if temporary.is_symlink():
        raise PollError("STATE_INVALID", "poll state must not be a symlink")
    blob = (canonical(payload) + "\n").encode()
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise PollError("STATE_INVALID", "poll state could not be written") from None
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def poll_once(
    *,
    repository,
    clone,
    jobs,
    image,
    credential_file,
    api_base,
    state,
    caller,
    clock=None,
    app_key=None,
    list_token=None,
    ssh_host=None,
    places=None,
):
    """Run one pass and return its summary. The caller talks to the worker."""

    if clock is None:
        clock = datetime.now(UTC)
    return Pass(
        repository=repository,
        clone=clone,
        jobs=jobs,
        image=image,
        credential_file=credential_file,
        api_base=api_base,
        state=state,
        caller=caller,
        clock=clock,
        app_key=app_key,
        list_token=list_token,
        ssh_host=ssh_host,
        places=places,
    ).run()
