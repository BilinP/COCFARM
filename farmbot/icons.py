"""COCFARM's own resource icons, drawn in code: a gold coin, a pink elixir
drop and a dark elixir drop. Used on the Discord loot card and the "When to
stop" buttons, so no game artwork ships with COCFARM. Needs Qt."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient

# Light, main, dark (outline) colour of each.
COLOURS = {
    "gold": ("#FFF1A8", "#F5C02E", "#A86E00"),
    "elixir": ("#FFC2F7", "#D158E8", "#7B1F96"),
    "dark": ("#A99BC4", "#4B3D66", "#17111F"),
}


def _drop(rect: QRectF) -> QPainterPath:
    """A drop: a round bottom narrowing to a point at the top."""
    r = rect.width() * 0.36
    cx, cy = rect.center().x(), rect.bottom() - r - rect.height() * 0.06
    tip = QPointF(cx, rect.top() + rect.height() * 0.04)
    path = QPainterPath(tip)
    path.quadTo(cx + r * 1.05, cy - r * 0.95, cx + r, cy)
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 0, -180)
    path.quadTo(cx - r * 1.05, cy - r * 0.95, tip.x(), tip.y())
    return path


def draw(painter: QPainter, rect: QRectF, key: str) -> None:
    """Draw the ``key`` ("gold", "elixir" or "dark") icon filling ``rect``."""
    light, main, edge = (QColor(c) for c in COLOURS[key])
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    size = min(rect.width(), rect.height())
    rect = QRectF(rect.center().x() - size / 2, rect.center().y() - size / 2, size, size)
    outline = max(1.0, size / 22)
    glow = QRadialGradient(rect.left() + size * 0.35, rect.top() + size * 0.35, size * 0.7)
    glow.setColorAt(0, light)
    glow.setColorAt(0.55, main)
    glow.setColorAt(1, main.darker(125))
    painter.setPen(QPen(edge, outline))
    painter.setBrush(glow)
    if key == "gold":
        coin = rect.adjusted(size * 0.08, size * 0.08, -size * 0.08, -size * 0.08)
        painter.drawEllipse(coin)
        # The raised rim: a ring just inside the edge.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(main.darker(130), outline))
        inner = size * 0.13
        painter.drawEllipse(coin.adjusted(inner, inner, -inner, -inner))
        shine = QRectF(coin.left() + size * 0.2, coin.top() + size * 0.17, size * 0.22, size * 0.13)
    else:
        painter.drawPath(_drop(rect))
        shine = QRectF(rect.left() + size * 0.36, rect.top() + size * 0.5, size * 0.12, size * 0.2)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(255, 255, 255, 150))
    painter.drawEllipse(shine)
    painter.restore()


def pixmap(key: str, size: int, ratio: float = 2.0) -> QPixmap:
    """The icon as a transparent pixmap, sharp at ``ratio`` x pixel density."""
    image = QPixmap(round(size * ratio), round(size * ratio))
    image.fill(Qt.transparent)
    painter = QPainter(image)
    draw(painter, QRectF(0, 0, image.width(), image.height()), key)
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


def icon(key: str, size: int = 32) -> QIcon:
    return QIcon(pixmap(key, size))
