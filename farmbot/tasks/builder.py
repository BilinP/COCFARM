"""Builder base: sail over, attack, and see both stages of the battle through.

A builder battle has two bases. Taking the first to 100% moves the attack on
to a second one, with a few more troops. Each stage is played the same way:

1. The hero goes down at the map corner farthest from the base.
2. ``troop_delay`` seconds later every troop follows at the same corner,
   like a player would: select the first card, then tap the map once per
   troop with no pause (the game moves on to the next card by itself).
3. Troop abilities fire in batches (``ability_batch`` cards at a time,
   ``ability_delay`` seconds apart).
4. The hero's ability fires once all three charge bars over its card are
   full, again each time they fill back up (firing with less only shows a
   "wait" message; a hero about to die uses it by itself). Where the bars
   can't be seen, it fires once, ``hero_ability_after`` seconds after the
   hero landed.

The band across the top of each card says what is going on: purple = ready
to deploy, green = on the field, grey = dead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable

import cv2
import numpy as np

from farmbot import human, text
from farmbot.config import BuilderConfig
from farmbot.geometry import Rect
from farmbot.tasks.search import Loot
from farmbot.game import VIEW_AREA, VIEW_NEAR, VIEW_STILL, Game, T, view_change, view_shift
from farmbot.tasks.build_list import asks_for_gems
from farmbot.tasks import village
from farmbot.vision import Match

log = logging.getLogger(__name__)

# Map corners of the zoomed-out builder battle (bot coordinates). The bottom
# corner is under the troop bar, so it isn't used.
CORNERS = {"left": (200, 468), "top": (595, 165), "right": (995, 455)}
MAP = np.array([(170, 468), (595, 140), (1020, 455), (600, 770)])
ISO_RATIO = 0.75
MAX_STAGES = 3  # two in practice; one more in case of a surprise
# The elixir cart sits by the dock (zoomed-out builder base, bot coordinates);
# its tag looks just like an elixir collector's bubble.
CART_AREA = (690, 90, 830, 200)  # x0, y0, x1, y1
BUBBLE_THRESHOLD = 0.8
BUBBLE_STAYED = 12  # px: a bubble this close to the one just tapped is the same one
CARD_DETAIL = 25.0  # grey-level spread of a card's picture (cards ~50, empty slots under 10)
PLACE_STEPS = 6  # how often a refused drop point is moved towards the base
PLACE_STEP = 0.15  # share of the way to the base each move covers
BATTLE_AREA = [0, -200, 220, 200]  # [x, y, w, h]: the Battle button, bottom left


class BuilderError(RuntimeError):
    pass


class DeploymentCountError(BuilderError):
    """A deployment count is unknown: finish the battle, then stop without retry."""


# ------------------------------------------------------------------ getting there
def in_builder_base(game: Game, refresh: bool = True) -> bool:
    if not game.has(T.BB_BATTLE):
        return False
    if refresh:
        game.refresh()
    return game.find(T.BB_BATTLE, region=game.region(BATTLE_AREA)) is not None


BOAT_SIZES = (0.9, 1.0, 1.1, 1.2, 1.3)  # the village isn't always zoomed all the way out
BOAT_THRESHOLD = 0.85
BOAT_DRAG = (500, 250, 400, 600, 600)  # x1, y1, x2, y2, ms: pulls the builder base's dock down into view
# The map holds two bases; the second lies up and left of the first (whose
# dock the boat home is at). Upgrading something on the second leaves the
# camera there: drag the map up and left (the view goes down and right)
# until it stops moving, at the first base, then pull the dock into view.
# Drags stay clear of the buttons a selected building shows at the bottom.
FIRST_BASE_DRAG = (725, 580, 425, 280, 700)
# The home village's view can settle low, the boat (bottom left) behind the
# Battle button: drag the map up and right, the boat with it.
HOME_BOAT_DRAG = (600, 450, 750, 250, 600)  # x1, y1, x2, y2, ms
HOME_BOAT_DRAGS = 3
BATTLE_BUTTON = (3.0, 12.0, 1.5)  # around the Battle label, in its heights (either side, above, below): the
# button and the margin its touch reaches past it (a boat 45 px above it opened the battle menu)
FIRST_BASE_DRAGS = 8


def _find_scaled(game: Game, name: str, threshold: float = BOAT_THRESHOLD,
                 below: float = 0.0) -> tuple[int, int] | None:
    """Centre of ``name`` on screen at whatever size it shows, or ``None``.
    ``below``: the point this many of its heights under its centre instead."""
    game.refresh()
    match = game.find(name) if not below else None
    if match is not None:
        return match.x, match.y
    found = _sized_match(game, name, BOAT_SIZES, threshold)
    return None if found is None else (found.x, found.y + round(below * found.h))


def _sized_match(game: Game, name: str, sizes: tuple[float, ...], threshold: float):
    """The best match of ``name`` on the current screenshot at whichever of
    ``sizes`` it shows, or ``None``. Each picture is tried first at the
    size it last matched at, alone (one look instead of one per size, and
    no screen-wide scoring); at every size when it isn't found there."""
    from farmbot.vision import match_template, _resized

    known = memory(game).sizes
    for tname, image in game.vision.library.get(name):
        last = known.get(tname)
        if last:
            found = match_template(game.screen, _resized(image, last), threshold)
            if found:
                return found[0]
        # Every size, each looked at coarse to fine (a fraction of scoring the whole screen at each).
        best = None
        for scale in sizes:
            found = match_template(game.screen, _resized(image, scale), threshold)
            if found and (best is None or found[0].score > best[1].score):
                best = scale, found[0]
        if best is not None:
            known[tname] = best[0]
            return best[1]
    return None


# A scenery can draw its own boat (a ghost ship, say), somewhere else on the
# shore; the game then pins a small picture of the usual boat above it. The
# pin is small and drawn over the scenery, so it matches less exactly than
# the boat does (0.81 live, the next best spot 0.62).
MARKER_THRESHOLD = 0.72
MARKER_TO_BOAT = 1.7  # the boat's middle, in marker heights under the marker's


def _marked_boat(game: Game) -> tuple[int, int] | None:
    """The boat under the scenery's boat marker, or ``None``."""
    if not game.has(T.BB_BOAT_MARKER):
        return None
    return _find_scaled(game, T.BB_BOAT_MARKER, MARKER_THRESHOLD, MARKER_TO_BOAT)


def go_to_builder_base(game: Game) -> bool:
    """Sail from the home village to the builder base. True once there."""
    if in_builder_base(game):
        return True
    memory(game).on_second = False  # the boat lands on the first base
    game.zoom_out()
    boat = _home_boat(game)
    if boat is None:
        raise BuilderError("boat to the builder base not visible; is the home village showing?")
    game.tap(*boat)
    if not game.wait_for(T.BB_BATTLE, timeout=20):
        raise BuilderError("builder base didn't load")
    log.info("Sailed to the builder base")
    return True


def _home_boat(game: Game) -> tuple[int, int] | None:
    """The home village's boat, clear of the Battle button: the view can
    settle low, with the boat behind the button (a tap there opens the
    battle menu) or off the screen. Then the view is dragged up and right
    and looked at again."""
    for look in range(HOME_BOAT_DRAGS + 1):
        boat = _find_scaled(game, T.BB_HOME_BOAT) or _marked_boat(game)
        if boat is not None and not _behind_battle(game, boat):
            return boat
        if look == HOME_BOAT_DRAGS:
            break
        log.info("The boat is %s; dragging the view", "behind the Battle button" if boat else "out of sight")
        game.swipe(*HOME_BOAT_DRAG)
        game.wait(1)
    return None


def _behind_battle(game: Game, point: tuple[int, int]) -> bool:
    if not game.has(T.BATTLE):
        return False
    label = game.find(T.BATTLE)  # the button's "Battle" label, at its bottom
    if label is None:
        return False
    side, above, below = (round(k * label.h) for k in BATTLE_BUTTON)
    return (abs(point[0] - label.x) <= label.w // 2 + side
            and label.y - above <= point[1] <= label.y + below)


def _boat_at_first_base(game: Game) -> tuple[int, int] | None:
    """Drag the view to the first base (see FIRST_BASE_DRAG), looking for
    the boat home on the way, then pull its dock into view."""
    before = game.refresh().copy()
    for _ in range(FIRST_BASE_DRAGS):
        game.swipe(*FIRST_BASE_DRAG)
        game.wait(1)
        boat = _find_scaled(game, T.BB_BUILDER_BOAT)
        if boat is not None:
            return boat
        if view_change(before, game.screen) < VIEW_STILL:
            break  # at the map's edge: the first base
        before = game.screen.copy()
    game.swipe(*BOAT_DRAG)
    game.wait(1)
    return _find_scaled(game, T.BB_BUILDER_BOAT)


