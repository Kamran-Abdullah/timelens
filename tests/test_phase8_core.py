"""Startup shortcut, real Windows message handling, and sleep/lock/shutdown accounting."""
import time
from datetime import datetime, timedelta, timezone

import pytest
import win32con
import win32gui

from analytics import time_calc as tc
from database.db_manager import Database
from services.startup_manager import SHORTCUT_NAME, StartupManager, default_startup_dir
from tracker import power_monitor as pm
from tracker.engine import TrackingEngine
from tracker.power_monitor import PowerMonitor, SystemEvent, SystemEventKind as K
from tracker.session_manager import Snapshot
from tracker.window_detector import WindowInfo

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
XL = WindowInfo(1, 1, "A.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
NOTEPAD = r"C:\Windows\System32\notepad.exe"


# ---------------------------------------------------------------- startup folder
def test_startup_shortcut_enable_disable(tmp_path):
    mgr = StartupManager(tmp_path / "Startup", (NOTEPAD, '"C:\\x\\main.py" --background', r"C:\x"))
    assert not mgr.is_enabled()
    mgr.enable()
    assert mgr.is_enabled() and mgr.shortcut_path.name == SHORTCUT_NAME
    assert mgr.points_to_current()
    mgr.disable()
    assert not mgr.is_enabled()
    mgr.disable()                                   # disabling twice is harmless


def test_startup_sync_repairs_stale_shortcut(tmp_path):
    StartupManager(tmp_path / "S", (NOTEPAD, "--old", r"C:\Windows")).enable()
    new = StartupManager(tmp_path / "S", (NOTEPAD, "--background", r"C:\Windows"))
    assert not new.points_to_current()
    new.sync(True)
    assert new.points_to_current()
    new.sync(False)
    assert not new.is_enabled()


def test_startup_default_location_is_users_own_startup_folder():
    d = default_startup_dir()
    assert d.name == "Startup" and "Start Menu" in d.parts and "Programs" in d.parts


# --------------------------------------------- real Windows messages -> events
@pytest.fixture
def monitor():
    events = []
    m = PowerMonitor(events.append)
    assert m.start()
    yield m, events
    m.stop()


def send(m, msg, wparam, lparam=0):
    return win32gui.SendMessage(m.hwnd, msg, wparam, lparam)


def test_lock_unlock_messages_become_events(monitor):
    m, events = monitor
    send(m, pm.WM_WTSSESSION_CHANGE, pm.WTS_SESSION_LOCK, 1)
    send(m, pm.WM_WTSSESSION_CHANGE, pm.WTS_SESSION_UNLOCK, 1)
    assert [e.kind for e in events] == [K.LOCK, K.UNLOCK]
    assert all(e.when.tzinfo is not None for e in events)


def test_sleep_and_wake_messages_become_events_once(monitor):
    m, events = monitor
    send(m, pm.WM_POWERBROADCAST, pm.PBT_APMSUSPEND)
    send(m, pm.WM_POWERBROADCAST, pm.PBT_APMRESUMEAUTOMATIC)
    send(m, pm.WM_POWERBROADCAST, pm.PBT_APMRESUMESUSPEND)     # duplicate resume is ignored
    assert [e.kind for e in events] == [K.SUSPEND, K.RESUME]


def test_shutdown_and_logoff_messages(monitor):
    m, events = monitor
    assert send(m, win32con.WM_QUERYENDSESSION, 0, 0) == 1      # never blocks shutdown
    send(m, win32con.WM_ENDSESSION, 0, 0)                       # cancelled shutdown: no event
    send(m, win32con.WM_ENDSESSION, 1, 0)
    send(m, win32con.WM_ENDSESSION, 1, pm.ENDSESSION_LOGOFF)
    assert [e.kind for e in events] == [K.SHUTDOWN, K.LOGOFF]


def test_monitor_stops_cleanly():
    m = PowerMonitor(lambda e: None)
    assert m.start()
    m.stop()
    assert not m._thread.is_alive()


# ------------------------------------------------ engine accounting of events
class Rig:
    def __init__(self, db):
        self.t, self.locked, self.last_input = T0, False, T0
        self.engine = TrackingEngine(db, snapshot_provider=self._snap, clock=lambda: self.t)
        self.engine.start(run_thread=False)

    def _snap(self):
        if not self.locked:
            self.last_input = self.t
        return Snapshot(self.t, XL, self.last_input, self.locked)

    def run(self, seconds):
        end = self.t + timedelta(seconds=seconds)
        while self.t < end:
            self.engine.monitor.tick()
            self.t += timedelta(seconds=2)

    def event(self, kind):
        self.engine.handle_system_event(SystemEvent(kind, self.t))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p8.db").open()
    yield d
    d.close()


def totals(db, end):
    return tc.get_totals(db, T0, end, now=NOW)


def test_sleep_is_recorded_exactly_and_is_not_idle_or_active(db):
    r = Rig(db)
    r.run(600)                                   # 10 min working
    r.event(K.SUSPEND)
    r.t += timedelta(hours=2)                    # asleep 2 h
    r.event(K.RESUME)
    r.run(300)
    r.engine.stop()
    t = totals(db, r.t)
    assert t.sleep_off == 7200 and t.idle == 0
    assert t.active == 600 + 300
    assert t.unmonitored == 0 and t.accounted == t.period_seconds
    kinds = [e["event_type"] for e in db.query("SELECT event_type FROM system_events")]
    assert [k for k in kinds if k in ("sleep", "wake")] == ["sleep", "wake"]


def test_samples_while_suspended_are_ignored(db):
    r = Rig(db)
    r.run(60)
    r.event(K.SUSPEND)
    r.run(60)                                    # stray ticks during sleep must not count
    r.event(K.RESUME)
    r.engine.stop()
    assert totals(db, r.t).active == 60


def test_sleep_while_user_paused_is_not_recorded(db):
    r = Rig(db)
    r.run(60)
    r.engine.pause()
    r.event(K.SUSPEND)
    r.t += timedelta(hours=1)
    r.event(K.RESUME)
    r.engine.stop()
    assert totals(db, r.t).sleep_off == 0


def test_lock_event_marks_locked_immediately(db):
    r = Rig(db)
    r.run(60)
    r.locked = True
    r.event(K.LOCK)
    assert r.engine.monitor._lock_hint_until is not None
    r.run(120)
    r.locked = False
    r.event(K.UNLOCK)
    assert r.engine.monitor._lock_hint_until is None
    r.run(60)
    r.engine.stop()
    t = totals(db, r.t)
    assert t.locked == 120 and t.active == 120


def test_shutdown_then_restart_counts_powered_off(db):
    r = Rig(db)
    r.run(120)
    r.event(K.SHUTDOWN)                          # Windows shutting down
    assert not r.engine.running
    assert db.query("SELECT event_type FROM system_events ORDER BY id DESC LIMIT 1")[0][0] == "os_shutdown"
    boot = TrackingEngine(db, clock=lambda: T0 + timedelta(hours=10), snapshot_provider=lambda: None)
    boot.start(run_thread=False)                 # TimeLens starts at next Windows sign-in
    boot.stop()
    t = tc.get_totals(db, T0, T0 + timedelta(hours=10), now=NOW)
    assert t.active == 120
    assert t.sleep_off == 10 * 3600 - 120        # off from shutdown until the next start
    assert t.unmonitored == 0


def test_normal_exit_then_restart_is_unmonitored_not_off(db):
    r = Rig(db)
    r.run(120)
    r.engine.stop()                              # user chose Exit TimeLens
    boot = TrackingEngine(db, clock=lambda: T0 + timedelta(hours=5), snapshot_provider=lambda: None)
    boot.start(run_thread=False)
    boot.stop()
    t = tc.get_totals(db, T0, T0 + timedelta(hours=5), now=NOW)
    assert t.sleep_off == 0 and t.unmonitored == 5 * 3600 - 120


def test_logoff_stops_tracking_without_claiming_power_off(db):
    r = Rig(db)
    r.run(60)
    r.event(K.LOGOFF)
    assert not r.engine.running
    boot = TrackingEngine(db, clock=lambda: T0 + timedelta(hours=1), snapshot_provider=lambda: None)
    boot.start(run_thread=False)
    boot.stop()
    assert tc.get_totals(db, T0, T0 + timedelta(hours=1), now=NOW).sleep_off == 0


def test_engine_with_real_power_monitor_lifecycle(db):
    e = TrackingEngine(db, poll_interval=0.05, heartbeat_interval=0.05)
    e.start()
    assert e.start_power_monitor()
    win32gui.SendMessage(e.power.hwnd, pm.WM_POWERBROADCAST, pm.PBT_APMSUSPEND, 0)
    time.sleep(0.2)
    assert e.monitor.suspended
    win32gui.SendMessage(e.power.hwnd, pm.WM_POWERBROADCAST, pm.PBT_APMRESUMEAUTOMATIC, 0)
    assert not e.monitor.suspended
    e.stop()
    assert e.power is None
    assert db.query("SELECT 1 FROM activity_sessions WHERE activity_status='SLEEP_OFF'")
