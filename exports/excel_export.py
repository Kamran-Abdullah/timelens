"""Excel (.xlsx) report export using OpenPyXL.

Durations are stored as real Excel time values ([h]:mm:ss) so they can be summed and
re-formatted in Excel; an hours column is added next to charted data. Values are written
as numbers (not formulas) so every viewer shows them, even without recalculating.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from analytics.report_data import SECTIONS, ReportData
from exports.common import (NO_TIMELINE_NOTE, hours, local_clock, percent, reconciliation_text,
                            summary_rows, usage_rate_text)
from ui.theme import STATUS_LABELS

DURATION = "[h]:mm:ss"
HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
BAND_FILL = PatternFill("solid", fgColor="F2F5FA")
TOTAL_FILL = PatternFill("solid", fgColor="DCE6F5")
THIN = Side(style="thin", color="D0D7E2")
STATUS_FILLS = {"ACTIVE": "10B981", "IDLE": "F59E0B", "LOCKED": "0EA5E9",
                "SLEEP_OFF": "8B5CF6", "UNMONITORED": "CBD5E1"}


def _days(seconds: float) -> float:
    return seconds / 86400.0          # Excel stores durations as fractions of a day


def _title(ws, data: ReportData, heading: str) -> int:
    ws["A1"] = heading
    ws["A1"].font = Font(size=16, bold=True, color="1F3A5F")
    ws["A2"] = f"{data.period_label}: {data.date_text}"
    ws["A3"] = f"Generated {local_clock(data.generated_at, True)} (local time)"
    for c in ("A2", "A3"):
        ws[c].font = Font(color="5B6678")
    return 5


def _table(ws, row: int, headers: list[str], rows: list[list], formats: dict[int, str],
           total: list | None = None) -> int:
    """Write a styled table at `row`; returns the next free row. `formats` maps a
    0-based column index to an Excel number format."""
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r, values in enumerate(rows, row + 1):
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = Border(bottom=THIN)
            if (r - row) % 2 == 0:
                cell.fill = BAND_FILL
            if (c - 1) in formats:
                cell.number_format = formats[c - 1]
    end = row + len(rows) + 1
    if total:
        for c, v in enumerate(total, 1):
            cell = ws.cell(row=end, column=c, value=v)
            cell.font = Font(bold=True)
            cell.fill = TOTAL_FILL
            if (c - 1) in formats:
                cell.number_format = formats[c - 1]
        end += 1
    return end + 1


def _widths(ws, widths: list[float]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _note(ws, row: int, text: str, bold: bool = False) -> int:
    ws.cell(row=row, column=1, value=text).font = Font(italic=not bold, bold=bold, color="5B6678")
    return row + 1


# --- sheets ------------------------------------------------------------------------
def _summary(wb: Workbook, data: ReportData) -> None:
    ws = wb.active
    ws.title = "Summary"
    r = _title(ws, data, data.title)
    t = data.totals
    rows = [[label, _days(sec), hours(sec), percent(sec, t.period_seconds), why]
            for label, sec, why in summary_rows(data)]
    r = _table(ws, r, ["Measure", "Time (h:mm:ss)", "Hours", "% of period", "Meaning"], rows,
               {1: DURATION, 2: "0.00", 3: "0.0%"},
               total=["Observed period", _days(t.period_seconds), hours(t.period_seconds), 1.0
                      if t.period_seconds else 0.0, "Sum of the five rows above"])
    rows2 = [["Available time", _days(t.available), hours(t.available), "Active + Idle"],
             ["Usage rate", usage_rate_text(t), "", "Active / Available x 100"]]
    r = _table(ws, r, ["Measure", "Value", "Hours", "Definition"], rows2, {1: DURATION, 2: "0.00"})
    r = _note(ws, r, reconciliation_text(data))
    r = _note(ws, r, f"Applications used: {len(data.applications)}   "
                     f"Documents used: {len(data.documents)}")
    r = _note(ws, r, "Included sections: " + ", ".join(SECTIONS[s] for s in data.sections))
    _widths(ws, [24, 18, 12, 14, 52])


def _applications(wb: Workbook, data: ReportData) -> None:
    ws = wb.create_sheet("Applications")
    r = _title(ws, data, "Application usage (active time)")
    if not data.applications:
        _note(ws, r, "No application activity in this period.")
        return
    total = sum(s for _, s in data.applications)
    rows = [[a, _days(s), hours(s), percent(s, total)] for a, s in data.applications]
    first = r + 1
    end = _table(ws, r, ["Application", "Time (h:mm:ss)", "Hours", "% of active"], rows,
                 {1: DURATION, 2: "0.00", 3: "0.0%"},
                 total=["Total", _days(total), hours(total), 1.0])
    top = min(10, len(rows))
    chart = BarChart()
    chart.type, chart.title, chart.style = "bar", "Top applications (hours)", 10
    chart.add_data(Reference(ws, min_col=3, min_row=r, max_row=r + top), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=first, max_row=first + top - 1))
    chart.legend = None
    chart.height, chart.width = 8, 16
    ws.add_chart(chart, f"F{r}")
    _widths(ws, [34, 16, 10, 12])


def _documents(wb: Workbook, data: ReportData) -> None:
    ws = wb.create_sheet("Documents")
    r = _title(ws, data, "Document usage (file names only)")
    if not data.documents:
        _note(ws, r, "No document names were detected in this period.")
        return
    total = sum(d[2] for d in data.documents)
    rows = [[d, a, _days(s), hours(s), n, _days(s / max(n, 1))] for d, a, s, n in data.documents]
    _table(ws, r, ["Document", "Application", "Time (h:mm:ss)", "Hours", "Sessions", "Avg session"],
           rows, {2: DURATION, 3: "0.00", 5: DURATION},
           total=["Total", "", _days(total), hours(total), sum(d[3] for d in data.documents), ""])
    _widths(ws, [40, 24, 16, 10, 10, 14])


def _availability(wb: Workbook, data: ReportData) -> None:
    ws = wb.create_sheet("Daily availability")
    r = _title(ws, data, "Computer availability, idle and locked time by day")
    rows = []
    for d, t in data.daily:
        rate = None if t.usage_rate is None else t.usage_rate / 100
        rows.append([d, _days(t.active), _days(t.idle), _days(t.locked), _days(t.sleep_off),
                     _days(t.unmonitored), _days(t.available), rate if rate is not None else "n/a",
                     hours(t.active), hours(t.idle), hours(t.locked), hours(t.sleep_off)])
    T = data.totals
    total = ["Total", _days(T.active), _days(T.idle), _days(T.locked), _days(T.sleep_off),
             _days(T.unmonitored), _days(T.available),
             None if T.usage_rate is None else T.usage_rate / 100, hours(T.active), hours(T.idle),
             hours(T.locked), hours(T.sleep_off)]
    if total[7] is None:
        total[7] = "n/a"
    head = r
    r = _table(ws, r, ["Date", "Active", "Idle", "Locked", "Sleep/Off", "Unmonitored", "Available",
                       "Usage rate", "Active (h)", "Idle (h)", "Locked (h)", "Sleep/Off (h)"], rows,
               {0: "ddd dd mmm yyyy", 1: DURATION, 2: DURATION, 3: DURATION, 4: DURATION,
                5: DURATION, 6: DURATION, 7: "0.0%", 8: "0.00", 9: "0.00", 10: "0.00", 11: "0.00"},
               total=total)
    r = _note(ws, r, "Available = Active + Idle.  Usage rate = Active / Available.  "
                     "Each day: Active+Idle+Locked+Sleep/Off+Unmonitored = length of the day.")
    if len(rows) >= 1:
        chart = BarChart()
        chart.type, chart.grouping, chart.overlap = "col", "stacked", 100
        chart.title, chart.height, chart.width = "Hours per day", 8, 20
        chart.add_data(Reference(ws, min_col=9, max_col=12, min_row=head, max_row=head + len(rows)),
                       titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=head + 1, max_row=head + len(rows)))
        for series, color in zip(chart.series, ("10B981", "F59E0B", "0EA5E9", "8B5CF6")):
            series.graphicalProperties.solidFill = color
        ws.add_chart(chart, f"A{r + 1}")
    _widths(ws, [18, 12, 12, 12, 12, 13, 12, 11, 11, 10, 11, 13])


def _periods(wb: Workbook, data: ReportData, sheet: str, heading: str, entries, empty: str) -> None:
    ws = wb.create_sheet(sheet)
    r = _title(ws, data, heading)
    if not entries:
        _note(ws, r, empty)
        return
    rows = [[local_clock(e.start, True), local_clock(e.end, True), _days(e.seconds),
             hours(e.seconds)] for e in entries]
    longest = max(e.seconds for e in entries)
    tot = sum(e.seconds for e in entries)
    r = _table(ws, r, ["From", "To", "Duration", "Hours"], rows, {2: DURATION, 3: "0.00"},
               total=[f"{len(entries)} period(s)", f"longest {longest / 60:.0f} min",
                      _days(tot), hours(tot)])
    _widths(ws, [24, 24, 14, 10])


def _productivity(wb: Workbook, data: ReportData) -> None:
    ws = wb.create_sheet("Productivity")
    r = _title(ws, data, "Productivity (active time by category)")
    if not data.categories:
        _note(ws, r, "No active time in this period.")
        return
    total = sum(c["seconds"] for c in data.categories)
    rows = [[c["name"], c["kind"].capitalize(), _days(c["seconds"]), hours(c["seconds"]),
             percent(c["seconds"], total)] for c in data.categories]
    first = r + 1
    r = _table(ws, r, ["Category", "Kind", "Time (h:mm:ss)", "Hours", "Usage % of active"], rows,
               {2: DURATION, 3: "0.00", 4: "0.0%"}, total=["Total", "", _days(total), hours(total), 1.0])
    s = data.score
    ws.cell(row=r, column=1, value="Productivity score").font = Font(bold=True)
    ws.cell(row=r, column=2, value="n/a" if s.score is None else s.score / 100).number_format = "0.0%"
    r += 1
    ws.cell(row=r, column=1, value="How it is calculated (weights are set by you):").font = Font(bold=True)
    r += 1
    for line in s.lines:
        r = _note(ws, r, line)
    pie = PieChart()
    pie.title, pie.height, pie.width = "Active time by category", 8, 11
    pie.add_data(Reference(ws, min_col=4, min_row=first - 1, max_row=first - 1 + len(rows)), titles_from_data=True)
    pie.set_categories(Reference(ws, min_col=1, min_row=first, max_row=first - 1 + len(rows)))
    ws.add_chart(pie, "G5")
    _widths(ws, [30, 14, 16, 10, 18])


def _timeline(wb: Workbook, data: ReportData) -> None:
    ws = wb.create_sheet("Timeline")
    r = _title(ws, data, "Daily activity timeline")
    if not data.timeline:
        _note(ws, r, NO_TIMELINE_NOTE)
        return
    rows = [[e.start.astimezone().date(), e.start.astimezone().strftime("%H:%M:%S"),
             e.end.astimezone().strftime("%H:%M:%S"), _days(e.seconds), STATUS_LABELS[e.status],
             e.label, e.detail] for e in data.timeline]
    head = r
    _table(ws, r, ["Date", "From", "To", "Duration", "Status", "Activity", "Application / detail"],
           rows, {0: "dd mmm yyyy", 3: DURATION})
    for i, e in enumerate(data.timeline, head + 1):          # colour-code the Status cell
        ws.cell(row=i, column=5).font = Font(bold=True, color=STATUS_FILLS[e.status])
    if data.timeline_truncated:
        ws.cell(row=head + len(rows) + 2, column=1,
                value="Only the most recent entries are listed; choose a shorter period for all.")
    ws.freeze_panes = ws.cell(row=head + 1, column=1)
    _widths(ws, [14, 11, 11, 12, 14, 38, 28])


def export_excel(data: ReportData, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    _summary(wb, data)
    want = set(data.sections)
    if "applications" in want:
        _applications(wb, data)
    if "documents" in want:
        _documents(wb, data)
    if want & {"availability", "idle", "locked"}:
        _availability(wb, data)
    if "idle" in want:
        _periods(wb, data, "Idle periods", "Idle periods (no input beyond the threshold)",
                 data.idle_periods, "No idle periods in this period.")
    if "locked" in want:
        _periods(wb, data, "Locked periods", "Locked periods (Windows session locked)",
                 data.locked_periods, "No locked periods in this period.")
    if "productivity" in want:
        _productivity(wb, data)
    if "timeline" in want:
        _timeline(wb, data)
    wb.save(path)
    return path
