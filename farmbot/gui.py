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

import contextlib
import copy
import io
import json
import logging
import queue
import threading
import time
from html import escape
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QEvent, QFileInfo, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFileDialog, QFileIconProvider, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QFrame, QListWidgetItem, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

# Loading QFluentWidgets prints an ad for its Pro version, which the windowed
# exe would write to farmbot.log: load it with printing switched off.
with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets  # noqa: F401
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, EditableComboBox, ElevatedCardWidget, ExpandSettingCard,
                            Action, FluentIcon as FIF, FluentWindow, InfoBadge, InfoBadgePosition, InfoBar, InfoBarIcon, InfoBarPosition, LineEdit, ListWidget, MessageBox, MessageBoxBase,
                            NavigationAvatarWidget, NavigationDisplayMode, NavigationItemPosition, PlainTextEdit, PrimaryDropDownPushButton, PrimaryPushButton, ProgressBar, PushButton,
                            IconWidget, RoundMenu, SegmentedWidget,
                            SettingCard, SettingCardGroup, SimpleCardWidget, Slider, SmoothScrollArea,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, Theme, TitleLabel, TogglePushButton,
                            TransparentPushButton, TransparentToolButton,
                            isDarkTheme, qconfig, setCustomStyleSheet, setTheme, setThemeColor)

from farmbot import __version__, accounts, catalog, icons, instances, splash, standard, townhall
from farmbot.config import EMULATORS, Config, DeviceConfig, ensure_config, load_config, save_config, unreachable_minimums, unreachable_note
from farmbot.form import BY_PATH, apply_form, form_values, parse_amount, plan_summary, validate
from farmbot.friendly import Problem, Status, describe_status, explain, in_sentence
from farmbot.geometry import Rect
from farmbot.human import HABITS
from farmbot.timings import TIMINGS_FILE, Timings, cost, duration
from farmbot.vision import TemplateLibrary

log = logging.getLogger(__name__)

APP_NAME = "COCFARM"
ASSETS = Path(__file__).parent / "assets"
LOGO = ASSETS / "cocfarm.png"
ICON_TOOLS = getattr(FIF, "DEVELOPER_TOOLS", FIF.SETTING)
ICON_SCREEN = getattr(FIF, "FIT_PAGE", FIF.ZOOM)
UPGRADE_HELP = ("Spends a full storage on the dearest upgrades it covers while builders are free, before walls "
                "(keeping a builder for them: see Builders to use). Off by default: it spends loot.")
ORDER_HELP = ("Top first: each takes what it can of the full storages before the next. The arrows move one; the "
              "switch turns it off. Only the resources shown are spent.")
FIRST_HELP = "Then the rest. Army: camps, barracks, the laboratory and the like. Any: the dearest of any kind."


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


# Buttons and the like: white text on the light theme's accent reads 5.5:1
# (the dark theme's would be 2.9:1); the dark theme's buttons carry dark text.
ACCENT = Tone("#4c9aff", "#0067c0")
ON_ACCENT = Tone("#0b1a2e", "#ffffff")  # text on an ACCENT background
GOOD = Tone("#6ccb5f", "#0f7b0f")
BAD = Tone("#ff99a4", "#c42b1c")
WARN = Tone("#fce100", "#8a5a00")
MUTED = Tone("#9da5b4", "#5d6470")
TEXT = Tone("#d6dae0", "#1b1b1b")
SUBTLE = Tone("rgba(255,255,255,0.06)", "rgba(0,0,0,0.05)")  # a faint chip background
LINE = Tone("rgba(255,255,255,0.08)", "rgba(0,0,0,0.08)")  # dividers inside a card
RESOURCE_COLOURS = {"gold": Tone("#f5c542", "#8a6100"), "elixir": Tone("#d58cff", "#8b3fbf"),
                    "dark": Tone("#b4b4e6", "#4a4a8c")}
RESOURCES = (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir"))
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


NOTICE_LONG = 60  # characters of title and text past which a bar puts the text under the title


def notice(icon: InfoBarIcon, title: str, content: str, duration: int, parent: QWidget,
           button: QWidget | None = None) -> InfoBar:
    """A bar in the window's bottom right corner. A longer one, or one with
    a button, puts its text under the title and the button under that, so
    it stays inside the window; it's shown once the button is in, being
    placed by its full width."""
    orient = Qt.Vertical if button is not None or len(title) + len(content) > NOTICE_LONG else Qt.Horizontal
    bar = InfoBar(icon, title, content, orient, duration=duration, position=InfoBarPosition.BOTTOM_RIGHT,
                  parent=parent)
    if button is not None:
        bar.addWidget(button)
    bar.show()
    return bar


def timings_phases(village: str) -> list[str]:
    from farmbot.timings import PHASES
    return list(PHASES[village])


def tint(widget: QWidget, tone: Any, extra: str = "") -> QWidget:
    """Colour ``widget``'s text (``None``: the theme's own) plus any extra
    style, and do it again whenever the theme changes."""
    _TINTED[:] = [t for t in _TINTED if t[0] is not widget]
    _TINTED.append((widget, tone, extra))
    widget.setStyleSheet((f"color: {tone}; " if tone is not None else "") + (extra() if callable(extra) else extra))
    return widget


def retint() -> None:
    alive = []
    for widget, tone, extra in _TINTED:
        try:
            widget.setStyleSheet((f"color: {tone}; " if tone is not None else "") +
                                 (extra() if callable(extra) else extra))
            alive.append((widget, tone, extra))
        except RuntimeError:  # deleted with its window
            pass
    _TINTED[:] = alive
# Top-level pages, most used first; Ctrl+1... follow this order.
PAGES = ("Dashboard", "Home village", "Builder base", "When to play", "Like a person", "History", "Setup",
         "Advanced")


class _QueueLogHandler(logging.Handler):
    """A window's log: its farm's lines (farm 1's window also shows the lines
    of no farm in particular)."""

    def __init__(self, events: queue.Queue, farm: int = 1):
        super().__init__()
        self.events = events
        self.farm = farm
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "file_only", False):  # details for farmbot.log, not the window
            return
        if (getattr(record, "farm", 0) or 1) != self.farm:  # another farm's
            return
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


def with_commas(w: LineEdit) -> LineEdit:
    """Group a plain number's digits as it's typed (10000000 → 10,000,000),
    the cursor staying by the same digit, so an extra zero shows. Shorthand
    like 1.5m or 500k is left as typed."""

    def regroup(text: str) -> None:
        plain = text.replace(",", "")
        # A leading 0 (the first digit just deleted, to type another) is left as it is.
        if not plain.isdigit() or plain[0] == "0" and len(plain) > 1 or (grouped := f"{int(plain):,}") == text:
            return
        after = sum(c.isdigit() for c in text[w.cursorPosition():])  # digits right of the cursor
        w.setText(grouped)
        pos = len(grouped)
        while after:
            pos -= 1
            after -= grouped[pos].isdigit()
        w.setCursorPosition(pos)

    w.textEdited.connect(regroup)
    return w


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


