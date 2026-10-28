# SPDX-License-Identifier: MIT
"""Find runs whose final commit status was never posted, and re-post it.

A run is a candidate when its record is final (not queued or running) and
every ``status_posts`` row for its (run, context, sha) is ``pending``.
GitHub is read first; a context that already shows the final state is
skipped, so running reconcile twice posts nothing the second time.
"""

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from .status import StatusError, github_state, status_url

RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 1.0


def post_with_retry(post, *, attempts=RETRY_ATTEMPTS, base=RETRY_BASE_SECONDS, sleep=time.sleep):
    """Call ``post()`` until it returns, backing off 1s, 2s, 4s between tries.

    Rate-limit and worker status refusals are not retried. Other ``StatusError``
    and ``OSError`` failures are retried, and the last failure is raised.
    """

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return post()
        except StatusError as error:
            if error.kind in {"RATE_LIMITED", "STATUS_REFUSED"}:
                raise
            if attempt == attempts - 1:
                raise
        except OSError:
            if attempt == attempts - 1:
                raise
        sleep(base * (2**attempt))
    raise AssertionError("unreachable")


def pending_final(db_path, shas=()):
    """Return final runs that may still show ``pending`` on GitHub. Read-only.

    Without ``shas``, only runs whose every recorded post is ``pending`` qualify.
    With ``shas``, the newest final run for each context on those commits
    qualifies even when a final post was recorded, because a recorded post can
    still be missing on GitHub; ``reconcile`` skips the ones GitHub already shows.
    """

    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        rows = db.execute(
            """
            SELECT s.run_id, s.context, s.sha,
                   group_concat(s.state),
                   json_extract(r.record, '$.state'),
                   json_extract(r.record, '$.exit_code')
            FROM status_posts s JOIN runs r ON r.id = s.run_id
            GROUP BY s.run_id, s.context, s.sha
            ORDER BY r.sequence
            """
        ).fetchall()
    finally:
        db.close()
    newest = {}
    for run_id, context, sha, posted, run_state, exit_code in rows:
        if shas and not any(sha.startswith(prefix) for prefix in shas):
            continue
        newest[(context, sha)] = {
            "run_id": run_id,
            "context": context,
            "sha": sha,
            "posted": set(posted.split(",")),
            "final": github_state(run_state, exit_code),
        }
    return [
        {key: value for key, value in item.items() if key != "posted"}
        for item in newest.values()
        if item["final"] != "pending" and (shas or item["posted"] == {"pending"})
    ]


def github_statuses(api_base, repository, sha, token, opener=None):
    """Return ``{context: state}`` from the combined status for ``sha``."""

    url = status_url(api_base, repository, sha).replace("/statuses/", "/commits/") + "/status"
    url += "?per_page=100"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "rookrunner")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=10) as response:
            body = json.loads(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        raise StatusError(
            "STATUS_REJECTED", f"GitHub status read failed with HTTP {error.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise StatusError("STATUS_REJECTED", "GitHub status read failed") from None
    return {item["context"]: item["state"] for item in body.get("statuses", [])}


def reconcile(candidates, remote_for_sha, post=None, *, dry_run=True, sleep=time.sleep):
    """Decide and, unless ``dry_run``, post each candidate's final state.

    ``remote_for_sha(sha)`` returns GitHub's ``{context: state}``.
    ``post(candidate)`` posts and records one status; it is retried with backoff.
    """

    if not dry_run and post is None:
        raise ValueError("a live reconcile needs a post function")
    remote_cache = {}
    actions = []
    for candidate in candidates:
        sha = candidate["sha"]
        if sha not in remote_cache:
            remote_cache[sha] = remote_for_sha(sha)
        remote = remote_cache[sha].get(candidate["context"])
        entry = {**candidate, "github": remote}
        if remote == candidate["final"]:
            entry["action"] = "already_final"
        elif dry_run:
            entry["action"] = "would_post"
        else:
            try:
                post_with_retry(lambda c=candidate: post(c), sleep=sleep)
                entry["action"] = "posted"
            except (StatusError, OSError) as error:
                entry["action"] = "failed"
                entry["error"] = str(error)
        actions.append(entry)
    return actions
