"""Small reusable widgets shared by the pages."""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QScrollArea, QGraphicsDropShadowEffect, QHeaderView,
                               QLabel, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ui import theme


def _soft_shadow(widget: QWidget) -> None:
    """A gentle drop shadow that makes a card float above the page."""
    fx = QGraphicsDropShadowEffect(widget)
    fx.setBlurRadius(22)
    fx.setOffset(0, 3)
    fx.setColor(QColor(40, 40, 110, 38))
    widget.setGraphicsEffect(fx)


class Card(QFrame):
    """A titled number tile, e.g. 'Active time / 3h 12m'. The accent colour picks a tone:
    the tile gets a tinted gradient, a coloured top edge and a coloured number."""

    def __init__(self, title: str, accent: str | None = None):
        super().__init__()
        self.setObjectName("card")
        tone = theme.TONE_BY_COLOR.get((accent or "").lower())
        if tone:
            self.setProperty("tone", tone)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.value = QLabel("-")
        self.value.setObjectName("cardValue")
        self.note = QLabel("")
        self.note.setObjectName("sub")
        for w in (self.title, self.value, self.note):
            lay.addWidget(w)
        _soft_shadow(self)

    def set(self, value: str, note: str = "") -> None:
        self.value.setText(value)
        self.note.setText(note)


class Panel(QFrame):
    """A rounded container with a heading."""

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        heading = QLabel(title)
        heading.setObjectName("panelTitle")
        self.body.addWidget(heading)
        _soft_shadow(self)


class PageHeader(QFrame):
    """The gradient banner at the top of every page: a title and a one-line status."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setObjectName("banner")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)   # never stretch to fill the page
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 18, 26, 18)
        lay.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("bannerTitle")
        self.sub = QLabel(subtitle)
        self.sub.setObjectName("bannerSub")
        self.sub.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)

    def paintEvent(self, event):
        super().paintEvent(event)           # the gradient comes from the stylesheet
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), 18, 18)     # keep the circles inside the corners
        p.setClipPath(clip)
        for cx, cy, r, alpha in ((w - 40, 10, 110, 34), (w - 150, h + 10, 80, 26), (w - 270, -20, 60, 20)):
            p.setBrush(QColor(255, 255, 255, alpha))
            p.drawEllipse(QPointF(cx, cy), r, r)
        p.end()


def add_header(layout, title: str, subtitle: str = "") -> QLabel:
    """Add a page banner to `layout`; returns its subtitle label (pages update it with status)."""
    header = PageHeader(title, subtitle)
    layout.addWidget(header)
    return header.sub


class SortItem(QTableWidgetItem):
    """Table cell that shows text but sorts by a number (so '9m' < '2h')."""

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self._key = text if key is None else key

    def __lt__(self, other):
        if isinstance(other, SortItem):
            return self._key < other._key
        return super().__lt__(other)


def make_table(headers: list[str], scroll: bool = False) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.NoSelection)
    t.setShowGrid(False)
    t.setAlternatingRowColors(True)
    if not scroll:
        t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # small top-5 tables
    t.setFocusPolicy(Qt.NoFocus)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    t.setMinimumHeight(185)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return t


@contextmanager
def bulk_fill(t: QTableWidget):
    """Fill a table without a column on ResizeToContents re-measuring every row on each setItem()
    (quadratic: a second refill of ~2000 rows never finished). Modes are restored afterwards."""
    hdr = t.horizontalHeader()
    modes = [hdr.sectionResizeMode(c) for c in range(t.columnCount())]
    for c in range(t.columnCount()):
        hdr.setSectionResizeMode(c, hdr.ResizeMode.Interactive)
    t.setUpdatesEnabled(False)
    try:
        yield
    finally:
        for c, m in enumerate(modes):
            hdr.setSectionResizeMode(c, m)
        t.setUpdatesEnabled(True)


def fill_table(t: QTableWidget, rows: list[list[str]], right_cols: tuple[int, ...] = ()) -> None:
    with bulk_fill(t):
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if c in right_cols:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r, c, item)


class PendingPage(QWidget):
    """Honest stand-in for a page that belongs to a later phase."""

    def __init__(self, title: str, phase: int, what: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        h = QLabel(title)
        h.setObjectName("h1")
        msg = QLabel(f"Not built yet - scheduled for Phase {phase}.\n{what}")
        msg.setObjectName("sub")
        msg.setWordWrap(True)
        lay.addWidget(h)
        lay.addWidget(msg)
        lay.addStretch(1)


def fill_sortable(t: QTableWidget, rows: list[list[tuple]], right_cols: tuple[int, ...] = ()) -> None:
    """Fill with (text, sort_key) cells. Rows keep the given order until the user clicks a
    column header, which then sorts by the numeric key."""
    t.setSortingEnabled(False)
    with bulk_fill(t):
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, (text, key) in enumerate(row):
                item = SortItem(text, key)
                if c in right_cols:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r, c, item)
    t.horizontalHeader().setSortIndicator(-1, Qt.DescendingOrder)  # -1 = keep given order
    t.setSortingEnabled(True)


def set_headers(t: QTableWidget, headers: list[str], right_cols: tuple[int, ...] = ()) -> None:
    """Set header labels, right-aligning the numeric columns to sit over their numbers."""
    t.setHorizontalHeaderLabels(headers)
    for c in right_cols:
        t.horizontalHeaderItem(c).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)


def page_layout(page: QWidget, spacing: int = 12) -> QVBoxLayout:
    """The standard page body: a vertical layout inside a scroll area.

    When the window is too short for a page's content the page scrolls instead of squeezing its
    widgets on top of each other (which showed up as overlapping text)."""
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    outer.addWidget(scroll)
    content = QWidget()
    content.setObjectName("page")
    scroll.setWidget(content)
    body = QVBoxLayout(content)
    body.setContentsMargins(28, 24, 28, 24)
    body.setSpacing(spacing)
    return body