def _fade(w: QWidget, on: bool) -> None:
    if on and not isinstance(w.graphicsEffect(), QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(w)
        effect.setOpacity(0.35)
        w.setGraphicsEffect(effect)
    elif not on and w.graphicsEffect() is not None:
        w.setGraphicsEffect(None)


class _SegmentFader(QObject):
    """Fluent's segments look the same switched off: fade the whole control
    when it's off (or what it's on is), or just the choices that are."""

    def __init__(self, w: SegmentedWidget):
        super().__init__(w)
        self.w = w
        for item in (w, *w.items.values()):
            item.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.EnabledChange:
            self.refresh()
        return False

    def refresh(self) -> None:
        on = self.w.isEnabled()
        _fade(self.w, not on)
        for item in self.w.items.values():
            _fade(item, on and not item.isEnabled())


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
    w.fader = _SegmentFader(w)

    def set_(value: Any) -> None:
        key = str(value)
        state["value"] = key
        if key in labels:
            w.setCurrentItem(key)

    return Binding(lambda: state["value"], set_, callbacks.append, [w])


def tabular(label: QLabel) -> QLabel:
    """Digits all one width (Segoe UI's own differ: a 1 is narrower), so
    a number counting up doesn't shuffle sideways."""
    font = label.font()
    font.setFeature(QFont.Tag("tnum"), 1)
    label.setFont(font)
    return label


def toggle_button(*args) -> TogglePushButton:
    """A toggle button that shows it's on by bolder text as well as its
    colour."""
    b = TogglePushButton(*args)
    qss = "TogglePushButton:checked { font-weight: 600; }"
    setCustomStyleSheet(b, qss, qss)
    return b


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


def _summarised(binding: Binding, header, empty: str = "None picked") -> Binding:
    """``binding`` with what's picked shown on a closed list's header
    (``empty`` says what happens when nothing is)."""
    def show(*_):
        names = binding.get()
        header.setContent(", ".join(names) if names else empty)

    def set_(value) -> None:
        binding.set(value)
        show()

    binding.connect(show)
    show()
    return Binding(binding.get, set_, binding.connect, binding.widgets)


def bind_presets(edits: dict[str, LineEdit]) -> Binding:
    """One line per Clan Castle level ↔ ``{level: "2 Valkyrie, 1 Wizard"}``."""
    def set_(value) -> None:
        for level, edit in edits.items():
            edit.setText(str(value.get(level, "")))

    return Binding(lambda: {level: edit.text() for level, edit in edits.items()}, set_,
                   lambda cb: [edit.textChanged.connect(cb) for edit in edits.values()], list(edits.values()))


def bind_toggles(buttons: dict[Any, TogglePushButton]) -> Binding:
    """A set of toggle buttons ↔ a list of the keys that are on."""
    return Binding(
        lambda: [k for k, b in buttons.items() if b.isChecked()],
        lambda values: [b.setChecked(k in values) for k, b in buttons.items()],
        lambda cb: [b.toggled.connect(cb) for b in buttons.values()],
        list(buttons.values()),
    )


# ====================================================================== pages
class LockBanner(SimpleCardWidget):
    """At the top of a page or tab: what this account's Town Hall hasn't
    unlocked there yet. Hidden while there's nothing to say."""

    def __init__(self):
        super().__init__()
        line = QHBoxLayout(self)
        line.setContentsMargins(16, 12, 16, 12)
        line.setSpacing(12)
        icon = IconWidget(FIF.INFO)
        icon.setFixedSize(20, 20)
        line.addWidget(icon, 0, Qt.AlignTop)
        self.label = BodyLabel("")
        self.label.setWordWrap(True)
        tint(self.label, WARN)
        line.addWidget(self.label, 1)
        self.hide()

    def show_text(self, text: str) -> None:
        self.label.setText(text)
        self.setVisible(bool(text))


class Page(SmoothScrollArea):
    """A scrolling page of setting groups. Without a title it's the body of
    a tab (the tabbed page above it shows the title)."""

    def __init__(self, name: str, title: str = "", subtitle: str = "", badge: str = ""):
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
        if title and badge:  # e.g. "Beta", in a pill after the title
            head = QHBoxLayout()
            head.setSpacing(12)
            head.addWidget(TitleLabel(title))
            self.badge = QLabel(badge)
            tint(self.badge, ON_ACCENT, lambda: f"background: {ACCENT}; border-radius: 9px; padding: 2px 12px; "
                                                "font-size: 14px; font-weight: 600;")
            head.addWidget(self.badge, 0, Qt.AlignVCenter)
            head.addStretch(1)
            self.layout_.addLayout(head)
        elif title:
            self.layout_.addWidget(TitleLabel(title))
        if subtitle:
            self.layout_.addWidget(_subtitle(subtitle))
        self._head = self.layout_.count()  # the title and subtitle: the notice goes after them
        self.banner: LockBanner | None = None
        self.setWidget(body)

    def add(self, widget: QWidget) -> QWidget:
        self.layout_.addWidget(widget)
        return widget

    def show_lock(self, text: str, whole: bool = False) -> None:
        """``text`` in a notice at the top ("" hides it); ``whole``: everything
        below it greyed out too."""
        if self.banner is None:
            if not text:
                return
            self.banner = LockBanner()
            self.layout_.insertWidget(self._head, self.banner)
        self.banner.show_text(text)
        for i in range(self._head, self.layout_.count()):
            w = self.layout_.itemAt(i).widget()
            if w is not None and w is not self.banner:
                w.setEnabled(not (whole and text))


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
        self.banner = LockBanner()
        head.addWidget(self.banner)
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

    def show_lock(self, text: str) -> None:
        """``text`` in a notice above the tabs, and every tab greyed out ("":
        neither). The tabs can still be looked through."""
        self.banner.show_text(text)
        self.stack.setEnabled(not text)


CARD_HEIGHT = 70  # a setting card's usual height
# Advanced settings shown only for the emulator picked in Setup.
EMULATOR_ONLY = {"device.ldplayer": "ldplayer", "device.ldplayer_index": "ldplayer", "device.mumu_index": "mumu",
                 "device.serial": "manual"}


def emulator_defaults() -> dict[str, dict[str, str]]:
    """Emulator -> setting -> what COCFARM sets it to, read from what each
    emulator's set-up writes (``farmbot/standard.py`` in its own terms)."""
    from farmbot import bluestacks, ldplayer, mumu

    m, b = mumu.WANTED, bluestacks.wanted_settings()
    ld = {flag: want for flag, (_key, want) in ldplayer.MODIFY.items()}
    off = lambda v: "off" if str(v).lower() in ("0", "false") else "on"  # noqa: E731
    return {
        "MuMu Player": {
            "Screen": f"{m['resolution_width.custom']} x {m['resolution_height.custom']}",
            "DPI": m["resolution_dpi.custom"],
            "Frame rate": f"{m['max_frame_rate']} FPS",
            "Vertical sync": off(m["vertical_sync"]),
            "High / adaptive FPS": off(m["dynamic_adjust_frame_rate"]),
            "CPU cores": m["performance_cpu.custom"],
            "Memory": f"{m['performance_mem.custom']} GB",
            "Phone": f"{m['phone_brand']} {m['phone_model']} ({m['phone_miit']})",
        },
        "LDPlayer": {
            "Screen": f"{standard.WIDTH} x {standard.HEIGHT}",
            "DPI": str(standard.DPI),
            "Frame rate": f"{ldplayer.CONFIG['basicSettings.fps']} FPS",
            "Vertical sync": off(ldplayer.CONFIG["basicSettings.verticalSync"]),
            "High / adaptive FPS": off(ldplayer.CONFIG["basicSettings.heightFrameRate"]),
            "CPU cores": str(ld["--cpu"]),
            "Memory": f"{ld['--memory'] / 1024:g} GB",
            "Phone": f"{ld['--manufacturer']} {ld['--model']}",
        },
        "BlueStacks": {
            "Screen": f"{b['fb_width']} x {b['fb_height']}",
            "DPI": str(b["dpi"]),
            "Frame rate": f"{b['max_fps']} FPS",
            "Vertical sync": off(b["enable_vsync"]),
            "High / adaptive FPS": off(b["enable_high_fps"]),
            "CPU cores": str(b["cpus"]),
            "Memory": f"{b['ram'] / 1024:g} GB",
            "Phone": "left as BlueStacks has it",
        },
    }


def _relayout_group(group: "SettingCardGroup") -> None:
    """Re-place a group's cards after one changed height, and resize the group."""
    group._relayout_due = False
    cards = group.cardLayout
    cards.setGeometry(cards.geometry())  # moves every card under the one above it
    height = group.layout().totalHeightForWidth(group.width()) if group.layout().hasHeightForWidth() else 0
    if height and (height != group.height() or height != group.minimumHeight()):
        group.setMinimumHeight(height)
        group.resize(group.width(), height)
    group.updateGeometry()


def _add_expanding(group: "SettingCardGroup", card: ExpandSettingCard) -> None:
    """Add a card that opens and closes to ``group``. Once it's done moving,
    the group is measured again: a height it was held at while the card was
    open would otherwise leave a gap when it closes."""
    group.addSettingCard(card)
    card.expandAni.finished.connect(lambda: _relayout_group(group))


class FoldCard(ExpandSettingCard):
    """An :class:`ExpandSettingCard` that measures what's inside at its real
    width. The stock card asks for the inside's preferred height, which is
    wrong for text that wraps, so a closed card stayed taller than its
    header (a gap under it) and the cards below jumped at the end."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expandAni.finished.connect(self._settle)

    def _view_height(self) -> int:
        layout = self.viewLayout
        width = self.view.width() or self.width()
        if layout.hasHeightForWidth() and width > 0:
            return layout.totalHeightForWidth(width)
        return layout.sizeHint().height()

    def _onExpandValueChanged(self):
        h = self.viewportMargins().top()
        self.setFixedHeight(max(h + self._view_height() - self.verticalScrollBar().value(), h))

    def _adjustViewSize(self):
        h = self._view_height()
        self.spaceWidget.setFixedHeight(h)
        if self.isExpand:
            self.setFixedHeight(self.card.height() + h)

    def _settle(self) -> None:
        """Once it's done moving: exactly its header, or header and inside."""
        top = self.viewportMargins().top()
        self.setFixedHeight(top + self._view_height() if self.isExpand else top)


class MoreCard(FoldCard):
    """A group's less-used settings, folded under one row: their defaults
    suit most players, so the page reads shorter. Cards go in with
    ``addSettingCard``, as into a group."""

    def __init__(self, title: str, content: str):
        super().__init__(FIF.MORE, title, content)
        self.viewLayout.setContentsMargins(24, 2, 0, 6)
        self.viewLayout.setSpacing(2)
        # A card inside may grow taller once it has the open view's width.
        self.expandAni.finished.connect(self._adjustViewSize)

    def addSettingCard(self, card: QWidget) -> None:
        self.viewLayout.addWidget(card)
        self._adjustViewSize()


def _folded_in(card: QWidget) -> MoreCard | None:
    """The closed :class:`MoreCard` hiding ``card``, if any."""
    parent = card.parentWidget()
    while parent is not None and not isinstance(parent, MoreCard):
        parent = parent.parentWidget()
    return parent if parent is not None and not parent.isExpand else None


HELP_WIDTH = 560  # px: a card's explanation wraps here rather than running across a wide window


class Card(SettingCard):
    """A setting card whose description doubles as its error line."""

    def __init__(self, icon, title: str, help_text: str, control: QWidget | None = None, *extra: QWidget):
        super().__init__(icon, title, help_text or " ")
        self.help_text = help_text
        self.contentLabel.setMaximumWidth(HELP_WIDTH)
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
        if event.size().width() != event.oldSize().width() and self.titleLabel.width() > 0:
            # Its layout has already given the labels their new width: fit
            # now, so the group places the cards below by the new height in
            # this same pass (later, every card would jump a frame after).
            self._fit_height()
        else:
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
        text = (self.contentLabel.heightForWidth(min(width, HELP_WIDTH)) if self.contentLabel.text().strip()
                else 0)
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
            painter.setPen(QPen(QColor(str(ACCENT)), 2))
            painter.drawRect(shown)


def _muted(label: QLabel) -> QLabel:
    tint(label, MUTED)
    return label


STAR_ON, STAR_OFF = "\u2605", "\u2606"  # the starred profile: new accounts start from its settings
STAR = Tone("#f5c542", "#8a6100")
AVATARS = ("#2f6fdf", "#0f8a5f", "#b5530f", "#8b3fbf", "#c42b6a", "#00838f", "#5b5fc7")  # white text reads on each


def pill(text: str, tone: Tone, background: str) -> QLabel:
    """A small rounded label: what an account is (in use, the default)."""
    label = QLabel(text)
    tint(label, tone, f"background: {background}; border-radius: 9px; padding: 1px 8px; font-size: 12px; "
                      "font-weight: 600;")
    return label


# The resources' colours, for the tags saying what an upgrade may spend.
RESOURCE_TAGS = {"gold": ("Gold", Tone("#f5c542", "#8a6100"), "rgba(245,197,66,0.18)"),
                 "elixir": ("Elixir", Tone("#d68cff", "#8b3fbf"), "rgba(184,90,230,0.18)"),
                 "dark": ("Dark elixir", Tone("#b9b2d6", "#3d3654"), "rgba(90,80,130,0.22)")}


WATCH_MARGIN = 12  # px left above the panel scrolled to the top on Start
WATCH_HOLD_MS = 40  # drawing paused while the page settles after the status panel changes size
LOG_MIN = 80  # the step-by-step log's least height (on Start it grows to fill the window)


class _Watcher(QObject):
    """Calls ``callback`` when a watched widget gets one of ``kinds`` of event."""

    def __init__(self, parent: QObject, kinds: tuple, callback) -> None:
        super().__init__(parent)
        self.kinds, self.callback = kinds, callback

    def eventFilter(self, obj, event) -> bool:
        if event.type() in self.kinds:
            self.callback()
        return False


class FitCard(SimpleCardWidget):
    """A card as tall as what's in it at its width (wrapped text included):
    the settings groups lay their cards out by the cards' own height."""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_soon()

    def event(self, event) -> bool:
        handled = super().event(event)
        # What's in it changed size (rows restyled, text set) at the same
        # width: fit again, or the last of it spills out of the card.
        if event.type() == QEvent.LayoutRequest:
            self._fit_soon()
        return handled

    def _fit_soon(self) -> None:
        # Not in the middle of a layout pass: a height set there isn't laid out.
        if not getattr(self, "_fit_due", False):
            self._fit_due = True
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        self._fit_due = False
        layout = self.layout()
        height = layout.heightForWidth(self.width()) if layout is not None and layout.hasHeightForWidth() else -1
        if height <= 0 and layout is not None:
            height = layout.sizeHint().height()
        if height > 0 and height != self.height():
            self.setFixedHeight(height)
            # The group's layout only follows a card's height when its width
            # didn't change too (as on :class:`Card`): have it place its cards again.
            group = self.parentWidget()
            if isinstance(group, SettingCardGroup) and not getattr(group, "_relayout_due", False):
                group._relayout_due = True
                QTimer.singleShot(0, lambda: _relayout_group(group))


class ListCard(FitCard):
    """A card with a title and an explanation over a list; the explanation
    doubles as its error line, as on :class:`Card`."""

    def __init__(self, title: str, help_text: str):
        super().__init__()
        self.help_text = help_text
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(16, 12, 16, 14)
        self.column.setSpacing(8)
        self.column.addWidget(StrongBodyLabel(title))
        self.hint = _muted(_wrapping(CaptionLabel(help_text)))
        self.column.addWidget(self.hint)

    def show_error(self, message: str | None) -> None:
        self.hint.setText(f"⚠  {message}" if message else self.help_text)
        tint(self.hint, BAD if message else MUTED)

    def show_warning(self, message: str) -> None:
        self.hint.setText(f"⚠  {message}")
        tint(self.hint, WARN)


class PriorityList(QWidget):
    """Rows to put in order with up and down buttons, each with a switch:
    what gets upgraded first. ``rows``: key -> (icon, title, resources)."""

    def __init__(self, rows: dict[str, tuple[Any, str, tuple[str, ...]]], parent: QWidget | None = None):
        super().__init__(parent)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(6)
        self.order = list(rows)
        self.rows: dict[str, QWidget] = {}
        self.ranks: dict[str, QLabel] = {}
        self.switches: dict[str, SwitchButton] = {}
        self.buttons: list[QWidget] = []
        self.callbacks: list[Callable] = []
        for key, (icon, title, resources) in rows.items():
            row = SimpleCardWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(12, 8, 12, 8)
            line.setSpacing(10)
            rank = SubtitleLabel("")
            rank.setFixedWidth(24)
            rank.setAlignment(Qt.AlignCenter)
            tint(rank, ACCENT)
            picture = IconWidget(icon)
            picture.setFixedSize(20, 20)
            name = StrongBodyLabel(title)
            name.setMinimumWidth(110)
            line.addWidget(rank)
            line.addWidget(picture)
            line.addWidget(name)
            for resource in resources:
                text, tone, background = RESOURCE_TAGS[resource]
                line.addWidget(pill(text, tone, background))
            line.addStretch(1)
            up, down = TransparentToolButton(FIF.UP), TransparentToolButton(FIF.DOWN)
            up.setToolTip("Earlier")
            down.setToolTip("Later")
            up.clicked.connect(lambda _=False, k=key: self.move(k, -1))
            down.clicked.connect(lambda _=False, k=key: self.move(k, 1))
            switch = SwitchButton()
            line.addWidget(up)
            line.addWidget(down)
            line.addSpacing(6)
            line.addWidget(switch)
            self.rows[key], self.ranks[key], self.switches[key] = row, rank, switch
            self.buttons += [up, down]
        self.set_order(self.order)

    def move(self, key: str, step: int) -> None:
        i = self.order.index(key)
        j = min(max(i + step, 0), len(self.order) - 1)
        if i == j:
            return
        order = list(self.order)
        order[i], order[j] = order[j], order[i]
        self.set_order(order)
        for cb in self.callbacks:
            cb()

    def set_order(self, order: list[str]) -> None:
        known = [k for k in dict.fromkeys(order) if k in self.rows]
        self.order = known + [k for k in self.rows if k not in known]
        for row in self.rows.values():
            self.layout_.removeWidget(row)
        for n, key in enumerate(self.order, 1):
            self.layout_.addWidget(self.rows[key])
            self.ranks[key].setText(str(n))

    def binding(self) -> Binding:
        return Binding(lambda: list(self.order), self.set_order, self.callbacks.append, self.buttons)


def avatar(name: str, size: int = 40) -> QLabel:
    """A coloured circle with the name's first letter; the same name always
    gets the same colour."""
    label = QLabel(name[:1].upper() or "?")
    label.setFixedSize(size, size)
    label.setAlignment(Qt.AlignCenter)
    colour = AVATARS[sum(map(ord, name)) % len(AVATARS)]
    label.setStyleSheet(f"background: {colour}; color: white; border-radius: {size // 2}px; "
                        f"font-size: {size * 2 // 5}px; font-weight: 600;")
    return label


EMULATOR_LOOK = {"ldplayer": ("LD", "#d9822b"), "mumu": ("MU", "#1f9d55"), "bluestacks": ("BS", "#2f6fdf")}
_EMULATOR_PICTURES: dict[tuple[str, str, int, float], QPixmap] = {}


def emulator_picture(mode: str, install: str = "", size: int = 40) -> QLabel:
    """An emulator instance's picture: the emulator's own icon (from its
    program on this PC; else its initials on its colour), with its version
    number on it (LDPlayer 9, LDPlayer 14, BlueStacks 5...)."""
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    key = (mode, install, size, ratio)
    pixmap = _EMULATOR_PICTURES.get(key)
    if pixmap is None:
        program, version = instances.emulator_badge(mode, install)
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        inner = size - 4
        icon = QFileIconProvider().icon(QFileInfo(str(program))) if program is not None else QIcon()
        if not icon.isNull():
            painter.drawPixmap(QRect(0, 0, inner, inner), icon.pixmap(QSize(inner, inner) * 2))
        else:
            letters, colour = EMULATOR_LOOK.get(mode, ("?", AVATARS[0]))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(QRect(0, 0, inner, inner), 8, 8)
            font = painter.font()
            font.setPixelSize(inner * 2 // 5)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(QRect(0, 0, inner, inner), Qt.AlignCenter, letters)
        if version:
            font = painter.font()
            font.setPixelSize(max(9, size // 4))
            font.setBold(True)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(version) + 8
            height = max(14, size * 2 // 5)
            tag = QRect(size - width, size - height, width, height)
            painter.setPen(QPen(QColor("white"), 1.5))
            painter.setBrush(QColor("#202020"))
            painter.drawRoundedRect(tag.adjusted(1, 1, -1, -1), height / 2 - 1, height / 2 - 1)
            painter.setPen(QColor("white"))
            painter.drawText(tag, Qt.AlignCenter, version)
        painter.end()
        _EMULATOR_PICTURES[key] = pixmap
    label = QLabel()
    label.setFixedSize(size, size)
    label.setPixmap(pixmap)
    name = install or instances.EMULATOR_NAMES.get(mode, mode)
    version = instances.emulator_badge(mode, install)[1]
    label.setToolTip(name if mode == "ldplayer" or not version else f"{name} {version}")
    return label


class BelowTitleBar:
    """For a dimmed dialog over the window: the dimming starts below the
    title bar, so the window can still be dragged, and its minimise,
    maximise and close buttons light up and work in one click. (Mix in
    before the dialog class.)"""

    def _fit_below_title_bar(self) -> None:
        parent, main = self.parentWidget(), self.window()
        bar = getattr(main, "titleBar", None)
        if parent is None or bar is None:
            return
        top = max(0, bar.mapTo(main, QPoint(0, bar.height())).y() - parent.mapTo(main, QPoint(0, 0)).y())
        self.setGeometry(0, top, parent.width(), parent.height() - top)

    def showEvent(self, e) -> None:
        self._fit_below_title_bar()
        super().showEvent(e)

    def eventFilter(self, obj, e: QEvent) -> bool:
        if obj is self.window() and e.type() == QEvent.Resize:  # the base class would cover the title bar again
            self._fit_below_title_bar()
            return False
        return super().eventFilter(obj, e)


class Confirm(BelowTitleBar, MessageBox):
    """An "are you sure?" box."""


class NameDialog(BelowTitleBar, MessageBoxBase):
    """Ask for a profile's new name."""

    def __init__(self, title: str, name: str, parent: QWidget):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title))
        self.edit = LineEdit()
        self.edit.setText(name)
        self.edit.selectAll()
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("Rename")
        self.widget.setMinimumWidth(360)


class VillageDialog(BelowTitleBar, MessageBoxBase):
    """The account's village data, as the game's Data Export gave it: its
    Town Hall, buildings and army, and what isn't unlocked yet."""

    def __init__(self, path, parent: QWidget):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Village data"))
        kept = townhall.load_export(path)
        if kept is None:
            self.viewLayout.addWidget(_wrapping(BodyLabel(
                "Not read yet. With \"Read the village data once a day\" on, the bot reads it in the game "
                "(Settings → More Settings → Data Export) the next time it's in the home village.")))
        else:
            export, read_on = kept
            village = townhall.parse(json.dumps(export))
            when = f" · read {read_on}" if read_on else ""
            self.viewLayout.addWidget(tint(CaptionLabel(f"Town Hall {village.town_hall}{when} · from the game's "
                                                        "Data Export"), MUTED))
            body = QWidget()
            body.setStyleSheet("background: transparent;")
            lines = QVBoxLayout(body)
            lines.setContentsMargins(0, 0, 8, 0)
            lines.setSpacing(2)
            locked = townhall.locked(village)
            sections = ([("Not unlocked yet", [(townhall.NAMES[f], why) for f, why in locked.items()])]
                        if locked else []) + list(townhall.overview(export).items())
            for title, rows in sections:
                if not rows:
                    continue
                lines.addSpacing(8)
                lines.addWidget(tint(CaptionLabel(title.upper()), MUTED))
                for name, detail in rows:
                    row = QHBoxLayout()
                    row.setSpacing(12)
                    label = _wrapping(StrongBodyLabel(name))
                    label.setFixedWidth(170)
                    row.addWidget(label, 0, Qt.AlignTop)
                    row.addWidget(_wrapping(BodyLabel(detail[:1].upper() + detail[1:])), 1, Qt.AlignTop)
                    lines.addLayout(row)
                    lines.addSpacing(4)
            scroll = SmoothScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            scroll.setWidget(body)
            body.setFixedWidth(456)
            scroll.setFixedHeight(min(body.sizeHint().height(), 440))
            self.viewLayout.addWidget(scroll)
        self.yesButton.setText("Close")
        self.cancelButton.hide()
        self.setClosableOnMaskClicked(True)
        self.widget.setMinimumWidth(520)
        self.widget.setMaximumWidth(620)


class WhatsNewDialog(BelowTitleBar, MessageBoxBase):
    """What changed in the versions since the player last looked."""

    def __init__(self, notes: list[tuple[str, list[tuple[str, str, str]]]], parent: QWidget):
        from farmbot.whatsnew import KINDS

        super().__init__(parent)
        title = f"What's new in COCFARM {notes[0][0]}" if len(notes) == 1 else "What's new in COCFARM"
        self.viewLayout.addWidget(SubtitleLabel(title))
        # Several versions at once can be taller than the window: they scroll.
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lines = QVBoxLayout(body)
        lines.setContentsMargins(0, 0, 8, 0)
        for version, changes in notes:
            if len(notes) > 1:
                lines.addSpacing(6)
                lines.addWidget(StrongBodyLabel(f"Version {version}"))
            for kind in KINDS:
                here = [(headline, detail) for k, headline, detail in changes if k == kind]
                if not here:
                    continue
                lines.addSpacing(4)
                section = CaptionLabel(kind.upper())
                tint(section, MUTED)
                lines.addWidget(section)
                for headline, detail in here:
                    lines.addWidget(_wrapping(StrongBodyLabel(f"•  {headline}")))
                    if detail:
                        text = _wrapping(BodyLabel(detail))
                        text.setContentsMargins(18, 0, 0, 4)
                        tint(text, MUTED)
                        lines.addWidget(text)
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setWidget(body)
        body.setFixedWidth(456)  # the window's width less its margins and the scroll bar
        scroll.setFixedHeight(min(body.sizeHint().height(), 420))
        self.viewLayout.addWidget(scroll)
        self.yesButton.setText("Got it")
        self.cancelButton.hide()
        self.setClosableOnMaskClicked(True)  # a click anywhere outside closes it too
        self.widget.setMinimumWidth(520)
        self.widget.setMaximumWidth(620)


class AccountsPanel(QWidget):
    """The Farms page's list, by account: a group for each account's profile
    with the farms that play it (a copy of a farm's instance starts in its
    account's group), and "Not assigned" for farms whose account isn't known
    yet and the instances that aren't farms. Each group folds away. Its
    header stars the profile (new accounts start from its settings), copies
    its settings to another, renames or deletes it."""

    UNASSIGNED = ""  # the "Not assigned" group's key (no profile is named "")

    def __init__(self, hub: FarmbotWindow, page: FarmsPage | None = None):
        super().__init__()
        self.main = hub  # farm 1's window (not self.window: QWidget.window() is taken)
        self.page = page
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.list = QWidget()
        self.rows = QVBoxLayout(self.list)  # a group each
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(12)
        layout.addWidget(self.list)
        self.footer = CaptionLabel()
        self.footer.setWordWrap(True)
        tint(self.footer, MUTED)
        layout.addWidget(self.footer)
        self.folded: set[str] = set()  # the groups folded away
        self.groups: dict[str, QWidget] = {}
        self._shown: tuple | None = None

    def _farms(self) -> list[int]:
        return list(self.page.rows) if self.page is not None else instances.numbers()

    def arrangement(self) -> tuple[list[str], dict[str, list[int]], list[int]]:
        """The profiles (the ones farms play first, in farm order), the farms
        playing each, and the farms with no known account."""
        names = sorted(self.main.profiles.names(), key=str.lower)
        farms = self._farms()
        users = {name: [n for n in farms if self.main.farm_profile(n) == name] for name in names}
        unassigned = [n for n in farms if not any(n in u for u in users.values())]
        names.sort(key=lambda name: (not users[name], users[name][:1] or [0]))
        return names, users, unassigned

    def refresh(self, force: bool = True) -> None:
        names, users, unassigned = self.arrangement()
        running = {n for n in instances.numbers() if self.main.farm_running(n)}
        spares = [id(spare) for spare in self.page.spares] if self.page is not None else []
        rows = [id(row) for row in self.page.rows.values()] if self.page is not None else []
        shown = (tuple(names), tuple((k, tuple(v)) for k, v in users.items()), tuple(unassigned),
                 tuple(sorted(running)), self.main.config.profiles.default, tuple(spares), tuple(rows))
        if not force and shown == self._shown:
            return
        self._shown = shown
        # The farms' rows move to the new groups: off the old ones before those go.
        moving = [*self.page.rows.values(), *self.page.spares] if self.page is not None else []
        for widget in moving:
            widget.setParent(self.list)
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.hide()  # gone now, not when Qt gets round to deleting it
                widget.deleteLater()
        self.groups = {}
        page_rows = self.page.rows if self.page is not None else {}
        for name in names:
            farms = [page_rows[n] for n in users[name] if n in page_rows]
            self._add_group(name, self._row(name, names, users[name], running), farms,
                            "No farm plays this account now.")
        loose = [page_rows[n] for n in unassigned if n in page_rows] + list(self.page.spares if self.page else [])
        if loose or not names:
            self._add_group(self.UNASSIGNED, self._unassigned_header(len(unassigned), len(loose) - len(unassigned)),
                            loose, "Every emulator instance here plays a known account.")
        self.footer.setText(f"{STAR_ON} Star a profile and new accounts start from its settings. A running "
                            "farm's profile can't be renamed or deleted. A farm moves to its account's group once "
                            "it sees the player's name in the game.")

    def _add_group(self, key: str, header: QWidget, farms: list[QWidget], empty: str) -> None:
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(header)
        body = QWidget()
        inside = QVBoxLayout(body)
        inside.setContentsMargins(28, 0, 0, 0)  # under the account, set in
        inside.setSpacing(6)
        for row in farms:
            inside.addWidget(row)
            row.show()
        if not farms:
            inside.addWidget(tint(CaptionLabel(empty), MUTED))
        column.addWidget(body)
        folded = key in self.folded
        body.setVisible(not folded)
        fold = header.findChild(TransparentToolButton, "fold")
        if fold is not None:
            fold.setIcon(FIF.CHEVRON_RIGHT_MED if folded else FIF.CHEVRON_DOWN_MED)
            fold.clicked.connect(lambda _=False, k=key, b=body, f=fold: self._fold(k, b, f))
        group.body = body
        self.groups[key] = group
        self.rows.addWidget(group)

    def _fold(self, key: str, body: QWidget, button: QWidget) -> None:
        folded = not body.isHidden()  # (isVisible: also whether the page is on screen)
        body.setVisible(not folded)
        (self.folded.add if folded else self.folded.discard)(key)
        button.setIcon(FIF.CHEVRON_RIGHT_MED if folded else FIF.CHEVRON_DOWN_MED)
        button.setToolTip("Show its farms" if folded else "Fold its farms away")

    def _fold_button(self) -> TransparentToolButton:
        fold = TransparentToolButton(FIF.CHEVRON_DOWN_MED)
        fold.setObjectName("fold")
        fold.setFixedSize(28, 28)
        fold.setToolTip("Fold its farms away")
        return fold

    def _unassigned_header(self, farms: int, spares: int) -> QWidget:
        card = SimpleCardWidget()
        row = QHBoxLayout(card)
        row.setContentsMargins(4, 10, 8, 10)
        card.setMinimumHeight(66)
        row.setSpacing(12)
        row.addWidget(self._fold_button())
        row.addWidget(avatar("?"))
        about = QVBoxLayout()
        about.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        title = StrongBodyLabel("Not assigned")
        title.setStyleSheet("font-size: 15px;")
        top.addWidget(title)
        if farms:
            top.addWidget(pill(f"{farms} farm{'s' * (farms != 1)}", GOOD, "rgba(16,124,16,0.16)"))
        if spares:
            top.addWidget(pill(f"{spares} not a farm", MUTED, "rgba(128,128,128,0.16)"))
        top.addStretch(1)
        about.addLayout(top)
        about.addWidget(tint(CaptionLabel("Farms whose account isn't known yet (found the first time they play), "
                                          "and the instances that aren't farms"), MUTED))
        row.addLayout(about, 1)
        return card

    def _busy(self, name: str, running: set[int] | None = None) -> bool:
        """A running farm plays with this profile's settings."""
        running = running if running is not None else {n for n in instances.numbers() if self.main.farm_running(n)}
        return any(n in running for n in self.main.farms_using(name))

    def _row(self, name: str, names: list[str], users: list[int], running: set[int]) -> QWidget:
        starred = name == self.main.config.profiles.default
        busy = any(n in running for n in users)
        card = SimpleCardWidget()
        row = QHBoxLayout(card)
        row.setContentsMargins(4, 10, 8, 10)
        card.setMinimumHeight(66)
        row.setSpacing(12)
        row.addWidget(self._fold_button())
        row.addWidget(avatar(name))
        about = QVBoxLayout()
        about.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        title = StrongBodyLabel(name)
        title.setStyleSheet("font-size: 15px;")
        top.addWidget(title)
        if users:
            top.addWidget(pill(f"{len(users)} farm{'s' * (len(users) != 1)}", GOOD, "rgba(16,124,16,0.16)"))
        if starred:
            top.addWidget(pill(f"{STAR_ON} Default", STAR, "rgba(245,197,66,0.18)"))
        top.addStretch(1)
        about.addLayout(top)
        seen = QHBoxLayout()
        seen.setSpacing(6)
        picture = self._name_picture(name)
        if picture is None:
            seen.addWidget(tint(CaptionLabel("Not seen in the game yet"), MUTED))
        else:
            seen.addWidget(tint(CaptionLabel("In game:"), MUTED))
            seen.addWidget(picture)
        seen.addStretch(1)
        about.addLayout(seen)
        row.addLayout(about, 1)
        star = TransparentPushButton(STAR_ON if starred else STAR_OFF)
        star.setFixedSize(36, 36)
        tint(star, STAR if starred else MUTED, "font-size: 20px; background: transparent; border: none;")
        star.setToolTip("New accounts start from this profile's settings (click to unstar)" if starred
                        else "Star: new accounts start from this profile's settings")
        star.clicked.connect(lambda _=False: self._star("" if starred else name))
        row.addWidget(star)
        more = TransparentToolButton(FIF.MORE)
        more.setToolTip("Copy, rename, delete")
        more.clicked.connect(lambda _=False, b=more: self._more(name, names, b, busy))
        row.addWidget(more)
        return card

    def _name_picture(self, name: str) -> QLabel | None:
        """The player name as the game shows it (the picture the bot matches),
        in the theme's text colour."""
        mask = self.main.profiles.mask(name)
        if mask is None:
            return None
        colour = QColor(str(TEXT))
        rgba = np.zeros((*mask.shape, 4), np.uint8)
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = colour.red(), colour.green(), colour.blue()
        rgba[..., 3] = mask
        image = QImage(rgba.data, mask.shape[1], mask.shape[0], mask.shape[1] * 4, QImage.Format_RGBA8888).copy()
        picture = QLabel()
        picture.setPixmap(QPixmap.fromImage(image).scaledToHeight(16, Qt.SmoothTransformation))
        picture.setToolTip("The name the bot looks for in the game")
        return picture

    def _more(self, name: str, names: list[str], button: QWidget, busy: bool) -> None:
        menu = RoundMenu(parent=self)
        copy_to = RoundMenu(f"Copy {name}'s settings to", self)
        copy_to.setIcon(FIF.COPY)
        others = [n for n in names if n != name]
        for other in others:
            action = Action(FIF.PEOPLE, other)
            action.setEnabled(not self._busy(other))
            action.triggered.connect(lambda _=False, o=other: self._copy(name, o))
            copy_to.addAction(action)
        if others:
            menu.addMenu(copy_to)
        else:
            none = Action(FIF.COPY, "Copy settings to… (no other profile yet)")
            none.setEnabled(False)
            menu.addAction(none)
        rename = Action(FIF.EDIT, "Rename")
        rename.setEnabled(not busy)
        rename.triggered.connect(lambda _=False: self._rename(name))
        menu.addAction(rename)
        menu.addSeparator()
        delete = Action(FIF.DELETE, "Delete")
        delete.setEnabled(not busy)
        delete.triggered.connect(lambda _=False: self._delete(name))
        menu.addAction(delete)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _done(self) -> None:
        self.main.refresh_farm_pages()

    def _star(self, name: str) -> None:
        for n in instances.numbers():  # each farm's file has the star: new accounts there start from it too
            self.main.edit_farm(n, lambda c: setattr(c.profiles, "default", name))
        self._done()

    def _copy(self, source: str, target: str) -> None:
        box = Confirm(f"Copy {source}'s settings to {target}?",
                      f"{target}'s settings are replaced with {source}'s. What the bot read of {target}'s "
                      "account (its storage sizes) stays.", self.main)
        box.yesButton.setText("Copy")
        if not box.exec():
            return
        main = self.main
        for window in main.farm_windows().values():
            window._flush_save()
        try:
            main.profiles.copy(source, target, load_config(main.config_path))
        except (OSError, ValueError) as exc:
            notice(InfoBarIcon.ERROR, "Couldn't copy", str(exc), duration=6000, parent=main)
            return
        for n in main.farms_using(target):
            window = main.farm_window(n, create=False)
            if window is not None:
                window._use_profile(target)  # show its new settings
        notice(InfoBarIcon.SUCCESS, "Copied", f"{target} now has {source}'s settings.", duration=4000, parent=main)
        self._done()

    def _rename(self, name: str) -> None:
        box = NameDialog(f"Rename {name}", name, self.main)
        if not box.exec() or box.edit.text().strip() == name:
            return
        main = self.main
        for window in main.farm_windows().values():
            window._flush_save()
        try:
            new = main.profiles.rename(name, box.edit.text())
        except (OSError, ValueError) as exc:
            notice(InfoBarIcon.ERROR, "Couldn't rename", str(exc), duration=6000, parent=main)
            return

        def renamed(config: Config) -> None:
            if config.profiles.active == name:
                config.profiles.active = new
            if config.profiles.default == name:
                config.profiles.default = new

        for n in instances.numbers():
            main.edit_farm(n, renamed)
        self._done()

    def _delete(self, name: str) -> None:
        box = Confirm(f"Delete {name}?", "Its settings go. If the bot sees that account again, it makes a new "
                      "profile for it from the starred profile's settings.", self.main)
        box.yesButton.setText("Delete")
        if not box.exec():
            return
        main = self.main
        for n in main.farms_using(name):
            window = main.farm_window(n, create=False)
            if window is not None:
                window._use_profile("")
            else:
                main.edit_farm(n, lambda c: setattr(c.profiles, "active", ""))
        for n in instances.numbers():
            main.edit_farm(n, lambda c: setattr(c.profiles, "default", "") if c.profiles.default == name else None)
        main.profiles.delete(name)
        self._done()


class FarmRow(SimpleCardWidget):
    """One farm on the Farms page: its emulator instance and account, what
    it's doing, its on/off switch; show it in this window or one of its own."""

    def __init__(self, page: FarmsPage, n: int):
        super().__init__()
        self.page, self.hub, self.n = page, page.hub, n
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 10, 10)
        row.setSpacing(12)
        self.setMinimumHeight(72)
        try:
            window = self._window()
            device = window.config.device if window is not None else instances.device_of(n)
        except (OSError, ValueError):
            device = DeviceConfig()
        self.picture = emulator_picture(device.mode, device.ldplayer if device.mode == "ldplayer" else "")
        row.addWidget(self.picture)
        about = QVBoxLayout()
        about.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = StrongBodyLabel(f"Farm {n}")
        self.title.setStyleSheet("font-size: 15px;")
        top.addWidget(self.title)
        self.chip = QLabel()
        top.addWidget(self.chip)
        top.addStretch(1)
        about.addLayout(top)
        self.what = tint(CaptionLabel(), MUTED)
        about.addWidget(self.what)
        self.doing = CaptionLabel()
        self.doing.setWordWrap(True)
        about.addWidget(self.doing)
        row.addLayout(about, 1)
        self.switch = SwitchButton()
        self.switch.setOnText("")
        self.switch.setOffText("")
        self.switch.setToolTip("Picked: Start farms it")
        self.switch.checkedChanged.connect(self._switched)
        row.addWidget(self.switch)
        # Stop it from here, without showing it in this window first.
        self.stop_button = PushButton(FIF.PAUSE, "Stop")
        self.stop_button.setMinimumWidth(110)
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)
        self.view_button = PushButton(FIF.VIEW, "View")
        self.view_button.setMinimumWidth(110)
        self.view_button.clicked.connect(lambda: self.hub.view_farm(self.n, self.page.owner))
        row.addWidget(self.view_button)
        self.own_button = TransparentToolButton(FIF.FULL_SCREEN)
        self.own_button.setToolTip("Open in a window of its own (closing that window switches the farm off)")
        self.own_button.clicked.connect(lambda: self.hub.open_separately(self.n))
        row.addWidget(self.own_button)
        self.remove_button = TransparentToolButton(FIF.DELETE)
        self.remove_button.setToolTip("Remove this farm (its attack history is kept)")
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.remove_button)
        if n == 1:  # COCFARM's own: never removed (its place kept, so the rows line up)
            policy = self.remove_button.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            self.remove_button.setSizePolicy(policy)
            self.remove_button.hide()
        self.refresh()

    def _window(self, create: bool = False):
        return self.hub.farm_window(self.n, create=create)

    def refresh(self) -> None:
        window = self._window()
        running = window is not None and window._running()
        account = self.hub.farm_profile(self.n)
        self.title.setText(f"Farm {self.n}")  # its account: the group it's in
        try:
            device = window.config.device if window is not None else instances.device_of(self.n)
            self.what.setText(instances.describe(device) + ("" if account else " · its account is found when it "
                                                                                 "first plays"))
        except (OSError, ValueError) as exc:
            self.what.setText(f"Its settings can't be read: {exc}")
        if window is not None and (running or window.status_now.state == "problem"):
            status = window.status_now
            fg, bg = STATES[status.state]
            word = ("Switching off" if window.switching_off() else
                    "Paused" if running and window.pause_event.is_set() else status.word)
            self.chip.setText(f"{status.symbol}  {word}")
            attacks = window.stats["attacks"]
            self.doing.setText(status.headline + (f" · {attacks} attack{'s' * (attacks != 1)}" if attacks else ""))
        else:
            fg, bg = STATES["stopped"]
            self.chip.setText("Off")
            attacks = window.stats["attacks"] if window is not None else 0
            self.doing.setText(f"{attacks} attack{'s' * (attacks != 1)} this session" if attacks else "")
        self.chip.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 9px; padding: 1px 10px; "
                                "font-weight: 600;")
        self.doing.setVisible(bool(self.doing.text()))
        stopping = running and window.switching_off()
        if running and not stopping and self.n not in self.page.picked:  # started from its own window
            instances.pick(self.n)
            self.page.picked.append(self.n)
        self.switch.blockSignals(True)
        self.switch.setChecked(self.n in self.page.picked)
        self.switch.blockSignals(False)
        here = self.page.owner.farm == self.n
        self.view_button.setText("Showing" if here else "View")
        self.view_button.setEnabled(not here)
        self.view_button.setToolTip("This window shows this farm" if here else "Show this farm in this window")
        separate = window is not None and window.separate and window.isVisible()
        self.own_button.setEnabled(not here and not separate)
        self.remove_button.setEnabled(not running)
        self.stop_button.setVisible(running)
        if stopping:
            self.stop_button.setText("Stop now")
            self.stop_button.setIcon(FIF.CLOSE)
            self.stop_button.setToolTip("Stop straight away, even in the middle of a battle")
        else:
            self.stop_button.setText("Stop")
            self.stop_button.setIcon(FIF.PAUSE)
            self.stop_button.setToolTip("Stop this farm once its current action is done (it stays picked for "
                                        "Start)")

    def _stop(self) -> None:
        """First press: stop after the current action; a second: straight away."""
        window = self._window()
        if window is None or not window._running():
            return
        if window.switching_off():
            window.stop_now()
        else:
            window.stop_soon()
        self.page.refresh()

    def _switched(self, on: bool) -> None:
        """Picks the farm for Start; while farms are farming, it starts or stops this one too."""
        instances.pick(self.n, on)
        self.page.picked = instances.picked()
        if self.page.farming():
            window = self._window()
            if on and not (window is not None and window._running()):
                self.hub.start_farm(self.n)
            elif not on and window is not None and window._running():
                window.stop_soon()
        self.page.refresh()

    def _remove(self) -> None:
        box = Confirm(f"Remove farm {self.n}?", "COCFARM stops using that emulator instance (the instance itself "
                      "stays in the emulator). Its attack history is kept in the instances folder.", self.page.owner)
        box.yesButton.setText("Remove")
        if box.exec():
            self.hub.remove_farm(self.n)


