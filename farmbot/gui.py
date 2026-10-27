"""COCFARM (farmbot) control window, built with PySide6 (Qt 6) and Fluent Widgets.

* Pages are grouped by what the player wants to do (Dashboard, each village,
  When to play, History, Setup, Advanced); the first run opens Setup.
* The Dashboard and the title bar always show what the bot is doing in plain words
  (farmbot/friendly.py), and problems come with a button for the fix.
* Every setting is a Fluent setting card with its explanation underneath,
  and that line turns into the error message if the value is invalid.
* Settings save automatically as soon as they are valid, and a running
  bot picks them up at its next step. Light and dark themes.

Keys: F5 start/resume · Esc pause, then Esc again to stop now · Ctrl+1…7 switch page ·
Tab between controls, Enter presses the focused button.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from html import escape
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidgetItem,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, EditableComboBox, ElevatedCardWidget,
                            FluentIcon as FIF, FluentWindow, InfoBar, InfoBarIcon, InfoBarPosition, LineEdit, ListWidget,
                            NavigationItemPosition, PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton,
                            SegmentedWidget,
                            SettingCard, SettingCardGroup, SimpleCardWidget, Slider, SmoothScrollArea,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, Theme, TitleLabel, TogglePushButton,
                            isDarkTheme, qconfig, setCustomStyleSheet, setTheme, setThemeColor)

from farmbot import __version__, catalog, icons
from farmbot.config import Config, ensure_config, load_config, save_config
from farmbot.form import BY_PATH, apply_form, form_values, validate
from farmbot.friendly import Problem, Status, describe_status, explain
from farmbot.geometry import Rect
from farmbot.vision import TemplateLibrary

log = logging.getLogger(__name__)

APP_NAME = "COCFARM"
ASSETS = Path(__file__).parent / "assets"
LOGO = ASSETS / "cocfarm.png"
ACCENT = "#4c9aff"
ICON_TOOLS = getattr(FIF, "DEVELOPER_TOOLS", FIF.SETTING)
MEMORY_CHOICES = (2048, 4096, 8192)  # MB; sizes both BlueStacks and LDPlayer accept
DPI_CHOICES = (160, 240, 320)
TRAINED_DISPLAY = {"device.display_width": 860, "device.display_height": 672, "device.display_dpi": 240}
TRAINED_NOTE = "COCFARM is trained on 860 x 672 at 240 DPI; other values may stop it reading the game."
ICON_SCREEN = getattr(FIF, "FIT_PAGE", FIF.ZOOM)


class Tone:
    """A colour with a twin for the light theme; formats as whichever theme
    is showing. Every pair keeps text at WCAG AA contrast (4.5:1) on its
    theme's cards."""

    def __init__(self, dark: str, light: str):
        self.dark, self.light = dark, light

    def __str__(self) -> str:
        return self.dark if isDarkTheme() else self.light

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


GOOD = Tone("#6ccb5f", "#0f7b0f")
BAD = Tone("#ff99a4", "#c42b1c")
WARN = Tone("#fce100", "#8a5a00")
MUTED = Tone("#9da5b4", "#5d6470")
TEXT = Tone("#d6dae0", "#1b1b1b")
SUBTLE = Tone("rgba(255,255,255,0.06)", "rgba(0,0,0,0.05)")  # a faint chip background
RESOURCE_COLOURS = {"gold": Tone("#f5c542", "#8a6100"), "elixir": Tone("#d58cff", "#8b3fbf"),
                    "dark": Tone("#b4b4e6", "#4a4a8c")}
STATES = {  # chip text colour, chip background: the same in both themes
    "ready": ("#c5c9d0", "#3a3f47"),
    "running": ("#ffffff", "#1f6feb"),
    "attacking": ("#ffffff", "#c2410c"),
    "paused": ("#1b1b1b", "#e3b341"),
    "waiting": ("#10233b", "#8cc8ff"),
    "stopped": ("#ffffff", "#4a5160"),
    "problem": ("#ffffff", "#c42b1c"),
}
THEMES = {"auto": "Follow Windows", "light": "Light", "dark": "Dark"}

_TINTED: list[tuple[QWidget, Any, str]] = []  # every coloured widget, to recolour on a theme change


def tint(widget: QWidget, tone: Any, extra: str = "") -> QWidget:
    """Colour ``widget``'s text (``None``: the theme's own) plus any extra
    style, and do it again whenever the theme changes."""
    _TINTED[:] = [t for t in _TINTED if t[0] is not widget]
    _TINTED.append((widget, tone, extra))
    widget.setStyleSheet((f"color: {tone}; " if tone is not None else "") + extra)
    return widget


def retint() -> None:
    alive = []
    for widget, tone, extra in _TINTED:
        try:
            widget.setStyleSheet((f"color: {tone}; " if tone is not None else "") + extra)
            alive.append((widget, tone, extra))
        except RuntimeError:  # deleted with its window
            pass
    _TINTED[:] = alive
# Top-level pages, most used first; Ctrl+1... follow this order.
PAGES = ("Dashboard", "Home village", "Builder base", "When to play", "History", "Setup", "Advanced")


class _QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", (record.levelno, self.format(record))))


# ====================================================================== bindings
class Binding:
    """Connects one config path to a widget: read, write, and change signal."""

    def __init__(self, get: Callable[[], Any], set_: Callable[[Any], None], connect: Callable[[Callable], None],
                 widgets: list[QWidget]):
        self.get, self.set, self.connect, self.widgets = get, set_, connect, widgets

    def enable(self, on: bool) -> None:
        for w in self.widgets:
            w.setEnabled(on)


def bind_switch(w: SwitchButton) -> Binding:
    w.setOnText("On")
    w.setOffText("Off")
    return Binding(w.isChecked, lambda v: w.setChecked(bool(v)), w.checkedChanged.connect, [w])


def bind_line(w: LineEdit) -> Binding:
    return Binding(w.text, lambda v: w.setText(str(v)), w.textChanged.connect, [w])


def bind_combo(w: EditableComboBox) -> Binding:
    return Binding(w.currentText, lambda v: w.setText(str(v)), w.currentTextChanged.connect, [w])


def bind_choice(w: ComboBox, labels: dict[str, str]) -> Binding:
    """A fixed dropdown ↔ a value; ``labels`` maps values to the text shown."""
    values = list(labels)
    for value in values:
        w.addItem(labels[value])

    def set_(value: Any) -> None:
        key = str(value)
        if key in values:
            w.setCurrentIndex(values.index(key))

    return Binding(lambda: values[max(w.currentIndex(), 0)], set_, w.currentIndexChanged.connect, [w])


def bind_segments(w: SegmentedWidget, keys: list[str] | dict[str, str]) -> Binding:
    """Segments ↔ a value; ``keys`` maps values to labels (or is a list of both)."""
    labels = keys if isinstance(keys, dict) else {k: k for k in keys}
    state = {"value": next(iter(labels))}
    callbacks: list[Callable] = []

    def chosen(key: str) -> None:
        state["value"] = key
        for cb in callbacks:
            cb()

    for key, label in labels.items():
        w.addItem(key, label, onClick=lambda _=False, k=key: chosen(k))

    def set_(value: Any) -> None:
        key = str(value)
        state["value"] = key
        if key in labels:
            w.setCurrentItem(key)

    return Binding(lambda: state["value"], set_, callbacks.append, [w])


def _wrapping(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    return label


def bind_unit_picker(w: ListWidget, units, has_picture: Callable[[str], bool]) -> Binding:
    """A tick-and-drag list of units ↔ the ticked names, in list order."""
    callbacks: list[Callable] = []

    def fill(chosen: list[str]) -> None:
        w.blockSignals(True)
        w.clear()
        order = [u for name in chosen for u in units if u.name == name] + [u for u in units if u.name not in chosen]
        for unit in order:
            text = f"{unit.name}  ·  {unit.housing} space" + ("" if has_picture(unit.name) else "  (no picture yet)")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, unit.name)
            # Ticked by a click anywhere on the row (below), not only on the small box.
            item.setFlags((item.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if unit.name in chosen else Qt.Unchecked)
            w.addItem(item)
        w.blockSignals(False)

    def get() -> list[str]:
        return [w.item(i).data(Qt.UserRole) for i in range(w.count()) if w.item(i).checkState() == Qt.Checked]

    def changed(*_):
        for cb in callbacks:
            cb()

    def clicked(item: QListWidgetItem) -> None:
        # Ticked units gather at the top in the order they're ticked (the
        # order they're given in); unticked ones drop back below them.
        on = item.checkState() != Qt.Checked
        w.blockSignals(True)
        row = w.row(item)
        w.takeItem(row)
        ticked = sum(w.item(i).checkState() == Qt.Checked for i in range(w.count()))
        item.setCheckState(Qt.Checked if on else Qt.Unchecked)
        w.insertItem(ticked, item)
        w.setCurrentItem(item)
        w.blockSignals(False)
        changed()

    w.itemClicked.connect(clicked)
    w.itemChanged.connect(changed)
    w.model().rowsMoved.connect(changed)
    fill([])
    return Binding(get, fill, callbacks.append, [w])


def bind_toggles(buttons: dict[Any, TogglePushButton]) -> Binding:
    """A set of toggle buttons ↔ a list of the keys that are on."""
    return Binding(
        lambda: [k for k, b in buttons.items() if b.isChecked()],
        lambda values: [b.setChecked(k in values) for k, b in buttons.items()],
        lambda cb: [b.toggled.connect(cb) for b in buttons.values()],
        list(buttons.values()),
    )


# ====================================================================== pages
class Page(SmoothScrollArea):
    """A scrolling page of setting groups. Without a title it's the body of
    a tab (the tabbed page above it shows the title)."""

    def __init__(self, name: str, title: str = "", subtitle: str = ""):
        super().__init__()
        self.setObjectName(name)
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self.layout_ = QVBoxLayout(body)
        self.layout_.setContentsMargins(*((36, 28, 36, 28) if title else (36, 8, 36, 28)))
        self.layout_.setSpacing(20)
        self.layout_.setAlignment(Qt.AlignTop)
        if title:
            self.layout_.addWidget(TitleLabel(title))
        if subtitle:
            self.layout_.addWidget(_subtitle(subtitle))
        self.setWidget(body)

    def add(self, widget: QWidget) -> QWidget:
        self.layout_.addWidget(widget)
        return widget


def _subtitle(text: str) -> BodyLabel:
    sub = BodyLabel(text)
    sub.setWordWrap(True)  # never wider than the window, however small
    sub.setTextColor(QColor(MUTED.light), QColor(MUTED.dark))
    return sub


class TabbedPage(QWidget):
    """A page whose settings are split over tabs, each a scrolling page."""

    def __init__(self, name: str, title: str, subtitle: str):
        super().__init__()
        self.setObjectName(name)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 28, 0, 0)
        outer.setSpacing(12)
        head = QVBoxLayout()
        head.setContentsMargins(36, 0, 36, 0)
        head.setSpacing(12)
        head.addWidget(TitleLabel(title))
        head.addWidget(_subtitle(subtitle))
        self.tabs = SegmentedWidget()
        head.addWidget(self.tabs, 0, Qt.AlignLeft)
        outer.addLayout(head)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.tab_pages: dict[str, Page] = {}

    def tab(self, key: str, label: str, intro: str = "") -> Page:
        page = Page(f"{self.objectName()}-{key}", "", intro)
        self.tab_pages[key] = page
        self.stack.addWidget(page)
        self.tabs.addItem(key, label, onClick=lambda _=False, k=key: self.show_tab(k))
        if len(self.tab_pages) == 1:
            self.show_tab(key)
        return page

    def show_tab(self, key: str) -> None:
        self.stack.setCurrentWidget(self.tab_pages[key])
        self.tabs.setCurrentItem(key)


CARD_HEIGHT = 70  # a setting card's usual height


def _relayout_group(group: "SettingCardGroup") -> None:
    """Re-place a group's cards after one changed height, and resize the group."""
    group._relayout_due = False
    cards = group.cardLayout
    cards.setGeometry(cards.geometry())  # moves every card under the one above it
    height = group.layout().totalHeightForWidth(group.width()) if group.layout().hasHeightForWidth() else 0
    if height and height != group.height():
        group.setMinimumHeight(height)
        group.resize(group.width(), height)
    group.updateGeometry()


class Card(SettingCard):
    """A setting card whose description doubles as its error line."""

    def __init__(self, icon, title: str, help_text: str, control: QWidget | None = None, *extra: QWidget):
        super().__init__(icon, title, help_text or " ")
        self.help_text = help_text
        self.controls = ([control] if control else []) + list(extra)
        for w in self.controls:
            self.hBoxLayout.addWidget(w, 0, Qt.AlignRight | Qt.AlignVCenter)
            self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addSpacing(8)
        # The title and explanation wrap instead of widening the window, and
        # the card grows to fit them (SettingCard is a fixed 70 px otherwise).
        # Their column takes the spare width (the base class gives it to a
        # stretch after it, and pins the labels to their one-line width).
        for label in (self.titleLabel, self.contentLabel):
            label.setWordWrap(True)
            self.vBoxLayout.setAlignment(label, Qt.Alignment())
        # Centre the text with stretches: the layouts' own centring goes by a
        # wrapping label's preferred size, which Qt makes narrow and tall.
        self.hBoxLayout.setAlignment(Qt.Alignment())
        self.vBoxLayout.setAlignment(Qt.Alignment())
        self.vBoxLayout.insertStretch(0, 1)
        self.vBoxLayout.addStretch(1)
        self.hBoxLayout.setStretch(2, 1)  # the title/explanation column
        self.hBoxLayout.setStretch(4, 0)  # the base class's stretch
        # Measure once the labels have their new width (the card's own resize
        # comes before its layout moves them).
        self.titleLabel.installEventFilter(self)
        for w in self.controls:
            if isinstance(w, QLabel) and w.wordWrap():
                w.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize:
            self._fit_soon()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_soon()

    def setContent(self, content: str) -> None:
        super().setContent(content)
        self._fit_soon()

    def _fit_soon(self) -> None:
        # Not in the middle of a layout pass: a height set there isn't laid out.
        if not getattr(self, "_fit_due", False):
            self._fit_due = True
            QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        self._fit_due = False
        width = self.titleLabel.width()
        if width <= 0:
            return
        text = self.contentLabel.heightForWidth(width) if self.contentLabel.text().strip() else 0
        needed = max(CARD_HEIGHT, self.titleLabel.heightForWidth(width) + text + 26)
        for w in self.controls:  # a wrapping label used as the control (e.g. what was found)
            if isinstance(w, QLabel) and w.wordWrap() and w.width() > 0:
                needed = max(needed, w.heightForWidth(w.width()) + 24)
        if needed != self.height():
            self.setFixedHeight(needed)
            self.hBoxLayout.setGeometry(self.rect())
            # The group's layout only follows a card's height when its width
            # didn't change too: have it place its cards and measure again.
            group = self.parentWidget()
            if isinstance(group, SettingCardGroup) and not getattr(group, "_relayout_due", False):
                group._relayout_due = True
                QTimer.singleShot(0, lambda: _relayout_group(group))

    def show_error(self, message: str | None) -> None:
        if message:
            self.setContent(f"⚠  {message}")
            tint(self.contentLabel, BAD)
        else:
            self.setContent(self.help_text or " ")
            tint(self.contentLabel, None)

    def show_warning(self, message: str) -> None:
        self.setContent(f"⚠  {message}")
        tint(self.contentLabel, WARN)