def go_to_home_village(game: Game) -> bool:
    """Sail back from the builder base. True if it did."""
    if not in_builder_base(game):
        return False
    game.zoom_out()
    boat = _find_scaled(game, T.BB_BUILDER_BOAT)
    if boat is None:
        # Fresh from loading, the dock sits under the resource bars (top
        # right): drag the map down to bring it into view.
        game.swipe(*BOAT_DRAG)
        game.wait(1)
        boat = _find_scaled(game, T.BB_BUILDER_BOAT)
    if boat is None:
        log.info("Boat home not in view: looking for it at the first base")
        boat = _boat_at_first_base(game)
    for tries in range(2):
        if boat is None:
            log.info("Boat to the home village not visible")
            return False
        game.wait(0.5)  # (found while the view still glided from a drag: found again where it stopped)
        boat = _find_scaled(game, T.BB_BUILDER_BOAT) or boat
        game.tap(*boat)
        if _home_loaded(game, HOME_LOADS):
            log.info("Sailed back to the home village")
            return True
        if not in_builder_base(game) or tries:
            break
        # Still here: the tap missed the boat (it took it for the dock, or the view moved).
        log.info("Boat home tapped, but still in the builder base: looking for it again")
        boat = _find_scaled(game, T.BB_BUILDER_BOAT)
    log.info("Home village didn't load")
    return False


HOME_LOADS = 20.0  # s at most from the boat tapped to the home village shown


