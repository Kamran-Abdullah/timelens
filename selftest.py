"""Built-in self-check: exercises every part of TimeLens in a throw-away folder.

    TimeLens.exe --selftest result.txt      (or: python main.py --selftest result.txt)

It never touches your real data. It is how the packaged EXE is verified: if a library or
file is missing from the bundle, a step below fails and result.txt says which one.
Exit code 0 = all steps passed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def run_selftest(result_path: str) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"      # no window flashes during the check
    lines: list[str] = []
    failures = 0
    tmp = Path(tempfile.mkdtemp(prefix="timelens-selftest-"))

    def step(name, fn):
        nonlocal failures
        t0 = time.perf_counter()
        try:
            detail = fn()
            lines.append(f"PASS  {name}  ({time.perf_counter() - t0:.1f}s){'  ' + str(detail) if detail else ''}")
        except Exception:
            failures += 1
            lines.append(f"FAIL  {name}\n" + "".join("      " + l for l in traceback.format_exc().splitlines(True)))

    ctx: dict = {}
    frozen = getattr(sys, "frozen", False)
    lines.append(f"TimeLens self-test  (frozen EXE: {frozen}, python {sys.version.split()[0]})")

    def database():
        from database.db_manager import Database
        ctx["db"] = Database(tmp / "t.db").open()
        return f"schema ok, integrity={ctx['db'].integrity_ok()}"

    def tracking():
        from tracker.engine import TrackingEngine
        from tracker.session_manager import Snapshot
        from tracker.window_detector import WindowInfo, get_foreground_window
        get_foreground_window()                       # real Win32 call must work
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        w = WindowInfo(1, 1, "Payroll.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
        clock = {"t": t0}
        eng = TrackingEngine(ctx["db"], snapshot_provider=lambda: Snapshot(clock["t"], w, clock["t"], False),
                             clock=lambda: clock["t"])
        eng.start(run_thread=False)
        for _ in range(30):
            eng.monitor.tick()
            clock["t"] += timedelta(seconds=2)
        eng.stop()
        n = ctx["db"].query("SELECT COUNT(*) c FROM activity_sessions WHERE document_name='Payroll.xlsx'")[0]["c"]
        assert n == 1, "document session not stored"
        ctx["engine"] = eng

    def power():
        from tracker.power_monitor import PowerMonitor
        events = []
        pm = PowerMonitor(events.append)
        assert pm.start(), "power monitor window did not start"
        pm.stop()

    def startup():
        from services.startup_manager import StartupManager
        m = StartupManager(tmp / "Startup", (sys.executable, "--background", str(tmp)))
        m.enable()
        assert m.is_enabled() and m.points_to_current()
        m.disable()
        assert not m.is_enabled()

    def ui():
        from PySide6.QtWidgets import QApplication
        from services.startup_manager import StartupManager
        from tracker.engine import TrackingEngine
        from ui.main_window import MainWindow
        from ui.workers import wait_for_jobs
        app = QApplication.instance() or QApplication([])
        ctx["db"].set_setting("assistant_folders", str(tmp))    # keep the self-test inside its temp folder
        eng = TrackingEngine(ctx["db"], snapshot_provider=lambda: None)
        eng.start(run_thread=False)
        win = MainWindow(ctx["db"], eng, startup=StartupManager(tmp / "S2", (sys.executable, "", str(tmp))))
        win.show()
        for i in range(len(win.nav_buttons)):         # open every page, let its data load, draw charts
            win.show_page(i)
            end = time.time() + 1.5
            while time.time() < end:
                app.processEvents()
                time.sleep(0.01)
        win._quitting = True
        win.close()
        wait_for_jobs()
        return f"{len(win.nav_buttons)} pages opened"

    def reports():
        from analytics.report_data import build_report
        from exports.excel_export import export_excel
        from exports.pdf_export import export_pdf
        data = build_report(ctx["db"], "Today", today=date.today())
        x = export_excel(data, tmp / "r.xlsx")
        p = export_pdf(data, tmp / "r.pdf")
        assert x.stat().st_size > 3000 and p.stat().st_size > 3000
        assert p.read_bytes().startswith(b"%PDF")
        from openpyxl import load_workbook
        assert "Summary" in load_workbook(x).sheetnames
        return f"xlsx {x.stat().st_size // 1024} KB, pdf {p.stat().st_size // 1024} KB, reconciles={data.reconciles}"

    def backup():
        from services import backup_manager as bm
        b = bm.create_backup(ctx["db"].path, tmp / "bk.db")
        assert bm.validate_backup(b) >= 1

    def assistant():
        from assistant.engine import AssistantEngine
        from assistant.file_index import FileIndex, default_roots
        docs = tmp / "docs"
        docs.mkdir()
        (docs / "October Payroll.xlsx").write_bytes(b"x")
        (docs / "run.exe").write_bytes(b"x")
        idx = FileIndex([docs])
        idx.build()
        opened: list = []
        started: list = []
        (docs / "Finance").mkdir()
        idx.build()
        bot = AssistantEngine(idx, ctx["db"], opened.append, lambda p: None, launcher=started.append)
        bot.handle("open October Payroll.xlsx")
        assert opened == [str(docs / "October Payroll.xlsx")], "did not open the document"
        assert "won't open" in bot.handle("open run.exe").text, "did not refuse a program"
        bot.handle("open the finance folder")
        assert opened[-1] == str(docs / "Finance"), "did not open the folder"
        bot.handle("open calculator")
        assert started == ["calc.exe"], "did not launch an app"
        assert "don't open command-line" in bot.handle("open powershell").text, "launched a shell"
        assert started == ["calc.exe"] and len(opened) == 2
        assert bot.handle("show my most used folders").text            # runs without error
        return f"file, folder and app opened; program and shell refused; {len(default_roots())} default search folder(s)"

    def websites():
        import sqlite3
        from database.db_manager import Database
        from services import web_history as wh
        hist = tmp / "History"
        conn = sqlite3.connect(hist)
        conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
        conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER, visit_time INTEGER, "
                     "visit_duration INTEGER)")
        conn.execute("INSERT INTO urls VALUES (1, 'https://www.google.com/search?q=hello+world&secret=1', 'hello world')")
        conn.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES (1, ?, 5000000)",
                     (wh.datetime_to_webkit(datetime.now(timezone.utc) - timedelta(minutes=5)),))
        conn.commit()
        conn.close()
        site_db = Database(tmp / "web.db").open()
        try:
            assert wh.import_history(site_db, {"chrome:Default": hist}).new_visits == 0, "imported while OFF"
            wh.enable_tracking(site_db, 1)
            assert wh.import_history(site_db, {"chrome:Default": hist}).new_visits == 1
            row = site_db.query("SELECT domain, search_query FROM web_visits")[0]
            assert (row["domain"], row["search_query"]) == ("google.com", "hello world")
            assert "secret" not in str(tuple(site_db.query("SELECT * FROM web_visits")[0]))
        finally:
            site_db.close()
        return f"off by default; imports site+search only; {len(wh.history_sources(['chrome', 'edge']))} browser profile(s) found here"

    def offline():
        import socket
        def blocked(*a, **k):
            raise AssertionError("network access attempted")
        real = socket.socket.connect, socket.getaddrinfo
        socket.socket.connect, socket.getaddrinfo = blocked, blocked
        try:
            from analytics.report_data import build_report
            build_report(ctx["db"], "Today", today=date.today())
        finally:
            socket.socket.connect, socket.getaddrinfo = real

    step("database create + migrations", database)
    step("tracking engine stores a document session", tracking)
    step("Windows power/lock/sleep event window", power)
    step("Startup-folder shortcut", startup)
    step("main window, all pages, charts", ui)
    step("Excel + PDF reports", reports)
    step("backup + validate", backup)
    step("file assistant: find, open, refuse programs", assistant)
    step("optional website tracking (off by default)", websites)
    step("works with network blocked", offline)

    try:
        ctx["db"].close()
    except Exception:
        pass
    lines.append("RESULT: " + ("ALL PASSED" if failures == 0 else f"{failures} STEP(S) FAILED"))
    Path(result_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if failures == 0 else 1
