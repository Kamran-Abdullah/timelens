import re
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from analytics import productivity as pr
from analytics import time_calc as tc
from analytics.report_data import SECTIONS, build_report
from database.db_manager import Database
from exports.excel_export import export_excel
from exports.pdf_export import export_pdf

TODAY = date(2026, 10, 21)       # Wednesday
YDAY = date(2026, 10, 20)
NOW = tc.local_day_bounds(date(2026, 11, 1))[0]
X, C, T = ("Microsoft Excel", "EXCEL.EXE"), ("Google Chrome", "chrome.exe"), ("Microsoft Teams", "ms-teams.exe")


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "x.db").open()
    a = d.add_closed_session
    a(at(YDAY, 9), at(YDAY, 9.75), "ACTIVE", *X, "October Payroll.xlsx")
    a(at(YDAY, 9.75), at(YDAY, 10.25), "ACTIVE", *X, "Bank Reconciliation.xlsx")
    a(at(YDAY, 10.25), at(YDAY, 10.5), "ACTIVE", *C)
    a(at(YDAY, 10.5), at(YDAY, 10.5 + 1 / 3), "IDLE", "Idle")
    a(at(YDAY, 11), at(YDAY, 11.5), "ACTIVE", *T)
    a(at(YDAY, 11.5), at(YDAY, 12), "LOCKED", "Windows", "LockApp.exe")
    a(at(YDAY, 12), at(YDAY, 12.5), "SLEEP_OFF", "Sleep")
    a(at(TODAY, 9), at(TODAY, 10), "ACTIVE", *X, "October Payroll.xlsx")
    pr.set_app_category(d, "EXCEL.EXE", next(c["id"] for c in pr.list_categories(d) if c["kind"] == "productive"))
    yield d
    d.close()


def report(db, period="Yesterday", **kw):
    return build_report(db, period, today=TODAY, now=NOW, **kw)


def frac(v):
    """Duration cells are real Excel durations; openpyxl reads them back as timedelta."""
    return v.total_seconds() / 86400 if isinstance(v, timedelta) else v


def find_row(ws, label, col=1):
    for row in ws.iter_rows():
        if row[col - 1].value == label:
            return row
    raise AssertionError(f"{label!r} not found in {ws.title}")


# --------------------------------------------------------------- report data
def test_report_data_totals_and_reconciliation(db):
    r = report(db)
    t = r.totals
    assert (t.active, t.idle, t.locked, t.sleep_off) == (7200, 1200, 1800, 1800)
    assert t.available == 8400 and t.usage_rate == pytest.approx(7200 / 8400 * 100)
    assert r.reconciles and t.accounted == t.period_seconds == (tc.local_day_bounds(YDAY)[1] - tc.local_day_bounds(YDAY)[0]).total_seconds()
    assert t.unmonitored == t.period_seconds - 12000      # includes the 10:50-11:00 gap
    assert r.first_day == r.last_day == YDAY and len(r.daily) == 1
    assert r.applications[0] == ("Microsoft Excel", 4500)
    assert r.documents[0][:2] == ("October Payroll.xlsx", "Microsoft Excel")
    assert len(r.idle_periods) == 1 and len(r.locked_periods) == 1
    assert r.score.score == pytest.approx(100.0)          # only Excel categorized (productive)


def test_periods_cover_expected_days(db):
    assert [len(report(db, p).daily) for p in ("Today", "Yesterday", "This week", "Last week")] == [1, 1, 3, 7]
    assert len(report(db, "This month").daily) == 21
    last = report(db, "Last month")
    assert (last.first_day, last.last_day) == (date(2026, 9, 1), date(2026, 9, 30))
    c = build_report(db, "Custom", today=TODAY, custom=(date(2026, 10, 19), date(2026, 10, 21)), now=NOW)
    assert len(c.daily) == 3 and c.reconciles
    month = report(db, "This month")
    assert month.reconciles
    assert sum(t.active for _, t in month.daily) == month.totals.active == 7200 + 3600


def test_report_too_long_is_rejected(db):
    with pytest.raises(ValueError):
        build_report(db, "Custom", today=TODAY, custom=(date(2024, 1, 1), date(2026, 1, 1)), now=NOW)


# ---------------------------------------------------------------------- Excel
def test_excel_workbook_contents(db, tmp_path):
    p = export_excel(report(db), tmp_path / "r.xlsx")
    wb = load_workbook(p)
    assert wb.sheetnames == ["Summary", "Applications", "Documents", "Daily availability",
                             "Idle periods", "Locked periods", "Productivity", "Timeline"]
    s = wb["Summary"]
    assert s["A1"].value == "TimeLens Activity Report"
    assert "Yesterday" in s["A2"].value and "20 October 2026" in s["A2"].value
    active = find_row(s, "Active time")
    assert frac(active[1].value) == pytest.approx(7200 / 86400) and active[1].number_format == "[h]:mm:ss"
    assert active[2].value == 2.0
    obs = find_row(s, "Observed period")
    assert frac(obs[1].value) == pytest.approx(1.0) and obs[3].value == 1.0
    assert frac(find_row(s, "Available time")[1].value) == pytest.approx(8400 / 86400)
    assert find_row(s, "Usage rate")[1].value == "85.7%"
    notes = " ".join(str(c.value) for row in s.iter_rows() for c in row if c.value)
    assert "reconciles" in notes and "DOES NOT" not in notes


