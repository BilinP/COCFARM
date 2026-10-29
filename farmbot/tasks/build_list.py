"""The Build button's list: Buildings & Heroes, Army Upgrades (the
laboratory), Hero Equipment and Hero Pets, one tab each along its bottom.
The builder base's has two tabs (Buildings & Heroes, and the Star
Laboratory's Army Upgrades), its own resource pictures and a plainer upgrade
window: see BUILDER.

Each row shows what an upgrade costs, a number with the resource's icon at
the right. The filter menu (the slider button left of the title) narrows a
tab to one resource and one type and sorts it by cost, so the rows left are
exactly the candidates for an upgrade. Tapping a row opens its upgrade window
(walls instead select a wall in the village: see village.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.text import pieces

log = logging.getLogger(__name__)

# The list hangs on the screen's right edge, so on wider screens it's
# measured from there: x from the right edge, y from the bottom.
LIST_SWIPE_X = 247
LIST_SWIPE_FROM_BOTTOM = 220  # where a swipe starts
LIST_SWIPE_TO_Y = 250  # where a long swipe ends
LIST_STEP = 150  # a short swipe: under two rows
LIST_TOP = 70  # the list's rows start under its title...
LIST_BOTTOM_FROM_BOTTOM = 110  # ...and end above its tabs
LIST_PAGE = 400  # a swipe in a sorted list: under the list's height, so no row is skipped
SLOW_SWIPE_MS = 900
MAX_SORTED_SCROLLS = 30  # the whole list, top to bottom
# While the list is open it pushes the resource bar this many "Wall x"
# heights to the left (487 px at 860 x 672).
HUD_SHIFT = 23.2
HUD_SHIFT_PX = 487  # the same, for when no "Wall x" is on show to measure by

# The tabs along the list's bottom, x from the screen's right edge.
TABS = {"buildings": 433, "army": 309, "equipment": 187, "pets": 65}
TAB_FROM_BOTTOM = 58

# The filter menu's buttons, in groups: one of each group can be on.
TYPE_BUTTONS = (T.FILTER_ARMY, T.FILTER_DEFENSE, T.FILTER_HERO, T.FILTER_RESOURCE, T.FILTER_NEW,
                T.FILTER_PERMANENT, T.FILTER_TROOPS, T.FILTER_SPELLS, T.FILTER_SIEGE)
SORT_BUTTONS = (T.SORT_COST_MAX, T.SORT_COST_MIN)
RESOURCE_FILTER = {"gold": T.FILTER_GOLD, "elixir": T.FILTER_ELIXIR, "dark": T.FILTER_DARK}
LIT_SATURATION, LIT_VALUE = 180, 190  # on (yellow): 218, 234; off (brown): 125, 148
GREY_SATURATION = 40  # a grey button: nothing of that type to upgrade, so it can't be picked

COST_ICONS = {"gold": T.COST_GOLD, "elixir": T.COST_ELIXIR, "dark": T.COST_DARK}
ICON_COLUMN = 90  # the cost icons sit this far from the screen's right edge, at most
# A row's price, in icon heights left of its icon.
PRICE_LEFT, PRICE_WIDTH = 8.0, 7.4
ROW_TAP_FROM_RIGHT = 280  # where a row is tapped: on its name, clear of the price
ROW_TAP_ABOVE = 0.6  # ...this many icon heights above the price line
WALL_ROW_BAND = (-50, 0)  # a "Wall x" text this far above a price is a wall row

# The first row of a tab, on its own: a dark grey bar (an upgrade or research
# under way) or a brown heading/row.
BUSY_STRIP = (277, 117, 92, 108)  # x from the right edge (from, to), y (from, to)
BUSY_SATURATION = (50, 100)  # under way: 74; a heading: 118
BUSY_VALUE = (85, 125)  # under way: 103; a heading: 78

GREEN_HUE, GREEN_SATURATION, GREEN_VALUE = (30, 60), 80, 150  # the resource Confirm (40, 111, 226)
CONFIRM_TIMEOUT = 3

# The builder counter ("3/5": free of all) above the Build button, in Build
# text heights from its middle.
BUILDERS = (-3.0, -6.8, 2.75, 1.2)  # x, y, width, height
SEPARATOR_SCORE = 0.65  # the "/" reads as a poor digit
# A tab's own counter ("1/2": free of all) under its picture: x either side
# of the tab's middle, y from the bottom (top, height).
TAB_COUNTER = (45, 55, 30)

# The builder base's upgrade windows have no Confirm: the price itself is a
# big green button, with the resource's icon in it (a gem button never has
# a gold or elixir icon).
PAY_GREEN = ((35, 150, 170), (50, 255, 255))  # HSV low, high
PAY_SIZE = (150, 5000)  # this wide, and this many pixels, at least
PAY_ICON = {"gold": ((18, 150, 170), (32, 255, 255)), "elixir": ((125, 120, 120), (160, 255, 255))}
PAY_ICON_PIXELS = 500


@dataclass(frozen=True)
class BuildList:
    """What differs between the villages' lists."""

    tabs: dict[str, int]  # x from the screen's right edge
    tab_from_bottom: int
    resource_filter: dict[str, str]  # resource -> filter menu button
    cost_icons: dict[str, str]  # resource -> a row's price icon
    builder_base: bool = False

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(self.cost_icons)


