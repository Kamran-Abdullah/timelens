"""Live Monitor page: what is being tracked right now.

The current session is read from the tracking engine (in memory, cheap) once a second.
Today's cumulative usage for that application comes from SQLite via a worker thread.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from analytics import time_calc as tc
from database.db_manager import Database
from tracker.engine import TrackingEngine
from ui import theme
from ui.widgets import page_layout, add_header, Card, Panel
from ui.workers import run_async


class LiveMonitorPage(QWidget):
    open_timeline_requested = Signal()

    def __init__(self, db: Database, engine: TrackingEngine):
        super().__init__()
        self.setObjectName("page")
        self.db, self.engine = db, engine
        self._today_by_app: dict[str, float] = {}
        self._loading = False

        root = page_layout(self, 14)
        self.sub = add_header(root, "Live Monitor", "")

        grid = QGridLayout()
        grid.setSpacing(12)
        self.c_status = Card("Monitoring status", theme.ACCENTS["indigo"])
        self.c_app = Card("Current application", theme.ACCENTS["sky"])
        self.c_doc = Card("Current document", theme.ACCENTS["violet"])
        self.c_start = Card("Session started", theme.ACCENTS["orange"])
        self.c_dur = Card("Session duration", theme.STATUS_COLORS["ACTIVE"])
        self.c_today = Card("Today in this application", theme.ACCENTS["pink"])
        for i, c in enumerate((self.c_status, self.c_app, self.c_doc,
                               self.c_start, self.c_dur, self.c_today)):
            grid.addWidget(c, i // 3, i % 3)
        root.addLayout(grid)

        panel = Panel("Controls")
        row = QHBoxLayout()
        self.toggle = QPushButton("Pause monitoring")
        self.toggle.setObjectName("primary")
        self.toggle.clicked.connect(self._toggle)
        timeline = QPushButton("Open activity timeline")
        timeline.clicked.connect(self.open_timeline_requested.emit)
        row.addWidget(self.toggle)
        row.addWidget(timeline)
        row.addStretch(1)
        panel.body.addLayout(row)
        self.note = QLabel("Pausing closes the current session immediately; nothing is recorded "
                           "until you resume.")
        self.note.setObjectName("sub")
        self.note.setWordWrap(True)
        panel.body.addWidget(self.note)
        root.addWidget(panel)
        root.addStretch(1)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._update)
        self.tick.start(1000)
        self._ticks = 0
        self._update()

    def refresh(self) -> None:
        """Reload today's per-application totals from the database (off the GUI thread)."""
        if self._loading:
            return
        self._loading = True
        db = self.db
        start, end = tc.local_day_bounds(date.today())
        run_async(lambda: dict(tc.application_totals(db, start, end)), self._on_totals,
                  self._on_totals_error)

    def _on_totals(self, totals: dict) -> None:
        self._loading = False
        self._today_by_app = totals

    def _on_totals_error(self, _msg: str) -> None:
        self._loading = False

    def _toggle(self) -> None:
        if self.engine.paused:
            self.engine.resume()
        else:
            self.engine.pause()
        self._update()

    def _update(self) -> None:
        self._ticks += 1
        if self._ticks % 5 == 1:
            self.refresh()
        eng = self.engine
        seg = eng.monitor.current_segment() if eng.running else None
        if not eng.running:
            status = "Stopped"
        elif eng.paused:
            status = "Paused"
        elif seg is None:
            status = "Starting..."
        else:
            status = "Monitoring - " + theme.STATUS_LABELS[seg.status.value]
        self.c_status.set(status)
        self.toggle.setText("Resume monitoring" if eng.paused else "Pause monitoring")
        self.toggle.setEnabled(eng.running)

        if seg is None:
            for c in (self.c_app, self.c_doc, self.c_start, self.c_dur, self.c_today):
                c.set("-")
            self.sub.setText("Paused - no activity is being recorded." if eng.paused
                             else "Waiting for the first sample...")
            return
        self.sub.setText("Updated every second from the tracking engine.")
        name = seg.application_name
        self.c_app.set(name, seg.executable_name)
        self.c_doc.set(seg.document_name or "-",
                       "" if seg.document_name else "No document detected")
        self.c_start.set(seg.start.astimezone().strftime("%H:%M:%S"))
        secs = (datetime.now(timezone.utc) - seg.start).total_seconds()
        self.c_dur.set(theme.fmt_duration(max(0, secs)))
        if seg.status.value == "ACTIVE":
            self.c_today.set(theme.fmt_duration(self._today_by_app.get(name, 0)))
        else:
            self.c_today.set("-", "Not an active session")
