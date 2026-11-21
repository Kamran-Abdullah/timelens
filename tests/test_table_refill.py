"""Regression: refilling a big table whose columns auto-size to content must not freeze the window.

Found on a real database: the Timeline page filled fine the first time (0.2 s for 2009 rows) and
never finished the second time, because ResizeToContents columns re-measured every row on each cell.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication, QHeaderView, QTableWidget

from analytics.timeline import TimelineEntry
from ui.timeline import TimelinePage
from ui.widgets import fill_sortable, fill_table

ROWS = 2000


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def entries(n):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return [TimelineEntry(t0 + timedelta(minutes=2 * i), t0 + timedelta(minutes=2 * i + 1), "ACTIVE",
                          f"App {i % 7}", f"Document {i}.xlsx", "EXCEL.EXE") for i in range(n)]


def test_timeline_can_be_refilled_many_times(qapp, tmp_path):
    from database.db_manager import Database
    db = Database(tmp_path / "t.db").open()
    page = TimelinePage(db)
    page.resize(1300, 800)
    page.show()
    data = entries(ROWS)
    for _ in range(3):                                   # the 2nd fill was the one that hung
        t0 = time.perf_counter()
        page._fill(data)
        qapp.processEvents()
        assert time.perf_counter() - t0 < 5, "refill is far too slow (quadratic column sizing?)"
    assert page.table.rowCount() == ROWS
    hdr = page.table.horizontalHeader()
    assert hdr.sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents    # still auto-sized for the user
    assert hdr.sectionResizeMode(3) == QHeaderView.ResizeMode.Stretch
    page.hide()
    db.close()


@pytest.mark.parametrize("filler,cell", [(fill_table, lambda r, c: f"r{r}c{c} text"),
                                          (fill_sortable, lambda r, c: (f"r{r}c{c} text", r))])
def test_shared_fillers_keep_modes_and_stay_fast(qapp, filler, cell):
    t = QTableWidget(0, 3)
    t.show()
    hdr = t.horizontalHeader()
    hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    rows = [[cell(r, c) for c in range(3)] for r in range(ROWS)]
    for _ in range(3):
        t0 = time.perf_counter()
        filler(t, rows)
        assert time.perf_counter() - t0 < 5
    assert t.rowCount() == ROWS and t.updatesEnabled()
    assert hdr.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert hdr.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
