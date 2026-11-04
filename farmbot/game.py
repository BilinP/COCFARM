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
from farmbot.text import TextStyle
from farmbot.vision import Match, Vision, _resized, best_scale, match_template

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
CALIBRATE_RETRY = 5.0  # seconds before an anchor that didn't give the UI scale is measured again
COARSE_MARGIN = 0.1  # how far below the threshold the half-size search may score and still be worth refining


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

    coarse_score, coarse = max((score(s, coarse=True), s) for s in UI_SCALES)
    # Not even close at half size: the button isn't there, so skip the slow
    # full-size search. (On the real screenshots in tests/fixtures, a button
    # that is there scores 0.89 or more here, one that isn't 0.81 at most.)
    if coarse_score < threshold - COARSE_MARGIN:
        return None
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
    # A super troop that isn't boosted: "Activate" under its card on the army screen, the
    # "Boost?" window that opens, and that window's dark elixir button (never the Super Potion)
    SUPER_ACTIVATE = "buttons/super_activate"
    SUPER_BOOST_TITLE = "buttons/super_boost_title"
    SUPER_BOOST_DARK = "buttons/super_boost_dark"
    # Scouting / battle
    NEXT = "buttons/next"
    END_BATTLE = "buttons/end_battle"  # directory: "End Battle", or "Surrender" once units are out
    SURRENDER_OK = "buttons/okay"
    SPEED = "buttons/speed_1x"  # "1x" in the last minute of a home battle: tap for 4x
    RETURN_HOME = "buttons/return_home"  # directory: "Return Home", or "Claim Reward" after a star bonus
    EVENT_CHEST = "buttons/event_chest"  # chest room shown after some battles during events
    CONTINUE = "buttons/continue"  # under the chest's reward
    PICK_REWARD = "buttons/pick_reward"  # "Pick a Reward!" banner over event reward cards
    # Home village (optional)
    CLOSE = "buttons/close"
    BUILD = "buttons/build"  # home village, bottom right: opens Buildings & Heroes
    GOBLIN = "buttons/goblin"  # over the builder/lab counter: the Goblin Builder/Researcher (gems)
    WALL_SUGGESTION = "buttons/wall_suggestion"  # "Wall x N" in that list
    UPGRADE = "buttons/upgrade"  # under a selected building (gold and elixir versions look alike)
    UPGRADE_CONFIRM = "buttons/upgrade_confirm"  # "Confirm" in the upgrade window
    UPGRADE_MORE = "buttons/upgrade_more"  # under a selected wall: starts picking more of its level
    # then: the words "Add Wall" under the +10 button (left) and the +1 one (only +1
    # when fewer than ten walls of that level are left). The words, not the
    # buttons' wall pictures: those look different at every wall level.
    ADD_WALL = "buttons/add_wall"
    # The Buildings & Heroes list's filter menu (the slider button left of its
    # title). The buttons light up yellow when on; the resource and type
    # ones aren't used yet (for upgrading other things later).
    LIST_FILTER = "buttons/list_filter"
    SORT_COST_MAX = "buttons/filter/cost_max"  # "Max" of Cost Max: dearest first
    SORT_COST_MIN = "buttons/filter/cost_min"  # "Min" of Cost Min: cheapest first
    FILTER_GOLD = "buttons/filter/gold"
    FILTER_ELIXIR = "buttons/filter/elixir"
    FILTER_DARK = "buttons/filter/dark"
    FILTER_ARMY = "buttons/filter/army"
    FILTER_DEFENSE = "buttons/filter/defense"
    FILTER_HERO = "buttons/filter/hero"
    FILTER_RESOURCE = "buttons/filter/resource"
    FILTER_NEW = "buttons/filter/new"
    FILTER_PERMANENT = "buttons/filter/permanent"
    FILTER_TROOPS = "buttons/filter/troops"  # the Army Upgrades tab's types
    FILTER_SPELLS = "buttons/filter/spells"
    FILTER_SIEGE = "buttons/filter/siege"
    # A list row's price icon (right of the number).
    COST_GOLD = "buttons/cost/gold"
    COST_ELIXIR = "buttons/cost/elixir"
    COST_DARK = "buttons/cost/dark"
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
    BB_HERO_MACHINE = "builder/hero_machine"  # the Battle Machine's card art
    BB_HERO_COPTER = "builder/hero_copter"  # the Battle Copter's card art
    BB_SPEED = "builder/speed_1x"  # "1x" during a builder battle: tap for 4x
    BB_COLLECT_GOLD = "builder/collect_gold"  # bubbles over full builder base collectors
    BB_COLLECT_ELIXIR = "builder/collect_elixir"  # (the elixir cart's tag looks the same)
    BB_COLLECT_GEMS = "builder/collect_gems"  # the green gem over a full gem mine
    BB_CART_COLLECT = "builder/cart_collect"  # Collect in the Elixir Cart window
    BB_CLOCK_READY = "builder/clock_ready"  # the clock over the Clock Tower while its free boost is ready
    BB_CLOCK_FREE_BOOST = "builder/clock_free_boost"  # "Free Boost!" under the selected Clock Tower
    BB_FREE_BOOST_WINDOW = "builder/free_boost_window"  # title of the window it opens
    BB_FREE_BOOST_CONFIRM = "builder/free_boost_confirm"  # its green Boost (no price: free)
    BB_UPGRADE = "builder/upgrade"  # under a selected builder base building
    BB_UPGRADE_TITLE = "builder/upgrade_title"  # "Upgrade to level N?" window (price on a green button)
    BB_FILTER_GOLD = "builder/filter/gold"  # the Build list's filter menu, builder base resources
    BB_FILTER_ELIXIR = "builder/filter/elixir"
    BB_COST_GOLD = "builder/cost/gold"  # right of a builder base row's price
    BB_COST_ELIXIR = "builder/cost/elixir"
    LOOT_CART = "buttons/loot_cart"  # the "Collect" label over the loot cart
    LOOT_CART_COLLECT = "buttons/loot_cart_collect"  # the Collect button once it is selected
    GRAVESTONE = "collectors/gravestone"  # left by an attacker's troops: tapping one clears them all
    # Full-collector bubbles (one tap empties every collector of that kind)
    COLLECT_GOLD = "collectors/gold"
    COLLECT_ELIXIR = "collectors/elixir"
    COLLECT_DARK = "collectors/dark"
    TRY_AGAIN = "buttons/try_again"
    RELOAD = "buttons/reload"
    LOGIN = "buttons/login"  # the game's start screen with no account logged in: "Log in with Supercell ID"
    RATE_TITLE = "buttons/rate_title"  # Google Play's "Rate Clash of Clans" dialog, over the game
    RATE_NEVER = "buttons/rate_never"  # its NEVER: it isn't asked again
    MAINTENANCE = "buttons/maintenance"  # the "Maintenance break" banner: the game can't be played meanwhile
    OKAY = "buttons/okay"
    # Clan (optional)
    REQUEST = "buttons/request"
    REQUEST_SEND = "buttons/request_send"
    OPEN_CHAT = "buttons/open_chat"
    CLOSE_CHAT = "buttons/close_chat"
    NO_CLAN = "buttons/no_clan"  # "Join A Clan" in the chat panel: not in a clan
    DONATE = "buttons/donate"
    CONFIRM = "buttons/confirm"  # "Confirm" on notices such as "The Event has ended"
    GEM = "icons/gem"  # a gem: a Confirm window showing one is never pressed
    STORAGE_MAX = "icons/storage_max"  # "Max:" in the box a tapped resource bar opens
    # "Defensive Layout obstructed" after asking for a battle, and its Attack button
    LAYOUT_OBSTRUCTED = "buttons/layout_obstructed"
    OBSTRUCTED_ATTACK = "buttons/obstructed_attack"
    # Clan Capital raids (Raid Weekends)
    CAPITAL_BADGE = "capital/raid_badge"  # the sword on the red badge over the capital airship: raids left
    CAPITAL_COLLAPSE = "capital/collapse_bar"  # "<" at the end of the event buttons: folds them off the airship
    CAPITAL_NEXT_RAID = "capital/next_raid"  # "Next Raid" under the last raid's summary
    CAPITAL_RAID_MAP = "capital/raid_map"  # "Raid Map", bottom right of the capital
    CAPITAL_LEAVE_RAID = "capital/leave_raid"  # the airship "Map" button, bottom left of the raid map
    CAPITAL_RETURN_HOME = "capital/return_home"  # "Return Home", bottom left of the capital
    CAPITAL_DISTRICT_ATTACK = "capital/district_attack"  # red Attack under a picked district
    CAPITAL_ATTACK_NOW = "capital/attack_now"  # "Attack Now?" title
    CAPITAL_ATTACK_GO = "capital/attack_go"  # its Attack, green only with a full army
    CAPITAL_SURE_ATTACK = "capital/sure_attack"  # "Capital Gold storage is full. Attack anyway?"
    CAPITAL_NOT_MEMBER = "capital/not_member"  # "Only members who were in the Clan before the Raid Weekend..."
    CAPITAL_CARD_DIR = "capital/cards"  # capital/cards/<name>.png: the army editor's units, found in the troop bar too
    CAPITAL_BATTLE_HEAL = "capital/battle_heal"  # the heal spell's card in the raid's troop bar
    CAPITAL_RESULT_MAP = "capital/result_map"  # "Map" under a raid's result
    BAT_SPELL_CARD = "cards/bat_spell"  # the Bat Spell card's picture in the troop bar
    DONATE_ITEM_DIR = "donate"  # donate/<unit slug>.png: units in the donate window
    DONATE_WINDOW = "buttons/donate_window"  # the donate window's "Donation Resource" title
    # Digit glyphs (directory, made with ``farmbot learn-digits``)
    LOOT_FONT = "fonts/loot"


