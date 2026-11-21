"""Chronological activity timeline built from activity_sessions.

Time with no recorded session inside the observed period is shown explicitly as
UNMONITORED (TimeLens was not running or could not tell), never silently skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from analytics.time_calc import clipped_sessions
from database.db_manager import Database
from database.models import from_ts

MIN_GAP_SECONDS = 1.0     # ignore sub-second rounding gaps between sessions
MAX_ROWS = 5000


@dataclass
class TimelineEntry:
    start: datetime
    end: datetime
    status: str
    application: str = ""
    document: Optional[str] = None
    executable: str = ""

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def label(self) -> str:
        """What the user did: document, application, or the system state."""
        if self.status == "ACTIVE":
            return self.document or self.application
        return {"IDLE": "Idle", "LOCKED": "Locked", "SLEEP_OFF": "Sleep / Off",
                "UNMONITORED": "Unmonitored"}[self.status]

    @property
    def detail(self) -> str:
        if self.status == "ACTIVE":
            return self.application if self.document else ""
        return {"IDLE": "No user input", "LOCKED": "Windows",
                "SLEEP_OFF": "Computer unavailable",
                "UNMONITORED": "TimeLens was not recording"}[self.status]

    def _key(self):
        return (self.status, self.application, self.document)


def build_timeline(db: Database, start: datetime, end: datetime,
                   application: Optional[str] = None, now: Optional[datetime] = None,
                   max_rows: int = MAX_ROWS) -> tuple[list[TimelineEntry], bool]:
    """Entries in chronological order, plus whether older rows were dropped (truncated).
    With an `application` filter only that application's ACTIVE entries are returned."""
    now = now or datetime.now(timezone.utc)
    first = db.query("SELECT MIN(start_timestamp) AS t FROM activity_sessions")[0]["t"]
    if first is None:
        return [], False
    start = max(start, from_ts(first))          # nothing is claimed before TimeLens existed
    eff_end = min(end, now)
    entries: list[TimelineEntry] = []
    cursor = start

    def add(e: TimelineEntry) -> None:
        if entries and entries[-1]._key() == e._key() and \
                (e.start - entries[-1].end).total_seconds() <= MIN_GAP_SECONDS:
            entries[-1].end = e.end
        else:
            entries.append(e)

    for row, s, e in clipped_sessions(db, start, end, now):
        if application:
            if row["activity_status"] == "ACTIVE" and row["application_name"] == application:
                add(TimelineEntry(s, e, "ACTIVE", row["application_name"], row["document_name"],
                                  row["executable_name"]))
            continue
        if (s - cursor).total_seconds() > MIN_GAP_SECONDS:
            add(TimelineEntry(cursor, s, "UNMONITORED"))
        add(TimelineEntry(s, e, row["activity_status"], row["application_name"],
                          row["document_name"], row["executable_name"]))
        cursor = e
    if not application and (eff_end - cursor).total_seconds() > MIN_GAP_SECONDS and eff_end > start:
        add(TimelineEntry(cursor, eff_end, "UNMONITORED"))

    truncated = len(entries) > max_rows
    return (entries[-max_rows:] if truncated else entries), truncated


def application_names(db: Database) -> list[str]:
    """Applications that have recorded active time (for the filter drop-down)."""
    rows = db.query("SELECT DISTINCT application_name FROM activity_sessions "
                    "WHERE activity_status = 'ACTIVE' ORDER BY application_name COLLATE NOCASE")
    return [r["application_name"] for r in rows]