def to_pixmap(image: np.ndarray) -> QPixmap:
    """A BGR OpenCV image as a QPixmap."""
    rgb = np.ascontiguousarray(image[:, :, ::-1])
    h, w = rgb.shape[:2]
    return QPixmap.fromImage(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())


class CropCanvas(QWidget):
    """Shows a screenshot scaled to fit; drag to select a box on it."""

    def __init__(self):
        super().__init__()
        self.image: np.ndarray | None = None
        self._pixmap: QPixmap | None = None
        self._start: QPoint | None = None
        self._box: QRect | None = None  # in image pixels
        self.on_change: Callable[[], None] = lambda: None
        self.setMinimumHeight(360)
        self.setCursor(Qt.CrossCursor)

    def set_image(self, image: np.ndarray) -> None:
        self.image, self._pixmap, self._box = image, to_pixmap(image), None
        self.on_change()
        self.update()

    def selection(self) -> Rect | None:
        if self._box is None or self._box.width() < 4 or self._box.height() < 4:
            return None
        b = self._box
        return Rect(b.x(), b.y(), b.width(), b.height())

    def crop(self) -> np.ndarray | None:
        r = self.selection()
        if self.image is None or r is None:
            return None
        return self.image[r.y:r.y + r.h, r.x:r.x + r.w].copy()

    def select(self, rect: Rect | None) -> None:
        self._box = QRect(rect.x, rect.y, rect.w, rect.h) if rect else None
        self.on_change()
        self.update()

    def _frame(self) -> tuple[QRect, float]:
        """Where the image is drawn, and widget pixels per image pixel."""
        iw, ih = self._pixmap.width(), self._pixmap.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = round(iw * scale), round(ih * scale)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), scale

    def _to_image(self, pos: QPoint) -> QPoint:
        frame, scale = self._frame()
        x = min(max(round((pos.x() - frame.x()) / scale), 0), self._pixmap.width())
        y = min(max(round((pos.y() - frame.y()) / scale), 0), self._pixmap.height())
        return QPoint(x, y)

    def mousePressEvent(self, event) -> None:
        if self._pixmap is not None and event.button() == Qt.LeftButton:
            self._start = self._to_image(event.position().toPoint())
            self._box = QRect(self._start, self._start)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            self._box = QRect(self._start, self._to_image(event.position().toPoint())).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._start is not None:
            self._start = None
            self.on_change()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(255, 255, 255, 10))
        if self._pixmap is None:
            painter.setPen(QColor(str(MUTED)))
            painter.drawText(self.rect(), Qt.AlignCenter, "Take a screenshot or open one, then drag a box around the button.")
            return
        frame, scale = self._frame()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(frame, self._pixmap)
        if self._box is not None:
            b = self._box
            shown = QRect(frame.x() + round(b.x() * scale), frame.y() + round(b.y() * scale),
                          round(b.width() * scale), round(b.height() * scale))
            painter.fillRect(shown, QColor(76, 154, 255, 50))
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.drawRect(shown)


def _muted(label: QLabel) -> QLabel:
    tint(label, MUTED)
    return label


