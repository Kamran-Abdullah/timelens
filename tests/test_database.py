import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from analytics import time_calc as tc
from database import migrations
from database.db_manager import Database, SessionAlreadyOpenError
from database.models import from_ts, to_ts
from services import backup_manager as bm

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def mins(n):
    return timedelta(minutes=n)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db").open()
    yield d
    d.close()


def add(db, start_min, end_min, status="ACTIVE", app="Microsoft Excel", exe="EXCEL.EXE", doc=None):
    return db.add_closed_session(T0 + mins(start_min), T0 + mins(end_min), status, app, exe, doc)


# ---- schema / settings ------------------------------------------------------
def test_schema_created_with_defaults(db):
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"applications", "documents", "activity_sessions", "system_events", "categories",
            "settings", "daily_summaries"} <= tables
    assert db.get_float("idle_threshold_seconds") == 300
    assert db.get_bool("start_with_windows") and not db.get_bool("open_dashboard_on_startup")
    assert len(db.query("SELECT * FROM categories")) == 4


def test_settings_persist_across_reopen(tmp_path):
    p = tmp_path / "s.db"
    with Database(p) as d:
        d.set_setting("idle_threshold_seconds", 600)
        d.set_setting("system_tray", False)
    with Database(p) as d:
        assert d.get_float("idle_threshold_seconds") == 600
        assert d.get_bool("system_tray") is False


def test_indexes_exist(db):
    names = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_sessions_start", "uq_sessions_identity", "uq_one_open_session"} <= names


def test_timestamps_are_utc_fixed_format(db):
    local = datetime(2026, 11, 3, 14, 0, tzinfo=timezone(timedelta(hours=5)))
    assert to_ts(local) == "2026-11-03T09:00:00.000000Z"
    assert from_ts(to_ts(local)) == local
    with pytest.raises(ValueError):
        to_ts(datetime(2026, 1, 1))


# ---- migrations ---------------------------------------------------------------
def test_migration_preserves_history_and_backs_up(tmp_path, monkeypatch):
    p = tmp_path / "m.db"
    with Database(p) as d:
        add(d, 0, 30, doc="A.xlsx")
    v2 = migrations.MIGRATIONS + [(3, "ALTER TABLE activity_sessions ADD COLUMN note TEXT")]
    monkeypatch.setattr(migrations, "MIGRATIONS", v2)
    with Database(p) as d:
        assert migrations.current_version(d._writer) == 3
        rows = d.query("SELECT document_name, duration_seconds, note FROM activity_sessions")
        assert rows[0]["document_name"] == "A.xlsx" and rows[0]["duration_seconds"] == 1800
    assert (tmp_path / "m.pre-upgrade-v2.db").exists()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    p = tmp_path / "f.db"
    with Database(p):
        pass
    bad = migrations.MIGRATIONS + [(3, "CREATE TABLE ok_t(a); CREATE TABLE ok_t(a)")]
    monkeypatch.setattr(migrations, "MIGRATIONS", bad)
    with pytest.raises(sqlite3.Error):
        Database(p).open()
    conn = sqlite3.connect(p)
    assert migrations.current_version(conn) == 2
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ok_t'").fetchone()


def test_newer_database_is_refused(tmp_path):
    p = tmp_path / "n.db"
    with Database(p):
        pass
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError):
        Database(p).open()


