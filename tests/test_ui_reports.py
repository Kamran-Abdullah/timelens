import time
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook
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
    d = Database(tmp_path / "ur.db").open()
    a = d.add_closed_session
    a(at(YDAY, 9), at(YDAY, 10), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "Payroll.xlsx")
    a(at(YDAY, 10), at(YDAY, 10.5), "IDLE", "Idle")
    a(at(YDAY, 10.5), at(YDAY, 11), "LOCKED", "Windows", "LockApp.exe")
    engine = TrackingEngine(d, snapshot_provider=lambda: None)
    engine.start(run_thread=False)
    w = MainWindow(d, engine, day_provider=lambda: TODAY)
    w.show()
    yield w
    w.close()
    wait_for_jobs()
    d.close()


def wait_for(qapp, cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_preview_matches_database(qapp, window):
    window.show_page(6)
    page = window.reports
    page.filter.combo.setCurrentText("Yesterday")
    assert wait_for(qapp, lambda: page.cards["active"].value.text() == "1h 00m")
    assert page.cards["idle"].value.text() == "30m 00s"
    assert page.cards["locked"].value.text() == "30m 00s"
    assert page.cards["available"].value.text() == "1h 30m"
    assert page.cards["rate"].value.text() == "66.7%"
    assert "reconciles" in page.preview.text() and "1 applications, 1 documents" in page.preview.text()


def test_period_list_matches_spec(window):
    items = [window.reports.filter.combo.itemText(i) for i in range(window.reports.filter.combo.count())]
    assert items == ["Today", "Yesterday", "This week", "Last week", "This month", "Last month", "Custom"]


def test_export_excel_and_pdf_files_are_created(qapp, window, tmp_path):
    window.show_page(6)
    page = window.reports
    page.filter.combo.setCurrentText("Yesterday")
    assert wait_for(qapp, lambda: page.cards["active"].value.text() == "1h 00m")
    out = tmp_path / "out"
    out.mkdir()
    page.export_to("xlsx", out / "daily.xlsx")
    assert wait_for(qapp, lambda: page.status.text().startswith("Saved"))
    wb = load_workbook(out / "daily.xlsx")
    assert "Summary" in wb.sheetnames and "Timeline" in wb.sheetnames
    assert page.btn_open.isEnabled() and page.btn_excel.isEnabled()
    page.export_to("pdf", out / "daily")                    # missing extension is added
    assert wait_for(qapp, lambda: page.status.text().endswith("daily.pdf"))
    assert (out / "daily.pdf").read_bytes().startswith(b"%PDF")


def test_unticked_sections_are_left_out(qapp, window, tmp_path):
    page = window.reports
    window.show_page(6)
    for key, box in page.checks.items():
        box.setChecked(key == "applications")
    assert page.selected_sections() == ("applications",)
    page.export_to("xlsx", tmp_path / "one.xlsx")
    assert wait_for(qapp, lambda: page.status.text().startswith("Saved"))
    assert load_workbook(tmp_path / "one.xlsx").sheetnames == ["Summary", "Applications"]


def test_monthly_export_from_ui(qapp, window, tmp_path):
    page = window.reports
    window.show_page(6)
    page.filter.combo.setCurrentText("This month")
    page.export_to("xlsx", tmp_path / "month.xlsx")
    assert wait_for(qapp, lambda: page.status.text().startswith("Saved"))
    assert (tmp_path / "month.xlsx").stat().st_size > 5000


def test_export_failure_is_reported_not_swallowed(qapp, window, tmp_path):
    page = window.reports
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    page.export_to("xlsx", blocker / "sub" / "bad.xlsx")    # parent is a file: cannot be created
    assert wait_for(qapp, lambda: page.status.text().startswith("Export failed"))
    assert page.btn_excel.isEnabled()                        # buttons come back after a failure
