"""COCFARM (farmbot) control window, built with PySide6 (Qt 6) and Fluent Widgets.

* The title bar always shows what the bot is doing, with Start/Stop.
* Pages sit in a navigation rail; every setting is a Fluent setting card
  with its explanation underneath, and that line turns into the error
  message if the value is invalid.
* Settings save automatically as soon as they are valid, and a running
  bot picks them up at its next step.

Shortcuts: F5 start/resume · Esc pause, then Esc again to stop now · Ctrl+1…9 switch page.
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
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidgetItem,
                               QSizePolicy,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, EditableComboBox, ElevatedCardWidget,
                            FluentIcon as FIF, FluentWindow, InfoBar, InfoBarPosition, LineEdit, ListWidget,
                            NavigationItemPosition, PlainTextEdit, PrimaryPushButton, PushButton, SegmentedWidget,
                            SettingCard, SettingCardGroup, SimpleCardWidget, Slider, SmoothScrollArea,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, Theme, TitleLabel, TogglePushButton,
                            setCustomStyleSheet, setTheme, setThemeColor)

from farmbot import catalog
from farmbot.config import Config, ensure_config, load_config, save_config
from farmbot.form import BY_PATH, apply_form, form_values, validate
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
GOOD, BAD, WARN, MUTED = "#6ccb5f", "#ff99a4", "#fce100", "#9da5b4"
RESOURCE_COLOURS = {"gold": "#f5c542", "elixir": "#d58cff", "dark": "#b4b4e6"}
STATES = {  # chip text colour, chip background
    "idle": ("#c5c9d0", "#3a3f47"),
    "running": ("#ffffff", "#1f6feb"),
    "attacking": ("#ffffff", "#c2410c"),
    "paused": ("#1b1b1b", "#e3b341"),
    "error": ("#ffffff", "#c42b1c"),
}
PAGES = ("Dashboard", "History", "Search", "Attack", "Village", "Clan", "Builder attack", "Builder village", "Schedule",
         "Pictures", "Settings")


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
    """A scrolling page of setting groups."""

    def __init__(self, name: str, title: str, subtitle: str = ""):
        super().__init__()
        self.setObjectName(name)
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self.layout_ = QVBoxLayout(body)
        self.layout_.setContentsMargins(36, 28, 36, 28)
        self.layout_.setSpacing(20)
        self.layout_.setAlignment(Qt.AlignTop)
        self.layout_.addWidget(TitleLabel(title))
        if subtitle:
            sub = BodyLabel(subtitle)
            sub.setWordWrap(True)  # never wider than the window, however small
            sub.setTextColor(QColor(MUTED), QColor(MUTED))
            self.layout_.addWidget(sub)
        self.setWidget(body)

    def add(self, widget: QWidget) -> QWidget:
        self.layout_.addWidget(widget)
        return widget


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
            self.contentLabel.setStyleSheet(f"color: {BAD};")
        else:
            self.setContent(self.help_text or " ")
            self.contentLabel.setStyleSheet("")

    def show_warning(self, message: str) -> None:
        self.setContent(f"⚠  {message}")
        self.contentLabel.setStyleSheet(f"color: {WARN};")


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
            painter.setPen(QColor(MUTED))
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
    label.setStyleSheet(f"color: {MUTED};")
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

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(LOGO)))
        self._sharp_title_icon()
        # Keep page names visible next to the icons (recognition over recall).
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setMinimumExpandWidth(900)
        self.resize(1160, 780)
        self.setMinimumSize(980, 640)
        self._build_title_bar()
        self.pages: dict[str, QWidget] = {}
        self._add_page(self._dashboard(), FIF.HOME, "Dashboard")
        self._add_page(self._history(), FIF.HISTORY, "History")
        nav = self.navigationInterface
        nav.addItemHeader("Home village")
        self._add_page(self._search(), FIF.SEARCH, "Search")
        self._add_page(self._attack(), FIF.GAME, "Attack")
        self._add_page(self._army(), FIF.HOME_FILL if hasattr(FIF, "HOME_FILL") else FIF.LEAF, "Village")
        self._add_page(self._clan(), FIF.PEOPLE, "Clan")
        nav.addItemHeader("Builder base")
        self._add_page(self._builder(), FIF.GAME, "Attack", key="Builder attack")
        self._add_page(self._builder_village(), FIF.HOME_FILL if hasattr(FIF, "HOME_FILL") else FIF.LEAF, "Village",
                       key="Builder village")
        nav.addItemHeader("General")
        self._add_page(self._schedule(), FIF.DATE_TIME, "Schedule")
        self._add_page(self._pictures(), FIF.PHOTO, "Pictures")
        self._add_page(self._device(), FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM)
        self.navigationInterface.expand(useAni=False)

        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._autosave)
        self._load()
        for binding in self.bindings.values():
            binding.connect(lambda *_: self._changed())
        self._changed(save=False)

        handler = _QueueLogHandler(self.events)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self.poll_timer = QTimer(self, interval=100, timeout=self._poll)
        self.poll_timer.start()
        self.clock_timer = QTimer(self, interval=1000, timeout=self._tick)
        self.clock_timer.start()
        QShortcut(QKeySequence("F5"), self, activated=self.start)
        if self.first_run:
            QTimer.singleShot(300, self._welcome)
        self._check_for_update()
        QShortcut(QKeySequence("Esc"), self, activated=self.stop)
        for i, name in enumerate(PAGES[:9], start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, activated=lambda n=name: self.show_page(n))

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
        self._set_state("idle", "Ready. Check your settings, then press Start.")

    def _add_page(self, page: QWidget, icon, text: str, position=NavigationItemPosition.TOP,
                  key: str | None = None) -> None:
        self.pages[key or text] = page
        self.addSubInterface(page, icon, text, position)

    def show_page(self, name: str) -> None:
        self.switchTo(self.pages[name])

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
        return "Missing pictures (add them on the Pictures page): " + ", ".join(missing)

    def library(self) -> TemplateLibrary:
        return TemplateLibrary(self.config.bot.templates)

    def _note(self, page: Page, text: str, colour: str = WARN) -> None:
        card = SimpleCardWidget()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        label = BodyLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {colour};")
        layout.addWidget(label)
        page.add(card)

    # ------------------------------------------------------------ dashboard
    def _stat(self, title: str, colour: str | None = None, big: bool = True) -> tuple[ElevatedCardWidget, QLabel, QLabel]:
        card = ElevatedCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(4)
        head = StrongBodyLabel(title)
        if colour:
            head.setStyleSheet(f"color: {colour};")
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
            label.setStyleSheet(f"color: {RESOURCE_COLOURS[key]};")
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

    def _dashboard(self) -> Page:
        page = Page("dashboard", "Dashboard", "The base being scouted, this session's totals, and what the bot is doing.")
        farm = page.add(SettingCardGroup("What to farm"))
        self.segments(farm, "bot.mode", FIF.GAME, "Mode",
                      "Home village: search for loot and attack (Home village pages). Builder base: two-stage "
                      "builder battles (Builder base pages).", {"home": "Home village", "builder": "Builder base"},
                      width=130)
        # The builder panel is shorter than the page: keep this group from stretching into the gap.
        farm.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Home village panel (unchanged); a builder base panel replaces it in builder mode.
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
        self.live_view.setToolTip("The last screen the bot scouted. Boxes show where it reads the loot.")
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
        for key, name, col in (("bases", "Bases scouted", 0), ("attacks", "Attacks", 1), ("runtime", "Running for", 4)):
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

        activity = QHBoxLayout()
        activity.addWidget(SubtitleLabel("Activity"))
        activity.addStretch(1)
        clear = PushButton(FIF.DELETE, "Clear")
        clear.clicked.connect(lambda: self.log_view.clear())
        activity.addWidget(clear)
        page.layout_.addLayout(activity)
        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        self.log_view.setPlaceholderText("Nothing yet. Press Start (F5) and the bot's progress will appear here.")
        page.add(self.log_view)
        return page

    # ------------------------------------------------------------ history
    HISTORY_DAYS = 14  # days in the chart
    HISTORY_ROWS = 300  # latest attacks in the table

    def _history(self) -> Page:
        page = Page("history", "History", "Every attack from your history file: totals, loot per hour, the last "
                    "two weeks, and each attack.")
        top = QHBoxLayout()
        top.addStretch(1)
        refresh = PushButton(FIF.SYNC, "Refresh")
        refresh.clicked.connect(self._show_history)
        open_file = PushButton(FIF.DOCUMENT, "Open the CSV")
        open_file.setToolTip("Opens the history file in your spreadsheet app (Excel, Google Sheets import...)")
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
        self.history_chart.setTheme(QChart.ChartThemeDark)
        self.history_chart.setBackgroundVisible(False)
        self.history_chart.legend().setAlignment(Qt.AlignBottom)
        self.history_chart.setTitle(f"Loot gained per day (last {self.HISTORY_DAYS} days)")
        view = QChartView(self.history_chart)
        view.setRenderHint(QPainter.Antialiasing)
        view.setMinimumHeight(300)
        view.setStyleSheet("background: transparent;")
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

        chart = self.history_chart
        chart.removeAllSeries()
        for axis in chart.axes():
            chart.removeAxis(axis)
        span = [today - timedelta(days=i) for i in range(self.HISTORY_DAYS - 1, -1, -1)]
        series = QBarSeries()
        top = 0.0
        for key, name in (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir ×100")):
            bars = QBarSet(name)
            bars.setColor(QColor(RESOURCE_COLOURS[key]))
            for day in span:
                value = getattr(days.get(day, (0, Loot(0, 0, 0)))[1], key)
                value = value * 100 if key == "dark" else value  # dark is ~100x scarcer: same scale
                value /= 1_000_000  # the axis is in millions
                bars.append(max(value, 0))
                top = max(top, value)
            series.append(bars)
        chart.addSeries(series)
        x = QBarCategoryAxis()
        x.append([str(d.day) for d in span])  # day of the month: fits at the window's smallest
        x.setTitleText(f"{span[0]:%b %d} – {span[-1]:%b %d}")
        y = QValueAxis()
        y.setRange(0, max(top, 0.1) * 1.1)
        y.setLabelFormat("%.1f")
        y.setTitleText("Millions")
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
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
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
        colours = {"Waiting": (MUTED, "rgba(255,255,255,0.06)"), "First base": ("#ffffff", "#c2410c"),
                   "Second base": ("#0b2e13", GOOD), "Done": ("#0b2e13", GOOD)}
        fg, bg = colours.get(text, (MUTED, "rgba(255,255,255,0.06)"))
        self.bb_stage.setText(text)
        self.bb_stage.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 14px; padding: 6px 18px; font-weight: 700;")

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
    def _search(self) -> Page:
        page = Page("search", "Search", "Which bases are worth attacking. Amounts accept 500k, 1.5m or 1,500,000.")
        loot = page.add(SettingCardGroup("Minimum loot"))
        self.amount(loot, "search.min_gold", FIF.MARKET, "Gold", "Skip bases offering less gold than this. 0 ignores gold.")
        self.amount(loot, "search.min_elixir", FIF.MARKET, "Elixir", "0 ignores elixir.")
        self.amount(loot, "search.min_dark", FIF.MARKET, "Dark elixir", "0 ignores dark elixir.")
        self.switch(loot, "search.match_any", FIF.FILTER, "Any one is enough",
                    "On: attack when any one minimum is met. Off: all of them must be met.")
        self.amount(loot, "search.min_total", FIF.MARKET, "And in total at least",
                    "Gold + elixir + dark elixir (weighted below) together. 0 ignores the total.")
        self.number(loot, "search.dark_weight", FIF.MARKET, "Dark elixir is worth",
                    "In the total, each dark elixir counts as this much gold or elixir.", "×")

        filters = page.add(SettingCardGroup("Filters"))
        self.switch(filters, "search.dead_base", FIF.LEAF, "Dead bases only",
                    "Needs pictures of full collectors in templates/deadbase/.")
        self.number(filters, "search.dead_base_collectors", FIF.TILES, "Full collectors needed", "How many must be visible.")
        self.depends("search.dead_base", "search.dead_base_collectors")
        self.number(filters, "search.max_searches", FIF.SYNC, "Give up after", "Bases to skip before starting over.", "bases")
        self.switch(filters, "search.sound_on_match", FIF.RINGER, "Sound on match", "Play a sound when a base is worth attacking.")

        before = page.add(SettingCardGroup("Before searching"))
        self.amount(before, "search.min_own_gold", FIF.SHOPPING_CART, "Don't search while my gold is below",
                    "Each Next costs gold. 0 always searches.")
        stop = page.add(SettingCardGroup("Stop when storage is full"))
        icons = QWidget()
        row = QHBoxLayout(icons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        buttons = {}
        for key, name in (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark")):
            b = TogglePushButton(QIcon(str(ASSETS / f"{key}.png")), f"  {name}")
            b.setMinimumWidth(104)
            b.setIconSize(QSize(28, 28))
            b.setMinimumHeight(44)
            b.setToolTip(f"Stop once your {name.lower()} storage is full")
            row.addWidget(b)
            buttons[key] = b
        self._bind("bot.stop_when_full", bind_toggles(buttons))
        card = self._card(stop, "bot.stop_when_full", FIF.POWER_BUTTON, "Stop when these are full",
                          "Pick the resources you farm for. The bot stops once every one you picked is full "
                          "(none picked: it never stops for this).", icons)
        card.setFixedHeight(96)
        self.switch(before, "search.maintain_only", FIF.HOME, "Only look after the village",
                    "Collect, train and donate, but never search or attack.")
        return page

    def _attack(self) -> Page:
        page = Page("attack", "Attack", "How the army is deployed and when the battle ends.")
        deploy = page.add(SettingCardGroup("Deployment"))
        grid = QWidget()
        g = QGridLayout(grid)
        g.setContentsMargins(0, 6, 0, 6)
        g.setSpacing(6)
        buttons = {}
        for side, (r, c, label) in {"nw": (0, 0, "↖  NW"), "ne": (0, 1, "NE  ↗"),
                                    "sw": (1, 0, "↙  SW"), "se": (1, 1, "SE  ↘")}.items():
            b = TogglePushButton(label)
            b.setFixedWidth(96)
            g.addWidget(b, r, c)
            buttons[side] = b
        self._bind("attack.sides", bind_toggles(buttons))
        card = self._card(deploy, "attack.sides", FIF.ZOOM, "Deploy from",
                          "Laid out like the map. Troops spread over the sides you turn on.", grid)
        card.setFixedHeight(110)
        self.segments(deploy, "attack.waves", FIF.TILES, "Waves", "Split each troop type into this many waves.", ["1", "2", "3"])
        self.speed(deploy, "attack.troop_speed", FIF.SPEED_HIGH, "Troop speed", "Pause between single troops.")
        self.speed(deploy, "attack.wave_speed", FIF.SPEED_MEDIUM, "Wave speed", "Pause between waves.")
        self.switch(deploy, "redline.enabled", FIF.HIGHTLIGHT, "Follow the red line",
                    "The thin orange boundary the game draws around the base.")
        self.switch(deploy, "zone.enabled", FIF.LAYOUT, "Otherwise deploy around the buildings",
                    "Off: use the fixed deploy lines from the config file.")

        self.segments(deploy, "attack.spell_placement", FIF.ALBUM, "Spells",
                      "One spot: every spell at once on the middle of the base. Spread: over the base.",
                      {"together": "One spot", "spread": "Spread"}, width=90)

        heroes = page.add(SettingCardGroup("Heroes and Clan Castle"))
        self.switch(heroes, "attack.use_cc", FIF.PEOPLE, "Use Clan Castle troops",
                    "To leave them out, add templates/cards/clan_castle.png.")
        self.switch(heroes, "attack.hero_abilities", FIF.CALORIES, "Activate hero abilities")
        self.number(heroes, "attack.hero_ability_delay", FIF.STOP_WATCH, "Ability delay", "Seconds after the heroes land.", "s")
        self.number(heroes, "attack.ability_health", FIF.HEART, "Or when health drops to",
                    "Fire each hero's ability once it's this hurt instead of after the delay. 0% uses the delay.", "%")
        self.number(heroes, "attack.ability_max_wait", FIF.HISTORY, "At the latest after",
                    "Heroes still unhurt by then fire anyway.", "s")
        self.depends("attack.hero_abilities", "attack.hero_ability_delay", "attack.ability_health",
                     "attack.ability_max_wait")

        end = page.add(SettingCardGroup("Ending the battle"))
        self.switch(end, "attack.speed_up", FIF.SPEED_HIGH, "Play the last minute at 4x",
                    "Presses the 1x button when it shows, with a minute of the battle left.")
        self.switch(end, "attack.end_when_no_loot", FIF.CANCEL, "End early when no loot is gained")
        self.number(end, "attack.no_loot_seconds", FIF.STOP_WATCH, "No loot for", "Seconds without the loot dropping.", "s")
        self.depends("attack.end_when_no_loot", "attack.no_loot_seconds")
        self.number(end, "attack.end_at_loot_share", FIF.ACCEPT, "End once this much of the loot is taken",
                    "Share of the base's loot (as scouted) to take before leaving. 0 waits for the other rules.", "%")
        return page

    def _builder(self) -> Page:
        page = Page("builder", "Builder base attack", "Two-stage builder battles: the hero goes in first at the far "
                    "corner, the troops follow, and abilities fire on a timer. Turn this mode on from the Dashboard.")
        deploy = page.add(SettingCardGroup("Deploying"))
        self.segments(deploy, "builder.corner", FIF.ZOOM, "Go in from",
                      "Auto picks the map corner farthest from the enemy buildings.",
                      {"auto": "Auto", "left": "Left", "top": "Top", "right": "Right"}, width=80)
        self.switch(deploy, "builder.use_hero", FIF.PEOPLE, "Hero goes first", "Off: troops only.")
        self.number(deploy, "builder.troop_delay", FIF.STOP_WATCH, "Troops follow after",
                    "Seconds after the hero lands.", "s")
        self.segments(deploy, "builder.first_hero", FIF.PEOPLE, "First hero",
                      "The hero for the first base. Keep: whichever is set in the game.",
                      {"any": "Keep", "machine": "Battle Machine", "copter": "Battle Copter"}, width=120)
        self.switch(deploy, "builder.switch_hero", FIF.SYNC, "Switch hero when hurt",
                    "Before a base, swap to your other builder hero if this one's health is low.")
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
        self.number(abilities, "builder.ability_batch", FIF.TILES, "Cards per batch", "Abilities fired together.")
        self.number(abilities, "builder.ability_delay", FIF.STOP_WATCH, "Between batches",
                    "Seconds from one batch to the next.", "s")
        self.depends("builder.troop_abilities", "builder.ability_batch", "builder.ability_delay")

        battle = page.add(SettingCardGroup("Battle"))
        self.switch(battle, "builder.speed_up", FIF.SPEED_HIGH, "Play at 4x speed",
                    "Presses the 1x button whenever it shows.")
        self.number(battle, "builder.stage_delay", FIF.STOP_WATCH, "Wait before the second base",
                    "Seconds after the first base falls, while the second loads.", "s")
        self.switch(battle, "builder.return_home_after", FIF.HOME, "Sail home when the bot stops",
                    "Ends in the home village rather than the builder base.")
        return page

    def _builder_village(self) -> Page:
        page = Page("builder_village", "Builder base village", "Looking after the builder base between battles.")
        loot = page.add(SettingCardGroup("Collecting"))
        self.switch(loot, "builder.collect_resources", FIF.BASKETBALL, "Collect the gold mines, elixir collectors and gem mine",
                    "Before each builder battle.")
        self.switch(loot, "builder.collect_cart", FIF.SHOPPING_CART, "Collect the elixir cart",
                    "The cart by the dock that holds your defense rewards.")
        self.number_range(loot, "builder.cart_every_min", "builder.cart_every_max", FIF.SYNC, "Every",
                          "A random number of battles in this range each time. Same number twice = fixed.",
                          "battles")
        self.depends("builder.collect_cart", "builder.cart_every_min", "builder.cart_every_max")

        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "builder.walls", FIF.UP, "Upgrade builder base walls with spare loot",
                    "Before each battle: Build → the cheapest wall → Upgrade. Off by default: it spends.")
        self.segments(walls, "builder.walls_use", FIF.MARKET, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.amount(walls, "builder.walls_min_gold", FIF.SAVE, "Keep at least this much gold",
                    "Walls only get what's above it.")
        self.amount(walls, "builder.walls_min_elixir", FIF.SAVE, "Keep at least this much elixir",
                    "Walls only get what's above it.")
        self.number(walls, "builder.walls_per_round", FIF.TILES, "Walls per round", "Upgraded between two battles at most.")
        self.depends("builder.walls", "builder.walls_use", "builder.walls_min_gold", "builder.walls_min_elixir",
                     "builder.walls_per_round")
        return page

    def _army(self) -> Page:
        page = Page("village", "Village", "Looking after the home village between attacks.")
        village = page.add(SettingCardGroup("Village"))
        self.switch(village, "bot.collect_resources", FIF.BASKETBALL, "Collect resources",
                    "Empties full collectors between attacks: one tap on a bubble collects them all.")
        self.switch(village, "bot.collect_gold", FIF.MARKET, "Gold", "From the gold mines.")
        self.switch(village, "bot.collect_elixir", FIF.MARKET, "Elixir", "From the elixir collectors.")
        self.switch(village, "bot.collect_dark", FIF.MARKET, "Dark elixir", "From the dark elixir drills.")
        self.depends("bot.collect_resources", "bot.collect_gold", "bot.collect_elixir", "bot.collect_dark")
        self.switch(village, "bot.collect_loot_cart", FIF.SHOPPING_CART, "Collect the loot cart",
                    "Taps the loot cart and its Collect button when it shows \"Collect\".")
        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "walls.enabled", FIF.UP, "Upgrade walls with spare loot",
                    "Between attacks: Build → the cheapest suggested wall → Upgrade → Confirm. Off by default: it spends.")
        self.segments(walls, "walls.use", FIF.MARKET, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.amount(walls, "walls.min_gold", FIF.SAVE, "Keep at least this much gold", "Walls only get what's above it.")
        self.amount(walls, "walls.min_elixir", FIF.SAVE, "Keep at least this much elixir", "Walls only get what's above it.")
        self.number(walls, "walls.max_per_round", FIF.TILES, "Walls per round", "Upgraded between two attacks at most.")
        self.depends("walls.enabled", "walls.use", "walls.min_gold", "walls.min_elixir", "walls.max_per_round")
        self._note(page, self._template_note("Collecting resources", "Collecting the loot cart", "Upgrading walls"))
        return page

    def _clan(self) -> Page:
        page = Page("clan", "Clan", "Fill clan requests and ask for Clan Castle troops.")
        folder = Path(self.config.bot.templates) / "donate"
        names = sorted(p.stem for p in folder.glob("*.png")) if folder.is_dir() else []
        donate = page.add(SettingCardGroup("Donations"))
        self.switch(donate, "clan.donate", FIF.HEART, "Donate troops", "Gives the units below to every open request in clan chat.")
        for path, title, icon in (("clan.donate_troop", "Troop", FIF.PEOPLE), ("clan.donate_siege", "Siege machine", FIF.CAR),
                                  ("clan.donate_spell", "Spell", FIF.PALETTE)):
            w = EditableComboBox()
            w.addItems(names)
            w.setFixedWidth(200)
            w.setPlaceholderText("picture name, e.g. yeti")
            self._bind(path, bind_combo(w))
            self._card(donate, path, icon, title, "From templates/donate/<name>.png.", w)
        self.number(donate, "clan.max_donations_per_request", FIF.ADD, "Up to", "Units per request.", "units")
        self.depends("clan.donate", "clan.donate_troop", "clan.donate_siege", "clan.donate_spell", "clan.max_donations_per_request")

        request = page.add(SettingCardGroup("Requests"))
        self.switch(request, "clan.request_troops", FIF.SEND, "Request Clan Castle troops",
                    "Clan chat → Request → Send, then again as soon as the game's cooldown runs out.")
        self.number(request, "clan.request_every_minutes", FIF.HISTORY, "If the cooldown can't be read, wait",
                    "The bot normally reads the timer on the Request button.", "min")
        self.depends("clan.request_troops", "clan.request_every_minutes")
        self._note(page, self._template_note("Donating", "Requesting troops"))
        return page

    def _schedule(self) -> Page:
        page = Page("schedule", "Schedule", "When the bot may play, and how much.")
        hours_group = page.add(SettingCardGroup("Active hours"))
        self.switch(hours_group, "schedule.enabled", FIF.CALENDAR, "Use a schedule", "Only play during the hours selected below (local time).")
        grid_card = SimpleCardWidget()
        grid_card.setFixedHeight(150)
        grid = QGridLayout(grid_card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setSpacing(6)
        hour_buttons = {}
        for h in range(24):
            b = TogglePushButton(f"{h:02d}")
            b.setFixedWidth(64)
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
        self.hours_error.setStyleSheet(f"color: {BAD};")
        grid.addWidget(self.hours_error, 3, 0, 1, 12)
        grid.setColumnStretch(12, 1)
        hours_group.addSettingCard(grid_card)
        self.depends("schedule.enabled", "schedule.active_hours")

        limits = page.add(SettingCardGroup("Limits"))
        self.number(limits, "schedule.daily_attack_limit", FIF.FLAG, "Attacks per day", "0 = no limit. Resets at midnight.")
        self.number(limits, "bot.max_attacks", FIF.POWER_BUTTON, "Stop after", "Attacks this session. 0 = until you press Stop.")

        breaks = page.add(SettingCardGroup("Breaks"))
        self.switch(breaks, "schedule.breaks", FIF.CAFE, "Take breaks",
                    "Rest now and then, at random, to play more like a person.")
        self.number_range(breaks, "schedule.break_every_min", "schedule.break_every_max", FIF.SYNC,
                          "Every", "A new random number after each break.", "attacks")
        self.number_range(breaks, "schedule.break_minutes_min", "schedule.break_minutes_max", FIF.STOP_WATCH,
                          "For", "Minutes, picked at random.", "min")
        self.depends("schedule.breaks", "schedule.break_every_min", "schedule.break_minutes_min")
        return page

    def _pictures(self) -> Page:
        page = Page("pictures", "Pictures", "The bot finds buttons by comparing the screen with small pictures of them. "
                    "Pick one, show the screen it appears on, and drag a box tightly around its text.")
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
        return page

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
            item.setForeground(QColor(GOOD if have else BAD if picture.required else WARN if recommended else MUTED))
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
            self.picture_status.setText(f"✓ In place: {paths[0].relative_to(library.root).as_posix()}{extra}. "
                                        "Saving a new one replaces it." if not picture.folder else
                                        f"✓ {len(paths)} picture(s) in {picture.name}/. Saving adds another.")
            self.picture_status.setStyleSheet(f"color: {GOOD};")
        else:
            self.picture_preview.clear()
            if picture.required:
                text, colour = "✗ Not added yet.", BAD
            elif picture.feature == catalog.QUICK_EXIT:
                text, colour = "! Recommended: until it's added, the bot closes the game to leave each battle.", WARN
            else:
                text, colour = "○ Not added (optional).", MUTED
            self.picture_status.setText(text)
            self.picture_status.setStyleSheet(f"color: {colour};")
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
            self.crop_info.setStyleSheet(f"color: {MUTED};")
            return
        r = self.canvas.selection()
        lookalike = catalog.lookalike_score(self.canvas.image, crop)
        risky = lookalike >= self.config.bot.threshold - 0.05
        self.crop_info.setText(f"Box {r.w}×{r.h} at ({r.x}, {r.y}). "
                               + (f"⚠ Something else on screen looks {lookalike:.0%} similar: crop tighter."
                                  if risky else f"✓ Unique on this screen (next best {max(lookalike, 0):.0%}).")
                               + ("  Enter a name first." if needs_name else ""))
        self.crop_info.setStyleSheet(f"color: {WARN if risky or needs_name else GOOD};")

    def _capture_screen(self) -> None:
        if not self._autosave():
            return
        self.capture_button.setEnabled(False)
        self.crop_info.setText("Taking a screenshot…")
        self.crop_info.setStyleSheet(f"color: {MUTED};")

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
            InfoBar.error("Can't open that file", path, duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        vision = Vision(self.library(), self.config.bot.threshold)
        self.canvas.set_image(normalize_screen(image, vision, self.config.device.ui_scale))

    def _show_screen(self, result) -> None:
        self.capture_button.setEnabled(True)
        if isinstance(result, str):
            self.crop_info.setText(f"✗ {result}")
            self.crop_info.setStyleSheet(f"color: {BAD};")
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
            InfoBar.error("Couldn't save", str(exc), duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        InfoBar.success("Picture saved", path.as_posix(), duration=2500, position=InfoBarPosition.TOP_RIGHT, parent=self)
        self.canvas.select(None)
        self._refresh_pictures(keep=picture.name)

    def _device(self) -> Page:
        page = Page("settings", "Settings", "The emulator running the game: connecting to it and setting it up.")
        group = page.add(SettingCardGroup("Connection"))
        self.segments(group, "device.mode", FIF.CONNECT if hasattr(FIF, "CONNECT") else FIF.WIFI, "Connect to",
                      "Which emulator plays the game. COCFARM finds it and sets it up by itself.",
                      {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer"}, width=110)
        self.bluestacks_info = BodyLabel("")
        self.bluestacks_info.setWordWrap(True)
        self.bluestacks_info.setMinimumWidth(240)
        self.bluestacks_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        refresh = PushButton(FIF.SYNC, "Find")
        refresh.setToolTip("Look for BlueStacks and LDPlayer again")
        refresh.clicked.connect(self._find_bluestacks)
        info_card = self._card(group, None, FIF.INFO, "What COCFARM found", "", self.bluestacks_info, refresh)
        info_card.setFixedHeight(96)

        bs = self.bluestacks_group = page.add(SettingCardGroup("BlueStacks 5"))
        self.instance_box = EditableComboBox()
        self.instance_box.setFixedWidth(260)
        self.instance_box.setPlaceholderText("Main instance")
        self._bind("device.bluestacks_instance", bind_combo(self.instance_box))
        self._card(bs, "device.bluestacks_instance", FIF.APPLICATION, "Instance",
                   "Which BlueStacks instance plays the game. Empty uses the main one.", self.instance_box)
        self.depends("device.mode", "device.bluestacks_instance", when="bluestacks")

        ld = self.ldplayer_group = page.add(SettingCardGroup("LDPlayer"))
        self.ldplayer_box = EditableComboBox()
        self.ldplayer_box.setFixedWidth(260)
        self.ldplayer_box.setPlaceholderText("Newest installed")
        self._bind("device.ldplayer", bind_combo(self.ldplayer_box))
        self._card(ld, "device.ldplayer", FIF.APPLICATION, "Version",
                   "Which LDPlayer to use (9 or newer). Empty uses the newest one installed.", self.ldplayer_box)
        self.number(ld, "device.ldplayer_index", FIF.TILES, "Instance number",
                    "0 is the first instance in LDMultiPlayer.")
        self.depends("device.mode", "device.ldplayer", "device.ldplayer_index", when="ldplayer")

        setup = page.add(SettingCardGroup("Emulator setup"))
        self.switch(setup, "device.launch_emulator", FIF.PLAY, "Start the emulator automatically",
                    "Opens it when you press Start if it isn't running.")
        self.switch(setup, "device.enable_adb", ICON_TOOLS, "Turn on ADB",
                    "Needed to play in the background. If it's off, COCFARM turns it on (the emulator restarts once).")
        self.switch(setup, "device.set_display", ICON_SCREEN, "Set the display",
                    "860 x 672 at 240 DPI, the size COCFARM is made for. Applied when it connects; the emulator "
                    "restarts once if it had another size.")
        self.number(setup, "device.display_width", ICON_SCREEN, "Width", "", "px")
        self.number(setup, "device.display_height", ICON_SCREEN, "Height", "", "px")
        dpi = ComboBox()
        dpi.setFixedWidth(150)
        self._bind("device.display_dpi", bind_choice(dpi, {str(d): str(d) for d in DPI_CHOICES}))
        self._card(setup, "device.display_dpi", ICON_SCREEN, "DPI", "240 is what COCFARM is trained on.", dpi)
        self.depends("device.set_display", "device.display_width", "device.display_height", "device.display_dpi")
        self.switch(setup, "device.set_resources", FIF.SPEED_OFF, "Use as little of the PC as possible",
                    "Gives the emulator only the cores and memory below.")
        self.segments(setup, "device.cpus", ICON_TOOLS, "CPU cores", "2 is enough for the game.", ["2", "3", "4"])
        memory = ComboBox()
        memory.setFixedWidth(150)
        self._bind("device.memory", bind_choice(memory, {str(mb): f"{mb} MB" for mb in MEMORY_CHOICES}))
        self._card(setup, "device.memory", ICON_TOOLS, "Memory", "2048 MB is enough for the game.", memory)
        self.depends("device.set_resources", "device.cpus", "device.memory")
        self.depends("device.mode", "device.launch_emulator", "device.enable_adb", "device.set_display",
                     "device.set_resources", when=("bluestacks", "ldplayer"))

        check = page.add(SettingCardGroup("Check"))
        test = PrimaryPushButton(FIF.SYNC, "Test connection")
        test.clicked.connect(self._test_connection)
        self.device_result = CaptionLabel("")
        self._card(check, None, FIF.PHONE, "Connect now",
                   "Connects to the emulator chosen above (starting it if needed) and checks Clash of Clans.",
                   self.device_result, test)
        self.game_installed = CaptionLabel("Not checked yet")
        self.game_logged_in = CaptionLabel("Not checked yet")
        for label in (self.game_installed, self.game_logged_in):
            label.setStyleSheet(f"color: {MUTED};")
        self._card(check, None, FIF.DOWNLOAD, "Clash of Clans downloaded",
                   "Install it from the Play Store inside the emulator.", self.game_installed)
        self._card(check, None, FIF.PEOPLE, "Logged in",
                   "Open the game once and sign in to your account; COCFARM can't do this part.", self.game_logged_in)
        see = PushButton(FIF.VIEW, "What the bot sees")
        see.clicked.connect(self._inspect_screen)
        self.see_button = see
        self._card(check, None, FIF.SEARCH, "Check the bot's vision",
                   "Show a base being scouted in the game, then press: shows the loot, troop cards and deploy "
                   "points it finds, without attacking.", see)
        self.seen_text = BodyLabel("")
        self.seen_text.setWordWrap(True)
        self.seen_view = QLabel()
        self.seen_view.setAlignment(Qt.AlignCenter)
        for w in (self.seen_text, self.seen_view):
            w.hide()
            page.add(w)

        notify = page.add(SettingCardGroup("Notifications"))
        self.text(notify, "notify.discord_webhook", FIF.MESSAGE, "Discord webhook",
                  "Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.", width=320)
        self.switch(notify, "notify.on_storage_full", FIF.FLAG, "When storage is full")
        self.switch(notify, "notify.on_stopped", FIF.POWER_BUTTON, "When the bot stops", "After a run, or by an error.")
        self.switch(notify, "notify.on_emulator_restart", FIF.SYNC, "When the emulator is restarted")
        self.switch(notify, "notify.daily_summary", FIF.CALENDAR, "Daily summary",
                    "Yesterday's attacks and loot, sent after midnight.")
        test_notify = PushButton(FIF.SEND, "Send a test")
        test_notify.clicked.connect(self._test_notify)
        self.notify_result = CaptionLabel("")
        self._card(notify, None, FIF.CHAT, "Test", "Sends a message to the webhook above.",
                   self.notify_result, test_notify)
        self._find_bluestacks()
        return page

    def _welcome(self) -> None:
        """First run: open Settings and say what to do, with a button for the first step."""
        self.show_page("Settings")
        bar = InfoBar.info(
            "Welcome to COCFARM",
            "1. Pick your emulator and press Connect now (COCFARM sets it up). 2. Open Clash of Clans and sign in. "
            "3. Set your loot minimums on Search. 4. Press Start.",
            duration=-1, position=InfoBarPosition.TOP, parent=self)
        go = PrimaryPushButton("Connect now")
        go.clicked.connect(lambda: (self._test_connection(), bar.close()))
        bar.addWidget(go)

    def _check_for_update(self) -> None:
        repo = self.config.bot.update_repo
        if not repo:
            return

        def work() -> None:
            from farmbot.update import newer_release

            release = newer_release(repo)
            if release:
                self.events.put(("update", release))

        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, release) -> None:
        import webbrowser

        bar = InfoBar.success("Update available", f"COCFARM {release.version} is out.", duration=-1,
                              position=InfoBarPosition.TOP_RIGHT, parent=self)
        get = PushButton(FIF.DOWNLOAD, "Download")
        get.clicked.connect(lambda: webbrowser.open(release.url))
        bar.addWidget(get)

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
            self.seen_text.setStyleSheet(f"color: {BAD};")
            self.seen_view.hide()
            return
        lines, overlay = result
        self.seen_text.setText("\n".join(lines))
        self.seen_text.setStyleSheet("")
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
            lines.append("BlueStacks 5: not installed.")
        else:
            self.instance_box.addItems([i.display_name for i in install.instances])
            lines.append(f"BlueStacks 5: {install.install_dir} - instances: "
                         + (", ".join(i.display_name for i in install.instances) or "none"))
        self.instance_box.setText(current)
        installs = ldplayer.find_installs()
        current = self.ldplayer_box.currentText()
        self.ldplayer_box.clear()
        self.ldplayer_box.addItems([i.name for i in installs])
        self.ldplayer_box.setText(current)
        lines.append("LDPlayer: " + (", ".join(f"{i.name} ({i.path})" for i in installs) or "not installed."))
        self.bluestacks_info.setText(chr(10).join(lines))
        self.bluestacks_info.setStyleSheet(f"color: {WARN};" if install is None and not installs else "")

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
        form = self.collect()
        _, errors = validate(form)
        self.has_errors = bool(errors)
        for path, card in self.cards.items():
            card.show_error(errors.get(path))
        self.hours_error.setText(f"⚠  {errors['schedule.active_hours']}" if "schedule.active_hours" in errors else "")
        for path, trained in TRAINED_DISPLAY.items():
            card = self.cards.get(path)
            if card is not None and path not in errors and str(form.get(path)).strip() != str(trained):
                card.show_warning(TRAINED_NOTE)
        emulator = form.get("device.mode")
        self.bluestacks_group.setVisible(emulator == "bluestacks")
        self.ldplayer_group.setVisible(emulator == "ldplayer")
        builder_mode = form.get("bot.mode") == "builder"
        self.home_panel.setVisible(not builder_mode)
        self.builder_panel.setVisible(builder_mode)
        for controller, bindings, when in self.dependents:
            on = (bool(form.get(controller)) if when is True
                  else form.get(controller) in when if isinstance(when, tuple) else form.get(controller) == when)
            for b in bindings:
                b.enable(on)
        self.start_button.setEnabled(not errors and (not self._running() or self.pause_event.is_set()))
        if errors:
            self.save_label.setText(f"⚠ Fix {len(errors)} setting{'s' if len(errors) > 1 else ''}")
            self.save_label.setStyleSheet(f"color: {BAD};")
            self.save_timer.stop()
        elif save:
            self.save_label.setText("Saving…")
            self.save_label.setStyleSheet(f"color: {MUTED};")
            self.save_timer.start()

    def _autosave(self) -> bool:
        try:
            apply_form(self.config, self.collect())
        except ValueError:
            self._changed(save=False)
            return False
        save_config(self.config, self.config_path)
        self.save_label.setText("✓ Saved")
        self.save_label.setStyleSheet(f"color: {MUTED};")
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
                self._set_state("running", "Resuming…")
            return
        if not self._autosave():
            InfoBar.error("Can't start yet", "Some settings need fixing first.", duration=3000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        attacking = not self.config.search.maintain_only
        missing = catalog.missing(self.library(), required_only=True) if attacking else []
        if missing:
            self._pictures_bar(InfoBar.error, "Pictures missing", "The bot can't attack without: "
                               + ", ".join(p.title for p in missing) + ".")
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
        self._set_state("running", "Starting…")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
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
            self._set_state("paused", "Pausing after the current action…")
        else:
            self.stop_event.set()
            self.stop_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self._set_state("paused", "Stopping now…")

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
        bar = kind(title, text, duration=8000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        go = PushButton("Open Pictures")
        go.clicked.connect(lambda: (self.show_page("Pictures"), bar.close()))
        bar.addWidget(go)

    def _run_bot(self, stop_event: threading.Event) -> None:
        from farmbot.bot import Bot
        from farmbot.bluestacks import open_device
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
        self.device_result.setText("Connecting…")
        self.device_result.setStyleSheet(f"color: {MUTED};")

        name = {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer"}.get(self.config.device.mode, "the device")
        for label in (self.game_installed, self.game_logged_in):
            label.setText("Checking…")
            label.setStyleSheet(f"color: {MUTED};")

        def work() -> None:
            from farmbot import gamecheck
            from farmbot.bluestacks import open_device
            from farmbot.game import Game
            from farmbot.vision import Vision

            try:
                device = open_device(self.config.device)
                h, w = device.screenshot().shape[:2]
                self.events.put(("device", (True, f"✓ Connected to {name} · {w}×{h}")))
            except Exception as exc:
                self.events.put(("device", (False, f"✗ {exc}")))
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

    def _show_game(self, installed: bool | None, logged_in: bool | None) -> None:
        def show(label: QLabel, state: bool | None, yes: str, no: str, pending: str) -> None:
            text, colour = (pending, MUTED) if state is None else (yes, GOOD) if state else (no, BAD)
            label.setText(text)
            label.setStyleSheet(f"color: {colour};")

        show(self.game_installed, installed, "✓ Installed", "✗ Not installed", "Couldn't check")
        if installed is False:
            show(self.game_logged_in, False, "", "✗ Install the game first", "")
        else:
            show(self.game_logged_in, logged_in, "✓ Logged in (the village loaded)",
                 "✗ Not logged in, or the game is stuck on a screen", "Checking…" if installed else "Couldn't check")

    # ------------------------------------------------------------ events
    def _set_state(self, state: str, text: str) -> None:
        fg, bg = STATES[state]
        self.state_chip.setText(state.capitalize())
        self.state_chip.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 11px; padding: 3px 12px; font-weight: 600;")
        self.status_label.setText(text)

    def _verdict(self, kind: str) -> None:
        text, fg, bg = {"wait": ("Waiting for a base", MUTED, "rgba(255,255,255,0.06)"),
                        "attack": ("ATTACK", "#0b2e13", GOOD),
                        "skip": ("Skip", MUTED, "rgba(255,255,255,0.08)")}[kind]
        self.verdict.setText(text)
        self.verdict.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 14px; padding: 6px 18px; font-weight: 700;")

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._append_log(*data)
                elif kind == "status":
                    if data.startswith("Attacking (builder base): base"):
                        self._bb_stage("Second base" if data.endswith("2") else "First base")
                    if data.startswith("Stopped: storage full"):
                        InfoBar.success("Storage full", data.split("(", 1)[-1].rstrip(")") + " full, so the bot stopped.",
                                        duration=-1, position=InfoBarPosition.TOP_RIGHT, parent=self)
                    state = ("attacking" if data.startswith("Attacking")
                             else "paused" if data.startswith(("Paused", "Saving up")) else "running")
                    self._set_state(state, data)
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
                    self._show_update(data)
                elif kind == "notify":
                    self.notify_result.setText("✓ Sent" if data else "✗ Not sent: see the Activity log")
                    self.notify_result.setStyleSheet(f"color: {GOOD if data else BAD};")
                elif kind == "device":
                    ok, message = data
                    self.device_result.setText(message)
                    self.device_result.setStyleSheet(f"color: {GOOD if ok else BAD};")
                elif kind == "error":
                    self._set_state("error", data)
                    InfoBar.error("Bot stopped", data, duration=6000, position=InfoBarPosition.TOP_RIGHT, parent=self)
                elif kind == "finished":
                    if self.state_chip.text() != "Error":
                        full = self.status_label.text().startswith("Stopped: storage full")
                        self._set_state("idle", self.status_label.text() if full else "Stopped. Press Start to go again.")
                    self.stop_button.setEnabled(False)
                    self.pause_event.clear()
                    self._show_pause_button()
                    self._changed(save=False)
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
            self.loot_note[key].setStyleSheet(f"color: {GOOD if met else MUTED};")
        from farmbot.tasks.search import loot_value

        total = loot_value(loot, search)
        self.total_note.setText(f"Total {total:,}" + (f"  ·  {'✓' if total >= search.min_total else '✗'} minimum "
                                                       f"{search.min_total:,}" if search.min_total else ""))
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
        colour = BAD if level >= logging.ERROR else WARN if level >= logging.WARNING else GOOD if "→ attack" in line else "#d6dae0"
        self.log_view.appendHtml(f'<span style="color:{colour}; font-family: Consolas, monospace;">{escape(line)}</span>')

    def closeEvent(self, event) -> None:
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
