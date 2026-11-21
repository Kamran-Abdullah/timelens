import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analytics import time_calc as tc
from assistant import intent as it
from assistant.engine import AssistantEngine, HELP_TEXT
from assistant.file_index import FileIndex, dedupe_roots, normalize
from assistant.safety import check_openable
from database.db_manager import Database

DAY = date(2026, 10, 21)


# ----------------------------------------------------------------------- parser
@pytest.mark.parametrize("text,kind,terms,exts,place,period,newest,exact", [
    ("open October Payroll.xlsx", it.OPEN, [], None, None, None, False, "October Payroll.xlsx"),
    ("find invoices in downloads", it.SEARCH, ["invoices"], None, "downloads", None, False, None),
    ("show my recent excel files", it.SEARCH, [], (".xlsx", ".xlsm", ".xls", ".xlsb", ".csv"), None, None, True, None),
    ("open my latest pdf", it.OPEN, [], (".pdf",), None, None, True, None),
    ("where is the bank reconciliation file", it.REVEAL, ["bank", "reconciliation"], None, None, None, False, None),
    ("payroll", it.SEARCH, ["payroll"], None, None, None, False, None),
    ('open "Board Minutes"', it.OPEN, ["board", "minutes"], None, None, None, False, None),
    ("list word documents from last week", it.SEARCH, [], (".docx", ".doc", ".docm", ".rtf"), None, "Last week", False, None),
    ("Can you open the payroll file please", it.OPEN, ["payroll"], None, None, None, False, None),
    ("show me the folder of invoice 2026.pdf", it.REVEAL, [], None, None, None, False, "invoice 2026.pdf"),
    ("recent files", it.RECENT, [], None, None, None, True, None),
    ("search pdf bank statement in my documents", it.SEARCH, ["bank", "statement"], (".pdf",), "documents", None, False, None),
])
def test_parser(text, kind, terms, exts, place, period, newest, exact):
    i = it.parse(text)
    assert (i.kind, i.terms, i.exts, i.place, i.period, i.newest, i.exact_name) == (
        kind, terms, exts, place, period, newest, exact)


@pytest.mark.parametrize("text,period", [
    ("what did I work on today", "Today"), ("what did i work on yesterday", "Yesterday"),
    ("which documents did I use this week", "This week"), ("summary", "Today"),
    ("how much time did I spend last month", "Last month"),
])
def test_parser_activity_questions(text, period):
    i = it.parse(text)
    assert i.kind == it.ACTIVITY and i.period == period


def test_parser_help_and_blank_and_blocked_and_paths():
    assert it.parse("help").kind == it.HELP and it.parse("   ").kind == it.HELP
    assert it.parse("open setup.exe").blocked_name == "setup.exe"
    assert it.parse("open invoice.pdf.exe").blocked_name is not None
    assert it.parse(r"open C:\Users\me\Desktop\a b.xlsx").path == r"C:\Users\me\Desktop\a b.xlsx"
    assert it.parse("find notes").terms == ["notes"]            # 'notes' is a name word, not a file type


# ----------------------------------------------------------------------- safety
@pytest.mark.parametrize("name,ok", [
    ("report.xlsx", True), ("A.DOCX", True), ("scan.pdf", True), ("pic.jpg", True), ("song.mp3", True),
    ("run.exe", False), ("x.bat", False), ("x.cmd", False), ("s.ps1", False), ("s.vbs", False),
    ("s.js", False), ("setup.msi", False), ("link.lnk", False), ("page.html", False), ("pic.svg", False),
    ("invoice.pdf.exe", False), ("report.xlsx.bat", False), ("evil.exe.", False), ("evil.exe ", False),
    ("noextension", False), ("weird.xyz", False), (".gitignore", False),
])
def test_only_documents_can_be_opened(name, ok):
    allowed, reason = check_openable(name)
    assert allowed is ok
    assert (reason == "") == ok


def test_refusal_explains_why():
    assert "won't open .exe" in check_openable("a.exe")[1]


