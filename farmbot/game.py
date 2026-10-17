"""The ``Game`` facade: screenshot → find templates → tap."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

import cv2
import numpy as np

from farmbot.device import Device
from farmbot.geometry import Rect
from farmbot.vision import Match, Vision, match_template

log = logging.getLogger(__name__)

# Screens are scaled to this height before any matching, so templates and
# coordinates (all taken at this height) work on any device resolution.
REFERENCE_HEIGHT = 920


# The game draws its buttons bigger on some screens than on the one the
# templates came from (about 1.2x on a 16:9 BlueStacks window). Screens are
# scaled to ``REFERENCE_HEIGHT / ui_scale`` so the buttons come out the size
# of the templates; the UI is anchored to the screen edges, so edge-relative
# coordinates still line up.
UI_SCALES = [round(0.6 + 0.05 * i, 2) for i in range(19)]  # 0.6 .. 1.5


def normalize(image: np.ndarray, height: int = REFERENCE_HEIGHT, ui_scale: float = 1.0) -> np.ndarray:
    """Scale a screenshot to the reference height every coordinate is given in."""
    h, w = image.shape[:2]
    if ui_scale > 0:
        height = round(height / ui_scale)
    if not height or h == height:
        return image
    interpolation = cv2.INTER_AREA if height < h else cv2.INTER_LINEAR
    return cv2.resize(image, (round(w * height / h), height), interpolation=interpolation)


def detect_ui_scale(image: np.ndarray, vision: Vision, names: list[str], threshold: float | None = None) -> float | None:
    """The UI scale at which any of ``names`` matches best on a raw screenshot,
    or ``None`` if none of them is on the screen at any scale."""
    threshold = vision.default_threshold if threshold is None else threshold
    screen = normalize(image)
    small = cv2.resize(screen, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    templates = [image_ for name in names if vision.library.has(name) for _, image_ in vision.library.get(name)]

    def score(scale: float, coarse: bool = False) -> float:
        area, factor = (small, scale / 2) if coarse else (screen, scale)
        best = 0.0
        for template in templates:
            t = cv2.resize(template, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA if coarse else cv2.INTER_LINEAR)
            if t.shape[0] <= area.shape[0] and t.shape[1] <= area.shape[1]:
                best = max(best, float(cv2.minMaxLoc(cv2.matchTemplate(area, t, cv2.TM_CCOEFF_NORMED))[1]))
        return best

    coarse = max(UI_SCALES, key=lambda s: score(s, coarse=True))
    # The match falls off within a couple of hundredths, so finish in fine steps.
    fine = max((round(coarse + 0.01 * i, 2) for i in range(-4, 5)), key=score)
    # Verify the way the bot will actually see it: the screen rescaled, templates as they are.
    rescaled = normalize(image, ui_scale=fine)
    found = any(match_template(rescaled, t, threshold) for t in templates)
    return fine if found else None


def normalize_screen(image: np.ndarray, vision: Vision, ui_scale: float = 0.0) -> np.ndarray:
    """``normalize`` with a configured UI scale, or one worked out from the
    buttons the bot uses when ``ui_scale`` is 0 (falling back to 1)."""
    if not ui_scale:
        ui_scale = detect_ui_scale(image, vision, ANCHORS) or 1.0
    return normalize(image, ui_scale=ui_scale)


class T:
    """Template names the bot expects under the template root.

    See ``templates/README.md`` for what to crop for each one.
    """

    # Getting into a battle: Battle → Find a Match → Attack! (army screen)
    BATTLE = "buttons/battle"  # directory: the button with its old and new icons
    FIND_MATCH = "buttons/find_match"
    ARMY_ATTACK = "buttons/attack"
    # Scouting / battle
    NEXT = "buttons/next"
    END_BATTLE = "buttons/end_battle"  # directory: "End Battle", or "Surrender" once units are out
    SURRENDER_OK = "buttons/okay"
    RETURN_HOME = "buttons/return_home"  # directory: "Return Home", or "Claim Reward" after a star bonus
    EVENT_CHEST = "buttons/event_chest"  # chest room shown after some battles during events
    CONTINUE = "buttons/continue"  # under the chest's reward
    CC_CARD = "cards/clan_castle"  # optional: lets the bot leave CC troops out
    DEAD_BASE = "deadbase"  # directory: full collectors, for dead-base search
    # Home village (optional)
    CLOSE = "buttons/close"
    BUILD = "buttons/build"  # home village, bottom right: opens Buildings & Heroes
    WALL_SUGGESTION = "buttons/wall_suggestion"  # "Wall x N" in that list
    UPGRADE = "buttons/upgrade"  # under a selected building (gold and elixir versions look alike)
    UPGRADE_CONFIRM = "buttons/upgrade_confirm"  # "Confirm" in the upgrade window
    # Builder base
    BB_HOME_BOAT = "builder/home_boat"  # home village, bottom left: sails to the builder base
    BB_BUILDER_BOAT = "builder/builder_boat"  # builder base, top right: sails back
    BB_BATTLE = "builder/battle"  # builder base Battle (crossed axes)
    BB_FIND_NOW = "builder/find_now"
    BB_BATTLE_STARTS = "builder/battle_starts"  # "Battle starts in:" before the first troop
    BB_BATTLE_ENDS = "builder/battle_ends"  # "Battle ends in:" once it started
    BB_RETURN_HOME = "builder/return_home"  # builder battle result
    BB_OKAY = "builder/okay"  # Star Bonus window after a builder battle
    BB_CHANGE_HEROES = "builder/change_heroes"  # the hero picker that opens from the hero card's swap button
    BB_SPEED = "builder/speed_1x"  # "1x" during a builder battle: tap for 4x
    BB_COLLECT_GOLD = "builder/collect_gold"  # bubbles over full builder base collectors
    BB_COLLECT_ELIXIR = "builder/collect_elixir"  # (the elixir cart's tag looks the same)
    BB_CART_COLLECT = "builder/cart_collect"  # Collect in the Elixir Cart window
    BB_UPGRADE = "builder/upgrade"  # under a selected builder base building
    BB_UPGRADE_TITLE = "builder/upgrade_title"  # "Upgrade to level N?" window (price on a green button)
    LOOT_CART = "buttons/loot_cart"  # the "Collect" label over the loot cart
    LOOT_CART_COLLECT = "buttons/loot_cart_collect"  # the Collect button once it is selected
    # Full-collector bubbles (one tap empties every collector of that kind)
    COLLECT_GOLD = "collectors/gold"
    COLLECT_ELIXIR = "collectors/elixir"
    COLLECT_DARK = "collectors/dark"
    TRY_AGAIN = "buttons/try_again"
    RELOAD = "buttons/reload"
    OKAY = "buttons/okay"
    # Clan (optional)
    REQUEST = "buttons/request"
    REQUEST_SEND = "buttons/request_send"
    OPEN_CHAT = "buttons/open_chat"
    CLOSE_CHAT = "buttons/close_chat"
    NO_CLAN = "buttons/no_clan"  # "Join A Clan" in the chat panel: not in a clan
    DONATE = "buttons/donate"
    DONATE_ITEM_DIR = "donate"  # donate/<name>.png: units in the donate window
    # Digit glyphs (directory, made with ``farmbot learn-digits``)
    LOOT_FONT = "fonts/loot"


# HUD buttons, drawn at the same scale as the loot numbers and troop bar,
# used to work out the UI scale. (Windows such as the army screen are sized
# differently; Vision finds their buttons' sizes on its own.)
ANCHORS = [T.BATTLE, T.NEXT, T.END_BATTLE]


class Stopped(Exception):
    """Raised inside long waits when the bot is asked to stop."""


class Game:
    def __init__(
        self,
        device: Device,
        vision: Vision,
        stop_event: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        reference_height: int = REFERENCE_HEIGHT,
        ui_scale: float = 0.0,
        window_scale: float = 0.0,
        picture_scales: dict[str, float] | None = None,
    ):
        self.device = device
        self.vision = vision
        self.stop_event = stop_event or threading.Event()
        self._sleep = sleep
        self.rng = rng or random.Random()
        self.clock = clock
        self.reference_height = reference_height
        self.scale = 1.0  # device pixels per reference pixel
        # 0 = not known yet: found from the first button that is on screen.
        self.ui_scale = ui_scale
        self.window_scale = window_scale
        self.picture_scales = dict(picture_scales or {})
        vision.scales.update(self.picture_scales)
        vision.fixed_scales = set(self.picture_scales)
        self.raw: np.ndarray | None = None
        self.screen: np.ndarray | None = None
        self._calibration_tried: set[str] = set()  # on the current screenshot

    # -- timing ---------------------------------------------------------
    def wait(self, seconds: float) -> None:
        if self.stop_event.is_set():
            raise Stopped
        self._sleep(seconds)
        if self.stop_event.is_set():
            raise Stopped

    # -- screen ---------------------------------------------------------
    def refresh(self) -> np.ndarray:
        self.raw = self.device.screenshot()
        self._calibration_tried.clear()
        return self._rescale()

    def _rescale(self) -> np.ndarray:
        h = self.raw.shape[0]
        ui = self.ui_scale or 1.0
        self.vision.window_scale = self.window_scale or round(1 / ui, 2)
        self.scale = h * ui / self.reference_height if self.reference_height else 1.0
        self.screen = normalize(self.raw, self.reference_height, ui)
        return self.screen

    def _calibrate(self, name: str) -> bool:
        """Work out the UI scale from ``name`` when it isn't known yet."""
        if self.ui_scale or self.raw is None or name in self._calibration_tried or not self.has(name):
            return False
        self._calibration_tried.add(name)
        scale = detect_ui_scale(self.raw, self.vision, [name])
        if scale is None:
            return False
        log.info("Buttons on this screen are %.2fx the pictures' size; scaling screenshots to match", scale)
        self.ui_scale = scale
        # Sizes found before were relative to the old scale.
        self.vision.scales.clear()
        self.vision.scales.update(self.picture_scales)
        self._rescale()
        return True

    def _current(self) -> np.ndarray:
        return self.screen if self.screen is not None else self.refresh()

    @property
    def size(self) -> tuple[int, int]:
        h, w = self._current().shape[:2]
        return w, h

    def region(self, values: list[int] | Rect) -> Rect:
        """A config rectangle resolved against the current screen size."""
        rect = values if isinstance(values, Rect) else Rect.from_list(values)
        return rect.resolve(*self.size)

    def has(self, name: str) -> bool:
        return self.vision.library.has(name)

    def find(self, name: str, threshold: float | None = None, region: Rect | None = None) -> Match | None:
        matches = self.find_all(name, threshold, region)
        return matches[0] if matches else None

    def find_all(self, name: str, threshold: float | None = None, region: Rect | None = None) -> list[Match]:
        screen = self._current()
        if not self.ui_scale and name in ANCHORS and self._calibrate(name):
            screen = self.screen
        return self.vision.find_all(screen, name, threshold, region)

    def read_number(self, region: Rect, font: str) -> int | None:
        return self.vision.read_number(self._current(), region, font)

    # -- input ----------------------------------------------------------
    def tap(self, x: int, y: int, jitter: int = 0) -> None:
        if jitter:
            x += self.rng.randint(-jitter, jitter)
            y += self.rng.randint(-jitter, jitter)
        self.device.tap(round(x * self.scale), round(y * self.scale))

    def tap_many(self, points: list[tuple[int, int]], jitter: int = 0, pause: float = 0.0) -> None:
        """Tap each point in quick succession: one burst on devices that
        support it, single taps ``pause`` seconds apart otherwise."""
        if self.stop_event.is_set():
            raise Stopped
        scaled = []
        for x, y in points:
            if jitter:
                x += self.rng.randint(-jitter, jitter)
                y += self.rng.randint(-jitter, jitter)
            scaled.append((round(x * self.scale), round(y * self.scale)))
        burst = getattr(self.device, "tap_many", None)
        if burst is not None:
            burst(scaled, pause)
            return
        for point in scaled:
            self.device.tap(*point)
            if pause:
                self.wait(pause)

    def zoom_out(self) -> bool:
        """Pinch the view all the way out, on devices that can. The game
        keeps its zoom between battles, and a zoomed-in view hides the
        map's edges where troops go."""
        pinch = getattr(self.device, "zoom_out", None)
        if pinch is None or not pinch():
            return False
        self.wait(0.5)
        self.screen = None
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        s = self.scale
        self.device.swipe(round(x1 * s), round(y1 * s), round(x2 * s), round(y2 * s), duration_ms)
        self.screen = None

    def tap_match(self, match: Match) -> None:
        # Tap somewhere inside the middle of the matched button.
        self.tap(match.x, match.y, jitter=max(0, min(match.w, match.h) // 5))

    def click(self, name: str, threshold: float | None = None, region: Rect | None = None, leftmost: bool = False) -> bool:
        matches = self.find_all(name, threshold, region)
        if not matches:
            return False
        match = min(matches, key=lambda m: m.x) if leftmost else matches[0]
        log.debug("tap %s at %s (%.2f)", name, match.point, match.score)
        self.tap_match(match)
        self.screen = None
        return True

    def wait_for(self, name: str, timeout: float, interval: float = 0.5, threshold: float | None = None) -> Match | None:
        deadline = self.clock() + timeout
        while True:
            self.refresh()
            match = self.find(name, threshold)
            if match or self.clock() >= deadline:
                return match
            self.wait(interval)

    def click_when_visible(self, name: str, timeout: float, threshold: float | None = None, leftmost: bool = False) -> bool:
        if not self.wait_for(name, timeout, threshold=threshold):
            return False
        return self.click(name, threshold, leftmost=leftmost)
