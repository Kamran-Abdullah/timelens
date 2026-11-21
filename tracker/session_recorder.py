"""Writes SessionManager events to the SQLite database.

The monitor thread calls `on_event`. A database failure is logged and never raised, so a
disk problem cannot stop the tracker; the affected session is simply not stored.

Privacy: if the user excluded an application, its time is still counted (so Active/Idle
totals stay honest) but it is stored under the anonymous name "Excluded Application"
with no executable and no document.
"""
from __future__ import annotations

import logging
from typing import Optional

from database.db_manager import Database, SessionAlreadyOpenError
from tracker.session_manager import EventKind, SessionEvent

log = logging.getLogger(__name__)

EXCLUDED_LABEL = "Excluded Application"


class SessionRecorder:
    def __init__(self, db: Database):
        self.db = db
        self.current_db_id: Optional[int] = None

    def on_event(self, ev: SessionEvent) -> None:
        try:
            if ev.kind is EventKind.OPENED:
                self._opened(ev)
            elif ev.kind is EventKind.CLOSED:
                self._closed(ev)
            elif ev.kind is EventKind.GAP:
                # Not stored as a session: uncovered time is reported as "unmonitored".
                # The event is logged so Phase 8 can label it sleep/off from power events.
                secs = (ev.gap_end - ev.gap_start).total_seconds()
                self.db.log_event(ev.gap_end, "sample_gap",
                                  f"{ev.gap_start.isoformat()} .. {ev.gap_end.isoformat()} "
                                  f"({secs:.0f}s)")
        except Exception:
            log.exception("Could not record %s event", ev.kind.value)

    def _opened(self, ev: SessionEvent) -> None:
        s = ev.segment
        name, exe, doc = s.application_name, s.executable_name, s.document_name
        if exe and self.db.is_excluded(exe):
            name, exe, doc = EXCLUDED_LABEL, "", None
        try:
            self.current_db_id = self.db.open_session(s.start, s.status.value, name, exe, doc)
        except SessionAlreadyOpenError:
            # A stale open row exists (should not happen). Close it where this one starts.
            stale = self.db.query("SELECT id FROM activity_sessions WHERE end_timestamp IS NULL")
            if not stale:
                log.warning("Duplicate session ignored: %s at %s", name, s.start)
                self.current_db_id = None
                return
            self.db.close_session(stale[0]["id"], s.start)
            self.current_db_id = self.db.open_session(s.start, s.status.value, name, exe, doc)

    def _closed(self, ev: SessionEvent) -> None:
        if self.current_db_id is not None:
            self.db.close_session(self.current_db_id, ev.segment.end)
            self.current_db_id = None
