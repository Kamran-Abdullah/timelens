"""Schema versioning.

The database stores its schema version in SQLite's `PRAGMA user_version`. Each migration
is a (version, SQL) pair that runs once, inside a transaction, in order. To change the
schema in a future release, ADD a new entry - never edit an old one - so existing users'
history is upgraded in place and never lost.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_V1 = """
CREATE TABLE categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind  TEXT NOT NULL CHECK (kind IN ('productive','neutral','distraction','personal','custom')),
    color TEXT
);

CREATE TABLE applications (
    id              INTEGER PRIMARY KEY,
    executable_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    excluded        INTEGER NOT NULL DEFAULT 0      -- user-controlled exclusion
);

CREATE TABLE documents (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,                    -- file name only, never a path
    UNIQUE (application_id, name)
);

CREATE TABLE activity_sessions (
    id                INTEGER PRIMARY KEY,
    start_timestamp   TEXT NOT NULL,                 -- UTC, see models.TS_FORMAT
    end_timestamp     TEXT,                          -- NULL while the session is open
    application_id    INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    document_id       INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    application_name  TEXT NOT NULL,                 -- copied so history survives deletes
    executable_name   TEXT NOT NULL DEFAULT '',
    document_name     TEXT,
    activity_status   TEXT NOT NULL CHECK (activity_status IN
                        ('ACTIVE','IDLE','LOCKED','SLEEP_OFF','UNMONITORED')),
    duration_seconds  REAL NOT NULL DEFAULT 0,
    recovered         INTEGER NOT NULL DEFAULT 0,    -- 1 = closed by crash recovery
    CHECK (end_timestamp IS NULL OR end_timestamp >= start_timestamp)
);
CREATE INDEX idx_sessions_start  ON activity_sessions(start_timestamp);
CREATE INDEX idx_sessions_end    ON activity_sessions(end_timestamp);
CREATE INDEX idx_sessions_app    ON activity_sessions(application_id, start_timestamp);
CREATE INDEX idx_sessions_doc    ON activity_sessions(document_id, start_timestamp);
-- Duplicate prevention: the same state starting at the same instant is stored once.
CREATE UNIQUE INDEX uq_sessions_identity ON activity_sessions(
    start_timestamp, activity_status, executable_name, COALESCE(document_name, ''));
-- Integrity: at most ONE session may be open at any time.
CREATE UNIQUE INDEX uq_one_open_session ON activity_sessions((1)) WHERE end_timestamp IS NULL;

CREATE TABLE system_events (
    id         INTEGER PRIMARY KEY,
    timestamp  TEXT NOT NULL,
    event_type TEXT NOT NULL,                        -- startup, shutdown, crash_recovered, ...
    detail     TEXT
);
CREATE INDEX idx_events_ts ON system_events(timestamp);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cache of finished local days; rebuilt from activity_sessions (the source of truth).
CREATE TABLE daily_summaries (
    day                TEXT PRIMARY KEY,             -- local date, YYYY-MM-DD
    active_seconds     REAL NOT NULL,
    idle_seconds       REAL NOT NULL,
    locked_seconds     REAL NOT NULL,
    sleep_off_seconds  REAL NOT NULL,
    unmonitored_seconds REAL NOT NULL,
    application_count  INTEGER NOT NULL,
    document_count     INTEGER NOT NULL,
    updated_at         TEXT NOT NULL
);

INSERT INTO categories (name, kind, color) VALUES
    ('Productive',  'productive',  '#2e9e6b'),
    ('Neutral',     'neutral',     '#8a94a6'),
    ('Distraction', 'distraction', '#d9534f'),
    ('Personal',    'personal',    '#5b7fd6');
"""

# v2: optional website tracking (only ever filled if the user turns it on in Settings).
# Stores the SITE (domain), page title, search words and times - never the full web address.
_V2 = """
CREATE TABLE web_visits (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,              -- e.g. 'chrome:Default' (browser and profile)
    source_visit_id  INTEGER NOT NULL,           -- the browser's own visit id (prevents duplicates)
    visit_time       TEXT NOT NULL,              -- UTC, see models.TS_FORMAT
    domain           TEXT NOT NULL,              -- 'github.com' (no path, no query string)
    title            TEXT NOT NULL DEFAULT '',
    search_query     TEXT,                       -- words typed into a search engine, if it was a search
    duration_seconds REAL NOT NULL DEFAULT 0,    -- time the page was open, as reported by the browser
    UNIQUE (source, source_visit_id)
);
CREATE INDEX idx_web_time   ON web_visits(visit_time);
CREATE INDEX idx_web_domain ON web_visits(domain, visit_time);
"""

# (version, SQL). Append new migrations at the end; never edit existing ones.
MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2)]


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version(migrations=None) -> int:
    migrations = MIGRATIONS if migrations is None else migrations
    return max(v for v, _ in migrations)


def migrate(conn: sqlite3.Connection, migrations=None) -> int:
    """Apply pending migrations (connection must be in autocommit mode). Returns the
    resulting version. Each migration is atomic: it fully applies or not at all."""
    migrations = sorted(MIGRATIONS if migrations is None else migrations)
    version = current_version(conn)
    if version > migrations[-1][0]:
        raise RuntimeError(
            f"Database schema v{version} is newer than this TimeLens (v{migrations[-1][0]}). "
            "Update TimeLens instead of opening this database with an older version.")
    for target, sql in migrations:
        if target <= version:
            continue
        log.info("Migrating database v%d -> v%d", version, target)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(target)}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.exception("Migration to v%d failed and was rolled back", target)
            raise
        version = target
    return version


def _split(script: str):
    """Split a SQL script into statements (our scripts contain no ';' inside strings)."""
    # Remove whole-line comments first so a ';' inside a comment cannot split a statement.
    body = "\n".join(l for l in script.splitlines() if not l.strip().startswith("--"))
    for stmt in body.split(";"):
        if stmt.strip():
            yield stmt.strip()
