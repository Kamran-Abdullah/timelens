"""PDF report export using ReportLab (charts are drawn with Matplotlib's Agg backend, which
needs no window system, then embedded as images)."""
from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from analytics.report_data import SECTIONS, ReportData
from exports.common import (NO_TIMELINE_NOTE, hms, local_clock, percent, reconciliation_text,
                            summary_rows, usage_rate_text)
from ui.theme import CATEGORY_COLORS, STATUS_COLORS, STATUS_LABELS

NAVY = colors.HexColor("#1F3A5F")
BAND = colors.HexColor("#F2F5FA")
GRID = colors.HexColor("#D0D7E2")
MAX_TIMELINE_ROWS = 400
MAX_TABLE_ROWS = 40


def _fonts(unicode_font: bool) -> tuple[str, str]:
    """Segoe UI (ships with Windows) covers non-Latin file names; Helvetica is the fallback."""
    if unicode_font:
        regular, bold = Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\segoeuib.ttf")
        if regular.exists() and bold.exists():
            try:
                pdfmetrics.registerFont(TTFont("SegoeUI", str(regular)))
                pdfmetrics.registerFont(TTFont("SegoeUI-Bold", str(bold)))
                return "SegoeUI", "SegoeUI-Bold"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold"


# --- charts -------------------------------------------------------------------------
def _png(fig: Figure) -> io.BytesIO:
    buf = io.BytesIO()
    FigureCanvasAgg(fig).print_png(buf)
    buf.seek(0)
    return buf


def _style(ax):
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=7)
    ax.yaxis.grid(True, color="#E3E7EE", linewidth=0.6)
    ax.set_axisbelow(True)


def chart_time_split(data: ReportData) -> io.BytesIO | None:
    t = data.totals
    parts = [("Active", t.active, STATUS_COLORS["ACTIVE"]), ("Idle", t.idle, STATUS_COLORS["IDLE"]),
             ("Locked", t.locked, STATUS_COLORS["LOCKED"]),
             ("Sleep / Off", t.sleep_off, STATUS_COLORS["SLEEP_OFF"]),
             ("Unmonitored", t.unmonitored, STATUS_COLORS["UNMONITORED"])]
    parts = [p for p in parts if p[1] > 0]
    if not parts:
        return None
    fig = Figure(figsize=(4.2, 2.6))
    ax = fig.add_subplot(111)
    ax.pie([p[1] for p in parts], colors=[p[2] for p in parts], startangle=90, counterclock=False,
           wedgeprops=dict(width=0.45, edgecolor="white"))
    ax.legend([f"{p[0]}  {p[1] / t.period_seconds * 100:.0f}%" for p in parts], fontsize=7,
              loc="center left", bbox_to_anchor=(0.95, 0.5), frameon=False)
    fig.subplots_adjust(left=0.02, right=0.62)
    return _png(fig)


def chart_top_apps(data: ReportData) -> io.BytesIO | None:
    top = data.applications[:8][::-1]
    if not top:
        return None
    fig = Figure(figsize=(5.2, 2.6))
    ax = fig.add_subplot(111)
    ax.barh([a for a, _ in top], [s / 3600 for _, s in top], color=STATUS_COLORS["ACTIVE"])
    ax.set_xlabel("hours", fontsize=7)
    _style(ax)
    ax.yaxis.grid(False)
    fig.tight_layout()
    return _png(fig)


def chart_daily(data: ReportData) -> io.BytesIO | None:
    if not data.daily or data.totals.period_seconds <= 0:
        return None
    fig = Figure(figsize=(7.2, 2.6))
    ax = fig.add_subplot(111)
    labels = [d.strftime("%d %b") for d, _ in data.daily]
    bottom = [0.0] * len(labels)
    for key, attr in (("ACTIVE", "active"), ("IDLE", "idle"), ("LOCKED", "locked"),
                      ("SLEEP_OFF", "sleep_off")):
        vals = [getattr(t, attr) / 3600 for _, t in data.daily]
        ax.bar(labels, vals, bottom=bottom, color=STATUS_COLORS[key], label=STATUS_LABELS[key], width=0.7)
        bottom = [b + v for b, v in zip(bottom, vals)]
    step = max(1, len(labels) // 15)
    ax.set_xticks(range(0, len(labels), step))
    ax.set_xticklabels(labels[::step], rotation=0 if len(labels) <= 10 else 45, fontsize=7)
    ax.set_ylabel("hours", fontsize=7)
    ax.legend(fontsize=7, ncol=4, frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0))
    _style(ax)
    fig.tight_layout()
    return _png(fig)


def chart_categories(data: ReportData) -> io.BytesIO | None:
    cats = [c for c in data.categories if c["seconds"] > 0]
    if not cats:
        return None
    fig = Figure(figsize=(4.2, 2.4))
    ax = fig.add_subplot(111)
    ax.barh([c["name"] for c in cats][::-1], [c["seconds"] / 3600 for c in cats][::-1],
            color=[CATEGORY_COLORS.get(c["kind"], "#999999") for c in cats][::-1])
    ax.set_xlabel("hours", fontsize=7)
    _style(ax)
    ax.yaxis.grid(False)
    fig.tight_layout()
    return _png(fig)