# HUD buttons, drawn at the same scale as the loot numbers and troop bar,
# used to work out the UI scale. (Windows such as the army screen are sized
# differently; Vision finds their buttons' sizes on its own.)
ANCHORS = [T.BATTLE, T.NEXT, T.END_BATTLE]

# Window buttons that give the windows' size when it isn't the HUD's: MuMu
# draws the game's windows (the battle menu, the army screen) about 0.95x the
# size the HUD buttons work out to, where the other emulators draw them alike.
WINDOW_ANCHORS = [T.FIND_MATCH]
WINDOW_SIZES = (0.9, 0.95, 1.05, 1.1)  # each looked at give or take 0.03

# The buttons under a selected building change size with the layout: with
# the Buildings & Heroes list open they move into two rows, drawn about 1.3x
# (the wall's Upgrade, Upgrade More, Add Wall). When one isn't found at the
# usual sizes these are tried too, and the size that matched is tried first
# from then on.
ANY_SIZE = {T.UPGRADE, T.UPGRADE_MORE, T.ADD_WALL}
ANY_SIZES = (1.3, 1.2, 1.1, 1.4, 0.85)

# Zooming out: pinches at most, and the share of the middle of the screen
# whose grey level changes by more than VIEW_GREY (0-255) below which the
# view has stopped moving. The village's animations (water, flags, troops
# walking) change 2-4% of it; a zoom or a drag, over half.
ZOOM_PASSES = 3
ZOOM_TRIES = 2  # zoom-outs, when one leaves a building selected
VIEW_GREY = 40
VIEW_STILL = 0.12
SELECTED_TOP = 0.6  # a selected building's buttons show below this share of the screen's height


