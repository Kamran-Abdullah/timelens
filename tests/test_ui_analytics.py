import time
from datetime import date, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from analytics import time_calc as tc
from database.db_manager import Database
from tracker.engine import TrackingEngine
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs

TODAY = date(2026, 10, 21)


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    d = Database(tmp_path / "ua.db").open()
    add = d.add_closed_session
    X, W = ("Microsoft Excel", "EXCEL.EXE"), ("Microsoft Word", "WINWORD.EXE")
    add(at(date(2026, 9, 20), 9), at(date(2026, 9, 20), 10), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 10, 20), 9), at(date(2026, 10, 20), 10), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 10, 21), 10), at(date(2026, 10, 21), 10 + 2 / 3), "ACTIVE", *X, "Bank.xlsx")
    add(at(date(2026, 10, 21), 12), at(date(2026, 10, 21), 12 + 1 / 6), "ACTIVE", *W, "Policy.docx")
    add(at(date(2026, 10, 21), 13), at(date(2026, 10, 21), 13.5), "ACTIVE", "Google Chrome", "chrome.exe")
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


def test_application_page_values_and_drilldown(qapp, window):
    window.show_page(2)
    page = window.apps
    assert wait_for(qapp, lambda: page.table.rowCount() == 3)
    assert col(page.table, 0) == ["Microsoft Excel", "Google Chrome", "Microsoft Word"]
    # Excel: today 40m, yesterday 1h, week 1h 40m, month 1h 40m, total 2h 40m
    excel = [page.table.item(0, c).text() for c in range(1, 6)]
    assert excel == ["40m 00s", "1h 00m", "1h 40m", "1h 40m", "2h 40m"]
    assert page.table.horizontalHeaderItem(5).text() == "Total (All time)"
    assert page.table.item(2, 2).text() == "-"            # Word was not used yesterday
    page.table.selectRow(0)
    assert wait_for(qapp, lambda: page.doc_table.rowCount() == 2)
    assert col(page.doc_table, 0) == ["Payroll.xlsx", "Bank.xlsx"]
    assert "Microsoft Excel" in page.doc_title.text()
    assert col(page.doc_table, 2) == ["2", "1"]            # sessions


def test_application_page_date_filter(qapp, window):
    window.show_page(2)
    page = window.apps
    assert wait_for(qapp, lambda: page.table.rowCount() == 3)
    page.filter.combo.setCurrentText("Last month")
    assert wait_for(qapp, lambda: page.table.horizontalHeaderItem(5).text() == "Total (Last month)")
    assert wait_for(qapp, lambda: page.table.item(0, 5).text() == "1h 00m")
    page.filter.combo.setCurrentText("Custom")
    assert page.filter.start.isVisible()


def test_selection_survives_refresh(qapp, window):
    window.show_page(2)
    page = window.apps
    assert wait_for(qapp, lambda: page.table.rowCount() == 3)
    page.table.selectRow(2)
    assert wait_for(qapp, lambda: page.doc_table.rowCount() == 1)
    page.refresh()
    assert wait_for(qapp, lambda: page.table.selectionModel().hasSelection())
    assert page.table.item(page.table.selectionModel().selectedRows()[0].row(), 0).text() == "Microsoft Word"


def test_document_page_sort_search_and_header_sort(qapp, window):
    window.show_page(3)
    page = window.docs
    assert wait_for(qapp, lambda: page.table.rowCount() == 3)
    assert col(page.table, 0) == ["Payroll.xlsx", "Bank.xlsx", "Policy.docx"]    # most used
    page.sort.setCurrentText("Least used")
    assert col(page.table, 0) == ["Policy.docx", "Bank.xlsx", "Payroll.xlsx"]
    page.sort.setCurrentText("Application")
    assert col(page.table, 1) == ["Microsoft Excel", "Microsoft Excel", "Microsoft Word"]
    page.search.setText("bank")
    assert col(page.table, 0) == ["Bank.xlsx"]
    page.search.setText("")
    # header click sorts numerically by the hidden key, not alphabetically by text
    page.table.sortByColumn(2, Qt.DescendingOrder)               # "Today" column
    assert page.table.item(0, 0).text() == "Bank.xlsx"           # 40m today
    page.table.sortByColumn(5, Qt.AscendingOrder)                # period total
    assert page.table.item(0, 0).text() == "Policy.docx"         # 10m (alphabetical would differ)
    assert page.table.item(0, 6).text() == "1"


def test_document_page_only_shows_file_names(qapp, window):
    window.show_page(3)
    page = window.docs
    assert wait_for(qapp, lambda: page.table.rowCount() == 3)
    for text in col(page.table, 0):
        assert "\\" not in text and "/" not in text


def test_empty_database_message(qapp, tmp_path):
    d = Database(tmp_path / "empty2.db").open()
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    w = MainWindow(d, engine, day_provider=lambda: TODAY)
    w.show_page(3)
    assert wait_for(qapp, lambda: "No document names" in w.docs.sub.text())
    w.show_page(2)
    assert wait_for(qapp, lambda: "No application activity" in w.apps.sub.text())
    w.close()
    wait_for_jobs()
    d.close()


def test_analytics_pages_do_not_auto_refresh(window):
    assert window.apps.auto_refresh is False and window.docs.auto_refresh is False