# --- document ------------------------------------------------------------------------
class _Styles:
    def __init__(self, regular: str, bold: str):
        base = getSampleStyleSheet()
        self.title = ParagraphStyle("t", parent=base["Title"], fontName=bold, fontSize=20,
                                    textColor=NAVY, alignment=0, spaceAfter=2)
        self.h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName=bold, fontSize=13,
                                 textColor=NAVY, spaceBefore=10, spaceAfter=4, keepWithNext=1)
        self.body = ParagraphStyle("b", parent=base["BodyText"], fontName=regular, fontSize=9,
                                   leading=12)
        self.muted = ParagraphStyle("m", parent=self.body, textColor=colors.HexColor("#5B6678"))
        self.cell = ParagraphStyle("c", parent=self.body, fontSize=8, leading=10)
        self.cell_r = ParagraphStyle("cr", parent=self.cell, alignment=2)
        self.regular, self.bold = regular, bold


def _p(text, style) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _table(st: _Styles, headers, rows, widths, right_cols=(), total=None) -> Table:
    def cell(v, c, head=False):
        s = st.cell_r if c in right_cols else st.cell
        if head:
            s = ParagraphStyle("h", parent=s, fontName=st.bold, textColor=colors.white)
        return _p(v, s)

    body = [[cell(h, c, True) for c, h in enumerate(headers)]]
    body += [[cell(v, c) for c, v in enumerate(r)] for r in rows]
    if total:
        body.append([_p(v, ParagraphStyle("tt", parent=st.cell_r if c in right_cols else st.cell,
                                          fontName=st.bold)) for c, v in enumerate(total)])
    t = Table(body, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for i in range(2, len(body), 2):
        style.append(("BACKGROUND", (0, i), (-1, i), BAND))
    if total:
        style.append(("BACKGROUND", (0, len(body) - 1), (-1, len(body) - 1), colors.HexColor("#DCE6F5")))
    t.setStyle(TableStyle(style))
    return t


def _img(buf, width_mm) -> Image | None:
    if buf is None:
        return None
    from PIL import Image as PILImage
    w, h = PILImage.open(buf).size
    buf.seek(0)
    width = width_mm * mm
    return Image(buf, width=width, height=width * h / w)


def export_pdf(data: ReportData, path: str | Path, unicode_font: bool = True,
               compress: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    regular, bold = _fonts(unicode_font)
    st = _Styles(regular, bold)
    W = A4[0] - 30 * mm
    want = set(data.sections)
    t = data.totals
    story: list = []

    # --- heading + summary
    story += [_p(data.title, st.title),
              _p(f"{data.period_label}: {data.date_text}", st.body),
              _p(f"Generated {local_clock(data.generated_at, True)} (local time)", st.muted),
              Spacer(1, 6)]
    rows = [[label, hms(sec), f"{percent(sec, t.period_seconds) * 100:.1f}%", why]
            for label, sec, why in summary_rows(data)]
    story.append(_table(st, ["Measure", "Time (h:mm:ss)", "% of period", "Meaning"], rows,
                        [38 * mm, 28 * mm, 22 * mm, W - 88 * mm], (1, 2),
                        total=["Observed period", hms(t.period_seconds), "100.0%" if t.period_seconds else "-",
                               "Sum of the five rows above"]))
    story += [Spacer(1, 4),
              _p(f"Available time = Active + Idle = {hms(t.available)}.   "
                 f"Usage rate = Active / Available = {usage_rate_text(t)}.", st.body),
              _p(reconciliation_text(data), st.muted),
              _p(f"Applications used: {len(data.applications)}.  Documents used: {len(data.documents)}.  "
                 f"Sections: " + ", ".join(SECTIONS[s] for s in data.sections) + ".", st.muted)]
    pie, apps = chart_time_split(data), chart_top_apps(data)
    imgs = [i for i in (_img(pie, 80), _img(apps, 95)) if i]
    if imgs:
        story += [Spacer(1, 6), Table([imgs], colWidths=[W / len(imgs)] * len(imgs))]

    # --- applications
    if "applications" in want:
        story.append(_p("Application usage (active time)", st.h2))
        if data.applications:
            total = sum(s for _, s in data.applications)
            shown = data.applications[:MAX_TABLE_ROWS]
            story.append(_table(st, ["Application", "Time (h:mm:ss)", "% of active"],
                                [[a, hms(s), f"{percent(s, total) * 100:.1f}%"] for a, s in shown],
                                [W - 60 * mm, 32 * mm, 28 * mm], (1, 2),
                                total=["Total", hms(total), "100.0%"]))
            if len(data.applications) > len(shown):
                story.append(_p(f"Top {len(shown)} of {len(data.applications)} applications shown "
                                "(the Excel export lists all).", st.muted))
        else:
            story.append(_p("No application activity in this period.", st.muted))

    # --- documents
    if "documents" in want:
        story.append(_p("Document usage (file names only)", st.h2))
        if data.documents:
            shown = data.documents[:MAX_TABLE_ROWS]
            story.append(_table(st, ["Document", "Application", "Time (h:mm:ss)", "Sessions"],
                                [[d, a, hms(s), n] for d, a, s, n in shown],
                                [W - 100 * mm, 40 * mm, 32 * mm, 28 * mm], (2, 3)))
            if len(data.documents) > len(shown):
                story.append(_p(f"Top {len(shown)} of {len(data.documents)} documents shown "
                                "(the Excel export lists all).", st.muted))
        else:
            story.append(_p("No document names were detected in this period.", st.muted))

    # --- availability / idle / locked (per day)
    if want & {"availability", "idle", "locked"}:
        story.append(_p("Computer availability, idle and locked time", st.h2))
        daily = chart_daily(data)
        if daily:
            story.append(_img(daily, 175))
        drows = [[d.strftime("%a %d %b %Y"), hms(x.active), hms(x.idle), hms(x.locked),
                  hms(x.sleep_off), hms(x.unmonitored), hms(x.available), usage_rate_text(x)]
                 for d, x in data.daily]
        story.append(_table(st, ["Date", "Active", "Idle", "Locked", "Sleep/Off", "Unmonitored",
                                 "Available", "Usage"], drows,
                            [28 * mm] + [(W - 28 * mm - 14 * mm) / 6] * 6 + [14 * mm],
                            tuple(range(1, 8)),
                            total=["Total", hms(t.active), hms(t.idle), hms(t.locked), hms(t.sleep_off),
                                   hms(t.unmonitored), hms(t.available), usage_rate_text(t)]))
        story.append(_p("Available = Active + Idle.  Usage rate = Active / Available.", st.muted))
        for key, label, entries in (("idle", "Idle periods", data.idle_periods),
                                    ("locked", "Locked periods", data.locked_periods)):
            if key not in want:
                continue
            story.append(_p(f"{label}: {len(entries)} totalling "
                            f"{hms(sum(e.seconds for e in entries))}"
                            + (f", longest {hms(max(e.seconds for e in entries))}" if entries else ""),
                            st.body))
            top = sorted(entries, key=lambda e: -e.seconds)[:10]
            if top:
                story.append(_table(st, ["From", "To", "Duration"],
                                    [[local_clock(e.start, True), local_clock(e.end, True), hms(e.seconds)]
                                     for e in top], [55 * mm, 55 * mm, 30 * mm], (2,)))
                if len(entries) > len(top):
                    story.append(_p(f"Longest {len(top)} of {len(entries)} shown.", st.muted))

    # --- productivity
    if "productivity" in want:
        story.append(_p("Productivity", st.h2))
        if data.categories:
            total = sum(c["seconds"] for c in data.categories)
            story.append(_table(st, ["Category", "Kind", "Time (h:mm:ss)", "% of active"],
                                [[c["name"], c["kind"].capitalize(), hms(c["seconds"]),
                                  f"{percent(c['seconds'], total) * 100:.1f}%"] for c in data.categories],
                                [W - 100 * mm, 30 * mm, 38 * mm, 32 * mm], (2, 3),
                                total=["Total", "", hms(total), "100.0%"]))
            img = _img(chart_categories(data), 100)
            if img:
                story += [Spacer(1, 4), img]
            s = data.score
            story.append(_p("Score: " + ("n/a" if s.score is None else f"{s.score:.0f}%")
                            + "   (weights are set by you; uncategorized time is not guessed)", st.body))
            for line in s.lines:
                story.append(_p(line, st.muted))
        else:
            story.append(_p("No active time in this period.", st.muted))

    # --- timeline
    if "timeline" in want:
        story += [PageBreak(), _p("Daily activity timeline", st.h2)]
        if data.timeline:
            shown = data.timeline[:MAX_TIMELINE_ROWS]
            story.append(_table(
                st, ["Date", "Time", "Duration", "Status", "Activity", "Application / detail"],
                [[e.start.astimezone().strftime("%d %b"),
                  f"{e.start.astimezone():%H:%M:%S}-{e.end.astimezone():%H:%M:%S}", hms(e.seconds),
                  STATUS_LABELS[e.status], e.label, e.detail] for e in shown],
                [16 * mm, 32 * mm, 18 * mm, 22 * mm, W - 128 * mm, 40 * mm], (2,)))
            if len(data.timeline) > len(shown) or data.timeline_truncated:
                story.append(_p(f"First {len(shown)} entries shown; the Excel export lists more.", st.muted))
        else:
            story.append(_p(NO_TIMELINE_NOTE, st.muted))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(colors.HexColor("#5B6678"))
        canvas.drawString(15 * mm, 9 * mm, f"TimeLens - {data.period_label} - {data.date_text}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm, title=data.title,
                            author="TimeLens", pageCompression=1 if compress else 0)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path
