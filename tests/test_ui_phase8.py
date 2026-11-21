import time
from datetime import date, datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from analytics import time_calc as tc
from database.db_manager import Database
from services.startup_manager import StartupManager
from services.tray_manager import TrayManager
from tracker.engine import TrackingEngine
from ui.icons import save_icon_files
from ui.instance_bridge import InstanceServer, notify_running_instance
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs

NOTEPAD = r"C:\Windows\System32\notepad.exe"
DAY = date(2026, 10, 20)


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


class FakeTray:
    """Stands in for the real tray icon (the offscreen test platform has none)."""
    visible = True

    def __init__(self):
        self.refreshed = 0

    def refresh_state(self):
        self.refreshed += 1

    def set_visible(self, v):
        self.visible = v


@pytest.fixture
def window(qapp, tmp_path):
    d = Database(tmp_path / "p8.db").open()
    d.add_closed_session(at(DAY, 9), at(DAY, 10), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "A.xlsx")
    d.add_closed_session(at(DAY, 10), at(DAY, 11), "ACTIVE", "Google Chrome", "chrome.exe")
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    startup = StartupManager(tmp_path / "Startup", (NOTEPAD, "--background", r"C:\Windows"))
    w = MainWindow(d, engine, day_provider=lambda: DAY, startup=startup)
    w.confirm_all = True
    w.settings.confirm = lambda title, text: w.confirm_all
    w.show()
    yield w
    w._quitting = True
    w.close()
    wait_for_jobs()
    d.close()


