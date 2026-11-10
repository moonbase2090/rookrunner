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

Each inode is counted once. A second hard link to a file already under the
state directory does not add that file's size again. Symlinks are counted by
their own length and are not followed. Admission stops only when the charged
bytes would exceed the budget. The low watermark is 60 percent of the budget
and the high watermark is 80 percent. Crossing a watermark reports pressure
and does not by itself refuse work.
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
LOW_WATERMARK_PERCENT = 60
HIGH_WATERMARK_PERCENT = 80


def low_watermark(budget):
    """Byte count at which storage pressure is first reported."""

    return budget * LOW_WATERMARK_PERCENT // 100


def high_watermark(budget):
    """Byte count at which storage pressure is high. Admission still runs."""

    return budget * HIGH_WATERMARK_PERCENT // 100


def storage_pressure(used, budget):
    """`ok`, `low`, `high`, or `full` for one used/budget pair.

    `full` is the only level that means another byte would not fit.
    `low` and `high` are visible while admission can still succeed.
    """

    if used >= budget:
        return "full"
    high = high_watermark(budget)
    low = low_watermark(budget)
    if high > 0 and used >= high:
        return "high"
    if low > 0 and used >= low:
        return "low"
    return "ok"


def storage_report(used, budget):
    """Diagnostics for describe, status, and the storage plan."""

    return {
        "budget_bytes": budget,
        "used_bytes": used,
        "low_watermark_bytes": low_watermark(budget),
        "high_watermark_bytes": high_watermark(budget),
        "pressure": storage_pressure(used, budget),
    }


def usage(root):
    """Byte size of one inode under root, counted once.

    Symlinks are counted by their own length and are not followed. An
    unreadable entry raises OSError so the caller can fail closed.
    """

    return _scan(root).total


class _Scan:
    def __init__(self):
        self.total = 0
        self.sizes = {}
        self.links = {}
        self.directories = {}
        self.nlinks = {}


def _scan(root):
    root = Path(root)
    scan = _Scan()
    seen_directories = set()
    pending = [root]
    while pending:
        current = pending.pop()
        info = current.lstat()
        ident = (info.st_dev, info.st_ino)
        is_dir = stat.S_ISDIR(info.st_mode)
        if is_dir and ident in seen_directories:
            scan.links[ident] = scan.links.get(ident, 0) + 1
            continue
        if ident not in scan.sizes:
            scan.sizes[ident] = info.st_size
            scan.links[ident] = 1
            scan.total += info.st_size
        else:
            scan.links[ident] = scan.links.get(ident, 0) + 1
        if not is_dir:
            scan.nlinks[ident] = info.st_nlink
            continue
        seen_directories.add(ident)
        scan.directories[current] = info.st_size
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)
    return scan


class StateUsage:
    """Maintain a state-tree byte total with targeted filesystem updates.

    An inode already included in the total is not charged again when a later
    add or refresh sees another name for it. File growth updates the charged
    size in place. Each name found for a known file raises its recorded link
    count, capped at the inode's link count on disk, so removing one name of
    a hard-linked file keeps the bytes charged while another name remains.
    A repeated sync of a file that also has names outside the tree can leave
    the count high. That only delays freeing until the next refresh.
    A path that vanished outside the tracker is settled by a full refresh.
    """

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        scan = _scan(self.root)
        self._sizes = scan.sizes
        self._links = scan.links
        self._directories = scan.directories
        self._total = scan.total
        self._file_inodes = {}

    @property
    def total(self):
        with self._lock:
            return self._total

    def add(self, path):
        scan = _scan(path)
        with self._lock:
            self._integrate(scan)
            self._directories.update(scan.directories)
            self._sync_ancestor_directories(path)
        return scan.total

    def sync_path(self, path):
        path = Path(path)
        try:
            scan = _scan(path)
        except FileNotFoundError:
            self.refresh()
            return 0
        with self._lock:
            self._integrate(scan)
            self._directories.update(scan.directories)
            self._sync_ancestor_directories(path)
        return scan.total

    def remove(self, path):
        path = Path(path)
        try:
            scan = _scan(path)
        except FileNotFoundError:
            before = self.total
            self.refresh()
            return max(0, before - self.total)
        with self._lock:
            freed = 0
            for ident, count in scan.links.items():
                known = self._links.get(ident, 0)
                left = known - count
                if left <= 0:
                    freed += self._sizes.pop(ident, 0)
                    self._links.pop(ident, None)
                else:
                    self._links[ident] = left
            self._total -= freed
            for recorded in tuple(self._file_inodes):
                if recorded == path or path in recorded.parents:
                    del self._file_inodes[recorded]
            for directory in tuple(self._directories):
                if directory == path or path in directory.parents:
                    del self._directories[directory]
        return freed

    def sync_files(self, paths):
        """Reconcile a small set of mutable files without walking the tree."""

        noted = []
        missing = []
        for path in map(Path, paths):
            try:
                info = path.lstat()
            except FileNotFoundError:
                missing.append(path)
                continue
            noted.append((path, (info.st_dev, info.st_ino), info.st_size))
        with self._lock:
            for path in missing:
                ident = self._file_inodes.pop(path, None)
                if ident is None:
                    continue
                known = self._links.get(ident, 0) - 1
                if known <= 0:
                    self._total -= self._sizes.pop(ident, 0)
                    self._links.pop(ident, None)
                else:
                    self._links[ident] = known
            for path, ident, size in noted:
                self._file_inodes[path] = ident
                self._note_inode(ident, size, 1)
                self._sync_ancestor_directories(path)

    def sync_parent_directories(self, path):
        """Reconcile parent directory entry sizes after a create or removal."""

        with self._lock:
            self._sync_ancestor_directories(Path(path))

    def remember_entries(self, paths):
        """Record files already covered by the initial full measurement."""

        found = []
        for path in map(Path, paths):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((path, (info.st_dev, info.st_ino)))
        with self._lock:
            self._file_inodes.update(found)

    def refresh(self):
        scan = _scan(self.root)
        with self._lock:
            self._total = scan.total
            self._sizes = scan.sizes
            self._links = scan.links
            self._directories = scan.directories
            alive = set(scan.sizes)
            for path, ident in tuple(self._file_inodes.items()):
                if ident not in alive:
                    del self._file_inodes[path]
        return scan.total

    def _integrate(self, scan):
        for ident, size in scan.sizes.items():
            names = scan.links.get(ident, 1)
            on_disk = scan.nlinks.get(ident)
            if on_disk is not None and ident in self._links:
                self._links[ident] = min(on_disk, self._links[ident] + names)
            self._note_inode(ident, size, names)

    def _note_inode(self, ident, size, links_if_new):
        previous = self._sizes.get(ident)
        if previous is None:
            self._sizes[ident] = size
            self._links[ident] = links_if_new
            self._total += size
            return
        if size != previous:
            self._total += size - previous
            self._sizes[ident] = size

    def _sync_ancestor_directories(self, path):
        current = Path(path).parent
        while current == self.root or self.root in current.parents:
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                continue
            if stat.S_ISDIR(info.st_mode):
                ident = (info.st_dev, info.st_ino)
                self._directories[current] = info.st_size
                self._note_inode(ident, info.st_size, 1)
            current = current.parent
