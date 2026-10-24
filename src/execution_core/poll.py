# SPDX-License-Identifier: MIT

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
- A fork, a head repository other than the owner repository, or
  untrusted code is refused with a warning that names the reason. It
  is not silently skipped. ``--allow-untrusted SHA`` runs that one
  pull-request head or push tip and the run records the override. The
  flag is not stored and it is not the default. The head and base
  repository ids match only when both are integers and equal. A null
  head repository is a fork. The full name is not the comparison.
- A pull request's author association must be OWNER, MEMBER, or
  COLLABORATOR. A missing association, and one GitHub does not list,
  is refused.
- A pushed tip commit, and every commit on a pull request, needs an
  author and a committer with write or admin permission on the
  repository, as ``/collaborators/{login}/permission`` reports it. A
  commit identity without a GitHub account is refused. So is a
  permission request that GitHub answers with 401, 403, or 404.
  Without ``--app-key`` that request carries no credential and GitHub
  answers 401. The committer ``web-flow`` passes only when GitHub
  verified the commit signature, and the author is still checked.
  GitHub's commit list for a pull request must name exactly the
  commits the head has and the base does not reach in the clone. It
  lists at most 250 commits, and a longer pull request is refused.
- A pull request that changes a workflow file is refused with a
  warning that names the path. A workflow file is a configured job
  path, or a file directly in ``.github/workflows/`` whose name ends
  in ``.yml`` or ``.yaml``. A nested path does not count. The diff is
  the pull base against the head. When that diff cannot be read, the
  pull request is refused. The fetched merge commit is checked out
  only when its first parent is the listed base and its second parent
  is the listed head. Any other commit is left unchecked and creates
  no run. ``--allow-pr-workflow ASSOCIATION`` runs
  that pull request when the author association is the named
  ``OWNER``, ``MEMBER``, or ``COLLABORATOR``. With the flag, any other
  listed association on a same-repository pull request runs when
  ``/collaborators/{login}/permission`` reports the author as write,
  maintain, or admin, since GitHub reports owners and members to an
  app as ``CONTRIBUTOR``. A fork and a missing association never match.
  A refusal from that lookup is not stored, so each pass asks again.
  The flag is not stored and it is not the default.
- A refused same-repository pull request gets a ``rookrunner/trust``
  failure status on the head SHA with the reason, once per head and
  reason. When a later pass lets that head run, the status turns to
  success. A fork and a merge-commit mismatch post nothing.
- A repository with ``.github/CODEOWNERS`` at the pull base or head
  gets a ``rookrunner/signoff`` status on the head SHA. Patterns are
  the union of the two blobs. A protected path without
  ``mb2090-signoff`` is a failure. No CODEOWNERS blob is skipped and
  recorded. The pass does not apply the label and does not run
  ``signoff.yml``.
- A terminal run whose snapshot is not readable posts an ``error``
  status with the reason as its description, in token mode. The
  submission stays, and a later pass tries that run again. The pass
  still posts every later run. App-key mode keeps the skip. Any other
  status refusal stops the pass.
- After posts and before the final state save, the pass calls
  ``poll.record`` on the single caller and on each place whose origin
  was accepted. ``PollError`` and ``OSError`` from that call are a
  ``poll_stamp`` skip. Checkpoints do not call it. A pass that raises
  before that call leaves the previous stamp.
- The submission key is ``poll-`` plus the SHA-256 of the repository,
  event, tested SHA, workflow, and job. A schedule run also hashes the
  due minute and the cron. Leaving that part off keeps the key the same.
- A schedule is read from workflow files on the default-branch tip.
  The first time a workflow is seen, the latest due minute is stored
  and nothing is submitted. A later pass submits at most that newest
  due minute. The minute and the tip SHA are stored before submit.
  A lost reply retries that SHA, including after the branch moves.
  The cursor is stored after the submit. The check context ends in
  ``/schedule``.
- ``dispatch`` is a separate command. This pass does not submit
  ``workflow_dispatch`` and it does not listen for that event.
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
from .cron import cron_matches, parse_cron, schedule_decision
from .plan import PlanError, workflow_job_ids, workflow_on
from .protocol import MAX_LIST_PAGE, Fault, canonical, is_integer, strict_json
from .status import (
    StatusError,
    github_state,
    header_remaining,
    post_status,
    read_credential,
    status_url,
)
from .trigger import (
    COMMIT_PATH_LIMIT,
    MAX_CHANGED_FILES,
    schedule_expressions,
    submission_triggered,
)

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
# The worker's plan error when the workflow at that commit has no such job.
_JOB_ABSENT = "no selected job"
_SNAPSHOT_UNREADABLE = "snapshot is not readable"
_SIGNOFF_CONTEXT = "rookrunner/signoff"
_TRUST_CONTEXT = "rookrunner/trust"
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


def submission_key(repository, event, sha, workflow, job, tick=None):
    """Key built from the repository, event, tested SHA, workflow, and job.

    ``tick`` is appended only when the caller passes one. A schedule run
    passes the due minute and the cron. Push and pull-request keys omit it,
    and those keys stay the same as before.
    """

    parts = (repository, event, sha, workflow, job)
    if tick is not None:
        parts = (*parts, tick)
    material = "\0".join(parts).encode()
    return "poll-" + hashlib.sha256(material).hexdigest()


def _job_context(workflow, job_id, suffix):
    return f"rookrunner/{Path(workflow).name}/{job_id}{suffix}"


def _schedule_cursor(value):
    """Return a UTC minute from stored poll state, or None when it is absent."""

    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


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


_COLLABORATOR_ASSOCIATIONS = frozenset({"COLLABORATOR", "MEMBER", "OWNER"})
# Every author association GitHub lists. Another string is not recognized.
# https://docs.github.com/en/graphql/reference/enums#commentauthorassociation
_KNOWN_ASSOCIATIONS = _COLLABORATOR_ASSOCIATIONS | frozenset(
    {"CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE"}
)
# Repository permissions that can push. read, triage, and none cannot.
# https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user
_WRITE_PERMISSIONS = frozenset({"admin", "write"})
_PERMISSION_REFUSED = frozenset({401, 403, 404})
# GitHub commits a pull-request merge or a web edit as this account and
# signs it with its own key.
_WEB_FLOW_LOGIN = "web-flow"
_WEB_FLOW_ID = 19864447
# GitHub lists at most 250 commits for one pull request.
# https://docs.github.com/en/rest/pulls/pulls#list-commits-on-a-pull-request
_PULL_COMMIT_LIMIT = 250
_STOPPED = object()


def _association(value):
    """Return a non-empty association string, or None."""

    if not isinstance(value, str) or value == "":
        return None
    return value