def pump(qapp, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


# --------------------------------------------------------------- settings
def test_settings_defaults_match_spec(window):
    s = window.settings
    assert s.rb_auto.isChecked() and not s.rb_manual.isChecked()
    assert {k: b.isChecked() for k, b in s.flags.items()} == {
        "open_dashboard_on_startup": False, "background_monitoring": True,
        "system_tray": True, "minimize_to_tray": True}
    assert s.idle_box.value() == 5


def test_startup_modes_create_and_remove_the_shortcut(window):
    s = window.settings
    s.rb_manual.setChecked(True)
    assert not window.startup.is_enabled() and window.db.get_setting("start_with_windows") == "0"
    s.rb_auto.setChecked(True)
    assert window.startup.is_enabled() and window.db.get_setting("start_with_windows") == "1"
    assert "start automatically" in s.status.text()
    s.rb_manual.setChecked(True)
    assert not window.startup.is_enabled()
    assert "starts only when you open it" in s.status.text()


def test_flags_are_saved_and_tray_off_disables_minimize(window):
    s = window.settings
    s.flags["open_dashboard_on_startup"].setChecked(True)
    assert window.db.get_bool("open_dashboard_on_startup")
    s.flags["system_tray"].setChecked(False)
    assert not window.db.get_bool("system_tray") and not s.flags["minimize_to_tray"].isEnabled()
    s.flags["system_tray"].setChecked(True)
    assert s.flags["minimize_to_tray"].isEnabled()


def test_idle_threshold_is_configurable_and_applied_live(window):
    window.settings.idle_box.setValue(10)
    assert window.db.get_float("idle_threshold_seconds") == 600
    assert window.engine.monitor.manager.idle_threshold_seconds == 600
    window.settings.idle_box.setValue(1)
    assert window.engine.monitor.manager.idle_threshold_seconds == 60


def test_settings_persist_across_reopen(window, tmp_path):
    window.settings.idle_box.setValue(12)
    window.settings.flags["minimize_to_tray"].setChecked(False)
    path = window.db.path
    window.db.close()
    window.db.open()
    window.settings.load()
    assert window.settings.idle_box.value() == 12
    assert not window.settings.flags["minimize_to_tray"].isChecked()


# --------------------------------------------------------------- privacy
def test_exclusions_hide_application_details(window, qapp):
    s = window.settings
    s.load()
    names = [s.excl_table.item(r, 1).text() for r in range(s.excl_table.rowCount())]
    assert names == ["chrome.exe", "EXCEL.EXE"] and "LockApp.exe" not in names
    s.add_exclusion("Teams")                                   # ".exe" is added for you
    assert window.db.is_excluded("Teams.exe")
    s.add_exclusion(r"C:\Program Files\x.exe")                  # a path is refused
    assert "just the executable" in s.status.text() and not window.db.is_excluded("x.exe")
    row = [s.excl_table.item(r, 1).text() for r in range(s.excl_table.rowCount())].index("EXCEL.EXE")
    s.excl_table.cellWidget(row, 2).findChild(type(s.flags["system_tray"])).setChecked(True)
    assert window.db.is_excluded("EXCEL.EXE")
    # new activity from an excluded app is stored anonymously
    from tracker.session_manager import Snapshot
    from tracker.window_detector import WindowInfo
    now = datetime.now(timezone.utc)
    w = WindowInfo(1, 1, "Secret.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
    window.engine.monitor._snapshot_provider = lambda: Snapshot(now, w, now, False)
    window.engine.monitor.tick()
    window.engine.monitor.pause()
    last = window.db.query("SELECT * FROM activity_sessions ORDER BY id DESC LIMIT 1")[0]
    assert last["application_name"] == "Excluded Application" and last["document_name"] is None


def test_delete_history_requires_confirmation(window):
    s = window.settings
    window.confirm_all = False
    assert s.delete_app_history("chrome.exe") == 0
    assert s.delete_range(DAY, DAY) == 0
    assert len(window.db.query("SELECT 1 FROM activity_sessions")) == 2          # untouched
    window.confirm_all = True
    assert s.delete_app_history("chrome.exe") == 1
    assert s.delete_range(DAY, DAY) == 1
    assert window.db.query("SELECT 1 FROM activity_sessions WHERE end_timestamp IS NOT NULL") == []


def test_delete_all_history(window):
    assert window.settings.delete_all() == 2
    assert "cleared" in window.settings.status.text()


# --------------------------------------------------------------- backup / restore
def test_backup_then_restore_roundtrip_keeps_tracking(window, qapp, tmp_path):
    s = window.settings
    bk = s.backup_to(tmp_path / "bk" / "b.db")
    assert bk and bk.exists() and "Backup saved" in s.status.text()
    s.delete_all()
    assert window.db.query("SELECT 1 FROM activity_sessions WHERE end_timestamp IS NOT NULL") == []
    assert s.restore_from(bk) is True
    assert "Restored from b.db" in s.status.text()
    assert len(window.db.query("SELECT 1 FROM activity_sessions")) == 2
    assert window.engine.running and window.db.integrity_ok()
    assert list(tmp_path.glob("p8.before-restore.db"))                           # safety copy


def test_restore_rejects_bad_file_and_respects_cancel(window, tmp_path):
    s = window.settings
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    assert s.restore_from(junk) is False and "Cannot restore" in s.status.text()
    good = s.backup_to(tmp_path / "g.db")
    window.confirm_all = False
    assert s.restore_from(good) is False
    assert window.engine.running and len(window.db.query("SELECT 1 FROM activity_sessions")) == 2


# --------------------------------------------------------------- close / exit behaviour
def test_close_without_tray_exits_and_stops_monitoring(window):
    assert not window.will_hide_to_tray()          # offscreen: no tray at all
    window.close()
    assert not window.engine.running


def test_close_hides_to_tray_and_keeps_monitoring(window, qapp):
    window.tray = FakeTray()
    assert window.will_hide_to_tray()
    window.close()
    assert not window.isVisible() and window.engine.running and not window.engine.paused


def test_background_monitoring_off_pauses_while_hidden_then_resumes(window):
    window.tray = FakeTray()
    window.db.set_setting("background_monitoring", False)
    window.close()
    assert not window.isVisible() and window.engine.paused
    window.show_dashboard()
    assert window.isVisible() and not window.engine.paused


def test_user_pause_is_not_undone_by_showing_window(window):
    window.tray = FakeTray()
    window.engine.pause()
    window.db.set_setting("background_monitoring", False)
    window.close()
    window.show_dashboard()
    assert window.engine.paused                    # the user's own pause is respected


def test_minimize_to_tray_off_means_close_exits(window):
    window.tray = FakeTray()
    window.db.set_setting("minimize_to_tray", False)
    assert not window.will_hide_to_tray()
    window.close()
    assert not window.engine.running


def test_tray_exit_fully_stops_monitoring(window):
    window.tray = FakeTray()
    window.exit_app()
    assert not window.engine.running and not window.isVisible()


def test_open_settings_shows_settings_page(window):
    window.hide()
    window.open_settings()
    assert window.isVisible() and window.stack.currentWidget() is window.settings


# --------------------------------------------------------------- tray menu
def test_tray_menu_has_the_specified_items_and_state(qapp, tmp_path):
    d = Database(tmp_path / "t.db").open()
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    calls = []
    tray = TrayManager(engine, lambda: calls.append("dash"), lambda: calls.append("settings"),
                       lambda: calls.append("exit"), lambda: calls.append("folder"))
    texts = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert texts == ["Open Dashboard", "Pause Monitoring", "Resume Monitoring", "Settings",
                     "Open Data Folder", "Exit TimeLens"]
    assert tray.act_pause.isEnabled() and not tray.act_resume.isEnabled()
    tray.act_pause.trigger()
    assert engine.paused and not tray.act_pause.isEnabled() and tray.act_resume.isEnabled()
    tray.act_resume.trigger()
    assert not engine.paused
    for act in (tray.act_open, tray.act_settings, tray.act_folder, tray.act_exit):
        act.trigger()
    assert calls == ["dash", "settings", "folder", "exit"]
    engine.stop()
    tray.refresh_state()
    assert not tray.act_pause.isEnabled() and not tray.act_resume.isEnabled()
    d.close()


# --------------------------------------------------------------- second launch + icon
def test_second_launch_asks_running_instance_to_show(qapp):
    name = f"TimeLens.Test.{time.time_ns()}"
    server = InstanceServer(name)
    assert server.listening
    got = []
    server.show_requested.connect(lambda: got.append(1))
    assert notify_running_instance(name)
    end = time.time() + 3
    while not got and time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert got == [1]
    assert not notify_running_instance(f"TimeLens.Nobody.{time.time_ns()}", 200)


def test_icon_files_are_written(qapp, tmp_path):
    png, ico = save_icon_files(tmp_path)
    assert png.stat().st_size > 500 and ico.stat().st_size > 500