def view_change(before: np.ndarray, after: np.ndarray) -> float:
    """How much the map moved between two screens: the share (0-1) of the
    middle of the screen that changed."""
    if before.shape != after.shape:
        return 1.0
    h, w = before.shape[:2]
    a, b = (cv2.cvtColor(s[h // 6:h * 5 // 6, w // 6:w * 5 // 6], cv2.COLOR_BGR2GRAY) for s in (before, after))
    return float((cv2.absdiff(a, b) > VIEW_GREY).mean())


class Stopped(Exception):
    """Raised inside long waits when the bot is asked to stop."""


class GameNotInFront(RuntimeError):
    """Another app is in front of the game (it crashed or closed): taps
    would land in that app instead."""


FRONT_CHECK = 3.0  # seconds between checks that the game is still the app in front, before tapping


# Matching two pictures of the map by features (they only need to overlap
# in part): how far the camera moved between them.
VIEW_AREA = (150, 240, 1000, 760)  # x0, y0, x1, y1: the map, clear of the buttons, trophies and resource bars
VIEW_FEATURES = 1500
VIEW_RATIO = 0.75  # Lowe's ratio test
VIEW_AGREE = 15  # matches that must agree on the shift (within VIEW_NEAR px)
VIEW_NEAR = 6


def view_shift(reference: np.ndarray, screen: np.ndarray) -> tuple[int, int] | None:
    """How far the map has moved from ``reference`` to ``screen`` (what was
    at p is now at p + shift), or ``None`` if they don't overlap enough."""
    x0, y0, x1, y1 = VIEW_AREA
    mask = np.zeros(reference.shape[:2], np.uint8)
    mask[y0:y1, x0:x1] = 255
    orb = cv2.ORB_create(VIEW_FEATURES)
    k1, d1 = orb.detectAndCompute(cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY), mask)
    k2, d2 = orb.detectAndCompute(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), mask)
    if d1 is None or d2 is None or len(k1) < 2 or len(k2) < 2:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < VIEW_RATIO * p[1].distance]
    if len(good) < VIEW_AGREE:
        return None
    shifts = np.array([np.subtract(k2[m.trainIdx].pt, k1[m.queryIdx].pt) for m in good])
    shift = np.median(shifts, axis=0)
    if int(np.sum(np.all(np.abs(shifts - shift) <= VIEW_NEAR, axis=1))) < VIEW_AGREE:
        return None
    return round(shift[0]), round(shift[1])


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
        vision.two_sizes = {T.OKAY}  # the surrender Okay and the Star Bonus window's
        self.raw: np.ndarray | None = None
        self.screen: np.ndarray | None = None
        # When each anchor last failed to give the UI scale. Measuring takes about a second, so while
        # the game loads it's tried again every CALIBRATE_RETRY seconds, not on every screenshot.
        self._calibration_tried: dict[str, float] = {}
        self._any_sizes: dict[str, float] = {}  # ANY_SIZE picture -> the size it last matched at
        self._selected_sizes: dict[str, float] = {}  # the same, under a selected building (no list open)
        self._zoomed_view: np.ndarray | None = None  # the screen the last full zoom-out ended on
        # Finds on the screenshot in ``_seen_on``: (name, threshold, region) -> matches.
        self._seen_on: np.ndarray | None = None
        self._seen: dict[tuple, list[Match]] = {}
        # The game's package, set by the bot: taps stop if another app comes in front.
        self.package = ""
        self._front_checked = -FRONT_CHECK

    # -- timing ---------------------------------------------------------
    def wait(self, seconds: float) -> None:
        if self.stop_event.is_set():
            raise Stopped
        if self._sleep is time.sleep:
            self.stop_event.wait(seconds)  # "Stop now" cuts the wait short
        else:
            self._sleep(seconds)
        if self.stop_event.is_set():
            raise Stopped

    # -- screen ---------------------------------------------------------
    def refresh(self) -> np.ndarray:
        self.raw = self.device.screenshot()
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
        if self.ui_scale or self.raw is None or not self.has(name):
            return False
        if self.clock() - self._calibration_tried.get(name, -CALIBRATE_RETRY) < CALIBRATE_RETRY:
            return False
        self._calibration_tried[name] = self.clock()
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

    def _calibrate_window(self, screen: np.ndarray, name: str, threshold: float | None,
                          region: Rect | None) -> bool:
        """Work out the windows' size from ``name`` when it isn't on screen at
        the size the HUD gives (and none is set in the config)."""
        if self.window_scale or not self.has(name):
            return False
        key = "window:" + name
        if self.clock() - self._calibration_tried.get(key, -CALIBRATE_RETRY) < CALIBRATE_RETRY:
            return False
        self._calibration_tried[key] = self.clock()
        threshold = self.vision.default_threshold if threshold is None else threshold
        now = self.vision.window_scale
        for tname, image in self.vision.library.get(name):
            size, score = best_scale(screen, image, list(WINDOW_SIZES), region)
            if score >= threshold and abs(size - now) >= 0.02:
                log.info("Windows on this screen are %.2fx the pictures' size; looking for their buttons at it", size)
                self.window_scale = size
                self.vision.scales.clear()  # sizes found before were relative to the old one
                self.vision.scales.update(self.picture_scales)
                self.vision.scales.setdefault(tname, size)
                self._rescale()
                return True
        return False

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
        # The same look at the same screenshot (the screen check, then the
        # step that acts on it, say) is answered from the first one.
        if self._seen_on is not screen:
            self._seen_on, self._seen = screen, {}
        key = (name, threshold, region)
        if key not in self._seen:
            found = self.vision.find_all(screen, name, threshold, region)
            if not found and name in ANY_SIZE:
                found = self._find_any_size(screen, name, threshold, region)
            if not found and name in WINDOW_ANCHORS and self._calibrate_window(screen, name, threshold, region):
                found = self.vision.find_all(self.screen, name, threshold, region)
            self._seen[key] = found
        return list(self._seen[key])

    def _find_any_size(self, screen: np.ndarray, name: str, threshold: float | None,
                       region: Rect | None) -> list[Match]:
        """``name`` at a size near one of ANY_SIZES (the one that last
        matched is tried first, on its own: quick)."""
        threshold = self.vision.default_threshold if threshold is None else threshold
        for tname, image in self.vision.library.get(name):
            last = self._any_sizes.get(tname)
            if last:
                found = match_template(screen, _resized(image, last), threshold, region, tname)
                if found:
                    return found
            size, score = best_scale(screen, image, list(ANY_SIZES), region)
            if score >= threshold:
                log.debug("%s found at %.2fx its picture's size", tname, size)
                self._any_sizes[tname] = size
                return match_template(screen, _resized(image, size), threshold, region, tname)
        return []

    def likeness(self, name: str) -> float:
        """How close ``name``'s best picture comes to matching the screen
        (0..1): for logs, when a button that should be there isn't found."""
        if not self.has(name):
            return 0.0
        screen = self._current()
        return max(max(self.vision.likeness(screen, image),
                       best_scale(screen, image, list(ANY_SIZES))[1] if name in ANY_SIZE else 0.0)
                   for _, image in self.vision.library.get(name))

    def read_number(self, region: Rect, font: str, style: TextStyle = TextStyle()) -> int | None:
        return self.vision.read_number(self._current(), region, font, style)

    # -- input ----------------------------------------------------------
    def check_in_front(self) -> None:
        """Raise ``GameNotInFront`` if another app has come in front of the
        game (a crash drops to the emulator's home screen, whose icons and
        ads the next taps would open). Looked at every FRONT_CHECK seconds."""
        if not self.package or self.clock() - self._front_checked < FRONT_CHECK:
            return
        front = self.device.foreground_package()
        if front and front != self.package:
            self._front_checked = -FRONT_CHECK  # look again before the next tap
            raise GameNotInFront(f"{front} is in front instead of the game (it crashed or closed); not tapping")
        self._front_checked = self.clock()

    def tap(self, x: int, y: int, jitter: int = 0) -> None:
        self.check_in_front()
        if jitter:
            x += self.rng.randint(-jitter, jitter)
            y += self.rng.randint(-jitter, jitter)
        self.device.tap(round(x * self.scale), round(y * self.scale))

    def tap_many(self, points: list[tuple[int, int]], jitter: int = 0, pause: float = 0.0) -> None:
        """Tap each point in quick succession: one burst on devices that
        support it, single taps ``pause`` seconds apart otherwise."""
        if self.stop_event.is_set():
            raise Stopped
        self.check_in_front()
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

    def stop_fast_taps(self) -> bool:
        """Switch the device from its fast tap bursts to plain taps. True if
        that changed anything (so a refused deploy is worth trying again)."""
        stop = getattr(self.device, "stop_fast_taps", None)
        return bool(stop and stop())

    def zoom_out(self) -> bool:
        """Pinch the view all the way out, on devices that can. The game
        keeps its zoom between battles, and a zoomed-in view hides the
        map's edges where troops go. A pinch close in on a wall just
        upgraded can select it (and a pinch finger on a selected building
        moves it, so whoever left one selected drops it first): one that
        did is dropped, and the view pinched again."""
        pinch = getattr(self.device, "zoom_out", None)
        if pinch is None:
            return False
        self.check_in_front()
        if self._still_zoomed_out(self.refresh()):
            # The view the last zoom-out ended on, unmoved: still all the
            # way out. Another pinch would only make the map spring back.
            log.debug("Still zoomed out: no pinch")
            return True
        self._zoomed_view = None
        for _ in range(ZOOM_TRIES):
            # Pinch again while the view keeps changing: a pinch the game misses
            # (it happens on LDPlayer) must not leave a battle zoomed in.
            before = self.refresh()
            still = False
            for _ in range(ZOOM_PASSES):
                log.debug("Pinching out")
                if not pinch():
                    return False
                self.wait(0.5)
                after = self.refresh()
                if view_change(before, after) < VIEW_STILL:
                    still = True
                    break
                before = after
            if not self.deselect():
                if still:
                    self._zoomed_view = after
                return True
            log.info("Zooming out selected a building: dropped it, zooming again")
        return True

    def _still_zoomed_out(self, screen: np.ndarray) -> bool:
        """``screen`` shows the very view the last full zoom-out ended on:
        hardly anything changed, and the map's features sit where they did
        (another base, mostly grass too, can change as little)."""
        view = self._zoomed_view
        if view is None or view_change(view, screen) >= VIEW_STILL:
            return False
        shift = view_shift(view, screen)
        return shift is not None and abs(shift[0]) <= VIEW_NEAR and abs(shift[1]) <= VIEW_NEAR

    def selected(self) -> bool:
        """A building is selected in a village (its Upgrade button shows
        under it). The button's size is looked for once (about a second),
        then only that size is (quick)."""
        buttons = [n for n in (T.UPGRADE, T.BB_UPGRADE) if self.has(n)]
        if not buttons or not self.has(T.BUILD):
            return False
        self.refresh()
        if self.find(T.BUILD) is None:
            return False
        w, h = self.size
        below = Rect(0, round(h * SELECTED_TOP), w, h - round(h * SELECTED_TOP))  # where the buttons show
        for name in buttons:
            if self.vision.find_all(self.screen, name, None, below):
                return True
            if name not in ANY_SIZE:
                continue
            for tname, image in self.vision.library.get(name):
                size = self._selected_sizes.get(tname)
                if size is None:
                    size, score = best_scale(self.screen, image, list(ANY_SIZES), below)
                    if score < self.vision.default_threshold:
                        continue
                    self._selected_sizes[tname] = size
                if match_template(self.screen, _resized(image, size), self.vision.default_threshold, below, tname):
                    return True
        return False

    def deselect(self, always: bool = False) -> bool:
        """Leave nothing selected: a building stays selected once the windows
        over it are closed. Opening the Build list and closing it again drops
        it (Back would ask to quit the game). ``always``: even when no
        Upgrade button shows (a building being upgraded has none). True if
        the list was opened for it."""
        if not (always or self.selected()):
            return False
        self.refresh()
        if self.click(T.BUILD) and self.wait_for(T.CLOSE, timeout=4):
            self.wait(0.5)
            for _ in range(2):
                self.refresh()
                if not self.click(T.CLOSE):
                    break
                self.wait(0.8)
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        self.check_in_front()
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
