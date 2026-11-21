"""Tracker -> database integration, driven by a scripted clock (no real waiting)."""
from datetime import datetime, timedelta, timezone

import pytest

from analytics import time_calc as tc
from database.db_manager import Database
from services.single_instance import SingleInstance
from tracker.engine import TrackingEngine
from tracker.session_manager import Snapshot
from tracker.window_detector import WindowInfo

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def win(exe, app, title):
    return WindowInfo(1, 1, title, exe, app)


XL_A = win("EXCEL.EXE", "Microsoft Excel", "A.xlsx - Excel")
XL_B = win("EXCEL.EXE", "Microsoft Excel", "B.xlsx - Excel")
CHROME = win("chrome.exe", "Google Chrome", "Private page - Google Chrome")


class Rig:
    """Engine with a fake clock; `step()` feeds one 2-second sample."""

    def __init__(self, db):
        self.t = T0
        self.last_input = T0
        self.window = XL_A
        self.locked = False
        self.input_active = True
        self.engine = TrackingEngine(db, snapshot_provider=self._snap, clock=lambda: self.t)

    def _snap(self):
        if self.input_active and not self.locked:
            self.last_input = self.t
        return Snapshot(self.t, self.window, self.last_input, self.locked)

    def start(self):
        return self.engine.start(run_thread=False)

    def run(self, seconds, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        end = self.t + timedelta(seconds=seconds)
        while self.t < end:
            self.engine.monitor.tick()
            self.t += timedelta(seconds=2)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "i.db").open()
    yield d
    d.close()


def rows(db):
    return db.query("SELECT * FROM activity_sessions ORDER BY start_timestamp")


def test_full_day_story_saved_and_reconciled(db):
    r = Rig(db)
    r.start()
    r.run(1800, window=XL_A)                       # 30 min in A.xlsx
    r.run(600, window=XL_B)                        # 10 min in B.xlsx
    r.run(300, window=CHROME)                      # 5 min Chrome
    r.run(900, input_active=False)                 # 15 min no input -> 5 active + 10 idle
    r.run(120, input_active=True, locked=True)     # 2 min locked
    r.run(60, locked=False, window=XL_A)           # unlock, back to work
    r.engine.stop()
    t = tc.get_totals(db, T0, r.t, now=NOW)
    # Last input is the sample 2 s before the 15-min quiet period, so idle starts 298 s in
    # (5 min threshold) and runs until the first locked sample 900 s in: 602 s.
    assert t.idle == 602 and t.locked == 120
    assert t.active == 1800 + 600 + 300 + 298 + 60
    assert t.unmonitored == 0 and t.accounted == t.period_seconds
    docs = {(a, d): s for a, d, s in tc.document_totals(db, T0, r.t, now=NOW)}
    assert docs[("Microsoft Excel", "A.xlsx")] == 1860
    assert docs[("Microsoft Excel", "B.xlsx")] == 600
    assert dict(tc.application_totals(db, T0, r.t, NOW))["Google Chrome"] == 598
    assert not db.query("SELECT 1 FROM activity_sessions WHERE end_timestamp IS NULL")


def test_document_totals_exact(db):
    r = Rig(db)
    r.start()
    r.run(600, window=XL_A)
    r.run(300, window=XL_B)
    r.run(120, window=XL_A)
    r.engine.stop()
    docs = {(a, d): s for a, d, s in tc.document_totals(db, T0, r.t, now=NOW)}
    assert docs[("Microsoft Excel", "A.xlsx")] == 720
    assert docs[("Microsoft Excel", "B.xlsx")] == 300


def test_sleep_gap_not_recorded_as_activity(db):
    r = Rig(db)
    r.start()
    r.run(120)
    r.t += timedelta(hours=1)                      # laptop asleep
    r.run(120)
    r.engine.stop()
    t = tc.get_totals(db, T0, r.t, now=NOW)
    assert t.active == 118 + 120      # up to the last sample before sleep, then after wake
    assert t.unmonitored == 3602
    assert t.idle == 0
    assert db.query("SELECT 1 FROM system_events WHERE event_type='sample_gap'")


def test_pause_closes_immediately_and_resume_continues(db):
    r = Rig(db)
    r.start()
    r.run(60)
    r.engine.pause()
    assert r.engine.paused
    assert not db.query("SELECT 1 FROM activity_sessions WHERE end_timestamp IS NULL")
    r.run(600)                                     # paused: nothing recorded
    r.engine.resume()
    r.run(60)
    r.engine.stop()
    t = tc.get_totals(db, T0, r.t, now=NOW)
    assert t.active == 120 and t.unmonitored == 600
    kinds = [e["event_type"] for e in db.query("SELECT event_type FROM system_events")]
    assert {"startup", "pause", "resume", "shutdown"} <= set(kinds)


def test_crash_then_restart_recovers_without_inventing_time(db):
    r = Rig(db)
    r.start()
    r.run(300)
    db.heartbeat(r.t)                              # last sign of life
    # crash: no engine.stop(); an hour passes; TimeLens starts again
    r2 = Rig(db)
    r2.t = T0 + timedelta(hours=1, minutes=5)
    assert r2.start() == 1
    r2.run(60)
    r2.engine.stop()
    t = tc.get_totals(db, T0, r2.t, now=NOW)
    assert t.active == 300 + 60
    assert t.unmonitored == 3600
    assert [x["recovered"] for x in rows(db)][0] == 1


def test_excluded_application_hidden_but_time_kept(db):
    db.set_excluded("chrome.exe", True)
    r = Rig(db)
    r.start()
    r.run(120, window=CHROME)
    r.engine.stop()
    row = rows(db)[0]
    assert row["application_name"] == "Excluded Application"
    assert row["executable_name"] == "" and row["document_name"] is None
    assert not db.query("SELECT 1 FROM applications WHERE executable_name='chrome.exe' AND 0")
    assert tc.get_totals(db, T0, r.t, now=NOW).active == 120


def test_browser_page_title_never_stored(db):
    r = Rig(db)
    r.start()
    r.run(60, window=CHROME)
    r.engine.stop()
    stored = " ".join(str(v) for row in rows(db) for v in tuple(row))
    assert "Private page" not in stored


def test_idle_threshold_setting_is_applied(db):
    db.set_setting("idle_threshold_seconds", 60)
    r = Rig(db)
    r.start()
    r.run(30)
    r.run(200, input_active=False)
    r.engine.stop()
    t = tc.get_totals(db, T0, r.t, now=NOW)
    assert t.active == 88 and t.idle == 142    # idle begins 60 s after the last input (28 s)


def test_stop_is_idempotent_and_closes(db):
    r = Rig(db)
    r.start()
    r.run(20)
    r.engine.stop()
    r.engine.stop()
    assert not r.engine.running
    assert len([e for e in db.query("SELECT 1 FROM system_events WHERE event_type='shutdown'")]) == 1


def test_real_thread_start_and_stop(db):
    engine = TrackingEngine(db, poll_interval=0.05, heartbeat_interval=0.05)
    engine.start()
    import time
    time.sleep(0.4)
    engine.stop()
    assert not db.query("SELECT 1 FROM activity_sessions WHERE end_timestamp IS NULL")
    assert db.query("SELECT 1 FROM activity_sessions")
    assert db.get_setting("last_heartbeat")


def test_single_instance_lock():
    name = "Local\\TimeLens.Test.%d" % id(object())
    a, b = SingleInstance(name), SingleInstance(name)
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
