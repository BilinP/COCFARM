"""COCFARM's loading screen, shown while the window is built (a first start
from a cold disk can take several seconds). Only Qt is imported here, so it
can show before the rest of COCFARM loads."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from farmbot import __version__

LOGO = Path(__file__).parent / "assets" / "cocfarm.png"
WIDTH, HEIGHT = 420, 300
RADIUS, WINDOW_RADIUS = 12, 8  # the card's corners, and a Windows 11 window's
BACKGROUND = QColor("#202020")  # the window's dark theme
BORDER = QColor("#3a3a3a")
ACCENT = QColor("#4c9aff")
TEXT = QColor("#ffffff")
FAINT = QColor("#a0a0a0")

# Leaving it: the card grows into the window (to its size and place) while
# what's on it fades and the logo grows to GROW times its size; then the
# window is shown under it and the card fades away.
GROW = 1.5
GROW_MS = 380
PAINT_MS = 80  # the window's first paint, under the card, before it fades
FADE_MS = 180
LOGO_SIZE = 130
# Where things sit, from the card's middle (it was laid out 420x300).
LOGO_MIDDLE, TITLE_TOP, VERSION_TOP, BAR_TOP, MESSAGE_TOP = -55, 18, 52, 84, 96

_running: list[QVariantAnimation] = []  # kept until they finish


def application() -> QApplication:
    """The one QApplication (made here if the loading screen comes first)."""
    if sys.platform == "win32":
        # Own taskbar entry and icon, rather than grouping under python.exe.
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("COCFARM.App")
        except (AttributeError, OSError):
            pass
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Splash(QWidget):
    """The whole screen, see-through and click-through, with the card in
    the middle: one window from start to finish, so the card can grow into
    COCFARM's without another window taking over (which flickers)."""

    def __init__(self, screen) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput)  # Tool: no taskbar entry
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setScreen(screen)
        self.setGeometry(screen.availableGeometry())
        self.start = QRectF((self.width() - WIDTH) / 2, (self.height() - HEIGHT) / 2, WIDTH, HEIGHT)
        self.end = self.start
        self.text = ""
        self.logo = QPixmap(str(LOGO))
        self.grow = self.fade = 0.0

    def set_grow(self, t: float) -> None:
        self.grow = t
        self.update()

    def set_fade(self, t: float) -> None:
        self.fade = t
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        t = self.grow
        a, b = self.start, self.end
        card = QRectF(a.left() + (b.left() - a.left()) * t, a.top() + (b.top() - a.top()) * t,
                      a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(1 - self.fade)
        path = QPainterPath()
        radius = RADIUS + (WINDOW_RADIUS - RADIUS) * t
        path.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.fillPath(path, BACKGROUND)
        p.setPen(BORDER)
        p.drawPath(path)
        # What's on it: centred, fading as the logo grows.
        x, middle, width = card.left(), card.center().y(), card.width()
        p.setClipPath(path)
        p.setOpacity(p.opacity() * (1 - t))
        if not self.logo.isNull():
            size = LOGO_SIZE * (1 + (GROW - 1) * t)
            p.drawPixmap(QRectF(card.center().x() - size / 2, middle + LOGO_MIDDLE - size / 2, size, size),
                         self.logo, QRectF(self.logo.rect()))
        p.setPen(TEXT)
        p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        p.drawText(QRectF(x, middle + TITLE_TOP, width, 36), Qt.AlignmentFlag.AlignCenter, "COCFARM")
        p.setPen(FAINT)
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(QRectF(x, middle + VERSION_TOP, width, 20), Qt.AlignmentFlag.AlignCenter, f"Version {__version__}")
        p.fillRect(QRectF(card.center().x() - 40, middle + BAR_TOP, 80, 3), ACCENT)
        if self.text:
            p.setFont(QApplication.font())
            p.drawText(QRectF(x, middle + MESSAGE_TOP, width, 20), Qt.AlignmentFlag.AlignCenter, self.text)
        p.end()


_splash: _Splash | None = None


def show() -> QWidget | None:
    """Show the loading screen (once; later calls return the same one)."""
    global _splash
    if _splash is None:
        screen = application().primaryScreen()
        if screen is None:
            return None
        _splash = _Splash(screen)
        _splash.show()
        message("Loading…")
    return _splash


def message(text: str) -> None:
    """Say what's loading, under the logo."""
    if _splash is not None:
        _splash.text = text
        _splash.repaint()
        QApplication.processEvents()


def _animate(duration: int, curve: QEasingCurve.Type, step, done=None) -> None:
    anim = QVariantAnimation()
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    anim.valueChanged.connect(step)

    def finished() -> None:
        if done:
            done()
        _running.remove(anim)

    anim.finished.connect(finished)
    _running.append(anim)
    anim.start()


def _centre(window, screen) -> None:
    """Put ``window`` in the middle of ``screen`` (where the loading screen
    was), not wherever Windows would open it; shrunk to fit a small screen."""
    if screen is None:
        return
    area = screen.availableGeometry()
    window.resize(min(window.width(), area.width()), min(window.height(), area.height()))
    shown = window.geometry()  # what's on screen: the window has no frame (Qt's frameGeometry() is off for it)
    shown.moveCenter(area.center())
    window.setGeometry(shown)


def close() -> None:
    """Leave the loading screen without a window (COCFARM won't open one)."""
    global _splash
    splash, _splash = _splash, None
    if splash is not None:
        splash.close()


def finish(window) -> None:
    """Show ``window`` and leave the loading screen: the card grows into
    the window, which is then shown under it, and fades away."""
    global _splash
    splash, _splash = _splash, None
    if splash is None:
        _centre(window, QApplication.primaryScreen())
        window.show()
        return
    # Shown unseen first: its first show takes a moment, lays it out and
    # settles its size, which the card then grows to.
    window.setWindowOpacity(0.0)
    window.show()
    QApplication.processEvents()
    _centre(window, splash.screen())
    QApplication.processEvents()
    splash.end = QRectF(window.geometry().translated(-splash.geometry().topLeft()))

    def grown() -> None:
        window.setWindowOpacity(1.0)  # under the card (it stays on top), which covers it now

        def gone() -> None:
            splash.close()
            splash.deleteLater()

        QTimer.singleShot(PAINT_MS, lambda: _animate(FADE_MS, QEasingCurve.Type.OutQuad, splash.set_fade, gone))

    _animate(GROW_MS, QEasingCurve.Type.OutCubic, splash.set_grow, grown)
