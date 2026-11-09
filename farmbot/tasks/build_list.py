"""The Build button's list: Buildings & Heroes, Army Upgrades (the
laboratory), Hero Equipment and Hero Pets, one tab each along its bottom.
The builder base's has two tabs (Buildings & Heroes, and the Star
Laboratory's Army Upgrades), its own resource pictures and a plainer upgrade
window: see BUILDER.

Each row shows what an upgrade costs, a number with the resource's icon at
the right. The filter menu (the slider button left of the title) narrows a
tab to one resource and one type and sorts it by cost, so the rows left are
exactly the candidates for an upgrade. Tapping a row opens its upgrade window
(walls instead select a wall in the village: see village.py). A building not
built yet ("New" under its name) is instead put down on the map, at a free
spot the game picks, with a green tick (to build it there) and a red cross
over it; the list closes. A row that can't be upgraded yet opens "Upgrade
Locked" (the Town Hall, until the buildings it lists are built).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import cv2
import numpy as np

from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.text import pieces
from farmbot.vision import Match

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
LIST_PANEL_W = 360  # the list's panel, from the screen's right edge
LIST_STILL = 4  # mean change in the panel under this: it didn't scroll (a scroll: 40+;
                # its own shimmer, ~1; the village beside it animates, so it's left out)
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
# A price icon is found from this likeness (the Town Hall row's, under its green tag: 0.85; anything else
# in that column: 0.71 at most)
COST_THRESHOLD = 0.78
# A row's price, in icon heights left of its icon.
PRICE_LEFT, PRICE_WIDTH = 8.0, 7.4
ROW_TAP_FROM_RIGHT = 280  # where a row is tapped: on its name, clear of the price
ROW_TAP_ABOVE = 0.6  # ...this many icon heights above the price line
WALL_ROW_BAND = (-50, 0)  # a "Wall x" (or "Town Hall") text this far above a price is its row
FREE_TAP_ABOVE = 1.0  # a "FREE" row (a new wall) is tapped this many "FREE" heights above the word

# Putting a new building down: the red cross sits left of the green tick, at
# the same height, this many tick widths from it (middle to middle).
PLACE_GAP = (1.2, 3.5)
PLACE_ROW = 0.5  # ...and no more than this many tick heights above or below it
PLACE_GREEN = (35, 85), 100  # the tick's hue (from, to) and saturation at least: it can be built there
PLACE_TIMEOUT = 3
PLACE_THRESHOLD = 0.7  # the two together are found at a lower likeness than one alone (the map shows through)

# What tapping a row did (upgrade_row): the first two started something.
UPGRADED, PLACED, LOCKED, FAILED = "upgraded", "placed", "locked", ""


def started(outcome: str) -> bool:
    return outcome in (UPGRADED, PLACED)

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
# Before the laboratory (town hall 1-2) there's one counter, wider, centred
# over Build, its number beside the builder's face ("[face] 2/2"): from
# the face, at the same height. Its pieces before the number are skipped.
BUILDERS_ALONE = (-1.6, -6.8, 3.2, 1.2)
# Where a goblin shows over the counters, in Build text heights from its
# middle: the builders' left of it, the laboratory's right.
GOBLINS = (-4.0, 4.0, -9.0, -5.5)  # x from, to; y from, to
SEPARATOR_SCORE = 0.65  # the "/" reads as a poor digit
# A tab's own counter ("1/2": free of all) under its picture: x either side
# of the tab's middle, y from the bottom (top, height).
TAB_COUNTER = (45, 55, 30)
# The open tab is lighter than the others (V ~155 to ~114, ~100 if locked;
# a window over the list dims them all alike), in a strip beside its picture:
TAB_STRIP = (52, 42, 100, 20)  # x left of the tab's middle (from, to), y above the bottom (from, to)
TAB_LIGHTER = 15  # brighter than every other tab by at least this (seen: 21 dimmed, 41 or more)
TABS_ALIKE = 25  # ...which differ by less than this (seen: 14, a locked tab beside an open one)

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
    # The rows the bot leaves alone, by the words on them: walls (upgraded
    # on their own, see village.py), and the Town Hall when it's left to the
    # player (see ``leaving_town_hall``).
    left_out: tuple[str, ...] = (T.WALL_SUGGESTION,)

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(self.cost_icons)


HOME = BuildList(TABS, TAB_FROM_BOTTOM, RESOURCE_FILTER, COST_ICONS)


def leaving_town_hall(lst: BuildList) -> BuildList:
    """``lst``, with the Town Hall's row left alone too."""
    return replace(lst, left_out=(*lst.left_out, T.TOWN_HALL_ROW))
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


