"""Activity Timeline page: a colour strip of the period plus the chronological list."""
from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from analytics.timeline import TimelineEntry, application_names, build_timeline
from database.db_manager import Database
from ui import theme
from ui.date_filter import DateFilter
from ui.widgets import page_layout, add_header, Panel, make_table, set_headers
from ui.workers import run_async

ALL_APPS = "All applications"


def _clock(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


def _legend_text() -> str:
    return "   ".join(f"<span style='color:{theme.STATUS_COLORS[k]}'>&#9679;</span> {v}"
                     for k, v in theme.STATUS_LABELS.items())


class TimelineStrip(QWidget):
    """Horizontal bar: one coloured block per entry, width proportional to its duration."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(46)
        self.setMouseTracking(True)
        self.entries: list[TimelineEntry] = []

    def set_entries(self, entries: list[TimelineEntry]) -> None:
        self.entries = entries
        self.update()

    def _span(self):
        return self.entries[0].start, self.entries[-1].end

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.entries:
            return
        start, end = self._span()
        total = max(1.0, (end - start).total_seconds())
        w, bar_h = self.width(), 22
        for e in self.entries:
            x = (e.start - start).total_seconds() / total * w
            width = max(1.0, e.seconds / total * w)
            p.fillRect(QRectF(x, 0, width, bar_h), QBrush(QColor(theme.STATUS_COLORS[e.status])))
        p.setPen(QColor("#8a94a6"))
        p.drawText(0, bar_h + 4, 120, 16, Qt.AlignLeft, start.astimezone().strftime("%d %b %H:%M"))
        p.drawText(w - 120, bar_h + 4, 120, 16, Qt.AlignRight, end.astimezone().strftime("%d %b %H:%M"))

    def mouseMoveEvent(self, ev):
        if not self.entries:
            return
        start, end = self._span()
        total = max(1.0, (end - start).total_seconds())
        t = ev.position().x() / max(1, self.width()) * total
        for e in self.entries:
            if (e.start - start).total_seconds() <= t <= (e.end - start).total_seconds():
                self.setToolTip(f"{_clock(e.start)}-{_clock(e.end)}  {e.label}"
                                f"{'  (' + e.detail + ')' if e.detail else ''}  "
                                f"{theme.fmt_duration(e.seconds)}")
                return


class TimelinePage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db, self._day = db, day_provider
        self._token = 0
        self._apps_loaded = False

        root = page_layout(self, 12)
        self.sub = add_header(root, "Activity Timeline", "")

        bar = QHBoxLayout()
        self.filter = DateFilter("Today")
        self.filter.changed.connect(self.refresh)
        self.app_filter = QComboBox()
        self.app_filter.addItem(ALL_APPS)
        self.app_filter.setMinimumWidth(180)
        self.app_filter.currentTextChanged.connect(self._app_changed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addWidget(QLabel("Application:"))
        bar.addWidget(self.app_filter)
        bar.addStretch(1)
        bar.addWidget(refresh)
        root.addLayout(bar)

        strip_panel = Panel("Overview")
        self.strip = TimelineStrip()
        legend = QLabel(_legend_text())
        legend.setTextFormat(Qt.RichText)
        legend.setObjectName("sub")
        strip_panel.body.addWidget(self.strip)
        strip_panel.body.addSpacing(6)
        strip_panel.body.addWidget(legend)
        root.addWidget(strip_panel)

        self.headers = ["", "Time", "Duration", "Activity", "Application / detail", "Status"]
        self.table = make_table(self.headers, scroll=True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setMinimumHeight(260)
        set_headers(self.table, self.headers, (2,))
        panel = Panel("Activity")
        panel.body.addWidget(self.table)
        root.addWidget(panel, 1)

    # --- loading ---------------------------------------------------------------
    def refresh(self) -> None:
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        lo, hi = self.filter.date_range(day)
        app = None if self.app_filter.currentText() == ALL_APPS else self.app_filter.currentText()
        run_async(lambda: (token, build_timeline(db, lo, hi, app), application_names(db)),
                  self._on_data, self._on_error)

    def _app_changed(self, *_):
        if self._apps_loaded:      # ignore changes made while we repopulate the list
            self.refresh()

    def _on_error(self, message: str) -> None:
        self.sub.setText(f"Could not load data: {message}")

    def _on_data(self, payload) -> None:
        token, (entries, truncated), names = payload
        if token != self._token:
            return
        self._apps_loaded = False
        current = self.app_filter.currentText()
        self.app_filter.blockSignals(True)
        self.app_filter.clear()
        self.app_filter.addItems([ALL_APPS] + names)
        self.app_filter.setCurrentText(current if current in names else ALL_APPS)
        self.app_filter.blockSignals(False)
        self._apps_loaded = True
        self.strip.set_entries(entries)
        self._fill(entries)
        self.sub.setText(
            "No activity recorded for this period." if not entries else
            f"{len(entries)} entries" + (" (showing the most recent; narrow the period to see more)"
                                         if truncated else "") + ". Times are local.")

    def _fill(self, entries: list[TimelineEntry]) -> None:
        t = self.table
        hdr = t.horizontalHeader()
        # A column left on ResizeToContents re-measures every row on each setItem(), which makes a
        # second refill quadratic and freezes the window. Size columns by hand while filling.
        for c in range(len(self.headers)):
            hdr.setSectionResizeMode(c, hdr.ResizeMode.Interactive)
        t.setUpdatesEnabled(False)
        t.setRowCount(len(entries))
        for r, e in enumerate(entries):
            day_prefix = e.start.astimezone().strftime("%d %b  ")
            cells = ["●", day_prefix + f"{_clock(e.start)} - {_clock(e.end)}",
                     theme.fmt_duration(e.seconds), e.label, e.detail,
                     theme.STATUS_LABELS[e.status]]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:   # coloured dot = the status indicator
                    item.setForeground(QBrush(QColor(theme.STATUS_COLORS[e.status])))
                    item.setToolTip(theme.STATUS_LABELS[e.status])
                    item.setTextAlignment(Qt.AlignCenter)
                t.setItem(r, c, item)
        t.setUpdatesEnabled(True)
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Fixed)
        t.setColumnWidth(0, 28)
        hdr.setSectionResizeMode(1, hdr.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, hdr.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, hdr.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, hdr.ResizeMode.ResizeToContents)
