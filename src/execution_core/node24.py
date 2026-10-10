# SPDX-License-Identifier: MPL-2.0

"""Operator-supplied Node 24 directory.

The worker does not download or bundle Node. ``worker --node24 DIR`` names
an unpacked Linux distribution for the job image architecture. The job
container mounts that directory read-only at ``/opt/node24``. ``run`` steps
do not receive it on ``PATH``.
"""

import os
import re
from pathlib import Path

from .actions import ActionUnavailable, _iter_tree, tree_digest

MOUNT = "/opt/node24"
BINARY = "/opt/node24/bin/node"
_MESSAGE = "node24 directory is not accepted"
_VERSION = re.compile(r"v24\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")


def inspect_node24(directory):
    """Return the absolute directory and the content digest of its tree.

    A symlink root, a missing executable ``bin/node``, or a tree the action
    store would refuse raises ValueError. The message does not include the path.
    """

    root = _root(directory)
    try:
        entries = _iter_tree(root)
        digest = tree_digest(root)
    except ActionUnavailable as exc:
        raise ValueError(_MESSAGE) from exc
    if not any(
        relative == "bin/node" and kind in {"100755", "120000"}
        for relative, kind, _payload in entries
    ):
        raise ValueError(_MESSAGE)
    return {"root": root, "digest": digest}


def node_version(text):
    """Return the Node 24 version line, or raise ValueError."""

    if not isinstance(text, str):
        raise ValueError("node is not Node 24")
    line = text.strip("\r\n")
    if line != text.strip() or len(line) > 64 or not _VERSION.fullmatch(line):
        raise ValueError("node is not Node 24")
    return line


def _root(directory):
    if isinstance(directory, Path):
        text = os.fspath(directory)
    elif isinstance(directory, str):
        text = directory
    else:
        raise ValueError(_MESSAGE)
    if text == "" or "\0" in text or "\n" in text or "," in text:
        raise ValueError(_MESSAGE)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError(_MESSAGE)
        parent = candidate.parent.resolve()
    except OSError as exc:
        raise ValueError(_MESSAGE) from exc
    root = parent / candidate.name
    located = os.fspath(root)
    if "," in located or "\n" in located or "\0" in located:
        raise ValueError(_MESSAGE)
    return root
