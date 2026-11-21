import time
from datetime import date, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from analytics import time_calc as tc
from database.db_manager import Database
from tracker.engine import TrackingEngine
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs

TODAY = date(2026, 10, 21)
YDAY = date(2026, 10, 20)


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    d = Database(tmp_path / "p6.db").open()
    a = d.add_closed_session
    X, C, T = ("Microsoft Excel", "EXCEL.EXE"), ("Google Chrome", "chrome.exe"), ("Microsoft Teams", "ms-teams.exe")
    a(at(YDAY, 9), at(YDAY, 9.75), "ACTIVE", *X, "October Payroll.xlsx")
    a(at(YDAY, 9.75), at(YDAY, 10.25), "ACTIVE", *X, "Bank Reconciliation.xlsx")
    a(at(YDAY, 10.25), at(YDAY, 10.5), "ACTIVE", *C)
    a(at(YDAY, 10.5), at(YDAY, 11), "IDLE", "Idle")
    a(at(YDAY, 11), at(YDAY, 11.5), "LOCKED", "Windows", "LockApp.exe")
    a(at(TODAY, 9), at(TODAY, 12), "ACTIVE", *X, "A.xlsx")
    a(at(TODAY, 12), at(TODAY, 13), "ACTIVE", *T)
    a(at(TODAY, 13), at(TODAY, 14), "ACTIVE", *C)
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    w = MainWindow(d, engine, day_provider=lambda: TODAY)
    w.show()
    yield w
    w.close()
    wait_for_jobs()
    d.close()


def wait_for(qapp, cond, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def col(table, c):
    return [table.item(r, c).text() for r in range(table.rowCount())]


# ------------------------------------------------------------------ timeline
def test_timeline_page_lists_day_with_indicators(qapp, window):
    window.show_page(4)
    page = window.timeline
    page.filter.combo.setCurrentText("Yesterday")
    assert wait_for(qapp, lambda: page.table.rowCount() == 6 and col(page.table, 3)[0] == "October Payroll.xlsx")
    assert col(page.table, 3) == ["October Payroll.xlsx", "Bank Reconciliation.xlsx",
                                  "Google Chrome", "Idle", "Locked", "Unmonitored"]
    assert col(page.table, 3)[:5] == ["October Payroll.xlsx", "Bank Reconciliation.xlsx",
                                      "Google Chrome", "Idle", "Locked"]
    assert col(page.table, 5)[:5] == ["Active", "Active", "Active", "Idle", "Locked"]
    assert col(page.table, 4)[3] == "No user input"
    assert col(page.table, 1)[0] == "20 Oct  09:00 - 09:45"
    assert col(page.table, 2)[0] == "45m 00s"
    # each status has its own indicator colour
    colors = {page.table.item(r, 0).foreground().color().name() for r in range(5)}
    assert len(colors) == 3          # active, idle, locked
    assert len(page.strip.entries) == 6


def test_timeline_application_filter(qapp, window):
    window.show_page(4)
    page = window.timeline
    page.filter.combo.setCurrentText("Yesterday")
    assert wait_for(qapp, lambda: page.table.rowCount() == 6)
    page.app_filter.setCurrentText("Microsoft Excel")
    assert wait_for(qapp, lambda: page.table.rowCount() == 2)
    assert col(page.table, 3) == ["October Payroll.xlsx", "Bank Reconciliation.xlsx"]
    assert col(page.table, 4) == ["Microsoft Excel", "Microsoft Excel"]


def test_timeline_empty_message(qapp, window):
    window.show_page(4)
    page = window.timeline
    page.filter.combo.setCurrentText("Last month")
    assert wait_for(qapp, lambda: "No activity recorded" in page.sub.text())


def test_live_monitor_button_opens_timeline(window):
    window.live.open_timeline_requested.emit()
    assert window.stack.currentWidget() is window.timeline


# -------------------------------------------------------------- productivity
def test_nothing_categorized_until_user_assigns(qapp, window):
    window.show_page(5)
    page = window.productivity
    assert wait_for(qapp, lambda: page.cat_table.rowCount() == 1)
    assert col(page.cat_table, 0) == ["Uncategorized"]
    assert page.cards["score"].value.text() == "n/a"
    assert page.cards["productive"].value.text() == "0s"


def test_rules_flow_updates_overview_and_score(qapp, window):
    page = window.productivity
    window.show_page(5)
    page.tabs.setCurrentIndex(1)                              # rules tab
    assert col(page.app_table, 1) == ["chrome.exe", "EXCEL.EXE", "ms-teams.exe"]
    for r in range(page.app_table.rowCount()):
        combo = page.app_table.cellWidget(r, 2)
        exe = page.app_table.item(r, 1).text()
        wanted = {"EXCEL.EXE": "Productive", "ms-teams.exe": "Neutral", "chrome.exe": "Distraction"}[exe]
        combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemText(i).startswith(wanted)))
    assert "Saved:" in page.rules_status.text()
    page.tabs.setCurrentIndex(0)                              # overview refreshes
    assert wait_for(qapp, lambda: page.cards["productive"].value.text() == "3h 00m")
    assert page.cards["neutral"].value.text() == "1h 00m"
    assert page.cards["distraction"].value.text() == "1h 00m"
    assert page.cards["score"].value.text() == "70%"          # (3*100 + 1*50 + 0) / 5
    assert "= 70%" in page.explain.text()
    assert col(page.cat_table, 3) == ["60%", "20%", "20%"]    # usage share of active time
    # comparison table: Today, Yesterday, This week, Last week
    assert col(page.cmp_table, 0) == ["Today", "Yesterday", "This week", "Last week (same days)"]
    assert col(page.cmp_table, 2)[0] == "3h 00m"
    assert "Today vs Yesterday" in page.cmp_note.text()


def test_changing_weights_changes_score(qapp, window):
    page = window.productivity
    window.show_page(5)
    page.tabs.setCurrentIndex(1)
    for r in range(page.app_table.rowCount()):
        combo = page.app_table.cellWidget(r, 2)
        exe = page.app_table.item(r, 1).text()
        want = "Productive" if exe == "EXCEL.EXE" else "Distraction"
        combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemText(i).startswith(want)))
    page.weight_boxes["distraction"].setValue(50)
    assert window.db.get_setting("weight_distraction") == "50"
    page.tabs.setCurrentIndex(0)
    # 3h productive @100 + 2h distraction @50 = 4h credit / 5h = 80%
    assert wait_for(qapp, lambda: page.cards["score"].value.text() == "80%")


def test_add_custom_category_appears_in_rules(qapp, window):
    page = window.productivity
    window.show_page(5)
    page.tabs.setCurrentIndex(1)
    page.new_name.setText("Client work")
    page._add_category()
    assert [page.cat_list.item(i).text() for i in range(page.cat_list.count())] == ["Client work"]
    assert any("Client work" in page.app_table.cellWidget(0, 2).itemText(i)
               for i in range(page.app_table.cellWidget(0, 2).count()))


def test_weights_persist_in_database(window):
    page = window.productivity
    page.load_rules()
    assert [page.weight_boxes[k].value() for k in ("productive", "neutral", "distraction", "personal", "custom")] \
        == [100, 50, 0, 50, 50]
