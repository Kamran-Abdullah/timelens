"""Small line icons for the sidebar, drawn with QPainter (no image files to ship)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF


def _canvas(size: int):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    return pix, p


def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw(name: str, p: QPainter, s: float, color: str) -> None:
    u = s / 24.0                                   # all coordinates are on a 24-unit grid
    p.setPen(_pen(color, 1.8 * u))
    p.setBrush(Qt.NoBrush)
    r = lambda x, y, w, h: QRectF(x * u, y * u, w * u, h * u)
    pt = lambda x, y: QPointF(x * u, y * u)
    if name == "Dashboard":
        for x, y in ((3, 3), (13, 3), (3, 13), (13, 13)):
            p.drawRoundedRect(r(x, y, 8, 8), 2 * u, 2 * u)
    elif name == "Live Monitor":
        p.drawEllipse(pt(12, 12), 8 * u, 8 * u)
        p.setBrush(QColor(color))
        p.drawEllipse(pt(12, 12), 2.6 * u, 2.6 * u)
    elif name == "Application Analytics":
        for y, w in ((5, 16), (11, 11), (17, 7)):
            p.drawLine(pt(4, y), pt(4 + w, y))
    elif name == "Document Analytics":
        p.drawRoundedRect(r(5, 3, 14, 18), 2 * u, 2 * u)
        p.drawLine(pt(8.5, 9), pt(15.5, 9))
        p.drawLine(pt(8.5, 13), pt(15.5, 13))
        p.drawLine(pt(8.5, 17), pt(12.5, 17))
    elif name == "Activity Timeline":
        p.drawLine(pt(7, 4), pt(7, 20))
        for y in (6, 12, 18):
            p.setBrush(QColor(color))
            p.drawEllipse(pt(7, y), 2 * u, 2 * u)
            p.drawLine(pt(11.5, y), pt(20, y))
    elif name == "Productivity Analytics":
        p.drawPolyline(QPolygonF([pt(3, 17), pt(9, 11), pt(13, 15), pt(21, 6)]))
        p.drawPolyline(QPolygonF([pt(15, 6), pt(21, 6), pt(21, 12)]))
    elif name == "Reports":
        p.drawRoundedRect(r(4, 3, 16, 18), 2 * u, 2 * u)
        for x, y, h in ((8, 14, 4), (12, 10, 8), (16, 12, 6)):
            p.drawLine(pt(x, y + h), pt(x, y))
    elif name == "Websites":
        p.drawEllipse(pt(12, 12), 9 * u, 9 * u)
        p.drawEllipse(pt(12, 12), 4 * u, 9 * u)
        p.drawLine(pt(3, 12), pt(21, 12))
    elif name == "Assistant":
        p.drawRoundedRect(r(3, 4, 18, 13), 4 * u, 4 * u)
        p.drawPolyline(QPolygonF([pt(8, 17), pt(8, 21), pt(12.5, 17)]))
        p.drawLine(pt(8, 9), pt(16, 9))
        p.drawLine(pt(8, 12.5), pt(13, 12.5))
    elif name == "Settings":
        p.drawEllipse(pt(12, 12), 3.2 * u, 3.2 * u)
        p.drawEllipse(pt(12, 12), 7.4 * u, 7.4 * u)
        import math
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(pt(12 + 7.4 * math.cos(a), 12 + 7.4 * math.sin(a)),
                       pt(12 + 10 * math.cos(a), 12 + 10 * math.sin(a)))
    elif name == "sun":
        import math
        p.drawEllipse(pt(12, 12), 4.5 * u, 4.5 * u)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(pt(12 + 7.5 * math.cos(a), 12 + 7.5 * math.sin(a)),
                       pt(12 + 10 * math.cos(a), 12 + 10 * math.sin(a)))
    elif name == "moon":
        p.setBrush(QColor(color))
        p.drawEllipse(pt(12, 12), 8 * u, 8 * u)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.setBrush(QColor(0, 0, 0, 255))
        p.setPen(Qt.NoPen)
        p.drawEllipse(pt(16, 9), 7 * u, 7 * u)


def nav_icon(name: str, color: str = "#ffffff", size: int = 22) -> QIcon:
    pix, p = _canvas(size * 2)                      # drawn at 2x so it stays sharp on HiDPI
    _draw(name, p, size * 2, color)
    p.end()
    pix.setDevicePixelRatio(2.0)
    return QIcon(pix)


def ensure_icons() -> dict:
    """Draw the small tick and arrow images the stylesheet needs (once) and return their paths
    (forward slashes, as Qt stylesheets require)."""
    folder = Path(tempfile.gettempdir()) / "TimeLens-ui"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {name: folder / f"{name}.png" for name in ("check", "down", "up")}
    if not paths["check"].exists():
        pix, p = _canvas(36)
        p.setPen(_pen("#ffffff", 4.2))
        p.drawPolyline(QPolygonF([QPointF(8, 19), QPointF(15, 26), QPointF(28, 10)]))
        p.end()
        pix.save(str(paths["check"]), "PNG")
    if not paths["down"].exists():
        for name, pts in (("down", [(8, 13), (18, 23), (28, 13)]), ("up", [(8, 23), (18, 13), (28, 23)])):
            pix, p = _canvas(36)
            p.setPen(_pen("#8a8fb5", 4.0))
            p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
            p.end()
            pix.save(str(paths[name]), "PNG")
    return {k: v.as_posix() for k, v in paths.items()}
