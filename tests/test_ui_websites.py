import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from database.db_manager import Database
from services import web_history as wh
from services.startup_manager import StartupManager
from tracker.engine import TrackingEngine
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs

VISIT_DAY = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
VISITS = [
    ("https://www.github.com/org/private-repo?token=XYZ", "Private repo", 3, 600),
    ("https://github.com/explore", "Explore", 2.5, 120),
    ("https://www.google.com/search?q=nadra+office+timings", "nadra office timings - Google Search", 2, 30),
    ("https://mybank.com/account/123456", "My Bank", 1, 60),
]


def make_history(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER, visit_time INTEGER, "
                 "visit_duration INTEGER)")
    for i, (url, title, hours_before, secs) in enumerate(VISITS, 1):
        conn.execute("INSERT INTO urls VALUES (?,?,?)", (i, url, title))
        when = VISIT_DAY - timedelta(hours=hours_before)
        conn.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES (?,?,?)",
                     (i, wh.datetime_to_webkit(when), secs * 1_000_000))
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    hist = make_history(tmp_path / "History")
    calls = []

    def fake_sources(browsers, local_appdata=None):
        calls.append(list(browsers))
        return {"chrome:Default": hist}

    monkeypatch.setattr("services.web_history.history_sources", fake_sources)
    db = Database(tmp_path / "ui.db").open()
    eng = TrackingEngine(db, snapshot_provider=lambda: None)
    eng.start(run_thread=False)
    w = MainWindow(db, eng, day_provider=lambda: date(2026, 10, 21),
                   startup=StartupManager(tmp_path / "S", (r"C:\Windows\notepad.exe", "", "C:\\")))
    w.sources_calls = calls
    w.show()
    yield w
    w._quitting = True
    w.close()
    wait_for_jobs()
    db.close()


def wait_for(qapp, cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def col(table, c):
    return [table.item(r, c).text() for r in range(table.rowCount())]


def test_page_is_off_by_default_and_reads_nothing(qapp, window):
    page = window.websites
    window.sources_calls.clear()                      # (Settings lists profile NAMES at start-up; that is not reading history)
    window.show_page(list(window.nav_buttons).index("Websites"))
    assert not page.tracking_on() and not page.off_panel.isHidden() and page.on_widget.isHidden()
    window.import_websites()                         # the 5-minute timer does this
    wait_for_jobs()
    assert window.sources_calls == []                # no browser history was located or read while tracking is off
    assert window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 0


def test_declining_the_prompt_keeps_tracking_off(qapp, window):
    page = window.websites
    window.sources_calls.clear()
    page.ask_consent = lambda: None
    assert page.turn_on() is False
    assert not page.tracking_on() and window.sources_calls == []


def test_turning_on_imports_and_shows_the_data(qapp, window):
    page = window.websites
    page.ask_consent = lambda: 30                     # "turn on and include the past 30 days"
    assert page.turn_on() is True
    page.filter.combo.setCurrentText("This month")
    assert wait_for(qapp, lambda: page.cards["visits"].value.text() == "4")
    assert not page.on_widget.isHidden() and page.off_panel.isHidden()
    assert page.cards["sites"].value.text() == "3" and page.cards["searches"].value.text() == "1"
    assert page.cards["time"].value.text() == "13m 30s"                     # 600+120+30+60 s
    assert col(page.sites_table, 0)[0] == "github.com" and col(page.sites_table, 1)[0] == "2"
    assert col(page.search_table, 1) == ["nadra office timings"]
    assert col(page.search_table, 2) == ["google.com"]
    titles = col(page.visits_table, 2)
    assert "Private repo" in titles and "My Bank" in titles
    # no full address anywhere in the visible tables
    shown = " ".join(col(page.visits_table, c)[i] for c in range(4) for i in range(page.visits_table.rowCount()))
    assert "token=XYZ" not in shown and "123456" not in shown and "https://" not in shown
    assert "4 page visit(s) on 3 site(s)" in page.sub.text()


def test_turn_off_hides_data_but_keeps_it_stored(qapp, window):
    page = window.websites
    page.ask_consent = lambda: 30
    page.turn_on()
    assert wait_for(qapp, lambda: window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4)
    page.turn_off()
    assert not page.tracking_on() and page.on_widget.isHidden()
    assert window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4


# ------------------------------------------------------------------ settings
def test_settings_toggle_asks_first_and_reverts_on_no(qapp, window):
    s = window.settings
    s.ask_web_consent = lambda: None
    s.web_on.setChecked(True)
    assert not s.web_on.isChecked() and not window.db.get_bool("track_websites")
    assert "stays off" in s.status.text()
    s.ask_web_consent = lambda: 0
    s.web_on.setChecked(True)
    assert window.db.get_bool("track_websites") and "from now on" in s.status.text()
    s.web_on.setChecked(False)
    assert not window.db.get_bool("track_websites") and "already saved stays" in s.status.text()


def test_settings_browsers_and_exclusions(qapp, window):
    s = window.settings
    s.web_edge.setChecked(False)
    assert window.db.get_setting("web_browsers") == "chrome"
    s.web_chrome.setChecked(False)
    assert window.db.get_setting("web_browsers") == ""
    window.websites.ask_consent = lambda: 30
    window.websites.turn_on()
    s.web_chrome.setChecked(True)
    window.websites.update_now()
    assert wait_for(qapp, lambda: window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4)
    s.save_web_excluded("MyBank.com\n  www.example.org ")
    assert window.db.get_setting("web_excluded_domains") == "example.org\nmybank.com"
    assert {r["domain"] for r in window.db.query("SELECT domain FROM web_visits")} == {"github.com", "google.com"}
    assert "Removed 1 stored visit" in s.status.text()


def test_delete_all_website_history_needs_confirmation(qapp, window):
    s = window.settings
    window.websites.ask_consent = lambda: 30
    window.websites.turn_on()
    assert wait_for(qapp, lambda: window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4)
    s.confirm = lambda t, x: False
    assert s.delete_web_history() == 0 and window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4
    s.confirm = lambda t, x: True
    assert s.delete_web_history() == 4 and window.db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 0


def test_settings_panel_reflects_saved_values(qapp, window):
    window.db.set_setting("web_excluded_domains", "bank.com")
    window.settings.load()
    assert window.settings.web_excluded.toPlainText() == "bank.com"
    assert window.settings.web_chrome.isChecked() and window.settings.web_edge.isChecked()
    assert not window.settings.web_on.isChecked()


def test_import_timer_is_running(window):
    assert window.web_timer.isActive() and window.web_timer.interval() == 300_000


def test_settings_lists_profiles_and_saves_the_choice(qapp, window):
    s = window.settings
    s.load_web_profiles()
    assert s.web_profiles.count() == 1                       # the fake source: chrome:Default
    item = s.web_profiles.item(0)
    assert "Default" in item.text() and item.checkState() == Qt.Checked      # Default is read by default
    item.setCheckState(Qt.Unchecked)
    assert window.db.get_setting("web_profiles") == ""       # nothing ticked = back to the default rule
    item.setCheckState(Qt.Checked)
    assert window.db.get_setting("web_profiles") == "chrome:Default"
    assert "Browser profiles to read: 1" in s.status.text()
