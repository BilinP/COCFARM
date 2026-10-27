"""COCFARM's loading screen, shown while the window is built (a first start
from a cold disk can take several seconds). Only Qt is imported here, so it
can show before the rest of COCFARM loads."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from farmbot import __version__

LOGO = Path(__file__).parent / "assets" / "cocfarm.png"
WIDTH, HEIGHT = 420, 300
BACKGROUND = QColor("#202020")  # the window's dark theme
BORDER = QColor("#3a3a3a")
ACCENT = QColor("#4c9aff")
TEXT = QColor("#ffffff")
FAINT = QColor("#a0a0a0")

# Leaving it, as on Android: the logo grows to GROW times its size while the
# loading screen fades out and the window fades in.
GROW = 1.5
LEAVE_MS = 420
WINDOW_IN_MS = 380
LOGO_SIZE, LOGO_TOP = 130, 30

_splash: QSplashScreen | None = None
_ratio = 1.0
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


def _picture(ratio: float, grow: float = 0.0) -> QPixmap:
    """The card; ``grow`` 0..1: how far the logo has grown on the way out."""
    pixmap = QPixmap(round(WIDTH * ratio), round(HEIGHT * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    card = QPainterPath()
    card.addRoundedRect(QRectF(0.5, 0.5, WIDTH - 1, HEIGHT - 1), 12, 12)
    p.fillPath(card, BACKGROUND)
    p.setPen(BORDER)
    p.drawPath(card)
    logo = QPixmap(str(LOGO))
    if not logo.isNull():
        size = LOGO_SIZE * (1 + (GROW - 1) * grow)
        middle = LOGO_TOP + LOGO_SIZE / 2
        p.save()
        p.setClipPath(card)
        p.setOpacity(1 - grow)
        p.drawPixmap(QRectF((WIDTH - size) / 2, middle - size / 2, size, size), logo, QRectF(logo.rect()))
        p.restore()
    p.setPen(TEXT)
    p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
    p.drawText(QRectF(0, 168, WIDTH, 36), Qt.AlignmentFlag.AlignCenter, "COCFARM")
    p.setPen(FAINT)
    p.setFont(QFont("Segoe UI", 9))
    p.drawText(QRectF(0, 202, WIDTH, 20), Qt.AlignmentFlag.AlignCenter, f"Version {__version__}")
    p.fillRect(QRectF(WIDTH / 2 - 40, 234, 80, 3), ACCENT)
    p.end()
    return pixmap


def show() -> QSplashScreen:
    """Show the loading screen (once; later calls return the same one)."""
    global _splash, _ratio
    if _splash is None:
        app = application()
        screen = app.primaryScreen()
        _ratio = screen.devicePixelRatio() if screen else 1.0
        _splash = QSplashScreen(_picture(_ratio))
        _splash.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        _splash.show()
        message("Loading…")
    return _splash


def message(text: str) -> None:
    """Say what's loading, under the logo."""
    if _splash is not None:
        _splash.showMessage(text, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, FAINT)
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


def finish(window) -> None:
    """Show ``window`` and leave the loading screen: its logo grows and it
    fades out over the window, which fades in."""
    global _splash
    splash, _splash = _splash, None
    if splash is None:
        window.show()
        return
    window.setWindowOpacity(0.0)
    window.show()
    splash.raise_()
    _animate(WINDOW_IN_MS, QEasingCurve.Type.OutQuad, window.setWindowOpacity)

    def leave(t: float) -> None:
        splash.setPixmap(_picture(_ratio, t))
        splash.setWindowOpacity(1 - t)

    _animate(LEAVE_MS, QEasingCurve.Type.InCubic, leave, splash.close)