def _absent_reason(exc, job_id):
    """Return the skip reason when a configured job cannot run at this commit.

    A workflow file that is not tracked is ``workflow_absent``. A workflow
    whose jobs do not include the configured job, as on a branch that
    predates that job, is ``job_absent``. A job the planner rejects is
    ``workflow_invalid`` when the rejection names that job by its path,
    ``jobs.<job_id>.`` or ``jobs.<job_id>:``. Each skips that one job, and
    the pass continues. Any other error is None and still stops the pass.
    """

    if exc.kind != "INVALID_PARAMS":
        return None
    text = str(exc)
    if text == _WORKFLOW_ABSENT:
        return "workflow_absent"
    if text == _JOB_ABSENT:
        return "job_absent"
    prefix = f"jobs.{job_id}"
    if text.startswith((prefix + ".", prefix + ":")):
        return "workflow_invalid"
    return None


def untrusted_reason(head, base, author_association):
    """Return why this pull request is untrusted, or None when it may run.

    The repository comparison is ``_same_repository``. When that is false
    and both ids are integers, the head repository is not the owner
    repository. Any other mismatch is a fork. A missing association and
    one GitHub does not list are reasons. OWNER, MEMBER, and COLLABORATOR
    are collaborators.

    A same-repository pull request with any other listed association is
    not refused here. GitHub reports an owner or member as CONTRIBUTOR
    when the app cannot read organization membership, and an app author
    as NONE. The head branch is in this repository, and every commit on
    it still goes through ``_commit_reason``.
    """

    if (head is not None or base is not None) and not _same_repository(head, base):
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        head_id = _integer_id(head_repo.get("id")) if isinstance(head_repo, dict) else None
        base_id = _integer_id(base_repo.get("id")) if isinstance(base_repo, dict) else None
        if head_id is not None and base_id is not None and head_id != base_id:
            return "head repository is not the owner repository"
        return "pull request is from a fork"
    association = _association(author_association)
    if association is None:
        return "author association is missing"
    if association not in _KNOWN_ASSOCIATIONS:
        return "author association is not recognized"
    if association not in _COLLABORATOR_ASSOCIATIONS and not _same_repository(head, base):
        return "author is not a repository collaborator"
    return None


def _app_user(user):
    """Return whether GitHub reports this account as a GitHub App bot.

    Only GitHub's ``type`` field is read. A login that merely ends in
    ``[bot]`` or ``-bot`` is not an app.
    """

    return isinstance(user, dict) and _login(user) is not None and user.get("type") == "Bot"


def _github_committed(user, commit):
    """Return whether GitHub itself committed and signed this commit.

    The committer must be the ``web-flow`` account by login and id, and
    GitHub must report the signature as verified.
    """

    if _login(user) != _WEB_FLOW_LOGIN or _integer_id(user.get("id")) != _WEB_FLOW_ID:
        return False
    detail = commit.get("commit") if isinstance(commit, dict) else None
    verification = detail.get("verification") if isinstance(detail, dict) else None
    return isinstance(verification, dict) and verification.get("verified") is True


def _allow_untrusted(values):
    """Return the SHA set for one invocation. It is not read from state."""

    if values is None:
        values = ()
    if not isinstance(values, list | tuple):
        raise PollError("INVALID_PARAMS", "allow-untrusted must be a list of commit SHAs")
    allowed = []
    for item in values:
        if not isinstance(item, str) or _SHA.fullmatch(item) is None:
            raise PollError("INVALID_PARAMS", "allow-untrusted must be a commit SHA")
        allowed.append(item)
    return frozenset(allowed)


_PR_WORKFLOW_ASSOCIATIONS = frozenset({"COLLABORATOR", "MEMBER", "OWNER"})
# Repository permissions that let an author with another association change
# a workflow file under --allow-pr-workflow. GitHub reports maintain as write
# in the legacy permission field; both are accepted.
_PR_WORKFLOW_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_WORKFLOW_DIR = ".github/workflows/"
_WORKFLOW_PATH_LIMIT = 5


def _allow_pr_workflow(values):
    """Return the association set for one invocation. It is not read from state."""

    if values is None:
        values = ()
    if not isinstance(values, list | tuple):
        raise PollError("INVALID_PARAMS", "allow-pr-workflow association is not accepted")
    allowed = []
    for item in values:
        if item not in _PR_WORKFLOW_ASSOCIATIONS:
            raise PollError("INVALID_PARAMS", "allow-pr-workflow association is not accepted")
        allowed.append(item)
    return frozenset(allowed)


def _workflow_definition(path, configured):
    """A configured job path, or a workflow file directly in the workflows directory."""

    if path in configured:
        return True
    if not path.startswith(_WORKFLOW_DIR):
        return False
    name = path[len(_WORKFLOW_DIR) :]
    if "/" in name or name == "":
        return False
    return name.endswith((".yml", ".yaml"))


def _changed_names(clone, before, after):
    """Return changed paths with both sides of a rename, or None."""

    files = _git(
        clone,
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
        before,
        after,
        check=False,
    )
    if files.returncode != 0:
        return None
    text = _stdout(files)
    names = [] if text == "" else text.split("\n")
    if names and names[-1] == "":
        names.pop()
    if len(names) > MAX_CHANGED_FILES:
        return None
    return names


def _show_paths(paths):
    shown = ", ".join(paths[:_WORKFLOW_PATH_LIMIT])
    if len(paths) > _WORKFLOW_PATH_LIMIT:
        shown += ", ..."
    return shown


def _workflow_message(kind, paths, association):
    if association is None and kind == "unavailable":
        return "pull request workflow diff is not available"
    if association is None:
        return f"pull request changes a workflow file: {_show_paths(paths)}"
    if kind == "unavailable":
        return (
            f"pull request workflow change allowed for {association}: "
            "workflow diff is not available"
        )
    return f"pull request workflow change allowed for {association}: {_show_paths(paths)}"


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
    """Return the authenticated actor login for eligibility, or None.

    No polled event carries one. A push carries none, and a pull
    request carries none: ``pull_request.user.login`` is the request
    author, but the poller cannot see which account opened the request
    or pushed its latest update — first observation of a pre-existing
    request is indistinguishable from a genuine opened event — so
    eligibility and claims fail closed instead of using the author as
    a stand-in.
    """

    if not isinstance(event, dict):
        return None
    return None


