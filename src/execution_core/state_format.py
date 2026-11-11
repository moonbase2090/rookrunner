# SPDX-License-Identifier: MIT

"""Version the worker's durable SQLite state."""

from .release_manifest import STATE_FORMAT_VERSION


def check_existing_state_format(database):
    """Refuse an unknown format before SQLite schema migration can write."""

    table = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in database.execute("PRAGMA table_info(metadata)")}
    if not {"key", "value"}.issubset(columns):
        raise ValueError("STATE_FORMAT_UNSUPPORTED: state metadata is not supported")
    row = database.execute("SELECT value FROM metadata WHERE key='state_format_version'").fetchone()
    if row is not None and row[0] != str(STATE_FORMAT_VERSION):
        raise ValueError("STATE_FORMAT_UNSUPPORTED: state format is not supported")


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
