"""The onboarding tour for a new install (who gets it: ``farmbot/onboarding.py``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, IconWidget, LineEdit, MessageBoxBase,
                            PrimaryPushButton, PushButton, ScrollArea, SegmentedWidget, SimpleCardWidget,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, TitleLabel, TransparentPushButton,
                            isDarkTheme)
from qfluentwidgets import FluentIcon as FIF

from farmbot import icons, instances, onboarding
from farmbot.gui import (ACCENT, BAD, GOOD, LINE, MUTED, ON_ACCENT, TEXT, WARN, BelowTitleBar,
                         tint)

if TYPE_CHECKING:
    from farmbot.gui import FarmbotWindow

STEPS = ("Welcome", "Your emulator", "Check the game", "What to farm")
BLURBS = ("What COCFARM does", "Where the game runs", "COCFARM can see it", "Village and loot")
TITLES = ("Welcome to COCFARM", "Your emulator", "Check the game", "What to farm")
INTROS = (
    "COCFARM plays Clash of Clans for you in an Android emulator on this PC. A few choices and it's ready.",
    "Where Clash of Clans runs. COCFARM sets the emulator up by itself (it may restart once).",
    "COCFARM opens the emulator if needed, connects to it, and checks Clash of Clans is installed and signed in.",
    "Which village to farm, and how much loot a base must offer before COCFARM attacks it.",
)
FEATURES = (
    (FIF.SEARCH, "Finds bases worth it", "Skips bases until one offers the loot you ask for."),
    (FIF.GAME, "Attacks for you", "Deploys your army and plays the battle out, around the clock if you like."),
    (FIF.SHOPPING_CART, "Collects the loot", "Empties collectors and mines and brings the loot home."),
    (FIF.CONSTRACT, "Keeps builders busy", "Can spend full storages on upgrades and walls while you're away."),
)
MODES = {"mumu": "MuMu", "ldplayer": "LDPlayer", "bluestacks": "BlueStacks", "manual": "Other"}
VILLAGES = {"home": "Home village", "builder": "Builder base", "auto": "Auto switch"}
RESOURCES = (("gold", "Gold"), ("elixir", "Elixir"), ("dark", "Dark elixir"))
# The emulators, at the versions COCFARM is tested with (others may work). MuMu: its current download.
DOWNLOADS = (
    ("mumu", "MuMu Player", "the latest", True,
     "https://api.mumuplayer.com/api/dl/win?channel=gw-win-download"),
    ("ldplayer9", "LDPlayer 9", "9.5.25", False,
     "https://static.ldrescdn.com/download/package/LDPlayer_9.5.25.0.exe?n=LDPlayer9.5.25.0_ens_1420_ld.exe"),
    ("ldplayer14", "LDPlayer 14", "14.0.29", False,
     "https://setup.ldrescdn.com/download/package/LDPlayer_14.0.29.2.exe?n=LDPlayer14.0.29.2_ens_1420_ld.exe"),
    ("bluestacks", "BlueStacks 5", "5.22", False,
     "https://ak-build.bluestacks.com/public/app-player/windows/nxt/5.22.290.1031/b3b8c6d5d7f2ef642075595e7aa5754c/"
     "BlueStacksMicroInstaller_5.22.290.1031_native.exe"
     "?filename=BlueStacksInstaller_5.22.290.1031_native_e0175934c4a0a56f1db683585ffd1bcf.exe"),
)


def _wrap(label):
    label.setWordWrap(True)
    return label


def _card(spacing: int = 12) -> tuple[SimpleCardWidget, QVBoxLayout]:
    card = SimpleCardWidget()
    column = QVBoxLayout(card)
    column.setContentsMargins(20, 16, 20, 16)
    column.setSpacing(spacing)
    return card, column


def _row(*widgets, stretch_at: int = 1) -> QHBoxLayout:
    """``widgets`` side by side; the space goes before the one at ``stretch_at`` (-1: after them all)."""
    row = QHBoxLayout()
    row.setSpacing(12)
    wraps = False
    for i, w in enumerate(widgets):
        if i == stretch_at:
            row.addStretch(1)
        wrap = isinstance(w, QLabel) and w.wordWrap()
        wraps = wraps or wrap
        row.addWidget(w, 1 if wrap else 0, Qt.AlignVCenter)
    if stretch_at == -1 and not wraps:  # (a wrapping label takes the room itself)
        row.addStretch(1)
    return row


def _icon(icon, size: int = 20) -> IconWidget:
    widget = IconWidget(icon)
    widget.setFixedSize(size, size)
    return widget


def _line() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {LINE};")
    return line


def _section(column: QVBoxLayout, title: str, note: str = "") -> None:
    column.addSpacing(8)
    column.addWidget(StrongBodyLabel(title))
    if note:
        column.addWidget(tint(_wrap(CaptionLabel(note)), MUTED))


class _RailStep(QWidget):
    """A step on the rail: click it to go there."""

    def __init__(self, go):
        super().__init__()
        self._go = go
        self.setObjectName("railStep")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setCursor(Qt.PointingHandCursor)
        self._hover(False)

    def _hover(self, on: bool) -> None:
        shade = "rgba(255, 255, 255, 0.06)" if isDarkTheme() else "rgba(0, 0, 0, 0.04)"
        self.setStyleSheet(f"#railStep {{ border-radius: 8px; background: {shade if on else 'transparent'}; }}")

    def enterEvent(self, e) -> None:  # noqa: N802 (Qt's name)
        self._hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802 (Qt's name)
        self._hover(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 (Qt's name)
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self._go()
        super().mouseReleaseEvent(e)


class OnboardingDialog(BelowTitleBar, MessageBoxBase):
    """Four steps filling the window (the rest of COCFARM hidden behind):
    welcome, pick the emulator and its instance (or download one), check the
    connection and the game, choose what to farm. Every choice goes straight
    into the window's settings (and so is saved); farming starts from the
    window afterwards."""

    RAIL = 240  # the steps down the left

    def __init__(self, window: FarmbotWindow):
        super().__init__(window)
        self.window_ = window
        self.done_ = False
        self._choices: list[instances.Choice] = []
        self._shown_choices: list[str] | None = None
        self._mode_picked = False
        self._visited: set[int] = set()  # the steps seen: ticked on the rail
        self.yesButton.hide()
        self.cancelButton.hide()
        self.buttonGroup.hide()  # (its own buttons, beside the steps rail)
        self.setClosableOnMaskClicked(False)
        # The whole window, in its own background (no card over COCFARM) until it's done or skipped.
        self.setMaskColor(QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243))
        self.widget.setGraphicsEffect(None)
        self.widget.setStyleSheet("#centerWidget { border: none; border-radius: 0px; background: transparent; }")
        self._hBoxLayout.removeWidget(self.widget)
        self._hBoxLayout.addWidget(self.widget, 1)
        self._hBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self._rail())
        self.content = QVBoxLayout()
        self.content.setSpacing(6)
        self.progress = CaptionLabel("")
        self.title = TitleLabel("")
        self.intro = tint(_wrap(BodyLabel("")), MUTED)
        self.content.addWidget(self.progress)
        self.content.addWidget(self.title)
        self.content.addWidget(self.intro)
        self.content.addSpacing(16)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")
        self.content.addWidget(self.stack, 1)
        for build in (self._welcome, self._emulator, self._check, self._farm):
            page = QWidget()
            page.setStyleSheet("background: transparent;")
            column = QVBoxLayout(page)
            column.setContentsMargins(0, 0, 10, 4)
            column.setSpacing(10)
            build(column)
            column.addStretch(1)
            scroll = ScrollArea()  # each step on its own: a small window scrolls it, the buttons stay
            scroll.setFrameShape(ScrollArea.NoFrame)
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.enableTransparentBackground()
            self.stack.addWidget(scroll)

        self.skip_button = TransparentPushButton("Skip the tour")
        self.skip_button.clicked.connect(self.skip)
        self.back_button = PushButton(FIF.LEFT_ARROW, "Back")
        self.back_button.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next_button = PrimaryPushButton("Next")
        self.next_button.clicked.connect(self._next)
        for b in (self.back_button, self.next_button):
            b.setMinimumWidth(116)
        self.content.addWidget(_line())
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 14, 0, 0)
        footer.setSpacing(10)
        footer.addWidget(self.skip_button)
        footer.addStretch(1)
        footer.addWidget(self.back_button)
        footer.addWidget(self.next_button)
        self.content.addLayout(footer)
        body.addLayout(self.content, 1)
        self.viewLayout.addLayout(body, 1)
        # The title bar's Start and status are the window's: back once the tour is done.
        self.title_bar_tools = window.start_button.parentWidget()
        self.title_bar_tools.hide()
        self.go(0)

    # ------------------------------------------------------------ the steps rail
    def _rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(self.RAIL)
        rail.setStyleSheet(f"#rail {{ background: transparent; border-right: 1px solid {LINE}; }}")
        column = QVBoxLayout(rail)
        column.setContentsMargins(22, 32, 18, 28)
        column.setSpacing(0)
        brand = QVBoxLayout()
        brand.setContentsMargins(6, 0, 0, 0)
        brand.setSpacing(0)
        brand.addWidget(SubtitleLabel("COCFARM"))
        brand.addWidget(tint(CaptionLabel("First-time setup"), MUTED))
        column.addLayout(brand)
        column.addSpacing(36)
        self.rail_steps: list[tuple[QLabel, StrongBodyLabel, CaptionLabel]] = []
        for i, (step, blurb) in enumerate(zip(STEPS, BLURBS)):
            dot = QLabel(str(i + 1))
            dot.setFixedSize(30, 30)
            dot.setAlignment(Qt.AlignCenter)
            label = StrongBodyLabel(step)
            note = CaptionLabel(blurb)
            text = QVBoxLayout()
            text.setSpacing(0)
            text.addWidget(label)
            text.addWidget(note)
            button = _RailStep(lambda i=i: self.go(i))
            button.setToolTip(f"Go to {step}")
            row = QHBoxLayout(button)
            row.setContentsMargins(6, 6, 6, 6)
            row.setSpacing(14)
            row.addWidget(dot, 0, Qt.AlignTop)
            row.addLayout(text, 1)
            column.addWidget(button)
            if i < len(STEPS) - 1:  # the line down to the next step
                line = QFrame()
                line.setFixedSize(2, 24)
                line.setStyleSheet(f"background: {LINE};")
                gap = QHBoxLayout()
                gap.setContentsMargins(20, 0, 0, 0)
                gap.addWidget(line)
                gap.addStretch(1)
                column.addLayout(gap)
            self.rail_steps.append((dot, label, note))
        column.addStretch(1)
        foot = tint(_wrap(CaptionLabel("Click a step to go to it. Everything here can be changed later in "
                                       "the window, and farming starts from there.")), MUTED)
        foot.setContentsMargins(6, 0, 0, 0)
        column.addWidget(foot)
        return rail

    def _show_rail(self, index: int) -> None:
        for i, (dot, label, note) in enumerate(self.rail_steps):
            if i in self._visited and i != index:
                dot.setText("✓")
                dot.setStyleSheet(f"border-radius: 15px; border: 2px solid {ACCENT}; color: {ACCENT}; "
                                  "font-weight: 600; font-size: 14px;")
            elif i == index:
                dot.setText(str(i + 1))
                dot.setStyleSheet(f"border-radius: 15px; background: {ACCENT}; color: {ON_ACCENT}; "
                                  "font-weight: 600; font-size: 14px;")
            else:
                dot.setText(str(i + 1))
                dot.setStyleSheet(f"border-radius: 15px; border: 2px solid {LINE}; color: {MUTED}; font-size: 14px;")
            tint(label, TEXT if i <= index or i in self._visited else MUTED)
            tint(note, MUTED)

    # ------------------------------------------------------------ steps
    def _welcome(self, column: QVBoxLayout) -> None:
        grid = QGridLayout()
        grid.setSpacing(12)
        for n, (icon, title, text) in enumerate(FEATURES):
            card, inner = _card(6)
            inner.setContentsMargins(20, 18, 20, 18)
            inner.addLayout(_row(_icon(icon, 22), StrongBodyLabel(title), stretch_at=-1))
            inner.addWidget(tint(_wrap(CaptionLabel(text)), MUTED))
            grid.addWidget(card, n // 2, n % 2)
        column.addLayout(grid)
        _section(column, "Before you start")
        card, inner = _card(10)
        for line in ("An Android emulator on this PC: MuMu Player, LDPlayer, BlueStacks or another. The next "
                     "step can download one.",
                     "Clash of Clans installed in it, and signed in to your account.",
                     "A couple of minutes for this tour."):
            inner.addLayout(_row(_icon(FIF.ACCEPT, 14), _wrap(BodyLabel(line)), stretch_at=-1))
        column.addWidget(card)

    def _emulator(self, column: QVBoxLayout) -> None:
        # Which emulators are here, with Look again beside them.
        self.found_label = BodyLabel("")
        self.found_icon = _icon(FIF.ACCEPT, 16)
        again = PushButton(FIF.SYNC, "Look again")
        again.setToolTip("Look for BlueStacks, LDPlayer and MuMu again, e.g. after installing one")
        again.clicked.connect(self.look_again)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(StrongBodyLabel("Download an emulator"))
        heading.addLayout(_row(self.found_icon, self.found_label, stretch_at=-1))
        top = QHBoxLayout()
        top.addLayout(heading, 1)
        top.addWidget(again, 0, Qt.AlignBottom)
        column.addLayout(top)

        # The emulators COCFARM is tested with, one line each.
        card, inner = _card(6)
        inner.setContentsMargins(20, 10, 16, 10)
        self.download_buttons: dict[str, PushButton] = {}
        self.download_status: dict[str, CaptionLabel] = {}
        for n, (key, name, version, best, url) in enumerate(DOWNLOADS):
            if n:
                inner.addWidget(_line())
            words = [StrongBodyLabel(name)]
            if best:
                words.append(tint(CaptionLabel("★ Recommended"), ON_ACCENT,
                                  f"background: {ACCENT}; border-radius: 9px; padding: 1px 8px; font-weight: 600;"))
            words.append(tint(CaptionLabel(f"tested: {version}"), MUTED))
            status = CaptionLabel("")
            status.setMinimumHeight(32)  # (a row without its button: as tall as one with)
            self.download_status[key] = status
            button = (PrimaryPushButton if best else PushButton)(FIF.DOWNLOAD, "Download")
            button.setToolTip(url.split("?")[0])
            button.setMinimumWidth(120)
            button.clicked.connect(lambda *_, u=url: self.download(u))
            self.download_buttons[key] = button
            inner.addLayout(_row(*words, status, button, stretch_at=len(words)))
        column.addWidget(card)
        column.addWidget(tint(_wrap(CaptionLabel("Other emulators may work too: pick Other below and give its "
                                                 "ADB address.")), MUTED))

        # Which one, and which of its instances (or, for Other, its address).
        column.addSpacing(4)
        card, inner = _card(10)
        self.mode_picker = SegmentedWidget()
        for key, name in MODES.items():
            self.mode_picker.addItem(key, name, lambda *_, k=key: self._pick_mode(k))
        self.mode_picker.setFixedWidth(360)
        inner.addLayout(_row(_icon(FIF.APPLICATION, 18), StrongBodyLabel("Use"), self.mode_picker, stretch_at=2))
        inner.addWidget(_line())

        self.instance_picker = ComboBox()
        self.instance_picker.setFixedWidth(300)
        self.instance_picker.currentIndexChanged.connect(self._pick_instance)
        self.instance_note = tint(_wrap(CaptionLabel("")), MUTED)
        self.instance_row = self._setting(inner, FIF.TILES, "Instance", self.instance_note, self.instance_picker)

        self.serial_box = LineEdit()
        self.serial_box.setFixedWidth(300)
        self.serial_box.setPlaceholderText("127.0.0.1:5555")
        self.serial_box.textChanged.connect(lambda text: text.strip() and self._set("device.serial", text.strip()))
        self.serial_row = self._setting(
            inner, FIF.CONNECT, "ADB address",
            tint(_wrap(CaptionLabel("In the emulator's settings, often under ADB or Developer options.")), MUTED),
            self.serial_box)
        self.adb_box = LineEdit()
        self.adb_box.setFixedWidth(300)
        self.adb_box.setPlaceholderText("adb")
        self.adb_box.textChanged.connect(lambda text: self._set("device.adb_path", text.strip() or "adb"))
        self.adb_row = self._setting(
            inner, FIF.COMMAND_PROMPT, "ADB program",
            tint(_wrap(CaptionLabel('"adb", or the full path to the emulator\'s adb.exe.')), MUTED), self.adb_box)

        column.addWidget(card)

    @staticmethod
    def _setting(column: QVBoxLayout, icon, name: str, note: QLabel | None, control: QWidget) -> QWidget:
        """One setting in a card: icon, name (and a note under it), its control on the right."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(12)
        line.addWidget(_icon(icon, 18), 0, Qt.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(StrongBodyLabel(name))
        if note is not None:
            words.addWidget(note)
        line.addLayout(words, 1)
        line.addWidget(control, 0, Qt.AlignVCenter)
        column.addWidget(row)
        return row

    def _check(self, column: QVBoxLayout) -> None:
        self.check_button = PrimaryPushButton(FIF.SYNC, "Check now")
        self.check_button.setMinimumWidth(140)
        self.check_button.clicked.connect(self.check)
        column.addWidget(self.check_button, 0, Qt.AlignLeft)
        column.addSpacing(6)
        card, inner = _card(0)
        inner.setContentsMargins(0, 4, 0, 4)
        self.check_lines: dict[str, BodyLabel] = {}
        items = (("device", FIF.CONNECT, "Connected to the emulator", "COCFARM opens it if needed and sets it up."),
                 ("installed", FIF.GAME, "Clash of Clans installed", "From the Play Store inside the emulator."),
                 ("signed_in", FIF.PEOPLE, "Signed in to your account",
                  "Open the game once and sign in: the one thing COCFARM can't do for you."))
        for n, (key, icon, name, note) in enumerate(items):
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(StrongBodyLabel(name))
            text.addWidget(tint(_wrap(CaptionLabel(note)), MUTED))
            self.check_lines[key] = tint(BodyLabel("Not checked yet"), MUTED)
            row = QHBoxLayout()
            row.setContentsMargins(20, 14, 20, 14)
            row.setSpacing(14)
            row.addWidget(_icon(icon, 20))
            row.addLayout(text, 1)
            row.addWidget(self.check_lines[key])
            inner.addLayout(row)
            if n < len(items) - 1:
                inner.addWidget(_line())
        column.addWidget(card)
        self.fix_card, inner = _card(8)
        self.fix_card.setObjectName("fix")
        self.fix_card.setStyleSheet(f"#fix {{ border: 1px solid {WARN}; border-radius: 10px; }}")
        self.fix_text = _wrap(BodyLabel(""))
        self.fix_button = PushButton("")
        self.fix_button.clicked.connect(self._fix)
        inner.addLayout(_row(_icon(FIF.INFO, 18), StrongBodyLabel("To fix"), stretch_at=-1))
        inner.addWidget(self.fix_text)
        inner.addWidget(self.fix_button, 0, Qt.AlignLeft)
        column.addWidget(self.fix_card)
        self.fix_card.hide()
        self.fix_text.hide()
        self.fix_button.hide()
        column.addWidget(tint(_wrap(CaptionLabel("You can go on without it and check later: Advanced → Emulator.")),
                              MUTED))

    def _farm(self, column: QVBoxLayout) -> None:
        card, inner = _card(10)
        self.village_picker = SegmentedWidget()
        for key, name in VILLAGES.items():
            self.village_picker.addItem(key, name, lambda *_, k=key: self._pick_village(k))
        self.village_picker.setFixedWidth(360)
        inner.addLayout(_row(_icon(FIF.HOME, 18), StrongBodyLabel("Village"), self.village_picker, stretch_at=2))
        inner.addWidget(tint(_wrap(CaptionLabel("Auto switch farms one village until its storages are full, then "
                                                "the other.")), MUTED))
        column.addWidget(card)

        _section(column, "Loot a base must offer", "Bases offering less are skipped; 0 ignores it.")
        card, inner = _card(12)
        self.suggested_switch = SwitchButton()
        self.suggested_switch.checkedChanged.connect(self._suggested)
        self.suggested_note = tint(_wrap(CaptionLabel("")), MUTED)
        self._setting(inner, FIF.CERTIFICATE, "Suggested amounts", self.suggested_note, self.suggested_switch)
        inner.addWidget(_line())
        self.loot_boxes: dict[str, LineEdit] = {}
        for kind, name in RESOURCES:
            picture = QLabel()
            picture.setPixmap(icons.pixmap(kind, 22, self.devicePixelRatioF() or 2.0))
            picture.setFixedSize(22, 22)
            box = LineEdit()
            box.setFixedWidth(180)
            box.setAlignment(Qt.AlignRight)
            box.setPlaceholderText("e.g. 1.5m")
            box.textEdited.connect(lambda text, k=kind: self._loot_typed(k, text))
            self.loot_boxes[kind] = box
            inner.addLayout(_row(picture, BodyLabel(name), box, stretch_at=2))
        self.loot_error = tint(_wrap(CaptionLabel("")), BAD)
        inner.addWidget(self.loot_error)
        self.loot_error.hide()
        column.addWidget(card)
        column.addSpacing(4)
        column.addLayout(_row(_icon(FIF.RINGER, 16),
                              tint(_wrap(CaptionLabel("Want a Discord message when the bot stops or a storage "
                                                      "fills? Advanced → Alerts.")), MUTED), stretch_at=-1))

    # ------------------------------------------------------------ moving
    def go(self, index: int) -> None:
        index = max(0, min(index, len(STEPS) - 1))
        self._visited.add(index)
        self.stack.setCurrentIndex(index)
        self.progress.setText(f"STEP {index + 1} OF {len(STEPS)}")
        tint(self.progress, ACCENT, "font-weight: 600;")
        self.title.setText(TITLES[index])
        self.intro.setText(INTROS[index])
        self._show_rail(index)
        self.stack.currentWidget().verticalScrollBar().setValue(0)
        self.back_button.setVisible(index > 0)
        last = index == len(STEPS) - 1
        self.next_button.setText("Finish" if last else "Next")
        self.next_button.setIcon(FIF.ACCEPT if last else FIF.RIGHT_ARROW)
        if index == 1:
            self.show_emulators()
        elif index == 3:
            self.show_farm()

    def _next(self) -> None:
        if self.stack.currentIndex() == len(STEPS) - 1:
            self.finish()
        else:
            self.go(self.stack.currentIndex() + 1)

    def skip(self) -> None:
        self.finish()

    def finish(self) -> None:
        """Done (or skipped): never shown again; to the Dashboard, where farming starts."""
        window = self.window_
        self.done_ = True
        onboarding.finish(window.config_path)
        window._autosave()
        window.onboarding_dialog = None
        self.title_bar_tools.show()
        lock_window(window, False)
        window.show_page("Dashboard")
        self.close()

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(e)
        side = max(40, (self.width() - self.RAIL - 820) // 2)  # a column that reads well, however wide
        self.content.setContentsMargins(side, 28, side, 20)

    def keyPressEvent(self, e) -> None:  # noqa: N802 (Qt's name)
        if e.key() == Qt.Key_Escape:  # Skip is a button: Esc doesn't throw the tour away
            e.accept()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------ settings
    def _set(self, path: str, value) -> None:
        window = self.window_
        if window._value(path) != value:
            window._set_value(path, value)
            window._changed()  # checked and saved, as a change in the window is

    def _pick_village(self, mode: str) -> None:
        if mode == "auto" and not self.window_._value("bot.stop_when_full"):
            # Auto switch moves on once these are full: gold and elixir, as most farm.
            self._set("bot.stop_when_full", ["gold", "elixir"])
        self._set("bot.mode", mode)

    def _pick_mode(self, mode: str) -> None:
        self._mode_picked = True  # (kept, even before that emulator is installed)
        self._set("device.mode", mode)
        self._show_instances()

    def show_emulators(self) -> None:
        window = self.window_
        found = window._found
        have = [name for k, name in (("mumu", "MuMu Player"), ("ldplayer", "LDPlayer"), ("bluestacks", "BlueStacks 5"))
                if found.get(k)]
        self.found_label.setText("On this PC: " + ", ".join(have) if have
                                 else "None on this PC yet: install one, then press Look again.")
        self.found_icon.setIcon(FIF.ACCEPT if have else FIF.INFO)
        tint(self.found_label, GOOD if have else MUTED)
        names = {c.emulator for c in (window.hub or window).found}
        for key, status in self.download_status.items():
            if key.startswith("ldplayer"):  # which LDPlayer: from its instances, when they're listed
                mine = f"LDPlayer {key[8:]}" in names or (found.get("ldplayer") and not names)
            else:
                mine = found.get(key)
            status.setText("✓ On this PC" if mine else "")
            tint(status, GOOD, "font-weight: 600;")
            self.download_buttons[key].setVisible(not mine)  # (installed: nothing to download)
        mode = window._value("device.mode")
        if mode not in MODES or (mode != "manual" and not self._mode_picked and not found.get(mode)
                                 and any(found.values())):
            # The starting settings say BlueStacks: an emulator that is installed instead.
            mode = next((k for k in MODES if found.get(k)), "bluestacks")
            self._set("device.mode", mode)
        # (Shown as they are: setting what's already set changes nothing.)
        self.mode_picker.setCurrentItem(mode)
        self._set("device.launch_emulator", True)  # Start opens the emulator (Advanced → Emulator turns it off)
        for box, path in ((self.serial_box, "device.serial"), (self.adb_box, "device.adb_path")):
            if not box.text():
                box.setText(str(window._value(path) or ""))
        self._show_instances()

    def _show_instances(self) -> None:
        window = self.window_
        mode = window._value("device.mode")
        other = mode == "manual"  # any emulator, by its ADB address: no instances, and not started by COCFARM
        self.instance_row.setVisible(not other)
        for widget in (self.serial_row, self.adb_row):
            widget.setVisible(other)
        self._choices = [c for c in (window.hub or window).found if c.mode == mode]
        labels = [f"{c.emulator} · {c.label}" for c in self._choices]
        current = instances.key_of(window.config.device) if window.config.device.mode == mode else None
        if labels != self._shown_choices:
            self._shown_choices = labels
            self.instance_picker.blockSignals(True)
            self.instance_picker.clear()
            self.instance_picker.addItems(labels)
            self.instance_picker.blockSignals(False)
        self.instance_picker.setEnabled(bool(labels))
        pick = next((i for i, c in enumerate(self._choices) if c.key == current), 0)
        if labels:
            self.instance_picker.blockSignals(True)
            self.instance_picker.setCurrentIndex(pick)
            self.instance_picker.blockSignals(False)
            self._pick_instance(pick)
            self.instance_note.setText("The one Clash of Clans is installed and signed in on.")
        else:
            self.instance_note.setText(f"None found yet: open {MODES.get(mode, 'it')} once, then Look again "
                                       "(or COCFARM uses its first).")

    def _pick_instance(self, index: int) -> None:
        if not 0 <= index < len(self._choices):
            return
        window = self.window_
        device = self._choices[index].device(window.config.device)
        for path in ("device.bluestacks_instance", "device.ldplayer", "device.ldplayer_index",
                     "device.mumu_index"):
            value = getattr(device, path.split(".", 1)[1])
            if path.endswith("_index"):
                value = str(value + 1)  # shown from 1, as the form does
            self._set(path, value)

    def download(self, url: str) -> None:
        """The browser downloads the installer (a direct link)."""
        QDesktopServices.openUrl(QUrl(url))

    def look_again(self) -> None:
        window = self.window_
        window._find_bluestacks()
        window.scan_emulators()  # (in the background: the list fills in when it's back)
        self.show_emulators()

    def refresh(self) -> None:
        """The emulators were listed again (by the window)."""
        if self.stack.currentIndex() == 1:
            self.show_emulators()

    # ------------------------------------------------------------ the check
    def check(self) -> None:
        self.check_button.setEnabled(False)
        self.fix_card.hide()
        self.fix_text.hide()
        self.fix_button.hide()
        for line in self.check_lines.values():
            line.setText("Checking…")
            tint(line, MUTED)
        self.window_._test_connection()

    def show_check(self) -> None:
        """Mirror the window's own check (Advanced → Emulator), as it goes."""
        window = self.window_
        for key, label in (("device", window.device_result), ("installed", window.game_installed),
                           ("signed_in", window.game_logged_in)):
            text = label.text()
            self.check_lines[key].setText(text)
            tint(self.check_lines[key], GOOD if text.startswith("✓") else BAD if text.startswith("✗") else MUTED,
                 "font-weight: 600;" if text[:1] in ("✓", "✗") else "")
        if not window.fix_card.isHidden():
            self.fix_text.setText(window.fix_card.help_text)
            self.fix_button.setText(window.fix_button.text())
            self.fix_card.show()
            self.fix_text.show()
            self.fix_button.show()
        else:
            self.fix_card.hide()
            self.fix_text.hide()
            self.fix_button.hide()
        self.check_button.setEnabled(window.test_button.isEnabled())
        if window.check_passed:
            self.check_button.setText("Check again")

    def _fix(self) -> None:
        action = self.window_.fix_action
        if action == "setup":  # the emulator or instance: picked a step back
            self.go(1)
            return
        self.check_lines["device"].setText("Checking…")
        self.window_._do_fix(action, retry=self.check)

    # ------------------------------------------------------------ farming
    def show_farm(self) -> None:
        window = self.window_
        self.village_picker.setCurrentItem(window._value("bot.mode") or "home")
        for kind, box in self.loot_boxes.items():
            box.setText(str(window._value(f"search.min_{kind}") or ""))
        suggested = not window.config.search.minimums_by_hand
        self.suggested_switch.blockSignals(True)
        self.suggested_switch.setChecked(suggested)
        self.suggested_switch.blockSignals(False)
        for box in self.loot_boxes.values():
            box.setEnabled(not suggested)  # (greyed: the suggested amounts are set for you)
        self.suggested_note.setText("From your Town Hall, and they follow it as it goes up. Off: set your own."
                                    if suggested else "Your own amounts, below. On: COCFARM suggests them.")

    def _suggested(self, on: bool) -> None:
        """The Town Hall's search amounts (they follow it), or the player's own."""
        from farmbot.config import SEARCH_DEFAULTS, town_hall_minimums

        window = self.window_
        window.config.search.minimums_by_hand = not on
        if on:
            village = window.current_village()
            amounts = town_hall_minimums(village.town_hall) if village else SEARCH_DEFAULTS
            for kind, amount in amounts.items():
                self._set(f"search.min_{kind}", f"{amount:,}")
            self.loot_error.hide()
        window._show_suggested()  # (the window's own switch, the same setting)
        window._changed()
        self.show_farm()

    def _loot_typed(self, kind: str, text: str) -> None:
        from farmbot.form import parse_amount

        try:
            parse_amount(text or "0")
        except ValueError:
            self.loot_error.setText(f"⚠  {dict(RESOURCES)[kind]}: enter an amount like 1.5m or 300k")
            self.loot_error.show()
            return
        self.loot_error.setText("")
        self.loot_error.hide()
        self.window_.config.search.minimums_by_hand = True
        self._set(f"search.min_{kind}", text)


TOUR_SIZE = QSize(1060, 720)  # the window during the tour: roomy, not the whole screen


def lock_window(window: FarmbotWindow, locked: bool) -> None:
    """During the tour the window is a set size, centred, and can't be resized or
    maximised; after it, the window is as it was (and Start is there)."""
    window.setResizeEnabled(not locked)
    window.titleBar.maxBtn.setVisible(not locked)
    window.navigationInterface.setVisible(not locked)  # its back arrow and menu: nothing to go to in the tour
    window.titleBar.setDoubleClickEnabled(not locked)
    if locked:
        window._before_tour = (window.geometry(), window.isMaximized())
        if window.isMaximized():
            window.showNormal()
        screen = (window.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        size = TOUR_SIZE.boundedTo(screen.size()).expandedTo(window.minimumSize())  # a small screen: all of it
        window.resize(size)
        window.move(screen.center() - QPoint(size.width() // 2, size.height() // 2))
    elif hasattr(window, "_before_tour"):
        geometry, maximised = window.__dict__.pop("_before_tour")
        window.setGeometry(geometry)
        if maximised:
            window.showMaximized()


def show(window: FarmbotWindow) -> OnboardingDialog:
    lock_window(window, True)
    dialog = OnboardingDialog(window)
    window.onboarding_dialog = dialog
    dialog.show()  # not modal: nothing waits on it
    return dialog
