-- SQLite schema from the worker immediately before d8a8bb1. The fixture has
-- no state_format_version row; tests seed synthetic records after applying it.
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    submission_key TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL,
    record TEXT NOT NULL,
    log BLOB NOT NULL
);
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    digest TEXT NOT NULL,
    name TEXT,
    UNIQUE(run_id, path)
);
CREATE TABLE status_posts (
    run_id TEXT NOT NULL,
    context TEXT NOT NULL,
    sha TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (run_id, context, sha, state)
);
CREATE TABLE check_posts (
    run_id TEXT NOT NULL,
    context TEXT NOT NULL,
    sha TEXT NOT NULL,
    check_run_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    PRIMARY KEY (run_id, context, sha)
);