# ---- sessions / duplicates / recovery -------------------------------------------
def test_open_close_session_duration(db):
    sid = db.open_session(T0, "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "A.xlsx")
    assert db.close_session(sid, T0 + mins(30)) == 1800
    r = db.query("SELECT * FROM activity_sessions")[0]
    assert r["duration_seconds"] == 1800 and r["document_name"] == "A.xlsx"
    assert r["document_id"] is not None and r["application_id"] is not None


def test_only_one_open_session(db):
    db.open_session(T0, "ACTIVE", "X", "x.exe")
    with pytest.raises(SessionAlreadyOpenError):
        db.open_session(T0 + mins(1), "ACTIVE", "Y", "y.exe")


def test_duplicate_session_prevented(db):
    assert add(db, 0, 10, doc="A.xlsx") is not None
    assert add(db, 0, 10, doc="A.xlsx") is None
    assert len(db.query("SELECT 1 FROM activity_sessions")) == 1


def test_negative_duration_clamped_when_clock_goes_back(db):
    sid = db.open_session(T0, "ACTIVE", "X", "x.exe")
    assert db.close_session(sid, T0 - mins(5)) == 0


def test_crash_recovery_uses_heartbeat_not_current_time(db):
    db.open_session(T0, "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "A.xlsx")
    db.heartbeat(T0 + mins(12))
    # "crash": process dies; much later TimeLens starts again
    assert db.recover_open_sessions(T0 + mins(600)) == 1
    r = db.query("SELECT * FROM activity_sessions")[0]
    assert r["duration_seconds"] == 12 * 60 and r["recovered"] == 1
    assert db.query("SELECT 1 FROM system_events WHERE event_type='crash_recovered'")
    # the lost 588 minutes show up as unmonitored, never as activity
    t = tc.get_totals(db, T0, T0 + mins(600), now=NOW)
    assert t.active == 720 and t.unmonitored == 600 * 60 - 720


def test_recovery_without_heartbeat_invents_nothing(db):
    db.open_session(T0, "ACTIVE", "X", "x.exe")
    db.recover_open_sessions(T0 + mins(100))
    assert db.query("SELECT duration_seconds FROM activity_sessions")[0][0] == 0


def test_reopen_after_unclean_stop_recovers(tmp_path):
    p = tmp_path / "c.db"
    d = Database(p).open()
    d.open_session(T0, "ACTIVE", "X", "x.exe")
    d.heartbeat(T0 + mins(5))
    d._writer.close()   # simulate abrupt process death (no clean close)
    d._writer = None
    with Database(p) as d2:
        assert d2.recover_open_sessions(T0 + mins(50)) == 1
        assert d2.integrity_ok()


# ---- time calculations ------------------------------------------------------------
def test_totals_reconcile_and_usage_rate(db):
    add(db, 0, 60, "ACTIVE")
    add(db, 60, 90, "IDLE", "Idle", "")
    add(db, 90, 120, "LOCKED", "Windows", "LockApp.exe")
    add(db, 120, 180, "SLEEP_OFF", "Sleep", "")
    # 180-200 not recorded -> unmonitored
    t = tc.get_totals(db, T0, T0 + mins(200), now=NOW)
    assert (t.active, t.idle, t.locked, t.sleep_off, t.unmonitored) == (
        3600, 1800, 1800, 3600, 1200)
    assert t.available == 5400
    assert t.usage_rate == pytest.approx(3600 / 5400 * 100)
    assert t.accounted == t.period_seconds == 200 * 60


def test_overlapping_rows_never_double_count(db):
    add(db, 0, 60, "ACTIVE")
    add(db, 30, 90, "IDLE", "Idle", "")     # bad data overlapping the first
    t = tc.get_totals(db, T0, T0 + mins(90), now=NOW)
    assert t.active == 3600 and t.idle == 1800
    assert t.accounted == t.period_seconds


def test_open_session_counts_up_to_now(db):
    db.open_session(T0, "ACTIVE", "X", "x.exe")
    t = tc.get_totals(db, T0, T0 + mins(600), now=T0 + mins(10))
    assert t.active == 600 and t.period_seconds == 600 and t.unmonitored == 0


def test_usage_rate_none_without_available_time(db):
    add(db, 0, 10, "LOCKED", "Windows", "LockApp.exe")
    assert tc.get_totals(db, T0, T0 + mins(10), now=NOW).usage_rate is None


def test_midnight_boundary_split(db):
    day = date(2026, 11, 3)
    d_start, d_end = tc.local_day_bounds(day)
    db.add_closed_session(d_end - mins(60), d_end + mins(30), "ACTIVE", "Microsoft Excel",
                          "EXCEL.EXE", "A.xlsx")
    today = tc.get_day_totals(db, day, now=NOW)
    tomorrow = tc.get_day_totals(db, day + timedelta(days=1), now=NOW)
    assert today.active == 3600 and tomorrow.active == 1800
    # the whole-session total equals the sum of the two days (nothing lost or doubled)
    assert today.active + tomorrow.active == 5400


def test_local_day_bounds_contiguous_and_plausible():
    for d in (date(2026, 3, 8), date(2026, 3, 29), date(2026, 10, 25), date(2026, 11, 3)):
        s, e = tc.local_day_bounds(d)
        assert tc.local_day_bounds(d + timedelta(days=1))[0] == e
        assert 23 * 3600 <= (e - s).total_seconds() <= 25 * 3600


def test_app_and_document_totals(db):
    add(db, 0, 30, doc="A.xlsx")
    add(db, 30, 40, doc="B.xlsx")
    add(db, 40, 50, app="Google Chrome", exe="chrome.exe")
    add(db, 50, 60, "IDLE", "Idle", "")
    apps = tc.application_totals(db, T0, T0 + mins(60), NOW)
    assert apps == [("Microsoft Excel", 2400), ("Google Chrome", 600)]
    docs = tc.document_totals(db, T0, T0 + mins(60), now=NOW)
    assert docs == [("Microsoft Excel", "A.xlsx", 1800), ("Microsoft Excel", "B.xlsx", 600)]


def test_daily_summary_cache(db):
    day = date(2026, 11, 3)
    s, _ = tc.local_day_bounds(day)
    db.add_closed_session(s + mins(10), s + mins(70), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "A.xlsx")
    tc.rebuild_daily_summary(db, day, now=NOW)
    r = db.query("SELECT * FROM daily_summaries WHERE day='2026-11-03'")[0]
    assert r["active_seconds"] == 3600 and r["application_count"] == 1 and r["document_count"] == 1


# ---- privacy -------------------------------------------------------------------
def test_delete_history_and_exclusion(db):
    add(db, 0, 10, doc="A.xlsx")
    add(db, 10, 20, app="Google Chrome", exe="chrome.exe")
    assert db.delete_application_history("chrome.exe") == 1
    assert db.delete_sessions_between(T0, T0 + mins(60)) == 1
    assert db.query("SELECT 1 FROM activity_sessions") == []
    db.set_excluded("Teams.exe", True)
    assert db.is_excluded("teams.exe") and not db.is_excluded("EXCEL.EXE")


# ---- backup / restore -------------------------------------------------------------
def test_backup_and_restore_roundtrip(tmp_path):
    p = tmp_path / "live.db"
    with Database(p) as d:
        add(d, 0, 30, doc="A.xlsx")
        backup = bm.create_backup(p, tmp_path / "bk" / "b.db")
        add(d, 30, 60, doc="B.xlsx")           # change after the backup
    assert bm.validate_backup(backup) == migrations.latest_version()
    safety = bm.restore_backup(backup, p)
    assert safety.exists()
    with Database(p) as d:
        names = [r[0] for r in d.query("SELECT document_name FROM activity_sessions")]
        assert names == ["A.xlsx"] and d.integrity_ok()
    with Database(safety) as d:                # the pre-restore state was kept
        assert len(d.query("SELECT 1 FROM activity_sessions")) == 2


def test_restore_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database" * 50)
    with pytest.raises(bm.BackupError):
        bm.validate_backup(bad)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x(a)").connection.close()
    with pytest.raises(bm.BackupError):
        bm.validate_backup(other)
    live = tmp_path / "live.db"
    with Database(live) as d:
        add(d, 0, 5)
    with pytest.raises(bm.BackupError):
        bm.restore_backup(bad, live)
    with Database(live) as d:                  # untouched
        assert len(d.query("SELECT 1 FROM activity_sessions")) == 1


# ---- threading ---------------------------------------------------------------------
def test_reader_thread_while_writer_writes(tmp_path):
    import threading
    with Database(tmp_path / "th.db") as d:
        errors, stop = [], threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    tc.get_totals(d, T0, T0 + mins(10000), now=NOW)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        th = threading.Thread(target=reader)
        th.start()
        for i in range(200):
            d.add_closed_session(T0 + mins(i), T0 + mins(i + 1), "ACTIVE", "X", "x.exe")
        stop.set()
        th.join()
        assert not errors
        assert tc.get_totals(d, T0, T0 + mins(200), now=NOW).active == 200 * 60
