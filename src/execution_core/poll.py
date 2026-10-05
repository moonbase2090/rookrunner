"""One poll pass for one configured repository.

The operating system's scheduler starts this command. The pass lists
branch heads and open pull requests, fetches into the dedicated clone,
submits each new SHA, and posts commit statuses. It then exits. It is
not a resident service, a listener, or a runner registration.

List requests are conditional and do not send the credential. The
credential is read only when a status is posted, which is the NS-40
rule. A response whose rate-limit header reports nothing remaining is
not applied, and the pass makes no further HTTP request. The next pass
starts from the last saved checkpoint.

Local rules, not GitHub-equivalence claims:

- The first observation of a branch uses forty ``0`` characters as
  ``before`` and reports the diff unavailable.
- A later push diffs the stored tip against the new tip.
- A pull request's activity is ``opened`` the first time it is seen
  and ``synchronize`` when its head or merge SHA changes.
- A missing ``refs/pull/<number>/merge`` is recorded and runs nothing.
- A fork is recorded and runs nothing.
- The submission key is ``poll-`` plus the SHA-256 of the repository,
  event, tested SHA, workflow, and job.
- A queued run is cancelled 24 hours after acceptance and reported as
  ``error``. An older SHA is not cancelled because a newer push arrived.
- This pass lists branches and open pull requests. It does not list tags.

https://docs.github.com/en/actions/reference/limits
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
https://docs.github.com/en/rest/commits/statuses
"""

import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .protocol import canonical, strict_json
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


def _get_json(api_base, path, etag):
    """GET one list. No credential is sent. 304 returns a null body."""

    # status_url validates the origin. The SHA is discarded with the path.
    origin = status_url(api_base, "owner/name", "ab" * 20).rsplit("/repos/", 1)[0]
    url = origin + path
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
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
        try:
            error.read(256)
        except OSError:
            pass
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
        self, *, repository, clone, jobs, image, credential_file, api_base, state, caller, clock
    ):
        self.repository = _repository_name(repository)
        self.clone = Path(clone)
        self.jobs = list(jobs)
        self.image = image
        self.credential_file = credential_file
        self.api_base = api_base
        self.state_dir = Path(state)
        self.caller = caller
        self.clock = clock
        self.budget = _Budget()
        self.state = None
        self.lock_fd = None
        self.result = {"stopped": False, "submitted": [], "statuses": [], "skipped": []}

    def run(self):
        if not self.jobs:
            raise PollError("INVALID_PARAMS", "poll requires at least one workflow job")
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

    def _list(self, kind, path):
        """Return (body, etag) when a new list should be applied.

        A 304 keeps the stored validator. A list whose rate-limit header
        reports nothing remaining is left unapplied so the next pass
        requests it again.
        """

        if self.budget.stopped:
            return None
        listed = _get_json(self.api_base, path, self.state["etags"].get(kind))
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
        before = _ZERO if previous is None else previous
        changed = None if previous is None else _diff(self.clone, previous, sha)
        event = {
            "ref": ref,
            "before": before,
            "after": sha,
            "repository": {"full_name": self.repository},
        }
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
        repo = head.get("repo")
        full_name = repo.get("full_name") if isinstance(repo, dict) else None
        if full_name != self.repository:
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
        activity = "opened" if seen is None else "synchronize"
        changed = _diff(self.clone, base_sha, head_sha)
        event = {
            "ref": ref,
            "before": base_sha,
            "after": merge,
            "repository": {"full_name": self.repository},
            "number": number,
            "pull_request": {
                "head": {
                    "sha": head_sha,
                    "ref": head_ref,
                    "repo": {"full_name": full_name},
                },
                "base": {"sha": base_sha, "ref": base_ref},
            },
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
                    if not self._post(existing, "pending"):
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
            submitted = self.caller("run.submit", params)
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
            if not self._post(entry, "pending"):
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
            decision = self.caller(
                "run.status",
                {
                    "run_id": entry["run_id"],
                    "tested_commit": entry["tested_commit"],
                    "status_sha": entry["status_sha"],
                    "context": entry["context"],
                },
            )
            if decision["action"] == "skip":
                self.result["statuses"].append(
                    {"run_id": entry["run_id"], "state": decision["state"], "action": "skip"}
                )
                continue
            if decision["action"] != "post":
                raise PollError("WORKER_ERROR", "worker status decision was not post or skip")
            if self._post(entry, decision["state"]):
                self.result["statuses"].append(
                    {"run_id": entry["run_id"], "state": decision["state"], "action": "posted"}
                )

    def _post(self, entry, state):
        if self.budget.stopped:
            return False
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
):
    """Run one pass and return its summary. The caller talks to the worker."""

    if clock is None:
        clock = datetime.now(timezone.utc)
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
    ).run()
