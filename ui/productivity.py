"""Productivity Analytics page: overview (time per category, score, comparisons) and the
rules tab (which application belongs to which category, custom categories, score weights).

The user decides what is productive: nothing is categorized until they assign it.
"""
from __future__ import annotations

import logging
from datetime import date

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from analytics import productivity as pr
from database.db_manager import Database
from ui import theme
from ui.date_filter import DateFilter
from ui.theme import fmt_duration as fd
from ui.widgets import page_layout, add_header, Card, Panel, fill_table, make_table, set_headers
from ui.workers import run_async

log = logging.getLogger(__name__)
UNCAT = "Uncategorized"


def pct(part: float, whole: float) -> str:
    return "-" if whole <= 0 else f"{part / whole * 100:.0f}%"


def _score_text(s: pr.ScoreResult) -> str:
    return "n/a" if s.score is None else f"{s.score:.0f}%"


class ProductivityPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, theme_name=lambda: "light", day_provider=date.today):
        super().__init__()
        self.setObjectName("page")
        self.db, self._theme, self._day = db, theme_name, day_provider
        self._token = 0
        self._last = None

        root = page_layout(self, 12)
        self.sub = add_header(root, "Productivity Analytics", "You decide what counts as productive: assign categories on the Rules tab.")
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_overview(), "Overview")
        self.tabs.addTab(self._build_rules(), "Rules and scoring")
        self.tabs.currentChanged.connect(self._tab_changed)

    # =========================================================================
    # Overview tab
    # =========================================================================
    def _build_overview(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)
        bar = QHBoxLayout()
        self.filter = DateFilter("Today")
        self.filter.changed.connect(self.refresh)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        bar.addWidget(refresh)
        lay.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(12)
        C = theme.CATEGORY_COLORS
        self.cards = {
            "productive": Card("Productive time", C["productive"]),
            "neutral": Card("Neutral time", C["neutral"]),
            "distraction": Card("Distraction time", C["distraction"]),
            "personal": Card("Personal time", C["personal"]),
            "score": Card("Productivity score"),
        }
        for i, c in enumerate(self.cards.values()):
            grid.addWidget(c, 0, i)
        lay.addLayout(grid)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        self.cat_table = make_table(["Category", "Kind", "Time", "Usage"], scroll=True)
        set_headers(self.cat_table, ["Category", "Kind", "Time", "Usage"], (2, 3))
        self.cat_table.setMinimumHeight(120)
        p1 = Panel("Time by category (usage = share of active time)")
        p1.body.addWidget(self.cat_table)
        self.explain = QLabel("")
        self.explain.setWordWrap(True)
        self.explain.setObjectName("sub")
        p2 = Panel("How the score is calculated")
        p2.body.addWidget(self.explain)
        p2.body.addStretch(1)
        mid.addWidget(p1, 3)
        mid.addWidget(p2, 2)
        lay.addLayout(mid)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.cmp_table = make_table(["Period", "Active", "Productive", "Neutral", "Distraction",
                                     "Personal", "Score"], scroll=True)
        set_headers(self.cmp_table, ["Period", "Active", "Productive", "Neutral", "Distraction",
                                     "Personal", "Score"], (1, 2, 3, 4, 5, 6))
        self.cmp_table.setMinimumHeight(170)
        self.cmp_table.horizontalHeader().setSectionResizeMode(
            0, self.cmp_table.horizontalHeader().ResizeMode.ResizeToContents)
        for c in range(1, 7):
            self.cmp_table.horizontalHeader().setSectionResizeMode(
                c, self.cmp_table.horizontalHeader().ResizeMode.Stretch)
        self.cmp_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cmp_note = QLabel("")
        self.cmp_note.setObjectName("sub")
        self.cmp_note.setWordWrap(True)
        p3 = Panel("Daily and weekly comparison")
        p3.body.addWidget(self.cmp_table)
        p3.body.addWidget(self.cmp_note)
        self.fig = Figure(figsize=(4, 2.4))
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setMinimumHeight(200)
        p4 = Panel("Last 7 days (hours per category)")
        p4.body.addWidget(self.canvas)
        bottom.addWidget(p3, 3)
        bottom.addWidget(p4, 2)
        lay.addLayout(bottom, 1)
        return w

    def refresh(self) -> None:
        self._token += 1
        token, db, day = self._token, self.db, self._day()
        lo, hi = self.filter.date_range(day)

        def load():
            weights = pr.get_weights(db)
            breakdown = pr.category_breakdown(db, lo, hi)
            by_kind = pr.seconds_by_kind(breakdown)
            return dict(token=token, breakdown=breakdown, by_kind=by_kind,
                        score=pr.compute_score(by_kind, weights),
                        comparisons=pr.comparisons(db, day), series=pr.daily_series(db, day, 7))

        run_async(load, self._on_data, self._on_error)

    def _on_error(self, message: str) -> None:
        self.sub.setText(f"Could not load data: {message}")

    def _on_data(self, data: dict) -> None:
        if data["token"] != self._token:
            return
        self._last = data
        self.render(data)

    def restyle(self) -> None:
        if self._last:
            self._draw_chart(self._last["series"])

    def render(self, d: dict) -> None:
        by_kind, score = d["by_kind"], d["score"]
        active = sum(by_kind.values())
        for kind in pr.BASE_KINDS:
            self.cards[kind].set(fd(by_kind.get(kind, 0)), pct(by_kind.get(kind, 0), active) + " of active time"
                                 if active > 0 else "")
        self.cards["score"].set(_score_text(score),
                                "" if score.coverage is None else f"{score.coverage:.0f}% of active time categorized")
        fill_table(self.cat_table, [[r["name"], r["kind"].capitalize(), fd(r["seconds"]),
                                     pct(r["seconds"], active)] for r in d["breakdown"]],
                   right_cols=(2, 3))
        self.explain.setText("\n".join(score.lines))
        self._fill_comparisons(d["comparisons"])
        self._draw_chart(d["series"])

    def _fill_comparisons(self, pairs) -> None:
        rows = []
        notes = []
        for cur, prev in pairs:
            for s in (cur, prev):
                bk = s.by_kind
                rows.append([s.label, fd(s.active_seconds), fd(bk.get("productive", 0)),
                             fd(bk.get("neutral", 0)), fd(bk.get("distraction", 0)),
                             fd(bk.get("personal", 0)), _score_text(s.score)])
            if cur.score.score is not None and prev.score.score is not None:
                diff = cur.score.score - prev.score.score
                notes.append(f"{cur.label} vs {prev.label}: score {diff:+.0f} points.")
            else:
                notes.append(f"{cur.label} vs {prev.label}: no score to compare "
                             f"(needs categorized time in both).")
        fill_table(self.cmp_table, rows, right_cols=(1, 2, 3, 4, 5, 6))
        self.cmp_note.setText("  ".join(notes))

    def _draw_chart(self, series) -> None:
        colors = theme.chart_colors(self._theme())
        fig = self.fig
        fig.clear()
        ax = fig.add_subplot(111)
        fig.set_facecolor(colors["bg"])
        ax.set_facecolor(colors["bg"])
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(colors["grid"])
        ax.tick_params(colors=colors["muted"], labelsize=8)
        ax.yaxis.grid(True, color=colors["grid"], linewidth=0.6)
        ax.set_axisbelow(True)
        labels = [d.strftime("%a") + "\n" + d.strftime("%d") for d, _ in series]
        if not any(sum(v.values()) for _, v in series):
            ax.text(0.5, 0.5, "No active time in the last 7 days", ha="center", va="center",
                    color=colors["muted"], transform=ax.transAxes)
            ax.set_axis_off()
        else:
            bottom = [0.0] * len(series)
            for kind in (*pr.KINDS, pr.UNCATEGORIZED):
                vals = [v.get(kind, 0) / 3600 for _, v in series]
                if not any(vals):
                    continue
                ax.bar(labels, vals, bottom=bottom, color=theme.CATEGORY_COLORS[kind],
                       label="No category" if kind == pr.UNCATEGORIZED else kind.capitalize(), width=0.7)
                bottom = [b + x for b, x in zip(bottom, vals)]
            leg = ax.legend(fontsize=6.5, ncol=3, frameon=False, loc="lower left",
                            bbox_to_anchor=(0, 1.0))
            for t in leg.get_texts():
                t.set_color(colors["muted"])
        fig.tight_layout()
        self.canvas.draw_idle()

    # =========================================================================
    # Rules tab
    # =========================================================================
    def _build_rules(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        left = Panel("Category rules: which category is each application in?")
        hint = QLabel("Changes apply immediately and affect all history (rules are by application).")
        hint.setObjectName("sub")
        self.app_table = make_table(["Application", "Executable", "Category"], scroll=True)
        self.app_table.setMinimumHeight(260)
        left.body.addWidget(hint)
        left.body.addWidget(self.app_table)
        self.rules_status = QLabel("")
        self.rules_status.setObjectName("sub")
        left.body.addWidget(self.rules_status)

        right = QVBoxLayout()
        right.setSpacing(12)
        cats = Panel("Custom categories")
        row = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("e.g. Client work")
        add = QPushButton("Add category")
        add.clicked.connect(self._add_category)
        self.new_name.returnPressed.connect(self._add_category)
        row.addWidget(self.new_name, 1)
        row.addWidget(add)
        self.cat_list = QListWidget()
        self.cat_list.setMaximumHeight(110)
        delete = QPushButton("Delete selected custom category")
        delete.clicked.connect(self._delete_category)
        cats.body.addLayout(row)
        cats.body.addWidget(self.cat_list)
        cats.body.addWidget(delete)

        weights = Panel("Score weights (percent credit per category kind)")
        form = QGridLayout()
        self.weight_boxes: dict[str, QSpinBox] = {}
        for i, kind in enumerate(pr.KINDS):
            form.addWidget(QLabel(kind.capitalize() + (" (user-created)" if kind == "custom" else "")), i, 0)
            box = QSpinBox()
            box.setRange(0, 100)
            box.setSuffix(" %")
            box.valueChanged.connect(lambda v, k=kind: self._weight_changed(k, v))
            self.weight_boxes[kind] = box
            form.addWidget(box, i, 1)
        weights.body.addLayout(form)
        formula = QLabel("Score = sum(time x weight) / sum(time), over categorized active time "
                         "only. Uncategorized time is never guessed and is left out. Idle, "
                         "locked and sleeping time never counts.")
        formula.setObjectName("sub")
        formula.setWordWrap(True)
        weights.body.addWidget(formula)
        right.addWidget(cats)
        right.addWidget(weights)
        right.addStretch(1)
        lay.addWidget(left, 3)
        lay.addLayout(right, 2)
        return w

    def _tab_changed(self, index: int) -> None:
        if index == 1:
            self.load_rules()
        else:
            self.refresh()

    def load_rules(self) -> None:
        """Fill the rules tab from the database (small tables, read on the GUI thread)."""
        cats = pr.list_categories(self.db)
        apps = pr.list_applications(self.db)
        t = self.app_table
        t.setRowCount(len(apps))
        for r, a in enumerate(apps):
            t.setItem(r, 0, _item(a["display_name"]))
            t.setItem(r, 1, _item(a["executable_name"]))
            combo = QComboBox()
            combo.addItem(UNCAT, None)
            for c in cats:
                combo.addItem(f"{c['name']}  ({c['kind']})", c["id"])
            idx = combo.findData(a["category_id"])
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(
                lambda _i, exe=a["executable_name"], cb=combo: self._app_category_changed(exe, cb))
            t.setCellWidget(r, 2, combo)
        t.horizontalHeader().setSectionResizeMode(1, t.horizontalHeader().ResizeMode.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(2, t.horizontalHeader().ResizeMode.ResizeToContents)
        self.rules_status.setText("No applications recorded yet." if not apps else "")
        self.cat_list.clear()
        for c in cats:
            if c["kind"] == "custom":
                item = QListWidgetItem(c["name"])
                item.setData(Qt.UserRole, c["id"])
                self.cat_list.addItem(item)
        weights = pr.get_weights(self.db)
        for kind, box in self.weight_boxes.items():
            box.blockSignals(True)
            box.setValue(int(weights[kind]))
            box.blockSignals(False)

    def _app_category_changed(self, exe: str, combo: QComboBox) -> None:
        try:
            pr.set_app_category(self.db, exe, combo.currentData())
            self.rules_status.setText(f"Saved: {exe} -> {combo.currentText().split('  (')[0]}")
        except Exception as e:
            log.exception("Could not save category")
            self.rules_status.setText(f"Could not save: {e}")

    def _add_category(self) -> None:
        try:
            pr.add_category(self.db, self.new_name.text())
        except ValueError as e:
            QMessageBox.warning(self, "Add category", str(e))
            return
        self.new_name.clear()
        self.load_rules()

    def _delete_category(self) -> None:
        item = self.cat_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Delete category", "Select a custom category first.")
            return
        if QMessageBox.question(self, "Delete category",
                                f"Delete '{item.text()}'? Its applications become uncategorized."
                                ) != QMessageBox.Yes:
            return
        pr.delete_category(self.db, item.data(Qt.UserRole))
        self.load_rules()

    def _weight_changed(self, kind: str, value: int) -> None:
        pr.set_weight(self.db, kind, value)
        self.rules_status.setText(f"Saved: {kind} weight = {value}%")


def _item(text: str):
    from PySide6.QtWidgets import QTableWidgetItem
    return QTableWidgetItem(text)
