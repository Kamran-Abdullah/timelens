"""Dashboard page: today's numbers, top apps/documents, distribution and hourly chart.

All values come from SQLite via analytics.dashboard_data, computed in a worker thread.
"""
from __future__ import annotations

import logging
from datetime import date

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from analytics.dashboard_data import DashboardData, build_dashboard_data
from database.db_manager import Database
from ui import theme
from ui.widgets import add_header, Card, Panel, fill_table, make_table
from ui.workers import run_async

log = logging.getLogger(__name__)


class DashboardPage(QWidget):
    def __init__(self, db: Database, theme_name=lambda: "light", day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db = db
        self._theme = theme_name
        self._day = day_provider   # injectable so tests can show a fixed day
        self._token = 0
        self._last: DashboardData | None = None

        # Scrollable so the page still works in a small window.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)

        root = QVBoxLayout(content)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        self.sub = add_header(root, "Dashboard", "Loading...")

        grid = QGridLayout()
        grid.setSpacing(12)
        C = theme.STATUS_COLORS
        self.cards = {
            "active": Card("Active time", C["ACTIVE"]),
            "idle": Card("Idle time", C["IDLE"]),
            "locked": Card("Locked time", C["LOCKED"]),
            "sleep": Card("Sleep / off time", C["SLEEP_OFF"]),
            "available": Card("Available time", theme.ACCENTS["indigo"]),
            "rate": Card("Usage rate", theme.ACCENTS["pink"]),
            "apps": Card("Applications used", theme.ACCENTS["sky"]),
            "docs": Card("Documents used", theme.ACCENTS["orange"]),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 4, i % 4)
        root.addLayout(grid)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        self.apps_table = make_table(["Top applications", "Time"])
        self.docs_table = make_table(["Top documents", "Application", "Time"])
        self.docs_table.horizontalHeader().setSectionResizeMode(
            1, self.docs_table.horizontalHeader().ResizeMode.ResizeToContents)
        p1, p2 = Panel("Top 5 applications"), Panel("Top 5 documents")
        p1.body.addWidget(self.apps_table)
        p2.body.addWidget(self.docs_table)
        mid.addWidget(p1, 2)
        mid.addWidget(p2, 3)
        root.addLayout(mid, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.dist_fig = Figure(figsize=(4, 2.4))
        self.dist_canvas = FigureCanvasQTAgg(self.dist_fig)
        self.hour_fig = Figure(figsize=(6, 2.4))
        self.hour_canvas = FigureCanvasQTAgg(self.hour_fig)
        p3, p4 = Panel("Productivity distribution (active time)"), Panel("Activity by hour (minutes)")
        p3.body.addWidget(self.dist_canvas)
        p4.body.addWidget(self.hour_canvas)
        bottom.addWidget(p3, 2)
        bottom.addWidget(p4, 3)
        self.dist_canvas.setMinimumHeight(220)
        self.hour_canvas.setMinimumHeight(220)
        root.addLayout(bottom, 2)

    # --- data loading ---------------------------------------------------------
    def refresh(self) -> None:
        self._token += 1
        token = self._token
        db, day = self.db, self._day()
        run_async(lambda: (token, build_dashboard_data(db, day)), self._on_data, self._on_error)

    def _on_error(self, message: str) -> None:
        self.sub.setText(f"Could not load data: {message}")

    def _on_data(self, payload) -> None:
        token, data = payload
        if token != self._token:   # a newer refresh is already running
            return
        self._last = data
        self.render(data)

    def restyle(self) -> None:
        if self._last:
            self.render(self._last)

    # --- drawing -------------------------------------------------------------
    def render(self, d: DashboardData) -> None:
        t = d.totals
        self.sub.setText(f"{d.day:%A, %d %B %Y}  -  unmonitored today: "
                         f"{theme.fmt_duration(t.unmonitored)}")
        fd = theme.fmt_duration
        self.cards["active"].set(fd(t.active))
        self.cards["idle"].set(fd(t.idle))
        self.cards["locked"].set(fd(t.locked))
        self.cards["sleep"].set(fd(t.sleep_off), "Windows power events: Phase 8"
                                if t.sleep_off == 0 else "")
        self.cards["available"].set(fd(t.available), "Active + Idle")
        self.cards["rate"].set("n/a" if t.usage_rate is None else f"{t.usage_rate:.0f}%",
                               "Active / Available")
        self.cards["apps"].set(str(d.application_count))
        self.cards["docs"].set(str(d.document_count))

        fill_table(self.apps_table, [[a, fd(s)] for a, s in d.top_apps], right_cols=(1,))
        fill_table(self.docs_table, [[doc, app, fd(s)] for app, doc, s in d.top_documents],
                   right_cols=(2,))
        self._draw_distribution(d)
        self._draw_hourly(d)

    def _style_axes(self, fig, ax, colors):
        fig.set_facecolor(colors["bg"])
        ax.set_facecolor(colors["bg"])
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(colors["grid"])
        ax.tick_params(colors=colors["muted"], labelsize=8)
        ax.yaxis.grid(True, color=colors["grid"], linewidth=0.6)
        ax.set_axisbelow(True)

    def _draw_distribution(self, d: DashboardData) -> None:
        colors = theme.chart_colors(self._theme())
        fig = self.dist_fig
        fig.clear()
        ax = fig.add_subplot(111)
        self._style_axes(fig, ax, colors)
        ax.yaxis.grid(False)
        total = sum(d.distribution.values())
        if total <= 0:
            ax.text(0.5, 0.5, "No active time recorded yet", ha="center", va="center",
                    color=colors["muted"], transform=ax.transAxes)
            ax.set_axis_off()
        else:
            items = sorted(d.distribution.items(), key=lambda kv: kv[1])
            labels = [k.capitalize() for k, _ in items]
            vals = [v / 60 for _, v in items]
            ax.barh(labels, vals, color=[theme.CATEGORY_COLORS.get(k, "#999") for k, _ in items])
            for i, (k, v) in enumerate(items):
                ax.text(vals[i], i, f" {v / total * 100:.0f}%", va="center", fontsize=8,
                        color=colors["text"])
            ax.set_xlabel("minutes", color=colors["muted"], fontsize=8)
            ax.xaxis.grid(True, color=colors["grid"], linewidth=0.6)
            if "uncategorized" in d.distribution:
                ax.set_title("Uncategorized until you classify apps",
                             color=colors["muted"], fontsize=8, loc="left")
        fig.tight_layout()
        self.dist_canvas.draw_idle()

    def _draw_hourly(self, d: DashboardData) -> None:
        colors = theme.chart_colors(self._theme())
        fig = self.hour_fig
        fig.clear()
        ax = fig.add_subplot(111)
        self._style_axes(fig, ax, colors)
        labels = [b.astimezone().strftime("%H") for b, _ in d.hourly]
        x = range(len(labels))
        bottom = [0.0] * len(labels)
        for key, attr in (("ACTIVE", "active"), ("IDLE", "idle"), ("LOCKED", "locked"),
                          ("SLEEP_OFF", "sleep_off")):
            vals = [getattr(tot, attr) / 60 for _, tot in d.hourly]
            ax.bar(x, vals, bottom=bottom, color=theme.STATUS_COLORS[key],
                   label=theme.STATUS_LABELS[key], width=0.75)
            bottom = [b + v for b, v in zip(bottom, vals)]
        ax.set_xticks(list(x)[::2])
        ax.set_xticklabels(labels[::2])
        ax.set_ylim(0, 60)
        leg = ax.legend(fontsize=7, ncol=4, frameon=False, loc="lower left",
                        bbox_to_anchor=(0.0, 1.0))
        for text in leg.get_texts():
            text.set_color(colors["muted"])
        fig.tight_layout()
        self.hour_canvas.draw_idle()
