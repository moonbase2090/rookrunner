# SPDX-License-Identifier: MIT

"""Run the exact poller gate used before activating the rest of a cohort."""

import re
import time

_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def run_poller_canary(poll, caller, *, expected_engine_commit, timeout=900, sleep=time.sleep):
    """Submit one real workflow, wait for success, post status, and poll cleanly."""

    if not isinstance(expected_engine_commit, str) or not _SHA.fullmatch(expected_engine_commit):
        raise ValueError("expected engine commit is invalid")
    description = caller("worker.describe", {})
    manifest = description.get("engine_release") if isinstance(description, dict) else None
    if not isinstance(manifest, dict) or manifest.get("source_commit") != expected_engine_commit:
        raise ValueError("canary worker is not running the requested engine release")

    submitted = poll()
    rows = submitted.get("submitted") if isinstance(submitted, dict) else None
    runs = [
        row for row in rows or [] if isinstance(row, dict) and isinstance(row.get("run_id"), str)
    ]
    if len(runs) != 1:
        raise ValueError("canary poll did not submit exactly one workflow run")
    run_id = runs[0]["run_id"]
    deadline = time.monotonic() + timeout
    record = caller("run.get", {"run_id": run_id})
    if not isinstance(record, dict):
        raise ValueError("canary worker returned an invalid run record")
    while record.get("state") in {"queued", "running"} and time.monotonic() < deadline:
        sleep(0.2)
        record = caller("run.get", {"run_id": run_id})
        if not isinstance(record, dict):
            raise ValueError("canary worker returned an invalid run record")
    if record.get("state") != "succeeded" or type(record.get("exit_code")) is not int:
        raise ValueError("canary workflow did not succeed")
    if record["exit_code"] != 0:
        raise ValueError("canary workflow did not exit successfully")
    input_record = record.get("input")
    digest = input_record.get("digest") if isinstance(input_record, dict) else None
    commit_sha = input_record.get("tested_commit") if isinstance(input_record, dict) else None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("canary workflow input digest is invalid")
    if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
        raise ValueError("canary workflow commit SHA is invalid")

    posted = poll()
    statuses = posted.get("statuses") if isinstance(posted, dict) else None
    matching = [
        row for row in statuses or [] if isinstance(row, dict) and row.get("run_id") == run_id
    ]
    if (
        len(matching) != 1
        or matching[0].get("state") != "success"
        or matching[0].get("action") != "posted"
    ):
        raise ValueError("canary final success status was not posted")

    clean = poll()
    if not isinstance(clean, dict) or clean.get("statuses") != []:
        raise ValueError("second poll after status posting was not clean")
    return {
        "engine_commit": expected_engine_commit,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "run_state": record["state"],
        "exit_code": record["exit_code"],
        "input_digest": digest,
        # Posting the final status calls run.status, which verifies the stored
        # snapshot manifest against this digest before accepting the disposition.
        "input_digest_verified": True,
        "status_disposition": "posted",
        "second_poll": "clean",
    }
