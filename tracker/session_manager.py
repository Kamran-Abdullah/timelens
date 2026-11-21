"""Turns raw system snapshots into non-overlapping activity segments.

This module is a pure state machine: it never calls Windows itself. It receives
`Snapshot`s (time, foreground window, last input time, locked flag) and emits events.
Because it has no Windows dependency, every time calculation can be unit-tested with a
simulated clock. (Phase 3 will persist the emitted segments to SQLite.)

Rules implemented
  * Exactly one segment is open at a time, so time can never overlap.
  * ACTIVE segments are keyed by (executable, document); switching either one closes the
    old segment and opens a new one.
  * Idle begins at `last_input + threshold` (the provisional ACTIVE session is kept until
    the threshold is reached, then split exactly there).
  * When input resumes, the IDLE segment ends at the moment of the last input.
  * LOCKED has priority over IDLE.
  * If two samples are too far apart (sleep, freeze, TimeLens not running) the open
    segment is closed at the LAST sample and a GAP event is emitted. The gap is not
    invented as activity, and idle detection cannot reach back into it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from tracker.document_detector import detect_document
from tracker.window_detector import WindowInfo

log = logging.getLogger(__name__)


class Status(str, Enum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    LOCKED = "LOCKED"
    SLEEP_OFF = "SLEEP_OFF"
    UNMONITORED = "UNMONITORED"


@dataclass(frozen=True)
class Snapshot:
    """One sample of the system. All datetimes must be timezone-aware UTC."""
    now: datetime
    window: Optional[WindowInfo]
    last_input: datetime
    locked: bool = False


@dataclass
class Segment:
    status: Status
    start: datetime
    application_name: str = ""
    executable_name: str = ""
    document_name: Optional[str] = None
    window_title: str = ""      # latest title; display/diagnostics only
    end: Optional[datetime] = None

    @property
    def key(self) -> tuple:
        return (self.status, self.executable_name.lower(), self.document_name)

    @property
    def duration_seconds(self) -> float:
        end = self.end or self.start
        return max(0.0, (end - self.start).total_seconds())


class EventKind(str, Enum):
    OPENED = "OPENED"
    CLOSED = "CLOSED"
    GAP = "GAP"


@dataclass
class SessionEvent:
    kind: EventKind
    segment: Optional[Segment] = None
    gap_start: Optional[datetime] = None
    gap_end: Optional[datetime] = None


@dataclass
class SessionManager:
    idle_threshold_seconds: float
    max_gap_seconds: float
    current: Optional[Segment] = None
    _last_tick: Optional[datetime] = field(default=None, repr=False)

    # --- public API ------------------------------------------------------------
    def process(self, snap: Snapshot) -> list[SessionEvent]:
        events: list[SessionEvent] = []

        if self.current and self._last_tick is not None:
            if (snap.now - self._last_tick).total_seconds() > self.max_gap_seconds:
                events.append(self._close(self._last_tick))
                events.append(SessionEvent(EventKind.GAP, gap_start=self._last_tick,
                                           gap_end=snap.now))
        self._last_tick = snap.now

        target, boundary = self._decide(snap)
        cur = self.current
        if cur is not None and cur.key == target.key:
            cur.window_title = target.window_title  # same segment, keep latest title
            return events

        # Never move the boundary before the open segment started or after "now".
        if cur is not None:
            boundary = max(boundary, cur.start)
        boundary = min(boundary, snap.now)

        if cur is not None:
            events.append(self._close(boundary))
        target.start = boundary
        self.current = target
        events.append(SessionEvent(EventKind.OPENED, segment=target))
        return events

    def stop(self, now: datetime) -> list[SessionEvent]:
        """Close the open segment (pause, exit). The next sample starts fresh."""
        events = [self._close(now)] if self.current else []
        self._last_tick = None
        return events

    # --- internals -------------------------------------------------------------
    def _close(self, end: datetime) -> SessionEvent:
        seg = self.current
        seg.end = max(end, seg.start)
        self.current = None
        return SessionEvent(EventKind.CLOSED, segment=seg)

    def _decide(self, snap: Snapshot) -> tuple[Segment, datetime]:
        """Work out which segment the system is in now, and when that state began."""
        cur = self.current
        started_fresh = cur is None  # first sample, or first after a gap/pause

        if snap.locked:
            return Segment(Status.LOCKED, snap.now, "Windows", "LockApp.exe"), snap.now

        idle_for = (snap.now - snap.last_input).total_seconds()
        if idle_for >= self.idle_threshold_seconds:
            # Idle started when the threshold was crossed - unless we only just began
            # observing, in which case we must not claim time we did not monitor.
            boundary = snap.now if started_fresh else (
                snap.last_input + timedelta(seconds=self.idle_threshold_seconds))
            return Segment(Status.IDLE, snap.now, "Idle", ""), boundary

        w = snap.window
        if w is None:
            seg = Segment(Status.ACTIVE, snap.now, "Desktop", "")
        else:
            doc = detect_document(w.executable_name, w.title)
            seg = Segment(Status.ACTIVE, snap.now, w.application_name, w.executable_name,
                          doc.name if doc else None, w.title)

        # Returning from IDLE: activity began at the last input, not at this sample.
        if cur is not None and cur.status is Status.IDLE:
            return seg, snap.last_input
        return seg, snap.now
