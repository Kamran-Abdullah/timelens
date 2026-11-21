"""Layout regression: in a short window pages must scroll, never squeeze widgets on top of each other."""
import time

import pytest
from PySide6.QtWidgets import QApplication, QScrollArea

from database.db_manager import Database
from services.startup_manager import StartupManager
from tracker.engine import TrackingEngine
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    db = Database(tmp_path / "l.db").open()
    eng = TrackingEngine(db, snapshot_provider=lambda: None)
    eng.start(run_thread=False)
    w = MainWindow(db, eng, startup=StartupManager(tmp_path / "S", (r"C:\Windows\notepad.exe", "", "C:\\")))
    yield w
    w._quitting = True
    w.close()
    wait_for_jobs()
    db.close()


def settle(qapp, seconds=0.8):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def test_productivity_comparison_table_and_note_do_not_overlap_in_a_short_window(qapp, window):
    window.resize(1100, 560)                         # shorter than the page needs (the reported situation)
    window.show()
    window.show_page(list(window.nav_buttons).index("Productivity Analytics"))
    settle(qapp)
    page = window.productivity
    table, note = page.cmp_table, page.cmp_note
    assert note.geometry().top() >= table.geometry().bottom() - 1, "note text sits on top of the table"
    needed = table.horizontalHeader().height() + table.verticalHeader().length()
    assert table.height() >= needed - 2, "table is squeezed shorter than its rows"


@pytest.mark.parametrize("name", ["Live Monitor", "Application Analytics", "Document Analytics", "Activity Timeline",
                                  "Productivity Analytics", "Reports", "Websites", "Assistant"])
def test_every_page_scrolls_when_the_window_is_short(qapp, window, name):
    window.resize(1100, 420)
    window.show()
    window.show_page(list(window.nav_buttons).index(name))
    settle(qapp, 0.6)
    page = window.stack.currentWidget()
    area = page.findChild(QScrollArea)
    assert area is not None and area.widgetResizable()
    content = area.widget()
    assert content.height() >= content.minimumSizeHint().height() - 1     # never squeezed below its needs


def test_tall_window_keeps_the_normal_stretching_layout(qapp, window):
    window.resize(1280, 1000)
    window.show()
    window.show_page(list(window.nav_buttons).index("Activity Timeline"))
    settle(qapp, 0.6)
    page = window.timeline
    area = page.findChild(QScrollArea)
    assert area.verticalScrollBar().maximum() == 0                       # plenty of room: no scrolling needed
    assert page.table.height() > 250                                      # the table uses the spare space
