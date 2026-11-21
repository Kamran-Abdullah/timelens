"""Websites page: which sites you visited and what you searched for in Chrome / Edge.

Everything shown comes from the optional web_visits table, which stays EMPTY until the user
opts in. Turning it on always goes through an explanation and a Yes/No choice.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional

from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTabWidget,
                               QVBoxLayout, QWidget)

from analytics import web_reports as wr
from database.db_manager import Database
from services import web_history
from ui import theme
from ui.date_filter import DateFilter
from ui.theme import fmt_duration as fd
from ui.web_consent import ask_web_consent
from ui.widgets import page_layout, Card, Panel, add_header, fill_sortable, make_table, set_headers
from ui.workers import run_async

log = logging.getLogger(__name__)


class WebsitesPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, day_provider=date.today, on_tracking_changed: Callable[[], None] = lambda: None):
        super().__init__()
        self.setObjectName("page")
        self.db, self._day, self._changed = db, day_provider, on_tracking_changed
        self._token = 0
        self.ask_consent: Callable[[], Optional[int]] = lambda: ask_web_consent(self)
        self.confirm: Callable[[str, str], bool] = self._confirm

        root = page_layout(self, 12)
        self.sub = add_header(root, "Websites", "")

        # --- state 1: tracking is off
        self.off_panel = Panel("Website tracking is off")
        msg = QLabel("Turn it on to see which sites you visit in Chrome and Edge and what you search "
                     "for. It is optional and off by default. TimeLens reads the history your browser "
                     "already keeps, stores only the site name, page title, search words and times "
                     "(never full web addresses), and keeps it on this computer.")
        msg.setWordWrap(True)
        msg.setObjectName("sub")
        self.btn_on = QPushButton("Turn on website tracking...")
        self.btn_on.setObjectName("primary")
        self.btn_on.clicked.connect(self.turn_on)
        row = QHBoxLayout()
        row.addWidget(self.btn_on)
        row.addStretch(1)
        self.off_panel.body.addWidget(msg)
        self.off_panel.body.addLayout(row)
        self.off_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        root.addWidget(self.off_panel)

        # --- state 2: tracking is on
        self.on_widget = QWidget()
        on = QVBoxLayout(self.on_widget)
        on.setContentsMargins(0, 0, 0, 0)
        on.setSpacing(12)
        bar = QHBoxLayout()
        self.filter = DateFilter("Today")
        self.filter.changed.connect(self.refresh)
        self.btn_refresh = QPushButton("Update now")
        self.btn_refresh.clicked.connect(self.update_now)
        self.btn_off = QPushButton("Turn off")
        self.btn_off.clicked.connect(self.turn_off)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_off)
        on.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(12)
        A = theme.ACCENTS
        self.cards = {"sites": Card("Sites visited", A["indigo"]), "visits": Card("Page visits", A["sky"]),
                      "searches": Card("Searches", A["pink"]), "time": Card("Time pages were open", A["orange"])}
        for i, c in enumerate(self.cards.values()):
            grid.addWidget(c, 0, i)
        on.addLayout(grid)

        self.tabs = QTabWidget()
        self.sites_table = make_table(["Site", "Visits", "Time open", "Last visit"], scroll=True)
        set_headers(self.sites_table, ["Site", "Visits", "Time open", "Last visit"], (1, 2))
        self.search_table = make_table(["Time", "What you searched for", "Search site"], scroll=True)
        self.search_table.horizontalHeader().setSectionResizeMode(0, self.search_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.search_table.horizontalHeader().setSectionResizeMode(1, self.search_table.horizontalHeader().ResizeMode.Stretch)
        self.visits_table = make_table(["Time", "Site", "Page title", "Open for"], scroll=True)
        set_headers(self.visits_table, ["Time", "Site", "Page title", "Open for"], (3,))
        h = self.visits_table.horizontalHeader()
        h.setSectionResizeMode(0, h.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, h.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, h.ResizeMode.Stretch)
        for table in (self.sites_table, self.search_table, self.visits_table):
            table.setMinimumHeight(260)
        self.tabs.addTab(self._wrap(self.sites_table), "Top sites")
        self.tabs.addTab(self._wrap(self.search_table), "Searches")
        self.tabs.addTab(self._wrap(self.visits_table), "Recent visits")
        on.addWidget(self.tabs, 1)
        self.note = QLabel("Time open is what the browser reports for each page (each visit counted up to 1 hour, "
                           "so a tab left open overnight does not look like a whole day).")
        self.note.setObjectName("sub")
        self.note.setWordWrap(True)
        on.addWidget(self.note)
        root.addWidget(self.on_widget, 1)
        root.addStretch()          # when tracking is off, keep the banner and card at the top
        self._show_state()

    @staticmethod
    def _wrap(table) -> QWidget:
        panel = Panel("")
        panel.body.itemAt(0).widget().hide()          # no heading: the tab already names it
        panel.body.addWidget(table)
        return panel

    def _confirm(self, title: str, text: str) -> bool:
        from PySide6.QtWidgets import QMessageBox
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    # --- on / off ------------------------------------------------------------------------
    def tracking_on(self) -> bool:
        return self.db.get_bool("track_websites")

    def _show_state(self) -> None:
        on = self.tracking_on()
        self.off_panel.setVisible(not on)
        self.on_widget.setVisible(on)
        if not on:
            self.sub.setText("Optional and off. Turn it on to see the sites you visit and what you search for.")

    def turn_on(self) -> bool:
        days = self.ask_consent()
        if days is None:
            return False
        web_history.enable_tracking(self.db, days)
        self._show_state()
        self._changed()
        self.update_now()
        return True

    def turn_off(self) -> None:
        web_history.disable_tracking(self.db)
        self._show_state()
        self._changed()

    # --- loading -------------------------------------------------------------------------------
    def update_now(self) -> None:
        """Read the browsers' latest history, then reload the tables (both off the GUI thread)."""
        self.sub.setText("Reading your browser history ...")
        self.btn_refresh.setEnabled(False)
        db = self.db
        run_async(lambda: web_history.import_history(db), self._on_imported, self._on_error)

    def _on_imported(self, result: web_history.ImportResult) -> None:
        self.btn_refresh.setEnabled(True)
        self._import_note = (f"{result.new_visits} new visit(s) read." if not result.errors
                             else "Could not read: " + "; ".join(result.errors))
        self.refresh()

    def refresh(self) -> None:
        self._show_state()
        if not self.tracking_on():
            return
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        lo, hi = self.filter.date_range(day)

        def load():
            return (token, wr.web_summary(db, lo, hi), wr.top_sites(db, lo, hi), wr.search_history(db, lo, hi),
                    wr.recent_visits(db, lo, hi))
        run_async(load, self._on_data, self._on_error)

    def _on_error(self, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.sub.setText(f"Could not load website data: {message}")

    def _on_data(self, payload) -> None:
        token, summary, sites, searches, visits = payload
        if token != self._token:
            return
        self.cards["sites"].set(str(summary.sites))
        self.cards["visits"].set(str(summary.visits))
        self.cards["searches"].set(str(summary.searches))
        self.cards["time"].set(fd(summary.seconds))
        clock = lambda dt: dt.astimezone().strftime("%d %b  %H:%M")
        fill_sortable(self.sites_table, [[(d, d), (str(v), v), (fd(t), t), (clock(last), last.timestamp())]
                                         for d, v, t, last in sites], right_cols=(1, 2))
        fill_sortable(self.search_table, [[(clock(t), t.timestamp()), (q, q.lower()), (d, d)]
                                          for t, q, d in searches])
        fill_sortable(self.visits_table, [[(clock(t), t.timestamp()), (d, d), (title or "-", title.lower()),
                                           (fd(s) if s > 0 else "-", s)] for t, d, title, s in visits],
                      right_cols=(3,))
        extra = getattr(self, "_import_note", "")
        self.sub.setText((f"{summary.visits} page visit(s) on {summary.sites} site(s). " + extra).strip()
                         if summary.visits else "No website visits recorded for this period. " + extra)