class FarmbotWindow(FluentWindow):
    def __init__(self, config_path: str, serial: str | None = None):
        super().__init__()
        self.config_path = Path(config_path)
        self.first_run = ensure_config(self.config_path)
        if self.first_run:
            log.info("First run: created %s with the settings for BlueStacks at 860 x 672", self.config_path)
        self.config = load_config(self.config_path)
        if serial:
            self.config.device.serial = serial
        self.events: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.pause_event = threading.Event()  # Pause: the bot waits at its next safe point
        self.thread: threading.Thread | None = None
        self.bindings: dict[str, Binding] = {}
        self.cards: dict[str, Card] = {}
        self.dependents: list[tuple[str, list[Binding], Any]] = []
        self.has_errors = False
        self._loading = False
        self.session_start: float | None = None
        self.stats = {"bases": 0, "attacks": 0}
        self.gained = {"gold": 0, "elixir": 0, "dark": 0}
        self.update_release = None  # a newer release on GitHub
        self.update_staged: Path | None = None  # downloaded and unpacked, ready to install
        self.update_busy = False
        self.update_bar: InfoBar | None = None

        self.setWindowTitle(f"{APP_NAME} {__version__}")  # the version is in every bug report screenshot
        self.setWindowIcon(QIcon(str(LOGO)))
        self._sharp_title_icon()
        # Keep page names visible next to the icons (recognition over recall).
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setMinimumExpandWidth(900)
        self.resize(1160, 780)
        self.setMinimumSize(980, 640)
        # Theme first: set once every widget exists, it would restyle them all again (seconds).
        self._theme = None
        if self.config.bot.theme in THEMES:
            self._apply_theme(self.config.bot.theme)
        self._build_title_bar()
        self.pages: dict[str, QWidget] = {}
        self.mirrors: dict[str, list[Binding]] = {}  # a setting shown on two pages
        self.mirror_cards: dict[str, list[Card]] = {}
        self._syncing = False
        self.status_now = Status("ready", "")
        self.problem: Problem | None = None
        self.setup_passed = not self.first_run  # Setup finished once: no "New here?" banner on the Dashboard
        self.check_passed = False  # step 2 of Setup passed this session
        self._found: dict[str, bool] = {}
        self.auto_village: str | None = None  # Auto switch: the village it moved on to this run
        self._verdict_kind = "wait"
        self._add_page(self._home(), FIF.SPEED_HIGH, "Dashboard")
        self._add_page(self._home_village(), FIF.HOME, "Home village")
        self._add_page(self._builder_base(), FIF.GAME, "Builder base")
        self._add_page(self._when_to_play(), FIF.DATE_TIME, "When to play")
        self._add_page(self._history(), FIF.HISTORY, "History")
        self._add_page(self._setup(), FIF.CHECKBOX if hasattr(FIF, "CHECKBOX") else FIF.ACCEPT, "Setup",
                       NavigationItemPosition.BOTTOM)
        self._add_page(self._advanced(), FIF.SETTING, "Advanced", NavigationItemPosition.BOTTOM)
        self._find_bluestacks()  # fills both Setup and Advanced
        self.navigationInterface.expand(useAni=False)

        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._autosave)
        self._load()
        for binding in self.bindings.values():
            binding.connect(lambda *_: self._changed())
        self._changed(save=False)
        self._show_status(Status("ready", "Welcome! Start with Setup", "Four quick steps, then press Start.")
                          if self.first_run else
                          Status("ready", "Ready to farm", "Press Start (F5). Check the village pages first if you "
                                 "want to change how it plays."))

        self.log_handler = _QueueLogHandler(self.events)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)
        self.poll_timer = QTimer(self, interval=100, timeout=self._poll)
        self.poll_timer.start()
        self.clock_timer = QTimer(self, interval=1000, timeout=self._tick)
        self.clock_timer.start()
        QShortcut(QKeySequence("F5"), self, activated=self.start)
        if self.first_run:
            QTimer.singleShot(300, self._welcome)
        self.update_timer = QTimer(self, interval=600_000, timeout=self._check_for_update)  # GitHub is asked hourly at most
        self.update_timer.start()
        QTimer.singleShot(1500, self._after_update)
        self._check_for_update()
        QShortcut(QKeySequence("Esc"), self, activated=self.stop)
        for i, name in enumerate(PAGES, start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, activated=lambda n=name: self.show_page(n))
        qconfig.themeChanged.connect(self._theme_signal)
        QApplication.instance().installEventFilter(self)  # Enter presses the focused button

    def eventFilter(self, obj, event) -> bool:
        if (event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and obj is QApplication.focusWidget() and isinstance(obj, QAbstractButton) and obj.isEnabled()
                and obj.window() is self):
            obj.animateClick()
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------ chrome
    TITLE_ICON = 28  # px; Fluent's own title bar icon is 18, too small for a detailed emblem

    def _sharp_title_icon(self) -> None:
        """Draw the title bar logo larger, scaled smoothly at the screen's
        pixel density, so the emblem stays crisp."""
        label = getattr(self.titleBar, "iconLabel", None)
        if label is None:
            return
        ratio = self.devicePixelRatioF()
        side = round(self.TITLE_ICON * ratio)
        pixmap = QPixmap(str(LOGO)).scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        label.setFixedSize(self.TITLE_ICON, self.TITLE_ICON)
        label.setPixmap(pixmap)

    def _build_title_bar(self) -> None:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(10)
        self.state_chip = QLabel("Idle")
        self.state_chip.setAlignment(Qt.AlignCenter)
        self.state_chip.setMinimumWidth(84)
        self.status_label = CaptionLabel("")
        self.status_label.setMinimumWidth(320)
        self.save_label = CaptionLabel("")
        self.start_button = PrimaryPushButton(FIF.PLAY, "Start")
        self.start_button.setToolTip("Start farming (F5)")
        self.start_button.clicked.connect(self.start)
        self.stop_button = PushButton(FIF.PAUSE, "Pause")
        self.stop_button.clicked.connect(self.stop)
        self._show_pause_button()
        self.stop_button.setEnabled(False)
        for w in (self.state_chip, self.status_label, self.save_label, self.start_button, self.stop_button):
            layout.addWidget(w)
        self.titleBar.hBoxLayout.insertWidget(3, bar, 0, Qt.AlignVCenter)

    def _add_page(self, page: QWidget, icon, text: str, position=NavigationItemPosition.TOP,
                  key: str | None = None) -> None:
        self.pages[key or text] = page
        self.addSubInterface(page, icon, text, position)

    def show_page(self, name: str, tab: str | None = None) -> None:
        page = self.pages[name]
        if tab is not None:
            page.show_tab(tab)
        self.switchTo(page)

    # ------------------------------------------------------------ helpers
    def _bind(self, path: str, binding: Binding) -> Binding:
        self.bindings[path] = binding
        return binding

    def _card(self, group: SettingCardGroup, path: str | None, icon, title: str, help_text: str,
              control: QWidget | None = None, *extra: QWidget) -> Card:
        card = Card(icon, title, help_text, control, *extra)
        group.addSettingCard(card)
        if path:
            self.cards[path] = card
        return card

    def switch(self, group, path: str, icon, title: str, help_text: str = "") -> Card:
        w = SwitchButton()
        self._bind(path, bind_switch(w))
        return self._card(group, path, icon, title, help_text, w)

    def amount(self, group, path: str, icon, title: str, help_text: str = "") -> Card:
        w = LineEdit()
        w.setFixedWidth(170)
        w.setAlignment(Qt.AlignRight)
        w.setPlaceholderText("e.g. 1.5m")
        self._bind(path, bind_line(w))
        return self._card(group, path, icon, title, help_text, w)

    def number(self, group, path: str, icon, title: str, help_text: str = "", suffix: str = "") -> Card:
        # A text field rather than a spin box: Fluent's SpinBox draws an
        # empty box with current PySide6 releases. The range is checked by
        # the form, and shown as a placeholder.
        field = BY_PATH[path]
        w = LineEdit()
        w.setFixedWidth(110)
        w.setAlignment(Qt.AlignRight)
        w.setPlaceholderText(f"{field.low:g}–{field.high:g}")
        self._bind(path, bind_line(w))
        extra = [_muted(BodyLabel(suffix))] if suffix else []
        return self._card(group, path, icon, title, help_text, w, *extra)

    def number_range(self, group, low: str, high: str, icon, title: str, help_text: str = "",
                     suffix: str = "") -> Card:
        """Two numbers on one row: ``title [low] to [high] suffix``."""
        edits = []
        for path in (low, high):
            field = BY_PATH[path]
            w = LineEdit()
            w.setFixedWidth(70)
            w.setAlignment(Qt.AlignRight)
            w.setPlaceholderText(f"{field.low:g}–{field.high:g}")
            self._bind(path, bind_line(w))
            edits.append(w)
        extra = [_muted(BodyLabel("to")), edits[1]] + ([_muted(BodyLabel(suffix))] if suffix else [])
        card = self._card(group, low, icon, title, help_text, edits[0], *extra)
        self.cards[high] = card
        return card

    def text(self, group, path: str, icon, title: str, help_text: str = "", width: int = 240) -> Card:
        w = LineEdit()
        w.setFixedWidth(width)
        self._bind(path, bind_line(w))
        return self._card(group, path, icon, title, help_text, w)

    def speed(self, group, path: str, icon, title: str, help_text: str) -> Card:
        slider = Slider(Qt.Horizontal)
        slider.setRange(1, 9)
        slider.setFixedWidth(220)
        value = StrongBodyLabel("")
        value.setFixedWidth(20)
        slider.valueChanged.connect(lambda v: value.setText(str(v)))
        slow, fast = _muted(CaptionLabel("Slow")), _muted(CaptionLabel("Fast"))
        self._bind(path, Binding(slider.value, lambda v: slider.setValue(int(v)), slider.valueChanged.connect, [slider]))
        return self._card(group, path, icon, title, help_text, slow, slider, fast, value)

    def segments(self, group, path: str, icon, title: str, help_text: str, keys: list[str] | dict[str, str],
                 width: int = 60) -> Card:
        w = SegmentedWidget()
        w.setFixedWidth(width * len(keys))
        self._bind(path, bind_segments(w, keys))
        return self._card(group, path, icon, title, help_text, w)

    def depends(self, controller: str, *paths: str, when: Any = True) -> None:
        """Enable the controls of ``paths`` only while ``controller`` is on
        (or, with ``when``, equals that value)."""
        self.dependents.append((controller, [self.bindings[p] for p in paths], when))

    def _template_note(self, *features: str) -> str:
        missing = [p.title for p in catalog.missing(self.library()) if p.feature in features]
        if not missing:
            return "All the pictures these features need are in place."
        return "Missing pictures (add them under Advanced → Pictures): " + ", ".join(missing)

    def library(self) -> TemplateLibrary:
        return TemplateLibrary(self.config.bot.templates)

    def _note(self, page: Page, text: str, colour: str = WARN) -> None:
        card = SimpleCardWidget()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        label = BodyLabel(text)
        label.setWordWrap(True)
        tint(label, colour)
        layout.addWidget(label)
        page.add(card)

    # ------------------------------------------------------------ dashboard
    def _stat(self, title: str, colour: str | None = None, big: bool = True) -> tuple[ElevatedCardWidget, QLabel, QLabel]:
        card = ElevatedCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(4)
        head = StrongBodyLabel(title)
        head.setWordWrap(True)
        if colour:
            tint(head, colour)
        value = TitleLabel("—") if big else SubtitleLabel("0")
        note = _muted(CaptionLabel(""))
        note.setWordWrap(True)  # stat tiles share a row: wrap rather than widen the page
        for label in (head, value, note):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        card.setMinimumWidth(120)
        layout.addWidget(head)
        layout.addWidget(value)
        layout.addWidget(note)
        return card, value, note

    def _gained_tile(self) -> ElevatedCardWidget:
        """One tile, three parts: the gold, elixir and dark elixir gained."""
        card = ElevatedCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(4)
        layout.addWidget(StrongBodyLabel("Loot gained this session"))
        row = QHBoxLayout()
        row.setSpacing(24)
        self.gained_value: dict[str, QLabel] = {}
        self.gained_today: dict[str, QLabel] = {}
        for key, name in (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir")):
            column = QVBoxLayout()
            column.setSpacing(0)
            label = CaptionLabel(name)
            tint(label, RESOURCE_COLOURS[key])
            value = SubtitleLabel("0")
            today = _muted(CaptionLabel(""))
            column.addWidget(label)
            column.addWidget(value)
            column.addWidget(today)
            row.addLayout(column)
            self.gained_value[key] = value
            self.gained_today[key] = today
        row.addStretch(1)
        layout.addLayout(row)
        self.gained_note = _muted(CaptionLabel(""))
        self.gained_note.setWordWrap(True)
        self.gained_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.gained_note)
        return card

    def _home(self) -> Page:
        page = Page("home", "Dashboard", "What COCFARM is doing right now, and what it has earned.")
        self.setup_banner = SimpleCardWidget()
        row = QHBoxLayout(self.setup_banner)
        row.setContentsMargins(20, 14, 16, 14)
        text = BodyLabel("New here? Setup takes a couple of minutes: pick your emulator, check the game, choose "
                         "the loot you want, and press Start.")
        text.setWordWrap(True)
        row.addWidget(text, 1)
        go = PrimaryPushButton(FIF.RIGHT_ARROW if hasattr(FIF, "RIGHT_ARROW") else FIF.PLAY, "Open Setup")
        go.clicked.connect(lambda: self.show_page("Setup"))
        row.addWidget(go)
        page.add(self.setup_banner)
        page.add(self._status_panel())

        farm = self.farm_group = page.add(SettingCardGroup("What to farm"))
        self.segments(farm, "bot.mode", FIF.GAME, "Village",
                      "Which village the bot plays; its settings are on the page with the same name. Auto switch: "
                      "one village until its storages are full, then the other.",
                      {"home": "Home village", "builder": "Builder base", "auto": "Auto switch"}, width=120)
        self.segments(farm, "bot.auto_first", FIF.SYNC, "Start with",
                      "Auto switch farms this village first, then the other one once the storages you picked on "
                      "When to play are full. It stops when both are full.",
                      {"home": "Home village", "builder": "Builder base"}, width=130)
        self.depends("bot.mode", "bot.auto_first", when="auto")
        # The builder panel is shorter than the page: keep this group from stretching into the gap.
        farm.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Home village panel; a builder base panel replaces it in builder mode.
        self.home_panel = QWidget()
        home = QVBoxLayout(self.home_panel)
        home.setContentsMargins(0, 0, 0, 0)
        home.setSpacing(12)
        page.add(self.home_panel)
        self.builder_panel = self._builder_dashboard()
        page.add(self.builder_panel)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("Base being scouted"))
        header.addStretch(1)
        self.total_note = _muted(CaptionLabel(""))
        header.addWidget(self.total_note)
        self.verdict = QLabel("")
        self.verdict.setAlignment(Qt.AlignCenter)
        self.verdict.setMinimumWidth(150)
        self._verdict("wait")
        header.addWidget(self.verdict)
        home.addLayout(header)

        loot = QGridLayout()
        loot.setSpacing(16)
        self.loot_value: dict[str, QLabel] = {}
        self.loot_note: dict[str, QLabel] = {}
        for col, (key, name) in enumerate((("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir"))):
            card, value, note = self._stat(name, RESOURCE_COLOURS[key])
            self.loot_value[key], self.loot_note[key] = value, note
            loot.addWidget(card, 0, col)
        home.addLayout(loot)
        self.live_view = QLabel()
        self.live_view.setAlignment(Qt.AlignCenter)
        self.live_view.setToolTip("The last base the bot looked at. Boxes show where it reads the loot.")
        self.live_view.hide()  # until the first base is scouted
        home.addWidget(self.live_view)

        session_head = QHBoxLayout()
        session_head.addWidget(SubtitleLabel("This session"))
        session_head.addStretch(1)
        self.resources_label = _muted(CaptionLabel(""))
        session_head.addWidget(self.resources_label)
        home.addLayout(session_head)
        session = QGridLayout()
        session.setSpacing(16)
        self.stat_value: dict[str, QLabel] = {}
        self.stat_note: dict[str, QLabel] = {}
        for key, name, col in (("bases", "Bases looked at", 0), ("attacks", "Attacks", 1), ("runtime", "Running for", 4)):
            card, value, note = self._stat(name, big=False)
            self.stat_value[key], self.stat_note[key] = value, note
            session.addWidget(card, 0, col)
        session.addWidget(self._gained_tile(), 0, 2, 1, 2)
        self.stat_value["runtime"].setText("—")
        self._show_today()
        home.addLayout(session)
        last, self.last_attack, self.last_attack_note = self._stat("Last attack", big=False)
        self.last_attack.setText("—")
        self.last_attack_note.setText("What the base offered, how much was taken, and what you gained.")
        home.addWidget(last)

        details = QHBoxLayout()
        details.addWidget(SubtitleLabel("Details"))
        details.addStretch(1)
        self.details_button = PushButton(FIF.VIEW, "Show the step-by-step log")
        self.details_button.setToolTip("Every step the bot takes, with times: useful when something goes wrong")
        self.details_button.clicked.connect(lambda: self._show_details(self.log_view.isHidden()))
        clear = PushButton(FIF.DELETE, "Clear")
        clear.clicked.connect(lambda: self.log_view.clear())
        self.clear_log_button = clear
        details.addWidget(self.details_button)
        details.addWidget(clear)
        page.layout_.addLayout(details)
        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        self.log_view.setPlaceholderText("Nothing yet. Press Start (F5) and each step will appear here.")
        page.add(self.log_view)
        self._show_details(False)
        return page

    def _show_details(self, on: bool) -> None:
        self.log_view.setVisible(on)
        self.clear_log_button.setVisible(on)
        self.details_button.setText("Hide the log" if on else "Show the step-by-step log")

    def _status_panel(self) -> QWidget:
        """The big "what's happening" panel: readable from across the room."""
        card = ElevatedCardWidget()
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)  # as tall as its text, never stretched
        layout = QHBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(20)
        self.big_state = QLabel("")
        self.big_state.setAlignment(Qt.AlignCenter)
        self.big_state.setMinimumWidth(170)
        layout.addWidget(self.big_state, 0, Qt.AlignVCenter)  # everything centred in the tile's height
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        self.big_headline = SubtitleLabel("")
        self.big_headline.setWordWrap(True)
        self.big_detail = BodyLabel("")
        self.big_detail.setWordWrap(True)
        self.big_trouble = BodyLabel("")
        self.big_trouble.setWordWrap(True)
        tint(self.big_trouble, WARN)
        self.big_trouble.hide()
        for w in (self.big_headline, self.big_detail, self.big_trouble):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            text.addWidget(w)
        text.addStretch(1)
        layout.addLayout(text, 1)
        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.big_primary = PrimaryPushButton("")
        self.big_primary.setMinimumWidth(150)
        self.big_primary.clicked.connect(self._big_primary_clicked)
        self.big_secondary = PushButton("")
        self.big_secondary.setMinimumWidth(150)
        self.big_secondary.clicked.connect(self._big_secondary_clicked)
        buttons.addWidget(self.big_primary)
        buttons.addWidget(self.big_secondary)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card


    # ------------------------------------------------------------ history
    HISTORY_DAYS = 14  # days in the chart
    HISTORY_ROWS = 300  # latest attacks in the table
    # What the chart shows: label -> resources drawn.
    HISTORY_VIEWS = {"All loot": ("gold", "elixir", "dark"), "Gold only": ("gold",),
                     "Elixir only": ("elixir",), "Dark elixir only": ("dark",)}

    def _history(self) -> Page:
        page = Page("history", "History", "Every attack COCFARM has made: totals, loot per hour, the last two "
                    "weeks, and each attack.")
        top = QHBoxLayout()
        top.addStretch(1)
        refresh = PushButton(FIF.SYNC, "Refresh")
        refresh.clicked.connect(self._show_history)
        open_file = PushButton(FIF.DOCUMENT, "Open in a spreadsheet")
        open_file.setToolTip("Opens every attack in your spreadsheet app, e.g. Excel")
        open_file.clicked.connect(self._open_history_file)
        top.addWidget(refresh)
        top.addWidget(open_file)
        page.layout_.addLayout(top)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.history_value: dict[str, QLabel] = {}
        self.history_note: dict[str, QLabel] = {}
        for col, (key, name) in enumerate((("today", "Today"), ("week", "Last 7 days"), ("all", "All time"),
                                           ("hour", "Per hour of play"))):
            card, value, note = self._stat(name, big=False)
            self.history_value[key], self.history_note[key] = value, note
            grid.addWidget(card, 0, col)
        page.layout_.addLayout(grid)

        from PySide6.QtCharts import QChart, QChartView
        from PySide6.QtGui import QPainter

        self.history_chart = QChart()
        self.history_chart.legend().setAlignment(Qt.AlignBottom)
        self.history_chart.setTitle(f"Loot gained per day (last {self.HISTORY_DAYS} days)")
        view = QChartView(self.history_chart)
        view.setRenderHint(QPainter.Antialiasing)
        view.setMinimumHeight(300)
        view.setStyleSheet("background: transparent;")
        show = QHBoxLayout()
        show.addWidget(BodyLabel("Show"))
        self.history_view = ComboBox()
        self.history_view.addItems(list(self.HISTORY_VIEWS))
        self.history_view.setMinimumWidth(170)
        self.history_view.currentTextChanged.connect(lambda *_: self._show_history())
        show.addWidget(self.history_view)
        show.addStretch(1)
        page.layout_.addLayout(show)
        page.add(view)

        from qfluentwidgets import TableWidget

        self.history_table = TableWidget()
        headers = ["Time", "Offered gold", "Offered elixir", "Offered dark", "Gained gold", "Gained elixir",
                   "Gained dark"]
        self.history_table.setColumnCount(len(headers))
        self.history_table.setHorizontalHeaderLabels(headers)
        self.history_table.verticalHeader().hide()
        self.history_table.setEditTriggers(TableWidget.NoEditTriggers)
        self.history_table.setMinimumHeight(360)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        page.add(self.history_table)
        self._show_history()
        return page

    def _show_history(self) -> None:
        from datetime import date, timedelta

        from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QBarSet, QValueAxis
        from PySide6.QtWidgets import QTableWidgetItem

        from farmbot import history
        from farmbot.tasks.search import Loot

        path = self.config.bot.history
        rows = history.read_all(path) if path else []
        days = history.daily_totals(rows)
        today = date.today()

        def total(since: date | None) -> tuple[int, Loot]:
            count, sum_ = 0, Loot(0, 0, 0)
            for day, (n, loot) in days.items():
                if since is None or day >= since:
                    count, sum_ = count + n, history.add(sum_, loot)
            return count, sum_

        def loot_text(loot: Loot) -> str:
            return f"gold {_short(loot.gold)} · elixir {_short(loot.elixir)} · dark {_short(loot.dark)}"

        for key, since in (("today", today), ("week", today - timedelta(days=6)), ("all", None)):
            count, loot = total(since)
            self.history_value[key].setText(f"{count:,} attacks")
            self.history_note[key].setText(loot_text(loot) if count else "Nothing yet")
        recent = [r for r in rows if r.time.date() >= today - timedelta(days=6)]
        hourly = history.per_hour(recent)
        self.history_value["hour"].setText(_short(hourly.gold + hourly.elixir) if hourly else "—")
        self.history_note["hour"].setText(loot_text(hourly) + " (last 7 days)" if hourly
                                          else "Needs a few attacks in a row")

        from PySide6.QtCharts import QChart

        chart = self.history_chart
        chart.setTheme(QChart.ChartThemeDark if isDarkTheme() else QChart.ChartThemeLight)
        chart.setBackgroundVisible(False)
        chart.removeAllSeries()
        for axis in chart.axes():
            chart.removeAxis(axis)
        span = [today - timedelta(days=i) for i in range(self.HISTORY_DAYS - 1, -1, -1)]
        keys = self.HISTORY_VIEWS.get(self.history_view.currentText(), self.HISTORY_VIEWS["All loot"])
        names = {"gold": "Gold", "elixir": "Elixir", "dark": "Dark elixir"}
        values = {k: [max(getattr(days.get(day, (0, Loot(0, 0, 0)))[1], k), 0) for day in span] for k in keys}
        peak = max((v for vs in values.values() for v in vs), default=0)
        # Millions, or thousands when the bars are small (dark elixir on its own).
        scale, unit = (1_000_000, "Millions") if peak >= 1_000_000 or peak == 0 else (1_000, "Thousands")
        series = QBarSeries()
        for key in keys:
            bars = QBarSet(names[key])
            bars.setColor(QColor(str(RESOURCE_COLOURS[key])))
            for value in values[key]:
                bars.append(value / scale)
            series.append(bars)
        top = peak / scale

        def hovered(on: bool, index: int, bars) -> None:
            # The real amount under the mouse, not the rounded axis.
            from PySide6.QtGui import QCursor
            from PySide6.QtWidgets import QToolTip

            if not on:
                QToolTip.hideText()
                return
            key = next(k for k in keys if names[k] == bars.label())
            QToolTip.showText(QCursor.pos(), f"{span[index]:%a %d %b}\n{names[key]}: {values[key][index]:,}")

        series.hovered.connect(hovered)
        chart.addSeries(series)
        x = QBarCategoryAxis()
        x.append([str(d.day) for d in span])  # day of the month: fits at the window's smallest
        x.setTitleText(f"{span[0]:%b %d} – {span[-1]:%b %d}")
        y = QValueAxis()
        y.setRange(0, max(top, 0.1) * 1.1)
        y.setLabelFormat("%.1f" if scale == 1_000_000 else "%.0f")
        y.setTitleText(unit)
        chart.addAxis(x, Qt.AlignBottom)
        chart.addAxis(y, Qt.AlignLeft)
        series.attachAxis(x)
        series.attachAxis(y)

        table = self.history_table
        latest = rows[::-1][:self.HISTORY_ROWS]
        table.setRowCount(len(latest))
        for r, row in enumerate(latest):
            gained = row.gained
            cells = [f"{row.time:%Y-%m-%d %H:%M}", f"{row.target.gold:,}", f"{row.target.elixir:,}",
                     f"{row.target.dark:,}"] + ([f"{gained.gold:+,}", f"{gained.elixir:+,}", f"{gained.dark:+,}"]
                                                if gained else ["?", "?", "?"])
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()

    def _open_history_file(self) -> None:
        import os

        path = Path(self.config.bot.history) if self.config.bot.history else None
        if not path or not path.is_file():
            InfoBar.warning("No history yet", "The file appears after the first attack.", duration=4000,
                            position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
            return
        os.startfile(str(path.resolve()))  # Windows: opens with the default app for .csv

    def _builder_dashboard(self) -> QWidget:
        """Builder base mode: the current battle, this session's builder loot,
        the last battle, the elixir cart and walls."""
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("Builder battle"))
        header.addStretch(1)
        self.bb_stage = QLabel("")
        self.bb_stage.setAlignment(Qt.AlignCenter)
        self.bb_stage.setMinimumWidth(150)
        header.addWidget(self.bb_stage)
        box.addLayout(header)
        self._bb_stage("Waiting")

        grid = QGridLayout()
        grid.setSpacing(16)
        self.bb_value: dict[str, QLabel] = {}
        self.bb_note: dict[str, QLabel] = {}
        for col, (key, name, colour) in enumerate((("gold", "Builder gold", RESOURCE_COLOURS["gold"]),
                                                   ("elixir", "Builder elixir", RESOURCE_COLOURS["elixir"]),
                                                   ("battles", "Battles", None), ("runtime", "Running for", None))):
            card, value, note = self._stat(name, colour, big=False)
            self.bb_value[key], self.bb_note[key] = value, note
            grid.addWidget(card, 0, col)
        for key, name, col, span in (("last", "Last battle", 0, 2), ("cart", "Elixir cart", 2, 1),
                                     ("walls", "Walls upgraded", 3, 1)):
            card, value, note = self._stat(name, big=False)
            self.bb_value[key], self.bb_note[key] = value, note
            grid.addWidget(card, 1, col, 1, span)
        box.addLayout(grid)
        self.bb_value["runtime"].setText("—")
        self.bb_session = {"gold": 0, "elixir": 0, "battles": 0, "second": 0, "walls": 0}
        self._bb_show({})
        self.bb_view = QLabel()
        self.bb_view.setAlignment(Qt.AlignCenter)
        self.bb_view.setToolTip("The result screen of the last builder battle.")
        self.bb_view.hide()
        box.addWidget(self.bb_view)
        return panel

    def _bb_stage(self, text: str) -> None:
        colours = {"Waiting": (MUTED, SUBTLE), "First base": ("#ffffff", "#c2410c"),
                   "Second base": ("#0b2e13", GOOD), "Done": ("#0b2e13", GOOD)}
        fg, bg = colours.get(text, (MUTED, SUBTLE))
        self.bb_stage.setText(text)
        tint(self.bb_stage, fg, f"background: {bg}; border-radius: 14px; padding: 6px 18px; font-weight: 700;")

    def _bb_show(self, battle: dict) -> None:
        """Update the builder panel; ``battle`` is the Bot's report on one battle (or empty)."""
        s = self.bb_session
        if battle:
            s["battles"] += 1
            s["second"] += battle.get("stages", 0) >= 2
            s["walls"] += battle.get("walls", 0)
            gained = battle.get("gained")
            if gained:
                s["gold"] += gained.gold
                s["elixir"] += gained.elixir
        for key in ("gold", "elixir"):
            self.bb_value[key].setText(_short(s[key]) if s["battles"] else "—")
            self.bb_note[key].setText("Gained this session")
        self.bb_value["battles"].setText(f"{s['battles']:,}")
        self.bb_note["battles"].setText(f"Second base reached {s['second']}×" if s["battles"] else "")
        if battle:
            mins, secs = divmod(int(battle.get("seconds", 0)), 60)
            gained = battle.get("gained")
            self.bb_value["last"].setText(("Both bases" if battle.get("stages", 0) >= 2 else "First base only")
                                          + f"  ·  {mins}m {secs:02d}s")
            self.bb_note["last"].setText(f"Gained gold {gained.gold:+,} · elixir {gained.elixir:+,}" if gained
                                         else "Gain unknown: the resource bar couldn't be read.")
            if battle.get("screen") is not None:
                self.bb_view.setPixmap(to_pixmap(battle["screen"]).scaled(760, 320, Qt.KeepAspectRatio,
                                                                          Qt.SmoothTransformation))
                self.bb_view.show()
            cart_in = battle.get("cart_in")
            self.bb_value["cart"].setText("Collected" if battle.get("cart") else
                                          f"In {cart_in} battle{'s' if cart_in != 1 else ''}" if cart_in is not None else "Off")
        else:
            self.bb_value["last"].setText("—")
            self.bb_note["last"].setText("How far it got, how long it took, and what it brought in.")
            self.bb_value["cart"].setText("—" if self.config.builder.collect_cart else "Off")
        self.bb_note["cart"].setText(f"Every {self.config.builder.cart_every_min}"
                                     + (f"-{self.config.builder.cart_every_max}"
                                        if self.config.builder.cart_every_max != self.config.builder.cart_every_min else "")
                                     + " battles" if self.config.builder.collect_cart else "Turn it on: Builder base → Village")
        self.bb_value["walls"].setText(f"{s['walls']:,}")
        self.bb_note["walls"].setText("This session" if self.config.builder.walls else "Off (Builder base → Village)")

    # ------------------------------------------------------------ settings pages
    def _home_village(self) -> TabbedPage:
        page = TabbedPage("home_village", "Home village", "Everything for farming the home village: which bases to "
                          "attack, how to attack them, and the chores in between.")
        self._bases_tab(page.tab("bases", "Bases to attack", "Amounts accept 500k, 1.5m or 1,500,000."))
        self._attacking_tab(page.tab("attacking", "Attacking"))
        self._village_tab(page.tab("village", "Village"))
        self._clan_tab(page.tab("clan", "Clan"))
        return page

    def _bases_tab(self, page: Page) -> None:
        top = page.add(SettingCardGroup("Attacking"))
        w = SwitchButton()
        bind_switch(w)  # On/Off labels
        # Stored the other way round: "only look after the village" = not attacking.
        self._bind("search.maintain_only", Binding(lambda: not w.isChecked(), lambda v: w.setChecked(not v),
                                                   w.checkedChanged.connect, [w]))
        self._card(top, "search.maintain_only", FIF.GAME, "Attack bases",
                   "Off: it only collects, donates and looks after the village, and never attacks.", w)
        loot = page.add(SettingCardGroup("Loot a base must have"))
        self.amount(loot, "search.min_gold", FIF.MARKET, "Gold", "Bases offering less gold are skipped. 0 ignores gold.")
        self.amount(loot, "search.min_elixir", FIF.MARKET, "Elixir",
                    "Bases offering less elixir are skipped. 0 ignores elixir.")
        self.amount(loot, "search.min_dark", FIF.MARKET, "Dark elixir",
                    "Bases offering less dark elixir are skipped. 0 ignores dark elixir.")
        self.switch(loot, "search.match_any", FIF.FILTER, "Any one is enough",
                    "On: attack when any one amount above is met. Off: all of them must be met.")
        more = page.add(SettingCardGroup("While searching"))
        self.amount(more, "search.min_own_gold", FIF.SHOPPING_CART, "Don't search while my gold is below",
                    "Each Next costs gold. 0 always searches.")
        self.switch(more, "search.sound_on_match", FIF.RINGER, "Sound on match",
                    "Beeps when it finds a base worth attacking.")
        self.depends("search.maintain_only", "search.min_gold", "search.min_elixir", "search.min_dark",
                     "search.match_any", "search.min_own_gold",
                     "search.sound_on_match", when=False)

    def _attacking_tab(self, page: Page) -> None:
        deploy = page.add(SettingCardGroup("Deploying"))
        grid = QWidget()
        g = QGridLayout(grid)
        g.setContentsMargins(0, 6, 0, 6)
        g.setSpacing(6)
        buttons = {}
        for side, (r, c, label) in {"nw": (0, 0, "Top left"), "ne": (0, 1, "Top right"),
                                    "sw": (1, 0, "Bottom left"), "se": (1, 1, "Bottom right")}.items():
            b = TogglePushButton(label)
            b.setFixedWidth(128)
            g.addWidget(b, r, c)
            buttons[side] = b
        self._bind("attack.sides", bind_toggles(buttons))
        card = self._card(deploy, "attack.sides", FIF.ZOOM, "Deploy from",
                          "Laid out like the map. Troops spread over the sides you turn on.", grid)
        card.setFixedHeight(110)
        self.segments(deploy, "attack.waves", FIF.TILES, "Waves", "Split each troop type into this many waves.",
                      ["1", "2", "3"])
        self.speed(deploy, "attack.troop_speed", FIF.SPEED_HIGH, "Troop speed", "How quickly single troops go down.")
        self.speed(deploy, "attack.wave_speed", FIF.SPEED_MEDIUM, "Wave speed", "How quickly one wave follows another.")
        self.segments(deploy, "attack.spells", FIF.ALBUM, "Spells to cast",
                      "Bat Spells only: other spells stay in the bar. All: every spell.",
                      {"bat": "Bat Spells only", "all": "All spells"}, width=120)
        self.segments(deploy, "attack.spell_placement", FIF.ALBUM, "Spells go",
                      "One spot: every spell at once, on one spot. Spread: over the base.",
                      {"together": "One spot", "spread": "Spread"}, width=90)
        self.segments(deploy, "attack.spell_spot", FIF.ZOOM, "The spot",
                      "Middle: the centre of the base. Random: a new spot each battle, within the middle half "
                      "of the base (never out in a corner).",
                      {"middle": "Middle", "random": "Random"}, width=90)
        self.depends("attack.spell_placement", "attack.spell_spot", when="together")

        heroes = page.add(SettingCardGroup("Heroes and siege machines"))
        self.switch(heroes, "attack.use_siege", FIF.CAR, "Use siege machines",
                    "Off: siege machines stay in the bar. Your Clan Castle troops ride inside the siege machine and "
                    "come out when it's destroyed, so this also decides whether they're used.")
        self.switch(heroes, "attack.hero_abilities", FIF.CALORIES, "Use hero abilities",
                    "Fires each hero's special ability once during the battle.")
        self.number(heroes, "attack.hero_ability_delay", FIF.STOP_WATCH, "Ability after",
                    "Seconds after the heroes land.", "s")
        self.number(heroes, "attack.ability_health", FIF.HEART, "Or when health drops to",
                    "Fires a hero's ability once it's this hurt instead of after the time above. 0% uses the time.", "%")
        self.number(heroes, "attack.ability_max_wait", FIF.HISTORY, "At the latest after",
                    "Heroes still unhurt by then fire anyway.", "s")
        self.depends("attack.hero_abilities", "attack.hero_ability_delay", "attack.ability_health",
                     "attack.ability_max_wait")

        end = page.add(SettingCardGroup("Ending the battle"))
        self.switch(end, "attack.speed_up", FIF.SPEED_HIGH, "Play the last minute at 4x",
                    "Presses the 1x button when it shows, with a minute of the battle left.")
        self.switch(end, "attack.end_when_no_loot", FIF.CANCEL, "Leave when the loot stops",
                    "Ends the battle early once no more loot is coming in.")
        self.number(end, "attack.no_loot_seconds", FIF.STOP_WATCH, "No loot for",
                    "Seconds without any loot before leaving.", "s")
        self.depends("attack.end_when_no_loot", "attack.no_loot_seconds")
        self.number(end, "attack.end_at_loot_share", FIF.ACCEPT, "Leave once this much loot is taken",
                    "Share of the base's loot to take before leaving. 0 waits for the rules above.", "%")

    def _village_tab(self, page: Page) -> None:
        village = page.add(SettingCardGroup("Collecting"))
        self.switch(village, "bot.collect_resources", FIF.BASKETBALL, "Collect resources",
                    "Empties full collectors between attacks: one tap on a bubble collects them all.")
        self.switch(village, "bot.collect_gold", FIF.MARKET, "Gold", "From the gold mines.")
        self.switch(village, "bot.collect_elixir", FIF.MARKET, "Elixir", "From the elixir collectors.")
        self.switch(village, "bot.collect_dark", FIF.MARKET, "Dark elixir", "From the dark elixir drills.")
        self.depends("bot.collect_resources", "bot.collect_gold", "bot.collect_elixir", "bot.collect_dark")
        self.switch(village, "bot.collect_loot_cart", FIF.SHOPPING_CART, "Collect the loot cart",
                    "Empties the loot cart when it shows \"Collect\".")
        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "walls.enabled", FIF.UP, "Upgrade walls with spare loot",
                    "Between attacks it upgrades a wall from the Build list. Off by default: it spends loot.")
        self.segments(walls, "walls.pick", FIF.SCROLL, "Which wall",
                      "Cheapest: scrolls down the list to the last wall. First: the first wall the list shows "
                      "(the dearest).", {"cheapest": "Cheapest", "first": "First"}, width=90)
        self.segments(walls, "walls.use", FIF.MARKET, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.amount(walls, "walls.min_gold", FIF.SAVE, "Keep at least this much gold", "Walls only get what's above it.")
        self.amount(walls, "walls.min_elixir", FIF.SAVE, "Keep at least this much elixir",
                    "Walls only get what's above it.")
        self.number(walls, "walls.max_per_round", FIF.TILES, "Walls per round", "At most this many between two attacks.")
        self.depends("walls.enabled", "walls.pick", "walls.use", "walls.min_gold", "walls.min_elixir", "walls.max_per_round")
        self._note(page, self._template_note("Collecting resources", "Collecting the loot cart", "Upgrading walls"))

    def _clan_tab(self, page: Page) -> None:
        from farmbot.units import SIEGE_MACHINES, SPELLS, TROOPS

        donate = page.add(SettingCardGroup("Donations"))
        self.switch(donate, "clan.donate", FIF.HEART, "Donate to requests",
                    "Fills every open request in clan chat. Each unit costs the elixir or dark elixir shown in the "
                    "donate window; it never pays with gems.")
        lists = []
        for path, title, hint, units in (
                ("clan.donate_main", "Main troops",
                 "Click the ones to give first, in order: they move to the top and go first, as many as fit. "
                 "Drag to change the order.",
                 TROOPS),
                ("clan.donate_siege", "Siege machines",
                 "Click the siege machines to give, in order. A request has room for one.", SIEGE_MACHINES),
                ("clan.donate_spells", "Main spells", "Click the spells to give, in order.", SPELLS)):
            box = SimpleCardWidget()
            box.setFixedHeight(300)
            column = QVBoxLayout(box)
            column.setContentsMargins(16, 12, 16, 12)
            column.addWidget(StrongBodyLabel(title))
            column.addWidget(_muted(_wrapping(CaptionLabel(hint))))
            picker = ListWidget()
            picker.setDragDropMode(ListWidget.InternalMove)
            column.addWidget(picker, 1)
            self._bind(path, bind_unit_picker(picker, units, self._has_unit_picture))
            donate.addSettingCard(box)
            lists.append(path)
        self.switch(donate, "clan.donate_fill", FIF.ADD, "Fill the rest",
                    "After the main troops, fills the space left with the biggest troops that still fit.")
        self.number(donate, "clan.max_donations_per_request", FIF.ADD, "Up to", "Units given to one request at most.",
                    "units")
        # The lists stay usable with donating off, so the choice can be made first.
        self.depends("clan.donate", "clan.donate_fill", "clan.max_donations_per_request")

        request = page.add(SettingCardGroup("Requests"))
        self.switch(request, "clan.request_troops", FIF.SEND, "Request Clan Castle troops",
                    "Asks the clan for troops, then again as soon as the game lets you.")
        self._note(page, self._template_note("Donating", "Requesting troops"))

    def _has_unit_picture(self, name: str) -> bool:
        from farmbot.units import slug

        return (Path(self.config.bot.templates) / "donate" / f"{slug(name)}.png").is_file()

    def _builder_base(self) -> TabbedPage:
        page = TabbedPage("builder_base", "Builder base", "Everything for builder battles. Pick Builder base under "
                          "What to farm on the Dashboard to play it. Army: fill it with Night Witches only; "
                          "the bot attacks best with an all Night Witch army.")
        self._builder_attack_tab(page.tab("attacking", "Attacking"))
        self._builder_village_tab(page.tab("village", "Village"))
        return page

    def _builder_attack_tab(self, page: Page) -> None:
        deploy = page.add(SettingCardGroup("Deploying"))
        self.segments(deploy, "builder.corner", FIF.ZOOM, "Go in from",
                      "Auto picks the map corner farthest from the enemy buildings.",
                      {"auto": "Auto", "left": "Left", "top": "Top", "right": "Right"}, width=80)
        self.switch(deploy, "builder.use_hero", FIF.PEOPLE, "Hero goes first", "Off: troops only.")
        self.speed(deploy, "builder.troop_speed", FIF.SPEED_HIGH, "Troop speed",
                   "How quickly the troops go down, one after another.")
        self.number(deploy, "builder.troop_delay", FIF.STOP_WATCH, "Troops follow after",
                    "Seconds after the hero lands.", "s")
        self.segments(deploy, "builder.first_hero", FIF.PEOPLE, "First hero",
                      "The hero for the first base. Keep: whichever is set in the game.",
                      {"any": "Keep", "machine": "Battle Machine", "copter": "Battle Copter"}, width=120)
        self.switch(deploy, "builder.switch_hero", FIF.SYNC, "Switch hero when hurt",
                    "Before a base, swaps to your other builder hero if this one's health is low.")
        self.number(deploy, "builder.switch_below", FIF.HEART if hasattr(FIF, "HEART") else FIF.SYNC,
                    "Switch below", "Hero health that triggers the swap.", "%")
        self.depends("builder.use_hero", "builder.troop_delay", "builder.first_hero", "builder.switch_hero")
        self.depends("builder.switch_hero", "builder.switch_below")

        abilities = page.add(SettingCardGroup("Abilities"))
        self.switch(abilities, "builder.hero_ability", FIF.CALORIES, "Hero ability",
                    "Fired once, when it has charged (firing early does nothing).")
        self.number(abilities, "builder.hero_ability_after", FIF.STOP_WATCH, "Fire it after",
                    "Seconds after the hero lands: how long its ability takes to charge.", "s")
        self.depends("builder.hero_ability", "builder.hero_ability_after")
        self.switch(abilities, "builder.troop_abilities", FIF.CALORIES, "Troop abilities",
                    "Taps the cards of troops still on the field, a few at a time.")
        self.number(abilities, "builder.ability_delay", FIF.STOP_WATCH, "Between taps",
                    "Seconds from one round of ability taps to the next.", "s")

        battle = page.add(SettingCardGroup("Battle"))
        self.switch(battle, "builder.speed_up", FIF.SPEED_HIGH, "Play at 4x speed",
                    "Presses the 1x button whenever it shows.")
        self.switch(battle, "builder.return_home_after", FIF.HOME, "Sail home when the bot stops",
                    "Ends in the home village rather than the builder base.")

    def _builder_village_tab(self, page: Page) -> None:
        loot = page.add(SettingCardGroup("Collecting"))
        self.switch(loot, "builder.collect_resources", FIF.BASKETBALL,
                    "Collect the gold mines, elixir collectors and gem mine", "Before each builder battle.")
        self.switch(loot, "builder.collect_cart", FIF.SHOPPING_CART, "Collect the elixir cart",
                    "The cart by the dock that holds your defense rewards.")
        self.number_range(loot, "builder.cart_every_min", "builder.cart_every_max", FIF.SYNC, "Every",
                          "A random number of battles in this range each time. The same number twice = always that many.",
                          "battles")
        self.depends("builder.collect_cart", "builder.cart_every_min", "builder.cart_every_max")

        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "builder.walls", FIF.UP, "Upgrade builder base walls with spare loot",
                    "Before each battle it upgrades the cheapest wall. Off by default: it spends loot.")
        self.segments(walls, "builder.walls_use", FIF.MARKET, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.amount(walls, "builder.walls_min_gold", FIF.SAVE, "Keep at least this much gold",
                    "Walls only get what's above it.")
        self.amount(walls, "builder.walls_min_elixir", FIF.SAVE, "Keep at least this much elixir",
                    "Walls only get what's above it.")
        self.number(walls, "builder.walls_per_round", FIF.TILES, "Walls per round",
                    "At most this many between two battles.")
        self.depends("builder.walls", "builder.walls_use", "builder.walls_min_gold", "builder.walls_min_elixir",
                     "builder.walls_per_round")

    def _when_to_play(self) -> Page:
        page = Page("when_to_play", "When to play", "The hours it may play, its breaks, and when it stops by itself.")
        hours_group = page.add(SettingCardGroup("Playing hours"))
        self.switch(hours_group, "schedule.enabled", FIF.CALENDAR, "Only play at certain hours",
                    "Off: plays whenever it's running. On: only in the hours lit up below (your PC's time).")
        grid_card = SimpleCardWidget()
        grid_card.setFixedHeight(150)
        grid = QGridLayout(grid_card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setSpacing(6)
        hour_buttons = {}
        for h in range(24):
            b = TogglePushButton(f"{h:02d}")
            b.setFixedWidth(64)
            b.setToolTip(f"{h:02d}:00 to {h:02d}:59")
            grid.addWidget(b, h // 12, h % 12)
            hour_buttons[h] = b
        binding = self._bind("schedule.active_hours", bind_toggles(hour_buttons))
        presets = QHBoxLayout()
        preset_buttons = []
        for label, chosen in (("All day", range(24)), ("Nights off (00–07)", range(8, 24)),
                              ("Evenings (17–23)", range(17, 24)), ("None", ())):
            b = PushButton(label)
            b.clicked.connect(lambda _=False, c=chosen: binding.set(list(c)))
            presets.addWidget(b)
            preset_buttons.append(b)
        presets.addStretch(1)
        grid.addLayout(presets, 2, 0, 1, 12)
        binding.widgets.extend(preset_buttons)
        self.hours_error = CaptionLabel("")
        tint(self.hours_error, BAD)
        grid.addWidget(self.hours_error, 3, 0, 1, 12)
        grid.setColumnStretch(12, 1)
        hours_group.addSettingCard(grid_card)
        self.depends("schedule.enabled", "schedule.active_hours")

        breaks = page.add(SettingCardGroup("Breaks"))
        self.switch(breaks, "schedule.breaks", FIF.CAFE, "Take breaks",
                    "Rests now and then, at random, to play more like a person.")
        self.number_range(breaks, "schedule.break_every_min", "schedule.break_every_max", FIF.SYNC,
                          "Every", "A new random number of attacks after each break.", "attacks")
        self.number_range(breaks, "schedule.break_minutes_min", "schedule.break_minutes_max", FIF.STOP_WATCH,
                          "For", "Minutes, picked at random.", "min")
        self.depends("schedule.breaks", "schedule.break_every_min", "schedule.break_minutes_min")

        stop = page.add(SettingCardGroup("When to stop"))
        storages = QWidget()
        row = QHBoxLayout(storages)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        buttons = {}
        for key, name in (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark")):
            b = TogglePushButton(icons.icon(key, 28), f"  {name}")
            b.setMinimumWidth(104)
            b.setIconSize(QSize(28, 28))
            b.setMinimumHeight(44)
            b.setToolTip(f"Stop once your {name.lower()} storage is full")
            row.addWidget(b)
            buttons[key] = b
        self._bind("bot.stop_when_full", bind_toggles(buttons))
        card = self._card(stop, "bot.stop_when_full", FIF.POWER_BUTTON, "When these storages are full",
                          "Pick the resources you farm for. It stops once every one you picked is full; with Auto "
                          "switch (Dashboard) it moves on to the other village first. None picked: it never stops for this.",
                          storages)
        card.setFixedHeight(96)
        self.number_range(stop, "schedule.daily_attacks_min", "schedule.daily_attacks_max", FIF.FLAG,
                          "Attacks per day", "Randomly picks a number in this range each day, then waits for the "
                          "next day. 0 to 0 = no limit.", "attacks")
        return page

    def _pictures(self, page: Page) -> None:
        body = QHBoxLayout()
        body.setSpacing(16)
        self.picture_list = ListWidget()
        self.picture_list.setFixedWidth(240)
        self.picture_list.setMinimumHeight(480)
        self.picture_list.currentRowChanged.connect(lambda _: self._picture_selected())
        body.addWidget(self.picture_list)

        right = QVBoxLayout()
        right.setSpacing(10)
        self.picture_title = SubtitleLabel("")
        self.picture_title.setWordWrap(True)
        self.picture_where = BodyLabel("")
        self.picture_where.setWordWrap(True)
        self.picture_status = CaptionLabel("")
        self.picture_status.setWordWrap(True)
        current = QHBoxLayout()
        self.picture_preview = QLabel()
        self.picture_preview.setFixedHeight(56)
        current.addWidget(self.picture_preview)
        current.addWidget(self.picture_status, 1)
        for w in (self.picture_title, self.picture_where):
            right.addWidget(w)
        right.addLayout(current)

        buttons = QHBoxLayout()
        self.capture_button = PrimaryPushButton(FIF.CAMERA, "Take screenshot")
        self.capture_button.setToolTip("Grab the game screen from the device")
        self.capture_button.clicked.connect(self._capture_screen)
        open_button = PushButton(FIF.FOLDER, "Open image…")
        open_button.clicked.connect(self._open_screen)
        self.variant_name = LineEdit()
        self.variant_name.setPlaceholderText("name, e.g. gold")
        self.variant_name.setFixedWidth(160)
        self.variant_name.textChanged.connect(lambda _: self._crop_changed())
        self.save_picture_button = PrimaryPushButton(FIF.SAVE, "Save picture")
        self.save_picture_button.clicked.connect(self._save_picture)
        # Two rows, so the panel fits beside the list at the window's smallest.
        for w in (self.capture_button, open_button):
            buttons.addWidget(w)
        buttons.addStretch(1)
        right.addLayout(buttons)
        save_row = QHBoxLayout()
        save_row.addWidget(self.variant_name)
        save_row.addWidget(self.save_picture_button)
        save_row.addStretch(1)
        right.addLayout(save_row)

        self.canvas = CropCanvas()
        self.canvas.on_change = self._crop_changed
        right.addWidget(self.canvas, 1)
        self.crop_info = CaptionLabel("")
        right.addWidget(self.crop_info)
        body.addLayout(right, 1)
        page.layout_.addLayout(body)
        self._refresh_pictures()

    def _refresh_pictures(self, keep: str | None = None) -> None:
        library = self.library()
        row = self.picture_list.currentRow()
        self.picture_list.blockSignals(True)
        self.picture_list.clear()
        for picture in catalog.PICTURES:
            have = catalog.present(library, picture)
            recommended = picture.feature == catalog.QUICK_EXIT
            mark = "✓" if have else "✗" if picture.required else "!" if recommended else "○"
            item = QListWidgetItem(f"{mark}  {picture.title}")
            item.setData(Qt.UserRole, picture.name)
            item.setToolTip(f"{picture.feature}{' (required)' if picture.required else ''}: "
                            f"{'in place' if have else 'missing'}")
            item.setForeground(QColor(str(GOOD if have else BAD if picture.required else WARN if recommended else MUTED)))
            self.picture_list.addItem(item)
        if keep:
            row = next(i for i, p in enumerate(catalog.PICTURES) if p.name == keep)
        elif row < 0:  # the first picture worth adding
            row = next((i for i, p in enumerate(catalog.PICTURES)
                        if (p.required or p.feature == catalog.QUICK_EXIT) and not catalog.present(library, p)), 0)
        self.picture_list.blockSignals(False)
        self.picture_list.setCurrentRow(row)
        self._picture_selected()

    def selected_picture(self) -> catalog.Picture:
        item = self.picture_list.currentItem()
        return catalog.BY_NAME[item.data(Qt.UserRole)] if item else catalog.PICTURES[0]

    def _picture_selected(self) -> None:
        picture = self.selected_picture()
        library = self.library()
        self.picture_title.setText(picture.title + ("  · required" if picture.required else ""))
        self.picture_where.setText(f"{picture.where}\nNeeded for: {picture.feature}.")
        self.variant_name.setVisible(picture.folder)
        paths = library.paths(picture.name)
        if paths:
            pixmap = QPixmap(str(paths[0]))
            self.picture_preview.setPixmap(pixmap.scaledToHeight(min(56, pixmap.height()), Qt.SmoothTransformation))
            extra = f" (+{len(paths) - 1} more)" if len(paths) > 1 else ""
            self.picture_status.setText(f"✓ Added{extra}. Saving a new one replaces it." if not picture.folder else
                                        f"✓ {len(paths)} picture(s) added. Saving adds another.")
            tint(self.picture_status, GOOD)
        else:
            self.picture_preview.clear()
            if picture.required:
                text, colour = "✗ Not added yet.", BAD
            elif picture.feature == catalog.QUICK_EXIT:
                text, colour = "! Recommended: until it's added, the bot closes the game to leave each battle.", WARN
            else:
                text, colour = "○ Not added (optional).", MUTED
            self.picture_status.setText(text)
            tint(self.picture_status, colour)
        self._crop_changed()

    def _crop_changed(self) -> None:
        crop = self.canvas.crop()
        picture = self.selected_picture()
        needs_name = picture.folder and not self.variant_name.text().strip()
        self.save_picture_button.setEnabled(crop is not None and not needs_name)
        if self.canvas.image is None:
            self.crop_info.setText("")
            return
        if crop is None:
            self.crop_info.setText("Drag a box around the button's text. Leave out timers, numbers and anything that moves.")
            tint(self.crop_info, MUTED)
            return
        r = self.canvas.selection()
        lookalike = catalog.lookalike_score(self.canvas.image, crop)
        risky = lookalike >= self.config.bot.threshold - 0.05
        self.crop_info.setText(f"Box {r.w}×{r.h} at ({r.x}, {r.y}). "
                               + (f"⚠ Something else on screen looks {lookalike:.0%} similar: crop tighter."
                                  if risky else f"✓ Unique on this screen (next best {max(lookalike, 0):.0%}).")
                               + ("  Enter a name first." if needs_name else ""))
        tint(self.crop_info, WARN if risky or needs_name else GOOD)

    def _capture_screen(self) -> None:
        if not self._autosave():
            return
        self.capture_button.setEnabled(False)
        self.crop_info.setText("Taking a screenshot…")
        tint(self.crop_info, MUTED)

        def work() -> None:
            from farmbot.bluestacks import open_device, restart_emulator
            from farmbot.game import normalize_screen
            from farmbot.vision import Vision

            try:
                raw = open_device(self.config.device).screenshot()
                vision = Vision(self.library(), self.config.bot.threshold)
                self.events.put(("screen", normalize_screen(raw, vision, self.config.device.ui_scale)))
            except Exception as exc:
                self.events.put(("screen", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _open_screen(self) -> None:
        import cv2

        from farmbot.game import normalize_screen
        from farmbot.vision import Vision

        path, _ = QFileDialog.getOpenFileName(self, "Open screenshot", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path:
            return
        image = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            InfoBar.error("Can't open that file", path, duration=4000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
            return
        vision = Vision(self.library(), self.config.bot.threshold)
        self.canvas.set_image(normalize_screen(image, vision, self.config.device.ui_scale))

    def _show_screen(self, result) -> None:
        self.capture_button.setEnabled(True)
        if isinstance(result, str):
            self.crop_info.setText(f"✗ {result}")
            tint(self.crop_info, BAD)
        else:
            self.canvas.set_image(result)

    def _save_picture(self) -> None:
        crop = self.canvas.crop()
        if crop is None:
            return
        picture = self.selected_picture()
        try:
            path = catalog.save_crop(Path(self.config.bot.templates), picture, crop, self.variant_name.text().strip())
        except OSError as exc:
            InfoBar.error("Couldn't save", str(exc), duration=4000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
            return
        InfoBar.success("Picture saved", f"{picture.title}: COCFARM uses it from now on.", duration=2500,
                        position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
        self.canvas.select(None)
        self._refresh_pictures(keep=picture.name)

    # ------------------------------------------------------------ setup
    STEP_TITLES = ("1.  Pick your emulator", "2.  Connect and check the game", "3.  Choose the loot you want",
                   "4.  Start farming")

    def _setup(self) -> Page:
        page = Page("setup", "Setup", "Four steps to get farming. COCFARM sets the emulator up by itself.")
        self.steps: list[SettingCardGroup] = []

        step = self._step(page, 0)
        self.segments(step, "device.mode", FIF.CONNECT if hasattr(FIF, "CONNECT") else FIF.WIFI, "Emulator",
                      "The Android emulator Clash of Clans runs in on this PC. Other: any device by its address "
                      "(Advanced).", {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer", "manual": "Other"}, width=100)
        self.bluestacks_info = BodyLabel("")
        self.bluestacks_info.setWordWrap(True)
        self.bluestacks_info.setMinimumWidth(240)
        self.bluestacks_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        refresh = PushButton(FIF.SYNC, "Look again")
        refresh.setToolTip("Look for BlueStacks and LDPlayer again, e.g. after installing one")
        refresh.clicked.connect(self._find_bluestacks)
        self.found_card = self._card(step, None, FIF.INFO, "On this PC", "", self.bluestacks_info, refresh)
        self.instance_box = EditableComboBox()
        self.instance_box.setFixedWidth(240)
        self.instance_box.setPlaceholderText("Main instance")
        self._bind("device.bluestacks_instance", bind_combo(self.instance_box))
        self.instance_card = self._card(step, "device.bluestacks_instance", FIF.APPLICATION, "BlueStacks window",
                                        "Which BlueStacks instance plays the game. Empty uses the main one.",
                                        self.instance_box)
        self.depends("device.mode", "device.bluestacks_instance", when="bluestacks")
        self.switch(step, "device.launch_emulator", FIF.PLAY, "Open the emulator automatically",
                    "When you press Start and it isn't open yet.")

        step = self._step(page, 1)
        test = PrimaryPushButton(FIF.SYNC, "Check now")
        test.clicked.connect(self._test_connection)
        self.test_button = test
        self.device_result = CaptionLabel("Not checked yet")
        self.device_result.setWordWrap(True)
        tint(self.device_result, MUTED)
        self._card(step, None, FIF.PHONE, "Connect to the emulator",
                   "Opens the emulator if needed and sets it up for COCFARM (it may restart once).",
                   self.device_result, test)
        self.game_installed = CaptionLabel("Not checked yet")
        self.game_logged_in = CaptionLabel("Not checked yet")
        for label in (self.game_installed, self.game_logged_in):
            label.setWordWrap(True)
            tint(label, MUTED)
        self._card(step, None, FIF.DOWNLOAD, "Clash of Clans installed",
                   "Install it from the Play Store inside the emulator.", self.game_installed)
        self._card(step, None, FIF.PEOPLE, "Signed in to your account",
                   "Open the game once in the emulator and sign in; COCFARM can't do this part.", self.game_logged_in)
        self.fix_button = PrimaryPushButton("")
        self.fix_button.clicked.connect(lambda: self._do_fix(self.fix_action, retry=self._test_connection))
        self.fix_action = ""
        self.fix_card = self._card(step, None, FIF.INFO, "What to do", " ", self.fix_button)
        self.fix_card.hide()

        step = self._step(page, 2)
        for path, name in (("search.min_gold", "Gold"), ("search.min_elixir", "Elixir"), ("search.min_dark", "Dark elixir")):
            w = LineEdit()
            w.setFixedWidth(170)
            w.setAlignment(Qt.AlignRight)
            w.setPlaceholderText("e.g. 1.5m")
            card = Card(FIF.MARKET, name, f"Only attack bases offering at least this much {name.lower()}. "
                        "0 ignores it. Accepts 500k or 1.5m.", w)
            step.addSettingCard(card)
            self._mirror(path, bind_line(w), card)
        more = PushButton(FIF.FILTER if hasattr(FIF, "FILTER") else FIF.MORE, "More choices")
        more.clicked.connect(lambda: self.show_page("Home village", "bases"))
        self._card(step, None, FIF.MORE if hasattr(FIF, "MORE") else FIF.INFO, "Want more control?",
                   "Totals, any-one-is-enough, and when to skip searching: Home village → Bases to attack.", more)

        step = self._step(page, 3)
        go = PrimaryPushButton(FIF.PLAY, "Start")
        go.clicked.connect(self.start)
        self.setup_start = go
        self._card(step, None, FIF.PLAY, "Press Start",
                   "It opens the game, collects loot and starts attacking. Watch it on the Dashboard; Pause (Esc) any time.", go)

        notify = page.add(SettingCardGroup("Alerts on your phone (optional)"))
        self.text(notify, "notify.discord_webhook", FIF.MESSAGE, "Discord webhook",
                  "Messages go to a Discord channel: Server Settings → Integrations → Webhooks → New Webhook → "
                  "Copy Webhook URL, then paste it here.", width=300)
        self.switch(notify, "notify.on_storage_full", FIF.FLAG, "When storage is full",
                    "A message when the storages you picked on When to play are full.")
        self.switch(notify, "notify.on_stopped", FIF.POWER_BUTTON, "When the bot stops",
                    "After a run, or when a problem stops it.")
        self.switch(notify, "notify.on_emulator_restart", FIF.SYNC, "When the emulator is restarted",
                    "A message when the game kept failing and COCFARM restarted the emulator.")
        self.switch(notify, "notify.daily_summary", FIF.CALENDAR, "Daily summary",
                    "Yesterday's attacks and loot, sent after midnight.")
        test_notify = PushButton(FIF.SEND, "Send a test")
        test_notify.clicked.connect(self._test_notify)
        self.notify_result = CaptionLabel("")
        self._card(notify, None, FIF.CHAT, "Test", "Sends a message to the webhook above.",
                   self.notify_result, test_notify)
        return page

    def _step(self, page: Page, index: int) -> SettingCardGroup:
        group = page.add(SettingCardGroup(self.STEP_TITLES[index]))
        self.steps.append(group)
        return group

    def _mirror(self, path: str, binding: Binding, card: Card) -> None:
        """Show the setting ``path`` (bound elsewhere) on a second card too."""
        self.mirrors.setdefault(path, []).append(binding)
        self.mirror_cards.setdefault(path, []).append(card)

        def changed(*_):
            if not self._syncing and self.bindings[path].get() != binding.get():
                self.bindings[path].set(binding.get())

        binding.connect(changed)

    def _sync_mirrors(self) -> None:
        self._syncing = True
        try:
            for path, bindings in self.mirrors.items():
                value = self.bindings[path].get()
                for b in bindings:
                    if b.get() != value:
                        b.set(value)
        finally:
            self._syncing = False

    def _steps_done(self) -> list[bool]:
        mode = self.bindings["device.mode"].get()
        found = (mode == "manual" or (mode == "bluestacks" and self._found.get("bluestacks"))
                 or (mode == "ldplayer" and self._found.get("ldplayer")))
        form = self.collect()
        loot = any(str(form.get(p, "0")).strip() not in ("", "0") for p in
                   ("search.min_gold", "search.min_elixir", "search.min_dark"))
        return [bool(found), self.check_passed, loot and not self.has_errors, self._running()]

    def _show_steps(self) -> None:
        if not getattr(self, "steps", None):
            return
        for group, title, done in zip(self.steps, self.STEP_TITLES, self._steps_done()):
            group.titleLabel.setText(("✓  " if done else "") + title)
            tint(group.titleLabel, GOOD if done else None)
        if hasattr(self, "setup_banner"):
            self.setup_banner.setVisible(not self.setup_passed)

    def _do_fix(self, action: str, retry: Callable[[], None]) -> None:
        """The button under a problem: do the next step it names."""
        if action == "setup":
            self.show_page("Setup")
        elif action == "autostart":
            self.bindings["device.launch_emulator"].set(True)
            self._autosave()
            retry()
        elif action == "pictures":
            self.show_page("Advanced", "pictures")
        elif action == "logs":
            import os

            folder = Path("logs").resolve()
            folder.mkdir(exist_ok=True)
            os.startfile(str(folder))  # Windows: opens it in Explorer
        else:
            retry()

    # ------------------------------------------------------------ advanced
    def _advanced(self) -> TabbedPage:
        page = TabbedPage("advanced", "Advanced", "Technical settings most players never need, and the tools for "
                          "teaching COCFARM new buttons.")
        tab = page.tab("settings", "Settings")
        look = tab.add(SettingCardGroup("Appearance"))
        self.segments(look, "bot.theme", FIF.PALETTE, "Theme", "Light or dark window. Follow Windows uses your "
                      "Windows setting.", THEMES, width=120)

        updates = tab.add(SettingCardGroup("Updates"))
        self.update_button = PushButton(FIF.SYNC, "Check now")
        self.update_button.clicked.connect(self._update_clicked)
        self.update_progress = ProgressBar()
        self.update_progress.setFixedWidth(160)
        self.update_progress.hide()
        self.update_card = self._card(updates, None, FIF.UPDATE, f"COCFARM {__version__}",
                                      "Updates come from COCFARM's GitHub releases. Your settings and history are kept.",
                                      self.update_progress, self.update_button)
        self.switch(updates, "bot.check_updates", FIF.SYNC, "Look for updates by itself",
                    "Checks GitHub when the window opens, and every hour while it's open.")

        emulator = tab.add(SettingCardGroup("Emulator"))
        self.switch(emulator, "device.enable_adb", ICON_TOOLS, "Turn on ADB",
                    "ADB is how COCFARM controls the emulator in the background. If it's off, COCFARM turns it on "
                    "(the emulator restarts once).")
        self.switch(emulator, "device.set_display", ICON_SCREEN, "Set the display",
                    "860 x 672 at 240 DPI, the size COCFARM is made for. Applied when it connects; the emulator "
                    "restarts once if it had another size.")
        self.number(emulator, "device.display_width", ICON_SCREEN, "Width", "Screen width in pixels (860 is trained).", "px")
        self.number(emulator, "device.display_height", ICON_SCREEN, "Height", "Screen height in pixels (672 is trained).",
                    "px")
        dpi = ComboBox()
        dpi.setFixedWidth(150)
        self._bind("device.display_dpi", bind_choice(dpi, {str(d): str(d) for d in DPI_CHOICES}))
        self._card(emulator, "device.display_dpi", ICON_SCREEN, "DPI", "Screen density. 240 is what COCFARM is trained on.",
                   dpi)
        self.depends("device.set_display", "device.display_width", "device.display_height", "device.display_dpi")
        self.switch(emulator, "device.set_resources", FIF.SPEED_OFF, "Use as little of the PC as possible",
                    "Gives the emulator only the cores and memory below.")
        self.segments(emulator, "device.cpus", ICON_TOOLS, "CPU cores", "2 is enough for the game.", ["2", "3", "4"])
        memory = ComboBox()
        memory.setFixedWidth(150)
        self._bind("device.memory", bind_choice(memory, {str(mb): f"{mb} MB" for mb in MEMORY_CHOICES}))
        self._card(emulator, "device.memory", ICON_TOOLS, "Memory", "2048 MB is enough for the game.", memory)
        self.depends("device.set_resources", "device.cpus", "device.memory")
        self.depends("device.mode", "device.enable_adb", "device.set_display", "device.set_resources",
                     when=("bluestacks", "ldplayer"))
        self.ldplayer_box = EditableComboBox()
        self.ldplayer_box.setFixedWidth(240)
        self.ldplayer_box.setPlaceholderText("Newest installed")
        self._bind("device.ldplayer", bind_combo(self.ldplayer_box))
        self._card(emulator, "device.ldplayer", FIF.APPLICATION, "LDPlayer version",
                   "Which LDPlayer to use (9 or newer). Empty uses the newest one installed.", self.ldplayer_box)
        self.number(emulator, "device.ldplayer_index", FIF.TILES, "LDPlayer instance number",
                    "0 is the first instance in LDMultiPlayer.")
        self.depends("device.mode", "device.ldplayer", "device.ldplayer_index", when="ldplayer")
        self.text(emulator, "device.serial", FIF.CONNECT if hasattr(FIF, "CONNECT") else FIF.WIFI, "Device address",
                  "For Emulator = Other: the ADB address of any emulator or phone, e.g. 127.0.0.1:5555.", width=200)
        self.depends("device.mode", "device.serial", when="manual")
        self.text(emulator, "device.adb_path", ICON_TOOLS, "ADB program",
                  "\"adb\" uses the one that comes with the emulator; or the full path to adb.exe.", width=200)

        battle = tab.add(SettingCardGroup("Attacks"))
        self.switch(battle, "redline.enabled", FIF.HIGHTLIGHT, "Follow the red line",
                    "Deploy along the thin boundary the game draws around a base.")
        self.switch(battle, "zone.enabled", FIF.LAYOUT, "Otherwise deploy around the buildings",
                    "Off: use the fixed deploy lines from farmbot.toml.")
        self.number(battle, "search.max_searches", FIF.SYNC, "Give up searching after",
                    "Bases to skip before going back to the village and starting over.", "bases")
        self.number(battle, "builder.stage_delay", FIF.STOP_WATCH, "Builder: wait before the second base",
                    "Seconds after the first base falls, while the second loads.", "s")
        self.number(battle, "builder.ability_batch", FIF.TILES, "Builder: troop abilities per tap round",
                    "How many troop cards are tapped together.")
        self.depends("builder.troop_abilities", "builder.ability_delay", "builder.ability_batch")
        self.number(battle, "clan.request_every_minutes", FIF.HISTORY, "Clan request: wait if the timer can't be read",
                    "Minutes between requests when the Request button's timer can't be read.", "min")
        self.depends("clan.request_troops", "clan.request_every_minutes")

        check = tab.add(SettingCardGroup("Check the bot's vision"))
        see = PushButton(FIF.VIEW, "What the bot sees")
        see.clicked.connect(self._inspect_screen)
        self.see_button = see
        self._card(check, None, FIF.SEARCH, "Look at a base",
                   "Show a base being scouted in the game, then press: shows the loot, troop cards and deploy "
                   "points it finds, without attacking.", see)
        self.seen_text = BodyLabel("")
        self.seen_text.setWordWrap(True)
        self.seen_view = QLabel()
        self.seen_view.setAlignment(Qt.AlignCenter)
        for w in (self.seen_text, self.seen_view):
            w.hide()
            tab.add(w)
        self._pictures(page.tab("pictures", "Pictures", "COCFARM finds buttons by comparing the screen with small "
                                "pictures of them. Pick one, show the screen it's on, and drag a box tightly around it."))
        return page

    def _welcome(self) -> None:
        """First run: open Setup, where the steps say what to do."""
        self.show_page("Setup")
        InfoBar.info("Welcome to COCFARM", "Follow the four steps on this page. It takes a couple of minutes.",
                     duration=8000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)

    def _check_for_update(self, force: bool = False) -> None:
        """Ask GitHub for a newer COCFARM in the background (see farmbot/update.py
        for how often it really asks)."""
        repo = self.config.bot.update_repo
        if not repo or self.update_busy or not (force or self.config.bot.check_updates):
            return
        if force:
            self.update_button.setEnabled(False)
            self._update_note("Asking GitHub…")

        def work() -> None:
            from farmbot import update

            self.events.put(("update", (update.check(repo, force=force), force)))

        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, check, force: bool) -> None:
        from farmbot.update import can_install

        self.update_button.setEnabled(True)
        if self.update_staged:
            return
        release = check.release
        if force or release:
            self._update_note(check.note)
        if not release:
            return
        installable = can_install() and bool(release.download)
        self.update_button.setText("Update now" if installable else "Download")
        self.update_button.setIcon(FIF.DOWNLOAD)
        new = self.update_release is None or self.update_release.version != release.version
        self.update_release = release
        if new:
            self._update_bar(f"COCFARM {release.version} is out",
                             PushButton(FIF.DOWNLOAD, "Update now" if installable else "Download"))

    def _update_bar(self, title: str, button: QAbstractButton) -> None:
        """A bar in the corner with one button, replacing the last one."""
        if self.update_bar:
            self.update_bar.close()
        # Built hidden and shown once the button is in: it's placed by its full width.
        self.update_bar = InfoBar(InfoBarIcon.SUCCESS, title, "", duration=-1, position=InfoBarPosition.BOTTOM_RIGHT,
                                  parent=self)
        button.clicked.connect(self._update_clicked)
        self.update_bar.addWidget(button)
        self.update_bar.show()

    def _update_clicked(self) -> None:
        if self.update_bar:
            self.update_bar.close()
            self.update_bar = None
        if self.update_staged:
            self._restart_to_update()
        elif self.update_release:
            self.show_page("Advanced", "settings")
            self._download_update()
        else:
            self._check_for_update(force=True)

    def _download_update(self) -> None:
        import webbrowser

        from farmbot import update

        release = self.update_release
        if not update.can_install() or not release.download:
            webbrowser.open(release.url)
            return
        if self.update_busy:
            return
        self.update_busy = True
        self.update_button.setEnabled(False)
        self.update_progress.setValue(0)
        self.update_progress.show()
        self._update_note(f"Downloading COCFARM {release.version}… You can keep farming meanwhile.")

        def progress(done: int, total: int) -> None:
            self.events.put(("update_progress", (done, total)))

        def work() -> None:
            try:
                staged = update.unpack(update.download(release, progress=progress))
                self.events.put(("update_ready", staged))
            except update.UpdateError as exc:
                self.events.put(("update_failed", str(exc)))
            except Exception as exc:
                log.exception("Update failed")
                self.events.put(("update_failed", f"Something went wrong: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_update_progress(self, done: int, total: int) -> None:
        if total:
            self.update_progress.setValue(int(done * 100 / total))
            self._update_note(f"Downloading COCFARM {self.update_release.version}… "
                                        f"{done / 2**20:.0f} of {total / 2**20:.0f} MB")

    def _update_ready(self, staged: Path) -> None:
        self.update_busy = False
        self.update_staged = staged
        self.update_progress.hide()
        self.update_button.setEnabled(True)
        self.update_button.setText("Restart and update")
        self.update_button.setIcon(FIF.UPDATE)
        self._update_note(f"COCFARM {self.update_release.version} is downloaded. It installs when "
                                    "COCFARM restarts (about 10 seconds).")
        self._update_bar(f"COCFARM {self.update_release.version} is ready",
                         PrimaryPushButton(FIF.UPDATE, "Restart and update"))

    def _update_failed(self, message: str) -> None:
        self.update_busy = False
        self.update_progress.hide()
        self.update_button.setEnabled(True)
        self._update_note(message, BAD)

    def _restart_to_update(self) -> None:
        if self._running():  # finish the current step first, like Stop
            self._update_note("Stopping farming first…")
            self.update_button.setEnabled(False)
            self.stop()
            QTimer.singleShot(500, self._restart_to_update)
            return
        from farmbot import update

        try:
            update.install(self.update_staged, version=self.update_release.version)
        except OSError as exc:
            self._update_failed(f"Couldn't start the update: {exc}")
            return
        self.close()
        QApplication.instance().quit()

    def _update_note(self, text: str, tone=None) -> None:
        self.update_card.setContent(text)
        tint(self.update_card.contentLabel, tone)

    def _after_update(self) -> None:
        from farmbot.update import finished_install

        message = finished_install()
        if message:
            bar = InfoBar.success if message.startswith("Updated") else InfoBar.warning
            bar("Update", message, duration=10000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)

    def _test_notify(self) -> None:
        if not self._autosave():
            return
        from farmbot.notify import Notifier

        notifier = Notifier(self.config.notify)
        if not notifier.configured:
            self.notify_result.setText("Set a Discord webhook first.")
            return
        self.notify_result.setText("Sending…")

        def work() -> None:
            ok = notifier.test()
            self.events.put(("notify", ok))

        threading.Thread(target=work, daemon=True).start()

    def _inspect_screen(self) -> None:
        if not self._autosave():
            return
        self.see_button.setEnabled(False)
        self.seen_text.setText("Taking a screenshot…")
        self.seen_text.show()

        def work() -> None:
            from farmbot.bluestacks import open_device
            from farmbot.game import normalize_screen
            from farmbot.inspect import describe
            from farmbot.vision import Vision

            try:
                vision = Vision(self.library(), self.config.bot.threshold)
                screen = normalize_screen(open_device(self.config.device).screenshot(), vision, self.config.device.ui_scale)
                self.events.put(("seen", describe(screen, self.config, vision)))
            except Exception as exc:
                self.events.put(("seen", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _show_seen(self, result) -> None:
        self.see_button.setEnabled(True)
        if isinstance(result, str):
            self.seen_text.setText(f"✗ {result}")
            tint(self.seen_text, BAD)
            self.seen_view.hide()
            return
        lines, overlay = result
        self.seen_text.setText("\n".join(lines))
        tint(self.seen_text, None)
        self.seen_view.setPixmap(to_pixmap(overlay).scaled(860, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.seen_view.show()

    def _find_bluestacks(self) -> None:
        """Fill in what's installed: BlueStacks and every LDPlayer."""
        from farmbot import bluestacks, ldplayer

        lines = []
        install = bluestacks.find_install()
        current = self.instance_box.currentText()
        self.instance_box.clear()
        if install is None:
            lines.append("✗ BlueStacks 5: not installed")
        else:
            self.instance_box.addItems([i.display_name for i in install.instances])
            lines.append("✓ BlueStacks 5: installed")
        self.instance_box.setText(current)
        installs = ldplayer.find_installs()
        current = self.ldplayer_box.currentText()
        self.ldplayer_box.clear()
        self.ldplayer_box.addItems([i.name for i in installs])
        self.ldplayer_box.setText(current)
        lines.append(("✓ " + ", ".join(i.name for i in installs) + ": installed") if installs
                     else "✗ LDPlayer: not installed")
        if install is None and not installs:
            lines.append("Install BlueStacks 5 (bluestacks.com) or LDPlayer 9 (ldplayer.net), then press Look again.")
        self._found = {"bluestacks": install is not None, "ldplayer": bool(installs)}
        self.bluestacks_info.setText(chr(10).join(lines))
        tint(self.bluestacks_info, WARN if install is None and not installs else None)
        self._show_steps()

    # ------------------------------------------------------------ form state
    def collect(self) -> dict[str, Any]:
        return {path: b.get() for path, b in self.bindings.items()}

    def _load(self) -> None:
        self._loading = True
        for path, value in form_values(self.config).items():
            if path in self.bindings:
                self.bindings[path].set(value)
        self._loading = False

    def _changed(self, save: bool = True) -> None:
        if self._loading:
            return
        self._sync_mirrors()
        form = self.collect()
        _, errors = validate(form)
        self.has_errors = bool(errors)
        for path, card in self.cards.items():
            card.show_error(errors.get(path))
        for path, cards in self.mirror_cards.items():
            for card in cards:
                card.show_error(errors.get(path))
        self.hours_error.setText(f"⚠  {errors['schedule.active_hours']}" if "schedule.active_hours" in errors else "")
        for path, trained in TRAINED_DISPLAY.items():
            card = self.cards.get(path)
            if card is not None and path not in errors and str(form.get(path)).strip() != str(trained):
                card.show_warning(TRAINED_NOTE)
        self.instance_card.setVisible(form.get("device.mode") == "bluestacks")
        mode = form.get("bot.mode")
        builder_mode = mode == "builder" or (mode == "auto" and (self.auto_village or form.get("bot.auto_first"))
                                             == "builder")
        start_with = self.cards["bot.auto_first"]
        if start_with.isVisibleTo(self.farm_group) != (mode == "auto"):
            start_with.setVisible(mode == "auto")
            QTimer.singleShot(0, lambda: _relayout_group(self.farm_group))
        self.home_panel.setVisible(not builder_mode)
        self.builder_panel.setVisible(builder_mode)
        for controller, bindings, when in self.dependents:
            on = (bool(form.get(controller)) if when is True
                  else form.get(controller) in when if isinstance(when, tuple) else form.get(controller) == when)
            for b in bindings:
                b.enable(on)
        self.start_button.setEnabled(not errors and (not self._running() or self.pause_event.is_set()))
        self.setup_start.setEnabled(self.start_button.isEnabled() and not self._running())
        self._show_steps()
        self._show_big_buttons()
        if form.get("bot.theme") in THEMES and form.get("bot.theme") != self._theme:
            self._apply_theme(form.get("bot.theme"))
        if errors:
            self.save_label.setText(f"⚠ Fix {len(errors)} setting{'s' if len(errors) > 1 else ''}")
            tint(self.save_label, BAD)
            self.save_timer.stop()
        elif save:
            self.save_label.setText("Saving…")
            tint(self.save_label, MUTED)
            self.save_timer.start()

    def _autosave(self) -> bool:
        try:
            apply_form(self.config, self.collect())
        except ValueError:
            self._changed(save=False)
            return False
        save_config(self.config, self.config_path)
        self.save_label.setText("✓ Saved")
        tint(self.save_label, MUTED)
        return True

    # ------------------------------------------------------------ bot
    def _running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        self.save_timer.stop()
        if self._running():
            if self.pause_event.is_set():  # Resume
                self.pause_event.clear()
                self._show_pause_button()
                self.start_button.setEnabled(False)
                self._show_status(describe_status("Resuming"))
            return
        if not self._autosave():
            InfoBar.error("Can't start yet", "A setting has a mistake: look for the red text, fix it, then press "
                          "Start again.", duration=5000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
            return
        attacking = not self.config.search.maintain_only
        missing = catalog.missing(self.library(), required_only=True) if attacking else []
        if missing:
            self._pictures_bar(InfoBar.error, "A button picture is missing", "The bot finds buttons by their "
                               "pictures, and can't attack without: " + ", ".join(p.title for p in missing) + ".")
            return
        if attacking and catalog.slow_exit(self.library()):
            self._pictures_bar(InfoBar.info, "Tip", "Without the Return Home and Okay pictures the bot closes "
                               "the game to leave each battle, which takes about 30 s longer.")
        self.stop_event = threading.Event()
        self.pause_event.clear()
        self._show_pause_button()
        self.session_start = time.monotonic()
        self.thread = threading.Thread(target=self._run_bot, args=(self.stop_event,), daemon=True)
        self.thread.start()
        self.problem = None
        self.auto_village = None
        self._show_status(describe_status("Starting…"))
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._changed(save=False)
        self.show_page("Dashboard")

    def stop(self) -> None:
        """First press: pause once the current action is done. Second
        press (the button is red by then): stop now, even mid-battle."""
        if not (self.stop_event and self._running()):
            return
        if not self.pause_event.is_set():
            self.pause_event.set()
            self._show_stop_now_button()
            self.start_button.setText("Resume")
            self.start_button.setIcon(FIF.PLAY)
            self.start_button.setToolTip("Carry on farming (F5)")
            self.start_button.setEnabled(True)
            self._show_status(describe_status("Pausing after the current action…"))
        else:
            self.stop_event.set()
            self.stop_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self._show_status(describe_status("Stopping now…"))

    def _show_pause_button(self) -> None:
        self.stop_button.setText("Pause")
        self.stop_button.setIcon(FIF.PAUSE)
        self.stop_button.setToolTip("Pause once the current action is done (Esc)")
        if self.stop_button.property("red"):
            self.stop_button.setProperty("red", False)
            setCustomStyleSheet(self.stop_button, "", "")
        self.start_button.setText("Start")
        self.start_button.setToolTip("Start farming (F5)")

    def _show_stop_now_button(self) -> None:
        self.stop_button.setText("Stop now")
        self.stop_button.setIcon(FIF.CLOSE)
        self.stop_button.setToolTip("Stop straight away, even in the middle of a battle (Esc)")
        red = ("PushButton { background: #c42b1c; color: white; border: 1px solid #a3261a; }"
               "PushButton:hover { background: #d13438; }"
               "PushButton:pressed { background: #a3261a; color: rgba(255,255,255,0.8); }")
        self.stop_button.setProperty("red", True)
        setCustomStyleSheet(self.stop_button, red, red)

    def _pictures_bar(self, kind, title: str, text: str) -> None:
        bar = kind(title, text, duration=8000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
        go = PushButton("Open Pictures")
        go.clicked.connect(lambda: (self.show_page("Advanced", "pictures"), bar.close()))
        bar.addWidget(go)

    def _run_bot(self, stop_event: threading.Event) -> None:
        from farmbot.bot import Bot
        from farmbot.bluestacks import open_device, restart_emulator
        from farmbot.game import Game
        from farmbot.vision import TemplateLibrary, Vision

        try:
            self.events.put(("status", "Connecting to the device…"))
            device = open_device(self.config.device)
            vision = Vision(TemplateLibrary(self.config.bot.templates), self.config.bot.threshold)
            game = Game(device, vision, stop_event=stop_event, ui_scale=self.config.device.ui_scale,
                        window_scale=self.config.device.window_scale,
                        picture_scales=self.config.device.picture_scales)
            bot = Bot(game, self.config,
                      on_status=lambda s: self.events.put(("status", s)),
                      on_base=lambda loot, ok: self.events.put(("base", (loot, ok, game.screen))),
                      on_resources=lambda loot: self.events.put(("resources", loot)),
                      on_attack=lambda record: self.events.put(("attack", record)),
                      on_builder=lambda battle: self.events.put(("builder", battle)),
                      pause_event=self.pause_event,
                      reopen_device=(lambda: restart_emulator(self.config.device))
                      if self.config.device.mode in ("bluestacks", "ldplayer") else None)
            bot.run()
        except Exception as exc:  # shown in the window instead of a silent thread death
            log.error("Bot stopped: %s", exc)
            self.events.put(("error", str(exc)))
        finally:
            self.events.put(("finished", None))

    def _test_connection(self) -> None:
        if not self._autosave():
            return
        self.device_result.setText("Connecting… (opening the emulator can take a minute)")
        tint(self.device_result, MUTED)
        self.fix_card.hide()
        self.test_button.setEnabled(False)
        name = self._emulator_name()
        for label in (self.game_installed, self.game_logged_in):
            label.setText("Checking…")
            tint(label, MUTED)

        def work() -> None:
            from farmbot import gamecheck
            from farmbot.bluestacks import open_device
            from farmbot.game import Game
            from farmbot.vision import Vision

            try:
                device = open_device(self.config.device)
                h, w = device.screenshot().shape[:2]
                self.events.put(("device", (True, f"✓ Connected to {name}")))
            except Exception as exc:
                self.events.put(("device", (False, str(exc))))
                self.events.put(("game", (None, None)))
                return
            package = self.config.device.package
            if not gamecheck.installed(device, package):
                self.events.put(("game", (False, False)))
                return
            self.events.put(("game", (True, None)))
            game = Game(device, Vision(self.library(), self.config.bot.threshold), ui_scale=self.config.device.ui_scale,
                        window_scale=self.config.device.window_scale, picture_scales=self.config.device.picture_scales)
            self.events.put(("game", (True, gamecheck.logged_in(game, package))))

        threading.Thread(target=work, daemon=True).start()

    def _emulator_name(self) -> str:
        return {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer"}.get(self.config.device.mode, "the emulator")

    def _show_device(self, ok: bool, message: str) -> None:
        if ok:
            self.device_result.setText(message)
            tint(self.device_result, GOOD)
            return
        problem = explain(message, self._emulator_name())
        self.device_result.setText(f"✗ {problem.title}")
        tint(self.device_result, BAD)
        self.device_result.setToolTip(problem.details)
        self._show_fix(problem.why, problem.action, problem.action_text)
        self.test_button.setEnabled(True)

    def _show_fix(self, text: str, action: str, button: str) -> None:
        self.fix_card.help_text = text  # survives the error/explanation refresh
        self.fix_card.setContent(text)
        tint(self.fix_card.contentLabel, WARN)
        self.fix_action = action
        self.fix_button.setText(button)
        self.fix_card.show()

    def _show_game(self, installed: bool | None, logged_in: bool | None) -> None:
        def show(label: QLabel, state: bool | None, yes: str, no: str, pending: str) -> None:
            text, colour = (pending, MUTED) if state is None else (yes, GOOD) if state else (no, BAD)
            label.setText(text)
            tint(label, colour)

        show(self.game_installed, installed, "✓ Installed", "✗ Not installed", "Couldn't check")
        if installed is False:
            show(self.game_logged_in, False, "", "✗ Install the game first", "")
            self._show_fix("Clash of Clans isn't installed in the emulator. Open the Play Store in the emulator, "
                           "install it, sign in, then check again.", "retry", "Check again")
        else:
            show(self.game_logged_in, logged_in, "✓ Signed in (the village loaded)",
                 "✗ The village didn't load", "Checking…" if installed else "Couldn't check")
            if logged_in is False:
                self._show_fix("Open Clash of Clans in the emulator and sign in. If it's open, close any window "
                               "covering the village, then check again.", "retry", "Check again")
        if logged_in is not None or installed is not None:
            self.test_button.setEnabled(logged_in is not None or installed is False)
        if installed and logged_in:
            self.fix_card.hide()
            self.check_passed = True
            if not self.setup_passed:
                self.setup_passed = True
                InfoBar.success("All set", "COCFARM can see your village. Choose the loot you want (step 3), then "
                                "press Start.", duration=6000, position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
            self._show_steps()

    # ------------------------------------------------------------ events
    def _show_status(self, status: Status) -> None:
        """Show what the bot is doing: the title bar chip and the Dashboard's big panel."""
        self.status_now = status
        fg, bg = STATES[status.state]
        self.state_chip.setText(f"{status.symbol}  {status.word}")
        self.state_chip.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 11px; padding: 3px 12px; "
                                      "font-weight: 600;")
        self.status_label.setText(status.headline)
        self.status_label.setToolTip(status.detail)
        self.big_state.setText(f"{status.symbol}  {status.word}")
        self.big_state.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 22px; padding: 10px 22px; "
                                     "font-size: 26px; font-weight: 700;")
        self.big_headline.setText(status.headline)
        self.big_detail.setText(status.detail)
        self.big_detail.setVisible(bool(status.detail))
        if status.state not in ("running", "attacking"):
            self.big_trouble.hide()
        self._show_big_buttons()

    def _show_problem(self, problem: Problem) -> None:
        self.problem = problem
        self._show_status(Status("problem", problem.title, problem.why))
        self.big_trouble.setText(f"Technical details: {problem.details}")
        tint(self.big_trouble, MUTED)
        self.big_trouble.show()
        bar = InfoBar.error(problem.title, problem.why, duration=10000, position=InfoBarPosition.BOTTOM_RIGHT,
                            parent=self)
        fix = PushButton(problem.action_text)
        fix.clicked.connect(lambda: (self._do_fix(problem.action, retry=self.start), bar.close()))
        bar.addWidget(fix)

    def _show_big_buttons(self) -> None:
        """The Dashboard's big buttons follow the state: Start, Pause, Resume/Stop now, or the fix for a problem."""
        if not hasattr(self, "big_primary"):
            return
        running, paused = self._running(), self.pause_event.is_set()
        if running and paused:
            primary, secondary = "Resume", "Stop now"
        elif running:
            primary, secondary = "Pause", ""
        elif self.status_now.state == "problem" and self.problem:
            primary = self.problem.action_text
            secondary = "Show the log" if self.problem.action == "retry" else "Start again"
        else:
            primary, secondary = "Start", ""
        self.big_primary.setText(primary)
        self.big_primary.setToolTip({"Start": "Start farming (F5)", "Pause": "Pause once the current step is done (Esc)",
                                     "Resume": "Carry on farming (F5)"}.get(primary, ""))
        self.big_primary.setEnabled(primary not in ("Start", "Resume") or self.start_button.isEnabled())
        self.big_secondary.setText(secondary)
        self.big_secondary.setVisible(bool(secondary))

    def _big_primary_clicked(self) -> None:
        text = self.big_primary.text()
        if text in ("Start", "Resume"):
            self.start()
        elif text == "Pause":
            self.stop()
        elif self.problem:
            self._do_fix(self.problem.action, retry=self.start)

    def _big_secondary_clicked(self) -> None:
        text = self.big_secondary.text()
        if text == "Stop now":
            self.stop()
        elif text == "Start again":
            self.start()
        elif text == "Show the log":
            self._show_details(True)

    def _apply_theme(self, name: str) -> None:
        self._theme = name
        setTheme({"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}[name])
        self._theme_changed()

    def _theme_changed(self) -> None:
        """Recolour everything coloured by hand for the theme now showing."""
        retint()
        if hasattr(self, "big_state"):
            self._show_status(self.status_now)
            self._verdict(self._verdict_kind)
        if hasattr(self, "history_chart"):
            self._show_history()
        if hasattr(self, "picture_list") and self.picture_list.count():
            self._refresh_pictures(keep=self.selected_picture().name)

    def _verdict(self, kind: str) -> None:
        self._verdict_kind = kind
        text, fg, bg = {"wait": ("Waiting for a base", MUTED, SUBTLE),
                        "attack": ("ATTACK", "#0b2e13", GOOD),
                        "skip": ("Skip", MUTED, SUBTLE)}[kind]
        self.verdict.setText(text)
        tint(self.verdict, fg, f"background: {bg}; border-radius: 14px; padding: 6px 18px; font-weight: 700;")

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._append_log(*data)
                    level, line = data
                    if level >= logging.ERROR and "Cycle failed" in line and self._running():
                        # The bot carries on by itself; say so, in plain words.
                        problem = explain(line.split(": ", 2)[-1], self._emulator_name())
                        self.big_trouble.setText(f"⚠  Last round didn't work: {problem.title[0].lower()}"
                                                 f"{problem.title[1:]}. It's trying again.")
                        tint(self.big_trouble, WARN)
                        self.big_trouble.show()
                elif kind == "status":
                    if data.startswith("Attacking (builder base): base"):
                        self._bb_stage("Second base" if data.endswith("2") else "First base")
                    status = describe_status(data)
                    if data.startswith("Storage full: switching to the "):
                        self.auto_village = "builder" if data.endswith("builder base") else "home"
                        self._changed(save=False)  # the Dashboard shows that village's panel
                    if data.startswith("Stopped: storage full"):
                        InfoBar.success(status.headline, status.detail, duration=-1,
                                        position=InfoBarPosition.BOTTOM_RIGHT, parent=self)
                    if data.startswith(("Attack ", "Searching")):
                        self.big_trouble.hide()  # a round went through: earlier trouble is over
                    self._show_status(status)
                    if data.endswith("done"):
                        self.stats["attacks"] += 1
                        self._update_stats()
                elif kind == "base":
                    self._show_base(*data)
                elif kind == "resources":
                    self.resources_label.setText(
                        f"You have  gold {data.gold:,}  ·  elixir {data.elixir:,}  ·  dark {data.dark:,}")
                elif kind == "builder":
                    self._bb_show(data)
                    self._bb_stage("Done")
                elif kind == "attack":
                    self._show_attack(data)
                    if data.gained:
                        for key in self.gained:
                            self.gained[key] += getattr(data.gained, key)
                        self._update_stats()
                    self._show_today()
                    self._show_history()
                elif kind == "seen":
                    self._show_seen(data)
                elif kind == "screen":
                    self._show_screen(data)
                elif kind == "game":
                    self._show_game(*data)
                elif kind == "update":
                    self._show_update(*data)
                elif kind == "update_progress":
                    self._show_update_progress(*data)
                elif kind == "update_ready":
                    self._update_ready(data)
                elif kind == "update_failed":
                    self._update_failed(data)
                elif kind == "notify":
                    self.notify_result.setText("✓ Sent: check your Discord channel" if data
                                               else "✗ Not sent: check the webhook address is copied in full")
                    tint(self.notify_result, GOOD if data else BAD)
                elif kind == "device":
                    self._show_device(*data)
                elif kind == "error":
                    self._show_problem(explain(data, self._emulator_name()))
                elif kind == "finished":
                    if self.status_now.state != "problem" and not self.status_now.headline.startswith("Storage full"):
                        self._show_status(describe_status("Stopped"))
                    self.stop_button.setEnabled(False)
                    self.pause_event.clear()
                    self._show_pause_button()
                    self._changed(save=False)
                    self._show_big_buttons()
        except queue.Empty:
            pass

    def _tick(self) -> None:
        if self.session_start is not None and self._running():
            seconds = int(time.monotonic() - self.session_start)
            self.stat_value["runtime"].setText(f"{seconds // 3600}h {seconds // 60 % 60:02d}m")
            self.bb_value["runtime"].setText(f"{seconds // 3600}h {seconds // 60 % 60:02d}m")

    def _show_base(self, loot, accepted: bool, screen: np.ndarray | None = None) -> None:
        self.stats["bases"] += 1
        if screen is not None:
            self._show_live(screen, accepted)
        search = self.config.search
        for key, value, minimum in (("gold", loot.gold, search.min_gold), ("elixir", loot.elixir, search.min_elixir),
                                    ("dark", loot.dark, search.min_dark)):
            self.loot_value[key].setText(f"{value:,}")
            met = value >= minimum
            self.loot_note[key].setText(f"{'✓' if met else '✗'}  minimum {minimum:,}")
            tint(self.loot_note[key], GOOD if met else MUTED)
        from farmbot.tasks.search import loot_value

        total = loot_value(loot, search)
        self.total_note.setText(f"Total {total:,}")
        self._verdict("attack" if accepted else "skip")
        if accepted and search.sound_on_match:
            QApplication.beep()
        self._update_stats()

    def _show_live(self, screen: np.ndarray, accepted: bool) -> None:
        import cv2

        h, w = screen.shape[:2]
        view = screen.copy()
        colour = (95, 203, 108) if accepted else (180, 165, 157)  # BGR of GOOD / MUTED
        for name in ("loot_gold", "loot_elixir", "loot_dark"):
            r = self.config.regions.rect(name).resolve(w, h)
            cv2.rectangle(view, (r.x, r.y), (r.x + r.w, r.y + r.h), colour, 3)
        width = max(320, self.live_view.width() if self.live_view.isVisible() else 860)
        self.live_view.setPixmap(to_pixmap(view).scaled(width, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.live_view.show()

    def _show_attack(self, record) -> None:
        t = record.target
        parts = [f"Offered {_short(t.gold)} gold · {_short(t.elixir)} elixir · {_short(t.dark)} dark"]
        if record.taken is not None:
            parts.append(f"took {record.taken:.0%}")
        self.last_attack.setText("  ·  ".join(parts))
        g = record.gained
        self.last_attack_note.setText(
            f"Gained gold {g.gold:+,} · elixir {g.elixir:+,} · dark {g.dark:+,}" if g
            else "Gain unknown: your resources couldn't be read afterwards.")

    def _show_today(self) -> None:
        """Today's attacks from the history file, so totals survive a restart."""
        from datetime import date

        from farmbot import history

        if not self.config.bot.history:
            self.stat_note["attacks"].setText("")
            for label in self.gained_today.values():
                label.setText("")
            return
        count, gained = history.totals_for(self.config.bot.history, date.today())
        self.stat_note["attacks"].setText(f"Today: {count}")
        self.stat_note["attacks"].setToolTip(f"Every attack is logged to {self.config.bot.history}")
        for key, label in self.gained_today.items():
            label.setText(f"Today {_short(getattr(gained, key))}")
        self.gained_note.setText(f"Today in total: {_short(gained.gold + gained.elixir + gained.dark)}")

    def _update_stats(self) -> None:
        self.stat_value["bases"].setText(f"{self.stats['bases']:,}")
        self.stat_value["attacks"].setText(f"{self.stats['attacks']:,}")
        for key, value in self.gained.items():
            self.gained_value[key].setText(_short(value))

    def _append_log(self, level: int, line: str) -> None:
        colour = BAD if level >= logging.ERROR else WARN if level >= logging.WARNING else GOOD if "→ attack" in line else TEXT
        self.log_view.appendHtml(f'<span style="color:{colour}; font-family: Consolas, monospace;">{escape(line)}</span>')

    def _theme_signal(self, *_) -> None:
        self._theme_changed()

    def closeEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        # Let go of the app-wide hooks, so a closed window can be freed.
        logging.getLogger().removeHandler(self.log_handler)
        try:
            qconfig.themeChanged.disconnect(self._theme_signal)
        except (RuntimeError, TypeError):
            pass
        self.stop()
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._autosave()
        super().closeEvent(event)


def _short(value: int) -> str:
    sign, size = ("-" if value < 0 else ""), abs(value)
    if size >= 1_000_000:
        return f"{sign}{size / 1_000_000:.1f}M"
    if size >= 1_000:
        return f"{sign}{size / 1_000:.0f}k"
    return f"{sign}{size}"


def create_app() -> QApplication:
    if sys.platform == "win32":
        # Own taskbar entry and icon, rather than grouping under python.exe.
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("COCFARM.App")
        except (AttributeError, OSError):
            pass
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(LOGO)))
    setTheme(Theme.DARK)
    setThemeColor(ACCENT)
    return app


def run_gui(config_path: str, serial: str | None = None) -> int:
    app = create_app()
    window = FarmbotWindow(config_path, serial)
    window.show()
    return app.exec()


__all__ = ["FarmbotWindow", "create_app", "run_gui"]
