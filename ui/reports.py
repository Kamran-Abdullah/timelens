"""Reports page: pick a period and sections, preview the totals, export to Excel or PDF."""
from __future__ import annotations

import logging
import subprocess
from datetime import date
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from analytics.report_data import REPORT_PERIODS, SECTIONS, ReportData, build_report
from database.db_manager import Database
from exports.common import reconciliation_text, usage_rate_text
from exports.excel_export import export_excel
from exports.pdf_export import export_pdf
from ui import theme
from ui.date_filter import DateFilter
from ui.widgets import page_layout, add_header, Card, Panel
from ui.workers import run_async

log = logging.getLogger(__name__)


class ReportsPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db, self._day = db, day_provider
        self._token = 0
        self._data: ReportData | None = None
        self._busy = False
        self.last_export: Path | None = None

        root = page_layout(self, 12)
        self.sub = add_header(root, "Reports", "Choose a period and what to include, then export to Excel or PDF.")

        bar = QHBoxLayout()
        self.filter = DateFilter("Today", REPORT_PERIODS)
        self.filter.changed.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        root.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(12)
        C = theme.STATUS_COLORS
        self.cards = {"active": Card("Active", C["ACTIVE"]), "idle": Card("Idle", C["IDLE"]),
                      "locked": Card("Locked", C["LOCKED"]), "sleep": Card("Sleep / off", C["SLEEP_OFF"]),
                      "available": Card("Available (Active + Idle)", theme.ACCENTS["indigo"]), "rate": Card("Usage rate", theme.ACCENTS["pink"])}
        for i, c in enumerate(self.cards.values()):
            grid.addWidget(c, 0, i)
        root.addLayout(grid)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        sections = Panel("Include in the report")
        self.checks: dict[str, QCheckBox] = {}
        for key, label in SECTIONS.items():
            box = QCheckBox(label)
            box.setChecked(True)
            self.checks[key] = box
            sections.body.addWidget(box)
        preview = Panel("Preview")
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setObjectName("sub")
        preview.body.addWidget(self.preview)
        preview.body.addStretch(1)
        mid.addWidget(sections, 2)
        mid.addWidget(preview, 3)
        root.addLayout(mid)

        actions = Panel("Export")
        row = QHBoxLayout()
        self.btn_excel = QPushButton("Export to Excel...")
        self.btn_excel.setObjectName("primary")
        self.btn_pdf = QPushButton("Export to PDF...")
        self.btn_pdf.setObjectName("primary")
        self.btn_open = QPushButton("Show file in folder")
        self.btn_open.setEnabled(False)
        self.btn_excel.clicked.connect(lambda: self._choose_and_export("xlsx"))
        self.btn_pdf.clicked.connect(lambda: self._choose_and_export("pdf"))
        self.btn_open.clicked.connect(self._show_in_folder)
        for b in (self.btn_excel, self.btn_pdf, self.btn_open):
            row.addWidget(b)
        row.addStretch(1)
        actions.body.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("sub")
        self.status.setWordWrap(True)
        actions.body.addWidget(self.status)
        root.addWidget(actions)
        root.addStretch(1)

    # --- preview ---------------------------------------------------------------
    def _custom(self):
        return (self.filter.start.date().toPython(), self.filter.end.date().toPython())

    def refresh(self) -> None:
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        period, custom = self.filter.label, self._custom()
        run_async(lambda: (token, build_report(db, period, day, custom)), self._on_data, self._on_error)

    def _on_error(self, message: str) -> None:
        self.preview.setText(f"Could not build the report: {message}")

    def _on_data(self, payload) -> None:
        token, data = payload
        if token != self._token:
            return
        self._data = data
        t, fd = data.totals, theme.fmt_duration
        self.cards["active"].set(fd(t.active))
        self.cards["idle"].set(fd(t.idle))
        self.cards["locked"].set(fd(t.locked))
        self.cards["sleep"].set(fd(t.sleep_off))
        self.cards["available"].set(fd(t.available))
        self.cards["rate"].set(usage_rate_text(t))
        lines = [f"{data.period_label}: {data.date_text}  ({len(data.daily)} day(s))",
                 f"{len(data.applications)} applications, {len(data.documents)} documents, "
                 f"{len(data.timeline)} timeline entries.",
                 f"Unmonitored (TimeLens not recording): {fd(t.unmonitored)}.",
                 reconciliation_text(data)]
        self.preview.setText("\n".join(lines))

    # --- export ----------------------------------------------------------------
    def selected_sections(self) -> tuple[str, ...]:
        return tuple(k for k, b in self.checks.items() if b.isChecked())

    def _choose_and_export(self, kind: str) -> None:
        if self._busy:
            return
        if not self.selected_sections():
            QMessageBox.information(self, "Export", "Tick at least one section to include.")
            return
        stem = self._data.file_stem if self._data else "TimeLens_Report"
        start_dir = Path.home() / "Documents"
        filt = "Excel workbook (*.xlsx)" if kind == "xlsx" else "PDF document (*.pdf)"
        path, _ = QFileDialog.getSaveFileName(self, "Save report", str(start_dir / f"{stem}.{kind}"), filt)
        if path:
            self.export_to(kind, path)

    def export_to(self, kind: str, path: str | Path) -> None:
        """Build the report with the chosen sections and write it, off the GUI thread."""
        path = Path(path)
        if path.suffix.lower() != f".{kind}":
            path = path.with_suffix(f".{kind}")
        sections = self.selected_sections()
        db, day = self.db, self._day()
        period, custom = self.filter.label, self._custom()
        self._set_busy(True, f"Creating {path.name} ...")

        def work():
            data = build_report(db, period, day, custom, sections=sections)
            return str(export_excel(data, path) if kind == "xlsx" else export_pdf(data, path)), data.reconciles

        run_async(work, self._on_exported, self._on_export_error)

    def _on_exported(self, result) -> None:
        path, reconciles = result
        self.last_export = Path(path)
        self._set_busy(False, f"Saved: {path}" + ("" if reconciles else
                                                   "  WARNING: totals did not reconcile - see the Summary."))
        self.btn_open.setEnabled(True)

    def _on_export_error(self, message: str) -> None:
        log.error("Export failed: %s", message)
        self._set_busy(False, f"Export failed: {message}")

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        for b in (self.btn_excel, self.btn_pdf):
            b.setEnabled(not busy)
        self.status.setText(message)

    def _show_in_folder(self) -> None:
        if self.last_export and self.last_export.exists():
            subprocess.Popen(["explorer", "/select,", str(self.last_export)])
