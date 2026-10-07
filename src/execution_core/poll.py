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
- The submission key is ``poll-`` plus the SHA-256 of the repository,
  event, tested SHA, workflow, and job.
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
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, UTC

from .checks import post_check_flow
from .protocol import canonical, is_integer, strict_json
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
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_REPOSITORY = re.compile(rf"{_NAME.pattern}/{_NAME.pattern}")


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


def _git(clone, *args, check=True):
    try:
        result = subprocess.run(
            ["git", "-C", str(clone), *args],
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


def _fetch_sha(clone, ref):
    result = _git(clone, "fetch", "--quiet", "origin", ref, check=False)
    if result.returncode != 0:
        return None
    sha = _stdout(_git(clone, "rev-parse", "FETCH_HEAD")).strip()
    if _SHA.fullmatch(sha) is None:
        raise PollError("GIT_FAILED", "git fetch did not resolve a commit")
    return sha


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
        self.budget = _Budget()
        self.state = None
        self.lock_fd = None
        self.repository_body = None
        self.result = {"stopped": False, "submitted": [], "statuses": [], "skipped": []}

    def run(self):
        if not self.jobs:
            raise PollError("INVALID_PARAMS", "poll requires at least one workflow job")
        if self.app_key and self.credential_file:
            raise PollError("INVALID_PARAMS", "one post accepts one credential")
        if not self.app_key and not self.credential_file:
            raise PollError("INVALID_PARAMS", "one post needs one credential")
        self._lock()
        try:
            described = self.caller("worker.describe", {})
            if Path(described["repository"]).resolve() != self.clone.resolve():
                raise PollError("CLONE_MISMATCH", "dedicated clone must be the worker repository")
            self.state = _load_state(self.state_dir / "poll.json", self.repository)
            self._expire_queued()
            self._branches()
            self._pulls()
            self._post_terminal()
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
        if seen and seen.get("head") == head_sha and seen.get("base") == base_sha:
            return True
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
        if not self._submit_jobs(
            "pull_request", ref, merge, head_sha, event, activity, changed, False
        ):
            return False
        self.state["pulls"][str(number)] = {"head": head_sha, "base": base_sha, "merge": merge}
        self._checkpoint()
        return True

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

        for workflow, job_id in self.jobs:
            key = submission_key(self.repository, event_name, tested, workflow, job_id)
            existing = next(
                (item for item in self.state["submissions"] if item.get("key") == key), None
            )
            if existing is not None:
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
            try:
                submitted = self.caller("run.submit", params)
            except PollError as exc:
                if exc.kind == "INVALID_PARAMS" and str(exc) == _WORKFLOW_ABSENT:
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
                self.result["submitted"].append(
                    {"event": event_name, "ref": ref, "triggered": False}
                )
                continue
            entry = {
                "key": key,
                "run_id": submitted["run_id"],
                "event": event_name,
                "tested_commit": tested,
                "status_sha": status_sha,
                "context": f"rookrunner/{Path(workflow).name}/{job_id}",
                "pending": False,
            }
            self.state["submissions"].append(entry)
            self._checkpoint()
            if not self._post_pending(entry):
                return False
            entry["pending"] = True
            self._checkpoint()
            self.result["submitted"].append(
                {"event": event_name, "ref": ref, "run_id": submitted["run_id"]}
            )
        return not self.budget.stopped

    def _expire_queued(self):
        for entry in self.state["submissions"]:
            record = self.caller("run.get", {"run_id": entry["run_id"]})
            if record["state"] == "queued" and queue_expired(record.get("accepted_at"), self.clock):
                self.caller("run.cancel", {"version": 0, "run_id": entry["run_id"]})

    def _post_terminal(self):
        for entry in self.state["submissions"]:
            if self.budget.stopped:
                return
            record = self.caller("run.get", {"run_id": entry["run_id"]})
            mapped = github_state(record["state"], record["exit_code"])
            if mapped == "pending":
                continue
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
                continue
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
            token = read_credential(self.credential_file, self.state_dir, described["repository"])
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
                repository_root=described["repository"],
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
    ).run()
