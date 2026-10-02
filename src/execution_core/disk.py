"""Bytes under one worker state directory.

The default budget is the default GitHub Actions cache storage limit: 10 GB
per repository, on every plan in the storage table. GitHub documents that
figure as 10 GB. This repository stores it as 10 * 1024 * 1024 * 1024 bytes,
the same 1024-based reading it uses for the documented 500 KB workflow-file
limit. GitHub does not define a 1000- or 1024-based byte count.
https://docs.github.com/en/actions/reference/limits

A repository administrator can raise GitHub's cache limit, and GitHub evicts
cache entries past it. This budget does not evict. Active runs and their
snapshots and attempt workspaces stay, and a new submission that would exceed
the budget is refused. An operator can set a different byte count. GitHub
Free artifact storage (500 MB) is an account artifact quota, not this
per-repository state budget. Artifacts are not implemented here.
https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy
"""

import os
from pathlib import Path
import stat

DEFAULT_DISK_BUDGET = 10 * 1024 * 1024 * 1024


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
