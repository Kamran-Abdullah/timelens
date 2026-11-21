"""Application Analytics page: usage per application, with a document drill-down."""
from __future__ import annotations

from datetime import date

import pandas as pd
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from analytics.app_reports import application_usage
from analytics.document_reports import document_usage, sort_documents
from database.db_manager import Database
from ui.date_filter import DateFilter
from ui.theme import fmt_duration as fd
from ui.widgets import page_layout, add_header, Panel, fill_sortable, make_table, set_headers
from ui.workers import run_async


def duration_cell(seconds: float) -> tuple:
    return (fd(seconds) if seconds > 0 else "-", float(seconds))


class ApplicationAnalyticsPage(QWidget):
    auto_refresh = False   # don't reshuffle the table while the user is reading it

    def __init__(self, db: Database, day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db, self._day = db, day_provider
        self._token = self._doc_token = 0
        self._selected: str | None = None

        root = page_layout(self, 12)
        self.sub = add_header(root, "Application Analytics", "Active time per application. Click an application to see its documents.")

        bar = QHBoxLayout()
        self.filter = DateFilter("All time")
        self.filter.changed.connect(self.refresh)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        bar.addWidget(refresh)
        root.addLayout(bar)

        self.headers = ["Application", "Today", "Yesterday", "This week", "This month", "Total"]
        self.table = make_table(self.headers, scroll=True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self._on_select)
        p1 = Panel("Applications")
        p1.body.addWidget(self.table)
        root.addWidget(p1, 3)

        self.doc_panel = Panel("Documents")
        self.doc_title = self.doc_panel.body.itemAt(0).widget()
        self.doc_table = make_table(["Document", "Time in period", "Sessions", "Avg session"],
                                    scroll=True)
        set_headers(self.doc_table, ["Document", "Time in period", "Sessions", "Avg session"], (1, 2, 3))
        self.doc_table.setMinimumHeight(140)
        self.doc_hint = QLabel("")
        self.doc_hint.setObjectName("sub")
        self.doc_panel.body.addWidget(self.doc_table)
        self.doc_panel.body.addWidget(self.doc_hint)
        root.addWidget(self.doc_panel, 2)
        self._show_doc_hint()

    # --- loading ---------------------------------------------------------------
    def refresh(self) -> None:
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        lo, hi = self.filter.date_range(day)
        run_async(lambda: (token, application_usage(db, lo, hi, day)), self._on_data,
                  self._on_error)

    def _on_error(self, message: str) -> None:
        self.sub.setText(f"Could not load data: {message}")

    def _on_data(self, payload) -> None:
        token, df = payload
        if token != self._token:
            return
        keep = self._selected
        self.headers[5] = f"Total ({self.filter.label})"
        set_headers(self.table, self.headers, (1, 2, 3, 4, 5))
        self.table.blockSignals(True)
        fill_sortable(self.table, [[(r.application, r.application.lower()),
                                    duration_cell(r.today), duration_cell(r.yesterday),
                                    duration_cell(r.week), duration_cell(r.month),
                                    duration_cell(r.total)] for r in df.itertuples()],
                      right_cols=(1, 2, 3, 4, 5))
        self.table.blockSignals(False)
        if df.empty:
            self.sub.setText("No application activity recorded yet.")
        else:
            self.sub.setText("Active time per application. Click an application to see its documents.")
        # keep the user's selection across refreshes
        self._selected = None
        if keep:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).text() == keep:
                    self.table.selectRow(r)
                    return
        self._show_doc_hint()

    # --- drill-down --------------------------------------------------------------
    def _on_select(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        app = self.table.item(rows[0].row(), 0).text()
        self._selected = app
        self._doc_token += 1
        token, db, day = self._doc_token, self.db, self._day()
        lo, hi = self.filter.date_range(day)
        run_async(lambda: (token, app, document_usage(db, lo, hi, application=app, today=day)),
                  self._on_docs, self._on_error)

    def _show_doc_hint(self) -> None:
        self.doc_title.setText("Documents")
        fill_sortable(self.doc_table, [])
        self.doc_hint.setText("Select an application above to see which documents it was used on.")

    def _on_docs(self, payload) -> None:
        token, app, df = payload
        if token != self._doc_token:
            return
        df = sort_documents(df, "Most used")
        self.doc_title.setText(f"Documents in {app} ({self.filter.label})")
        fill_sortable(self.doc_table, [[(r.document, r.document.lower()),
                                        duration_cell(r.range_seconds),
                                        (str(int(r.sessions)), int(r.sessions)),
                                        duration_cell(r.avg_session)]
                                       for r in df.itertuples()], right_cols=(1, 2, 3))
        self.doc_hint.setText("" if len(df) else
                              "No document names were detected for this application in this period.")