def test_excel_sheets_have_data_and_charts(db, tmp_path):
    wb = load_workbook(export_excel(report(db), tmp_path / "r.xlsx"))
    apps = wb["Applications"]
    excel = find_row(apps, "Microsoft Excel")
    assert excel[2].value == 1.25 and frac(excel[1].value) == pytest.approx(4500 / 86400) and excel[1].number_format == "[h]:mm:ss"
    assert find_row(apps, "Total")[2].value == 2.0
    assert len(apps._charts) == 1 and len(wb["Daily availability"]._charts) == 1
    assert len(wb["Productivity"]._charts) == 1
    docs = wb["Documents"]
    assert find_row(docs, "October Payroll.xlsx")[4].value == 1
    day = wb["Daily availability"]
    row = find_row(day, "Total")
    assert frac(row[1].value) == pytest.approx(7200 / 86400) and frac(row[2].value) == pytest.approx(1200 / 86400)
    assert frac(wb["Idle periods"]["C6"].value) == pytest.approx(1200 / 86400)
    tl = wb["Timeline"]
    assert tl.max_row == 5 + 9                                       # header row 5 + 9 entries
    statuses = [tl.cell(row=r, column=5).value for r in range(6, tl.max_row + 1)]
    assert statuses == ["Active", "Active", "Active", "Idle", "Unmonitored", "Active", "Locked",
                        "Sleep / Off", "Unmonitored"]
    prod = wb["Productivity"]
    assert find_row(prod, "Productivity score")[1].value == pytest.approx(1.0)


def test_excel_section_selection_and_privacy(db, tmp_path):
    wb = load_workbook(export_excel(report(db, sections=("applications",)), tmp_path / "a.xlsx"))
    assert wb.sheetnames == ["Summary", "Applications"]
    text = " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value)
    assert "\\" not in text.replace("\\n", "")                   # no file paths anywhere


def test_excel_empty_database(tmp_path):
    d = Database(tmp_path / "e.db").open()
    wb = load_workbook(export_excel(build_report(d, "Today", today=TODAY, now=NOW), tmp_path / "e.xlsx"))
    assert "Summary" in wb.sheetnames
    assert "No application activity" in str(wb["Applications"]["A5"].value)
    d.close()


def test_excel_monthly_report(db, tmp_path):
    wb = load_workbook(export_excel(report(db, "This month"), tmp_path / "m.xlsx"))
    day = wb["Daily availability"]
    dated = [r for r in range(6, day.max_row + 1) if hasattr(day.cell(row=r, column=1).value, "year")]
    assert len(dated) == 21
    assert frac(find_row(day, "Total")[1].value) == pytest.approx(10800 / 86400)


# ------------------------------------------------------------------------ PDF
def pdf_text(path):
    return path.read_bytes()


def test_pdf_daily_report(db, tmp_path):
    p = export_pdf(report(db), tmp_path / "d.pdf", unicode_font=False, compress=False)
    raw = pdf_text(p)
    assert raw.startswith(b"%PDF")
    for word in (b"TimeLens Activity Report", b"Application usage", b"Payroll.xlsx", b"Productivity",
                 b"Unmonitored", b"Available"):
        assert word in raw, word
    assert len(re.findall(rb"/Type /Page[^s]", raw)) >= 2          # summary pages + timeline page


def test_pdf_monthly_and_unicode_font(db, tmp_path):
    db.add_closed_session(at(YDAY, 14), at(YDAY, 15), "ACTIVE", "Microsoft Word", "WINWORD.EXE",
                          "Отчёт за октябрь.docx")
    p = export_pdf(report(db, "This month"), tmp_path / "m.pdf")   # default: Segoe UI + compression
    assert p.read_bytes().startswith(b"%PDF") and p.stat().st_size > 8000


def test_pdf_empty_database(tmp_path):
    d = Database(tmp_path / "e.db").open()
    p = export_pdf(build_report(d, "Today", today=TODAY, now=NOW), tmp_path / "e.pdf",
                   unicode_font=False, compress=False)
    raw = p.read_bytes()
    assert raw.startswith(b"%PDF") and b"No application activity" in raw
    d.close()


def test_pdf_section_selection(db, tmp_path):
    p = export_pdf(report(db, sections=("applications",)), tmp_path / "s.pdf", unicode_font=False, compress=False)
    raw = p.read_bytes()
    assert b"Application usage" in raw and b"Daily activity timeline" not in raw
    assert set(SECTIONS) == {"applications", "documents", "idle", "availability", "locked",
                             "productivity", "timeline"}