def _default_branch_run(event, event_name):
    """Return whether this push, schedule, or workflow_dispatch is on the default branch."""

    if event_name not in ("push", "schedule", "workflow_dispatch") or not isinstance(event, dict):
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
    only matches are a push, a schedule, or a workflow_dispatch event
    whose ``ref`` is ``refs/heads/`` plus ``repository.default_branch``.
    A listed ref matches that ``ref`` exactly. A listed login matches
    the actor login with ASCII case folding. That default-branch push,
    schedule, or workflow_dispatch stays a match when a list is
    non-empty. No polled event carries an actor login: neither a push
    nor a pull request (opened or synchronize) yields one, because the
    poller cannot verify which account triggered the run. A missing
    login does not match a pusher entry, so pusher entries only constrain
    events that carry a verified actor. An event that lacks these fields
    does not match the default rule. This does not read a secret.
    """

    if _default_branch_run(event, event_name):
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


def _get_json(api_base, path, etag, token=None, refused=frozenset()):
    """GET one list. 304 returns a null body.

    ``token`` is the installation token for an ``--app-key`` pass.
    The credential-file path passes none. A status in ``refused`` returns
    a null body and that status as ``refused`` instead of raising. A
    rate-limit stop is checked first.
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
        if code in refused:
            return {
                "modified": False,
                "body": None,
                "etag": None,
                "remaining": header_remaining(headers),
                "refused": code,
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


def _rev_list(clone, base, head):
    """Return the commits ``head`` has that ``base`` does not reach, or None."""

    if _SHA.fullmatch(base or "") is None or _SHA.fullmatch(head or "") is None:
        return None
    listed = _git(clone, "rev-list", f"{base}..{head}", check=False)
    if listed.returncode != 0:
        return None
    shas = [line for line in _stdout(listed).split("\n") if line != ""]
    if any(_SHA.fullmatch(sha) is None for sha in shas):
        return None
    return shas


def _merge_parents(clone, sha):
    """Return the two parents of one merge commit, or None."""

    listed = _git(clone, "rev-parse", f"{sha}^1", f"{sha}^2", check=False)
    if listed.returncode != 0:
        return None
    text = _stdout(listed)
    parents = [] if text == "" else text.split("\n")
    if parents and parents[-1] == "":
        parents.pop()
    if len(parents) != 2 or any(_SHA.fullmatch(parent) is None for parent in parents):
        return None
    return parents[0], parents[1]


def _moved_base_merge(clone, parents, head_sha, base_ref):
    """Return whether a merge is the head merged onto the base branch's current tip.

    GitHub rebuilds the merge ref when the base branch moves, so its first
    parent is the new tip while the listed base is still the old one. The
    second parent must be the head, and the first must be the tip fetched
    now. Any other merge is not accepted.
    """

    if parents is None or base_ref is None or parents[1] != head_sha:
        return False
    current = _fetch_sha(clone, f"refs/heads/{base_ref}")
    return current is not None and parents[0] == current


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
        allow_untrusted=(),
        allow_pr_workflow=(),
        key_dir=None,
        retry_failed=None,
    ):
        self.repository = _repository_name(repository)
        self.clone = Path(clone)
        self.jobs = list(jobs)
        self.image = image
        self.credential_file = credential_file
        self.app_key = app_key
        self.key_dir = key_dir
        self.list_token = list_token
        self.api_base = api_base
        self.state_dir = Path(state)
        self.caller = caller
        self.clock = clock
        self.ssh_host = ssh_host
        self.unplaced = False
        self.places = places
        self.retry_failed = retry_failed
        self.retry_entry = None
        self.retry_key = None
        self.retry_applied = False
        self.allow_untrusted = _allow_untrusted(allow_untrusted)
        self.allow_pr_workflow = _allow_pr_workflow(allow_pr_workflow)
        self.hold_branches = False
        self.hold_pulls = False
        self.remote_repository = None
        self.budget = _Budget()
        self.state = None
        self.lock_fd = None
        self.repository_body = None
        # Login to repository permission for this pass. None is unreadable.
        self.permissions = {}
        self.signoff_seen = False
        self.signoff_present = False
        self.result = {
            "stopped": False,
            "submitted": [],
            "statuses": [],
            "skipped": [],
            "warnings": [],
        }

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
            self._prepare_retry()
            self._branches()
            self._pulls()
            self._schedules()
            if self.places is None:
                self._post_terminal()
            else:
                self._post_placed()
            self.result["stopped"] = self.budget.stopped
            if self.retry_entry is not None and not self.retry_applied:
                self.result["skipped"].append(
                    {"reason": "retry_not_submitted", "key": self.retry_failed}
                )
            self._record_polls()
            _save_state(self.state_dir / "poll.json", self.state)
            return self.result
        finally:
            self._unlock()

    def _prepare_retry(self):
        if self.retry_failed is None:
            return
        if (
            not isinstance(self.retry_failed, str)
            or re.fullmatch(r"poll-[0-9a-f]{64}", self.retry_failed) is None
        ):
            raise PollError("INVALID_PARAMS", "retry key must be a poll submission key")
        entry = next(
            (row for row in self.state["submissions"] if row.get("key") == self.retry_failed), None
        )
        if (
            entry is None
            or "run_id" not in entry
            or entry.get("event") not in {"push", "pull_request"}
        ):
            raise PollError("INVALID_PARAMS", "retry requires a recorded push or pull-request run")
        if self.places is not None:
            if self._placed_caller(entry) is None:
                raise PollError("WORKER_ERROR", "retry worker is unavailable")
        elif not self._claims_this_host(entry):
            raise PollError("INVALID_PARAMS", "retry key belongs to another worker")
        record = self.caller("run.get", {"run_id": entry["run_id"]})
        if record.get("state") != "failed":
            raise PollError("INVALID_PARAMS", "only a failed run can be retried")
        self.retry_entry = entry
        self.retry_key = "poll-" + hashlib.sha256(("retry\0" + entry["key"]).encode()).hexdigest()
        self._post_record(entry)

    def _retry_matches(self, event, sha):
        return (
            self.retry_entry is not None
            and self.retry_entry["event"] == event
            and self.retry_entry["status_sha"] == sha
        )

    def _submission_key(self, base):
        entry = self.retry_entry
        if entry is None or base != entry.get("retry_base", entry["key"]):
            return base
        self.retry_applied = True
        return self.retry_key

    def _retry_metadata(self, key):
        entry = self.retry_entry
        if entry is None or key != self.retry_key:
            return {}
        return {"retry_of": entry["key"], "retry_base": entry.get("retry_base", entry["key"])}

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

    def _record_polls(self):
        """Stamp each worker reached in this pass. A refusal does not stop the save."""

        completed_at = self.clock.isoformat()
        if self.places is None:
            self._record_poll(self.caller, completed_at, None)
            return
        for place in self.places:
            if place.get("remote_repository") is None:
                continue
            self._record_poll(place["caller"], completed_at, place["name"])

    def _record_poll(self, caller, completed_at, host):
        try:
            caller(
                "poll.record",
                {"completed_at": completed_at, "repository": self.repository},
            )
        except (PollError, OSError):
            row = {"reason": "poll_stamp"}
            if host is not None:
                row["host"] = host
            self.result["skipped"].append(row)

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
        branch = body.get("default_branch")
        if isinstance(branch, str) and branch != "":
            self.state["default_branch"] = branch
        return body

    def _commit_body(self, sha):
        quoted = urllib.parse.quote(sha, safe="")
        return self._object(f"/repos/{self.repository}/commits/{quoted}")

    def _array(self, path):
        """GET one JSON list, or None when the rate-limit budget stopped the pass."""

        if self.budget.stopped:
            return None
        got = _get_json(self.api_base, path, None, self.list_token)
        self.budget.observe(got["remaining"])
        if got.get("exhausted") or self.budget.stopped:
            self.budget.stopped = True
            return None
        if not isinstance(got["body"], list):
            raise PollError("API_REJECTED", "GitHub response is not a list")
        return got["body"]

    def _permission(self, login):
        """Return the repository permission GitHub reports for one login.

        None means GitHub answered 401, 403, or 404, or sent no
        permission. A pass without ``--app-key`` sends no credential, and
        GitHub answers 401. ``_STOPPED`` means the rate-limit budget
        stopped the pass. One pass asks once per login.
        """

        if login in self.permissions:
            return self.permissions[login]
        if self.budget.stopped:
            return _STOPPED
        quoted = urllib.parse.quote(login, safe="")
        got = _get_json(
            self.api_base,
            f"/repos/{self.repository}/collaborators/{quoted}/permission",
            None,
            self.list_token,
            refused=_PERMISSION_REFUSED,
        )
        self.budget.observe(got["remaining"])
        if got.get("exhausted") or self.budget.stopped:
            self.budget.stopped = True
            return _STOPPED
        body = got["body"]
        permission = body.get("permission") if isinstance(body, dict) else None
        if not isinstance(permission, str) or permission == "":
            permission = None
        self.permissions[login] = permission
        return permission

    def _identity_reason(self, role, user, commit):
        """Return why one commit author or committer is untrusted.

        None means that identity passes. ``_STOPPED`` means the pass stopped.
        """

        login = _login(user)
        if login is None:
            return f"commit {role} is not a GitHub user"
        if role == "committer" and _github_committed(user, commit):
            return None
        # GitHub answers no collaborator permission for an app. Its commit
        # passes only when GitHub committed and signed it, as it does for
        # an app's squash merge or contents API write.
        if (
            role == "author"
            and _app_user(user)
            and isinstance(commit, dict)
            and _github_committed(commit.get("committer"), commit)
        ):
            return None
        permission = self._permission(login)
        if permission is _STOPPED:
            return _STOPPED
        if permission is None:
            return f"commit {role} permission is not readable"
        if permission not in _WRITE_PERMISSIONS:
            return f"commit {role} cannot write to the repository"
        return None

    def _commit_reason(self, commit):
        """Return why one commit is untrusted, None, or ``_STOPPED``.

        The author and the committer each need write or admin permission
        on this repository. A committer that ``_github_committed`` accepts
        is GitHub, and its author is still checked.
        """

        reasons = []
        for role in ("author", "committer"):
            reason = self._identity_reason(role, commit.get(role), commit)
            if reason is _STOPPED:
                return _STOPPED
            if reason is not None:
                reasons.append(reason)
        return "; ".join(reasons) if reasons else None

    def _pull_commits_reason(self, number, base_sha, head_sha):
        """Return why a pull request's commits are untrusted, None, or ``_STOPPED``.

        GitHub's commit list must name exactly the commits that ``head``
        has and ``base`` does not reach in the clone, so a commit the list
        leaves out is not skipped. Every one of them goes through
        ``_commit_reason``. GitHub lists at most 250 commits, and a longer
        pull request is refused.
        """

        expected = _rev_list(self.clone, base_sha, head_sha)
        if expected is None:
            return "pull request commits are not readable"
        if len(expected) > _PULL_COMMIT_LIMIT:
            return "pull request has more commits than GitHub lists"
        listed = self._pull_commit_list(number)
        if listed is None:
            return _STOPPED
        by_sha = {}
        for item in listed:
            if not isinstance(item, dict):
                raise PollError("API_REJECTED", "GitHub pull request commits are not usable")
            by_sha.setdefault(_sha(item.get("sha")), item)
        if len(by_sha) != len(listed) or set(by_sha) != set(expected):
            return "pull request commits do not match the listed head"
        reasons = []
        for sha in expected:
            reason = self._commit_reason(by_sha[sha])
            if reason is _STOPPED:
                return _STOPPED
            if reason is not None and reason not in reasons:
                reasons.append(reason)
        return "; ".join(reasons) if reasons else None

    def _pull_commit_list(self, number):
        """Return GitHub's commit list for one pull request, or None when the pass stopped."""

        listed = []
        page = 1
        while True:
            body = self._array(
                f"/repos/{self.repository}/pulls/{number}/commits?per_page=100&page={page}"
            )
            if body is None:
                return None
            listed.extend(body)
            if len(body) < 100 or len(listed) >= _PULL_COMMIT_LIMIT:
                return listed
            page += 1

    def _pull_commits_gate(self, number, base_sha, head_sha, override):
        """Return ``(verdict, override)`` for every commit on a pull request.

        The verdict is ``"trusted"``, ``"allowed"``, ``"refused"``, or
        ``"stop"``. A refusal is recorded here. When ``--allow-untrusted``
        names the head, the verdict is ``"allowed"`` and the override gains
        the commit reason.
        """

        reason = self._pull_commits_reason(number, base_sha, head_sha)
        if reason is _STOPPED:
            return "stop", override
        if reason is None:
            return "trusted", override
        if head_sha not in self.allow_untrusted:
            self._record_untrusted(number, head_sha, reason)
            return "refused", override
        self.result["warnings"].append(self._warning(number, head_sha, reason, True))
        return "allowed", reason if override is None else f"{override}; {reason}"

    def _fetch_pull_merge(self, number, base_sha, head_sha, base_ref):
        """Fetch a pull request's merge commit, or record why it is not used.

        The merge is used when its parents are the listed base and head, or
        the head and the base branch's current tip after the base moved.
        """

        merge = _fetch_sha(self.clone, f"refs/pull/{number}/merge")
        if merge is None:
            self._record_absent(number, head_sha)
            return None
        parents = _merge_parents(self.clone, merge)
        if parents == (base_sha, head_sha) or _moved_base_merge(
            self.clone, parents, head_sha, base_ref
        ):
            return merge
        self._record_merge_mismatch(number, head_sha)
        return None

    def _seen_pull(self, number, base_sha, head_sha, item, override, seen):
        """Post signoff for a pull request whose head and base already ran.

        The stored entry holds that head's commit verdict. Only ``trusted``
        skips the gate. An entry from poll state written before the gate,
        or from a run that ``--allow-untrusted`` let through, goes through
        the gate again, and a refusal posts no signoff.
        """

        if seen.get("commits") != "trusted":
            base = item.get("base")
            base_ref = base.get("ref") if isinstance(base, dict) else None
            if self._fetch_pull_merge(number, base_sha, head_sha, base_ref) is None:
                return True
            verdict, _override = self._pull_commits_gate(number, base_sha, head_sha, override)
            if verdict == "stop":
                return False
            if verdict == "refused":
                return True
            seen["commits"] = verdict
            self._checkpoint()
        return self._signoff(number, head_sha, base_sha, item)

    def _list(self, kind, path):
        """Return (body, etag) when a new list should be applied.

        A 304 keeps the stored validator. A list whose rate-limit header
        reports nothing remaining is left unapplied so the next pass
        requests it again.
        """

        if self.budget.stopped:
            return None
        listed = _get_json(
            self.api_base,
            path,
            None if self.retry_entry is not None else self.state["etags"].get(kind),
            self.list_token,
        )
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
        if self.hold_branches:
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
        if self.hold_pulls:
            return
        self.state["etags"]["pulls"] = etag
        self._checkpoint()

    def _schedules(self):
        """Submit at most one due minute per workflow on the default-branch tip.

        The workflow bytes are read from that tip. The clone is detached
        there only when a run is submitted, so a pull-request checkout
        earlier in this pass is not the tree the scheduled run captures.
        The due minute and that tip SHA are stored before submit. Recovery
        uses that stored row, including when the listed tip has since
        moved, and only then considers a later minute. The cursor is
        stored after the submit returns. A pass with no stored branch tip
        does not request the repository object.
        """

        if self.budget.stopped:
            return
        tips = self.state.get("tips")
        if not isinstance(tips, dict) or not tips:
            return
        branch = self.state.get("default_branch")
        repo = self.repository_body
        if not isinstance(branch, str) or branch == "":
            repo = self._repository_body()
            if repo is None:
                return
            branch = repo.get("default_branch")
        if not isinstance(branch, str) or branch == "":
            return
        ref = "refs/heads/" + branch
        tip = tips.get(ref)
        if not isinstance(tip, str):
            return
        seen = []
        for workflow, _job_id in self.jobs:
            if workflow not in seen:
                seen.append(workflow)
        for workflow in seen:
            if self.budget.stopped:
                return
            pending = self._pending_schedule(workflow)
            if pending is not None and not self._resume_schedule(workflow, pending, repo):
                return
            if not self._schedule_workflow(workflow, ref, tip, repo):
                return

    def _pending_schedule(self, workflow):
        pending = self.state.get("schedule_pending")
        if not isinstance(pending, dict) or workflow not in pending:
            return None
        row = pending[workflow]
        crons = row.get("crons") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or _schedule_cursor(row.get("tick")) is None
            or _SHA.fullmatch(row.get("sha") or "") is None
            or not isinstance(row.get("ref"), str)
            or row.get("ref") == ""
            or not isinstance(crons, list)
            or not crons
            or not all(isinstance(item, str) and item != "" for item in crons)
        ):
            raise PollError("STATE_INVALID", "poll state could not be read")
        return row

    def _resume_schedule(self, workflow, pending, repo):
        """Submit the tick stored before the lost reply, on its original SHA."""

        if repo is None:
            repo = self._repository_body()
            if repo is None:
                return False
        if not self._submit_schedule(
            workflow,
            pending["ref"],
            pending["sha"],
            repo,
            pending["crons"],
            pending["tick"],
        ):
            return False
        self._store_schedule(workflow, _schedule_cursor(pending["tick"]))
        self._clear_pending(workflow)
        return True

    def _schedule_workflow(self, workflow, ref, tip, repo):
        blob = self._workflow_bytes(workflow, tip)
        if blob is None:
            return True
        try:
            on = workflow_on(blob)
        except PlanError as exc:
            raise PollError("INVALID_PARAMS", str(exc)) from exc
        try:
            expressions = schedule_expressions(on)
        except Fault as exc:
            raise PollError(exc.kind, str(exc)) from exc
        if not expressions:
            return True
        schedules = self.state.get("schedules")
        stored = schedules.get(workflow) if isinstance(schedules, dict) else None
        previous = _schedule_cursor(stored)
        fire_at, cursor = schedule_decision(expressions, previous, self.clock)
        if fire_at is None:
            self._store_schedule(workflow, cursor)
            return True
        if repo is None:
            repo = self._repository_body()
            if repo is None:
                return False
        matching = [text for text in expressions if cron_matches(parse_cron(text), fire_at)]
        if not matching:
            self._store_schedule(workflow, cursor)
            return True
        tick = fire_at.isoformat()
        self._store_pending(
            workflow, {"tick": tick, "sha": tip, "ref": ref, "crons": list(matching)}
        )
        if not self._submit_schedule(workflow, ref, tip, repo, matching, tick):
            return False
        self._store_schedule(workflow, cursor)
        self._clear_pending(workflow)
        return True

    def _submit_schedule(self, workflow, ref, sha, repo, crons, tick):
        """Submit one stored minute. The SHA is the one recorded for that minute."""

        _checkout(self.clone, sha)
        for cron_text in crons:
            event = {
                "ref": ref,
                "repository": _event_repository(self.repository, repo),
                "schedule": cron_text,
            }
            if not self._submit_jobs(
                "schedule",
                ref,
                sha,
                sha,
                event,
                None,
                None,
                True,
                only=workflow,
                tick=tick + "\0" + cron_text,
                context_suffix="/schedule",
            ):
                return False
        return True

    def _store_pending(self, workflow, row):
        pending = self.state.get("schedule_pending")
        if not isinstance(pending, dict):
            pending = {}
            self.state["schedule_pending"] = pending
        if pending.get(workflow) == row:
            return
        pending[workflow] = row
        self._checkpoint()

    def _clear_pending(self, workflow):
        pending = self.state.get("schedule_pending")
        if not isinstance(pending, dict) or workflow not in pending:
            return
        del pending[workflow]
        self._checkpoint()

    def _workflow_bytes(self, workflow, tip):
        return _workflow_at(self.clone, workflow, tip)

    def _store_schedule(self, workflow, cursor):
        if cursor is None:
            return
        text = cursor.isoformat()
        schedules = self.state.get("schedules")
        if not isinstance(schedules, dict):
            schedules = {}
            self.state["schedules"] = schedules
        if schedules.get(workflow) == text:
            return
        schedules[workflow] = text
        self._checkpoint()

    def _push(self, ref, sha):
        previous = self.state["tips"].get(ref)
        if previous == sha and not self._retry_matches("push", sha):
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
        reason = self._commit_reason(commit)
        if reason is _STOPPED:
            return False
        if reason is not None and sha not in self.allow_untrusted:
            self._record_push_refusal(ref, sha, reason)
            return True
        if reason is not None:
            self.result["warnings"].append(self._warning(None, sha, reason, True))
        if not self._submit_jobs(
            "push",
            ref,
            sha,
            sha,
            event,
            None,
            changed,
            previous is None or self._retry_matches("push", sha),
            override=reason,
        ):
            return False
        if self.unplaced:
            # Keep the tip unsaved and the branch list unapplied so a later pass retries.
            self.hold_branches = True
            return True
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
        reason = untrusted_reason(head, base, item.get("author_association"))
        override = None
        if reason is not None and head_sha not in self.allow_untrusted:
            full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
            if not isinstance(full_name, str):
                full_name = None
            if _same_repository(head, base):
                self._record_untrusted(number, head_sha, reason)
            else:
                self._record_fork(number, head_sha, full_name, reason)
            return True
        if reason is not None:
            self.result["warnings"].append(self._warning(number, head_sha, reason, True))
            override = reason
        seen = self.state["pulls"].get(str(number))
        if (
            isinstance(seen, dict)
            and seen.get("head") == head_sha
            and seen.get("base") == base_sha
            and not self._retry_matches("pull_request", head_sha)
        ):
            return self._seen_pull(number, base_sha, head_sha, item, override, seen)
        ref = f"refs/pull/{number}/merge"
        merge = self._fetch_pull_merge(number, base_sha, head_sha, base_ref)
        if merge is None:
            return True
        verdict, override = self._pull_commits_gate(number, base_sha, head_sha, override)
        if verdict == "stop":
            return False
        if verdict == "refused":
            return True
        changed = _diff(self.clone, base_sha, head_sha)
        accepted, workflow_override = self._accept_pr_workflow(
            number, head_sha, item, base_sha, changed
        )
        if accepted is None:
            return False
        if not accepted:
            return True
        if workflow_override is not None:
            override = workflow_override if override is None else f"{override}; {workflow_override}"
        _checkout(self.clone, merge)
        repo = self._repository_body()
        if repo is None:
            return False
        activity = "opened" if seen is None else "synchronize"
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
            "action": activity,
            "pull_request": pull_request,
        }
        submitted = self._submit_jobs(
            "pull_request",
            ref,
            merge,
            head_sha,
            event,
            activity,
            changed,
            False,
            override=override,
        )
        if self.unplaced:
            self.hold_pulls = True
        if submitted and not self.unplaced:
            self.state["pulls"][str(number)] = {
                "head": head_sha,
                "base": base_sha,
                "merge": merge,
                "commits": verdict,
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
        """Post the sign-off status. There is no worker run to record.

        Token mode posts no description, as before.
        """

        if self.budget.stopped:
            return False, None
        return self._post_context(
            _SIGNOFF_CONTEXT,
            f"signoff-{number}",
            sha,
            state,
            summary,
            stored,
            describe=bool(self.app_key),
        )

    def _warning(self, number, sha, reason, allowed):
        verb = "allowed by override" if allowed else "refused"
        entry = {
            "sha": sha,
            "reason": reason,
            "action": "allowed" if allowed else "refused",
            "message": f"untrusted code {verb}: {reason}",
        }
        if number is not None:
            entry["number"] = number
        return entry

    def _record_fork(self, number, head_sha, full_name, reason):
        row = {"number": number, "head_sha": head_sha, "repository": full_name}
        if row not in self.state["forks"]:
            self.state["forks"].append(row)
            self.result["skipped"].append({"reason": "fork", "number": number})
            self.result["warnings"].append(self._warning(number, head_sha, reason, False))
            self._checkpoint()
        self.hold_pulls = True

    def _record_untrusted(self, number, head_sha, reason):
        row = {"number": number, "head_sha": head_sha, "reason": reason}
        rows = self.state.setdefault("refusals", [])
        if row not in rows:
            rows.append(row)
            self.result["skipped"].append({"reason": "untrusted", "number": number})
            self.result["warnings"].append(self._warning(number, head_sha, reason, False))
            self._checkpoint()
        self._post_refusal_status(number, head_sha, f"untrusted code: {reason}")
        self.hold_pulls = True

    def _accept_pr_workflow(self, number, head_sha, item, base_sha, changed):
        """Refuse a pull request whose diff changes a workflow file.

        Return ``(accepted, override)``, or ``(None, None)`` when the
        rate-limit budget stopped the pass. An allowed author records a
        warning, and its runs record the override, so an identity token is
        never minted for them.

        With ``--allow-pr-workflow``, a named association is allowed. Any
        other association on a same-repository pull request is allowed
        when GitHub reports the author's repository permission as write,
        maintain, or admin. GitHub reports owners and members to an app as
        CONTRIBUTOR when it cannot read organization membership. A fork, a
        missing association, and an unlisted one are never allowed this way. A refusal from that permission lookup is
        not stored, so a later pass asks again.
        """

        kind, paths = self._pr_workflow_change(base_sha, head_sha, changed)
        if kind is None:
            self._clear_refusal_status(number, head_sha)
            return True, None
        association = _association(item.get("author_association"))
        allowed_by = association if association in self.allow_pr_workflow else None
        permission_checked = False
        denial = None
        if (
            allowed_by is None
            and self.allow_pr_workflow
            and association in _KNOWN_ASSOCIATIONS
            and _same_repository(item.get("head"), item.get("base"))
        ):
            login = _login(item.get("user"))
            if login is None:
                denial = "author is not a GitHub user"
            else:
                permission = self._permission(login)
                if permission is _STOPPED:
                    return None, None
                permission_checked = True
                if permission in _PR_WORKFLOW_PERMISSIONS:
                    allowed_by = f"{permission} permission"
                elif permission is None:
                    denial = "author permission is not readable"
                else:
                    denial = "author cannot write to the repository"
        reason = "workflow_changed" if kind == "changed" else "workflow_diff_unavailable"
        message = _workflow_message(kind, paths, allowed_by)
        if allowed_by is None and denial is not None:
            message = f"{message}; {denial}"
        entry = {
            "number": number,
            "sha": head_sha,
            "action": "allowed" if allowed_by is not None else "refused",
            "reason": reason,
            "message": message,
        }
        if allowed_by is not None:
            self.result["warnings"].append(entry)
            self._clear_refusal_status(number, head_sha)
            return True, f"pull request workflow change allowed for {allowed_by}"
        self._record_workflow_refusal(number, head_sha, reason, entry, store=not permission_checked)
        return False, None

    def _pr_workflow_change(self, base_sha, head_sha, changed):
        if changed is None:
            return "unavailable", ()
        names = _changed_names(self.clone, base_sha, head_sha)
        if names is None:
            return "unavailable", ()
        configured = tuple(workflow for workflow, _job_id in self.jobs)
        paths = tuple(sorted({name for name in names if _workflow_definition(name, configured)}))
        if not paths:
            return None, ()
        return "changed", paths

    def _record_merge_mismatch(self, number, head_sha):
        """Do not check out a merge commit that is not the listed base and head.

        The head gets a visible error status, so the pull request is not silently skipped.
        """

        row = {"number": number, "head_sha": head_sha, "reason": "merge_commit_mismatch"}
        rows = self.state.setdefault("workflow_refusals", [])
        entry = {
            "number": number,
            "sha": head_sha,
            "action": "refused",
            "reason": "merge_commit_mismatch",
            "message": "fetched merge commit does not match the listed head",
        }
        if row not in rows:
            rows.append(row)
            self.result["skipped"].append({"reason": "merge_commit_mismatch", "number": number})
            self.result["warnings"].append(entry)
            self._checkpoint()
        self._post_trust(number, head_sha, "error", "refused: " + entry["message"])
        self.hold_pulls = True

    def _record_workflow_refusal(self, number, head_sha, reason, entry, store=True):
        """Record one workflow refusal and post it as a failed trust status.

        ``store`` false leaves the refusal out of poll state, so every
        pass warns again and asks for the author's permission again.
        """

        row = {"number": number, "head_sha": head_sha, "reason": reason}
        rows = self.state.setdefault("workflow_refusals", [])
        if not store or row not in rows:
            if store:
                rows.append(row)
            self.result["skipped"].append({"reason": reason, "number": number})
            self.result["warnings"].append(entry)
            self._checkpoint()
        self._post_refusal_status(number, head_sha, entry["message"])
        self.hold_pulls = True

    def _post_refusal_status(self, number, head_sha, message):
        """Post ``rookrunner/trust`` as a failure that says why no run started.

        One post per pull request head and message. A rate-limited post is
        not recorded, so a later pass posts it again.
        """

        self._post_trust(number, head_sha, "failure", f"refused: {message}")

    def _clear_refusal_status(self, number, head_sha):
        """Turn an earlier refusal on this head into a success."""

        stored = self.state.get("trust_statuses")
        record = stored.get(str(number)) if isinstance(stored, dict) else None
        if not isinstance(record, dict) or record.get("head") != head_sha:
            return
        if record.get("state") != "failure":
            return
        self._post_trust(number, head_sha, "success", "allowed to run")

    def _post_trust(self, number, head_sha, state, description):
        key = str(number)
        stored = self.state.get("trust_statuses")
        record = stored.get(key) if isinstance(stored, dict) else None
        if (
            isinstance(record, dict)
            and record.get("head") == head_sha
            and record.get("state") == state
            and record.get("description") == description
        ):
            return
        if self.budget.stopped:
            return
        posted, check_id = self._post_context(
            _TRUST_CONTEXT, f"trust-{number}", head_sha, state, description, record
        )
        if not posted:
            return
        saved = {"head": head_sha, "state": state, "description": description}
        if check_id is not None:
            saved["check_run_id"] = check_id
        if not isinstance(self.state.get("trust_statuses"), dict):
            self.state["trust_statuses"] = {}
        self.state["trust_statuses"][key] = saved
        self._checkpoint()
        self.result["statuses"].append(
            {"context": _TRUST_CONTEXT, "state": state, "action": "posted", "sha": head_sha}
        )

    def _post_context(self, context, run_id, sha, state, description, stored, describe=True):
        """Post one status that has no worker run. Return ``(posted, check_id)``."""

        described = self.caller("worker.describe", {})
        root = self._credential_repository(described)
        if self.app_key:
            check_run_id = None
            if isinstance(stored, dict) and is_integer(stored.get("check_run_id")):
                check_run_id = int(stored["check_run_id"])
            try:
                posted = post_check_flow(
                    api_base=self.api_base,
                    repository=self.repository,
                    sha=sha,
                    context=context,
                    run_id=run_id,
                    check_status="completed",
                    check_conclusion="success" if state == "success" else "failure",
                    check_summary_text=description,
                    check_run_id=check_run_id,
                    status_state=state,
                    post_status_request=True,
                    app_key=self.app_key,
                    state_dir=self.state_dir,
                    repository_root=root,
                    clock=self.clock,
                    key_dir=self.key_dir,
                )
            except StatusError as error:
                if error.kind == "RATE_LIMITED" and error.retryable:
                    self.budget.stopped = True
                    return False, None
                raise
            if posted.error is not None:
                if posted.error.kind == "RATE_LIMITED" and posted.error.retryable:
                    self.budget.stopped = True
                    return False, None
                raise posted.error
            if posted.status_posted:
                self.budget.observe(posted.status_remaining)
            return True, posted.check_id
        try:
            token = read_credential(self.credential_file, self.state_dir, root)
            try:
                remaining = post_status(
                    self.api_base,
                    self.repository,
                    sha,
                    state,
                    context,
                    token,
                    description=description if describe else None,
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

    def _record_push_refusal(self, ref, sha, reason):
        row = {"ref": ref, "sha": sha, "reason": reason}
        rows = self.state.setdefault("refusals", [])
        if row not in rows:
            rows.append(row)
            self.result["skipped"].append({"reason": "untrusted", "ref": ref, "sha": sha})
            self.result["warnings"].append(self._warning(None, sha, reason, False))
            self._checkpoint()
        self.hold_branches = True

    def _record_absent(self, number, head_sha):
        row = {"number": number, "head_sha": head_sha}
        if row not in self.state["merge_absent"]:
            self.state["merge_absent"].append(row)
            self.result["skipped"].append({"reason": "merge_ref_absent", "number": number})
            self._checkpoint()

    def _submit_jobs(
        self,
        event_name,
        ref,
        tested,
        status_sha,
        event,
        activity,
        changed,
        first,
        only=None,
        tick=None,
        context_suffix="",
        override=None,
    ):
        """Submit each configured job. Return false when a pending post did not finish."""

        self.unplaced = False
        if self.places is not None:
            return self._submit_placed(
                event_name,
                ref,
                tested,
                status_sha,
                event,
                activity,
                changed,
                first,
                only,
                tick,
                context_suffix,
                override,
            )
        jobs = self.jobs if only is None else [item for item in self.jobs if item[0] == only]
        for workflow, job_id in jobs:
            key = self._submission_key(
                submission_key(self.repository, event_name, tested, workflow, job_id, tick)
            )
            context = _job_context(workflow, job_id, context_suffix)
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
            if override is not None:
                params["untrusted_override"] = True
                params["untrusted_reason"] = override
            claimed = existing
            if claimed is None and self.ssh_host is not None:
                claimed = {
                    "key": key,
                    **self._retry_metadata(key),
                    "host": self.ssh_host,
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": context,
                    "pending": False,
                }
                self.state["submissions"].append(claimed)
                self._checkpoint()
            if self.ssh_host is not None:
                self._remote_checkout(tested)
            try:
                submitted = self.caller("run.submit", params)
            except PollError as exc:
                absent = _absent_reason(exc, job_id)
                if absent is not None:
                    if claimed is not None and "run_id" not in claimed:
                        self._drop_unstarted(claimed)
                    self.result["skipped"].append(
                        {
                            "reason": absent,
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
                    **self._retry_metadata(key),
                    "run_id": submitted["run_id"],
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": context,
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

    def _submit_placed(
        self,
        event_name,
        ref,
        tested,
        status_sha,
        event,
        activity,
        changed,
        first,
        only=None,
        tick=None,
        context_suffix="",
        override=None,
    ):
        """Place each new job. A recorded place is submitted again on that place."""

        jobs = self.jobs if only is None else [item for item in self.jobs if item[0] == only]
        for workflow, job_id in jobs:
            key = self._submission_key(
                submission_key(self.repository, event_name, tested, workflow, job_id, tick)
            )
            context = _job_context(workflow, job_id, context_suffix)
            existing = next(
                (item for item in self.state["submissions"] if item.get("key") == key), None
            )
            if existing is not None:
                place = self._place_for(existing.get("host"))
                if place is None or not place.get("reachable"):
                    if "run_id" not in existing:
                        self.unplaced = True
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
                    self.unplaced = True
                    continue
                claimed = existing
            else:
                place = self._choose_place()
                if place is None:
                    self.unplaced = True
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
                    **self._retry_metadata(key),
                    "host": place["name"],
                    "event": event_name,
                    "tested_commit": tested,
                    "status_sha": status_sha,
                    "context": context,
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
            if override is not None:
                params["untrusted_override"] = True
                params["untrusted_reason"] = override
            self._place_checkout(tested)
            try:
                submitted = self.caller("run.submit", params)
            except PollError as exc:
                absent = _absent_reason(exc, job_id)
                if absent is not None:
                    if "run_id" not in claimed:
                        self._drop_unstarted(claimed)
                    self.result["skipped"].append(
                        {
                            "reason": absent,
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
        # A removed snapshot cannot be posted. Keep the submission and
        # continue so a later run still posts. A later pass tries again.
        try:
            decision = self.caller("run.status", params)
        except PollError as exc:
            if exc.kind == "STATUS_REFUSED" and str(exc) == _SNAPSHOT_UNREADABLE:
                self.result["skipped"].append(
                    {"reason": "snapshot_unreadable", "run_id": entry["run_id"]}
                )
                if not self.app_key and self._post(
                    entry, "error", description=_SNAPSHOT_UNREADABLE, record=False
                ):
                    self.result["statuses"].append(
                        {"run_id": entry["run_id"], "state": "error", "action": "posted"}
                    )
                return
            raise
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

    def _post(self, entry, state, decision=None, description=None, record=True):
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
                    description,
                )
            finally:
                token = None
        except StatusError as error:
            if error.kind == "RATE_LIMITED" and error.retryable:
                self.budget.stopped = True
                return False
            raise
        if record:
            # The worker refuses to record a run whose snapshot is unreadable,
            # so the error post is not recorded and may repeat on a later pass.
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
                key_dir=self.key_dir,
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
    allow_untrusted=(),
    allow_pr_workflow=(),
    key_dir=None,
    retry_failed=None,
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
        allow_untrusted=allow_untrusted,
        allow_pr_workflow=allow_pr_workflow,
        key_dir=key_dir,
        retry_failed=retry_failed,
    ).run()


def _workflow_path(workflow):
    if (
        not isinstance(workflow, str)
        or workflow == ""
        or workflow.startswith(("/", "-"))
        or ".." in Path(workflow).parts
    ):
        raise PollError("INVALID_PARAMS", "workflow path is not accepted")


def _workflow_at(clone, workflow, tip):
    if not isinstance(tip, str) or _SHA.fullmatch(tip) is None:
        raise PollError("GIT_FAILED", "git command failed")
    _workflow_path(workflow)
    result = _git(clone, "show", f"{tip}:{workflow}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _origin_head(clone):
    """Return the branch name and tip SHA of ``origin/HEAD``. Does not fetch."""

    shown = _git(clone, "rev-parse", "--abbrev-ref", "origin/HEAD", check=False)
    if shown.returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    text = _stdout(shown).strip()
    prefix = "origin/"
    if not text.startswith(prefix):
        raise PollError("GIT_FAILED", "git command failed")
    name = text[len(prefix) :]
    if name == "" or _git(clone, "check-ref-format", "--branch", name, check=False).returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    parsed = _git(clone, "rev-parse", "--verify", "--end-of-options", "origin/HEAD", check=False)
    if parsed.returncode != 0:
        raise PollError("GIT_FAILED", "git command failed")
    sha = _stdout(parsed).strip()
    if _SHA.fullmatch(sha) is None:
        raise PollError("GIT_FAILED", "git command failed")
    return name, sha


def dispatch_once(*, repository, clone, workflow, caller, image=None):
    """Submit one workflow on the default-branch tip and return.

    This is one command. It does not fetch, does not listen, and does
    not read poll state. The tip is the clone's ``origin/HEAD``. A
    workflow that does not list ``workflow_dispatch`` checks nothing
    out and submits nothing.
    """

    repository = _repository_name(repository)
    clone = Path(clone)
    _workflow_path(workflow)
    described = caller("worker.describe", {})
    repository_path = described.get("repository") if isinstance(described, dict) else None
    if not isinstance(repository_path, str) or Path(repository_path).resolve() != clone.resolve():
        raise PollError("CLONE_MISMATCH", "dedicated clone must be the worker repository")
    name, sha = _origin_head(clone)
    ref = "refs/heads/" + name
    blob = _workflow_at(clone, workflow, sha)
    if blob is None:
        raise PollError("INVALID_PARAMS", "workflow is not on the default-branch tip")
    try:
        on = workflow_on(blob)
    except PlanError as exc:
        raise PollError("INVALID_PARAMS", str(exc)) from exc
    try:
        matched = submission_triggered(
            on,
            "workflow_dispatch",
            {"ref": ref},
            None,
            [],
            None,
            True,
        )
    except Fault as exc:
        raise PollError(exc.kind, str(exc)) from exc
    if not matched:
        return {"triggered": False, "ref": ref, "sha": sha}
    try:
        job_ids = workflow_job_ids(blob)
    except PlanError as exc:
        raise PollError("INVALID_PARAMS", str(exc)) from exc
    _checkout(clone, sha)
    event = {
        "ref": ref,
        "repository": {"full_name": repository, "default_branch": name},
    }
    submitted = []
    for job_id in job_ids:
        params = {
            "version": 1,
            "submission_key": submission_key(
                repository, "workflow_dispatch", sha, workflow, job_id
            ),
            "workflow": workflow,
            "job_id": job_id,
            "event": event,
            "event_name": "workflow_dispatch",
            "diff_unavailable": True,
        }
        if image is not None:
            params["image"] = image
        result = caller("run.submit", params)
        if isinstance(result, dict) and "run_id" in result:
            submitted.append({"job_id": job_id, "run_id": result["run_id"]})
            continue
        if isinstance(result, dict) and result.get("triggered") is False:
            submitted.append({"job_id": job_id, "triggered": False})
            continue
        raise PollError("WORKER_ERROR", "worker refused the request")
    return {"triggered": True, "ref": ref, "sha": sha, "submitted": submitted}