# ---------------------------------------------------------------- tree fixture
def touch(path: Path, age_days: float = 0, size: int = 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "Documents"
    dl = tmp_path / "Downloads"
    desk = tmp_path / "Desktop"
    touch(d / "October Payroll.xlsx", 3)
    touch(d / "Bank Reconciliation.xlsx", 1)
    touch(d / "Policy Draft.docx", 10)
    touch(d / "Invoice 2026.pdf", 5)
    touch(d / "notes.txt", 40)
    touch(d / "photo.png", 2)
    touch(d / "run.exe")
    touch(d / "setup.bat")
    touch(d / "script.ps1")
    touch(d / "invoice.pdf.exe")
    touch(d / "~$October Payroll.xlsx")                 # Office lock file
    touch(d / "node_modules" / "skipme.pdf")
    touch(d / ".hidden" / "skipme2.pdf")
    touch(d / "dist" / "skipme3.pdf")
    touch(dl / "Invoice March.pdf", 0.5)
    touch(dl / "Invoice 2026.pdf", 20)                  # same name as one in Documents
    touch(desk / "Payroll Summary.xlsx", 0.2)
    idx = FileIndex([d, dl, desk])
    idx.build()
    return tmp_path, idx


# ------------------------------------------------------------------------ index
def test_index_contains_only_safe_files_and_skips_junk(tree):
    _, idx = tree
    names = sorted(e.name for e in idx.entries)
    assert names == sorted(["October Payroll.xlsx", "Bank Reconciliation.xlsx", "Policy Draft.docx",
                            "Invoice 2026.pdf", "notes.txt", "photo.png", "Invoice March.pdf",
                            "Invoice 2026.pdf", "Payroll Summary.xlsx"])
    assert not any(e.ext in (".exe", ".bat", ".ps1") for e in idx.entries)


def test_index_is_memory_only_and_reports_state(tree):
    _, idx = tree
    assert idx.ready and idx.build_seconds >= 0 and not idx.truncated


def test_index_limits(tmp_path):
    for i in range(30):
        touch(tmp_path / f"f{i}.pdf")
    idx = FileIndex([tmp_path], max_files=10)
    assert idx.build() == 10 and idx.truncated
    deep = tmp_path / "a" / "b" / "c" / "d.pdf"
    touch(deep)
    shallow = FileIndex([tmp_path / "a"], max_depth=1)
    shallow.build()
    assert not any(e.name == "d.pdf" for e in shallow.entries)


def test_ranking_exact_then_prefix_then_contains(tree):
    _, idx = tree
    top = lambda *t, **k: [h.entry.name for h in idx.search(list(t), **k)]
    assert top("october", "payroll")[0] == "October Payroll.xlsx"          # all words
    assert top("payroll")[0] == "Payroll Summary.xlsx"                      # starts with beats contains
    assert top("bank reconciliation")[0] == "Bank Reconciliation.xlsx"      # exact
    assert top("recon")[0] == "Bank Reconciliation.xlsx"


def test_spelling_mistakes_still_find_the_file(tree):
    _, idx = tree
    assert idx.search(["reconcilation"])[0].entry.name == "Bank Reconciliation.xlsx"
    assert idx.search(["zzzzqqq"]) == []


def test_filters_ext_folder_and_time(tree):
    root, idx = tree
    assert {h.entry.ext for h in idx.search([], exts=(".xlsx",))} == {".xlsx"}
    in_dl = idx.search(["invoice"], folder=root / "Downloads")
    assert {h.entry.name for h in in_dl} == {"Invoice March.pdf", "Invoice 2026.pdf"}
    assert all("Downloads" in h.entry.path for h in in_dl)
    since = datetime.now() - timedelta(days=4)
    recent = {h.entry.name for h in idx.search([], since=since)}
    assert "Policy Draft.docx" not in recent and "Bank Reconciliation.xlsx" in recent
    old = {h.entry.name for h in idx.search([], until=since)}
    assert "Policy Draft.docx" in old and "Bank Reconciliation.xlsx" not in old


def test_newest_first_and_limit(tree):
    _, idx = tree
    names = [h.entry.name for h in idx.search([], exts=(".xlsx",), newest_first=True)]
    assert names == ["Payroll Summary.xlsx", "Bank Reconciliation.xlsx", "October Payroll.xlsx"]
    assert len(idx.search([], limit=2)) == 2


def test_helpers():
    assert normalize("October_Payroll-2026.xlsx") == "october payroll 2026 xlsx"
    assert dedupe_roots([Path(".")]) == [Path(".")]


def test_nested_roots_are_not_scanned_twice(tmp_path):
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    assert dedupe_roots([tmp_path, inner, tmp_path / "missing"]) == [tmp_path]


# ----------------------------------------------------------------------- engine
class Spy:
    def __init__(self):
        self.opened, self.revealed = [], []

    def open(self, p):
        self.opened.append(p)

    def reveal(self, p):
        self.revealed.append(p)


@pytest.fixture
def bot(tree, tmp_path):
    root, idx = tree
    spy = Spy()
    folders = {"documents": root / "Documents", "downloads": root / "Downloads", "desktop": root / "Desktop"}
    db = Database(tmp_path / "a.db").open()
    eng = AssistantEngine(idx, db, spy.open, spy.reveal, folders, today=lambda: DAY)
    yield eng, spy, root, db
    db.close()


def test_open_exact_name_opens_exactly_that_file(bot):
    eng, spy, root, _ = bot
    r = eng.handle("open October Payroll.xlsx")
    assert spy.opened == [str(root / "Documents" / "October Payroll.xlsx")]
    assert r.opened and "Opened October Payroll.xlsx" in r.text


def test_open_by_description_when_unambiguous(bot):
    eng, spy, root, _ = bot
    eng.handle("can you open the bank reconciliation file please")
    assert spy.opened == [str(root / "Documents" / "Bank Reconciliation.xlsx")]


def test_ambiguous_open_asks_instead_of_guessing(bot):
    eng, spy, _, _ = bot
    r = eng.handle("open payroll")                       # October Payroll and Payroll Summary
    assert spy.opened == [] and len(r.files) == 2 and "Which one" in r.text
    r = eng.handle("open invoice 2026.pdf")              # same name in two folders
    assert spy.opened == [] and len(r.files) == 2


def test_open_latest_excel(bot):
    eng, spy, root, _ = bot
    eng.handle("open my latest excel file")
    assert spy.opened == [str(root / "Desktop" / "Payroll Summary.xlsx")]


def test_programs_and_scripts_are_never_opened(bot):
    eng, spy, root, _ = bot
    for text in ("open run.exe", "launch setup.bat", "open invoice.pdf.exe", "run script.ps1",
                 rf"open {root}\Documents\run.exe"):
        r = eng.handle(text)
        assert "won't open" in r.text, text
    assert spy.opened == []
    assert "won't open" in eng.open_path(str(root / "Documents" / "run.exe")).text     # even by direct call


def test_open_full_path(bot):
    eng, spy, root, _ = bot
    target = root / "Documents" / "Policy Draft.docx"
    eng.handle(f"open {target}")
    assert spy.opened == [str(target)]
    r = eng.handle(rf"open {root}\Documents\missing.pdf")
    assert "can't find" in r.text and len(spy.opened) == 1


def test_search_lists_and_never_opens(bot):
    eng, spy, _, _ = bot
    r = eng.handle("find invoice")
    assert {e.name for e in r.files} == {"Invoice 2026.pdf", "Invoice March.pdf"} and spy.opened == []
    assert "Found 3 matches" in r.text
    r = eng.handle("find invoices in downloads")
    assert all("Downloads" in e.path for e in r.files) and len(r.files) == 2


def test_recent_files_and_type_filter(bot):
    eng, spy, _, _ = bot
    r = eng.handle("show my recent excel files")
    assert [e.name for e in r.files] == ["Payroll Summary.xlsx", "Bank Reconciliation.xlsx", "October Payroll.xlsx"]
    assert "newest first" in r.text
    r = eng.handle("recent files")
    assert r.files[0].name == "Payroll Summary.xlsx"


def test_no_results_message_is_helpful(bot):
    eng, _, _, _ = bot
    r = eng.handle("find quarterly forecast")
    assert r.files == [] and "couldn't find" in r.text and "9 documents" in r.text


def test_where_is_shows_in_explorer(bot):
    eng, spy, root, _ = bot
    r = eng.handle("where is the bank reconciliation file")
    assert spy.revealed == [str(root / "Documents" / "Bank Reconciliation.xlsx")] and r.revealed
    r = eng.handle("where is invoice 2026.pdf")           # two copies: list them instead
    assert len(r.files) == 2 and len(spy.revealed) == 1


def test_file_deleted_after_indexing(bot):
    eng, spy, root, _ = bot
    (root / "Documents" / "Policy Draft.docx").unlink()
    r = eng.handle("open Policy Draft.docx")
    assert "can't find" in r.text and spy.opened == []


def test_opener_failure_is_reported_not_raised(bot, tree):
    _, idx = tree
    eng = AssistantEngine(idx, None, lambda p: (_ for _ in ()).throw(OSError("no app")), None)
    r = eng.handle("open notes.txt")
    assert "could not open notes.txt" in r.text and "no app" in r.text


def test_help(bot):
    eng, *_ = bot
    assert eng.handle("help").text == HELP_TEXT and "never scripts" in HELP_TEXT


# ---------------------------------------------------------- activity (database)
def test_activity_summary_uses_recorded_history(bot):
    eng, spy, root, db = bot
    start = tc.local_day_bounds(DAY)[0]
    db.add_closed_session(start + timedelta(hours=9), start + timedelta(hours=11), "ACTIVE", "Microsoft Excel",
                          "EXCEL.EXE", "October Payroll.xlsx")
    db.add_closed_session(start + timedelta(hours=11), start + timedelta(hours=11, minutes=30), "ACTIVE",
                          "Microsoft Word", "WINWORD.EXE", "Missing Everywhere.docx")
    r = eng.handle("what did I work on today")
    assert "2 application(s) and 2 document(s)" in r.text
    assert "October Payroll.xlsx - 2h 00m (Microsoft Excel)" in r.text
    assert "Microsoft Excel (2h 00m)" in r.text
    assert [e.name for e in r.files] == ["October Payroll.xlsx"]       # only the one found on disk
    assert "couldn't match" not in r.text
    assert spy.opened == []                                              # a summary never opens anything
    assert "no recorded activity for yesterday" in eng.handle("what did i work on yesterday").text


def test_activity_with_no_database(tree):
    _, idx = tree
    assert "can't read" in AssistantEngine(idx, None).handle("what did I work on today").text


def test_assistant_never_writes_to_the_database(bot):
    eng, _, _, db = bot
    before = {t: db.query(f"SELECT COUNT(*) c FROM {t}")[0]["c"]
              for t in ("activity_sessions", "documents", "applications", "system_events")}
    snapshot = [tuple(r) for r in db.query("SELECT key, value FROM settings ORDER BY key")]
    for text in ("open October Payroll.xlsx", "find invoice", "where is notes.txt", "what did I work on today",
                 "open run.exe", "help"):
        eng.handle(text)
    after = {t: db.query(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in before}
    assert after == before
    assert [tuple(r) for r in db.query("SELECT key, value FROM settings ORDER BY key")] == snapshot


def test_file_paths_are_not_stored_in_the_database(bot):
    eng, _, root, db = bot
    eng.handle("find invoice")
    dump = " ".join(str(tuple(r)) for t in ("settings", "system_events", "documents")
                    for r in db.query(f"SELECT * FROM {t}"))
    assert str(root) not in dump and "Downloads" not in dump


# ------------------------------------------------------------------------ folders
@pytest.fixture
def fbot(tree, tmp_path):
    root, idx = tree
    for rel in ("Documents/Payroll", "Documents/Finance/Bank Statements", "Downloads/Archive 2025",
                "Documents/Old Archive", "Documents/node_modules/pkg", "Documents/.hidden/x"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    idx.build()
    spy = Spy()
    folders = {"documents": root / "Documents", "downloads": root / "Downloads", "desktop": root / "Desktop"}
    return AssistantEngine(idx, None, spy.open, spy.reveal, folders), spy, root, idx


@pytest.mark.parametrize("text,kind,terms,place,folder", [
    ("open downloads folder", it.OPEN, [], "downloads", True),
    ("open my downloads folder please", it.OPEN, [], "downloads", True),
    ("open the payroll folder", it.OPEN, ["payroll"], None, True),
    ("open downloads", it.OPEN, [], "downloads", True),
    ("find the bank statements folder", it.SEARCH, ["bank", "statements"], None, True),
    ("where is the payroll folder", it.REVEAL, ["payroll"], None, True),
    ("open folder payroll in documents", it.OPEN, ["payroll"], "documents", True),
    ("open folder of invoice 2026.pdf", it.REVEAL, [], None, False),     # that means "show the FILE's folder"
    ("open my latest pdf", it.OPEN, [], None, False),
    ("open payroll", it.OPEN, ["payroll"], None, False),
])
def test_parser_folder_requests(text, kind, terms, place, folder):
    i = it.parse(text)
    assert (i.kind, i.terms, i.place, i.folder_target) == (kind, terms, place, folder)


def test_index_records_folders_but_not_junk(fbot):
    _, _, _, idx = fbot
    names = {e.name for e in idx.folders}
    assert {"Payroll", "Finance", "Bank Statements", "Archive 2025", "Old Archive"} <= names
    assert not names & {"node_modules", "pkg", ".hidden", "x", "dist"}
    assert all(e.is_dir for e in idx.folders) and not any(e.is_dir for e in idx.entries)


def test_open_a_known_folder_by_name(fbot):
    eng, spy, root, _ = fbot
    r = eng.handle("open my downloads folder")
    assert spy.opened == [str(root / "Downloads")] and r.opened and "Opened the folder Downloads" in r.text
    eng.handle("open desktop")
    assert spy.opened[-1] == str(root / "Desktop")


def test_open_a_folder_found_by_search(fbot):
    eng, spy, root, _ = fbot
    eng.handle("open the payroll folder")
    assert spy.opened == [str(root / "Documents" / "Payroll")]
    eng.handle("open the bank statements folder")
    assert spy.opened[-1] == str(root / "Documents" / "Finance" / "Bank Statements")
    eng.handle("open folder archive in downloads")                          # restricted to Downloads
    assert spy.opened[-1] == str(root / "Downloads" / "Archive 2025")


def test_ambiguous_folder_asks_instead_of_guessing(fbot):
    eng, spy, _, _ = fbot
    r = eng.handle("open the archive folder")                                # Archive 2025 and Old Archive
    assert spy.opened == [] and {e.name for e in r.files} == {"Archive 2025", "Old Archive"}
    assert "Which folder" in r.text and all(e.is_dir for e in r.files)


def test_find_folder_lists_and_where_is_shows_it(fbot):
    eng, spy, root, _ = fbot
    r = eng.handle("find the payroll folder")
    assert [e.name for e in r.files] == ["Payroll"] and spy.opened == []
    eng.handle("where is the finance folder")
    assert spy.revealed == [str(root / "Documents" / "Finance")]


def test_missing_folder_and_unknown_name(fbot):
    eng, spy, _, _ = fbot
    assert "couldn't find a folder called 'zzqq'" in eng.handle("open the zzqq folder").text
    assert spy.opened == []


def test_typed_folder_path_and_folder_row_button(fbot):
    eng, spy, root, _ = fbot
    target = root / "Documents" / "Payroll"
    r = eng.handle(f"open {target}")
    assert spy.opened == [str(target)] and r.opened
    spy.opened.clear()
    assert "Opened the folder" in eng.open_path(str(root / "Downloads")).text       # a folder result row
    assert spy.opened == [str(root / "Downloads")]
    assert "can't find the folder" in eng.open_folder(str(root / "gone")).text


def test_opening_a_folder_never_runs_anything_and_files_still_checked(fbot):
    eng, spy, root, _ = fbot
    assert "won't open" in eng.handle(f"open {root}/Documents/run.exe").text
    assert "can't find the folder" in eng.open_folder(str(root / "Documents" / "notes.txt")).text   # a file is not a folder
