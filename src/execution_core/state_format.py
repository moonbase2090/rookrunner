# SPDX-License-Identifier: MIT

"""Version the worker's durable SQLite state."""

from .release_manifest import STATE_FORMAT_VERSION


def ensure_state_format(database):
    """Mark a legacy database current or refuse an unknown format."""

    with database:
        row = database.execute(
            "SELECT value FROM metadata WHERE key='state_format_version'"
        ).fetchone()
        if row is None:
            database.execute(
                "INSERT INTO metadata VALUES ('state_format_version', ?)",
                (str(STATE_FORMAT_VERSION),),
            )
        elif row[0] != str(STATE_FORMAT_VERSION):
            raise ValueError("STATE_FORMAT_UNSUPPORTED: state format is not supported")
    return STATE_FORMAT_VERSION
