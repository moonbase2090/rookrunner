# SPDX-License-Identifier: MIT

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
import threading

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

    return _measure(root)


def _measure(root, directories=None, subtree_sizes=None):
    root = Path(root)
    total = 0
    seen_directories = set()
    pending = [Path(root)]
    entry_sizes = {}
    while pending:
        current = pending.pop()
        info = current.lstat()
        total += info.st_size
        entry_sizes[current] = info.st_size
        if not stat.S_ISDIR(info.st_mode):
            continue
        if directories is not None:
            directories[current] = info.st_size
        identity = (info.st_dev, info.st_ino)
        if identity in seen_directories:
            continue
        seen_directories.add(identity)
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)
    if subtree_sizes is not None:
        totals = dict(entry_sizes)
        for path in sorted(entry_sizes, key=lambda item: len(item.parts), reverse=True):
            if path != root:
                totals[path.parent] += totals[path]
        subtree_sizes.update(totals)
    return total


class StateUsage:
    """Maintain a state-tree byte total with targeted filesystem updates."""

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._directories = {}
        self._total = _measure(self.root, self._directories)
        self._paths = {}

    @property
    def total(self):
        with self._lock:
            return self._total

    def add(self, path):
        path = Path(path)
        directories = {}
        size = _measure(path, directories)
        with self._lock:
            old = self._paths.get(path)
            self._total += size if old is None else size - old
            self._paths[path] = size
            self._directories.update(directories)
            self._sync_ancestor_directories(path)
        return size

    def sync_path(self, path):
        path = Path(path)
        directories = {}
        try:
            size = _measure(path, directories)
        except FileNotFoundError:
            size = 0
        with self._lock:
            self._total += size - self._paths.get(path, 0)
            self._paths[path] = size
            self._directories.update(directories)
            self._sync_ancestor_directories(path)
        return size

    def remove(self, path):
        path = Path(path)
        try:
            size = usage(path)
        except FileNotFoundError:
            with self._lock:
                size = self._paths.pop(path, 0)
                self._total -= size
                for tracked in tuple(self._paths):
                    if path in tracked.parents:
                        del self._paths[tracked]
                for directory in tuple(self._directories):
                    if directory == path or path in directory.parents:
                        del self._directories[directory]
            return size
        with self._lock:
            old = self._paths.get(path)
            if old is not None:
                self._total += size - old
            self._total -= size
            self._paths.pop(path, None)
            for tracked in tuple(self._paths):
                if tracked == path or path in tracked.parents:
                    del self._paths[tracked]
            for directory in tuple(self._directories):
                if directory == path or path in directory.parents:
                    del self._directories[directory]
        return size

    def sync_files(self, paths):
        """Reconcile a small set of mutable files without walking the tree."""

        sizes = {}
        for path in map(Path, paths):
            try:
                info = path.lstat()
            except FileNotFoundError:
                size = 0
            else:
                size = info.st_size
            sizes[path] = size
        with self._lock:
            for path, size in sizes.items():
                self._total += size - self._paths.get(path, 0)
                self._paths[path] = size
                self._sync_ancestor_directories(path)

    def sync_parent_directories(self, path):
        """Reconcile parent directory entry sizes after a create or removal."""

        with self._lock:
            self._sync_ancestor_directories(Path(path))

    def remember_entries(self, paths):
        """Record paths already covered by the initial full measurement."""

        sizes = {}
        for path in map(Path, paths):
            try:
                size = path.lstat().st_size
            except FileNotFoundError:
                size = 0
            sizes[path] = size
        with self._lock:
            self._paths.update(sizes)

    def refresh(self):
        directories = {}
        paths = {}
        total = _measure(self.root, directories, paths)
        with self._lock:
            self._total = total
            self._directories = directories
            for path in self._paths:
                self._paths[path] = paths.get(path, 0)
        return total

    def _sync_ancestor_directories(self, path):
        current = Path(path).parent
        while current == self.root or self.root in current.parents:
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                continue
            if stat.S_ISDIR(info.st_mode):
                size = info.st_size
                previous = self._directories.get(current)
                self._total += size if previous is None else size - previous
                self._directories[current] = size
            current = current.parent
