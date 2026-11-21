import time

import pytest
from PySide6.QtWidgets import QApplication

from assistant.apps import AppEntry, AppIndex, is_blocked_name
from assistant.engine import AssistantEngine, HELP_TEXT
from assistant.file_index import FileIndex
from database.db_manager import Database
from ui.assistant_page import AssistantPage, _AppRow
from ui.workers import wait_for_jobs

START_MENU_NAMES = [
    "Google Chrome", "Google Earth", "Excel", "Word", "Teams", "Teams Classic", "Foxit PDF Editor",
    "Uninstall Chrome", "Chrome Help", "Excel Documentation", "Setup Office", "Check for Updates",     # not the program itself
    "Windows PowerShell", "Command Prompt", "Python 3.13", "Computer Management", "Event Viewer",     # never launched
    "Windows Terminal", "Registry Editor",
]


@pytest.fixture
def menu(tmp_path):
    root = tmp_path / "Start Menu" / "Programs"
    (root / "Sub").mkdir(parents=True)
    for name in START_MENU_NAMES:
        (root / "Sub" / f"{name}.lnk").write_bytes(b"lnk")
    (root / "Some Website.url").write_text("[InternetShortcut]")
    return root


class Spy:
    def __init__(self):
        self.opened, self.revealed, self.launched = [], [], []

    def open(self, p):
        self.opened.append(p)

    def reveal(self, p):
        self.revealed.append(p)

    def launch(self, t):
        self.launched.append(t)


