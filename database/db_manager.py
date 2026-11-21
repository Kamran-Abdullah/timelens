"""SQLite access layer for TimeLens.

Design notes
  * One writer connection guarded by a lock (the tracker thread writes), plus one
    read-only-by-convention connection per thread (UI/report threads), so heavy report
    queries never block tracking. WAL mode lets readers and the writer work together.
  * Every multi-statement change runs inside a transaction (all or nothing).
  * Sessions are written when they OPEN (end_timestamp NULL) and completed when they
    CLOSE. After a crash the leftover open session is closed using the last heartbeat,
    so a crash never loses more than HEARTBEAT_INTERVAL_SECONDS and never invents time.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import config
from database import migrations
from database.models import from_ts, to_ts

log = logging.getLogger(__name__)


class SessionAlreadyOpenError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._writer: Optional[sqlite3.Connection] = None
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []

    # --- lifecycle --------------------------------------------------------------
    def open(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        version = migrations.current_version(conn)
        if 0 < version < migrations.latest_version():
            # Upgrading existing data: keep a copy first so an update can never lose history.
            self._copy_before_upgrade(conn, version)
        migrations.migrate(conn)
        self._writer = conn
        self._seed_settings()
        return self

    def close(self) -> None:
        with self._lock:
            for r in self._readers:
                try:
                    r.close()
                except sqlite3.Error:
                    pass
            self._readers.clear()
            self._local = threading.local()
            if self._writer:
                try:
                    self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    log.warning("WAL checkpoint failed on close", exc_info=True)
                self._writer.close()
                self._writer = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None -> we control transactions explicitly with BEGIN/COMMIT.
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False,
                               timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _copy_before_upgrade(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest = self.path.with_name(f"{self.path.stem}.pre-upgrade-v{version}{self.path.suffix}")
        shutil.copy2(self.path, dest)
        log.info("Copied database to %s before upgrade", dest)

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._lock:
                self._readers.append(conn)
        return conn

    # --- helpers ---------------------------------------------------------------
    @contextmanager
    def transaction(self):
        with self._lock:
            w = self._writer
            if w is None:
                raise RuntimeError("Database is not open")
            w.execute("BEGIN IMMEDIATE")
            try:
                yield w
            except BaseException:
                w.execute("ROLLBACK")
                raise
            else:
                w.execute("COMMIT")

    def query(self, sql: str, params: Iterable | dict = ()) -> list[sqlite3.Row]:
        """Run a SELECT on this thread's own connection."""
        return self._reader().execute(sql, params).fetchall()

    # --- settings --------------------------------------------------------------
    def _seed_settings(self) -> None:
        with self.transaction() as c:
            c.executemany("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                          config.DEFAULT_SETTINGS.items())

    def get_setting(self, key: str) -> str:
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        if rows:
            return rows[0]["value"]
        if key in config.DEFAULT_SETTINGS:
            return config.DEFAULT_SETTINGS[key]
        raise KeyError(key)

    def get_bool(self, key: str) -> bool:
        return self.get_setting(key) == "1"

    def get_float(self, key: str) -> float:
        return float(self.get_setting(key))

    def set_setting(self, key: str, value) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        with self.transaction() as c:
            c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    # --- applications / documents ------------------------------------------------
    def _app_id(self, c, executable_name: str, display_name: str,
                status: str = "ACTIVE") -> Optional[int]:
        # Only real foreground use creates an application. Locked/idle/sleep rows describe
        # the system (e.g. LockApp.exe), not something the user could categorize or exclude.
        if not executable_name or status != "ACTIVE":
            return None
        c.execute("INSERT OR IGNORE INTO applications(executable_name, display_name) "
                  "VALUES (?, ?)", (executable_name, display_name))
        return c.execute("SELECT id FROM applications WHERE executable_name = ?",
                         (executable_name,)).fetchone()[0]

    def _doc_id(self, c, app_id: Optional[int], name: Optional[str]) -> Optional[int]:
        if not name or app_id is None:
            return None
        c.execute("INSERT OR IGNORE INTO documents(application_id, name) VALUES (?, ?)",
                  (app_id, name))
        return c.execute("SELECT id FROM documents WHERE application_id = ? AND name = ?",
                         (app_id, name)).fetchone()[0]

    def is_excluded(self, executable_name: str) -> bool:
        rows = self.query("SELECT excluded FROM applications WHERE executable_name = ?",
                          (executable_name,))
        return bool(rows and rows[0]["excluded"])

    def set_excluded(self, executable_name: str, excluded: bool) -> None:
        with self.transaction() as c:
            self._app_id(c, executable_name, executable_name)
            c.execute("UPDATE applications SET excluded = ? WHERE executable_name = ?",
                      (int(excluded), executable_name))

    # --- sessions -------------------------------------------------------------
    def open_session(self, start: datetime, status: str, application_name: str,
                     executable_name: str = "", document_name: Optional[str] = None) -> int:
        """Record the start of a session and return its id. Only one may be open."""
        with self.transaction() as c:
            existing = c.execute("SELECT id FROM activity_sessions WHERE end_timestamp IS NULL"
                                 ).fetchone()
            if existing:
                raise SessionAlreadyOpenError(f"session {existing[0]} is still open")
            app_id = self._app_id(c, executable_name, application_name, status)
            doc_id = self._doc_id(c, app_id, document_name)
            try:
                cur = c.execute(
                    "INSERT INTO activity_sessions(start_timestamp, application_id, document_id,"
                    " application_name, executable_name, document_name, activity_status)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (to_ts(start), app_id, doc_id, application_name, executable_name,
                     document_name, status))
            except sqlite3.IntegrityError:
                # Same state at the same instant already stored: do not duplicate.
                raise SessionAlreadyOpenError("duplicate session (same start and state)")
            return cur.lastrowid

    def close_session(self, session_id: int, end: datetime) -> float:
        """Finish a session; returns its duration. If the clock went backwards the
        end is clamped to the start (duration 0) rather than storing negative time."""
        with self.transaction() as c:
            row = c.execute("SELECT start_timestamp FROM activity_sessions WHERE id = ? "
                            "AND end_timestamp IS NULL", (session_id,)).fetchone()
            if row is None:
                return 0.0
            start = from_ts(row[0])
            end = max(end, start)
            duration = (end - start).total_seconds()
            c.execute("UPDATE activity_sessions SET end_timestamp = ?, duration_seconds = ? "
                      "WHERE id = ?", (to_ts(end), duration, session_id))
            return duration

    def add_closed_session(self, start: datetime, end: datetime, status: str,
                           application_name: str, executable_name: str = "",
                           document_name: Optional[str] = None) -> Optional[int]:
        """Insert a finished session in one step (e.g. a sleep gap). Duplicates are ignored."""
        end = max(end, start)
        with self.transaction() as c:
            app_id = self._app_id(c, executable_name, application_name, status)
            doc_id = self._doc_id(c, app_id, document_name)
            cur = c.execute(
                "INSERT OR IGNORE INTO activity_sessions(start_timestamp, end_timestamp,"
                " application_id, document_id, application_name, executable_name,"
                " document_name, activity_status, duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
                (to_ts(start), to_ts(end), app_id, doc_id, application_name, executable_name,
                 document_name, status, (end - start).total_seconds()))
            return cur.lastrowid if cur.rowcount else None

    # --- crash safety ------------------------------------------------------------
    def heartbeat(self, now: datetime) -> None:
        """Remember 'TimeLens was alive at this moment' (a cheap single-row write)."""
        self.set_setting("last_heartbeat", to_ts(now))

    def recover_open_sessions(self, now: datetime) -> int:
        """Close any session left open by a crash/power loss at the last heartbeat
        (or at its own start if there is none). Time after that is NOT invented."""
        with self.transaction() as c:
            hb = c.execute("SELECT value FROM settings WHERE key = 'last_heartbeat'").fetchone()
            heartbeat = from_ts(hb[0]) if hb else None
            rows = c.execute("SELECT id, start_timestamp FROM activity_sessions "
                             "WHERE end_timestamp IS NULL").fetchall()
            for r in rows:
                start = from_ts(r["start_timestamp"])
                end = min(max(heartbeat or start, start), now)
                c.execute("UPDATE activity_sessions SET end_timestamp = ?, duration_seconds = ?,"
                          " recovered = 1 WHERE id = ?",
                          (to_ts(end), (end - start).total_seconds(), r["id"]))
            if rows:
                c.execute("INSERT INTO system_events(timestamp, event_type, detail) VALUES (?,?,?)",
                          (to_ts(now), "crash_recovered", f"{len(rows)} open session(s) closed"))
            return len(rows)

    def log_event(self, when: datetime, event_type: str, detail: str = "") -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO system_events(timestamp, event_type, detail) VALUES (?,?,?)",
                      (to_ts(when), event_type, detail))

    # --- privacy: deleting history -----------------------------------------------
    def delete_sessions_between(self, start: datetime, end: datetime) -> int:
        with self.transaction() as c:
            n = c.execute("DELETE FROM activity_sessions WHERE start_timestamp >= ? "
                          "AND start_timestamp < ? AND end_timestamp IS NOT NULL",
                          (to_ts(start), to_ts(end))).rowcount
            c.execute("DELETE FROM web_visits WHERE visit_time >= ? AND visit_time < ?",
                      (to_ts(start), to_ts(end)))        # website history is part of "history"
            c.execute("DELETE FROM daily_summaries")  # cache; rebuilt on demand
            return n

    def delete_web_history(self) -> int:
        """Remove every stored website visit (the browser's own history is not touched)."""
        with self.transaction() as c:
            return c.execute("DELETE FROM web_visits").rowcount

    def delete_application_history(self, executable_name: str) -> int:
        with self.transaction() as c:
            n = c.execute("DELETE FROM activity_sessions WHERE executable_name = ? "
                          "AND end_timestamp IS NOT NULL", (executable_name,)).rowcount
            c.execute("DELETE FROM documents WHERE application_id IN "
                      "(SELECT id FROM applications WHERE executable_name = ?)",
                      (executable_name,))
            c.execute("DELETE FROM daily_summaries")
            return n

    def integrity_ok(self) -> bool:
        return self._reader().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