class SpareRow(SimpleCardWidget):
    """An emulator instance on this PC that no farm plays yet. Switching it
    on makes it a farm (with the last farm's settings), picked."""

    def __init__(self, page: FarmsPage, choice: instances.Choice):
        super().__init__()
        self.page, self.hub, self.choice = page, page.hub, choice
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 10, 10)
        row.setSpacing(12)
        self.setMinimumHeight(72)
        row.addWidget(emulator_picture(choice.mode, choice.install))
        about = QVBoxLayout()
        about.setSpacing(4)
        self.title = StrongBodyLabel(_instance_name(choice))
        self.title.setStyleSheet("font-size: 15px;")
        about.addWidget(self.title)
        about.addWidget(tint(CaptionLabel(f"{choice.emulator} · not a farm yet: switch it on to farm with it"),
                             MUTED))
        row.addLayout(about, 1)
        self.switch = SwitchButton()
        self.switch.setOnText("")
        self.switch.setOffText("")
        self.switch.setToolTip("Make it a farm, picked for Start")
        self.switch.checkedChanged.connect(self._switched)
        row.addWidget(self.switch)
        self.delete_button = TransparentToolButton(FIF.DELETE)
        if instances.can_delete(choice):
            self.delete_button.setToolTip("Delete this emulator instance (it asks first)")
            self.delete_button.clicked.connect(lambda: self.hub.delete_instance(self.choice))
        elif choice.mode == "bluestacks":
            self.delete_button.setToolTip("Delete it in BlueStacks' Multi-Instance Manager")
            self.delete_button.clicked.connect(self.hub.open_bluestacks_manager)
        else:  # the emulator's first instance: its own
            self.delete_button.setEnabled(False)
            self.delete_button.setToolTip("The emulator's first instance can't be deleted")
        row.addWidget(self.delete_button)

    def _switched(self, on: bool) -> None:
        if not on:
            return
        farming = self.page.farming()
        n = self.hub.add_farm(self.choice.device(), quiet=True)
        if n is None:
            self.switch.blockSignals(True)
            self.switch.setChecked(False)
            self.switch.blockSignals(False)
        elif farming:  # farms are running: this one joins them
            self.hub.start_farm(n)


class FarmsPage(Page):
    """Several accounts at once (``farmbot/instances.py``): every farm, each
    switched on or off here, and every account's profile. Each farm's window
    has this page; ``owner`` is the window it's in."""

    def __init__(self, owner: FarmbotWindow):
        super().__init__("Farms", "Farms", "Farm several accounts at once, one emulator instance each. Switch "
                         "farms on, then press Start. View shows a farm here; the square button opens it in a "
                         "window of its own (closing it switches the farm off).", badge="Beta")
        self.owner = owner
        self.hub = owner.hub or owner
        top = QWidget()
        top.setStyleSheet("background: transparent;")
        head = QHBoxLayout(top)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(12)
        self.summary = StrongBodyLabel()
        head.addWidget(self.summary)
        head.addStretch(1)
        self.all_button = PrimaryPushButton(FIF.PLAY, "Start")
        self.all_button.setMinimumWidth(130)
        self.all_button.clicked.connect(self._start_or_stop_all)
        head.addWidget(self.all_button)
        self.add(top)
        self.rows: dict[int, FarmRow] = {}
        self.spares: list[SpareRow] = []
        self.accounts = AccountsPanel(self.hub, self)  # the farms, by account
        self.add(self.accounts)
        bar = QWidget()
        bar.setStyleSheet("background: transparent;")
        line = QHBoxLayout(bar)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        self.add_button = PrimaryDropDownPushButton(FIF.ADD, "New instance")
        self.add_button.setToolTip("Make a new emulator instance, a copy of one with the game, and a farm for it")
        self.add_menu = RoundMenu(parent=self.add_button)
        self.add_button.setMenu(self.add_menu)
        line.addWidget(self.add_button)
        rescan = TransparentToolButton(FIF.SYNC)
        rescan.setToolTip("Look again for emulator instances (made in an emulator's own manager, say)")
        rescan.clicked.connect(lambda: self.hub.scan_emulators())
        line.addWidget(rescan)
        line.addStretch(1)
        self.add(bar)
        self.footer = tint(CaptionLabel(), MUTED)
        self.footer.setWordWrap(True)
        self.add(self.footer)
        self.picked: list[int] = instances.picked()
        self._choices: list = []
        self.timer = QTimer(self, interval=1000, timeout=self.tick)
        self.timer.start()

    def rebuild(self) -> None:
        """The farms there are, and the instances that could be farms."""
        for row in [*self.rows.values(), *self.spares]:
            row.hide()
            row.deleteLater()
        self.rows, self.spares = {}, []
        numbers = instances.numbers()
        self.picked = instances.picked()
        for n in numbers:
            self.rows[n] = FarmRow(self, n)  # placed in its account's group
        self._show_all()
        self._fill_choices(numbers)
        self.accounts.refresh()

    def _fill_choices(self, numbers: list[int]) -> None:
        """The instances no farm plays yet (a row each, after the farms), and
        New instance's menu: one per emulator."""
        devices = self.hub.farm_devices()
        self._choices = [c for c in self.hub.found
                         if not any(instances.same_instance(d, c.device(d)) for d in devices.values())]
        if len(numbers) < instances.MOST:
            for choice in self._choices:
                self.spares.append(SpareRow(self, choice))  # under "Not assigned"
        self.add_menu.clear()
        for emulator in self.hub.found_emulators:
            if emulator.mode == "bluestacks":
                text = "BlueStacks: in its Multi-Instance Manager…"
            else:
                source = instances.source_for(emulator, devices, self.hub.found)
                named = next((c for c in self.hub.found if c.mode == emulator.mode and c.install == emulator.install
                              and instances.same_instance(c.device(source), source)), None)
                text = (f"{emulator.name}: a copy of "
                        f"{_instance_name(named) if named else instances.describe(source)}")
            new = Action(FIF.COPY, text)
            new.setEnabled(not self.hub.creating)
            new.triggered.connect(lambda _=False, e=emulator: self.hub.create_farm(e))
            self.add_menu.addAction(new)
        room = len(numbers) < instances.MOST
        busy = self.hub.creating
        self.add_button.setEnabled(room and bool(self.hub.found_emulators))
        if busy:
            self.footer.setText(busy)
        elif not self.hub.found_emulators:
            self.footer.setText("No emulator found: install BlueStacks, LDPlayer or MuMu Player (Setup, step 1).")
        elif not room:
            self.footer.setText(f"COCFARM runs {instances.MOST} farms at most.")
        else:
            self.footer.setText("Farms can mix emulators. A new instance is a copy, signed in to the same account: "
                                "switch it to another account once (Supercell ID) and it stays on it. Each instance "
                                "needs about 2 CPU cores and 4 GB of memory.")

    def refresh(self) -> None:
        if list(self.rows) != instances.numbers():
            self.rebuild()
            return
        self.picked = instances.picked()
        for row in self.rows.values():
            row.refresh()
        self._show_all()
        self.accounts.refresh(force=False)

    def tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def farming(self) -> list[int]:
        """The farms farming (not those switching off)."""
        on = []
        for n in self.rows:
            window = self.hub.farm_window(n, create=False)
            if window is not None and window._running() and not window.switching_off():
                on.append(n)
        return on

    def _show_all(self) -> None:
        on, picked = len(self.farming()), len([n for n in self.picked if n in self.rows])
        if on:
            self.summary.setText(f"{on} farm{'s' * (on != 1)} farming")
        else:
            self.summary.setText(f"{picked} of {len(self.rows)} farms picked" if len(self.rows) > 1 else "")
        self.all_button.setText("Stop" if on else "Start")
        self.all_button.setIcon(FIF.PAUSE if on else FIF.PLAY)
        self.all_button.setEnabled(bool(on or picked))
        self.all_button.setToolTip("Switch every farm off, each after its current step" if on else
                                   "Farm with every farm picked" if picked else "Pick a farm with its switch first")

    def _start_or_stop_all(self) -> None:
        on = self.farming()
        if on:
            for n in on:
                self.hub.farm_window(n).stop_soon()
        else:
            for n in self.picked:
                if n in self.rows:
                    self.hub.start_farm(n)
        self.refresh()

    def _add(self, choice: instances.Choice) -> None:
        self.hub.add_farm(choice.device())


def _instance_name(choice: instances.Choice) -> str:
    """An instance as its emulator's manager names it, e.g. "LDPlayer-1 (instance 1)"."""
    if choice.mode == "bluestacks":
        return choice.label
    number, _, name = choice.label.partition(": ")
    return f"{name or number} (instance {int(choice.key) + 1})"


