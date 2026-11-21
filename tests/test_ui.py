import time
from datetime import date, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from analytics import time_calc as tc
from analytics.dashboard_data import build_dashboard_data
from database.db_manager import Database
from tracker.engine import TrackingEngine
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs

DAY = date(2026, 10, 15)
NOW = tc.local_day_bounds(DAY)[1] + timedelta(days=30)   # "now" is well after DAY


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "ui.db").open()
    s, _ = tc.local_day_bounds(DAY)
    h = lambda x: s + timedelta(hours=x)
    add = d.add_closed_session
    add(h(9), h(10), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "Payroll.xlsx")
    add(h(10), h(10.5), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "Bank Rec.xlsx")
    add(h(10.5), h(11), "ACTIVE", "Google Chrome", "chrome.exe")
    add(h(11), h(11.25), "IDLE", "Idle")
    add(h(11.25), h(12), "LOCKED", "Windows", "LockApp.exe")
    # user classified Chrome as a distraction
    with d.transaction() as c:
        c.execute("UPDATE applications SET category_id=(SELECT id FROM categories "
                  "WHERE kind='distraction') WHERE executable_name='chrome.exe'")
    yield d
    wait_for_jobs()
    d.close()


def wait_for(qapp, cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_dashboard_data_exact(db):
    d = build_dashboard_data(db, DAY, NOW)
    assert d.totals.active == 7200 and d.totals.idle == 900 and d.totals.locked == 2700
    assert d.totals.available == 8100 and d.totals.usage_rate == pytest.approx(7200 / 8100 * 100)
    assert d.application_count == 2 and d.document_count == 2
    assert d.top_apps == [("Microsoft Excel", 5400), ("Google Chrome", 1800)]
    assert d.top_documents[0] == ("Microsoft Excel", "Payroll.xlsx", 3600)
    assert d.distribution == {"uncategorized": 5400, "distraction": 1800}
    assert len(d.hourly) in (23, 24, 25)
    assert sum(t.active for _, t in d.hourly) == 7200
    assert sum(t.idle for _, t in d.hourly) == 900
    # every hour bucket reconciles: nothing counted twice
    assert all(t.accounted == pytest.approx(t.period_seconds) for _, t in d.hourly)


def test_bucket_boundaries_split_a_session(db):
    s, _ = tc.local_day_bounds(DAY)
    b = dict(tc.bucket_totals(db, s + timedelta(hours=9), s + timedelta(hours=12), now=NOW))
    assert [t.active for t in b.values()] == [3600, 1800 + 1800, 0]


@pytest.fixture
def window(qapp, db):
    engine = TrackingEngine(db, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    w = MainWindow(db, engine, day_provider=lambda: DAY)
    w.show()
    yield w
    w.close()


def test_navigation_has_all_pages(window):
    assert list(window.nav_buttons) == [
        "Dashboard", "Live Monitor", "Application Analytics", "Document Analytics",
        "Activity Timeline", "Productivity Analytics", "Reports", "Websites", "Assistant", "Settings"]
    for i in range(10):
        window.show_page(i)
        assert window.stack.currentIndex() == i
        assert list(window.nav_buttons.values())[i].isChecked()


def test_dashboard_shows_database_values(qapp, window):
    window.show_page(0)
    cards = window.dashboard.cards
    assert wait_for(qapp, lambda: cards["active"].value.text() != "-")
    assert cards["active"].value.text() == "2h 00m"
    assert cards["idle"].value.text() == "15m 00s"
    assert cards["locked"].value.text() == "45m 00s"
    assert cards["available"].value.text() == "2h 15m"
    assert cards["rate"].value.text() == "89%"
    assert cards["apps"].value.text() == "2" and cards["docs"].value.text() == "2"
    assert window.dashboard.apps_table.item(0, 0).text() == "Microsoft Excel"
    assert window.dashboard.docs_table.item(0, 0).text() == "Payroll.xlsx"
    assert window.dashboard.docs_table.rowCount() == 2


def test_empty_database_dashboard_has_no_fake_data(qapp, tmp_path):
    d = Database(tmp_path / "empty.db").open()
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    w = MainWindow(d, engine, day_provider=lambda: DAY)
    w.show_page(0)
    assert wait_for(qapp, lambda: w.dashboard.cards["active"].value.text() != "-")
    assert w.dashboard.cards["active"].value.text() == "0s"
    assert w.dashboard.cards["rate"].value.text() == "n/a"
    assert w.dashboard.apps_table.rowCount() == 0
    w.close()
    d.close()


def test_live_monitor_pause_resume(qapp, window):
    window.show_page(1)
    live = window.live
    live._update()
    assert live.c_status.value.text().startswith("Starting")
    live.toggle.click()
    assert window.engine.paused and live.c_status.value.text() == "Paused"
    assert live.toggle.text() == "Resume monitoring"
    live.toggle.click()
    assert not window.engine.paused and live.toggle.text() == "Pause monitoring"


def test_live_monitor_shows_current_segment(qapp, window):
    from datetime import datetime, timezone
    from tracker.session_manager import Snapshot
    from tracker.window_detector import WindowInfo
    now = datetime.now(timezone.utc)
    w = WindowInfo(1, 1, "Live.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
    window.engine.monitor._snapshot_provider = lambda: Snapshot(now, w, now, False)
    window.engine.monitor.tick()
    window.show_page(1)
    window.live._update()
    assert window.live.c_app.value.text() == "Microsoft Excel"
    assert window.live.c_doc.value.text() == "Live.xlsx"
    assert window.live.c_status.value.text() == "Monitoring - Active"
    window.live.open_timeline_requested.emit()
    assert window.stack.currentIndex() == 4


def test_theme_toggle_persists(window, db):
    assert db.get_setting("theme") == "light"
    window.toggle_theme()
    assert db.get_setting("theme") == "dark"
    assert "Switch to light" in window.theme_btn.text()
    window.toggle_theme()
    assert db.get_setting("theme") == "light"


def test_closing_window_stops_tracking(qapp, window):
    window.close()
    assert not window.engine.running