HOME = BuildList(TABS, TAB_FROM_BOTTOM, RESOURCE_FILTER, COST_ICONS)
# The builder base's tabs are tapped higher: low down, a tap can miss them.
BUILDER = BuildList({"buildings": 371, "army": 124}, 75, {"gold": T.BB_FILTER_GOLD, "elixir": T.BB_FILTER_ELIXIR},
                    {"gold": T.BB_COST_GOLD, "elixir": T.BB_COST_ELIXIR}, builder_base=True)


@dataclass(frozen=True)
class Row:
    kind: str  # "gold", "elixir" or "dark"
    price: int
    x: int  # the cost icon's middle
    y: int
    h: int  # the cost icon's height


def _median_hsv(screen: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> tuple[float, float, float] | None:
    patch = screen[max(y0, 0):max(y1, 1), max(x0, 0):max(x1, 1)]
    if patch.size == 0:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    return tuple(float(np.median(hsv[:, :, i])) for i in range(3))


def _lit(screen: np.ndarray, match) -> bool:
    """Whether a filter menu button is on (yellow): the strip of button
    just above its text, or (the builder base's resource buttons, whose
    icons fill them nearly to the top) just left of it."""
    left = match.x - match.w // 2
    for hsv in (_median_hsv(screen, left, match.y - match.h, match.x + match.w // 2, match.y - match.h // 2 - 2),
                _median_hsv(screen, left - 14, match.y - 4, left - 4, match.y + 4)):
        if hsv is not None and hsv[1] >= LIT_SATURATION and hsv[2] >= LIT_VALUE:
            return True
    return False


def _grey(screen: np.ndarray, match) -> bool:
    hsv = _median_hsv(screen, match.x - match.w // 2, match.y - match.h, match.x + match.w // 2,
                      match.y - match.h // 2 - 2)
    return hsv is not None and hsv[1] < GREY_SATURATION


def set_filter(game: Game, resource: str | None = None, kind: str | None = None, sort: str | None = None,
               lst: BuildList = HOME) -> bool:
    """Set the open tab's filter menu to exactly this: ``resource`` a
    T.FILTER_GOLD/ELIXIR/DARK, ``kind`` a type button (T.FILTER_HERO, ...),
    ``sort`` T.SORT_COST_MAX or T.SORT_COST_MIN; ``None`` turns that group
    off. Every other button is turned off. False if the tab has no filter,
    or a wanted button isn't on it or can't be picked (grey: nothing of
    that type to upgrade)."""
    if not game.has(T.LIST_FILTER):
        return False
    game.refresh()
    button = game.find(T.LIST_FILTER)
    if button is None:
        return False
    game.tap_match(button)
    game.wait(1.0)
    # The menu opens below the button, left of the list (and below the
    # resource bar, whose icons look like the menu's).
    top = button.y + button.h
    menu = Rect(0, top, button.x + button.w, game.size[1] - top)
    ok = True
    for group, wanted in ((tuple(lst.resource_filter.values()), resource), (TYPE_BUTTONS, kind),
                          (SORT_BUTTONS, sort)):
        game.refresh()
        for name in group:
            m = game.has(name) and name != wanted and game.find(name, region=menu)
            if m and _lit(game.screen, m):
                game.tap_match(m)  # a second tap turns it off
                game.wait(0.5)
                game.refresh()
        if wanted is None:
            continue
        m = game.has(wanted) and game.find(wanted, region=menu)
        if not m or _grey(game.screen, m):
            ok = False
            continue
        if not _lit(game.screen, m):
            game.tap_match(m)
            game.wait(0.5)
            game.refresh()
            m = game.find(wanted, region=menu)
            ok &= bool(m) and _lit(game.screen, m)
    game.tap_match(button)  # closes the menu
    game.wait(1.0)
    return ok


def open_tab(game: Game, tab: str, lst: BuildList = HOME) -> None:
    """Open ``tab``. A tap on a tab is sometimes missed, so it's tapped once
    more if the list's title didn't change (which costs a tap when the tab
    was open already)."""
    w, h = game.size
    title = (slice(20, 50), slice(w - 377, w - 77))
    for _ in range(2):
        before = game._current()[title].astype(np.int16)
        game.tap(w - lst.tabs[tab], h - lst.tab_from_bottom)
        game.wait(1.2)
        game.refresh()
        if float(np.abs(game.screen[title].astype(np.int16) - before).mean()) > 5:
            return


def busy(game: Game) -> bool:
    """Whether the open tab's first row is an upgrade under way (the
    laboratory's research, a pet's upgrade): its dark grey bar."""
    w, _ = game.size
    x0, x1, y0, y1 = BUSY_STRIP
    hsv = _median_hsv(game._current(), w - x0, y0, w - x1, y1)
    return (hsv is not None and BUSY_SATURATION[0] <= hsv[1] <= BUSY_SATURATION[1]
            and BUSY_VALUE[0] <= hsv[2] <= BUSY_VALUE[1])


def rows(game: Game, kinds: tuple[str, ...] = ("gold", "elixir", "dark"), lst: BuildList = HOME) -> list[Row]:
    """The priced rows on show, top to bottom (walls and rows whose price
    can't be read are left out; so are upgrades under way, which show a
    timer and no price)."""
    w, h = game.size
    screen = game._current()
    column = Rect(w - ICON_COLUMN, LIST_TOP, ICON_COLUMN, h - LIST_BOTTOM_FROM_BOTTOM - LIST_TOP)
    walls = [m.y for m in game.find_all(T.WALL_SUGGESTION)] if game.has(T.WALL_SUGGESTION) else []
    found = []
    for kind in kinds:
        if not game.has(lst.cost_icons[kind]):
            continue
        for m in game.find_all(lst.cost_icons[kind], region=column):
            if any(WALL_ROW_BAND[0] <= y - m.y <= WALL_ROW_BAND[1] for y in walls):
                continue
            price = game.vision.read_number(screen, Rect(m.x - round(PRICE_LEFT * m.h), m.y - m.h // 2 - 2,
                                                         round(PRICE_WIDTH * m.h), m.h + 4), T.LOOT_FONT)
            if price:
                found.append(Row(kind, price, m.x, m.y, m.h))
    return sorted(found, key=lambda r: r.y)


def first_affordable(game: Game, budget: dict[str, int], lst: BuildList = HOME) -> Row | None:
    """In a tab sorted Cost Max, scroll down to the first row that
    ``budget`` (resource -> amount) covers: the dearest upgrade it can pay
    for. ``None`` if the list ends without one."""
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    for _ in range(MAX_SORTED_SCROLLS):
        game.refresh()
        found = [r for r in rows(game, tuple(budget), lst) if r.price <= budget[r.kind]]
        if found:
            return found[0]
        before = game.screen
        game.swipe(x, y, x, y - LIST_PAGE, duration_ms=SLOW_SWIPE_MS)
        game.wait(0.8)
        game.refresh()
        if float(np.abs(game.screen.astype(np.int16) - before.astype(np.int16)).mean()) < 1:
            return None  # the list's end
    return None


def green_confirm(game: Game):
    """The upgrade window's Confirm that pays in resources: green. (The grey
    one next to it spends a Hammer magic item: never pressed.)"""
    for m in game.find_all(T.UPGRADE_CONFIRM):
        hsv = _median_hsv(game.screen, m.x - 60, m.y + 20, m.x + 60, m.y + 45)
        if (hsv and GREEN_HUE[0] <= hsv[0] <= GREEN_HUE[1] and hsv[1] >= GREEN_SATURATION
                and hsv[2] >= GREEN_VALUE):
            return m
    return None


def pay_button(game: Game, kind: str) -> tuple[int, int] | None:
    """A builder base upgrade window's price button, if it pays in ``kind``
    (gold or elixir): the biggest wide bright green area, with that
    resource's icon in it."""
    hsv = cv2.cvtColor(game.screen, cv2.COLOR_BGR2HSV)
    count, _, stats, centres = cv2.connectedComponentsWithStats(cv2.inRange(hsv, *PAY_GREEN))
    best = None
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if w >= PAY_SIZE[0] and area >= PAY_SIZE[1] and w >= 2 * h and (best is None or area > best[0]):
            best = (area, x, y, w, h, centres[i])
    if best is None or kind not in PAY_ICON:
        return None
    _, x, y, w, h, (cx, cy) = best
    if np.count_nonzero(cv2.inRange(hsv[y:y + h, x:x + w], *PAY_ICON[kind])) < PAY_ICON_PIXELS:
        return None
    return round(cx), round(cy)


def close_windows(game: Game, list_close_y: int) -> None:
    """Close windows open over the list (an upgrade window left open, a
    popup offering to do it for gems) with their X, never their buttons.
    The list's own X, at ``list_close_y``, stays."""
    for _ in range(3):
        game.refresh()
        xs = [m for m in game.find_all(T.CLOSE) if m.y > list_close_y + 40]
        if not xs:
            return
        game.tap_match(max(xs, key=lambda m: m.score))
        game.wait(0.8)


def upgrade_row(game: Game, row: Row, lst: BuildList = HOME) -> bool:
    """Tap ``row`` and press its upgrade window's green Confirm (in the
    builder base: its price button), once. True if it was pressed (the
    caller checks the resource went down)."""
    game.refresh()
    list_close = game.find(T.CLOSE)
    list_y = list_close.y if list_close else 0
    w, _ = game.size
    confirm = None
    for _ in range(2):  # a tap just after the filter menu closes can be missed: once more if nothing opened
        game.tap(w - ROW_TAP_FROM_RIGHT, row.y - round(ROW_TAP_ABOVE * row.h))
        deadline = game.clock() + CONFIRM_TIMEOUT
        while confirm is None and game.clock() < deadline:
            game.wait(0.5)
            game.refresh()
            if lst.builder_base:
                confirm = pay_button(game, row.kind)
            else:
                m = green_confirm(game)
                confirm = (m.x, m.y) if m else None
        if confirm is not None or any(m.y > list_y + 40 for m in game.find_all(T.CLOSE)):
            break  # a window opened (to pay, or saying why not)
    if confirm is not None:
        game.tap(*confirm)
        game.wait(1.5)
    else:
        log.info("Upgrades: no upgrade window for that row (busy?)")
    # Pets keep their window open (with a gem "Finish Now" where Confirm
    # was); a busy laboratory offers its Goblin Researcher for gems.
    close_windows(game, list_y)
    return confirm is not None


def free_builders(game: Game) -> int | None:
    """Free builders, from the counter above the Build button ("3/5": 3)."""
    game.refresh()
    build = game.find(T.BUILD)
    if build is None:
        return None
    h = build.h
    x, y, width, height = BUILDERS
    return _read_free(game, Rect(build.x + round(x * h), build.y + round(y * h), round(width * h), round(height * h)))


def tab_free(game: Game, tab: str, lst: BuildList = HOME) -> int | None:
    """The free count under a tab's picture ("1/2": 1): builders under
    Buildings & Heroes, researchers under the builder base's Army Upgrades."""
    w, h = game.size
    half, top, height = TAB_COUNTER
    return _read_free(game, Rect(w - lst.tabs[tab] - half, h - top, 2 * half, height))


def _read_free(game: Game, region: Rect) -> int | None:
    font = game.vision.font(T.LOOT_FONT)
    digits = []
    for _, mask in pieces(game._current(), region):
        char, score = font.classify(mask)
        if score < SEPARATOR_SCORE or not char.isdigit():
            break  # the "/": the free count is before it
        digits.append(char)
    return int("".join(digits)) if digits else None
