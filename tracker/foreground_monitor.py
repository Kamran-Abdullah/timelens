"""Background sampling thread: reads Windows every ~2 s and feeds SessionManager.

The monitor runs in its own daemon thread so a GUI (Phase 4) never blocks on it.
Listeners are called from that thread; GUI code must hand results over via Qt signals.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import config
from tracker.idle_detector import get_idle_seconds
from tracker.lock_detector import is_session_locked
from tracker.session_manager import SessionEvent, SessionManager, Snapshot
from tracker.window_detector import get_foreground_window

log = logging.getLogger(__name__)

Listener = Callable[[SessionEvent], None]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ForegroundMonitor:
    def __init__(self, idle_threshold_seconds: float = config.DEFAULT_IDLE_THRESHOLD_SECONDS,
                 poll_interval: float = config.POLL_INTERVAL_SECONDS,
                 snapshot_provider: Optional[Callable[[], Snapshot]] = None,
                 clock: Callable[[], datetime] = utcnow):
        self.poll_interval = poll_interval
        self._clock = clock  # used when closing sessions on pause/stop (injectable for tests)
        self.manager = SessionManager(idle_threshold_seconds, config.MAX_SAMPLE_GAP_SECONDS)
        self._snapshot_provider = snapshot_provider or self.take_snapshot
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._paused = False
        self._suspended = False          # computer asleep (set by power events)
        self._lock_hint_until: Optional[datetime] = None   # lock event seen; poll may lag
        self._wake = threading.Event()   # lets an event trigger an immediate sample
        # Serialises sampling with pause/stop so a segment is never closed twice or late.
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._published = None           # lock-free copy of the current segment (see current_segment)

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def set_idle_threshold(self, seconds: float) -> None:
        self.manager.idle_threshold_seconds = seconds

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def suspended(self) -> bool:
        return self._suspended

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="TimeLensMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop monitoring completely and close the open segment."""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 5)
        with self._lock:
            self._emit(self.manager.stop(self._clock()))
            self._publish()

    def pause(self) -> None:
        """Close the open segment immediately (not at the next sample) and stop sampling."""
        with self._lock:
            if not self._paused:
                self._paused = True
                self._emit(self.manager.stop(self._clock()))
                self._publish()

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def suspend(self, when: datetime) -> None:
        """The computer is going to sleep: end the open segment exactly at `when`."""
        with self._lock:
            self._suspended = True
            self._emit(self.manager.stop(when))
            self._publish()

    def unsuspend(self) -> None:
        with self._lock:
            self._suspended = False
        self.request_tick()

    def hint_locked(self, locked: bool, when: datetime) -> None:
        """A lock/unlock event arrived: bridge the up-to-2s polling lag, then sample now."""
        self._lock_hint_until = when + timedelta(seconds=3) if locked else None
        self.request_tick()

    def request_tick(self) -> None:
        self._wake.set()

    def current_segment(self):
        """A copy of the segment being tracked right now (None if none/paused).

        LOCK-FREE on purpose: the GUI calls this every second, and it must never wait for the
        tracker thread (which may be inside a slow Windows call or a database write). The copy
        is published by the tracker after every change (see _publish)."""
        return self._published

    def _publish(self) -> None:
        """Remember an immutable copy of the current segment for current_segment()."""
        cur = self.manager.current
        self._published = dataclasses.replace(cur) if cur else None

    # --- sampling ---------------------------------------------------------------
    def take_snapshot(self) -> Snapshot:
        now = utcnow()
        window = get_foreground_window()
        locked = is_session_locked(window.executable_name if window else "")
        if not locked and self._lock_hint_until and now < self._lock_hint_until:
            locked = True   # lock event seen a moment ago; the desktop switch may lag
        last_input = now - timedelta(seconds=get_idle_seconds())
        return Snapshot(now, window, last_input, locked)

    def tick(self) -> None:
        """Take one sample and process it (the thread calls this every interval).

        The sample is taken OUTSIDE the lock: reading the foreground window is a blocking
        Windows call, and holding the lock across it let the GUI thread and this thread wait
        for each other forever. The lock only guards the quick in-memory bookkeeping."""
        if self._paused or self._suspended:
            return
        snapshot = self._snapshot_provider()
        with self._lock:
            if self._paused or self._suspended:        # changed while we were sampling
                return
            self._emit(self.manager.process(snapshot))
            self._publish()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Sampling failed; will retry")
            self._wake.wait(self.poll_interval)   # also woken early by system events
            self._wake.clear()

    def _emit(self, events: list[SessionEvent]) -> None:
        for ev in events:
            for fn in self._listeners:
                try:
                    fn(ev)
                except Exception:
                    log.exception("Listener failed")
