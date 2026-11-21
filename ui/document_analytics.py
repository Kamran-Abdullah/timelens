"""Document Analytics page: usage per document (file name only, never a path)."""
from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from analytics.document_reports import SORT_MODES, document_usage, sort_documents
from database.db_manager import Database
from ui.app_analytics import duration_cell
from ui.date_filter import DateFilter
from ui.widgets import page_layout, add_header, Panel, fill_sortable, make_table, set_headers
from ui.workers import run_async

SORT_HELP = ("Most used / Least used: total time in the period.  Application: A to Z.  "
             "Duration: longest average session first.  You can also click any column header.")


class DocumentAnalyticsPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db, self._day = db, day_provider
        self._token = 0
        self._df = None

        root = page_layout(self, 12)
        self.sub = add_header(root, "Document Analytics", "")

        bar = QHBoxLayout()
        self.filter = DateFilter("All time")
        self.filter.changed.connect(self.refresh)
        self.sort = QComboBox()
        self.sort.addItems(SORT_MODES)
        self.sort.setToolTip(SORT_HELP)
        self.sort.currentTextChanged.connect(self._render)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search document or application")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addWidget(QLabel("Sort:"))
        bar.addWidget(self.sort)
        bar.addWidget(self.search, 1)
        bar.addWidget(refresh)
        root.addLayout(bar)

        self.headers = ["Document", "Application", "Today", "This week", "This month",
                        "Period", "Sessions", "Avg session"]
        self.table = make_table(self.headers, scroll=True)
        self.table.horizontalHeader().setStretchLastSection(False)
        panel = Panel("Documents")
        panel.body.addWidget(self.table)
        self.note = QLabel(SORT_HELP)
        self.note.setObjectName("sub")
        self.note.setWordWrap(True)
        panel.body.addWidget(self.note)
        root.addWidget(panel, 1)

    def refresh(self) -> None:
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        lo, hi = self.filter.date_range(day)
        run_async(lambda: (token, document_usage(db, lo, hi, today=day)), self._on_data,
                  self._on_error)

    def _on_error(self, message: str) -> None:
        self.sub.setText(f"Could not load data: {message}")

    def _on_data(self, payload) -> None:
        token, df = payload
        if token != self._token:
            return
        self._df = df
        self._render()

    def _render(self, *_) -> None:
        df = self._df
        if df is None:
            return
        text = self.search.text().strip().lower()
        if text and not df.empty:
            df = df[df["document"].str.lower().str.contains(text, regex=False)
                    | df["application"].str.lower().str.contains(text, regex=False)]
        df = sort_documents(df, self.sort.currentText())
        self.headers[5] = f"{self.filter.label}"
        set_headers(self.table, self.headers, (2, 3, 4, 5, 6, 7))
        fill_sortable(self.table, [[(r.document, r.document.lower()),
                                    (r.application, r.application.lower()),
                                    duration_cell(r.today), duration_cell(r.week),
                                    duration_cell(r.month), duration_cell(r.range_seconds),
                                    (str(int(r.sessions)), int(r.sessions)),
                                    duration_cell(r.avg_session)]
                                   for r in df.itertuples()], right_cols=(2, 3, 4, 5, 6, 7))
        if self._df.empty:
            self.sub.setText("No document names recorded in this period. Documents are detected "
                             "for Office, VS Code, Notepad and PDF readers (not browsers).")
        else:
            self.sub.setText(f"{len(df)} of {len(self._df)} documents shown - file names only.")