# The filter menu is waited on by what shows, not for set times (it opens
# in ~0.25 s, a button lights in ~0.25-0.6 s, it closes in ~0.25-0.5 s and
# the list is still ~0.4-0.7 s later); these are only the most it's given.
MENU_OPENS = 3.0
BUTTON_LIGHTS = 1.5
MENU_CLOSES = 2.0
LIST_SETTLES = 1.5
POLL = 0.15
EMPTY_FOR = 3600.0  # s a filter found empty (its button grey) is taken as still empty


def _until(game: Game, done, timeout: float) -> bool:
    """Look (a fresh screenshot each time) until ``done()``, at most ``timeout`` s."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        if done():
            return True
        if game.clock() >= deadline:
            return False
        game.wait(POLL, exact=True)


def _still(game: Game, area: Rect, timeout: float) -> None:
    """Until ``area`` stops moving (two looks alike), at most ``timeout`` s."""
    def part(screen):
        return screen[area.y:area.y + area.h, area.x:area.x + area.w].astype(np.int16)
    before = part(game._current())
    deadline = game.clock() + timeout
    while game.clock() < deadline:
        game.wait(POLL, exact=True)
        game.refresh()
        now = part(game.screen)
        if float(np.abs(now - before).mean()) < LIST_STILL:
            return
        before = now


def _settled(game: Game, timeout: float) -> None:
    """Until the list stops moving (two looks alike), at most ``timeout`` s."""
    before = game._current().copy()
    deadline = game.clock() + timeout
    while game.clock() < deadline:
        game.wait(POLL, exact=True)
        game.refresh()
        if list_still(before, game.screen):
            return
        before = game.screen.copy()


def forget_filters(game: Game) -> None:
    """Start a round of the list: what each tab's filter was set to isn't
    known (the game may have changed it since), nor which tab is open."""
    game.list_filters = {}
    game.list_tab = None


def _tab_key(game: Game, lst: BuildList):
    tab = getattr(game, "list_tab", None)
    return None if tab is None or tab[0] != lst.builder_base else tab


def set_filter(game: Game, resource: str | None = None, kind: str | None = None, sort: str | None = None,
               lst: BuildList = HOME) -> bool:
    """Set the open tab's filter, remembering (within a round, see
    ``forget_filters``) what each tab's is, so the same one isn't set twice,
    and (for an hour) a filter that's empty, so it isn't opened for again."""
    tab = _tab_key(game, lst)
    wanted = (resource, kind, sort)
    filters = getattr(game, "list_filters", None)
    empty = getattr(game, "empty_filters", None)
    if empty is None:
        empty = game.empty_filters = {}
    now = game.clock()
    for key in [k for k, until in empty.items() if until <= now]:
        del empty[key]
    if tab is not None and (tab, resource, kind) in empty:
        log.debug("Filter %s: empty a moment ago, not opened", wanted)
        return False
    if tab is not None and filters is not None and filters.get(tab) == wanted:
        return True  # already so
    ok, grey = _set_filter(game, resource, kind, sort, lst)
    if filters is not None and tab is not None:
        filters[tab] = wanted if ok else None  # (part set: not known)
    if grey and tab is not None:
        empty[(tab, resource, kind)] = now + EMPTY_FOR
    return ok


def _set_filter(game: Game, resource: str | None, kind: str | None, sort: str | None,
                lst: BuildList) -> tuple[bool, bool]:
    """Set the open tab's filter menu to exactly this: ``resource`` a
    T.FILTER_GOLD/ELIXIR/DARK, ``kind`` a type button (T.FILTER_HERO, ...),
    ``sort`` T.SORT_COST_MAX or T.SORT_COST_MIN; ``None`` turns that group
    off. Every other button is turned off. False if the tab has no filter,
    or a wanted button isn't on it or can't be picked (grey: nothing of
    that type to upgrade; also says whether one was grey)."""
    if not game.has(T.LIST_FILTER):
        return False, False
    game.refresh()
    button = game.find(T.LIST_FILTER)
    if button is None:
        return False, False
    # The menu opens below the button, left of the list (and below the
    # resource bar, whose icons look like the menu's).
    top = button.y + button.h
    menu = Rect(0, top, button.x + button.w, game.size[1] - top)
    sorts = [n for n in SORT_BUTTONS if game.has(n)]

    def shown() -> bool:
        return any(game.find(n, region=menu) for n in sorts)

    game.tap_match(button)
    if not sorts or not _until(game, shown, MENU_OPENS):
        game.wait(1.0)  # (no sort pictures to see it by: as long as it takes)
    else:
        _still(game, menu, MENU_OPENS)  # it slides open: its buttons found where they stop
    ok, grey = True, False
    for group, wanted in ((tuple(lst.resource_filter.values()), resource), (TYPE_BUTTONS, kind),
                          (SORT_BUTTONS, sort)):
        game.refresh()
        for name in group:
            m = game.has(name) and name != wanted and game.find(name, region=menu)
            if m and _lit(game.screen, m):
                game.tap_match(m)  # a second tap turns it off
                _until(game, lambda: not _lit(game.screen, m), BUTTON_LIGHTS)
        if wanted is None:
            continue
        m = game.has(wanted) and game.find(wanted, region=menu)
        if not m or _grey(game.screen, m):
            ok = False
            grey = grey or bool(m)
            continue
        if not _lit(game.screen, m):
            game.tap_match(m)
            lit = m
            _until(game, lambda: _lit(game.screen, lit), BUTTON_LIGHTS)
            m = game.find(wanted, region=menu)
            ok &= bool(m) and _lit(game.screen, m)
    game.tap_match(button)  # closes the menu
    if not sorts or not _until(game, lambda: not shown(), MENU_CLOSES):
        game.wait(1.0)
    _settled(game, LIST_SETTLES)  # the rows re-sort
    return ok, grey


def tab_open(game: Game, tab: str, lst: BuildList = HOME) -> bool | None:
    """Whether ``tab`` is the list's open tab, lighter than the others,
    which all look alike; None if that isn't what's seen (no list, say)."""
    w, h = game.size
    x0, x1, y0, y1 = TAB_STRIP
    light = {}
    for name, x in lst.tabs.items():
        hsv = _median_hsv(game._current(), w - x - x0, h - y0, w - x - x1, h - y1)
        if hsv is None:
            return None
        light[name] = hsv[2]
    brightest = max(light, key=light.get)
    rest = [v for n, v in light.items() if n != brightest]
    if light[brightest] - max(rest) < TAB_LIGHTER or max(rest) - min(rest) >= TABS_ALIKE:
        return None  # none lighter, or the others unalike: not the tabs
    return brightest == tab


def open_tab(game: Game, tab: str, lst: BuildList = HOME) -> None:
    """Open ``tab``, unless it's open already. A tap on a tab is sometimes
    missed, so it's tapped once more if the tab still isn't lit (or, when
    the tabs can't be told apart, if the list's title didn't change)."""
    game.list_tab = None  # (known again once it's open)
    game.refresh()
    if tab_open(game, tab, lst):
        game.list_tab = (lst.builder_base, tab)
        return
    w, h = game.size
    title = (slice(20, 50), slice(w - 377, w - 77))
    for _ in range(2):
        before = game._current()[title].astype(np.int16)
        game.tap(w - lst.tabs[tab], h - lst.tab_from_bottom)
        game.wait(1.2)
        game.refresh()
        lit = tab_open(game, tab, lst)
        if lit or (lit is None and float(np.abs(game.screen[title].astype(np.int16) - before).mean()) > 5):
            game.list_tab = (lst.builder_base, tab)
            return


def busy(game: Game) -> bool:
    """Whether the open tab's first row is an upgrade under way (the
    laboratory's research, a pet's upgrade): its dark grey bar."""
    w, _ = game.size
    x0, x1, y0, y1 = BUSY_STRIP
    hsv = _median_hsv(game._current(), w - x0, y0, w - x1, y1)
    return (hsv is not None and BUSY_SATURATION[0] <= hsv[1] <= BUSY_SATURATION[1]
            and BUSY_VALUE[0] <= hsv[2] <= BUSY_VALUE[1])


def _left_out(game: Game, lst: BuildList = HOME) -> list[int]:
    """Where the names of the rows ``lst`` leaves alone are."""
    return [m.y for name in lst.left_out if game.has(name) for m in game.find_all(name)]


def rows(game: Game, kinds: tuple[str, ...] = ("gold", "elixir", "dark"), lst: BuildList = HOME) -> list[Row]:
    """The priced rows on show, top to bottom (the rows ``lst`` leaves out
    and rows whose price can't be read are left out; so are upgrades under
    way, which show a timer and no price, and new walls, "FREE")."""
    w, h = game.size
    screen = game._current()
    column = Rect(w - ICON_COLUMN, LIST_TOP, ICON_COLUMN, h - LIST_BOTTOM_FROM_BOTTOM - LIST_TOP)
    walls = _left_out(game, lst)
    found = []
    for kind in kinds:
        if not game.has(lst.cost_icons[kind]):
            continue
        for m in game.find_all(lst.cost_icons[kind], threshold=COST_THRESHOLD, region=column):
            if any(WALL_ROW_BAND[0] <= y - m.y <= WALL_ROW_BAND[1] for y in walls):
                continue
            price = game.vision.read_number(screen, Rect(m.x - round(PRICE_LEFT * m.h), m.y - m.h // 2 - 2,
                                                         round(PRICE_WIDTH * m.h), m.h + 4), T.LOOT_FONT)
            if price:
                found.append(Row(kind, price, m.x, m.y, m.h))
    return sorted(found, key=lambda r: r.y)


# Under the list's last row is its plain dark brown background: when the
# band just above the tabs is that, the whole list is on show and a swipe
# can't bring more (rows: V p90 ~180, saturation ~109; the background: ~88,
# ~130; a list dimmed by a window over it: 104, 109, so it's still swiped).
END_BAND = (95, 5)  # y above the list's bottom (from, to)
END_BAND_X = (340, 20)  # x from the right edge (from, to)
END_BRIGHTEST = 120  # V's 90th percentile under this...
END_SATURATION = 120  # ...and saturation's median at least this
TOWN_HALL_SCROLLS = 3  # pages of a Cost Max list looked through for the Town Hall (it's among the dearest)


def town_hall_row(game: Game, kinds: tuple[str, ...] = ("gold",),
                  lst: BuildList = HOME) -> tuple[Row | None, bool]:
    """In a tab sorted Cost Max, the Town Hall's priced row (its upgrade),
    looking down the first few pages, and whether its name was seen. A row
    seen with no price read is one it can't pay for yet (the game draws
    that price red, which the digits reader can't read); not seen at all:
    upgrading already, or the top level."""
    if not game.has(T.TOWN_HALL_ROW):
        return None, False
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    everything = replace(lst, left_out=tuple(n for n in lst.left_out if n != T.TOWN_HALL_ROW))
    game.refresh()
    seen = False
    for _ in range(TOWN_HALL_SCROLLS):
        names = [m.y for m in game.find_all(T.TOWN_HALL_ROW)]
        seen = seen or bool(names)
        found = [r for r in rows(game, kinds, everything)
                 if any(WALL_ROW_BAND[0] <= n - r.y <= WALL_ROW_BAND[1] for n in names)]
        if found:
            return found[0], True
        if names:
            return None, True  # there, but its price isn't readable: red
        if list_ends_here(game):
            return None, seen  # all of it on show: nothing to scroll to
        before = game.screen
        game.swipe(x, y, x, y - LIST_PAGE, duration_ms=SLOW_SWIPE_MS)
        game.wait(0.8)
        game.refresh()
        if list_still(before, game.screen):
            return None, seen
    return None, seen


def list_ends_here(game: Game) -> bool:
    """Whether the list's last row is on show: below it, down to the tabs,
    the list's empty background. Then there's nothing to scroll to."""
    screen = game._current()
    h, w = screen.shape[:2]
    bottom = h - LIST_BOTTOM_FROM_BOTTOM
    band = screen[bottom - END_BAND[0]:bottom - END_BAND[1], w - END_BAND_X[0]:w - END_BAND_X[1]]
    if band.size == 0:
        return False
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    return (float(np.percentile(hsv[..., 2], 90)) < END_BRIGHTEST
            and float(np.median(hsv[..., 1])) >= END_SATURATION)


def list_still(before: np.ndarray | None, after: np.ndarray | None) -> bool:
    """Whether the list's panel looks the same in both screenshots: a swipe
    didn't move it (its end). Only the panel is compared: the village
    beside it never stops moving."""
    if before is None or after is None or before.shape != after.shape:
        return False
    h, w = after.shape[:2]
    area = (slice(LIST_TOP, h - LIST_BOTTOM_FROM_BOTTOM), slice(max(w - LIST_PANEL_W, 0), w))
    return float(np.abs(after[area].astype(np.int16) - before[area].astype(np.int16)).mean()) < LIST_STILL


def first_affordable(game: Game, budget: dict[str, int], lst: BuildList = HOME,
                     skip: set[tuple[str, int]] | None = None) -> Row | None:
    """In a tab sorted Cost Max, scroll down to the first row that
    ``budget`` (resource -> amount) covers: the dearest upgrade it can pay
    for, passing over rows priced as in ``skip`` (resource, price): ones
    already tried that wouldn't upgrade. ``None`` if the list ends without one."""
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    game.refresh()
    for _ in range(MAX_SORTED_SCROLLS):
        found = [r for r in rows(game, tuple(budget), lst)
                 if r.price <= budget[r.kind] and (r.kind, r.price) not in (skip or ())]
        if found:
            return found[0]
        if list_ends_here(game):
            return None  # all of it on show: nothing to scroll to
        before = game.screen
        game.swipe(x, y, x, y - LIST_PAGE, duration_ms=SLOW_SWIPE_MS)
        game.wait(0.8)
        game.refresh()  # (looked at for rows on the next turn)
        if list_still(before, game.screen):
            return None  # the list's end
    return None


RED_PRICE = ((8, 172), 120, 150)  # hue at most / at least, saturation, value: a price drawn red
WHITE_PRICE = (50, 200)  # saturation at most, value at least: a price drawn white
PRICE_PIXELS = 200  # of the one colour, the other next to none (a dimmed list has neither)


def top_row_affordable(game: Game, kinds: tuple[str, ...], lst: BuildList = HOME) -> bool | None:
    """In a tab sorted Cost Min: whether its top priced row, the cheapest
    upgrade, can be paid for. The game draws a price it can't pay red
    (which the digits reader can't read) and one it can white. ``None``
    if there's no such row on show or its colour can't be told."""
    game.refresh()
    w, h = game.size
    column = Rect(w - ICON_COLUMN, LIST_TOP, ICON_COLUMN, h - LIST_BOTTOM_FROM_BOTTOM - LIST_TOP)
    walls = _left_out(game, lst)
    icons = [m for kind in kinds if game.has(lst.cost_icons[kind])
             for m in game.find_all(lst.cost_icons[kind], threshold=COST_THRESHOLD, region=column)
             if not any(WALL_ROW_BAND[0] <= y - m.y <= WALL_ROW_BAND[1] for y in walls)]
    if not icons:
        return None
    m = min(icons, key=lambda i: i.y)
    x0, y0 = m.x - round(PRICE_LEFT * m.h), m.y - m.h // 2 - 2
    area = cv2.cvtColor(game.screen[max(y0, 0):y0 + m.h + 4, max(x0, 0):x0 + round(PRICE_WIDTH * m.h)],
                        cv2.COLOR_BGR2HSV)
    (hue_low, hue_high), sat, val = RED_PRICE
    red = int(np.count_nonzero(((area[..., 0] <= hue_low) | (area[..., 0] >= hue_high))
                               & (area[..., 1] >= sat) & (area[..., 2] >= val)))
    white = int(np.count_nonzero((area[..., 1] <= WHITE_PRICE[0]) & (area[..., 2] >= WHITE_PRICE[1])))
    if red >= PRICE_PIXELS and white < PRICE_PIXELS // 4:
        return False
    if white >= PRICE_PIXELS and red < PRICE_PIXELS // 4:
        return True
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


GEM_SCALES = (0.5, 0.6, 0.7, 0.85, 1.0, 1.2)  # gem icons in windows are drawn at many sizes
GEM_LIKENESS = 0.8
POPUP_AREA = (150, 1000)  # x range of a centred window: clear of the resource bar's own gem
GEM_BELOW_BUTTON = 80  # an upgrade window's price is this far below its button's label (home) or middle
# And no further above it: higher up, with Buildings & Heroes open, is the
# resource bar's own gem count (moved left, into the window's x range).
GEM_ABOVE_BUTTON = 250


def shows_gems(screen, top: int, bottom: int, gem) -> bool:
    """True if a gem icon is anywhere in the window between ``top`` and
    ``bottom`` (bot px)."""
    area = screen[max(top, 0):bottom, POPUP_AREA[0]:POPUP_AREA[1]]
    for scale in GEM_SCALES:
        icon = cv2.resize(gem, None, fx=scale, fy=scale)
        if area.shape[0] < icon.shape[0] or area.shape[1] < icon.shape[1]:
            continue
        if cv2.matchTemplate(area, icon, cv2.TM_CCOEFF_NORMED).max() >= GEM_LIKENESS:
            return True
    return False


def asks_for_gems(game: Game, button_y: int) -> bool:
    """True if the upgrade window whose pay button is at ``button_y`` shows
    a gem near that button (or gems can't be looked for)."""
    if not game.has(T.GEM):
        return True  # can't check, so don't risk it
    [(_, gem)] = game.vision.library.get(T.GEM)[:1]
    return shows_gems(game.screen, max(120, button_y - GEM_ABOVE_BUTTON), button_y + GEM_BELOW_BUTTON, gem)


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


def upgrade_row(game: Game, row: Row, lst: BuildList = HOME) -> str:
    """Tap ``row`` and press its upgrade window's green Confirm (in the
    builder base: its price button), once; or, for a building not built
    yet, build it where the game puts it. UPGRADED or PLACED if that was
    pressed (the caller checks the resource went down; after PLACED the
    list has closed), LOCKED if the game says it can't be upgraded yet,
    FAILED otherwise. Nothing is left open over the list, or being put down."""
    game.refresh()
    list_close = game.find(T.CLOSE)
    list_y = list_close.y if list_close else 0
    w, _ = game.size
    confirm = None
    locked = False
    for _ in range(2):  # a tap just after the filter menu closes can be missed: once more if nothing opened
        game.tap(w - ROW_TAP_FROM_RIGHT, row.y - round(ROW_TAP_ABOVE * row.h))
        deadline = game.clock() + CONFIRM_TIMEOUT
        while confirm is None and game.clock() < deadline:
            game.wait(0.5)
            game.refresh()
            if placing(game):
                return PLACED if place(game) else FAILED
            if game.has(T.UPGRADE_LOCKED) and game.find(T.UPGRADE_LOCKED):
                locked = True
                break
            if lst.builder_base:
                confirm = pay_button(game, row.kind)
            else:
                m = green_confirm(game)
                confirm = (m.x, m.y) if m else None
        if locked or confirm is not None or any(m.y > list_y + 40 for m in game.find_all(T.CLOSE)):
            break  # a window opened (to pay, or saying why not)
    if confirm is not None and asks_for_gems(game, confirm[1]):
        # Some events make upgrades and research cost gems too: never paid.
        log.warning("Upgrades: that upgrade asks for gems; leaving it alone")
        confirm = None
    if confirm is not None:
        game.tap(*confirm)
        game.wait(1.5)
    elif locked:
        log.info("Upgrades: that upgrade is locked until other buildings are built; on to the next")
    else:
        log.info("Upgrades: no upgrade window for that row (busy?)")
    # Pets keep their window open (with a gem "Finish Now" where Confirm
    # was); a busy laboratory offers its Goblin Researcher for gems.
    close_windows(game, list_y)
    return UPGRADED if confirm is not None else LOCKED if locked else FAILED


def placing(game: Game) -> tuple[Match, Match] | None:
    """The green tick and red cross over a building being put down, if one
    is: the tick with the cross just left of it, at the same height (either
    alone could be something else on the map)."""
    if not (game.has(T.PLACE_OK) and game.has(T.PLACE_CANCEL)):
        return None
    crosses = game.find_all(T.PLACE_CANCEL, threshold=PLACE_THRESHOLD)
    for tick in game.find_all(T.PLACE_OK, threshold=PLACE_THRESHOLD):
        for cross in crosses:
            gap = (tick.x - cross.x) / max(tick.w, 1)
            if PLACE_GAP[0] <= gap <= PLACE_GAP[1] and abs(tick.y - cross.y) <= PLACE_ROW * tick.h:
                return tick, cross
    return None


def _green(screen: np.ndarray, m: Match) -> bool:
    hsv = _median_hsv(screen, m.x - m.w // 2, m.y - m.h // 2, m.x + m.w // 2, m.y + m.h // 2)
    (low, high), saturation = PLACE_GREEN
    return hsv is not None and low <= hsv[0] <= high and hsv[1] >= saturation


def place(game: Game) -> bool:
    """Build the building being put down where the game put it (its tick),
    if it may go there; otherwise take it away again (its cross). True if
    it was built (the tick and cross went)."""
    deadline = game.clock() + PLACE_TIMEOUT
    pair = placing(game)
    while pair is None and game.clock() < deadline:
        game.wait(0.5)
        game.refresh()
        pair = placing(game)
    if pair is None:
        return False
    tick, cross = pair
    if not _green(game.screen, tick):
        log.info("Upgrades: no room for that new building; leaving it for now")
        game.tap_match(cross)
        game.wait(1.0)
        return False
    game.tap_match(tick)
    game.wait(1.5)
    game.refresh()
    window = game.find_all(T.CLOSE)
    if not window and placing(game) is None:
        return True
    # A window over it (every builder busy, say: it offers to pay gems) is
    # closed with its X, and the building taken away.
    log.info("Upgrades: the new building wasn't built%s; taking it away again",
             " (the game asked for something first)" if window else "")
    for _ in range(2):
        if not window:
            break
        game.tap_match(max(window, key=lambda m: m.score))
        game.wait(0.8)
        game.refresh()
        window = game.find_all(T.CLOSE)
    again = placing(game)
    if again:
        game.tap_match(again[1])
        game.wait(1.0)
    return False


def list_shown(game: Game) -> bool:
    """Whether the list is open: its own X, top right (a window's X sits lower)."""
    return any(m.y < LIST_TOP for m in game.find_all(T.CLOSE))


def free_rows(game: Game, lst: BuildList = HOME) -> list[Match]:
    """The rows on show that cost nothing ("FREE": new walls, and some new
    buildings), top to bottom."""
    if not game.has(T.FREE_ROW):
        return []
    w, h = game.size
    column = Rect(w - LIST_PANEL_W, LIST_TOP, LIST_PANEL_W, h - LIST_BOTTOM_FROM_BOTTOM - LIST_TOP)
    return sorted(game.find_all(T.FREE_ROW, region=column), key=lambda m: m.y)


def place_free(game: Game, m: Match) -> bool:
    """Tap the "FREE" row whose word is ``m`` and build it where the game
    puts it. True if it was built (the list has closed either way, if it
    was put down)."""
    w, _ = game.size
    game.tap(w - ROW_TAP_FROM_RIGHT, m.y - round(FREE_TAP_ABOVE * m.h))
    game.wait(1.0)
    game.refresh()
    return place(game)


def free_builders(game: Game) -> int | None:
    """Free builders, from the counter above the Build button ("3/5": 3).
    With every builder busy the game offers its Goblin Builder (a goblin over
    the counter, "1/7"), who costs gems: 0."""
    if not game.has(T.BUILD):
        return None
    game.refresh()
    build = game.find(T.BUILD)
    if build is None:
        return None
    if "builder" in goblins(game, build):
        return 0
    h = build.h

    def at(box: tuple[float, float, float, float]) -> Rect:
        x, y, width, height = box
        return Rect(build.x + round(x * h), build.y + round(y * h), round(width * h), round(height * h))

    free = _read_free(game, at(BUILDERS))
    if free is None:
        # Nothing where the builders' counter is when there are two: one
        # counter only, the number beside the face.
        free = _read_free(game, at(BUILDERS_ALONE), lead=True)
    return free


def goblins(game: Game, build: Match | None = None) -> set[str]:
    """The counters above the Build button that show a goblin ("builder",
    "lab"): the Goblin Builder or Goblin Researcher, offered for gems once
    the real ones are busy."""
    if not game.has(T.GOBLIN):
        return set()
    build = build or game.find(T.BUILD)
    if build is None:
        return set()
    h = build.h
    x0, x1, y0, y1 = GOBLINS
    found = set()
    for goblin in game.vision.find_all(game._current(), T.GOBLIN, region=Rect(
            build.x + round(x0 * h), build.y + round(y0 * h), round((x1 - x0) * h), round((y1 - y0) * h))):
        found.add("builder" if goblin.x < build.x else "lab")
    return found


def tab_free(game: Game, tab: str, lst: BuildList = HOME) -> int | None:
    """The free count under a tab's picture ("1/2": 1): builders under
    Buildings & Heroes, researchers under the builder base's Army Upgrades."""
    w, h = game.size
    half, top, height = TAB_COUNTER
    return _read_free(game, Rect(w - lst.tabs[tab] - half, h - top, 2 * half, height))


def _read_free(game: Game, region: Rect, lead: bool = False) -> int | None:
    """The number before the "/" in ``region``; with ``lead``, what comes
    before the number (the builder's face) is skipped, and the "/" must
    follow it."""
    font = game.vision.font(T.LOOT_FONT)
    digits = []
    for _, mask in pieces(game._current(), region):
        char, score = font.classify(mask)
        if score < SEPARATOR_SCORE or not char.isdigit():
            if lead and not digits:
                continue  # (the face)
            if lead:
                return int("".join(digits))  # the "/" (or what ends the number)
            break  # the "/": the free count is before it
        digits.append(char)
    if lead:
        return None  # no "/" after it: not the counter
    return int("".join(digits)) if digits else None
