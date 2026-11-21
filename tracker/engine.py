"""TrackingEngine: the one object the app (and later the GUI/tray) talks to.

It wires together database, monitor and recorder and owns the lifecycle:

    start()  -> recover from a crash, log 'startup', begin sampling, start the heartbeat
    pause()  -> close the open session now and stop sampling
    resume() -> continue sampling (a new session starts)
    stop()   -> close the open session, log 'shutdown', stop all threads

Exiting TimeLens must call stop(): after it returns nothing is monitored.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from database.db_manager import Database
from database.models import from_ts
from tracker.foreground_monitor import ForegroundMonitor
from tracker.power_monitor import PowerMonitor, SystemEvent, SystemEventKind
from tracker.session_manager import Snapshot
from tracker.session_recorder import SessionRecorder

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TrackingEngine:
    def __init__(self, db: Database, snapshot_provider: Optional[Callable[[], Snapshot]] = None,
                 clock: Callable[[], datetime] = _utcnow,
                 poll_interval: float = config.POLL_INTERVAL_SECONDS,
                 heartbeat_interval: float = config.HEARTBEAT_INTERVAL_SECONDS):
        self.db = db
        self.clock = clock
        self.recorder = SessionRecorder(db)
        self.monitor = ForegroundMonitor(db.get_float("idle_threshold_seconds"), poll_interval,
                                         snapshot_provider, clock)
        self.monitor.add_listener(self.recorder.on_event)
        self._heartbeat_interval = heartbeat_interval
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._running = False
        self._threaded = True
        self._suspended_at: Optional[datetime] = None
        self.power: Optional[PowerMonitor] = None
        self._want_power = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self.monitor.paused

    def start(self, run_thread: bool = True) -> int:
        """Start tracking. Returns how many sessions crash recovery had to close.
        run_thread=False lets tests drive sampling manually via `monitor.tick()`."""
        now = self.clock()
        self._threaded = run_thread
        recovered = self.db.recover_open_sessions(now)
        self._record_poweroff_gap(now)
        if recovered:
            log.warning("Recovered %d session(s) left open by a previous crash", recovered)
        self.db.log_event(now, "startup")
        self.db.heartbeat(now)
        self.apply_settings()
        self._running = True
        if run_thread:
            self.monitor.start()
            self._hb_stop.clear()
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                               name="TimeLensHeartbeat")
            self._hb_thread.start()
        return recovered

    def start_power_monitor(self) -> bool:
        """Listen for real lock/unlock/sleep/wake/shutdown events (Windows only)."""
        self._want_power = True
        if self.power is None:
            self.power = PowerMonitor(self.handle_system_event)
        return self.power.start()

    def restart(self) -> int:
        """Stop and start again (used after restoring a database backup)."""
        want_power = self._want_power
        self.stop()
        self.recorder.current_db_id = None
        recovered = self.start(run_thread=self._threaded)
        if want_power:
            self.start_power_monitor()
        return recovered

    def _record_poweroff_gap(self, now: datetime) -> None:
        """If the last thing we saw was Windows shutting down, the computer was OFF until now."""
        rows = self.db.query("SELECT event_type, timestamp FROM system_events ORDER BY id DESC LIMIT 1")
        if rows and rows[0]["event_type"] == "os_shutdown":
            off_at = from_ts(rows[0]["timestamp"])
            if now > off_at:
                self.db.add_closed_session(off_at, now, "SLEEP_OFF", "Powered off")

    # --- Windows events -------------------------------------------------------------
    def handle_system_event(self, ev: SystemEvent) -> None:
        """React to a lock / unlock / sleep / wake / shutdown event at its exact time."""
        kind, when = ev.kind, ev.when
        if not self._running:
            return
        if kind in (SystemEventKind.LOCK, SystemEventKind.UNLOCK):
            self.monitor.hint_locked(kind is SystemEventKind.LOCK, when)
        elif kind is SystemEventKind.SUSPEND:
            if not self.monitor.paused:           # a user-paused period is not recorded
                self._suspended_at = when
                self.monitor.suspend(when)
                self.db.log_event(when, "sleep")
        elif kind is SystemEventKind.RESUME:
            start, self._suspended_at = self._suspended_at, None
            if start is not None and when > start:
                self.db.add_closed_session(start, when, "SLEEP_OFF", "Sleep / Off")
                self.db.log_event(when, "wake")
            if self.monitor.suspended:
                self.monitor.unsuspend()
        elif kind is SystemEventKind.SHUTDOWN:
            self.stop()                           # close the open session now
            self.db.log_event(when, "os_shutdown")   # last event: next start knows the PC was off
        elif kind is SystemEventKind.LOGOFF:
            self.stop()

    def apply_settings(self) -> None:
        """Re-read settings that can change while running (call after Settings is saved)."""
        self.monitor.set_idle_threshold(self.db.get_float("idle_threshold_seconds"))

    def pause(self) -> None:
        if not self.monitor.paused:
            self.monitor.pause()
            self.db.log_event(self.clock(), "pause")

    def resume(self) -> None:
        if self.monitor.paused:
            self.monitor.resume()
            self.db.log_event(self.clock(), "resume")

    def stop(self) -> None:
        if not self._running:
            return
        if self.power:
            self.power.stop()
            self.power = None
        self._hb_stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=5)
        self.monitor.stop()           # closes the open session via the recorder
        now = self.clock()
        self.db.log_event(now, "shutdown")
        self.db.heartbeat(now)
        self._running = False

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self._heartbeat_interval):
            try:
                self.db.heartbeat(self.clock())
            except Exception:
                log.exception("Heartbeat write failed")