def _home_loaded(game: Game, timeout: float) -> bool:
    """The home village is shown: its Battle button, and not the builder
    base's (the two look alike: the home one's picture is found on the
    builder base's too)."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        if not in_builder_base(game, refresh=False) and game.find(T.BATTLE) is not None:
            return True
        if game.clock() >= deadline:
            return False
        game.wait(0.5)


# Putting the camera back where a picture of the view was taken (upgrading
# moves it onto what was upgraded, and the elixir cart is looked for at a
# fixed spot). The builder base's map holds two bases, so there's no
# middle to centre on as at home: the two pictures are matched instead,
# by features (they only need to overlap in part).
VIEW_STEP = 350  # px per drag at most
# Shorter drags read as taps in the game: they select (and a drag starting on
# a selected building moves it). A smaller move goes out and back.
VIEW_LEAST_DRAG = 80
VIEW_MS = 900
VIEW_TRIES = 9
# No overlap: drag the map one way and look again, then back past where it
# started the other way (where it was 0, -300, 0, +300, 0, ... drags away).
VIEW_SEARCH = [(-300, -300), (300, 300), (300, 300), (-300, -300)]


def restore_view(game: Game, reference: np.ndarray) -> bool:
    """Drag the map back to where it was in ``reference`` (a zoomed-out
    screen). True once it's there. When the views don't overlap (the
    camera went to the second base, say), it goes to the first base, which
    the builder base opens on, and looks again from there."""
    if _restore_view(game, reference):
        return True
    log.info("Builder base view: looking for it again from the first base")
    _to_first_base(game)
    if _restore_view(game, reference):
        return True
    log.info("Couldn't put the builder base view back")
    return False


def _to_first_base(game: Game) -> None:
    """Drag the view down and right until it stops: the first base."""
    before = game.refresh().copy()
    for _ in range(FIRST_BASE_DRAGS):
        game.swipe(*FIRST_BASE_DRAG)
        game.wait(1)
        if view_change(before, game.refresh()) < VIEW_STILL:
            break
        before = game.screen.copy()


def _restore_view(game: Game, reference: np.ndarray) -> bool:
    cx, cy = (VIEW_AREA[0] + VIEW_AREA[2]) // 2, (VIEW_AREA[1] + VIEW_AREA[3]) // 2
    searched = 0
    for _ in range(VIEW_TRIES):
        shift = view_shift(reference, game.refresh())
        log.debug("Builder base view: %s from where it was", shift)
        if shift is None and _close_window(game):
            continue  # it hid the map
        if shift is None:
            dx, dy = VIEW_SEARCH[searched % len(VIEW_SEARCH)]
            searched += 1
        else:
            if max(map(abs, shift)) <= VIEW_NEAR:
                return True
            dx, dy = (int(np.clip(-v, -VIEW_STEP, VIEW_STEP)) for v in shift)
        _drag(game, cx, cy, dx, dy)
    return False


def _drag(game: Game, cx: int, cy: int, dx: int, dy: int) -> None:
    """Move the map by (dx, dy) with drags around (cx, cy), none short
    enough to count as a tap."""
    moves = [(dx, dy)]
    if np.hypot(dx, dy) < VIEW_LEAST_DRAG:
        moves = [(dx + VIEW_LEAST_DRAG, dy), (-VIEW_LEAST_DRAG, 0)]
    for mx, my in moves:
        game.swipe(cx - mx // 2, cy - my // 2, cx + mx // 2, cy + my // 2, VIEW_MS)
        game.wait(0.8)


# ------------------------------------------------------------------ collecting
# A full collector (or the full elixir cart) wears a red tag with its
# resource's icon on it, gold coin or elixir drop, rather than the usual
# bubble: found by its colours, as gold collectors' coins vary.
TAG_RED = ((3, 150, 120), (12, 255, 230))  # HSV
TAG_SIZE = (18, 48)  # px, either side
TAG_FILL = (0.25, 0.8)  # share of the tag's box that's red (the icon fills the rest)
TAG_ICON = {"gold": ((18, 120, 150), (35, 255, 255)), "elixir": ((125, 100, 100), (155, 255, 255))}
TAG_ICON_SHARE = 0.3  # of the middle of the tag


def full_tags(screen: np.ndarray) -> list[tuple[str, int, int]]:
    """``(kind, x, y)`` of each red "full" tag on the builder base, its
    icon's resource as ``kind``."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, *TAG_RED)
    count, _, stats, _ = cv2.connectedComponentsWithStats(red)
    tags = []
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if not (TAG_SIZE[0] <= w <= TAG_SIZE[1] and TAG_SIZE[0] <= h <= TAG_SIZE[1] and 0.7 <= w / h <= 1.5):
            continue
        if not TAG_FILL[0] <= area / (w * h) <= TAG_FILL[1]:
            continue
        middle = hsv[y + h // 4:y + 3 * h // 4, x + w // 4:x + 3 * w // 4]
        for kind, (low, high) in TAG_ICON.items():
            if np.count_nonzero(cv2.inRange(middle, low, high)) >= TAG_ICON_SHARE * middle.shape[0] * middle.shape[1]:
                tags.append((kind, x + w // 2, y + h // 2))
                break
    return tags


def _in_cart_area(x: int, y: int) -> bool:
    x0, y0, x1, y1 = CART_AREA
    return x0 <= x <= x1 and y0 <= y <= y1


def collect_resources(game: Game, tries: int = 4, second_base: bool = True,
                      kinds: tuple[str, ...] = ("gold", "elixir", "gems"), restore: bool = True) -> list[str]:
    """Empty the builder base's full gold mines, elixir collectors and gem
    mine (of ``kinds``: leave out a full storage's, whose collectors can't
    be emptied) by tapping their bubbles (never the elixir cart's tag), on
    the first base and then, with ``second_base``, the second one, after
    which (with ``restore``) the view is put back. Returns what was collected."""
    game.zoom_out()
    view = game.refresh().copy()
    collected = _collect_in_view(game, tries, kinds, view)
    mem = memory(game)
    on_second = tuple(k for k in kinds if k != "gems")
    if (second_base and on_second and game.clock() < mem.second_rest_until
            and set(on_second) <= mem.second_rest_kinds):
        log.debug("Builder base: the second base had nothing to collect lately; not going there yet")
    elif second_base and {"gold", "elixir"} & set(kinds):  # the gem mine is on the first base
        screen = _to_second_base(game)
        there = _collect_in_view(game, tries, on_second, screen)
        collected += [k for k in there if k not in collected]
        # Nothing there: its collectors were emptied lately, so the trip
        # (a few drags each way) waits a while.
        mem.second_rest_until = 0.0 if there else game.clock() + SECOND_BASE_REST
        mem.second_rest_kinds = set() if there else set(on_second)
        if not there:
            log.info("Builder base: nothing to collect on the second base; back in %d min",
                     SECOND_BASE_REST // 60)
        if restore:
            restore_view(game, view)
        else:
            mem.on_second = True
        # Otherwise left on the second base: what comes next goes by the
        # Build list (a row tapped takes the camera to its building, wherever
        # the view) or the Battle button, and the boat home is found from there.
    if collected:
        log.info("Collected builder base %s", ", ".join(collected))
    return collected


SECOND_BASE_REST = 1800.0  # s the second base isn't visited after a visit that collected nothing


@dataclass
class Memory:
    """What the builder base looked like last time, kept on the game for
    the session: the layout rarely changes, so it saves looking and
    dragging. Everything here is checked against what's on screen before
    it's used, and forgotten (looked for afresh) when it doesn't fit."""
    second_drags: int = 0  # drags from the first base to the second's edge (0: not known)
    second_view: np.ndarray | None = None  # the screen at the second base's edge
    # The elixir cart's tag: where it was on the screen kept with it (its
    # tag looks like an elixir bubble, and out of its usual spot it's
    # told apart only by tapping it, which opens its window).
    cart_view: np.ndarray | None = None
    cart: tuple[int, int] | None = None
    sizes: dict[str, float] = field(default_factory=dict)  # picture -> the size it last matched at (boat, clock)
    second_rest_until: float = 0.0  # game clock: the second base had nothing to collect, not visited till then
    second_rest_kinds: set[str] = field(default_factory=set)  # ...of these (more wanted than looked for: it's visited)
    on_second: bool = False  # the view was left on the second base (a battle, or the boat, brings it back)

    def remember_cart(self, screen: np.ndarray, point: tuple[int, int]) -> None:
        self.cart_view, self.cart = screen.copy(), point

    def cart_on(self, screen: np.ndarray) -> tuple[int, int] | None:
        """Where the cart's tag is on ``screen``, as remembered, or ``None``
        (not known, or the two views don't overlap)."""
        if self.cart is None or self.cart_view is None:
            return None
        shift = view_shift(self.cart_view, screen)
        return None if shift is None else (self.cart[0] + shift[0], self.cart[1] + shift[1])


def memory(game: Game) -> Memory:
    return game.__dict__.setdefault("_bb_memory", Memory())


def _to_second_base(game: Game) -> np.ndarray:
    """Drag the view up and left until it stops: the second base. Returns
    the screenshot it ended on. The drags it took last time are made in a
    row, then the view checked against the one they reached then (the
    camera stops at the map's edge, so it's the same view); without that,
    or if it isn't, it drags on until a drag doesn't move it."""
    x1, y1, x2, y2, ms = FIRST_BASE_DRAG
    mem = memory(game)
    drags = 0
    if mem.second_drags and mem.second_view is not None:
        for _ in range(mem.second_drags):
            game.swipe(x2, y2, x1, y1, ms)
            game.wait(1)
        drags = mem.second_drags
        shift = view_shift(mem.second_view, game.refresh())
        if shift is not None and max(map(abs, shift)) <= VIEW_NEAR:
            return game.screen
        log.info("Builder base: the second base isn't where it was (%s off); dragging on", shift)
        mem.second_drags, mem.second_view = 0, None
    before = game.screen if game.screen is not None else game.refresh()
    before = before.copy()
    for _ in range(FIRST_BASE_DRAGS):
        game.swipe(x2, y2, x1, y1, ms)
        game.wait(1)
        if view_change(before, game.refresh()) < VIEW_STILL:
            mem.second_drags, mem.second_view = max(drags, 1), game.screen.copy()
            break
        drags += 1
        before = game.screen.copy()
    return game.screen


def back_to_first_base(game: Game) -> None:
    """Where the round starts (the cart, the Clock Tower, the first base's
    collectors are looked for there): back from the second base, if the
    last round left the view there and no battle brought it back."""
    mem = memory(game)
    if mem.on_second:
        log.debug("Builder base: left on the second base last round; back to the first")
        _back_from_second_base(game)
        mem.on_second = False


def _back_from_second_base(game: Game) -> None:
    """Undo :func:`_to_second_base`: as many drags the other way as it
    took to get there (each moves the map the same distance), with no
    look between them. Not known how many: drag until it stops."""
    drags = memory(game).second_drags
    if not drags:
        _to_first_base(game)
        return
    for _ in range(drags):
        game.swipe(*FIRST_BASE_DRAG)
        game.wait(0.8)
    game.refresh()


BUBBLE_NAMES = (("gold", T.BB_COLLECT_GOLD), ("elixir", T.BB_COLLECT_ELIXIR), ("gems", T.BB_COLLECT_GEMS))
# The elixir cart's tag can show outside CART_AREA (the view isn't always
# where the boat left it): an elixir bubble up here, by the dock, may be the
# cart, so it's tapped last, on its own (its window would catch other taps).
DOCK_AREA = (600, 0, 1060, 420)  # x0, y0, x1, y1
SAME_SPOT = 20  # px: two finds this close are one bubble (seen by two of its pictures)


def bubbles(game: Game, kinds: tuple[str, ...], screen: np.ndarray | None = None) -> list[tuple[str, int, int]]:
    """``(kind, x, y)`` of every bubble or red "full" tag of ``kinds`` on
    one screenshot (the current one, or a fresh one if there's none), one
    per collector, clear of the elixir cart's usual spot."""
    if screen is None:
        screen = game.screen if game.screen is not None else game.refresh()
    from farmbot.game import find_pool

    # Each picture on its own thread (OpenCV lets go of Python's lock while
    # matching), the red tags meanwhile.
    pool = find_pool()
    looks = [(kind, [pool.submit(game.vision.find_all, screen, tname, BUBBLE_THRESHOLD)
                     for tname, _ in game.vision.library.get(name)])
             for kind, name in BUBBLE_NAMES if kind in kinds and game.has(name)]
    found: list[tuple[str, int, int]] = []
    tags = full_tags(screen) if {"gold", "elixir"} & set(kinds) else []
    for kind, futures in looks:
        matches = sorted((m for f in futures for m in f.result()), key=lambda m: -m.score)
        spots = [m.point for m in matches]
        spots += [(x, y) for tag, x, y in tags if tag == kind]  # full: a red tag
        for x, y in spots:
            if _in_cart_area(x, y) or any(np.hypot(x - fx, y - fy) <= SAME_SPOT for _, fx, fy in found):
                continue
            found.append((kind, x, y))
    return found


def _by_the_dock(kind: str, x: int, y: int) -> bool:
    x0, y0, x1, y1 = DOCK_AREA
    return kind == "elixir" and x0 <= x <= x1 and y0 <= y <= y1


def _near(point: tuple[int, int], points: list[tuple[int, int]], distance: float) -> bool:
    return any(np.hypot(point[0] - x, point[1] - y) <= distance for x, y in points)


def _tap_order(spots: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """The bubbles in an order that keeps the finger's path short (nearest
    next, from the left), one that may be the elixir cart last, alone."""
    plain = [s for s in spots if not _by_the_dock(*s)]
    order: list[tuple[str, int, int]] = []
    while plain:
        last = order[-1][1:] if order else (0, 0)
        nxt = min(plain, key=lambda s: np.hypot(s[1] - last[0], s[2] - last[1]))
        plain.remove(nxt)
        order.append(nxt)
    return order or [s for s in spots if _by_the_dock(*s)][:1]


def _collect_in_view(game: Game, tries: int, kinds: tuple[str, ...] = ("gold", "elixir", "gems"),
                     screen: np.ndarray | None = None) -> list[str]:
    """Tap every bubble in view, all kinds from one look, then look again:
    a bubble still there after its tap means its storage is full (or it
    isn't a bubble), and it isn't tapped again (that only selects the
    building, and a drag starting on it would move it). ``screen``: a
    screenshot of the view just taken (else one is)."""
    collected: list[str] = []
    tapped: list[tuple[int, int]] = []
    cart: list[tuple[int, int]] = []  # bubbles that turned out to be the elixir cart's
    said: set[str] = set()
    dock = False  # the last tap was on a bubble by the dock (perhaps the cart's)
    looked: np.ndarray | None = None  # the screen the last taps were aimed on
    for _ in range(tries):
        if screen is None:
            screen = game.refresh()
        if dock and _cart_open(game):
            # The last tap was the cart's tag: keep clear of it from now on.
            cart.append(tapped[-1])
            if looked is not None:
                memory(game).remember_cart(looked, tapped[-1])
            _close_window(game, look=False)
            screen = None
            continue
        if _close_window(game, look=False):
            screen = None
            continue  # it hid the map
        spots = bubbles(game, kinds, screen)
        if not cart and any(_by_the_dock(*s) for s in spots):
            known = memory(game).cart_on(screen)
            if known is not None:
                cart.append(known)
        spots = [s for s in spots if not _near(s[1:], cart, BUBBLE_STAYED)]
        for kind, x, y in spots:
            if _near((x, y), tapped, BUBBLE_STAYED) and kind not in said:
                said.add(kind)
                log.info("Builder base %s: the bubble stayed (storage full?)", kind)
        fresh = _tap_order([s for s in spots if not _near(s[1:], tapped, BUBBLE_STAYED)])
        if not fresh:
            break
        looked = screen
        for kind, x, y in fresh:
            game.tap(x, y, jitter=3)
            game.wait(1.0)
            tapped.append((x, y))
            dock = _by_the_dock(kind, x, y)
            if kind not in collected:
                collected.append(kind)
        screen = None
    return collected


def _cart_open(game: Game) -> bool:
    """The elixir cart's window is open (its Collect button shows)."""
    return game.has(T.BB_CART_COLLECT) and game.find(T.BB_CART_COLLECT, threshold=0.8) is not None


def _close_window(game: Game, look: bool = True) -> bool:
    """Close a window a tap opened over the map (the elixir cart's, when
    the second base's view brings it out of its usual place): drags under
    it would only scroll it. True if one was. ``look``: on a fresh
    screenshot (else the one just taken)."""
    if not game.has(T.CLOSE):
        return False
    if look or game.screen is None:
        game.refresh()
    if not game.click(T.CLOSE):
        return False
    log.info("Closed a window over the builder base")
    game.wait(0.6)
    return True


def collect_elixir_cart(game: Game, full_only: bool = False) -> bool:
    """Open the elixir cart by the dock and press Collect (only while it's
    green: grey means nothing to collect). True if it was collected.
    ``full_only``: only if it's full (its tag the drop on red)."""
    if not (game.has(T.BB_COLLECT_ELIXIR) and game.has(T.BB_CART_COLLECT)):
        return False
    game.zoom_out()
    screen = game.screen if game.screen is not None else game.refresh()
    # Its tag: an elixir drop (as on a collector's bubble), or, once it's full, the drop on red.
    # Looked for around its usual spot only, and where it was last seen, if the view moved.
    tags = _cart_tags(game, screen, CART_AREA, full_only)
    known = memory(game).cart_on(screen) if not tags else None
    if known is not None:
        tags = [p for p in _cart_tags(game, screen, _around(known, CART_NEAR), full_only)
                if _near(p, [known], BUBBLE_STAYED)]
    if not tags:
        if not full_only:
            log.info("Elixir cart: nothing to collect")
        return False
    if full_only:
        log.info("Elixir cart: full; collecting it before its turn")
    game.tap(*tags[0], jitter=3)
    button = game.wait_for(T.BB_CART_COLLECT, timeout=4)
    if button is not None:
        memory(game).remember_cart(screen, tags[0])
    collected = False
    if button is not None and _is_green(game.screen, button):
        game.tap_match(button)
        game.wait(3.0)  # the elixir flies to the storage over the window's X
        collected = True
        log.info("Collected the elixir cart")
    for _ in range(3):  # close the window, and make sure it's closed
        game.refresh()
        if not game.find(T.BB_CART_COLLECT, threshold=0.8):
            break
        game.click(T.CLOSE)
        game.wait(1.0)
    return collected


CART_NEAR = 40  # px around where the cart's tag was last seen, looked at when the view has moved


def _around(point: tuple[int, int], margin: int) -> tuple[int, int, int, int]:
    return point[0] - margin, point[1] - margin, point[0] + margin, point[1] + margin


def _cart_tags(game: Game, screen: np.ndarray, area: tuple[int, int, int, int],
               full_only: bool = False) -> list[tuple[int, int]]:
    """The cart's tag in ``area`` (x0, y0, x1, y1): an elixir drop, or the
    drop on red once it's full (only that, ``full_only``). Only that area
    (and a margin for the pictures' size) is looked at."""
    from farmbot.geometry import Rect

    x0, y0, x1, y1 = area
    pad = 40
    region = Rect(x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
    inside = (lambda x, y: x0 <= x <= x1 and y0 <= y <= y1)
    tags = [] if full_only else [m.point for m in game.find_all(T.BB_COLLECT_ELIXIR, threshold=0.75, region=region)
                                 if inside(m.x, m.y)]
    rx, ry = max(region.x, 0), max(region.y, 0)
    part = screen[ry:region.y + region.h, rx:region.x + region.w]
    tags += [(x + rx, y + ry) for kind, x, y in full_tags(part) if kind == "elixir" and inside(x + rx, y + ry)]
    return tags


CLOCK_SIZES = (0.9, 1.0, 1.1)
CLOCK_THRESHOLD = 0.8


def clock_ready(game: Game) -> bool:
    """The Clock Tower's free boost is ready: its clock bubble shows (on a
    fresh screenshot). A quick look (the bubble's size is remembered)."""
    if not (game.has(T.BB_CLOCK_READY) and game.has(T.BB_CLOCK_FREE_BOOST) and game.has(T.BB_FREE_BOOST_WINDOW)):
        return False
    game.refresh()
    return _sized_match(game, T.BB_CLOCK_READY, CLOCK_SIZES, CLOCK_THRESHOLD) is not None


def boost_clock_tower(game: Game) -> bool:
    """Use the Clock Tower's free boost if its clock bubble shows: tap the
    bubble (selects the tower), its "Free Boost!" button, then Boost in the
    "Free Boost!" window, which only ever opens for the free one. The blue
    Boost beside it (a magic item) is never touched. True if boosted."""
    if not (game.has(T.BB_CLOCK_READY) and game.has(T.BB_CLOCK_FREE_BOOST) and game.has(T.BB_FREE_BOOST_WINDOW)):
        return False
    game.refresh()
    bubble = _sized_match(game, T.BB_CLOCK_READY, CLOCK_SIZES, CLOCK_THRESHOLD)
    if bubble is None:
        return False
    game.tap(bubble.x, bubble.y, jitter=2)
    boosted = False
    button = game.wait_for(T.BB_CLOCK_FREE_BOOST, timeout=4)
    if button is not None:
        game.tap_match(button)
        if game.wait_for(T.BB_FREE_BOOST_WINDOW, timeout=4) is not None:
            confirm = game.find(T.BB_FREE_BOOST_CONFIRM)
            if confirm is not None:
                game.tap_match(confirm)
                game.wait(2.0)
                boosted = True
                log.info("Clock Tower: free boost on")
    if not boosted:
        log.info("Clock Tower: the free boost didn't go through")
    village.close_all_windows(game)
    game.deselect(always=True)
    return boosted


# ------------------------------------------------------------------ walls
BB_ICON_OFFSET = (39, -65)  # cost icon relative to the builder Upgrade button's text
# A selected builder base wall's Upgrade button: the old builder base look,
# and the home village look the game draws there since its November 2026 update.
BB_WALL_BUTTONS = (T.BB_UPGRADE, T.UPGRADE)


def _wall_buttons(game: Game) -> dict[str | None, Match]:
    """The Upgrade buttons under a selected builder base wall, by what they
    cost ("gold", "elixir"; None: no price, like Upgrade More), in either look."""
    from farmbot.tasks.village import _icon_offset, cost_kind

    buttons = {}
    for name in BB_WALL_BUTTONS:
        if not game.has(name):
            continue
        for m in game.find_all(name):
            offset = BB_ICON_OFFSET if name == T.BB_UPGRADE else _icon_offset(m)
            buttons.setdefault(cost_kind(game.screen, m.x, m.y, offset), m)
    return buttons


def _green_button(screen: np.ndarray, region: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Centre of the biggest bright green area in ``region`` (x0, y0, x1, y1):
    the price button of the builder base upgrade window."""
    x0, y0, x1, y1 = region
    hsv = cv2.cvtColor(screen[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 150, 170), (50, 255, 255))
    count, _, stats, centres = cv2.connectedComponentsWithStats(mask)
    if count < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < 2000:
        return None
    cx, cy = centres[i]
    return round(x0 + cx), round(y0 + cy)


def upgrade_walls(game: Game, cfg: BuilderConfig, read_resources: Callable[[], Loot | None],
                  keep: dict[str, int], known: dict | None = None, last: bool = False) -> int:
    """Spend spare builder gold and elixir on builder base walls, all in one
    go as at home: Build → scroll to the cheapest "Wall x N" → Upgrade More
    until as many are selected as the spare covers at the row's price →
    Upgrade → Okay (one wall: the upgrade window's green price button);
    then again in the other resource, if it has some to spare too.
    ``keep``: how much of each resource stays. ``known``:
    kept between calls, the wall's price (see ``village.upgrade_walls``).
    ``last``: a battle comes next, which zooms out by itself, so the walls
    are left as they are (selected, zoomed in on); otherwise they're
    dropped and the view zoomed out. Returns how many walls were upgraded."""
    from farmbot.tasks.village import payable, wall_budget, walls_done

    needed = (T.BUILD, T.WALL_SUGGESTION, T.BB_UPGRADE_TITLE, T.CLOSE)
    if not cfg.walls or not all(game.has(n) for n in needed) or not any(game.has(n) for n in BB_WALL_BUTTONS):
        return 0
    # Not dropped or zoomed out first: the Build list (opened for each
    # resource) drops what an upgrade left selected, and goes by no view.
    loot = read_resources()
    budget = wall_budget(cfg.walls_use, loot, keep) if loot else []
    known = {} if known is None else known
    if not budget:
        log.info("Builder walls: %s", "nothing to spare above what's kept" if keep else
                 "how much the storages hold isn't known yet")
    else:
        # The price last seen, for each resource: they pay for different walls
        # (a low wall level takes gold only).
        budget = [k for k in budget if payable(loot, [k], keep, known.setdefault(k, {}), "Builder walls")]
    if not budget:
        if not last:  # an upgrade before may have left something selected, zoomed in on
            game.deselect()
            game.zoom_out()
        return 0
    start, upgraded = loot, {}
    try:
        for i, kind in enumerate(budget):
            if i and not payable(loot, [kind], keep, known[kind], "Builder walls"):
                continue
            count, price, loot = _upgrade_walls(game, loot, [kind], keep, known[kind])
            if count:
                upgraded[kind] = (count, price)
    finally:
        village.close_all_windows(game)
        if not last:
            # Dropped and zoomed out, not dragged back: what comes next goes
            # by the Build list, or sails home (the boat is looked for at the
            # first base whatever the view).
            _drop(game)
    if not upgraded:
        return 0
    game.wait(1.5)  # the resource counters run down to what's left
    return walls_done(start, read_resources(), upgraded, "builder base wall")


def _drop(game: Game) -> None:
    """Drop what's selected (see ``village.deselect``) and zoom out."""
    from farmbot.tasks.village import deselect

    deselect(game)
    game.zoom_out()


def _cheapest_wall(game: Game, kind: str):
    """The cheapest "Wall x N" row that ``kind`` ("gold", "elixir") can pay
    for: the list filtered to that resource and sorted Cost Min, its first
    wall. (Unfiltered, the list's walls are spread through it by price, and
    a low wall level takes gold only.) Without the filter's pictures, the
    walls found by scrolling."""
    from farmbot.tasks import build_list
    from farmbot.tasks.village import find_wall_entry, first_wall

    lst = build_list.BUILDER
    if all(game.has(n) for n in (T.LIST_FILTER, T.SORT_COST_MIN, lst.resource_filter[kind])):
        build_list.open_tab(game, "buildings", lst)
        if build_list.set_filter(game, lst.resource_filter[kind], None, T.SORT_COST_MIN, lst):
            return first_wall(game)
        log.info("Builder walls: the list's %s filter didn't set; looking through it unfiltered", kind)
    return find_wall_entry(game)


def _upgrade_walls(game: Game, loot: Loot, budget: list[str], keep: dict[str, int],
                   known: dict) -> tuple[int, int | None, Loot]:
    """One resource's walls (see ``village._upgrade_walls``). Returns how
    many walls were selected and paid for, at what price, and the
    resources left after (as paid)."""
    from farmbot.tasks.village import (MAX_WALLS, WALL_SCREENS, _keep_screen, select_more,
                                       select_wall, wall_price, price_is_red)

    # The list doesn't stay open after an upgrade, as it does at home.
    village.close_all_windows(game)
    game.refresh()
    if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
        log.info("Builder walls: the Build list didn't open")
        return 0, None, loot
    game.wait(1.2)
    entry = _cheapest_wall(game, budget[0])
    if entry is None:
        log.info("No builder base wall to upgrade in the list")
        return 0, None, loot
    price = wall_price(game, entry)
    red = not price and price_is_red(game, entry)
    if price:
        known["price"] = price
        # The list is filtered to this resource: its price, so the wall
        # needn't be selected only to find one can't be paid for.
        spare = getattr(loot, budget[0]) - keep[budget[0]]
        if spare < price:
            log.info("Builder walls: %s %s spare, a wall costs %s", f"{spare:,}", budget[0], f"{price:,}")
            return 0, None, loot
    if not select_wall(game, entry, BB_WALL_BUTTONS):
        alike = max(game.likeness(n) for n in BB_WALL_BUTTONS)
        log.info("Builder walls: no Upgrade button under the wall (%.2f alike)", alike)
        _keep_screen(WALL_SCREENS, game.screen)
        return 0, None, loot
    game.wait(0.6)  # the buttons fade in
    game.refresh()
    buttons = _wall_buttons(game)
    kind = next((k for k in budget if k in buttons), None)
    if kind is None:
        log.info("Builder walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
        return 0, None, loot
    spare = getattr(loot, kind) - keep[kind]
    if not price:
        # Unread: the price last seen. Never tap Upgrade without knowing it's
        # paid for (short of loot, the game offers to make it up in gems).
        price = known.get("price")
        if not price:
            log.info("Builder walls: %s", "not enough to pay for one (its price shows red)" if red
                     else "couldn't read the price; trying again next round")
            return 0, None, loot
        log.info("Builder walls: couldn't read the price; it was %s", f"{price:,}")
    count = min(MAX_WALLS, spare // price)
    if count < 1:
        log.info("Builder walls: %s %s spare, a wall costs %s", f"{spare:,}", kind, f"{price:,}")
        return 0, None, loot
    if count > 1 and not (game.has(T.UPGRADE_MORE) and game.has(T.ADD_WALL)):
        count = 1
    if count > 1:
        count = select_more(game, count)
    game.refresh()
    buttons = _wall_buttons(game)
    if kind not in buttons:
        log.info("Builder walls: the %s Upgrade button went away", kind)
        return 0, None, loot
    game.tap_match(buttons[kind])
    if not _confirm_walls(game):
        log.info("Builder walls: the upgrade window didn't open")
        return 0, None, loot
    game.wait(1.5)
    left = getattr(loot, kind) - count * price
    return count, price, replace(loot, **{kind: left})


def _confirm_walls(game: Game, timeout: float = 4) -> bool:
    """Accept the window Upgrade opened: after Upgrade More, an "Upgrade
    Walls" question with Okay; for one wall, the upgrade window's green
    price button."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        okay = game.find(T.OKAY) if game.has(T.OKAY) else None
        button = (okay.x, okay.y) if okay else None
        title = game.find(T.BB_UPGRADE_TITLE) if button is None else None
        if title is not None:
            button = _green_button(game.screen, (title.x - 360, title.y + 450, title.x + 600, title.y + 720))
        if button is not None:
            if asks_for_gems(game, button[1]):
                log.warning("Builder walls: the upgrade asks for gems; leaving it alone")
                return False
            game.tap(*button)
            return True
        if game.clock() >= deadline:
            return False
        game.wait(0.5)


def _is_green(screen: np.ndarray, match) -> bool:
    x0, y0 = match.x - match.w // 2, match.y - match.h // 2
    patch = cv2.cvtColor(screen[y0:y0 + match.h, x0:x0 + match.w], cv2.COLOR_BGR2HSV)
    return float(np.median(patch[:, :, 1])) > 60 and 30 <= float(np.median(patch[:, :, 0])) <= 70


# ------------------------------------------------------------------ cards
@dataclass
class Slot:
    index: int  # 0 = hero, 1.. = troops
    x: int


def slot_x(index: int, cfg: BuilderConfig) -> int:
    return cfg.hero_x if index == 0 else round(cfg.first_troop_x + (index - 1) * cfg.card_step)


def card_state(screen: np.ndarray, x: int, cfg: BuilderConfig) -> str:
    """"ready", "out", "dead" or "empty" for the card centred on ``x``, from
    the band across its top: purple (fresh) or dark blue (carried over from
    the first base) = ready, green = on the field, grey = dead, anything
    else = no card."""
    y = cfg.card_y
    patch = screen[y - 34:y - 18, max(x - 30, 0):x + 30]
    if patch.size == 0:
        return "empty"
    # A card shows a detailed troop picture; an empty slot is a dashed
    # outline over the map (sometimes water, the same blue as some cards).
    picture = cv2.cvtColor(screen[y - 10:y + 30, max(x - 30, 0):x + 30], cv2.COLOR_BGR2GRAY)
    if float(picture.std()) < CARD_DETAIL:
        return "empty"
    h, s, v = (float(np.median(c)) for c in cv2.split(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)))
    if s < 30 and v > 80:
        return "dead"
    if 38 <= h <= 60 and s > 150:
        return "out"
    if 130 <= h <= 155 and s > 100 and v > 200:
        return "ready"  # fresh troop
    if 100 <= h <= 125 and s > 120 and v >= 60:
        return "ready"  # blue: counted ordinary troops, or carried-over troops
    return "empty"


def without_hero(cfg: BuilderConfig) -> BuilderConfig:
    """No unlocked hero: ordinary troops start one card further left."""
    return replace(cfg, use_hero=False, hero_ability=False,
                   first_troop_x=round(cfg.first_troop_x - cfg.card_step))


def troop_count(screen: np.ndarray, slot: Slot, cfg: BuilderConfig, vision) -> int | None:
    """Ordinary blue cards show Nx; purple/remaining single-unit cards hold one."""
    patch = screen[cfg.card_y - 34:cfg.card_y - 18, slot.x - 30:slot.x + 30]
    h, _, v = np.median(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV), axis=(0, 1))
    if not (100 <= h <= 125 and v > 170):
        return 1
    # Selecting a card raises its header eight pixels; keep both positions in view.
    pieces = text.segment(screen, Rect(slot.x + 5, cfg.card_y - 51, 43, 36))
    if len(pieces) < 2:
        return None
    digits = [vision.font("fonts/count").classify(p) for p in pieces[:-1]]  # trailing x
    if any(not char.isdigit() or score < 0.7 for char, score in digits):
        return None
    count = int("".join(char for char, _ in digits))
    return count if 0 < count <= 400 else None


def hero_out(screen: np.ndarray, cfg: BuilderConfig) -> bool:
    """The hero is on the field: its card pops up and the health bar
    (green, or yellow/red when hurt) shows above it. A hero carried hurt to
    the second base also has a bar, but inside the top of its card (lower
    down), and still has to be placed."""
    x, y = cfg.hero_x, cfg.card_y
    band = cv2.cvtColor(screen[y - 66:y - 40, x - 38:x + 40], cv2.COLOR_BGR2HSV).reshape(-1, 3)
    bar = (band[:, 1] > 160) & (band[:, 2] > 190) & ((band[:, 0] <= 60) | (band[:, 0] >= 170))
    # Not out (or waiting at the second base): 0; out: 0.04 (badly hurt) to 0.26.
    return float(bar.mean()) > 0.018


# The hero's health bar over its card (bot coordinates at 860 x 672): it
# starts here and is this long when full. It sits lower before the hero is
# placed (between the two bases) than once it's on the field.
BAR_LEFT, BAR_LENGTH = 33, 72
SWAP_BUTTON = (17, 58)  # the green swap button, relative to (hero_x, card_y)
PICKER_SLOTS = ((-35, 83), (63, 83), (161, 83))  # hero portraits relative to the "Change Heroes" title


def hero_health(screen: np.ndarray, cfg: BuilderConfig) -> float | None:
    """The hero's health (0-1) from the bar over its card: 0 if the card is
    greyed out (dead), ``None`` if there's no bar (not hurt yet)."""
    x, y = cfg.hero_x, cfg.card_y
    card = cv2.cvtColor(screen[y - 10:y + 40, x - 30:x + 30], cv2.COLOR_BGR2HSV)
    if card.size and float(np.median(card[:, :, 1])) < 30:
        return 0.0
    band = cv2.cvtColor(screen[y - 70:y - 18, BAR_LEFT - 6:BAR_LEFT + BAR_LENGTH + 6], cv2.COLOR_BGR2HSV)
    h, s, v = band[:, :, 0].astype(int), band[:, :, 1], band[:, :, 2]
    fill = (s > 150) & (v > 140) & (((h >= 35) & (h <= 65)) | ((h >= 18) & (h < 35)) | (h >= 170) | (h <= 6))
    best = 0
    for row in fill:
        run, start = 0, None
        for i, on in enumerate(row):
            if on and start is None:
                if i > 12:  # a bar fills from its left end
                    break
                start = i
            if start is not None:
                if not on:
                    break
                run = i - start + 1
        best = max(best, run)
    if best < 4:
        return None
    return min(best / BAR_LENGTH, 1.0)


# The hero's ability charge: three bars over its card while it's on the
# field (left to right, each taller), filling one after the other: purple
# once full, blue while it fills. Bot coordinates, relative to (hero_x, card_y).
CHARGE_ROW = -69  # a row across all three
CHARGE_BARS = ((-34, -18), (-10, 4), (12, 33))  # x spans, inside each bar's border
CHARGES_TO_FIRE = 3  # full bars: the ability is ready once all three are
CHARGE_FULL = 0.6  # share of a bar's row that's purple when it's full


def hero_charges(screen: np.ndarray, cfg: BuilderConfig) -> int:
    """How many of the hero's three ability bars are full (0: none, or
    the bars aren't showing). A full third bar flashes, so it reads as
    full only now and then: often enough, as it's read twice a second."""
    x, y = cfg.hero_x, cfg.card_y + CHARGE_ROW
    full = 0
    for x0, x1 in CHARGE_BARS:
        patch = screen[y - 2:y + 3, x + x0:x + x1]
        if patch.size == 0:
            break
        h, s, v = cv2.split(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV))
        if float(((h >= 135) & (h <= 165) & (s > 120) & (v > 150)).mean()) < CHARGE_FULL:
            break  # they fill in order
        full += 1
    return full


def _picker_right_edge(screen: np.ndarray, tx: int, ty: int) -> int:
    """x of the hero picker's light right border (the screen's edge if none)."""
    band = cv2.cvtColor(screen[ty + 10:ty + 150, tx:], cv2.COLOR_BGR2HSV)
    light = ((band[:, :, 2] > 170) & (band[:, :, 1] < 60)).mean(axis=0)
    columns = np.nonzero(light > 0.6)[0]
    return tx + int(columns[0]) if len(columns) else screen.shape[1]


def _likeness(image: np.ndarray, art: np.ndarray, sizes=np.arange(0.5, 1.25, 0.05)) -> float:
    """How well ``art`` (grey) shows up somewhere in ``image`` (grey), at the
    best of a few sizes."""
    best = 0.0
    for k in sizes:
        t = cv2.resize(art, None, fx=k, fy=k)
        if t.shape[0] < image.shape[0] and t.shape[1] < image.shape[1]:
            best = max(best, float(cv2.matchTemplate(image, t, cv2.TM_CCOEFF_NORMED).max()))
    return best


def _open_picker(game: Game, cfg: BuilderConfig) -> list[tuple[np.ndarray, tuple[int, int]]] | None:
    """Tap the swap button and return the picker's hero portraits (grey) with
    where to tap each; ``None`` if it didn't open."""
    if not game.has(T.BB_CHANGE_HEROES):
        return None
    dx, dy = SWAP_BUTTON
    game.tap(cfg.hero_x + dx, cfg.card_y + dy)
    title = game.wait_for(T.BB_CHANGE_HEROES, timeout=3)
    if title is None:
        log.info("The hero picker didn't open")
        return None
    screen = game.screen
    right = _picker_right_edge(screen, title.x, title.y)
    options = []
    for ox, oy in PICKER_SLOTS:
        px, py = title.x + ox, title.y + oy
        if px + 40 > right:
            break  # past the picker: the battle screen behind it
        portrait = cv2.cvtColor(screen[max(py - 50, 0):py + 55, max(px - 50, 0):px + 50], cv2.COLOR_BGR2GRAY)
        if portrait.shape[0] < 100 or portrait.shape[1] < 100 or float(portrait[25:80, 20:80].std()) < 25:
            continue  # no hero in this slot
        options.append((portrait, (px, py)))
    return options


def _close_picker(game: Game, cfg: BuilderConfig) -> None:
    dx, dy = SWAP_BUTTON
    game.tap(cfg.hero_x + dx, cfg.card_y + dy)


def switch_hero(game: Game, cfg: BuilderConfig) -> bool:
    """Open the hero picker from the swap button and choose the hero that
    looks least like the one on the card now. True if it switched."""
    x, y = cfg.hero_x, cfg.card_y
    # The card shows a close-up of the hero: matched at a few sizes against
    # each picker portrait, it looks most like its own.
    current = cv2.cvtColor(game.refresh()[y - 35:y + 40, x - 33:x + 33], cv2.COLOR_BGR2GRAY)
    options = _open_picker(game, cfg)
    if options is None:
        return False
    if len(options) < 2:
        _close_picker(game, cfg)
        return False
    _, point = min((_likeness(portrait, current), point) for portrait, point in options)
    game.tap(*point)
    game.wait(0.8)
    log.info("Switched hero")
    return True


HEROES = {"machine": T.BB_HERO_MACHINE, "copter": T.BB_HERO_COPTER}
HERO_NAMES = {"machine": "Battle Machine", "copter": "Battle Copter"}
HERO_MATCH = 0.7  # its own card art scores 0.88+ (card or picker), the other hero's 0.45 or less


def _hero_art(game: Game, key: str) -> list[np.ndarray]:
    name = HEROES[key]
    if not game.has(name):
        return []
    return [cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) for _, image in game.vision.library.get(name)]


def current_hero(game: Game, screen: np.ndarray, cfg: BuilderConfig) -> str | None:
    """Which hero is on the card ("machine" or "copter"), or ``None``."""
    x, y = cfg.hero_x, cfg.card_y
    area = cv2.cvtColor(screen[y - 35:y + 50, x - 43:x + 43], cv2.COLOR_BGR2GRAY)
    sizes = np.arange(0.85, 1.16, 0.05)
    scores = {key: max((_likeness(area, art, sizes) for art in _hero_art(game, key)), default=0.0) for key in HEROES}
    key = max(scores, key=scores.get)
    return key if scores[key] >= HERO_MATCH else None


def choose_hero(game: Game, cfg: BuilderConfig, want: str) -> bool:
    """Put ``want`` ("machine" or "copter") on the hero card, from the
    picker, if another hero is there. True if it changed the hero."""
    have = current_hero(game, game.refresh(), cfg)
    if have is None or have == want:
        return False
    art = _hero_art(game, want)
    options = _open_picker(game, cfg) if art else None
    if not options:
        return False
    score, point = max((max(_likeness(portrait, a) for a in art), point) for portrait, point in options)
    if score < HERO_MATCH:
        log.info("The %s isn't in the hero picker", HERO_NAMES[want])
        _close_picker(game, cfg)
        return False
    game.tap(*point)
    game.wait(0.8)
    log.info("Chose the %s for the first base", HERO_NAMES[want])
    return True


def ready_troops(screen: np.ndarray, cfg: BuilderConfig) -> list[Slot]:
    slots = [Slot(i, slot_x(i, cfg)) for i in range(1, cfg.max_troop_cards + 1)]
    return [s for s in slots if card_state(screen, s.x, cfg) == "ready"]


# ------------------------------------------------------------------ placement
def base_centre(screen: np.ndarray) -> tuple[int, int]:
    """Where the enemy buildings are thickest: bright or warm pixels on the
    dull teal ground, inside the map."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((v > 150) | ((s > 120) & ((h < 35) | (h > 130)))).astype(np.uint8) * 255
    inside = np.zeros_like(mask)
    cv2.fillPoly(inside, [MAP], 255)
    mask &= inside
    mask[650:] = 0  # the Boost buttons
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    density = cv2.blur(mask.astype(np.float32), (121, 91))
    y, x = np.unravel_index(int(np.argmax(density)), density.shape)
    return int(x), int(y)


def pick_corner(screen: np.ndarray, cfg: BuilderConfig) -> tuple[str, tuple[int, int]]:
    """The configured corner, or (``auto``) the one farthest from the base."""
    if cfg.corner in CORNERS:
        return cfg.corner, CORNERS[cfg.corner]
    cx, cy = base_centre(screen)
    name = max(CORNERS, key=lambda k: np.hypot(CORNERS[k][0] - cx, (CORNERS[k][1] - cy) / ISO_RATIO))
    return name, CORNERS[name]


# ------------------------------------------------------------------ battle
SEARCH_FOR = 60  # seconds the opponent search may take before it's cancelled and started again
SEARCHES = 3  # searches before giving up
LOADING_FOR = 20  # s more for an opponent found (no Cancel any more) to load
COOLDOWN_MAX = 360  # s an attack cooldown is waited out at most
COOLDOWN_LOOK = 10  # s between looks at it


def start_battle(game: Game) -> None:
    """Builder base → Battle → Find Now! → the battle screen. A search that
    runs long is cancelled and started again (the game's matchmaking can
    stall), rather than the whole game being restarted."""
    for search in range(SEARCHES):
        _find_now(game)
        if _opponent_found(game):
            game.wait(1.0)
            return
        game.refresh()
        if game.find(T.BB_BATTLE_STARTS) or game.find(T.BB_BATTLE_ENDS):  # found just then
            game.wait(1.0)
            return
        if search == SEARCHES - 1:
            break
        if not game.click(T.BB_SEARCH_CANCEL):
            # No Cancel: past the search, the opponent's base loading.
            if _opponent_found(game, LOADING_FOR):
                game.wait(1.0)
                return
            break
        log.info("Builder base: no opponent after %d s; cancelled the search to search again", SEARCH_FOR)
        if not game.wait_for(T.BB_BATTLE, timeout=10):
            break
    raise BuilderError("no opponent found")


def _find_now(game: Game) -> None:
    from farmbot.tasks.village import clear_popups

    for attempt in range(3):
        game.refresh()
        if game.find(T.BB_FIND_NOW):  # the Start Attack window is already open
            game.click(T.BB_FIND_NOW)
            return
        if game.find(T.BB_ATTACK_COOLDOWN):
            _wait_out_cooldown(game)
            return
        clear_popups(game)  # the Star Bonus window shows after some battles
        game.refresh()
        # Bottom left only: the Start Attack window has the same axes on top.
        if not game.click(T.BB_BATTLE, region=game.region(BATTLE_AREA)):
            raise BuilderError("builder base Battle button not visible")
        if game.click_when_visible(T.BB_FIND_NOW, timeout=6):
            return
        game.refresh()
        if game.find(T.BB_ATTACK_COOLDOWN):
            _wait_out_cooldown(game)
            return
        log.info("Find Now! didn't show; trying again")
    raise BuilderError("Find Now! didn't show")


def _wait_out_cooldown(game: Game) -> None:
    """The Start Attack window shows "Attack available in: 1m 45s" where
    Find Now! goes (a battle left early, by a restart, holds attacks back
    a while): it's kept open until Find Now! comes back, then tapped."""
    log.info("Builder base: attacks are on a cooldown (a battle was left early); waiting it out")
    deadline = game.clock() + COOLDOWN_MAX
    while game.clock() < deadline:
        game.wait(COOLDOWN_LOOK)
        game.refresh()
        if game.click(T.BB_FIND_NOW):
            log.info("Builder base: the cooldown is over")
            return
        if not game.find(T.BB_ATTACK_COOLDOWN):
            raise BuilderError("the Start Attack window closed during the cooldown")
    raise BuilderError("attacks still on cooldown")


def _opponent_found(game: Game, seconds: float | None = None) -> bool:
    """Whether the battle screen shows within ``seconds`` (SEARCH_FOR)."""
    from farmbot.tasks.village import attack_despite_obstructed_layout

    deadline = game.clock() + (SEARCH_FOR if seconds is None else seconds)
    while True:
        game.refresh()
        if game.find(T.BB_BATTLE_STARTS) or game.find(T.BB_BATTLE_ENDS):
            return True
        if attack_despite_obstructed_layout(game):
            continue
        if game.clock() >= deadline:
            return False
        game.wait(1.0)


def _rhythm_wait(game: Game, seconds: float, shortest: float = 0.85) -> None:
    """Wait about ``seconds``: exactly, or with "Battle rhythm varies" a
    little longer or shorter (never under ``shortest`` of it)."""
    varied = human.pause(game.rng, seconds, game.uses("battle_rhythm"), shortest=shortest)
    game.note("battle_rhythm", human.pause_extra(seconds, shortest=shortest), varied - seconds)
    if varied > 0:
        game.wait(varied, exact=True)


def _later(game: Game, seconds: float) -> float:
    """A battle's timing, ``seconds``: each battle its own, around it, with
    "Battle rhythm varies" (it adds no time: the battle plays on)."""
    return seconds * game.rng.uniform(*TIMING_SPREAD) if game.uses("battle_rhythm") else seconds


TIMING_SPREAD = (0.8, 1.25)


def _tap_card(game: Game, slot: Slot, cfg: BuilderConfig) -> None:
    game.tap(slot.x, cfg.card_y)
    game.wait(cfg.tap_gap)


def _toward(point: tuple[int, int], target: tuple[int, int], share: float) -> tuple[int, int]:
    return (round(point[0] + (target[0] - point[0]) * share), round(point[1] + (target[1] - point[1]) * share))


def place_first(game: Game, slot: Slot, start: tuple[int, int], centre: tuple[int, int],
                placed: Callable[[np.ndarray], bool], cfg: BuilderConfig) -> tuple[int, int] | None:
    """Drop the unit on ``slot`` at ``start``; where the game refuses (the
    corner is rock or water on some maps), move the point towards the base
    and try again. Returns the point that worked, or ``None``."""
    point = start
    for attempt in range(PLACE_STEPS):
        if attempt % 3 == 0:  # (re)select the card now and then; it normally stays selected
            _tap_card(game, slot, cfg)
        game.tap(*point, jitter=3)
        game.wait(0.6)
        if placed(game.refresh()):
            return point
        point = _toward(point, centre, PLACE_STEP)
    return None


def deploy_stage(game: Game, cfg: BuilderConfig, first: bool = False) -> tuple[float, list[Slot]]:
    """Hero at the far corner, then (``troop_delay`` s later) every ready
    troop there too. Returns when the hero landed and the troops sent.
    On the ``first`` base, the hero is set to ``cfg.first_hero`` first."""
    if first:
        game.look_around()  # "Look around before deploying"
    game.zoom_out(quick=True)  # each stage opens at the game's default zoom
    if first and cfg.use_hero and cfg.first_hero in HEROES:
        choose_hero(game, cfg, cfg.first_hero)
    screen = game.refresh()
    side, point = pick_corner(screen, cfg)
    centre = base_centre(screen)
    troops = ready_troops(screen, cfg)
    log.info("Stage: hero and %d troop card(s) from the %s corner", len(troops), side)
    hero = Slot(0, cfg.hero_x)
    if cfg.use_hero and cfg.switch_hero:
        health = hero_health(screen, cfg)
        if health is not None and health < cfg.switch_below:
            log.info("Hero health %d%%, below %d%%: switching hero", health * 100, cfg.switch_below * 100)
            if switch_hero(game, cfg):
                screen = game.refresh()
    landed = game.clock()
    sent: list[Slot] = []
    if cfg.use_hero and not hero_out(screen, cfg):
        found = place_first(game, hero, point, centre, lambda s: hero_out(s, cfg), cfg)
        if found is None:
            log.warning("The hero couldn't be placed")
        else:
            point = found
        landed = game.clock()
        _rhythm_wait(game, max(cfg.troop_delay - 0.6, 0))
    elif troops:
        # No hero to find a good spot with: the first troop does it.
        first = troops[0]
        count = troop_count(screen, first, cfg, game.vision)
        def placed(s):
            if card_state(s, first.x, cfg) != "ready":
                return True
            left = troop_count(s, first, cfg, game.vision)
            if left is None:
                raise DeploymentCountError("Builder troop count unreadable after first deployment; effect uncertain")
            return count is not None and left is not None and left < count
        found = place_first(game, first, point, centre, placed, cfg) if count is not None else None
        if found is not None:
            point = found
            sent.append(first)
        troops = ready_troops(game.refresh(), cfg)
    for _ in range(2):  # a second pass for cards whose tap was dropped
        if not troops:
            break
        # Select the first card; each tap on the map sends the selected
        # troop and the game selects the next card. The pause is the troop speed.
        counts = [troop_count(game.screen, s, cfg, game.vision) for s in troops]
        if any(n is None for n in counts):
            raise DeploymentCountError("Builder troop count unreadable; remaining deployments stopped")
        _tap_card(game, troops[0], cfg)
        taps = sum(counts)
        pause = human.pauses(game.rng, taps, cfg.tap_delay, game.uses("battle_rhythm"))
        game.note("battle_rhythm", human.pauses_extra(taps, cfg.tap_delay),
                  sum(pause[:-1]) - cfg.tap_delay * (taps - 1) if isinstance(pause, list) else 0.0)
        game.tap_many([point] * taps, jitter=6, pause=pause)
        sent += troops
        game.wait(0.5)
        tried = len(troops)
        troops = ready_troops(game.refresh(), cfg)
        if not troops:
            break
        log.info("%d troop card(s) didn't go out; trying again", len(troops))
        if len(troops) == tried:  # none went out: the fast taps aren't getting through
            game.stop_fast_taps()
    return landed, sorted({s.index: s for s in sent}.values(), key=lambda s: s.index)


def _fire_abilities(game: Game, batch: list[Slot], cfg: BuilderConfig) -> int:
    """Tap each card's ability; a card whose troop is gone is skipped."""
    screen = game.refresh()
    fired = 0
    for slot in batch:
        if card_state(screen, slot.x, cfg) != "out":
            continue
        _tap_card(game, slot, cfg)
        fired += 1
    return fired


HERO_ABILITY_GAP = 2.0  # s after firing the hero's ability before the bars are read again (they drop)
BANNER_THRESHOLD = 0.8  # "Battle starts in" scores 0.87+ on it, under 0.75 on "Battle ends in"


def stage_reset(game: Game) -> bool:
    """The first base fell and the second began: its "Battle starts in"
    countdown is up. (The cards alone can't tell: between the bases the
    survivors' cards look much like ready ones.)"""
    return game.has(T.BB_BATTLE_STARTS) and game.find(T.BB_BATTLE_STARTS, threshold=BANNER_THRESHOLD) is not None


@dataclass
class BattleResult:
    stages: int  # bases attacked (2 = the first fell and the second was played)
    screen: np.ndarray | None = None  # the result screen (damage, stars, rewards)


def end_battle(game: Game) -> bool:
    """Press End Battle (and Okay, if it asks) and wait for the result.
    True once the result's Return Home shows."""
    if not game.has(T.END_BATTLE) or not game.click(T.END_BATTLE):
        return False
    game.wait(0.6)
    if game.has(T.SURRENDER_OK):
        game.click_when_visible(T.SURRENDER_OK, timeout=3)
    return game.wait_for(T.BB_RETURN_HOME, timeout=15) is not None


def _finish(game: Game, stages: int) -> BattleResult:
    """Keep the result screen, then Return Home."""
    from farmbot.tasks.attack import look_at_result

    result = BattleResult(stages, None if game.screen is None else game.screen.copy())
    look_at_result(game)
    game.click(T.BB_RETURN_HOME)
    if game.wait_for(T.BB_BATTLE, timeout=15):
        if getattr(game, "note_wanted", False):  # a Clan Games challenge's note (it shows a few seconds)
            from farmbot.tasks import clan_games

            game.games_note = clan_games.catch_note(game)
        _settle_home(game)
    memory(game).on_second = False  # home from a battle: on the first base
    return result


HOME_SETTLE = 5.0  # s at most for the clouds to clear off the village after a battle


def _settle_home(game: Game) -> None:
    """Back from a battle the Battle button shows through the clouds while
    they still clear: wait until two looks agree. A pinch (the zoom-out
    that follows) made while they move lands as a one-finger drag."""
    before = game.refresh()
    deadline = game.clock() + HOME_SETTLE
    while game.clock() < deadline:
        game.wait(0.4, exact=True)
        after = game.refresh()
        if view_change(before, after) < VIEW_STILL:
            return
        before = after


def attack(game: Game, cfg: BuilderConfig, on_stage: Callable[[int], None] | None = None,
           on_deployment_error: Callable[[], None] | None = None) -> BattleResult:
    """One builder battle, every stage, back to the builder base.
    ``on_stage`` hears each base as it begins (1, 2)."""
    start_battle(game)
    stages = 0
    started = game.clock()
    while stages < MAX_STAGES:
        if on_stage:
            on_stage(stages + 1)
        try:
            landed, troops = deploy_stage(game, cfg, first=stages == 0)
        except DeploymentCountError:
            # Preserve this phase before waiting changes the screen. No further
            # deployments or abilities after an uncertain input effect.
            log.exception("Builder deployment stopped; waiting for the natural battle result")
            if on_deployment_error:
                on_deployment_error()
            if game.wait_for(T.BB_RETURN_HOME, timeout=cfg.battle_timeout + 90, interval=2):
                _finish(game, stages + 1)
            raise
        stages += 1
        batches = [troops[i:i + cfg.ability_batch] for i in range(0, len(troops), max(cfg.ability_batch, 1))]
        next_batch = game.clock() + _later(game, cfg.ability_delay)
        hero_after = _later(game, cfg.hero_ability_after)
        ability = cfg.use_hero and cfg.hero_ability
        charges_seen = False  # the charge bars showed: fire by them, not the timer
        hero_next, hero_uses = landed, 0
        new_stage = False
        while game.clock() - started < cfg.battle_timeout:
            now = game.clock()
            if cfg.troop_abilities and batches and now >= next_batch:
                batch = batches.pop(0)
                log.debug("Troop abilities: cards %s", [s.index for s in batch])
                _fire_abilities(game, batch, cfg)
                next_batch = now + _later(game, cfg.ability_delay)
            screen = game.refresh()
            if ability and now >= hero_next:
                charges = hero_charges(screen, cfg)
                charges_seen = charges_seen or charges > 0
                timer = not charges_seen and not hero_uses and now - landed >= hero_after
                if charges >= CHARGES_TO_FIRE or timer:
                    game.tap(cfg.hero_x, cfg.card_y)
                    hero_uses += 1
                    hero_next = now + HERO_ABILITY_GAP
                    log.info("Hero ability (%s)", f"{charges} bars charged" if charges_seen else "after the time set")
            if cfg.speed_up and game.has(T.BB_SPEED) and game.click(T.BB_SPEED):
                log.info("Battle speed: 4x")
            if game.find(T.BB_RETURN_HOME):
                log.info("Builder battle over after %d stage(s)", stages)
                return _finish(game, stages)
            if now - landed > 5 and stage_reset(game):
                if not cfg.second_base and end_battle(game):
                    log.info("First base down: ending the battle there (Attack the second base too is off)")
                    return _finish(game, stages)
                log.info("First base down: on to the next one")
                _rhythm_wait(game, cfg.stage_delay)
                new_stage = True
                break
            game.wait(0.5, exact=True)  # a poll while the battle plays
        if not new_stage:
            break
    # Time's up (or something unexpected): wait for the result, then go home.
    if game.wait_for(T.BB_RETURN_HOME, timeout=90, interval=2):
        return _finish(game, stages)
    raise BuilderError("builder battle result never showed")