@pytest.fixture
def bot(menu, tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    for name in ("Excel Tips.pdf", "Notepad.txt", "October Payroll.xlsx", "Invoice.pdf", "Report.docx"):
        (docs / name).write_bytes(b"x")
    idx = FileIndex([docs])
    apps = AppIndex([menu])
    spy = Spy()
    eng = AssistantEngine(idx, None, spy.open, spy.reveal, {"documents": docs}, apps=apps, launcher=spy.launch)
    return eng, spy, apps, menu


def lnk(menu, name):
    return str(menu / "Sub" / f"{name}.lnk")


# ----------------------------------------------------------------------- the app list
def test_index_has_installed_apps_and_windows_tools_but_not_the_rest(menu):
    apps = AppIndex([menu])
    apps.build()
    names = {e.name for e in apps.entries}
    assert {"Google Chrome", "Excel", "Word", "Teams", "Foxit PDF Editor"} <= names
    assert {"Notepad", "Calculator", "Paint", "Task Manager", "Settings"} <= names
    for banned in ("Uninstall Chrome", "Chrome Help", "Excel Documentation", "Setup Office", "Check for Updates",
                   "Windows PowerShell", "Command Prompt", "Python 3.13", "Computer Management", "Event Viewer",
                   "Windows Terminal", "Registry Editor", "Some Website"):
        assert banned not in names, banned


@pytest.mark.parametrize("text,blocked", [
    ("Command Prompt", True), ("cmd", True), ("Windows PowerShell", True), ("powershell", True),
    ("Windows Terminal", True), ("Registry Editor", True), ("regedit", True), ("Python 3.13", True),
    ("IDLE (Python 3.13 64-bit)", True), ("Computer Management", True), ("Task Scheduler", True),
    ("Google Chrome", False), ("Excel", False), ("Notepad", False), ("Word", False), ("Microsoft Teams", False),
])
def test_blocked_names(text, blocked):
    assert is_blocked_name(text) is blocked


def test_search_ranks_exact_names_first(menu):
    apps = AppIndex([menu])
    apps.build()
    assert apps.search(["excel"])[0].entry.name == "Excel" and apps.search(["excel"])[0].score == 100
    assert apps.search(["chrome"])[0].entry.name == "Google Chrome"
    assert apps.search(["calc"])[0].entry.name == "Calculator"                     # an alias
    assert apps.search(["zzqq"]) == [] and apps.search([]) == []


def test_only_listed_targets_are_known(menu):
    apps = AppIndex([menu])
    apps.build()
    assert apps.is_known("notepad.exe") and apps.is_known(lnk(menu, "Excel"))
    assert not apps.is_known(r"C:\Windows\System32\cmd.exe") and not apps.is_known(lnk(menu, "Windows PowerShell"))


# ----------------------------------------------------------------------- opening apps by name
def test_open_an_installed_app(bot):
    eng, spy, _, menu = bot
    r = eng.handle("open chrome")
    assert spy.launched == [lnk(menu, "Google Chrome")] and r.launched and "Opened Google Chrome" in r.text
    assert spy.opened == []                                                       # no file was opened
    eng.handle("launch excel")
    assert spy.launched[-1] == lnk(menu, "Excel")
    eng.handle("open Microsoft Word")
    assert spy.launched[-1] == lnk(menu, "Word")
    eng.handle("start teams")
    assert spy.launched[-1] == lnk(menu, "Teams")                                 # exact beats "Teams Classic"


def test_open_a_windows_tool(bot):
    eng, spy, _, _ = bot
    eng.handle("open calculator")
    eng.handle("open notepad")                                                    # even though Notepad.txt exists
    eng.handle("open settings")
    assert spy.launched == ["calc.exe", "notepad.exe", "ms-settings:"] and spy.opened == []


def test_file_words_and_filters_still_mean_files(bot):
    eng, spy, _, _ = bot
    eng.handle("open excel file")
    assert spy.launched == []                                                      # file flow, not the app
    eng.handle("open my latest excel file")
    assert spy.launched == [] and spy.opened[-1].endswith("October Payroll.xlsx")
    r = eng.handle("open word document")
    assert spy.launched == [] and not r.launched
    eng.handle("open pdf")                                                        # a file type, not the Foxit app
    assert spy.launched == []


def test_files_with_a_name_win_over_a_vague_app_match(bot):
    eng, spy, _, _ = bot
    eng.handle("open Invoice.pdf")
    eng.handle("open the report")
    assert spy.launched == [] and spy.opened[0].endswith("Invoice.pdf")


def test_ambiguous_app_asks_instead_of_guessing(bot):
    eng, spy, _, _ = bot
    r = eng.handle("open google")                                                  # Chrome or Earth
    assert spy.launched == [] and "Which app" in r.text
    assert {a.name for a in r.apps} == {"Google Chrome", "Google Earth"}


def test_command_line_and_admin_tools_are_refused(bot):
    eng, spy, _, _ = bot
    for text in ("open command prompt", "open powershell", "launch cmd", "run terminal", "open registry editor",
                 "open python", "open computer management", "open event viewer", "start windows terminal"):
        r = eng.handle(text)
        assert "don't open command-line" in r.text, text
    assert spy.launched == [] and spy.opened == []


def test_typed_program_paths_are_still_refused_with_a_hint(bot):
    eng, spy, _, _ = bot
    for text in ("open chrome.exe", r"open C:\Windows\System32\cmd.exe", "run setup.exe"):
        r = eng.handle(text)
        assert "won't open .exe" in r.text and "just say its name" in r.text, text
    assert spy.launched == [] and spy.opened == []


def test_launch_button_cannot_start_anything_outside_the_list(bot):
    eng, spy, _, menu = bot
    evil = AppEntry("Evil", "evil", r"C:\Users\x\Downloads\evil.exe", "startmenu")
    assert "only open apps from your installed programs list" in eng.launch_app(evil).text
    shell = AppEntry("Windows PowerShell", "windows powershell", lnk(menu, "Windows PowerShell"), "startmenu")
    assert "never command-line" in eng.launch_app(shell).text
    assert spy.launched == []
    ok = AppEntry("Excel", "excel", lnk(menu, "Excel"), "startmenu")
    assert eng.launch_app(ok).launched == lnk(menu, "Excel")


def test_launcher_failure_is_reported(menu, tmp_path):
    idx = FileIndex([tmp_path])

    def failing(target):
        raise OSError("blocked by policy")

    eng = AssistantEngine(idx, None, apps=AppIndex([menu]), launcher=failing)
    r = eng.handle("open excel")
    assert "could not start Excel" in r.text and "blocked by policy" in r.text and not r.launched


def test_help_mentions_apps_and_limits():
    assert "open chrome" in HELP_TEXT and "never scripts" in HELP_TEXT


# --------------------------------------------------------------------------- chat page
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


def test_chat_launches_apps_and_shows_choices(qapp, bot, tmp_path):
    eng, spy, _, menu = bot
    db = Database(tmp_path / "c.db").open()
    page = AssistantPage(db, eng.index, eng)
    page.show()
    page.ask("open chrome")
    assert wait_for(qapp, lambda: spy.launched)
    assert spy.launched == [lnk(menu, "Google Chrome")]
    assert wait_for(qapp, lambda: "Opened Google Chrome" in page.messages[-1][1])
    page.ask("open google")
    assert wait_for(qapp, lambda: len(page.chat.findChildren(_AppRow)) == 2)
    earth = next(r for r in page.chat.findChildren(_AppRow) if r.entry.name == "Google Earth")
    assert earth.btn_open.text() == "Open app"
    earth.btn_open.click()
    assert wait_for(qapp, lambda: len(spy.launched) == 2)
    assert spy.launched[-1] == lnk(menu, "Google Earth")
    page.hide()
    wait_for_jobs()
    db.close()
