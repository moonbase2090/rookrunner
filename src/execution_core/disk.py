"""Bytes under one worker state directory.

The default budget is the default GitHub Actions cache storage limit: 10 GB
per repository, on every plan in the storage table. GitHub documents that
figure as 10 GB. This repository stores it as 10 * 1024 * 1024 * 1024 bytes,
the same 1024-based reading it uses for the documented 500 KB workflow-file
limit. GitHub does not define a 1000- or 1024-based byte count.
https://docs.github.com/en/actions/reference/limits

A repository administrator can raise GitHub's cache limit, and GitHub evicts
cache entries past it. This budget removes finished run folders when they are
past the retention age or count, oldest first when a new submission still
would not fit. In-flight runs and unresolved cleanups stay. Run records and
submission keys stay. A submission that still would not fit is refused. An
operator can set a different byte count. GitHub Free artifact storage
(500 MB) is an account artifact quota, not this per-repository state budget.
Artifacts are not implemented here.
https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy

GitHub retains workflow artifacts and logs for 90 days by default.
https://docs.github.com/en/actions/reference/limits
"""

import os
from pathlib import Path
import stat

DEFAULT_DISK_BUDGET = 10 * 1024 * 1024 * 1024
# 90 days, the documented default retention for workflow artifacts and logs.
FINISHED_RUN_FOLDER_SECONDS = 90 * 24 * 60 * 60
# Newest finished run folders kept while they are younger than that age.
FINISHED_RUN_FOLDER_COUNT = 100


def usage(root):
    """Byte size of every directory entry under root.

    Symlinks are counted by their own length and are not followed. Each hard
    link is counted once per directory entry. An unreadable entry raises
    OSError so the caller can fail closed.
    """

    total = 0
    seen_directories = set()
    pending = [Path(root)]
    while pending:
        current = pending.pop()
        info = current.lstat()
        total += info.st_size
        if not stat.S_ISDIR(info.st_mode):
            continue
        identity = (info.st_dev, info.st_ino)
        if identity in seen_directories:
            continue
        seen_directories.add(identity)
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)
    return total
