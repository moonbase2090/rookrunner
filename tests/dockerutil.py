"""Omit the container this process is running in.

A job container is named ``rookrunner-`` plus 16 hexadecimal characters.
Docker sets that container's hostname to its id. The unit tests remove
every container matched by ``name=rookrunner-``. Inside a job, that list
includes the job itself. On a host, the hostname is not a container id,
so nothing is omitted.
"""

import re
import socket

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")


def container_id_from_hostname(host):
    """Return a container id when `host` is one, else None."""

    if not isinstance(host, str):
        return None
    text = host.strip().lower()
    if _CONTAINER_ID.fullmatch(text):
        return text
    return None


def current_container_id():
    return container_id_from_hostname(socket.gethostname())


def same_container(container_id, own):
    """True when `container_id` is `own`, including a short or full id."""

    if not own or not isinstance(container_id, str):
        return False
    left = container_id.strip().lower()
    if not left:
        return False
    return left == own or left.startswith(own) or own.startswith(left)


def foreign_ids(text, own=None):
    """Ids from `docker ps -q`, omitting this container."""

    if own is None:
        own = current_container_id()
    found = []
    for line in text.splitlines():
        item = line.strip()
        if item and not same_container(item, own):
            found.append(item)
    return found


def foreign_named(text, own=None):
    """Names from `docker ps --format '{{.ID}} {{.Names}}'`, omitting this container."""

    if own is None:
        own = current_container_id()
    found = []
    for line in text.splitlines():
        piece = line.strip()
        if not piece:
            continue
        container_id, _separator, name = piece.partition(" ")
        if name and not same_container(container_id, own):
            found.append(name)
    return found
