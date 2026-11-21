"""Phase 9 checks: no network use anywhere, and acceptable speed on a large history."""
import ast
import socket
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analytics import time_calc as tc
from analytics.app_reports import application_usage
from analytics.dashboard_data import build_dashboard_data
from analytics.report_data import build_report
from analytics.periods import preset_range
from database.db_manager import Database
from exports.excel_export import export_excel
from exports.pdf_export import export_pdf
from tracker.engine import TrackingEngine
from tracker.session_manager import Snapshot
from tracker.window_detector import WindowInfo

ROOT = Path(__file__).resolve().parents[1]
BANNED = {"urllib", "urllib3", "requests", "http", "httpx", "aiohttp", "ftplib", "smtplib",
          "telnetlib", "websocket", "websockets", "xmlrpc", "socket", "ssl"}
SKIP_DIRS = {".venv", "tests", "build", "dist", "__pycache__", ".pytest_cache"}


def project_sources():
    for p in ROOT.rglob("*.py"):
        if not SKIP_DIRS & set(p.relative_to(ROOT).parts):
            yield p


def test_no_source_file_imports_a_networking_library():
    offenders = []
    for path in project_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in BANNED:
                    offenders.append(f"{path.relative_to(ROOT)}: import {n}")
    # selftest.py imports socket ONLY to prove that blocking it changes nothing.
    offenders = [o for o in offenders if not o.startswith("selftest.py")]
    offenders = [o for o in offenders if not o.endswith("import urllib.parse")]   # string splitting only, no network
    assert offenders == []


def test_no_url_is_ever_opened_or_hardcoded_for_requests():
    for path in project_sources():
        text = path.read_text(encoding="utf-8")
        assert "urlopen" not in text and "requests.get" not in text, path


def test_full_workflow_runs_with_every_network_call_forbidden(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    db = Database(tmp_path / "o.db").open()
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    w = WindowInfo(1, 1, "A.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
    eng = TrackingEngine(db, snapshot_provider=lambda: Snapshot(clock["t"], w, clock["t"], False),
                         clock=lambda: clock["t"])
    eng.start(run_thread=False)
    for _ in range(100):
        eng.monitor.tick()
        clock["t"] += timedelta(seconds=2)
    eng.stop()
    data = build_report(db, "Custom", today=date(2026, 10, 21),
                        custom=(date(2026, 10, 19), date(2026, 10, 21)),
                        now=t0 + timedelta(days=5))
    export_excel(data, tmp_path / "x.xlsx")
    export_pdf(data, tmp_path / "x.pdf")
    assert (tmp_path / "x.xlsx").exists() and (tmp_path / "x.pdf").exists()
    db.close()


# ----------------------------------------------------------------------- scale
@pytest.fixture(scope="module")
def big_db(tmp_path_factory):
    """~60,000 sessions (about 2 years of heavy use)."""
    db = Database(tmp_path_factory.mktemp("big") / "big.db").open()
    today = date(2026, 10, 21)
    rows = []
    apps = [("Microsoft Excel", "EXCEL.EXE", True), ("Google Chrome", "chrome.exe", False),
            ("Microsoft Word", "WINWORD.EXE", True)]
    from database.models import to_ts
    for back in range(1, 731):
        t = tc.local_day_bounds(today - timedelta(days=back))[0] + timedelta(hours=8)
        for i in range(40):
            name, exe, has_doc = apps[i % 3]
            end = t + timedelta(minutes=10)
            doc = f"Doc{i % 9}.xlsx" if has_doc else None
            rows.append((to_ts(t), to_ts(end), name, exe, doc, "ACTIVE", 600.0))
            t = end
            end = t + timedelta(minutes=2)
            rows.append((to_ts(t), to_ts(end), "Idle", "", None, "IDLE", 120.0))
            t = end
    with db.transaction() as c:
        c.executemany("INSERT INTO activity_sessions(start_timestamp, end_timestamp, application_name,"
                      " executable_name, document_name, activity_status, duration_seconds)"
                      " VALUES (?,?,?,?,?,?,?)", rows)
    yield db, today, len(rows)
    db.close()


def timed(fn):
    t = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t


def test_scale_dashboard_for_a_day_is_fast(big_db):
    db, today, n = big_db
    assert n == 58400
    _, secs = timed(lambda: build_dashboard_data(db, today - timedelta(days=5),
                                                 now=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    print(f"\n  dashboard for one day over {n} sessions: {secs:.2f}s")
    assert secs < 2.0


def test_scale_all_time_application_report(big_db):
    db, today, n = big_db
    lo, hi = preset_range("All time", today)
    df, secs = timed(lambda: application_usage(db, lo, hi, today, datetime(2030, 1, 1, tzinfo=timezone.utc)))
    print(f"\n  all-time application report over {n} sessions: {secs:.2f}s")
    assert secs < 20.0
    assert df.total.sum() == pytest.approx(730 * 40 * 600)      # exact: no lost or doubled time


def test_scale_monthly_report_builds_and_reconciles(big_db):
    db, today, n = big_db
    data, secs = timed(lambda: build_report(db, "Last month", today=today,
                                            now=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    print(f"\n  last-month report: {secs:.2f}s")
    assert secs < 20.0 and data.reconciles


def test_scale_indexed_range_query_uses_index(big_db):
    db, today, _ = big_db
    plan = db.query("EXPLAIN QUERY PLAN SELECT * FROM activity_sessions WHERE start_timestamp < ? "
                    "AND (end_timestamp IS NULL OR end_timestamp > ?) ORDER BY start_timestamp, id",
                    ("2026-01-01T00:00:00.000000Z", "2025-12-01T00:00:00.000000Z"))
    assert any("idx_sessions" in str(tuple(r)) for r in plan)


def test_tracker_does_not_write_on_every_sample(tmp_path):
    """Avoid unnecessary database writes: 1 hour of steady work = a handful of rows."""
    db = Database(tmp_path / "w.db").open()
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    w = WindowInfo(1, 1, "A.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")
    eng = TrackingEngine(db, snapshot_provider=lambda: Snapshot(clock["t"], w, clock["t"], False),
                         clock=lambda: clock["t"])
    eng.start(run_thread=False)
    for _ in range(1800):                       # 1 hour of 2-second samples
        eng.monitor.tick()
        clock["t"] += timedelta(seconds=2)
    eng.stop()
    assert db.query("SELECT COUNT(*) c FROM activity_sessions")[0]["c"] == 1
    db.close()
