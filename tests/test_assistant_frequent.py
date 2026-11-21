import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from analytics import time_calc as tc
from assistant import intent as it
from assistant.engine import AssistantEngine
from assistant.file_index import FileIndex
from database.db_manager import Database
from ui.assistant_page import AssistantPage
from ui.workers import wait_for_jobs

TODAY = date(2026, 10, 21)


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


def touch(path: Path, age_days=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


@pytest.fixture
def world(tmp_path):
    docs = tmp_path / "Documents"
    touch(docs / "Payroll" / "October Payroll.xlsx")
    touch(docs / "Payroll" / "Payroll Summary.xlsx")
    touch(docs / "Finance" / "Bank Reconciliation.xlsx")
    touch(docs / "Finance" / "LC Exposure.xlsx")                       # recorded WITHOUT its extension
    touch(docs / "Old" / "Invoice 2026.pdf", age_days=30)              # same name in two places:
    touch(docs / "Reports" / "Invoice 2026.pdf", age_days=1)           # the newer copy is the one used
    touch(docs / "Unused" / "Never Opened.docx")
    db = Database(tmp_path / "f.db").open()
    a = db.add_closed_session
    X = ("Microsoft Excel", "EXCEL.EXE")
    a(at(TODAY, 9), at(TODAY, 11), "ACTIVE", *X, "October Payroll.xlsx")               # 2h   -> Payroll
    a(at(TODAY, 11), at(TODAY, 11.5), "ACTIVE", *X, "Payroll Summary.xlsx")            # 30m  -> Payroll
    a(at(TODAY, 12), at(TODAY, 13), "ACTIVE", *X, "Bank Reconciliation.xlsx")          # 1h   -> Finance
    a(at(TODAY, 13), at(TODAY, 13 + 1 / 3), "ACTIVE", *X, "LC Exposure")               # 20m  -> Finance
    a(at(TODAY, 14), at(TODAY, 14.25), "ACTIVE", "Microsoft Word", "WINWORD.EXE", "Ghost.docx")   # not on disk
    a(at(TODAY, 15), at(TODAY, 15.1), "ACTIVE", "Foxit PDF Editor", "foxit.exe", "Invoice 2026.pdf")  # 6m -> Reports
    a(at(TODAY - timedelta(days=60), 9), at(TODAY - timedelta(days=60), 20), "ACTIVE", *X,
      "Never Opened.docx")                                                              # outside the 30 days
    a(at(TODAY, 16), at(TODAY, 17), "IDLE", "Idle")                                    # never counts
    idx = FileIndex([docs])
    idx.build()
    spy = []
    eng = AssistantEngine(idx, db, spy.append, lambda p: None, {"documents": docs}, today=lambda: TODAY)
    yield eng, spy, docs, db, idx
    db.close()


@pytest.mark.parametrize("text", [
    "show my most used folders", "my frequently used folders", "which folders do I use most",
    "what are my favorite folders", "top folders", "list the folders I use often"])
def test_parser_recognises_most_used_folder_requests(text):
    assert it.parse(text).kind == it.FOLDERS


def test_parser_period_and_normal_folder_requests_unchanged():
    assert it.parse("top folders this week").period == "This week"
    assert it.parse("show my most used folders").period is None
    assert it.parse("open the payroll folder").kind == it.OPEN and it.parse("open the payroll folder").folder_target
    assert it.parse("open downloads folder").folder_target


def test_folders_ranked_by_time_spent_in_their_documents(world):
    eng, spy, docs, _, _ = world
    r = eng.handle("show my most used folders")
    assert [e.name for e in r.files] == ["Payroll", "Finance", "Reports"]
    assert all(e.is_dir for e in r.files) and r.files[0].path == str(docs / "Payroll")
    assert "1. Payroll - 2h 30m, 2 document(s)" in r.text
    assert "2. Finance - 1h 20m, 2 document(s)" in r.text                          # incl. "LC Exposure" without extension
    assert "3. Reports - 6m 00s, 1 document(s)" in r.text                          # newest copy of the duplicate name
    assert "last 30 days" in r.text
    assert spy == []                                                                 # listing never opens anything


def test_unmatched_old_and_idle_activity_is_ignored(world):
    eng, _, _, _, _ = world
    r = eng.handle("show my most used folders")
    assert "Unused" not in r.text and "Old" not in [e.name for e in r.files]         # 60 days ago, and the older copy
    assert "Ghost" not in r.text


def test_period_can_be_chosen(world):
    eng, _, _, db, _ = world
    r = eng.handle("top folders yesterday")
    assert "no recorded documents for yesterday" in r.text and r.files == []
    assert eng.handle("most used folders this month").files                          # October 2026 holds today's data


def test_nothing_to_show_cases(world, tmp_path):
    _, _, _, _, idx = world
    assert "can't read your activity" in AssistantEngine(idx, None).frequent_folders().text
    empty = Database(tmp_path / "e.db").open()
    assert "no recorded documents" in AssistantEngine(idx, empty, today=lambda: TODAY).frequent_folders().text
    empty.add_closed_session(at(TODAY, 9), at(TODAY, 10), "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "Elsewhere.xlsx")
    r = AssistantEngine(idx, empty, today=lambda: TODAY).frequent_folders()
    assert "couldn't match" in r.text and r.files == []
    empty.close()


def test_folder_rows_open_in_explorer(world):
    eng, spy, docs, _, _ = world
    first = eng.handle("show my most used folders").files[0]
    assert "Opened the folder Payroll" in eng.open_path(first.path).text and spy == [str(docs / "Payroll")]


def test_no_paths_are_stored_anywhere(world):
    eng, _, docs, db, _ = world
    eng.handle("show my most used folders")
    dump = " ".join(str(tuple(r)) for t in ("settings", "system_events", "documents", "applications")
                    for r in db.query(f"SELECT * FROM {t}"))
    assert str(docs) not in dump and "Payroll\\" not in dump


def test_find_by_name_helper(world):
    _, _, docs, _, idx = world
    assert [e.folder for e in idx.find_by_name("Invoice 2026.pdf")] == [str(docs / "Reports"), str(docs / "Old")]
    assert [e.name for e in idx.find_by_name("LC Exposure")] == ["LC Exposure.xlsx"]
    assert idx.find_by_name("lc exposure.XLSX")[0].name == "LC Exposure.xlsx"
    assert idx.find_by_name("nothing like this") == []


# ------------------------------------------------------------------------------- chat page
@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_page_shows_most_used_folders_by_itself_after_indexing(qapp, world):
    eng, spy, docs, db, idx = world
    page = AssistantPage(db, idx, eng)
    page.show()
    page.refresh()                                              # opening the page starts indexing
    assert wait_for(qapp, lambda: len(page.file_rows()) == 3)
    assert "most-used folders" in page.messages[-1][1]
    rows = page.file_rows()
    assert [r.entry.name for r in rows] == ["Payroll", "Finance", "Reports"]
    assert all(r.btn_open.text() == "Open folder" for r in rows)
    rows[0].btn_open.click()
    assert wait_for(qapp, lambda: spy)
    assert spy == [str(docs / "Payroll")]
    page.hide()
    wait_for_jobs()


def test_chip_asks_for_the_folders(qapp, world):
    eng, _, _, db, idx = world
    page = AssistantPage(db, idx, eng)
    page.show()
    next(c for c in page.chips if c.text() == "My most used folders").click()
    assert wait_for(qapp, lambda: len(page.file_rows()) == 3)
    page.hide()
    wait_for_jobs()


def test_page_stays_quiet_when_there_is_nothing_to_show(qapp, tmp_path):
    db = Database(tmp_path / "q.db").open()
    idx = FileIndex([tmp_path])
    page = AssistantPage(db, idx, AssistantEngine(idx, db))
    page.show()
    page.refresh()
    assert wait_for(qapp, lambda: page.index.ready and "Searching" in page.sub.text())
    wait_for_jobs()
    qapp.processEvents()
    assert len(page.messages) == 1 and page.file_rows() == []        # just the welcome message
    page.hide()
    db.close()
