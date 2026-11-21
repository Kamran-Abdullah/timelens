"""The TimeLens icon, drawn in code (no external image files needed)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap


def make_pixmap(size: int = 256, paused: bool = False) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#4f8bff") if not paused else QColor("#9aa4b5"))
    grad.setColorAt(1, QColor("#1f4fd6") if not paused else QColor("#6b7686"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    c = size / 2
    pen = QPen(QColor("white"), size * 0.09)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(c, c), size * 0.30, size * 0.30)          # the lens
    p.drawLine(QPointF(c, c), QPointF(c, c - size * 0.17))           # clock hands
    p.drawLine(QPointF(c, c), QPointF(c + size * 0.12, c + size * 0.07))
    p.end()
    return pix


def app_icon(paused: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_pixmap(size, paused))
    return icon


def save_icon_files(folder: Path) -> list[Path]:
    """Write assets/timelens.png and timelens.ico (the .ico is used when packaging the EXE)."""
    folder.mkdir(parents=True, exist_ok=True)
    png, ico = folder / "timelens.png", folder / "timelens.ico"
    make_pixmap(256).save(str(png), "PNG")
    make_pixmap(256).save(str(ico), "ICO")
    return [png, ico]