class FarmbotWindow(FluentWindow):
    """A farm's window. Farm 1's is the hub, which keeps the others (made
    hidden, soon after COCFARM opens). One window at a time is COCFARM's main
    one: View on the Farms page puts another farm's window in its place.
    A farm can also be opened in a window of its own (``separate``):
    closing that one switches the farm off."""

    def __init__(self, config_path: str, serial: str | None = None, farm: int = 1,
                 hub: FarmbotWindow | None = None):
        super().__init__()
        # Windows 11's see-through Mica background is drawn again at every step
        # of a resize, which makes dragging the window's edge stutter.
        self.setMicaEffectEnabled(False)
        self.farm, self.hub = farm, hub
        self.children: dict[int, FarmbotWindow] = {}  # the hub's: the other farms' windows
        self._closing = False
        self._names_read_again: set[str] = set()  # "Account 1" profiles whose name was read again
        self.separate = False  # opened in a window of its own, beside the main one
        self.main_view = 1  # the hub's: the farm in COCFARM's main window
        self.found: list[instances.Choice] = []  # the hub's: every emulator instance on this PC
        self.found_emulators: list[instances.Emulator] = []
        self.creating = ""  # the hub's: what Create new instance is doing
        self.last_input = time.monotonic()  # the hub's: when the mouse or keyboard was last used
        self._prebuilding = False  # the hub's: _prebuild has a turn coming
        self._build_overlay: splash.Overlay | None = None  # the hub's: shown while View builds a farm's window
        self._app_filter = False  # the hub's: its filter of every event in COCFARM is on
        self._stop_when_paused = False  # switched off: stops at the next safe point
        self._profile_cache: dict[int, tuple[float, str]] = {}
        self.config_path = Path(config_path)
        if farm == 1:  # the other farms' folders are beside its file
            instances.ROOT = self.config_path.resolve().parent
        self.first_run = ensure_config(self.config_path)
        if farm == 1:
            instances.mend_sizes()  # farms added before 0.9.10 had none of farm 1's button sizes
        if self.first_run:
            log.info("First run: created %s with the settings for BlueStacks at 860 x 672", self.config_path)
        self.profiles = accounts.Profiles.beside(self.config_path)
        self.config = accounts.load_active(self.config_path)  # with its profile's settings, if one is in use
        if serial:
            self.config.device.serial = serial
        if farm > 1:
            self.config.home = self.config_path.parent  # its attack history and error screens
        self.events: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.pause_event = threading.Event()  # Pause: the bot waits at its next safe point
        self.thread: threading.Thread | None = None
        self.bindings: dict[str, Binding] = {}
        self.cards: dict[str, Card] = {}
        self.dependents: list[tuple[str, list[Binding], Any]] = []
        # Share sliders: (path, the storage size it's a share of, its label, the resource's name)
        self.shares: list[tuple[str, str, QLabel, str]] = []
        self.has_errors = False
        self._loading = False
        self.session_start: float | None = None
        self.stats = {"bases": 0, "attacks": 0}
        self.run_from = 0  # attacks before this run: the Dashboard says how many it played
        self.stopped_by_you = False
        self.gained = {"gold": 0, "elixir": 0, "dark": 0}
        self.update_release = None  # a newer release on GitHub
        self.update_staged: Path | None = None  # downloaded and unpacked, ready to install
        self.update_busy = False
        self.update_bar: InfoBar | None = None

        # The version is in every bug report screenshot; the window's number, with several open.
        self.setWindowTitle(f"{APP_NAME} {__version__}" + (f" · Farm {farm}" if farm > 1 or len(instances.numbers()) > 1
                                                           else ""))
        self.setWindowIcon(QIcon(str(LOGO)))
        self._sharp_title_icon()
        # Keep page names visible next to the icons (recognition over recall).
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setMinimumExpandWidth(900)
        self.resize(1160, 780)
        self.setMinimumSize(980, 640)
        # Theme first: set once every widget exists, it would restyle them all again (seconds).
        self._theme = None
        if hub is not None and hub._theme == self.config.bot.theme:  # set already: restyling every window takes seconds
            self._theme = hub._theme
        elif self.config.bot.theme in THEMES:
            self._apply_theme(self.config.bot.theme)
        self._build_title_bar()
        self.pages: dict[str, QWidget] = {}
        self.mirrors: dict[str, list[Binding]] = {}  # a setting shown on two pages
        self.mirror_cards: dict[str, list[Card]] = {}
        # Red badges on the side bar's pages and the tabs holding a setting with a mistake.
        self.error_badges: list[QWidget] = []
        self.error_places: list[tuple[str, str | None]] = []  # (page, tab) of each, shown
        self.first_error: Card | None = None
        self._syncing = False
        self.status_now = Status("ready", "")
        self.problem: Problem | None = None
        self.setup_passed = not self.first_run  # Setup finished once: no "New here?" banner on the Dashboard
        self.check_passed = False  # step 2 of Setup passed this session
        self._found: dict[str, bool] = {}
        self.auto_village: str | None = None  # Auto switch: the village it moved on to this run
        self._verdict_kind = "wait"
        # Pages of settings only, built when first opened (or while nobody uses COCFARM): building
        # them all at once held a farm's window up for seconds. Until then their settings are
        # kept in ``_pending`` as the window would show them, so saving still has all of them.
        self._pending: dict[str, Any] = {}
        self._wired = False  # each setting's change is followed (and saved)
        self._lazy: dict[str, Callable[[], QWidget]] = {}  # page -> what builds it, until it's built
        self._shells: dict[str, QWidget] = {}  # page -> what's in the side bar's stack for it
        self._page_order: list[str] = []
        self._add_page(self._home(), FIF.PIE_SINGLE, "Dashboard")
        self._lazy_page(self._home_village, "home_village", FIF.HOME, "Home village")
        self._lazy_page(self._builder_base, "builder_base", FIF.CONSTRACT, "Builder base")
        self._lazy_page(self._when_to_play, "when_to_play", FIF.CALENDAR, "When to play")
        self._lazy_page(self._like_a_person, "like_a_person", FIF.FINGERPRINT, "Like a person")
        self._add_page(self._history(), FIF.HISTORY, "History")
        self.farms_page = FarmsPage(self)  # every farm, and every account's profile: opened by the account
        self.farms_page.setObjectName("profile")  # (bottom left), which shows as picked while it's open
        # As addSubInterface marks its pages: unmarked, every switch to or from it restyles the whole window.
        self.farms_page.setProperty("isStackedTransparent", False)
        self.stackedWidget.addWidget(self.farms_page)
        self.pages["Farms"] = self.farms_page
        self._page_order.append("Farms")
        self._add_page(self._setup(), FIF.CHECKBOX, "Setup",
                       NavigationItemPosition.BOTTOM)
        self._add_page(self._advanced(), FIF.SETTING, "Advanced", NavigationItemPosition.BOTTOM)
        # This farm's account; a click opens the Farms page: every farm and every account.
        self.profile_nav = NavigationAvatarWidget("")
        self.navigationInterface.addWidget("profile", self.profile_nav, lambda: self.show_page("Farms"),
                                           NavigationItemPosition.BOTTOM, tooltip="Farms and accounts")
        self._show_profiles()
        self._find_bluestacks()  # fills both Setup and Advanced
        self._dock_side_bar()
        self.stackedWidget.currentChanged.connect(self._page_shown)  # (back and forth too, not only clicks)

        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._autosave)
        self._load()
        for binding in self.bindings.values():
            binding.connect(lambda *_: self._changed())
        self._wired = True  # (settings changed before this aren't saved: they're as loaded)
        self._changed(save=False)
        self._show_status(Status("ready", "Welcome! Start with Setup", "Four quick steps, then press Start.")
                          if self.first_run else
                          Status("ready", "Ready to farm", plan_summary(validate(self.collect())[0])))

        self.log_handler = _QueueLogHandler(self.events, farm)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)
        self.poll_timer = QTimer(self, interval=100, timeout=self._poll)
        self.poll_timer.start()
        self.clock_timer = QTimer(self, interval=1000, timeout=self._tick)
        self.clock_timer.start()
        QShortcut(QKeySequence("F5"), self, activated=self.start)
        if farm == 1:  # the main window looks for updates and tells what's new, for every farm
            if self.first_run:
                QTimer.singleShot(300, self._welcome)
            self.update_timer = QTimer(self, interval=600_000, timeout=self._check_for_update)  # hourly at most
            self.update_timer.start()
            QTimer.singleShot(1500, self._after_update)
            QTimer.singleShot(600, self._whats_new)
            self._check_for_update()
            self.scan_emulators()
            self.farms_page.rebuild()
            QTimer.singleShot(1500, self._prebuild)  # the other farms' windows, so they show at once
        else:
            self.farms_page.rebuild()
        QShortcut(QKeySequence("Esc"), self, activated=self.stop)
        for i, name in enumerate(PAGES, start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, activated=lambda n=name: self.show_page(n))
        qconfig.themeChanged.connect(self._theme_signal)
        if farm == 1:  # one filter for every farm's window: each event passes through all of them
            QApplication.instance().installEventFilter(self)  # Enter presses the focused button
            self._app_filter = True
        if self.LAZY_PAGES:
            QTimer.singleShot(1500, self._build_pages_idle)
        else:
            self.build_all_pages()

    _INPUT = (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.Wheel)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind in self._INPUT:
            self.last_input = time.monotonic()
            if (kind == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                    and obj is QApplication.focusWidget() and isinstance(obj, QAbstractButton) and obj.isEnabled()
                    and isinstance(obj.window(), FarmbotWindow)):
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
        self.save_label.mousePressEvent = lambda _e: self._go_to_error()  # "Fix 1 setting": show it
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
        self._build_tick()
        self.pages[key or text] = page
        self._page_order.append(key or text)
        self.addSubInterface(page, icon, text, position)
        self._build_tick()

    def _build_tick(self) -> None:
        """While View waits for this window, or a page is built: the loading logo's next frame."""
        overlay = getattr(self.hub or self, "_build_overlay", None)
        if overlay is not None:
            overlay.tick()

    def _lazy_page(self, build: Callable[[], QWidget], route: str, icon, text: str,
                   position=NavigationItemPosition.TOP) -> None:
        """A page in the side bar, built (by ``build``) when first opened."""
        shell = QWidget()
        shell.setObjectName(route)  # the page's own name: the side bar finds it by that
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        self._lazy[text] = build
        self._shells[text] = shell
        self._page_order.append(text)
        self.addSubInterface(shell, icon, text, position)

    def build_page(self, name: str, loading: bool = True) -> None:
        """Build the page ``name`` now, if it isn't yet, with its settings
        (``loading``: the logo breathes over the window meanwhile)."""
        build = self._lazy.pop(name, None)
        if build is None:
            return
        hub = self.hub or self
        shell = self._shells[name]
        overlay = None
        if loading and self.isVisible() and hub._build_overlay is None:
            overlay = hub._build_overlay = splash.Overlay(self.stackedWidget, isDarkTheme())
            overlay.tick()
        before = set(self.bindings)
        was_loading, self._loading = self._loading, True  # (nothing's saved half made)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with self._building():
                page = build()
            shell.layout().addWidget(page)
            self.pages[name] = page
            # In the side bar's order, as if it had been built with the window.
            self.pages = dict(sorted(self.pages.items(), key=lambda kv: self._page_order.index(kv[0])))
            new = [path for path in self.bindings if path not in before]
            for path in new:
                if path in self._pending:
                    self.bindings[path].set(self._pending[path])
            for path in new:  # (only once it shows them: until then saving takes the kept ones)
                self._pending.pop(path, None)
                self.bindings[path].connect(lambda *_: self._changed())
        finally:
            self._loading = was_loading
            QApplication.restoreOverrideCursor()
            if overlay is not None:
                hub._build_overlay = None
                overlay.reveal()
        self._changed(save=False)  # its mistakes, greyed-out parts and the rest, as on every other page

    @contextlib.contextmanager
    def _building(self):
        """While widgets are made: the hub's filter of every event is off
        (building sends tens of thousands, a fifth of the time)."""
        hub = self.hub or self
        quiet = hub._app_filter
        if quiet:
            QApplication.instance().removeEventFilter(hub)
            hub._app_filter = False
        try:
            yield
        finally:
            if quiet:
                QApplication.instance().installEventFilter(hub)
                hub._app_filter = True

    def build_all_pages(self) -> None:
        for name in list(self._lazy):
            self.build_page(name, loading=False)

    LAZY_PAGES = True  # False: every page built with the window (tests that look through them all)
    IDLE_BEFORE_PAGES = 1.5  # s without the mouse or keyboard

    def _build_pages_idle(self) -> None:
        """Build the pages not opened yet one at a time, while nobody uses COCFARM."""
        if not self._lazy or self._closing:
            return
        hub = self.hub or self
        idle = time.monotonic() - hub.last_input
        if idle >= self.IDLE_BEFORE_PAGES and hub._build_overlay is None:
            self.build_page(next(iter(self._lazy)), loading=False)
            hub.last_input = time.monotonic() - self.IDLE_BEFORE_PAGES  # the next one after a short break
            wait = 300
        else:
            wait = int(max(self.IDLE_BEFORE_PAGES - idle, 0) * 1000) + 100
        QTimer.singleShot(wait, self._build_pages_idle)

    def _dock_side_bar(self) -> None:
        """The side bar open beside the pages, page names showing, as in every
        COCFARM window. Opened while the window was narrow (a farm's window
        is made hidden, before it has its place), it would float over the
        pages instead ("menu" mode) and stay that way."""
        panel = self.navigationInterface.panel
        if panel.displayMode == NavigationDisplayMode.EXPAND:
            return
        if panel.displayMode == NavigationDisplayMode.MENU:  # back into the window's side first
            panel.expandAni.setProperty("expand", False)
            panel._onExpandAniFinished()
            panel.resize(48, panel.height())
        if self.width() < self.minimumWidth():  # (not placed yet: the size it will have)
            self.resize(max(self.width(), 1160), self.height())
        panel.expand(useAni=False)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        # Only the floating state is put right: one collapsed with ≡ stays so.
        if self.navigationInterface.panel.displayMode == NavigationDisplayMode.MENU:
            self._dock_side_bar()
        super().showEvent(event)

    def _side_bar_like(self, other: FarmbotWindow) -> None:
        """Open or collapsed, as ``other``'s side bar (the window this one takes the place of)."""
        mine, theirs = self.navigationInterface.panel, other.navigationInterface.panel
        if theirs.displayMode == NavigationDisplayMode.EXPAND:
            self._dock_side_bar()
        elif mine.displayMode in (NavigationDisplayMode.EXPAND, NavigationDisplayMode.MENU):
            self._dock_side_bar()
            mine.collapse()

    def _page_shown(self, *_) -> None:
        current = self.stackedWidget.currentWidget()
        for name, shell in self._shells.items():
            if shell is current and name in self._lazy:
                QTimer.singleShot(0, lambda n=name: self.build_page(n))  # after the switch is drawn

    def switchTo(self, interface: QWidget) -> None:  # noqa: N802 (Qt's name)
        for name, shell in self._shells.items():
            if shell is interface and name in self._lazy:
                self.build_page(name)
        super().switchTo(interface)

    def current_page(self) -> str:
        """The page showing in the window."""
        current = self.stackedWidget.currentWidget()
        return next((key for key, w in {**self.pages, **self._shells}.items() if w is current), "Dashboard")

    def show_page(self, name: str, tab: str | None = None) -> None:
        self.build_page(name)
        page = self.pages[name]
        if tab is not None:
            page.show_tab(tab)
        self.switchTo(self._shells.get(name, page))

    def _value(self, path: str) -> Any:
        """A setting as the window shows it (kept, if its page isn't built yet)."""
        return self.bindings[path].get() if path in self.bindings else self._pending.get(path)

    def _set_value(self, path: str, value: Any) -> None:
        """Show ``value`` for the setting ``path`` (kept for its page, if that isn't built yet)."""
        if path in self.bindings:
            self.bindings[path].set(value)
        elif path in self._pending:
            self._pending[path] = value
            if self._wired:  # (as a box's change would be: not while the window is still being made)
                self._changed()

    # ------------------------------------------------------------ helpers
    def _bind(self, path: str, binding: Binding) -> Binding:
        self.bindings[path] = binding
        return binding

    def _card(self, group: SettingCardGroup, path: str | None, icon, title: str, help_text: str,
              control: QWidget | None = None, *extra: QWidget) -> Card:
        self._build_tick()
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
        self._bind(path, bind_line(with_commas(w)))
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
                     suffix: str = "", joiner: str = "to") -> Card:
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
        extra = [_muted(BodyLabel(joiner)), edits[1]] + ([_muted(BodyLabel(suffix))] if suffix else [])
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
        value = StrongBodyLabel(str(slider.value()))  # setting it to 1 (its least) changes nothing, so no signal
        value.setFixedWidth(20)
        slider.valueChanged.connect(lambda v: value.setText(str(v)))
        slow, fast = _muted(CaptionLabel("Slow")), _muted(CaptionLabel("Fast"))
        self._bind(path, Binding(slider.value, lambda v: slider.setValue(int(v)), slider.valueChanged.connect, [slider]))
        return self._card(group, path, icon, title, help_text, slow, slider, fast, value)

    def share(self, group, path: str, icon, title: str, help_text: str, storage: str, noun: str) -> Card:
        """A 0-100% slider for a share of what a storage holds, with that
        amount beside it (``storage``: the path of the storage's size)."""
        slider = Slider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setFixedWidth(160)
        amount = StrongBodyLabel("")
        amount.setFixedWidth(170)  # "70%  ·  14,700,000 elixir"
        self._bind(path, Binding(lambda: str(slider.value()), lambda v: slider.setValue(round(float(v))),
                                 slider.valueChanged.connect, [slider]))
        self.shares.append((path, storage, amount, noun))
        return self._card(group, path, icon, title, help_text, slider, amount)

    def _show_shares(self, form: dict[str, Any]) -> None:
        for path, storage, label, noun in self.shares:
            share = int(form.get(path) or 0)
            try:
                most = parse_amount(form.get(storage, ""))
            except ValueError:
                most = 0
            label.setText(f"{share}%  ·  {round(share / 100 * most):,} {noun}" if most else
                          f"{share}%  ·  size not known yet")

    def _storage_group(self, page: Page, village: str) -> None:
        """What the storages hold: filled in by the bot, editable by hand
        (editing one turns reading it from the game off, so it's kept)."""
        prefix = "builder_" if village == "builder" else ""
        read = f"storage.{prefix}read"
        group = page.add(SettingCardGroup("Storages"))
        self.switch(group, read, FIF.SYNC, "Read from the game",
                    "Once a day it taps your loot (top right) to see what the storages hold. Off: it keeps the "
                    "amounts below.")
        kinds = {"gold": "Gold", "elixir": "Elixir"}
        if village == "home":
            kinds["dark"] = "Dark elixir"
        paths = []
        for kind, name in kinds.items():
            path = f"storage.{prefix}{kind}"
            paths.append(path)
            self.amount(group, path, icons.icon(kind, 28), f"{name} storages hold",
                        "Wrong? Type the right amount (reading then turns off).")
            edit = self.bindings[path].widgets[0]
            edit.setPlaceholderText("not known yet")
            edit.textEdited.connect(lambda _, r=read: self.bindings[r].set(False))  # typed by you, not the bot

        def clear() -> None:
            for path in paths:
                self.bindings[path].set("")
            self.bindings[read].set(True)

        button = PushButton(FIF.DELETE, "Clear")
        button.clicked.connect(clear)
        self._card(group, None, FIF.BROOM, "Read them again",
                   "Clears the amounts so they're read again. Do it when you switch accounts.", button)

    def segments(self, group, path: str, icon, title: str, help_text: str, keys: list[str] | dict[str, str],
                 width: int = 60) -> Card:
        w = SegmentedWidget()
        w.setFixedWidth(width * len(keys))
        self._bind(path, bind_segments(w, keys))
        return self._card(group, path, icon, title, help_text, w)

    def toggle_row(self, group, icon, title: str, help_text: str, paths: dict[str, str]) -> Card:
        """On/off settings side by side on one card: a toggle button each."""
        buttons = []
        for path, label in paths.items():
            b = toggle_button(label)
            b.setMinimumWidth(80)
            self._bind(path, Binding(b.isChecked, lambda v, b=b: b.setChecked(bool(v)), b.toggled.connect, [b]))
            buttons.append(b)
        card = self._card(group, None, icon, title, help_text, *buttons)
        for path in paths:
            self.cards[path] = card
        return card

    def priority(self, group, path: str, title: str, hint: str,
                 rows: dict[str, tuple[str, Any, str, tuple[str, ...]]]) -> PriorityList:
        """An ordered list (``path``) of things to switch on and off, each
        row: key -> (its switch's path, icon, title, resources it spends)."""
        box = ListCard(title, hint)
        listing = PriorityList({k: (icon, name, spends) for k, (_, icon, name, spends) in rows.items()})
        box.column.addWidget(listing)
        self._bind(path, listing.binding())
        self.cards[path] = box
        for key, (switch_path, *_rest) in rows.items():
            self._bind(switch_path, bind_switch(listing.switches[key]))
            self.cards[switch_path] = box
        group.addSettingCard(box)
        return listing

    def depends(self, controller: str, *paths: str, when: Any = True) -> None:
        """Enable the controls of ``paths`` only while ``controller`` is on
        (or, with ``when``, equals that value)."""
        self.dependents.append((controller, [self.bindings[p] for p in paths], when))

    def _template_note(self, *features: str) -> str:
        missing = [p.title for p in catalog.missing(self.library()) if p.feature in features]
        if not missing:
            return ""  # nothing to say
        return "Missing pictures (add them under Advanced → Pictures): " + ", ".join(missing)

    def library(self) -> TemplateLibrary:
        return TemplateLibrary(self.config.bot.templates)

    def _note(self, page: Page, text: str, colour: str = WARN) -> None:
        if not text:
            return
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
        value = tabular(TitleLabel("—") if big else SubtitleLabel("0"))
        note = _muted(CaptionLabel(""))
        note.setWordWrap(True)  # stat tiles share a row: wrap rather than widen the page
        for label in (head, value, note):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        card.setMinimumWidth(120)
        layout.addWidget(head)
        layout.addWidget(value)
        layout.addWidget(note)
        return card, value, note

    @staticmethod
    def _divider(vertical: bool = False) -> QFrame:
        line = QFrame()
        if vertical:
            line.setFixedWidth(1)
        else:
            line.setFixedHeight(1)
        tint(line, None, lambda: f"background: {LINE}; border: none;")
        return line

    @staticmethod
    def _metric(title: str, colour: Any = None, value: str = "0") -> tuple[QWidget, QLabel, QLabel]:
        """A number with a small heading over it and a note under it: no card,
        for sharing one."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        head = _muted(CaptionLabel(title.upper()))
        if colour is not None:
            tint(head, colour)
        head.setFont(QFont(head.font().family(), 8, QFont.DemiBold))
        number = tabular(SubtitleLabel(value))
        note = _muted(CaptionLabel(""))
        for label in (head, number, note):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            layout.addWidget(label)
        return box, number, note

    def _scout_card(self) -> ElevatedCardWidget:
        """The base on screen next to what it holds, and whether it's attacked."""
        card = ElevatedCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("Base being scouted"))
        header.addStretch(1)
        self.total_note = _muted(CaptionLabel(""))
        header.addWidget(self.total_note)
        header.addSpacing(8)
        self.verdict = QLabel("")
        self.verdict.setAlignment(Qt.AlignCenter)
        self.verdict.setMinimumWidth(150)
        self._verdict("wait")
        header.addWidget(self.verdict)
        layout.addLayout(header)

        loot = QHBoxLayout()  # gold, elixir and dark elixir side by side, big enough to read across the room
        loot.setSpacing(24)
        self.loot_value: dict[str, QLabel] = {}
        self.loot_note: dict[str, QLabel] = {}
        for key, name in RESOURCES:
            row = QHBoxLayout()
            row.setSpacing(12)
            bar = QFrame()
            bar.setFixedWidth(4)
            tint(bar, None, lambda key=key: f"background: {RESOURCE_COLOURS[key]}; border-radius: 2px;")
            row.addWidget(bar)
            column = QVBoxLayout()
            column.setSpacing(0)
            head = CaptionLabel(name)
            tint(head, RESOURCE_COLOURS[key], "font-weight: 600;")
            value = tabular(SubtitleLabel("—"))
            font = value.font()
            font.setPixelSize(30)
            font.setWeight(QFont.DemiBold)
            value.setFont(font)
            note = _muted(CaptionLabel(""))
            for label in (head, value, note):
                label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                column.addWidget(label)
            row.addLayout(column, 1)
            loot.addLayout(row, 1)
            self.loot_value[key], self.loot_note[key] = value, note
        layout.addLayout(loot)
        return card

    def _session_card(self) -> ElevatedCardWidget:
        """Bases seen, attacks, time, loot gained and the last attack, in one card."""
        card = ElevatedCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("This session"))
        header.addStretch(1)
        self.town_hall_label = _muted(CaptionLabel(""))
        header.addWidget(self.town_hall_label)
        header.addSpacing(16)
        self.resources_label = _muted(CaptionLabel(""))
        header.addWidget(self.resources_label)
        layout.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(20)
        self.stat_value: dict[str, QLabel] = {}
        self.stat_note: dict[str, QLabel] = {}
        self.gained_value: dict[str, QLabel] = {}
        self.gained_today: dict[str, QLabel] = {}
        for key, name in (("bases", "Bases seen"), ("attacks", "Attacks"), ("runtime", "Running for")):
            box, value, note = self._metric(name)
            self.stat_value[key], self.stat_note[key] = value, note
            row.addWidget(box, 1)
        self.stat_value["runtime"].setText("—")
        row.addWidget(self._divider(vertical=True))
        for key, name in RESOURCES:
            box, value, note = self._metric(f"{name.split()[0]} gained", RESOURCE_COLOURS[key])
            box.setToolTip(f"{name} gained this session (under it: today, from the attack history)")
            self.gained_value[key], self.gained_today[key] = value, note
            row.addWidget(box, 1)
        layout.addLayout(row)
        self.gained_note = _muted(CaptionLabel(""))  # today's total: at the end of the last attack's row
        self.gained_note.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self._show_today()

        layout.addWidget(self._divider())
        last = QHBoxLayout()
        last.setSpacing(16)
        head = _muted(CaptionLabel("LAST ATTACK"))
        head.setFont(QFont(head.font().family(), 8, QFont.DemiBold))
        last.addWidget(head, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.last_attack = tabular(BodyLabel("None yet"))
        self.last_attack_note = _muted(CaptionLabel("What the base offered, how much was taken, and what you "
                                                    "gained."))
        for label in (self.last_attack, self.last_attack_note):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            text.addWidget(label)
        last.addLayout(text, 1)
        last.addWidget(self.gained_note, 0, Qt.AlignTop)
        layout.addLayout(last)
        return card

    def _show_profiles(self) -> None:
        """The farm's account, at the bottom of the side bar (click: switch farms)."""
        active = self.config.profiles.active
        several = len(instances.numbers()) > 1
        self.profile_nav.setName(f"Farm {self.farm} · {active}" if several and active else
                                 active or (f"Farm {self.farm}" if several else "No account yet"))

    # ------------------------------------------------------------ farms (the main window's)
    def farm_window(self, n: int, create: bool = True) -> FarmbotWindow | None:
        """Farm ``n``'s window, made (hidden) when first needed with ``create``."""
        hub = self.hub or self
        if n == 1:
            return hub
        window = hub.children.get(n)
        if window is None and create:
            QApplication.setOverrideCursor(Qt.WaitCursor)  # a second or two: not built yet
            try:
                with hub._building():
                    window = FarmbotWindow(str(instances.config_path(n)), farm=n, hub=hub)
            finally:
                QApplication.restoreOverrideCursor()
            hub.children[n] = window
        return window

    def farm_windows(self) -> dict[int, FarmbotWindow]:
        hub = self.hub or self
        return {1: hub, **hub.children}

    def farm_devices(self) -> dict[int, DeviceConfig]:
        """Each farm's emulator instance."""
        found = {}
        for n in instances.numbers():
            window = self.farm_window(n, create=False)
            try:
                found[n] = window.config.device if window is not None else instances.device_of(n)
            except (OSError, ValueError) as exc:
                log.warning("Farm %d's settings can't be read: %s", n, exc)
        return found

    IDLE_BEFORE_PREBUILD = 6.0  # s without the mouse or keyboard

    def _prebuild(self) -> None:
        """Make the farms' windows one by one while nobody uses COCFARM, so
        View and the own-window button show them at once. Building one holds
        COCFARM up for a second or two, so not while it's being used."""
        if self._prebuilding or self._closing:
            return
        if self._build_overlay is not None:  # View is building one now
            QTimer.singleShot(1000, self._prebuild)
            return
        missing = [n for n in instances.numbers() if n > 1 and n not in self.children]
        if not missing:
            return
        self._prebuilding = True
        idle = time.monotonic() - self.last_input
        if idle >= self.IDLE_BEFORE_PREBUILD:
            self.farm_window(missing[0])
            self.last_input = time.monotonic() - self.IDLE_BEFORE_PREBUILD  # the next one after a short break
            wait = 300
        else:
            wait = int((self.IDLE_BEFORE_PREBUILD - idle) * 1000) + 100
        QTimer.singleShot(wait, self._prebuild_again)

    def _prebuild_again(self) -> None:
        self._prebuilding = False
        self._prebuild()

    def scan_emulators(self, background: bool = True) -> None:
        """Every emulator instance on this PC. It asks the emulators' own
        tools (MuMu's can take a while), so not on the window's thread."""
        hub = self.hub or self

        def work() -> None:
            try:
                found_emulators, found = instances.emulators(), instances.choices()
            except Exception as exc:  # an emulator's own tool failed: say so rather than break the page
                log.warning("Couldn't list the emulators' instances: %s", exc)
                return
            if background:
                hub.events.put(("call", lambda: hub._scanned(found_emulators, found)))
            else:
                hub._scanned(found_emulators, found)

        if background:
            hub._spawn(work)
        else:
            work()

    def _scanned(self, found_emulators: list[instances.Emulator], found: list[instances.Choice]) -> None:
        self.found_emulators, self.found = found_emulators, found
        self.rebuild_farm_pages()

    def rebuild_farm_pages(self) -> None:
        for window in self.farm_windows().values():
            window.farms_page.rebuild()
        self._retitle()

    def refresh_farm_pages(self) -> None:
        for window in self.farm_windows().values():
            window.farms_page.refresh()

    def _retitle(self) -> None:
        several = len(instances.numbers()) > 1
        for n, window in self.farm_windows().items():
            window.setWindowTitle(f"{APP_NAME} {__version__}" + (f" · Farm {n}" if several or n > 1 else ""))

    def view_farm(self, n: int, here: FarmbotWindow | None = None) -> None:
        """Show farm ``n`` in COCFARM's main window: its window takes the
        place of the one showing there, on the same page."""
        hub = self.hub or self
        main = hub.farm_window(hub.main_view, create=False) or hub
        place = main if main.isVisible() else (here or main)
        page = place.current_page()
        loading = None
        built = hub.farm_window(n, create=False)
        if (built is None or page in built._lazy) and built is not place and place.isVisible():
            # Its window or this page of it not built yet: the logo breathes over this window meanwhile.
            loading = hub._build_overlay = splash.Overlay(place, isDarkTheme(), f"Loading farm {n}…")
            loading.tick()
        try:
            target = hub.farm_window(n)
            if target is not place:
                target.build_page(page, loading=False)
        finally:
            hub._build_overlay = None
        if target is place:
            if loading is not None:
                loading.reveal()
            return
        maximized = place.isMaximized()
        target.separate = False
        target.setGeometry(place.geometry())
        target._side_bar_like(place)
        if loading is not None:  # loaded: the logo grows away on the new window, showing it
            reveal = splash.Overlay(target, isDarkTheme(), scale=loading.scale())
            loading.deleteLater()
            QTimer.singleShot(0, reveal.reveal)
        target.show_page(page)
        target.showMaximized() if maximized else target.showNormal()
        target.raise_()
        target.activateWindow()
        if not place.separate:
            place.hide()
        hub.main_view = n
        hub.refresh_farm_pages()

    def open_separately(self, n: int) -> None:
        """Farm ``n`` in a window of its own, beside the main one."""
        hub = self.hub or self
        if n == hub.main_view:
            return
        window = hub.farm_window(n)
        if not window.isVisible():
            main = hub.farm_window(hub.main_view, create=False) or hub
            window.separate = True
            window.setGeometry(main.geometry().translated(40, 40))
            window.showNormal()
        window.raise_()
        window.activateWindow()
        hub.refresh_farm_pages()

    def add_farm(self, device: DeviceConfig, quiet: bool = False, account: str = "") -> int | None:
        """A farm for this emulator instance, with the last farm's settings;
        ``account``: the profile it plays, when known."""
        hub = self.hub or self
        source = instances.numbers()[-1]
        window = hub.farm_window(source, create=False)
        if window is not None:
            window._flush_save()  # the new farm starts from its settings
        others = hub.farm_devices()
        try:
            n = instances.add(device, source, account=account)
        except (OSError, ValueError) as exc:
            notice(InfoBarIcon.ERROR, "Couldn't add it", str(exc), duration=6000, parent=self._shown())
            return None
        hub.rebuild_farm_pages()
        clash = instances.port_clash(device, others, hub.found)
        if clash is not None and device.mode == others[clash].mode == "ldplayer":
            notice(InfoBarIcon.INFORMATION, f"Added farm {n}", f"Its LDPlayer instance has the same connection port "
                   f"as farm {clash}'s. When both farm, COCFARM copies one of them (with the game and its login) to "
                   "an instance with a port of its own: that takes a few minutes and several GB of disk.",
                   duration=12000, parent=self._shown())
        elif clash is not None:
            notice(InfoBarIcon.WARNING, f"Added farm {n}, but…", f"Its emulator instance has the same connection "
                   f"port as farm {clash}'s, so the two can't play at the same time. Give one of them another "
                   "instance (or another port in the emulator's settings).", duration=12000, parent=self._shown())
        elif not quiet:
            notice(InfoBarIcon.SUCCESS, f"Added farm {n}", "It's picked: Start farms it.", duration=5000,
                   parent=self._shown())
        QTimer.singleShot(500, hub._prebuild)
        return n

    def _shown(self) -> FarmbotWindow:
        """The window to tell things in: COCFARM's main one."""
        hub = self.hub or self
        main = hub.farm_window(hub.main_view, create=False)
        return main if main is not None and main.isVisible() else self

    def create_farm(self, emulator: instances.Emulator, close: bool = False) -> None:
        """Create new instance: a copy of one of ``emulator``'s instances (the
        last farm's on it, with the game set up), and a farm for it."""
        hub = self.hub or self
        if emulator.mode == "bluestacks":
            hub._create_in_bluestacks()
            return
        devices = hub.farm_devices()
        device = instances.source_for(emulator, devices, hub.found)
        playing = next((n for n, d in devices.items() if instances.same_instance(d, device) and hub.farm_running(n)),
                       None)
        if playing is not None:
            notice(InfoBarIcon.WARNING, f"Switch farm {playing} off first", "Its emulator instance is the one "
                   "copied, and a running one can't be.", duration=6000, parent=self._shown())
            return
        copied = next((n for n, d in devices.items() if instances.same_instance(d, device)), None)
        account = hub.farm_profile(copied) if copied is not None else ""
        if account not in hub.profiles.names():
            account = ""
        hub.creating = f"Copying {instances.describe(device)}… copying the game takes a minute or two."
        hub.rebuild_farm_pages()
        events = hub.events

        def work() -> None:
            try:
                key = instances.create_instance(device, close=close)
            except instances.StillOpen as exc:
                events.put(("call", lambda name=str(exc): hub._created(emulator, None, still_open=name)))
            except Exception as exc:
                events.put(("call", lambda message=str(exc): hub._created(emulator, None, error=message)))
            else:
                made = copy.deepcopy(device)
                instances.set_key(made, key)
                events.put(("call", lambda: hub._created(emulator, made, account=account)))

        hub._spawn(work)

    def _created(self, emulator: instances.Emulator, device: DeviceConfig | None, still_open: str = "",
                 error: str = "", account: str = "") -> None:
        self.creating = ""
        self.scan_emulators()  # the new instance
        self.rebuild_farm_pages()
        shown = self._shown()
        if still_open:
            box = Confirm(f"Close {still_open} to copy it?", "A running emulator instance can't be copied. COCFARM "
                          "closes it, makes the copy, and it can be opened again after.", shown)
            box.yesButton.setText("Close and copy")
            if box.exec():
                self.create_farm(emulator, close=True)
            return
        if error:
            notice(InfoBarIcon.ERROR, "Couldn't make the instance", error, duration=8000, parent=shown)
            return
        n = self.add_farm(device, account=account)
        if n is not None:
            where = f"{account}'s account, so it's listed under {account}" if account else "the same account"
            notice(InfoBarIcon.SUCCESS, f"Made farm {n}'s instance", f"It's a copy, signed in to {where}. To farm "
                   "another account with it, open it once and sign in to that account with Supercell ID: the farm "
                   "moves to that account's group when it sees the new name.", duration=15000, parent=shown)

    def delete_instance(self, choice: instances.Choice, close: bool = False) -> None:
        """Delete an emulator instance no farm plays, after asking."""
        hub = self.hub or self
        shown = hub._shown()
        name = f"{choice.emulator}'s {_instance_name(choice)}"
        if not close:
            box = Confirm(f"Delete {name}?", "The emulator instance is deleted, with everything in it: the game "
                          "and its files. It can't be undone. The Clash of Clans account itself is safe (it's kept "
                          "by Supercell, not in the emulator).", shown)
            box.yesButton.setText("Delete")
            if not box.exec():
                return
        device = choice.device()
        events = hub.events

        def work() -> None:
            try:
                instances.delete_instance(device, close=close)
            except instances.StillOpen:
                events.put(("call", lambda: hub._deleted(choice, still_open=True)))
            except Exception as exc:
                events.put(("call", lambda message=str(exc): hub._deleted(choice, error=message)))
            else:
                events.put(("call", lambda: hub._deleted(choice)))

        hub._spawn(work)

    def _deleted(self, choice: instances.Choice, still_open: bool = False, error: str = "") -> None:
        shown = self._shown()
        if still_open:
            box = Confirm(f"Close {_instance_name(choice)} to delete it?", "It's open in the emulator. COCFARM "
                          "closes it, then deletes it.", shown)
            box.yesButton.setText("Close and delete")
            if box.exec():
                self.delete_instance(choice, close=True)
            return
        if error:
            notice(InfoBarIcon.ERROR, "Couldn't delete it", error, duration=8000, parent=shown)
        else:
            notice(InfoBarIcon.SUCCESS, "Deleted", f"{choice.emulator}'s {_instance_name(choice)} is gone.",
                   duration=5000, parent=shown)
        self.scan_emulators()

    def open_bluestacks_manager(self) -> bool:
        import subprocess

        manager = instances.bluestacks_manager()
        try:
            if manager is None:
                raise OSError("it wasn't found in the BlueStacks folder")
            subprocess.Popen([str(manager)])
            return True
        except OSError as exc:
            notice(InfoBarIcon.ERROR, "Couldn't open BlueStacks' Multi-Instance Manager", str(exc), duration=6000,
                   parent=self._shown())
            return False

    def _create_in_bluestacks(self) -> None:
        """BlueStacks makes instances only in its Multi-Instance Manager: open
        it, and add a farm for the instance made there once it shows up."""
        import subprocess

        manager = instances.bluestacks_manager()
        if manager is None:
            notice(InfoBarIcon.ERROR, "BlueStacks' Multi-Instance Manager wasn't found",
                   "Open it from the BlueStacks folder, make an instance, then press the refresh button here.",
                   duration=8000, parent=self._shown())
            return
        before = {c.key for c in instances.choices(DeviceConfig(mode="bluestacks"))}
        try:
            subprocess.Popen([str(manager)])
        except OSError as exc:
            notice(InfoBarIcon.ERROR, "Couldn't open it", str(exc), duration=6000, parent=self._shown())
            return
        self.creating = ("Waiting for the new BlueStacks instance: in the Multi-Instance Manager, press Instance, "
                         "then Clone, and clone the instance with the game.")
        self.rebuild_farm_pages()
        tries = {"left": 200}  # 10 minutes

        def look() -> None:
            tries["left"] -= 1
            made = [c for c in instances.choices(DeviceConfig(mode="bluestacks")) if c.key not in before]
            if made or tries["left"] <= 0:
                timer.stop()
                self.creating = ""
                self.scan_emulators()
                self.rebuild_farm_pages()
                if made:
                    self.add_farm(made[0].device())

        timer = QTimer(self, interval=3000, timeout=look)
        timer.start()

    def farm_running(self, n: int) -> bool:
        window = self.farm_window(n, create=False)
        return window is not None and window._running()

    def farm_profile(self, n: int) -> str:
        """The profile farm ``n`` plays with ("": none yet)."""
        window = self.farm_window(n, create=False)
        if window is not None:
            return window.config.profiles.active
        path = instances.config_path(n)
        try:
            stamp = path.stat().st_mtime
            cached = self._profile_cache.get(n)
            if cached is None or cached[0] != stamp:
                cached = (stamp, load_config(path).profiles.active)
                self._profile_cache[n] = cached
            return cached[1]
        except (OSError, ValueError):
            return ""

    def farms_using(self, name: str) -> list[int]:
        return [n for n in instances.numbers() if name and self.farm_profile(n) == name]

    def edit_farm(self, n: int, change: Callable[[Config], Any]) -> None:
        """Change farm ``n``'s settings file (its window's settings, if it has one)."""
        window = self.farm_window(n, create=False)
        if window is not None:
            window._flush_save()
            change(window.config)
            accounts.save_active(window.config, window.config_path)
            window._show_profiles()
            return
        path = instances.config_path(n)
        config = load_config(path)
        change(config)
        save_config(config, path)

    def start_farm(self, n: int) -> None:
        window = self.farm_window(n)
        window.start(show=False)
        if not window._running() and not window.isVisible():  # it says why in its own window
            self.view_farm(n)

    def remove_farm(self, n: int) -> None:
        hub = self.hub or self
        window = hub.children.pop(n, None)
        if window is not None:
            window.stop_now()
            window._closing = True
            window.close()
            window.deleteLater()
        if hub.main_view == n:
            hub.main_view = 1
            hub.showNormal()
        try:
            instances.remove(n)
        except (OSError, ValueError) as exc:
            notice(InfoBarIcon.ERROR, "Couldn't remove it", str(exc), duration=6000, parent=hub)
        hub.rebuild_farm_pages()

    def _spawn(self, target: Callable, *args) -> threading.Thread:
        """A worker thread, its log lines marked as this window's farm's."""
        def run() -> None:
            instances.mark(self.farm)
            target(*args)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _flush_save(self) -> None:
        """Save edits still waiting to be (they belong to the profile in use)."""
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._autosave()

    def _use_profile(self, name: str) -> None:
        """Put profile ``name``'s settings ("": the main file's) in place:
        the running bot holds the same Config, so it plays with them too."""
        self._flush_save()  # the last edits belong to the profile being left
        main = load_config(self.config_path)
        source = self.profiles.load(name, main) if name else main
        accounts.take(source, self.config)
        self.config.profiles.active = name
        accounts.save_active(self.config, self.config_path)
        self._load()
        self._changed(save=False)
        self._show_profiles()

    def _account_from_bot(self, mask: np.ndarray, picture: np.ndarray | None = None) -> str | None:
        """Bot thread: the profile for the account with this name (switched
        to on the window's thread, which owns the settings)."""
        box: dict[str, str | None] = {}
        done = threading.Event()
        self.events.put(("account", (mask, box, done, picture)))
        while not done.wait(0.5):
            if self.stop_event is None or self.stop_event.is_set():
                return None
        return box.get("name")

    def _rename_profile(self, old: str, new: str) -> str | None:
        """Rename profile ``old`` to ``new`` in every farm's settings that
        use it. The new name, or None if it couldn't be renamed."""
        hub = self.hub or self
        for window in hub.farm_windows().values():
            window._flush_save()
        try:
            new = self.profiles.rename(old, new)
        except (OSError, ValueError) as exc:
            log.info("Couldn't rename profile %s to %s: %s", old, new, exc)
            return None

        def renamed(config: Config) -> None:
            if config.profiles.active == old:
                config.profiles.active = new
            if config.profiles.default == old:
                config.profiles.default = new

        for n in instances.numbers():
            hub.edit_farm(n, renamed)
        hub.rebuild_farm_pages()
        log.info("Renamed profile %s to %s (the account's name)", old, new)
        notice(InfoBarIcon.SUCCESS, "Account named", f"{old} is now {new}, after the account's name.",
               duration=6000, parent=self)
        return new

    def _account_seen(self, mask: np.ndarray, picture: np.ndarray | None = None) -> str | None:
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._autosave()
        try:
            namer = accounts.namer_for(picture)
            name, made = accounts.for_account(self.profiles, self.config, mask, namer)
        except (OSError, ValueError) as exc:
            log.warning("Couldn't pick a profile: %s", exc)
            return None
        if name is None:
            return None
        if not made and name not in self._names_read_again:
            # Made while its name couldn't be read ("Account 1"): read again,
            # once a session, and named after the account if that works.
            self._names_read_again.add(name)
            new = accounts.read_later(self.profiles, name, mask, namer)
            if new is not None:
                name = self._rename_profile(name, new) or name
        if made:
            starred = self.config.profiles.default
            known = starred and self.profiles.path(starred).is_file()
            source = f"{starred}'s settings" if known else "the settings in use"
            notice(InfoBarIcon.SUCCESS, "New account", f"Made the profile {name} for it, from {source}. "
                   "Manage accounts on the Farms page.", duration=8000,
                   parent=self)
        if name != self.config.profiles.active:
            self._use_profile(name)
        else:
            self._show_profiles()
        return name

    def _home(self) -> Page:
        page = Page("home", "Dashboard", "What COCFARM is doing right now, and what it has earned.")
        self.setup_banner = SimpleCardWidget()
        row = QHBoxLayout(self.setup_banner)
        row.setContentsMargins(20, 14, 16, 14)
        text = BodyLabel("New here? Setup takes a couple of minutes: pick your emulator, check the game, choose "
                         "the loot you want, and press Start.")
        text.setWordWrap(True)
        row.addWidget(text, 1)
        go = PrimaryPushButton(FIF.RIGHT_ARROW, "Open Setup")
        go.clicked.connect(lambda: self.show_page("Setup"))
        row.addWidget(go)
        page.add(self.setup_banner)
        self.status_card = page.add(self._status_panel())

        farm = self.farm_group = page.add(SettingCardGroup("What to farm"))
        self.segments(farm, "bot.mode", FIF.HOME, "Village",
                      "Its settings are on the page of the same name. Auto switch: one village until its "
                      "storages are full, then the other.",
                      {"home": "Home village", "builder": "Builder base", "auto": "Auto switch"}, width=120)
        self.segments(farm, "bot.auto_first", FIF.PIN, "Start with",
                      "The village Auto switch farms first. It stops once both villages' storages (picked on "
                      "When to play) are full.",
                      {"home": "Home village", "builder": "Builder base"}, width=130)
        self.depends("bot.mode", "bot.auto_first", when="auto")
        # The builder panel is shorter than the page: keep this group from stretching into the gap.
        farm.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Home village panel; a builder base panel replaces it in builder mode.
        self.home_panel = QWidget()
        home = QVBoxLayout(self.home_panel)
        home.setContentsMargins(0, 0, 0, 0)
        home.setSpacing(16)
        page.add(self.home_panel)
        self.builder_panel = self._builder_dashboard()
        page.add(self.builder_panel)
        home.addWidget(self._scout_card())
        home.addWidget(self._session_card())

        details = QHBoxLayout()
        details.addWidget(SubtitleLabel("Step-by-step log"))
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
        self.log_view.setMinimumHeight(LOG_MIN)
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.log_view.setFont(mono)
        self.log_view.setPlaceholderText("Nothing yet. Press Start (F5) and each step will appear here.")
        page.add(self.log_view)
        self._show_details(False)
        # The window resized, or a panel above the log grew, shrank or swapped: fit the log again.
        watcher = _Watcher(page, (QEvent.Resize, QEvent.Show, QEvent.Hide), self._fit_log_soon)
        for widget in (page.viewport(), self.home_panel, self.builder_panel):
            widget.installEventFilter(watcher)
        # The status panel above grows and shrinks as its text wraps: keep the watched view still.
        page.verticalScrollBar().rangeChanged.connect(self._keep_watch_view)
        anchor = _Watcher(page, (QEvent.Resize,), self._hold_watch_view)
        for widget in (self.status_card, self.farm_group, self.setup_banner):
            widget.installEventFilter(anchor)
        page.viewport().installEventFilter(_Watcher(page, (QEvent.Wheel,), self._stop_watching))
        page.verticalScrollBar().actionTriggered.connect(self._stop_watching)
        return page

    def _show_details(self, on: bool) -> None:
        self.log_view.setVisible(on)
        self.clear_log_button.setVisible(on)
        self.details_button.setText("Hide the log" if on else "Show the step-by-step log")
        if on:
            self._fit_log_soon()

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
        headers = ["Time", "Offered\ngold", "Offered\nelixir", "Offered\ndark", "Gained\ngold", "Gained\nelixir",
                   "Gained\ndark"]  # two lines: narrower columns, so the amounts fit at the window's smallest
        self.history_table.setColumnCount(len(headers))
        self.history_table.setHorizontalHeaderLabels(headers)
        self.history_table.verticalHeader().hide()
        self.history_table.setEditTriggers(TableWidget.NoEditTriggers)
        self.history_table.setMinimumHeight(360)
        # The columns share the page's width: it all fits at the window's smallest (no sideways scrolling).
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)  # even columns, filling the page's width
        self.history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_table.setWordWrap(False)
        page.add(self.history_table)
        self._show_history()
        return page

    def _show_history(self) -> None:
        from datetime import date, timedelta

        from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QBarSet, QValueAxis
        from PySide6.QtWidgets import QTableWidgetItem

        from farmbot import history
        from farmbot.tasks.search import Loot

        path = instances.resolve(self.config, self.config.bot.history) if self.config.bot.history else None
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
            # No-break spaces: a narrow tile wraps between the amounts, not inside one.
            return " · ".join(f"{name} {_short(value)}" for name, value in
                              (("gold", loot.gold), ("elixir", loot.elixir), ("dark", loot.dark)))

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
        # Newest first, by when they happened (the file can hold several
        # accounts' or runs' attacks out of order).
        latest = sorted(rows, key=lambda row: row.time, reverse=True)[:self.HISTORY_ROWS]
        table.setRowCount(len(latest))
        for r, row in enumerate(latest):
            gained = row.gained
            cells = [f"{row.time:%b %d  %H:%M}", f"{row.target.gold:,}", f"{row.target.elixir:,}",
                     f"{row.target.dark:,}"] + ([f"{gained.gold:+,}", f"{gained.elixir:+,}", f"{gained.dark:+,}"]
                                                if gained else ["—", "—", "—"])
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c >= 4 and not gained:
                    item.setToolTip("Not known: the loot screen after this attack couldn't be read")
                table.setItem(r, c, item)

    def _open_history_file(self) -> None:
        import os

        path = instances.resolve(self.config, self.config.bot.history) if self.config.bot.history else None
        if not path or not path.is_file():
            notice(InfoBarIcon.WARNING, "No history yet", "The file appears after the first attack.", duration=4000,
                   parent=self)
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
        self.bb_note["walls"].setText("This session" if self.config.builder.walls else "Off (Builder base → Upgrades)")

    # ------------------------------------------------------------ settings pages
    def _home_village(self) -> TabbedPage:
        page = TabbedPage("home_village", "Home village", "Everything for farming the home village: which bases to "
                          "attack, how to attack them, and the chores in between.")
        self._bases_tab(page.tab("bases", "Bases to attack", "Amounts accept 500k, 1.5m or 1,500,000."))
        self._attacking_tab(page.tab("attacking", "Attacking"))
        self._village_tab(page.tab("village", "Village"))
        self._upgrades_tab(page.tab("upgrades", "Upgrades", "Upgrades come first, then walls get the loot left."))
        self._clan_tab(page.tab("clan", "Clan"))
        self._raid_weekend_tab(page.tab("raids", "Raid Weekend (beta)"))
        return page

    def _bases_tab(self, page: Page) -> None:
        loot = page.add(SettingCardGroup("Bases to attack"))
        w = SwitchButton()
        bind_switch(w)  # On/Off labels
        # Stored the other way round: "only look after the village" = not attacking.
        self._bind("search.maintain_only", Binding(lambda: not w.isChecked(), lambda v: w.setChecked(not v),
                                                   w.checkedChanged.connect, [w]))
        self._card(loot, "search.maintain_only", FIF.FLAG, "Attack bases",
                   "Off: it only collects, donates and looks after the village.", w)
        for kind, name in (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir")):
            self.amount(loot, f"search.min_{kind}", icons.icon(kind, 28), f"At least this much {name.lower()}",
                        "Bases offering less are skipped. 0 ignores it.")
        for kind in ("gold", "elixir", "dark"):  # typed by you: kept from now on
            self.bindings[f"search.min_{kind}"].widgets[0].textEdited.connect(self._minimums_typed)
        button = PushButton(FIF.ACCEPT, "Use these")
        button.clicked.connect(self._suggested_minimums)
        self.suggested_card = self._card(loot, None, FIF.CERTIFICATE, "Suggested amounts", " ", button)
        self.switch(loot, "search.match_any", FIF.FILTER, "Any one is enough",
                    "On: one amount above is enough. Off: a base must have them all.")
        more = page.add(SettingCardGroup("While searching"))
        self.share(more, "search.min_own_gold_share", icons.icon("gold", 28), "Don't search while my gold is below",
                   "A share of your gold storages: each Next costs gold. 0% always searches.",
                   "storage.gold", "gold")
        self.number(more, "search.max_searches", FIF.CANCEL, "Give up after",
                    "Bases skipped before it goes back to the village and starts over.", "bases")
        self.switch(more, "search.sound_on_match", FIF.RINGER, "Sound on match",
                    "Beeps when it finds a base worth attacking.")
        self.depends("search.maintain_only", "search.min_gold", "search.min_elixir", "search.min_dark",
                     "search.match_any", "search.min_own_gold_share",
                     "search.sound_on_match", "search.max_searches", when=False)

    def _attacking_tab(self, page: Page) -> None:
        deploy = page.add(SettingCardGroup("Deploying"))
        grid = QWidget()
        g = QGridLayout(grid)
        g.setContentsMargins(0, 6, 0, 6)
        g.setSpacing(6)
        buttons = {}
        for side, (r, c, label) in {"nw": (0, 0, "Top left"), "ne": (0, 1, "Top right"),
                                    "sw": (1, 0, "Bottom left"), "se": (1, 1, "Bottom right")}.items():
            b = toggle_button(label)
            b.setFixedWidth(128)
            g.addWidget(b, r, c)
            buttons[side] = b
        self._bind("attack.sides", bind_toggles(buttons))
        card = self._card(deploy, "attack.sides", FIF.MOVE, "Deploy from",
                          "Laid out like the map. Troops spread over the sides you turn on.", grid)
        card.setFixedHeight(110)
        line = MoreCard("Where on a side", "Along the red line, or around the buildings")
        self.switch(line, "redline.enabled", FIF.HIGHTLIGHT, "Follow the red line",
                    "Along the thin boundary the game draws around a base.")
        self.switch(line, "zone.enabled", FIF.LAYOUT, "Otherwise go around the buildings",
                    "Where there's no red line. Off: along fixed lines at the map's edges.")
        _add_expanding(deploy, line)
        self.segments(deploy, "attack.waves", FIF.ALIGNMENT, "Waves", "Split each troop type into this many waves.",
                      ["1", "2", "3"])
        pace = MoreCard("Deploy speeds", "How quickly troops and waves go down")
        self.speed(pace, "attack.troop_speed", FIF.SPEED_HIGH, "Troop speed", "How quickly single troops go down.")
        self.speed(pace, "attack.wave_speed", FIF.SPEED_MEDIUM, "Wave speed", "How quickly one wave follows another.")
        _add_expanding(deploy, pace)

        heroes = page.add(SettingCardGroup("Troops, heroes and siege machines"))
        self.switch(heroes, "attack.super_troops", FIF.ASTERISK, "Activate super troops",
                    "Before a battle, boosts the super troops in your army for 25,000 dark elixir each. Never "
                    "with a Super Potion or gems.")
        self.switch(heroes, "attack.use_siege", FIF.CAR, "Use siege machines",
                    "Off: they stay in the bar, and so do your Clan Castle troops, which ride inside one.")
        self.switch(heroes, "attack.hero_abilities", FIF.BRIGHTNESS, "Use hero abilities",
                    "Fires each hero's special ability once during the battle.")
        timing = MoreCard("Ability timing", "When each hero's ability fires")
        self.number(timing, "attack.hero_ability_delay", FIF.STOP_WATCH, "Ability after",
                    "Seconds after the heroes land.", "s")
        self.number(timing, "attack.ability_health", FIF.HEART, "Or when health drops to",
                    "Fires a hero's ability once it's this hurt instead of after the time above. 0% uses the time.", "%")
        self.number(timing, "attack.ability_max_wait", FIF.STOP_WATCH, "At the latest after",
                    "Heroes still unhurt by then fire anyway.", "s")
        _add_expanding(heroes, timing)
        self.depends("attack.hero_abilities", "attack.hero_ability_delay", "attack.ability_health",
                     "attack.ability_max_wait")

        spells = page.add(SettingCardGroup("Spells"))
        self.segments(spells, "attack.spells", FIF.CALORIES, "Spells to cast",
                      "Bat Spells only: other spells stay in the bar. All: every spell.",
                      {"bat": "Bat Spells only", "all": "All spells"}, width=120)
        self.segments(spells, "attack.spell_placement", FIF.ZOOM_OUT, "Spells go",
                      "One spot: every spell at once, on one spot. Spread: over the base.",
                      {"together": "One spot", "spread": "Spread"}, width=90)
        self.segments(spells, "attack.spell_spot", FIF.ZOOM_IN, "The spot",
                      "Middle: the centre of the base. Random: a new spot each battle, in the middle half.",
                      {"middle": "Middle", "random": "Random"}, width=90)
        self.depends("attack.spell_placement", "attack.spell_spot", when="together")

        end = page.add(SettingCardGroup("Ending the battle"))
        self.switch(end, "attack.speed_up", FIF.SPEED_HIGH, "Play the last minute at 4x",
                    "Presses the 1x button when it shows, with a minute of the battle left.")
        self.switch(end, "attack.end_when_no_loot", FIF.RETURN, "Leave when the loot stops",
                    "Ends the battle early once no more loot is coming in.")
        leaving = MoreCard("When exactly to leave", "How long without loot, or how much loot is enough")
        self.number(leaving, "attack.no_loot_seconds", FIF.STOP_WATCH, "No loot for",
                    "Seconds without any loot before leaving.", "s")
        self.depends("attack.end_when_no_loot", "attack.no_loot_seconds")
        self.number(leaving, "attack.end_at_loot_share", FIF.COMPLETED, "Leave once this much loot is taken",
                    "Share of the base's loot to take before leaving. 0 waits for the rules above.", "%")
        _add_expanding(end, leaving)

    def _village_tab(self, page: Page) -> None:
        village = page.add(SettingCardGroup("Collecting"))
        self.switch(village, "bot.collect_resources", FIF.DOWNLOAD, "Collect resources",
                    "Empties full collectors between attacks.")
        self.toggle_row(village, FIF.FILTER, "Collectors", "Which kinds it empties.",
                        {"bot.collect_gold": "Gold", "bot.collect_elixir": "Elixir", "bot.collect_dark": "Dark"})
        self.number(village, "bot.collect_every", FIF.DATE_TIME, "Every",
                    "1 = every round. Always the first round after Start.", "rounds")
        self.depends("bot.collect_resources", "bot.collect_gold", "bot.collect_elixir", "bot.collect_dark",
                     "bot.collect_every")
        self.switch(village, "bot.collect_loot_cart", FIF.SHOPPING_CART, "Collect the loot cart",
                    "Empties the loot cart when it shows \"Collect\".")
        self.switch(village, "bot.clear_gravestones", FIF.BROOM, "Clear gravestones",
                    "After someone attacks you, taps a gravestone once: that clears them all.")
        self._storage_group(page, "home")
        village = page.add(SettingCardGroup("Village data"))
        self.switch(village, "bot.read_village", FIF.DICTIONARY, "Read the village data once a day",
                    "Profile → Settings → More Settings → Data Export → Copy: the game's own list of your Town "
                    "Hall and buildings, kept with the profile. What your Town Hall hasn't unlocked yet is "
                    "skipped, and marked here. Whatever you had copied on the PC is put back.")
        look = PushButton(FIF.VIEW, "Show")
        look.clicked.connect(self.show_village)
        self.village_card = self._card(village, None, FIF.DOCUMENT, "Your village data",
                                       "What the game's export says: your Town Hall, buildings and army.", look)
        self._note(page, self._template_note("Collecting resources", "Collecting the loot cart"))

    def _upgrades_tab(self, page: Page) -> None:
        ups = page.add(SettingCardGroup("Upgrades"))
        self.switch(ups, "upgrade.enabled", FIF.UP, "Upgrade when storage is full",
                    UPGRADE_HELP)
        self.upgrade_list = self.priority(ups, "upgrade.order", "What to upgrade first",
                      ORDER_HELP,
                      {"pets": ("upgrade.pets", FIF.LEAF, "Pets", ("dark",)),
                       "hero": ("upgrade.heroes", FIF.PEOPLE, "Heroes", ("elixir", "dark")),
                       "research": ("upgrade.lab", FIF.EDUCATION, "Research", ("elixir", "dark")),
                       "building": ("upgrade.buildings", FIF.HOME, "Buildings", ("gold", "elixir", "dark"))})
        self.segments(ups, "upgrade.first", FIF.PIN, "Buildings: this kind first",
                      FIRST_HELP,
                      {"none": "Any", "defense": "Defenses", "resource": "Resources", "army": "Army"}, width=90)
        self.switch(ups, "upgrade.new_buildings", FIF.ADD, "Build new buildings",
                    "Buildings your Town Hall has unlocked but you haven't built yet come first (the cheapest "
                    "take seconds), put down where the game places them; you can move them later. New walls, "
                    "which are free, are put down too.")
        self.switch(ups, "upgrade.town_hall", FIF.HOME_FILL, "Upgrade the Town Hall",
                    "Like any building. While it can't be (the game wants other buildings built first), the next "
                    "upgrade is started instead.")
        self.builders_card = self._builders(
            ups, "upgrade.builders", "How many of your builders upgrades may keep busy; the rest stay free for "
            "walls (a wall needs a free builder) or for you. Default: all but one. From your village data.")
        self.depends("upgrade.enabled", "upgrade.order", "upgrade.pets", "upgrade.heroes", "upgrade.lab",
                     "upgrade.buildings", "upgrade.first", "upgrade.new_buildings", "upgrade.town_hall",
                     "upgrade.builders")
        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "walls.enabled", FIF.TILES, "Upgrade walls with spare loot",
                    "Between attacks, as many of the cheapest walls as the spare loot covers. Off by default: "
                    "it spends loot.")
        self.segments(walls, "walls.use", FIF.TAG, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.share(walls, "walls.keep_gold", icons.icon("gold", 28), "Keep this much gold",
                   "A share of your gold storages; walls get only what's above it, and never the gold searching "
                   "needs.", "storage.gold", "gold")
        self.share(walls, "walls.keep_elixir", icons.icon("elixir", 28), "Keep this much elixir",
                   "A share of your elixir storages; walls get only what's above it.", "storage.elixir",
                   "elixir")
        self.switch(walls, "walls.only_when_full", FIF.SAVE, "Only when storage is full",
                    "Spends gold or elixir on walls only once its storages are full.")
        self.depends("walls.enabled", "walls.use", "walls.keep_gold", "walls.keep_elixir",
                     "walls.only_when_full")
        self._note(page, self._template_note("Upgrading walls", "Upgrades"))

    def _builders(self, group, path: str, help_text: str) -> Card:
        """How many builders upgrades may keep busy: a slider up to the
        village's builders, shown once the village data says how many there
        are. All but one (0, kept as 0 so it follows a new hut) by default."""
        from farmbot.tasks.upgrade import builder_limit

        slider = Slider(Qt.Horizontal)
        slider.setFixedWidth(180)
        shown = StrongBodyLabel("")
        shown.setFixedWidth(90)
        state = {"value": 0, "total": 0}

        def show() -> None:
            total = state["total"]
            if not total:
                return
            slider.blockSignals(True)  # (a new range isn't a change of the setting)
            slider.setRange(1, total)
            slider.setValue(builder_limit(total, state["value"]))
            slider.blockSignals(False)
            shown.setText(f"{slider.value()} of {total}")

        def moved(v: int) -> None:
            total = state["total"]
            if total:
                state["value"] = 0 if v == builder_limit(total, 0) else v
                shown.setText(f"{v} of {total}")

        def set_value(v) -> None:
            state["value"] = int(v or 0)
            show()

        def set_total(total: int) -> None:
            state["total"] = total
            show()
            choice = total > 1  # (one builder: nothing to choose)
            if card.isVisibleTo(card.parentWidget()) != choice:
                card.setVisible(choice)
                QTimer.singleShot(0, lambda g=card.parentWidget(): _relayout_group(g))

        slider.valueChanged.connect(moved)  # (before the window's own handler: the value is set first)
        self._bind(path, Binding(lambda: state["value"], set_value, slider.valueChanged.connect, [slider]))
        card = self._card(group, path, FIF.PEOPLE, "Builders to use", help_text, slider, shown)
        card.set_total = set_total
        card.setVisible(False)  # until the village data is read
        return card

    def _clan_tab(self, page: Page) -> None:
        from farmbot.units import SIEGE_MACHINES, SPELLS, TROOPS

        donate = page.add(SettingCardGroup("Donations"))
        self.switch(donate, "clan.donate", FIF.SHARE, "Donate to requests",
                    "Fills every open request in clan chat. Units cost elixir or dark elixir, never gems.")
        armies = "None picked: the armies by Clan Castle level below decide"
        for path, icon, title, hint, units, empty in (
                ("clan.donate_main", FIF.BUS, "Main troops",
                 "Click the ones to give first, in order: they move to the top and go first, as many as fit. "
                 "Drag to change the order. With none picked, each request gets the army set below for its "
                 "Clan Castle level.",
                 TROOPS, "None picked: each request gets its Clan Castle level's army (below)"),
                ("clan.donate_siege", FIF.CAR, "Siege machines",
                 "Click the siege machines to give, in order. A request has room for one.", SIEGE_MACHINES, armies),
                ("clan.donate_spells", FIF.CALORIES, "Main spells", "Click the spells to give, in order.", SPELLS,
                 armies)):
            # Closed, a list shows only what's picked; the arrow opens it.
            card = FoldCard(icon, title)
            card.viewLayout.setContentsMargins(48, 8, 16, 12)
            card.viewLayout.addWidget(_muted(_wrapping(CaptionLabel(hint))))
            picker = ListWidget()
            picker.setFixedHeight(240)
            picker.setDragDropMode(ListWidget.InternalMove)
            card.viewLayout.addWidget(picker)
            self._bind(path, _summarised(bind_unit_picker(picker, units, self._has_unit_picture), card.card, empty))
            _add_expanding(donate, card)
        # With no main troops picked: a set army for each Clan Castle level.
        from farmbot.castle import CASTLES

        card = FoldCard(FIF.LIBRARY, "Armies by Clan Castle level")
        card.card.setContent("Given when no main troops are picked")
        card.viewLayout.setContentsMargins(48, 8, 16, 12)
        card.viewLayout.addWidget(_muted(_wrapping(CaptionLabel(
            "With no main troops picked, a request gets the army set here for the requester's Clan Castle "
            "level (read from the room its request shows), split by commas, the first in colour going first: "
            "a name alone gives as many as fit, \"1 Super Yeti\" one at most. Units the request doesn't ask for, "
            "aren't boosted, or don't fit are passed over; with Fill the rest on, the room left goes to the biggest that fit. Donated units come at the castle's laboratory cap "
            "at most."))))
        edits = {}
        for castle in CASTLES:
            row = QHBoxLayout()
            label = BodyLabel(f"Level {castle.level}")
            label.setFixedWidth(70)
            room = _muted(CaptionLabel(f"{castle.troops} troops · {castle.spells} spells · {castle.siege} siege · "
                                       f"lab cap {castle.lab_cap}"))
            room.setFixedWidth(230)
            edits[str(castle.level)] = LineEdit()
            row.addWidget(label)
            row.addWidget(room)
            row.addWidget(edits[str(castle.level)], 1)
            card.viewLayout.addLayout(row)
        self.preset_error = CaptionLabel("")
        tint(self.preset_error, WARN)
        card.viewLayout.addWidget(self.preset_error)
        self._bind("clan.donate_presets", bind_presets(edits))
        _add_expanding(donate, card)
        self.switch(donate, "clan.donate_fill", FIF.ADD, "Fill the rest",
                    "After the main troops (or the Clan Castle level's army), fills the space left with the biggest "
                    "troops that still fit.")
        # The lists stay usable with donating off, so the choice can be made first.
        self.depends("clan.donate", "clan.donate_fill")

        request = page.add(SettingCardGroup("Requests"))
        self.switch(request, "clan.request_troops", FIF.SEND, "Request Clan Castle troops",
                    "Asks the clan for troops, then again as soon as the game lets you.")
        self.number(request, "clan.request_every_minutes", FIF.DATE_TIME, "If the timer can't be read",
                    "Minutes to wait between requests.", "min")
        self.depends("clan.request_troops", "clan.request_every_minutes")
        self._note(page, self._template_note("Donating", "Requesting troops"))

    def _raid_weekend_tab(self, page: Page) -> None:
        raids = page.add(SettingCardGroup("Clan Capital raids"))
        self.switch(raids, "capital.raid", FIF.FLAG, "Raid the Clan Capital on Raid Weekends",
                    "Friday to Monday (07:00 UTC): every raid attack, with the full capital army set in the game. "
                    "Never spends Capital Gold.")
        self.number(raids, "capital.miner_heal_at", FIF.HEART, "Heal Super Miners at",
                    "A heal goes where the miners are heading once their health falls under this.", "%")
        self.number(raids, "capital.fortress_heal_at", FIF.HEART, "Heal Flying Fortresses at",
                    "A heal goes on the fortresses once one falls under this.", "%")
        self.switch(raids, "capital.record", FIF.CAMERA, "Record raids",
                    "Saves each raid's screens, taps and log under screens/raids, to look into a raid that went "
                    "wrong. About 100 MB a raid.")
        self.depends("capital.raid", "capital.miner_heal_at", "capital.fortress_heal_at", "capital.record")
        self._note(page, self._template_note(catalog.CAPITAL))

    def _has_unit_picture(self, name: str) -> bool:
        from farmbot.units import slug

        return (Path(self.config.bot.templates) / "donate" / f"{slug(name)}.png").is_file()

    def _builder_base(self) -> TabbedPage:
        page = TabbedPage("builder_base", "Builder base", "Everything for builder battles. To play them, pick Builder "
                          "base under What to farm on the Dashboard. The bot attacks best with an all Night Witch "
                          "army.")
        self._builder_attack_tab(page.tab("attacking", "Attacking"))
        self._builder_village_tab(page.tab("village", "Village"))
        self._builder_upgrades_tab(page.tab("upgrades", "Upgrades", "Upgrades come first, then walls get the loot "
                                            "left."))
        return page

    def _builder_attack_tab(self, page: Page) -> None:
        deploy = page.add(SettingCardGroup("Deploying"))
        self.segments(deploy, "builder.corner", FIF.MOVE, "Go in from",
                      "Auto picks the map corner farthest from the enemy buildings.",
                      {"auto": "Auto", "left": "Left", "top": "Top", "right": "Right"}, width=80)
        self.switch(deploy, "builder.use_hero", FIF.PEOPLE, "Hero goes first", "Off: troops only.")
        self.segments(deploy, "builder.first_hero", FIF.PEOPLE, "First hero",
                      "The hero for the first base. Keep: whichever is set in the game.",
                      {"any": "Keep", "machine": "Battle Machine", "copter": "Battle Copter"}, width=120)
        self.switch(deploy, "builder.switch_hero", FIF.ROTATE, "Switch hero when hurt",
                    "Before a base, swaps to your other builder hero if this one's health is low.")
        self.number(deploy, "builder.switch_below", FIF.HEART,
                    "Switch below", "Hero health that triggers the swap.", "%")
        self.number(deploy, "builder.troop_delay", FIF.STOP_WATCH, "Troops follow after",
                    "Seconds after the hero lands.", "s")
        self.speed(deploy, "builder.troop_speed", FIF.SPEED_HIGH, "Troop speed",
                   "How quickly the troops go down, one after another.")
        self.depends("builder.use_hero", "builder.troop_delay", "builder.first_hero", "builder.switch_hero")
        self.depends("builder.switch_hero", "builder.switch_below")

        abilities = page.add(SettingCardGroup("Abilities"))
        self.switch(abilities, "builder.hero_ability", FIF.BRIGHTNESS, "Hero ability",
                    "Fired whenever the three charge bars over its card are full (and by the game when it's "
                    "about to die).")
        self.number(abilities, "builder.hero_ability_after", FIF.STOP_WATCH, "If the bars can't be seen",
                    "Fires it once this many seconds after the hero lands instead.", "s")
        self.depends("builder.hero_ability", "builder.hero_ability_after")
        self.switch(abilities, "builder.troop_abilities", FIF.BRIGHTNESS, "Troop abilities",
                    "Taps the cards of troops still on the field, a few at a time.")
        self.number(abilities, "builder.ability_delay", FIF.STOP_WATCH, "Between taps",
                    "Seconds from one round of ability taps to the next.", "s")
        self.number(abilities, "builder.ability_batch", FIF.ALBUM, "Cards per tap",
                    "How many troop cards are tapped together.")
        self.depends("builder.troop_abilities", "builder.ability_delay", "builder.ability_batch")

        battle = page.add(SettingCardGroup("Battle"))
        self.switch(battle, "builder.speed_up", FIF.SPEED_HIGH, "Play at 4x speed",
                    "Presses the 1x button whenever it shows.")
        self.switch(battle, "builder.return_home_after", FIF.AIRPLANE, "Sail home when the bot stops",
                    "Ends in the home village rather than the builder base.")
        self.number(battle, "builder.stage_delay", FIF.STOP_WATCH, "Wait before the second base",
                    "Seconds after the first base falls, while the second loads.", "s")

    def _builder_village_tab(self, page: Page) -> None:
        loot = page.add(SettingCardGroup("Collecting"))
        self.switch(loot, "builder.collect_resources", FIF.DOWNLOAD,
                    "Collect the gold mines, elixir collectors and gem mine",
                    "Looks over both bases, so it takes a trip to the second base and back.")
        self.number(loot, "builder.collect_every", FIF.DATE_TIME, "Every",
                    "1 = before each battle. Always the first battle after Start.", "battles")
        self.depends("builder.collect_resources", "builder.collect_every")
        self.switch(loot, "builder.collect_cart", FIF.SHOPPING_CART, "Collect the elixir cart",
                    "The cart by the dock that holds your defense rewards.")
        self.number_range(loot, "builder.cart_every_min", "builder.cart_every_max", FIF.DATE_TIME, "Every",
                          "A random number of battles in this range each time.",
                          "battles")
        self.depends("builder.collect_cart", "builder.cart_every_min", "builder.cart_every_max")
        self.switch(loot, "builder.clock_tower", FIF.SPEED_HIGH, "Use the Clock Tower's free boost",
                    "Whenever its clock shows (once a day). Never the Boost that uses a magic item.")

        self._storage_group(page, "builder")

    def _builder_upgrades_tab(self, page: Page) -> None:
        ups = page.add(SettingCardGroup("Upgrades"))
        self.switch(ups, "builder.upgrade", FIF.UP, "Upgrade when storage is full",
                    UPGRADE_HELP)
        self.priority(ups, "builder.upgrade_order", "What to upgrade first",
                      ORDER_HELP,
                      {"research": ("builder.upgrade_lab", FIF.EDUCATION, "Research", ("elixir",)),
                       "hero": ("builder.upgrade_heroes", FIF.PEOPLE, "Heroes", ("elixir",)),
                       "building": ("builder.upgrade_buildings", FIF.HOME, "Buildings", ("gold", "elixir"))})
        self.segments(ups, "builder.upgrade_first", FIF.PIN, "Buildings: this kind first",
                      FIRST_HELP,
                      {"none": "Any", "defense": "Defenses", "resource": "Resources", "army": "Army"}, width=90)
        self.builder_builders_card = self._builders(
            ups, "builder.upgrade_builders", "How many of the builder base's builders (the Master Builder, and "
            "O.T.T.O once you have him) upgrades may keep busy; the rest stay free for walls or for you. "
            "Default: all but one. From your village data.")
        self.depends("builder.upgrade", "builder.upgrade_order", "builder.upgrade_heroes", "builder.upgrade_lab",
                     "builder.upgrade_buildings", "builder.upgrade_first", "builder.upgrade_builders")
        walls = page.add(SettingCardGroup("Walls"))
        self.switch(walls, "builder.walls", FIF.TILES, "Upgrade walls with spare loot",
                    "Before each battle, as many of the cheapest walls as the spare loot covers. Off by default: "
                    "it spends loot.")
        self.segments(walls, "builder.walls_use", FIF.TAG, "Pay with", "Which resource may go on walls.",
                      {"gold": "Gold", "elixir": "Elixir", "both": "Either"}, width=90)
        self.share(walls, "builder.walls_keep_gold", icons.icon("gold", 28), "Keep this much gold",
                   "A share of your gold storages; walls get only what's above it.",
                   "storage.builder_gold", "gold")
        self.share(walls, "builder.walls_keep_elixir", icons.icon("elixir", 28), "Keep this much elixir",
                   "A share of your elixir storages; walls get only what's above it.",
                   "storage.builder_elixir", "elixir")
        self.depends("builder.walls", "builder.walls_use", "builder.walls_keep_gold", "builder.walls_keep_elixir")
        self._note(page, self._template_note("Builder base upgrades", "Upgrades"))

    HABIT_ICONS = {"tap_spread": "MOVE", "tap_hold": "STOP_WATCH", "waits": "HISTORY", "swipes": "SCROLL",
                   "curved_swipes": "SCROLL", "chore_order": "ROTATE", "odd_hours": "DATE_TIME",
                   "battle_rhythm": "MUSIC", "look_at_bases": "VIEW", "result_pause": "PAUSE",
                   "end_spread": "STOP_WATCH", "scout_look": "ZOOM"}

    def _like_a_person(self) -> Page:
        page = Page("like_a_person", "Like a person",
                    "Small habits that make it tap, wait and fight less like a machine. Each one is a switch: "
                    "turn on the ones you want. Most cost a little time; the table shows how much.")
        top = page.add(SettingCardGroup("Like a person"))
        self.switch(top, "human.enabled", FIF.FINGERPRINT, "Play like a person",
                    "Off: every habit below is off, and it plays the same way every time, as fast as it can.")
        # A round's time, part by part: without the habits, what the ones on
        # add, and with them; for each village.
        self.human_tables: dict[str, dict[str, tuple[QLabel, QLabel, QLabel, QLabel]]] = {}
        self.human_notes: dict[str, QLabel] = {}
        self._timings: Timings | None = None
        for village, name in (("home", "Home village: one round"), ("builder", "Builder base: one round")):
            card = SimpleCardWidget()
            grid = QGridLayout(card)
            grid.setContentsMargins(20, 14, 20, 14)
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(6)
            grid.addWidget(StrongBodyLabel(name), 0, 0)
            for col, head in enumerate(("Without habits", "Added", "With these"), start=1):
                label = CaptionLabel(head)
                tint(label, MUTED)
                grid.addWidget(label, 0, col, Qt.AlignRight)
            rows = {}
            for r, phase in enumerate(timings_phases(village) + ["total"], start=1):
                kind = StrongBodyLabel if phase == "total" else BodyLabel
                cells = tuple(kind("") for _ in range(4))
                for col, cell in enumerate(cells):
                    grid.addWidget(cell, r, col, Qt.AlignLeft if col == 0 else Qt.AlignRight)
                rows[phase] = cells
            note = CaptionLabel("")
            note.setWordWrap(True)
            tint(note, MUTED)
            grid.addWidget(note, len(rows) + 1, 0, 1, 4)
            grid.setColumnStretch(0, 1)
            self.human_tables[village] = rows
            self.human_notes[village] = note
            page.add(card)
        self.habit_cards: dict[str, Card] = {}
        for group, title in (("touch", "Taps, waits and swipes"), ("village", "Village"), ("battle", "Battles")):
            box = page.add(SettingCardGroup(title))
            for habit in (h for h in HABITS if h.group == group):
                icon = getattr(FIF, self.HABIT_ICONS.get(habit.key, "PEOPLE"), FIF.PEOPLE)
                self.habit_cards[habit.key] = self.switch(box, f"human.{habit.key}", icon, habit.title, habit.text)
        self.depends("human.enabled", *(f"human.{h.key}" for h in HABITS))
        return page

    def timings(self) -> Timings:
        """This farm's round timings (what the "Like a person" page shows)."""
        if self._timings is None:
            try:
                self._timings = Timings(instances.resolve(self.config, TIMINGS_FILE))
            except Exception:  # no farm folder yet: the typical numbers
                self._timings = Timings()
        return self._timings

    def _show_human(self, form: dict[str, Any]) -> None:
        """The "Like a person" page: the round's times with the habits that
        are on, and what each habit costs."""
        if not hasattr(self, "human_tables"):
            return
        on = bool(form.get("human.enabled"))
        habits = {h.key for h in HABITS if on and form.get(f"human.{h.key}")}
        timings = self.timings()
        for village, rows in self.human_tables.items():
            table, measured = timings.summary(village, habits)
            for (name, plain, added, total), key in zip(table, timings_phases(village) + ["total"]):
                cells = rows[key]
                cells[0].setText(name)
                cells[1].setText(duration(plain))
                cells[2].setText(cost(added) if added >= 0.005 else "–")
                cells[3].setText(duration(total))
            n = timings.rounds(village)
            self.human_notes[village].setText(
                f"Measured: the average of your last {n} round{'s' if n != 1 else ''} here." if measured else
                "Typical times (from test runs): yours show here after a few rounds. Upgrades and village care "
                "vary a lot with what there is to do.")
        for key, card in self.habit_cards.items():
            habit = next(h for h in HABITS if h.key == key)
            home, builder = timings.habit_cost("home", key), timings.habit_cost("builder", key)
            if key == "odd_hours":
                price = "Adds no time to a round: it plays a little less of the first and last hour each day."
            elif key in ("tap_spread", "chore_order") or max(home, builder) < 0.005:
                price = "Adds no time."
            else:
                price = f"Adds about {cost(home).lstrip('+')} per home round, {cost(builder).lstrip('+')} per " \
                        "builder base round."
            card.help_text = f"{habit.text} {price}"
            if not card.contentLabel.text().startswith("⚠"):
                card.setContent(card.help_text)

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
            b = toggle_button(f"{h:02d}")
            b.setMinimumWidth(36)  # twelve share the card's width: fixed widths overlapped at the window's smallest
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        hours_group.addSettingCard(grid_card)
        self.depends("schedule.enabled", "schedule.active_hours")

        breaks = page.add(SettingCardGroup("Breaks"))
        self.switch(breaks, "schedule.breaks", FIF.CAFE, "Take breaks",
                    "Rests now and then, at random, to play more like a person.")
        self.number_range(breaks, "schedule.break_every_min", "schedule.break_every_max", FIF.DATE_TIME,
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
            b = toggle_button(icons.icon(key, 28), f"  {name}")
            b.setMinimumWidth(104)
            b.setIconSize(QSize(28, 28))
            b.setMinimumHeight(44)
            b.setToolTip(f"Stop once your {name.lower()} storage is full")
            row.addWidget(b)
            buttons[key] = b
        self._bind("bot.stop_when_full", bind_toggles(buttons))
        self.stop_full_buttons = buttons
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

        self._spawn(work)

    def _open_screen(self) -> None:
        import cv2

        from farmbot.game import normalize_screen
        from farmbot.vision import Vision

        path, _ = QFileDialog.getOpenFileName(self, "Open screenshot", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path:
            return
        image = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            notice(InfoBarIcon.ERROR, "Can't open that file", path, duration=4000, parent=self)
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
            notice(InfoBarIcon.ERROR, "Couldn't save", str(exc), duration=4000, parent=self)
            return
        notice(InfoBarIcon.SUCCESS, "Picture saved", f"{picture.title}: COCFARM uses it from now on.", duration=2500,
               parent=self)
        self.canvas.select(None)
        self._refresh_pictures(keep=picture.name)

    # ------------------------------------------------------------ setup
    STEP_TITLES = ("1.  Pick your emulator", "2.  Connect and check the game", "3.  Choose the loot you want",
                   "4.  Start farming")

    def _setup(self) -> Page:
        page = Page("setup", "Setup", "Four steps to get farming. COCFARM sets the emulator up by itself.")
        self.steps: list[SettingCardGroup] = []

        step = self._step(page, 0)
        self.segments(step, "device.mode", FIF.PHONE, "Emulator",
                      "The Android emulator Clash of Clans runs in on this PC. Other: any device by its address "
                      "(Advanced).", {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer", "mumu": "MuMu",
                                     "manual": "Other"}, width=90)
        self.bluestacks_info = BodyLabel("")
        self.bluestacks_info.setWordWrap(True)
        self.bluestacks_info.setMinimumWidth(240)
        self.bluestacks_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        refresh = PushButton(FIF.SYNC, "Look again")
        refresh.setToolTip("Look for BlueStacks, LDPlayer and MuMu again, e.g. after installing one")
        refresh.clicked.connect(self._find_bluestacks)
        self.found_card = self._card(step, None, FIF.VIEW, "On this PC", "", self.bluestacks_info, refresh)
        self.instance_box = EditableComboBox()
        self.instance_box.setFixedWidth(240)
        self.instance_box.setPlaceholderText("Main instance")
        self._bind("device.bluestacks_instance", bind_combo(self.instance_box))
        self.instance_card = self._card(step, "device.bluestacks_instance", FIF.APPLICATION, "BlueStacks window",
                                        "Which BlueStacks instance plays the game. Empty uses the main one.",
                                        self.instance_box)
        self.depends("device.mode", "device.bluestacks_instance", when="bluestacks")
        self.switch(step, "device.launch_emulator", FIF.ROBOT, "Open the emulator automatically",
                    "When you press Start and it isn't open yet.")

        step = self._step(page, 1)
        test = PrimaryPushButton(FIF.SYNC, "Check now")
        test.clicked.connect(self._test_connection)
        self.test_button = test
        self.device_result = CaptionLabel("Not checked yet")
        self.device_result.setWordWrap(True)
        tint(self.device_result, MUTED)
        self._card(step, None, FIF.CONNECT, "Connect to the emulator",
                   "Opens the emulator if needed and sets it up for COCFARM (it may restart once).",
                   self.device_result, test)
        self.game_installed = CaptionLabel("Not checked yet")
        self.game_logged_in = CaptionLabel("Not checked yet")
        for label in (self.game_installed, self.game_logged_in):
            label.setWordWrap(True)
            tint(label, MUTED)
        self._card(step, None, FIF.GAME, "Clash of Clans installed",
                   "Install it from the Play Store inside the emulator.", self.game_installed)
        self._card(step, None, FIF.VPN, "Signed in to your account",
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
            card = Card(icons.icon(path.rsplit("_", 1)[-1], 28), name, "Bases offering less are skipped; 0 ignores it. "
                        "Untouched, it's set for your Town Hall.", w)
            step.addSettingCard(card)
            self._mirror(path, bind_line(with_commas(w)), card)
            w.textEdited.connect(self._minimums_typed)
        more = PushButton(FIF.FILTER, "More choices")
        more.clicked.connect(lambda: self.show_page("Home village", "bases"))
        self._card(step, None, FIF.MORE, "Want more control?",
                   "Totals, any-one-is-enough, and when to skip searching: Home village → Bases to attack.", more)

        step = self._step(page, 3)
        go = PrimaryPushButton(FIF.PLAY, "Start")
        go.clicked.connect(self.start)
        self.setup_start = go
        self._card(step, None, FIF.PLAY, "Press Start",
                   "It opens the game, collects loot and starts attacking. Watch it on the Dashboard; Pause (Esc) any time.", go)

        alerts = PushButton(FIF.RINGER, "Set up alerts")
        alerts.clicked.connect(lambda: self.show_page("Advanced", "alerts"))
        self._card(step, None, FIF.MESSAGE, "Alerts on your phone (optional)",
                   "A Discord message when the bot stops, a storage fills, or each day: Advanced → Alerts.", alerts)
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
            if not self._syncing and self._value(path) != binding.get():
                self._set_value(path, binding.get())

        binding.connect(changed)

    def _sync_mirrors(self) -> None:
        self._syncing = True
        try:
            for path, bindings in self.mirrors.items():
                value = self._value(path)
                for b in bindings:
                    if b.get() != value:
                        b.set(value)
        finally:
            self._syncing = False

    def show_village(self) -> None:
        """The village data kept for this account, in a window."""
        self._dialog = VillageDialog(self.village_file(self.config.profiles.active or None), self)
        self._dialog.show()

    def _minimums_typed(self, *_) -> None:
        self.config.search.minimums_by_hand = True

    def _suggested_minimums(self) -> None:
        """Back to the Town Hall's amounts (or the starting ones), which then
        follow the Town Hall again."""
        from farmbot.config import SEARCH_DEFAULTS, town_hall_minimums

        self.config.search.minimums_by_hand = False
        village = self.current_village()
        amounts = town_hall_minimums(village.town_hall) if village else SEARCH_DEFAULTS
        for kind, amount in amounts.items():
            self.bindings[f"search.min_{kind}"].set(f"{amount:,}")
        self._changed()

    def _show_suggested(self) -> None:
        from farmbot.config import town_hall_minimums

        card = getattr(self, "suggested_card", None)
        if card is None:
            return
        village = self.current_village()
        if village is None:
            card.help_text = ("Your Town Hall isn't known yet (the bot reads it from the game): this sets "
                              "300,000 gold and elixir.")
        else:
            amount = town_hall_minimums(village.town_hall)["gold"]
            card.help_text = (f"For Town Hall {village.town_hall}: {amount:,} gold and {amount:,} elixir, no dark "
                              "elixir. " + ("You've set your own, so they stay as you typed them."
                                            if self.config.search.minimums_by_hand else
                                            "In use: they follow your Town Hall as it goes up."))
        card.show_error(None)

    def _fit_search_minimums(self, storage: dict[str, int] | None = None) -> None:
        """Search minimums never set by hand: from the Town Hall if it's
        known, else the storages (``storage``: just read; None: as shown),
        as the bot does (see ``config.search_minimums_for``)."""
        from farmbot.config import search_minimums_for

        paths = {k: f"search.min_{k}" for k in ("gold", "elixir", "dark")}
        if any(self._value(p) is None for p in paths.values()):
            return
        try:
            current = {k: parse_amount(self._value(p) or "0") for k, p in paths.items()}
            if storage is None:
                storage = {k: parse_amount(self._value(f"storage.{k}")) for k in paths
                           if str(self._value(f"storage.{k}") or "").strip()}
        except ValueError:
            return
        village = self.current_village()
        for kind, least in search_minimums_for(storage, current, village and village.town_hall,
                                               self.config.search.minimums_by_hand).items():
            if self._value(paths[kind]) != f"{least:,}":
                self._set_value(paths[kind], f"{least:,}")

    def _steps_done(self) -> list[bool]:
        mode = self.bindings["device.mode"].get()
        found = (mode == "manual" or (mode == "bluestacks" and self._found.get("bluestacks"))
                 or (mode in ("ldplayer", "mumu") and self._found.get(mode)))
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

    def _emulator_defaults(self) -> ListCard:
        """What COCFARM sets each emulator to (``farmbot/standard.py``): shown,
        not settings."""
        card = ListCard("Emulator set-up (fixed)",
                        "What COCFARM sets each emulator to, the screen it's trained on. These can't be changed: "
                        "anything changed in the emulator is put back when COCFARM connects (it restarts once).")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        rows = emulator_defaults()
        names = list(rows)
        for col, name in enumerate(names, start=1):
            grid.addWidget(StrongBodyLabel(name), 0, col)
        for row, setting in enumerate(rows[names[0]], start=1):
            grid.addWidget(_muted(BodyLabel(setting)), row, 0)
            for col, name in enumerate(names, start=1):
                grid.addWidget(BodyLabel(rows[name][setting]), row, col)
        grid.setColumnStretch(len(names) + 1, 1)
        card.column.addLayout(grid)
        self.emulator_defaults_card = card
        return card

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
        page = TabbedPage("advanced", "Advanced", "Technical settings most players never need, alerts on your "
                          "phone, and the tools for teaching COCFARM new buttons.")
        tab = page.tab("settings", "Settings")
        emulator = tab.add(SettingCardGroup("Emulator"))
        self.switch(emulator, "device.enable_adb", ICON_TOOLS, "Turn on ADB",
                    "ADB is how COCFARM controls the emulator in the background. If it's off, COCFARM turns it on "
                    "(the emulator restarts once).")
        self.depends("device.mode", "device.enable_adb", when=("bluestacks", "ldplayer"))  # always on in MuMu
        self.switch(emulator, "device.mute", FIF.MUTE, "Keep the emulator muted",
                    "Turns the emulator's sound down to nothing each time COCFARM connects to it. On by default. "
                    "Off: the volume is left as you set it.")
        emulator.addSettingCard(self._emulator_defaults())
        self.ldplayer_box = EditableComboBox()
        self.ldplayer_box.setFixedWidth(240)
        self.ldplayer_box.setPlaceholderText("Newest installed")
        self._bind("device.ldplayer", bind_combo(self.ldplayer_box))
        self._card(emulator, "device.ldplayer", FIF.APPLICATION, "LDPlayer version",
                   "Which LDPlayer to use (9 or newer). Empty uses the newest one installed.", self.ldplayer_box)
        self.number(emulator, "device.ldplayer_index", FIF.COPY, "LDPlayer instance number",
                    "1 is the first instance in LDMultiPlayer.")
        self.depends("device.mode", "device.ldplayer", "device.ldplayer_index", when="ldplayer")
        self.number(emulator, "device.mumu_index", FIF.COPY, "MuMu instance number",
                    "1 is the first instance in MuMu Multi-instance.")
        self.depends("device.mode", "device.mumu_index", when="mumu")
        self.text(emulator, "device.serial", FIF.CONNECT, "Device address",
                  "For Emulator = Other: the ADB address of any emulator or phone, e.g. 127.0.0.1:5555.", width=200)
        self.depends("device.mode", "device.serial", when="manual")
        self.text(emulator, "device.adb_path", ICON_TOOLS, "ADB program",
                  "\"adb\" uses the one that comes with the emulator; or the full path to adb.exe.", width=200)

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
        news = PushButton(FIF.MEGAPHONE, "What's new")
        news.clicked.connect(lambda: self._whats_new(force=True))
        self.update_card.hBoxLayout.insertWidget(self.update_card.hBoxLayout.indexOf(self.update_button), news)
        self.switch(updates, "bot.check_updates", FIF.ROBOT, "Look for updates by itself",
                    "Checks GitHub when the window opens, and every hour while it's open.")
        tab = page.tab("alerts", "Alerts", "Messages on your phone through a Discord channel, so you know what the "
                       "bot did while you were away. Optional.")
        notify = tab.add(SettingCardGroup("Discord"))
        self.text(notify, "notify.discord_webhook", FIF.LINK, "Discord webhook",
                  "Messages go to a Discord channel: Server Settings → Integrations → Webhooks → New Webhook → "
                  "Copy Webhook URL, then paste it here.", width=300)
        self.switch(notify, "notify.on_storage_full", FIF.SAVE, "When storage is full",
                    "A message when the storages you picked on When to play are full.")
        self.switch(notify, "notify.on_stopped", FIF.POWER_BUTTON, "When the bot stops",
                    "After a run, or when a problem stops it.")
        self.switch(notify, "notify.on_emulator_restart", FIF.PHONE, "When the emulator is restarted",
                    "A message when the game kept failing and COCFARM restarted the emulator.")
        self.switch(notify, "notify.daily_summary", FIF.MAIL, "Daily summary",
                    "Yesterday's attacks and loot, sent after midnight.")
        test_notify = PushButton(FIF.SEND, "Send a test")
        test_notify.clicked.connect(self._test_notify)
        self.notify_result = CaptionLabel("")
        self._card(notify, None, FIF.SEND, "Test", "Sends a message to the webhook above.",
                   self.notify_result, test_notify)
        self._pictures(page.tab("pictures", "Pictures", "COCFARM finds buttons by comparing the screen with small "
                                "pictures of them. Pick one, show the screen it's on, and drag a box tightly around it."))
        return page

    def _welcome(self) -> None:
        """First run: open Setup, where the steps say what to do."""
        self.show_page("Setup")
        notice(InfoBarIcon.INFORMATION, "Welcome to COCFARM",
               "Follow the four steps on this page. It takes a couple of minutes.", duration=8000, parent=self)

    def _check_for_update(self, force: bool = False) -> None:
        """Ask GitHub for a newer COCFARM in the background (see farmbot/update.py
        for how often it really asks)."""
        repo = self.config.bot.update_repo
        if self.farm > 1:  # the main window looks for updates for every farm
            return
        if not repo or self.update_busy or not (force or self.config.bot.check_updates):
            return
        if force:
            self.update_button.setEnabled(False)
            self._update_note("Asking GitHub…")

        def work() -> None:
            from farmbot import update

            self.events.put(("update", (update.check(repo, force=force), force)))

        self._spawn(work)

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

        self._spawn(work)

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
        others = [n for n in self.children if self.farm_running(n)]
        if others:  # the update restarts COCFARM: every farm stops
            notice(InfoBarIcon.WARNING, "Switch the other farms off first",
                   "Updating restarts COCFARM. Switch farm " + ", ".join(map(str, others))
                   + " off on the Farms page, then press Restart again.", duration=8000, parent=self)
            self.update_button.setEnabled(True)
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
            notice(InfoBarIcon.SUCCESS if message.startswith("Updated") else InfoBarIcon.WARNING, "Update", message,
                   duration=10000, parent=self)

    def _whats_new(self, force: bool = False) -> None:
        """Once after an update (or on request): what changed, in a player's words."""
        from farmbot import whatsnew

        if self.farm > 1 and not force:  # the main window tells: once is enough
            return
        notes = (whatsnew.latest() if force
                 else whatsnew.unseen(self.config_path.with_name("update.json"), first_run=self.first_run))
        if notes:
            WhatsNewDialog(notes, self).show()  # not modal: nothing waits on it

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

        self._spawn(work)

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

        self._spawn(work)

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
        """Fill in what's installed: BlueStacks, every LDPlayer, and MuMu."""
        from farmbot import bluestacks, ldplayer, mumu

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
        mumu_install = mumu.find_install()
        lines.append("✓ MuMu Player: installed" if mumu_install else "✗ MuMu Player: not installed")
        self._found = {"bluestacks": install is not None, "ldplayer": bool(installs), "mumu": mumu_install is not None}
        if not any(self._found.values()):
            lines.append("Install BlueStacks 5 (bluestacks.com), LDPlayer 9 (ldplayer.net) or MuMu Player "
                         "(mumuplayer.com), then press Look again.")
        self.bluestacks_info.setText(chr(10).join(lines))
        tint(self.bluestacks_info, WARN if not any(self._found.values()) else None)
        self._show_steps()

    # ------------------------------------------------------------ form state
    def collect(self) -> dict[str, Any]:
        # Every setting: those of pages not built yet as kept (and while one is being built, too).
        return {**{path: b.get() for path, b in self.bindings.items()}, **self._pending}

    def _load(self) -> None:
        self._loading = True
        self._timings = None  # (another farm's, perhaps)
        self._village_for = ()  # (and its village export)
        for path, value in form_values(self.config).items():
            if path in self.bindings:
                self.bindings[path].set(value)
            elif self._lazy:  # on a page not built yet: kept as its box would give it back
                kind = BY_PATH[path].kind if path in BY_PATH else ""
                self._pending[path] = str(value) if kind in ("int", "instance") and isinstance(value, int) else value
        self._loading = False
        self._fit_search_minimums()  # this account's Town Hall's, if known and never set by hand

    def _changed(self, save: bool = True) -> None:
        if self._loading:
            return
        self._sync_mirrors()
        form = self.collect()
        _, errors = validate(form)
        if any(path in self._pending for path in errors):
            # A mistake on a page not built yet: built now, so its red text and badge show.
            self.build_all_pages()
            return  # (each built page went through here again)
        self.has_errors = bool(errors)
        self._show_shares(form)
        if (not self._running() and self.status_now.state == "ready" and not errors
                and self.status_now.headline == "Ready to farm"):
            # What Start will do, so a quick look shows if it's set right.
            self._show_status(Status("ready", "Ready to farm", plan_summary(validate(form)[0])))
        for path, card in self.cards.items():
            card.show_error(errors.get(path))
        for path, cards in self.mirror_cards.items():
            for card in cards:
                card.show_error(errors.get(path))
        self._show_human(form)
        if hasattr(self, "preset_error"):  # (on pages built when first opened)
            self.preset_error.setText(f"⚠  {errors['clan.donate_presets']}" if "clan.donate_presets" in errors else "")
        if hasattr(self, "hours_error"):
            self.hours_error.setText(f"⚠  {errors['schedule.active_hours']}" if "schedule.active_hours" in errors
                                     else "")
        values = validate(form)[0]
        minimums = {k: values.get(f"search.min_{k}", 0) for k in ("gold", "elixir", "dark")}
        for k, v in unreachable_minimums(minimums).items():
            path = f"search.min_{k}"
            if path not in errors:
                note = unreachable_note(k, v, values.get("search.match_any", False))
                for card in [self.cards.get(path), *self.mirror_cards.get(path, [])]:
                    if card is not None:
                        card.show_warning(note)
        self.instance_card.setVisible(form.get("device.mode") == "bluestacks")
        for path, mode in EMULATOR_ONLY.items():  # only the chosen emulator's settings
            card = self.cards[path]
            if card.isVisibleTo(card.parentWidget()) != (form.get("device.mode") == mode):
                card.setVisible(form.get("device.mode") == mode)
                QTimer.singleShot(0, lambda g=card.parentWidget(): _relayout_group(g))
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
        self._show_locked(form, values)  # after: what's locked stays greyed out
        self.start_button.setEnabled(not errors and (not self._running() or self.pause_event.is_set()))
        self.setup_start.setEnabled(self.start_button.isEnabled() and not self._running())
        self._show_steps()
        self._show_big_buttons()
        if form.get("bot.theme") in THEMES and form.get("bot.theme") != self._theme:
            self._apply_theme(form.get("bot.theme"))
        self._show_error_places(errors)
        if not errors:
            self.save_label.setToolTip("")
            self.save_label.unsetCursor()
        if errors:
            self.save_label.setText(f"⚠ Fix {len(errors)} setting{'s' if len(errors) > 1 else ''}")
            self.save_label.setToolTip("Show me")
            self.save_label.setCursor(Qt.PointingHandCursor)
            tint(self.save_label, BAD)
            self.save_timer.stop()
        elif save:
            self.save_label.setText("Saving…")
            tint(self.save_label, MUTED)
            self.save_timer.start()

    def village_file(self, profile: str | None) -> Path:
        """Where the account's village export is kept (``farmbot/townhall.py``)."""
        if profile:
            return self.profiles.village_path(profile)
        if getattr(self.config, "home", None) is None:
            return self.config_path.parent / townhall.VILLAGE_FILE  # (farm 1: beside its settings)
        return instances.resolve(self.config, townhall.VILLAGE_FILE)

    def current_village(self) -> townhall.Village | None:
        """The export of the account these settings are for, if read."""
        profile = self.config.profiles.active or None
        if getattr(self, "_village_for", ()) != (profile,):
            self._village_for = (profile,)
            try:
                self._village = townhall.load(self.village_file(profile))
            except Exception:  # no farm folder yet
                self._village = None
        return self._village

    def _show_locked(self, form: dict[str, Any], values: dict[str, Any]) -> None:
        """Mark the settings this account's Town Hall hasn't unlocked yet
        (they're skipped while it plays)."""
        village = self.current_village()
        if hasattr(self, "town_hall_label"):
            self.town_hall_label.setText(townhall.describe(village))
        if hasattr(self, "builders_card"):
            self.builders_card.set_total(village.builders if village else 0)
        if hasattr(self, "builder_builders_card"):
            self.builder_builders_card.set_total(village.builder_base_builders if village else 0)
        locked = townhall.locked(village)
        self._grey_locked(locked, village.town_hall if village else 0)
        self._show_suggested()
        if not locked:
            return
        th = village.town_hall

        def warn(path: str, feature: str, then: str = "Skipped until then.") -> None:
            note = f"Town Hall {th}: {locked[feature]}. {then}"
            for card in [self.cards.get(path), *self.mirror_cards.get(path, [])]:
                if card is not None:
                    card.show_warning(note)

        # (the rest: greyed out, with a notice atop their tab: _grey_locked)
        if values.get("search.min_dark", 0) and "dark" in locked:
            warn("search.min_dark", "dark", "No base offers it yet: keep this at 0, or none will do.")
        if form.get("bot.mode") in ("builder", "auto") and "builder_base" in locked:
            warn("bot.mode", "builder_base", "It farms the home village until then.")

    # Per tab of the home village: feature -> the settings it greys out.
    LOCKED_PARTS = {
        "bases": {"dark": ("search.min_dark",)},
        "attacking": {"spells": ("attack.spells", "attack.spell_placement", "attack.spell_spot"),
                      "heroes": ("attack.hero_abilities", "attack.hero_ability_delay", "attack.ability_health",
                                 "attack.ability_max_wait"),
                      "super_troops": ("attack.super_troops",),
                      "siege": ("attack.use_siege",)},
        "village": {"dark": ("bot.collect_dark", "storage.dark")},
        "upgrades": {"research": (), "heroes": (), "pets": (), "wall_elixir": ()},  # (rows; walls' Pay with)
    }
    # The same, per tab of the builder base (once it's open).
    BUILDER_LOCKED_PARTS = {
        "attacking": {"builder_heroes": ("builder.use_hero", "builder.troop_delay", "builder.first_hero",
                                         "builder.switch_hero", "builder.switch_below", "builder.hero_ability",
                                         "builder.hero_ability_after")},
    }
    LOCKED_ROWS = {"research": "research", "heroes": "hero", "pets": "pets"}
    # Single choices: feature -> (setting, its choices greyed out, the one to pick instead).
    LOCKED_CHOICES = {"builder_base": (("bot.mode", ("builder", "auto"), None),
                                       ("bot.auto_first", ("builder",), None)),
                      "wall_elixir": (("walls.use", ("elixir", "both"), "gold"),)}
    SHARED_CARDS = {"bot.collect_dark"}  # one toggle on a card with others: only it greys out

    def _grey_locked(self, locked: dict[str, str], th: int) -> None:
        """Pages, tabs and settings this Town Hall hasn't unlocked: greyed
        out, with a notice on top saying why (all back once unlocked)."""
        builder = self.pages.get("Builder base")
        if builder is not None:
            builder.show_lock(f"Not available at Town Hall {th}: {locked['builder_base']}. Everything here is "
                              "skipped until then, and it farms the home village instead."
                              if "builder_base" in locked else "")
        home = self.pages.get("Home village")
        if home is None:
            return
        for tab in ("clan", "raids"):
            if tab in home.tab_pages:
                home.tab_pages[tab].show_lock(
                    f"Not available at Town Hall {th}: {locked['clan']}. Everything here is skipped until then."
                    if "clan" in locked else "", whole=True)
        for feature, settings in self.LOCKED_CHOICES.items():
            off = feature in locked
            for path, keys, instead in settings:
                if path not in self.bindings:
                    continue
                w = self.bindings[path].widgets[0]
                for key in keys:
                    w.items[key].setEnabled(not off)
                    w.items[key].setToolTip(f"Town Hall {th}: {locked[feature]}" if off else "")
                if off and instead and self.bindings[path].get() in keys:
                    self.bindings[path].set(instead)
                w.fader.refresh()
        dark = getattr(self, "stop_full_buttons", {}).get("dark")
        if dark is not None and "dark" in locked:
            dark.setEnabled(False)  # (the dependents may turn it back on: none do)
            dark.setToolTip(f"Town Hall {th}: {locked['dark']}")
        elif dark is not None:
            dark.setEnabled(True)
            dark.setToolTip("Stop once your dark elixir storage is full")
        when = self.pages.get("When to play")
        if when is not None:
            when.show_lock(f"Town Hall {th} hasn't unlocked these yet, so they're greyed out and skipped:\n"
                           f"•  {townhall.NAMES['dark']}: {locked['dark']} (the Dark storage below)"
                           if "dark" in locked else "")
        listing = getattr(self, "upgrade_list", None)
        self._grey_parts(self.pages.get("Builder base"), self.BUILDER_LOCKED_PARTS, locked, th)
        for tab, parts in self.LOCKED_PARTS.items():
            lines = []
            for feature, paths in parts.items():
                off = feature in locked
                if off:
                    lines.append(f"•  {townhall.NAMES[feature]}: {locked[feature]}")
                for path in paths:
                    if path in self.SHARED_CARDS:
                        if off and path in self.bindings:  # (the dependents turned it on just before, if they should)
                            self.bindings[path].enable(False)
                        continue
                    for card in [self.cards.get(path), *self.mirror_cards.get(path, [])]:
                        if card is not None:
                            card.setEnabled(not off)
                row = self.LOCKED_ROWS.get(feature) if tab == "upgrades" else None
                if row and listing is not None and row in listing.rows:
                    listing.rows[row].setEnabled(not off)
            if tab in home.tab_pages:
                home.tab_pages[tab].show_lock(
                    f"Town Hall {th} hasn't unlocked these yet, so they're greyed out and skipped:\n"
                    + "\n".join(lines) if lines else "")

    def _grey_parts(self, page, parts_by_tab: dict, locked: dict[str, str], th: int) -> None:
        """Settings of ``parts_by_tab`` greyed out while locked, with a notice atop their tab."""
        if page is None:
            return
        for tab, parts in parts_by_tab.items():
            lines = []
            for feature, paths in parts.items():
                off = feature in locked
                if off:
                    lines.append(f"•  {townhall.NAMES[feature]}: {locked[feature]}")
                for path in paths:
                    for card in [self.cards.get(path), *self.mirror_cards.get(path, [])]:
                        if card is not None:
                            card.setEnabled(not off)
            if tab in page.tab_pages:
                page.tab_pages[tab].show_lock(
                    f"Town Hall {th} hasn't unlocked these yet, so they're greyed out and skipped:\n"
                    + "\n".join(lines) if lines else "")

    def _place_of(self, card: QWidget) -> tuple[str, str | None] | None:
        """The page (and tab) a card is on."""
        for key, page in self.pages.items():
            if isinstance(page, TabbedPage):
                for tab, body in page.tab_pages.items():
                    if body.isAncestorOf(card):
                        return key, tab
            elif page.isAncestorOf(card):
                return key, None
        return None

    def _show_error_places(self, errors: dict[str, str]) -> None:
        """A red badge on each page and tab with a mistake in it (on the side
        bar, and on the tab), so the setting can be found."""
        cards = [c for path in errors for c in [self.cards.get(path), *self.mirror_cards.get(path, [])] if c]
        places = list(dict.fromkeys(p for p in map(self._place_of, cards) if p))
        self.first_error = cards[0] if cards else None
        for card in cards:  # a mistake folded under "More" is opened, so its red text shows
            fold = _folded_in(card)
            if fold is not None:
                fold.setExpand(True)
        if places == self.error_places:
            return
        self.error_places = places
        for badge in self.error_badges:
            badge.hide()
            badge.deleteLater()
        self.error_badges = []
        for page_key in dict.fromkeys(page for page, _ in places):
            item = self.navigationInterface.widget(self.pages[page_key].objectName())
            if item is not None:
                self.error_badges.append(InfoBadge.error("!", self.navigationInterface, item,
                                                         InfoBadgePosition.NAVIGATION_ITEM))
        for page_key, tab in places:
            page = self.pages[page_key]
            if tab is not None and tab in page.tabs.items:
                item = page.tabs.items[tab]
                self.error_badges.append(InfoBadge.error("!", item.parentWidget(), item, InfoBadgePosition.TOP_RIGHT))

    def _go_to_error(self) -> None:
        """Open the page and tab of the first setting with a mistake, scrolled to it."""
        card = self.first_error
        place = self._place_of(card) if card is not None else None
        if place is None:
            return
        page_key, tab = place
        self.show_page(page_key, tab)
        page = self.pages[page_key]
        body = page.tab_pages[tab] if tab is not None else page
        QTimer.singleShot(50, lambda: body.ensureWidgetVisible(card, 0, 80))

    def _autosave(self) -> bool:
        try:
            apply_form(self.config, self.collect())
        except ValueError:
            self._changed(save=False)
            return False
        accounts.save_active(self.config, self.config_path)
        self.save_label.setText("✓ Saved")
        tint(self.save_label, MUTED)
        return True

    # ------------------------------------------------------------ bot
    def _running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, *_, show: bool = True) -> None:
        self.save_timer.stop()
        if self._running():
            if self.pause_event.is_set():  # Resume
                self.pause_event.clear()
                self._show_pause_button()
                self.start_button.setEnabled(False)
                self._show_status(describe_status("Resuming"))
            return
        if not self._autosave():
            notice(InfoBarIcon.ERROR, "Can't start yet", "A setting has a mistake: look for the red text, fix it, "
                   "then press Start again.", duration=5000, parent=self)
            return
        attacking = not self.config.search.maintain_only
        missing = catalog.missing(self.library(), required_only=True) if attacking else []
        if missing:
            self._pictures_bar(InfoBarIcon.ERROR, "A button picture is missing", "The bot finds buttons by their "
                               "pictures, and can't attack without: " + ", ".join(p.title for p in missing) + ".")
            return
        if attacking and catalog.slow_exit(self.library()):
            self._pictures_bar(InfoBarIcon.INFORMATION, "Tip", "Without the Return Home and Okay pictures the bot closes "
                               "the game to leave each battle, which takes about 30 s longer.")
        self.stop_event = threading.Event()
        self._stop_when_paused = False
        self.pause_event.clear()
        self._show_pause_button()
        self.session_start = time.monotonic()
        self.thread = self._spawn(self._run_bot, self.stop_event)
        self.problem = None
        self.auto_village = None
        self.run_from, self.stopped_by_you = self.stats["attacks"], False
        self._show_status(describe_status("Starting…"))
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._changed(save=False)
        self._show_profiles()  # picked by the bot while it runs
        if show:
            self.show_page("Dashboard")
            self._watch_view()

    def _watch_view(self) -> None:
        """The dashboard as it's best watched while farming: the base being
        scouted at the top of the window, this session under it, and the
        step-by-step log open at the bottom."""
        self._show_details(True)

        def scroll() -> None:  # once the open log is laid out and fitted
            self._fit_log()
            QTimer.singleShot(0, self._scroll_to_watch)
        QTimer.singleShot(0, scroll)

    def _scroll_to_watch(self) -> None:
        """The village's panel at the top of the window; remembered, so the
        view stays there while what's above it changes size."""
        page = self.pages["Dashboard"]
        panel = self.builder_panel if not self.builder_panel.isHidden() else self.home_panel
        top = panel.mapTo(page.widget(), QPoint(0, 0)).y()
        page.verticalScrollBar().setValue(max(top - WATCH_MARGIN, 0))
        self._watching = True

    def _keep_watch_view(self, *_) -> None:
        if getattr(self, "_watching", False):
            self._scroll_to_watch()

    def _hold_watch_view(self) -> None:
        """Something above the watched panels changed size: the page moves
        under the view for a moment while it's laid out again. Don't draw
        that moment; draw once the view is back on the panels."""
        if not getattr(self, "_watching", False):
            return
        viewport = self.pages["Dashboard"].viewport()
        viewport.setUpdatesEnabled(False)

        def thaw() -> None:
            self._keep_watch_view()
            viewport.setUpdatesEnabled(True)
        QTimer.singleShot(WATCH_HOLD_MS, thaw)

    def _stop_watching(self, *_) -> None:  # scrolled by hand: leave the page where it's put
        self._watching = False

    def _fit_log(self) -> None:
        """The open log fills the window under the village's panels, scrolled
        to the top, and is never taller than the window: its whole box shows
        at once, whatever the window's size."""
        self._log_fit_due = False
        if self.log_view.isHidden():
            return
        page = self.pages["Dashboard"]
        panel = self.builder_panel if not self.builder_panel.isHidden() else self.home_panel
        top = panel.mapTo(page.widget(), QPoint(0, 0)).y()
        log_top = self.log_view.mapTo(page.widget(), QPoint(0, 0)).y()
        window = page.viewport().height() - 2 * WATCH_MARGIN
        room = window - (log_top - top)
        height = max(min(LOG_MIN, window), min(room, window))
        if height != self.log_view.height() or self.log_view.minimumHeight() != height:
            lines = self.log_view.verticalScrollBar()
            at_end = lines.value() >= lines.maximum() - 2
            self.log_view.setFixedHeight(max(height, 40))
            if at_end:  # following the newest lines: still on them at the new height
                QTimer.singleShot(0, lambda: lines.setValue(lines.maximum()))

    def _fit_log_soon(self) -> None:
        if not getattr(self, "_log_fit_due", False):
            self._log_fit_due = True
            QTimer.singleShot(0, self._fit_log)

    def stop_soon(self) -> None:
        """Switch off (the Farms page's switch, closing a farm's window): stop
        once the current action is done, never in the middle of a battle."""
        if self.stop_event is None or not self._running() or self.stop_event.is_set():
            return
        if self.pause_event.is_set() and self.status_now.state == "paused":  # waiting at a safe point already
            self.stop()
            return
        self._stop_when_paused = True
        if not self.pause_event.is_set():
            self.stop()
        self._show_status(describe_status("Switching off after the current action…"))

    def switching_off(self) -> bool:
        return self._running() and (self._stop_when_paused or bool(self.stop_event and self.stop_event.is_set()))

    def stop_now(self) -> None:
        """Stop straight away (the Farms page's switch, closing a farm's window)."""
        if self.stop_event is None or not self._running() or self.stop_event.is_set():
            return
        if not self.pause_event.is_set():
            self.stop()
        self.stop()

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
            self.stopped_by_you = True
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

    def _pictures_bar(self, icon: InfoBarIcon, title: str, text: str) -> None:
        go = PushButton("Open Pictures")
        bar = notice(icon, title, text, duration=8000, parent=self, button=go)
        go.clicked.connect(lambda: (self.show_page("Advanced", "pictures"), bar.close()))

    def _run_bot(self, stop_event: threading.Event) -> None:
        from farmbot.bot import Bot
        from farmbot.bluestacks import open_device_retrying, restart_emulator
        from farmbot.game import Game
        from farmbot.vision import TemplateLibrary, Vision

        from farmbot.game import Stopped

        instances.active[self.farm] = self.config.device  # other farms leave its emulator alone
        instances.hooks[self.farm] = {"status": lambda s: self.events.put(("status", s)),
                                      "moved": lambda was, now: self.events.put(("moved", (was, now))),
                                      "stopped": stop_event.is_set}
        try:
            self.events.put(("status", "Connecting to the device…"))
            device = open_device_retrying(self.config.device)
            instances.connected.add(self.farm)
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
                      on_storage_max=lambda village, maxes: self.events.put(("storage_max", (village, maxes))),
                      on_account=self._account_from_bot,
                      on_timings=lambda t: self.events.put(("timings", t)),
                      on_village=lambda name, v: self.events.put(("village", (name, v))),
                      village_path=self.village_file,
                      pause_event=self.pause_event,
                      reopen_device=(lambda: restart_emulator(self.config.device))
                      if self.config.device.mode in EMULATORS else None)
            bot.run()
        except Stopped:  # Stop pressed while it waited for another farm
            pass
        except Exception as exc:  # shown in the window instead of a silent thread death
            log.error("Bot stopped: %s", exc)
            self.events.put(("error", str(exc)))
        finally:
            instances.active.pop(self.farm, None)
            instances.connected.discard(self.farm)
            instances.hooks.pop(self.farm, None)
            instances.accounts_playing.pop(self.farm, None)
            self.events.put(("finished", None))

    def _moved_instance(self, was: int, now: int) -> None:
        """The farm was copied to another LDPlayer instance (its old one shared
        a port with another farm's): kept in its settings and shown."""
        path = "device.ldplayer_index"  # shown from 1; a page not built yet keeps it as its box's text
        self._set_value(path, now + 1 if path in self.bindings else str(now + 1))
        self.config.device.ldplayer_index = now
        self._autosave()
        self.scan_emulators()  # the new instance (and the spare) listed on the Farms page
        notice(InfoBarIcon.INFORMATION, f"Farm {self.farm} moved to instance {now + 1}",
               f"Its instance {was + 1} had the same connection port as another farm's, so COCFARM copied it "
               f"(with the game and its login) to instance {now + 1}, which has a port of its own. Instance "
               f"{was + 1} is kept: delete it on the Farms page if you don't need it.",
               duration=15000, parent=self._shown())

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

        self._spawn(work)

    def _emulator_name(self) -> str:
        return {"bluestacks": "BlueStacks", "ldplayer": "LDPlayer", "mumu": "MuMu Player"}.get(
            self.config.device.mode, "the emulator")

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
                notice(InfoBarIcon.SUCCESS, "All set", "COCFARM can see your village. Choose the loot you want "
                       "(step 3), then press Start.", duration=6000, parent=self)
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

    def _stopped_status(self) -> Status:
        """Why the run ended, when, and what it did: a returning player sees
        at a glance whether it stopped by itself."""
        attacks = self.stats["attacks"] - self.run_from
        played = f"Stopped at {time.strftime('%H:%M')} after {attacks} attack{'s' * (attacks != 1)}."
        status = self.status_now
        if self.stopped_by_you:
            return Status("stopped", "Stopped by you", f"{played} Press Start to go again.")
        if status.state == "stopped" and status.headline != "Stopped":  # it said why: storage full, attacks played
            return Status("stopped", status.headline, f"{played} {status.detail}")
        return Status("stopped", "Stopped", f"{played} Press Start to go again.")

    def _show_problem(self, problem: Problem) -> None:
        self.problem = problem
        self._show_status(Status("problem", problem.title, problem.why))
        self.big_trouble.setText(f"Technical details: {problem.details}")
        tint(self.big_trouble, MUTED)
        self.big_trouble.show()
        fix = PushButton(problem.action_text)
        bar = notice(InfoBarIcon.ERROR, problem.title, problem.why, duration=10000, parent=self, button=fix)
        fix.clicked.connect(lambda: (self._do_fix(problem.action, retry=self.start), bar.close()))

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
        setThemeColor(str(ACCENT))
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
                        self.big_trouble.setText(f"⚠  Last round didn't work: {in_sentence(problem.title)}. "
                                                 "It's trying again.")
                        tint(self.big_trouble, WARN)
                        self.big_trouble.show()
                elif kind == "status":
                    if data.startswith("Attacking (builder base): base"):
                        self._bb_stage("Second base" if data.endswith("2") else "First base")
                    status = describe_status(data)
                    if self._stop_when_paused and data.startswith("Paused by you"):  # at a safe point: off now
                        self.stop()
                    if data.startswith("Storage full: switching to the "):
                        self.auto_village = "builder" if data.endswith("builder base") else "home"
                        self._changed(save=False)  # the Dashboard shows that village's panel
                    if data.startswith("Stopped: storage full"):
                        notice(InfoBarIcon.SUCCESS, status.headline, status.detail, duration=-1, parent=self)
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
                elif kind == "storage_max":  # read in the game: shown (and saved) like a typed amount
                    village, maxes = data
                    for resource, most in maxes.items():
                        path = f"storage.{'builder_' if village == 'builder' else ''}{resource}"
                        self._set_value(path, f"{most:,}")
                    if village == "home":
                        self._fit_search_minimums(maxes)
                    self.save_timer.start()  # the day they were read too, even if the amounts didn't change
                elif kind == "village":  # the account's export, read in the game
                    name, village = data
                    if (name or None) == (self.config.profiles.active or None):
                        self._village_for, self._village = (name or None,), village
                        self._fit_search_minimums()
                        self._changed(save=False)
                elif kind == "timings":
                    self._timings = data
                    self._show_human(self.collect())
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
                elif kind == "moved":
                    self._moved_instance(*data)
                elif kind == "account":  # the bot waits for the answer
                    mask, box, done, picture = data
                    try:
                        box["name"] = self._account_seen(mask, picture)
                    finally:
                        done.set()
                elif kind == "call":  # a worker's result, handled on the window's thread
                    data()
                elif kind == "error":
                    self._show_problem(explain(data, self._emulator_name()))
                elif kind == "finished":
                    self._stop_when_paused = False
                    QTimer.singleShot(0, self.refresh_farm_pages)  # its row says it's off
                    if self.status_now.state != "problem":
                        self._show_status(self._stopped_status())
                    self.stop_button.setEnabled(False)
                    self.pause_event.clear()
                    self._show_pause_button()
                    self._changed(save=False)
                    self._show_big_buttons()
                    self._show_profiles()
        except queue.Empty:
            pass

    def _tick(self) -> None:
        if self.session_start is not None and self._running():
            seconds = int(time.monotonic() - self.session_start)
            self.stat_value["runtime"].setText(f"{seconds // 3600}h {seconds // 60 % 60:02d}m")
            self.bb_value["runtime"].setText(f"{seconds // 3600}h {seconds // 60 % 60:02d}m")

    def _show_base(self, loot, accepted: bool, screen: np.ndarray | None = None) -> None:
        self.stats["bases"] += 1
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
        count, gained = history.totals_for(instances.resolve(self.config, self.config.bot.history), date.today())
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
        hub = self.hub or self
        if not self._closing and self.separate:  # a farm in a window of its own: switch it off
            instances.pick(self.farm, False)
            self.stop_soon()
            self._flush_save()
            self.separate = False
            self.hide()
            event.ignore()
            hub.refresh_farm_pages()
            return
        if not self._closing:  # COCFARM's main window: COCFARM closes, and every farm stops
            self._closing = True
            for window in list(hub.farm_windows().values()):
                if window is not self:
                    window._closing = True
                    window.stop_now()
                    window.close()
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
    app = splash.application()
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(LOGO)))
    setTheme(Theme.DARK)
    setThemeColor(str(ACCENT))
    return app


def run_gui(config_path: str, serial: str | None = None) -> int:
    app = create_app()
    splash.message("Building the window…")
    window = FarmbotWindow(config_path, serial)
    splash.finish(window)  # shows the window
    return app.exec()


__all__ = ["FarmbotWindow", "create_app", "run_gui"]
