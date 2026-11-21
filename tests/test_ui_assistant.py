import os
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from assistant.engine import AssistantEngine
from assistant.file_index import FileIndex
from database.db_manager import Database
from tracker.engine import TrackingEngine
from ui.assistant_page import AssistantPage
from ui.main_window import MainWindow
from ui.workers import wait_for_jobs


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Spy:
    def __init__(self):
        self.opened, self.revealed = [], []

    def open(self, p):
        self.opened.append(p)

    def reveal(self, p):
        self.revealed.append(p)


def touch(path: Path, age_days=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 2048)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


@pytest.fixture
def page(qapp, tmp_path):
    docs = tmp_path / "Documents"
    touch(docs / "October Payroll.xlsx", 3)
    touch(docs / "Bank Reconciliation.xlsx", 1)
    touch(docs / "Invoice 2026.pdf", 5)
    touch(docs / "run.exe")
    db = Database(tmp_path / "ac.db").open()
    index = FileIndex([docs])
    spy = Spy()
    engine = AssistantEngine(index, db, spy.open, spy.reveal, {"documents": docs})
    p = AssistantPage(db, index, engine)
    p.spy, p.docs, p.tmp = spy, docs, tmp_path
    p.show()
    yield p
    p.hide()
    wait_for_jobs()
    db.close()


def wait_for(qapp, cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def last_bot(page):
    return [m for who, m in page.messages if who == "bot"][-1]


def test_welcome_message_and_chips(page):
    assert page.messages[0][0] == "bot" and "find or open" in page.messages[0][1]
    assert [c.text() for c in page.chips] == ["Recent Excel files", "Open my latest PDF",
                                              "What did I work on today?", "My most used folders", "Help"]


def test_index_builds_when_page_is_shown_and_status_is_honest(qapp, page):
    page.refresh()
    assert wait_for(qapp, lambda: page.index.ready and "Searching 3 documents" in page.sub.text())
    assert "Nothing leaves this computer" in page.sub.text()


def test_typed_request_opens_the_file(qapp, page):
    page.ask("open October Payroll.xlsx")
    assert wait_for(qapp, lambda: page.spy.opened)
    assert page.spy.opened == [str(page.docs / "October Payroll.xlsx")]
    assert wait_for(qapp, lambda: "Opened October Payroll.xlsx" in last_bot(page))
    assert page.messages[-2] == ("user", "open October Payroll.xlsx")
    assert page.btn_send.isEnabled() and page.input.isEnabled()


def test_search_shows_rows_with_buttons_and_open_works(qapp, page):
    page.ask("find excel files")
    assert wait_for(qapp, lambda: len(page.file_rows()) == 2)
    assert page.spy.opened == []                                 # searching never opens anything
    rows = {r.entry.name: r for r in page.file_rows()}
    assert set(rows) == {"October Payroll.xlsx", "Bank Reconciliation.xlsx"}
    rows["Bank Reconciliation.xlsx"].btn_open.click()
    assert wait_for(qapp, lambda: page.spy.opened)
    assert page.spy.opened == [str(page.docs / "Bank Reconciliation.xlsx")]
    rows["October Payroll.xlsx"].btn_reveal.click()
    assert wait_for(qapp, lambda: page.spy.revealed)
    assert page.spy.revealed == [str(page.docs / "October Payroll.xlsx")]


def test_programs_are_refused_in_the_chat(qapp, page):
    page.ask("open run.exe")
    assert wait_for(qapp, lambda: "won't open" in last_bot(page))
    assert page.spy.opened == []


def test_chip_sends_its_text(qapp, page):
    page.chips[4].click()                                        # Help
    assert wait_for(qapp, lambda: "I can find and open your documents" in last_bot(page))


def test_empty_input_and_busy_state(qapp, page):
    page.input.setText("   ")
    page._send()
    assert len(page.messages) == 1                               # nothing was sent
    page.ask("find invoice")
    assert not page.btn_send.isEnabled()                         # busy while it works
    assert wait_for(qapp, lambda: page.btn_send.isEnabled())


def test_clear_chat_resets_to_welcome(qapp, page):
    page.ask("help")
    assert wait_for(qapp, lambda: len(page.messages) == 3)
    page.clear_chat()
    assert len(page.messages) == 1 and page.file_rows() == []


def test_adding_and_removing_search_folders_persists(qapp, page):
    extra = page.tmp / "Extra"
    touch(extra / "Quarterly Forecast.pdf")
    assert page.add_folder(str(extra)) is True
    assert wait_for(qapp, lambda: any(e.name == "Quarterly Forecast.pdf" for e in page.index.entries))
    assert str(extra) in page.db.get_setting("assistant_folders")
    assert page.add_folder(str(page.tmp / "nope")) is False
    page.remove_folder(str(extra))
    assert wait_for(qapp, lambda: not any(e.name == "Quarterly Forecast.pdf" for e in page.index.entries))
    assert str(extra) not in page.db.get_setting("assistant_folders")
    page.reset_folders()                                         # conftest makes the defaults empty
    assert wait_for(qapp, lambda: page.index.ready and page.index.entries == [])
    assert page.db.get_setting("assistant_folders") == ""


def test_folder_menu_lists_current_folders(qapp, page):
    page._fill_folder_menu()
    texts = [a.text() for a in page.menu.actions()]
    assert "Add a folder..." in texts and "Use the default folders" in texts


def test_chat_is_not_saved_anywhere(qapp, page):
    page.ask("find invoice")
    assert wait_for(qapp, lambda: len(page.file_rows()) == 1)
    dump = " ".join(str(tuple(r)) for t in ("settings", "system_events")
                    for r in page.db.query(f"SELECT * FROM {t}"))
    assert "invoice" not in dump.lower() and str(page.docs) not in dump


def test_assistant_is_in_the_main_window_navigation(qapp, tmp_path):
    from services.startup_manager import StartupManager
    db = Database(tmp_path / "w.db").open()
    eng = TrackingEngine(db, snapshot_provider=lambda: None)
    eng.start(run_thread=False)
    w = MainWindow(db, eng, startup=StartupManager(tmp_path / "S", (r"C:\Windows\notepad.exe", "", "C:\\")))
    names = list(w.nav_buttons)
    assert names[-3:] == ["Websites", "Assistant", "Settings"]
    w.show_page(names.index("Assistant"))
    assert w.stack.currentWidget() is w.assistant
    assert wait_for(qapp, lambda: w.assistant.index.ready)       # indexed in the background (nothing to scan here)
    w._quitting = True
    w.close()
    wait_for_jobs()
    db.close()


def test_folder_results_have_an_open_folder_button(qapp, page):
    (page.docs / "Finance").mkdir()
    page.index.build()
    page.ask("find the finance folder")
    assert wait_for(qapp, lambda: len(page.file_rows()) == 1)
    row = page.file_rows()[0]
    assert row.entry.is_dir and row.btn_open.text() == "Open folder" and row.btn_reveal.isHidden()
    row.btn_open.click()
    assert wait_for(qapp, lambda: page.spy.opened)
    assert page.spy.opened == [str(page.docs / "Finance")]
    assert wait_for(qapp, lambda: "Opened the folder Finance" in last_bot(page))
