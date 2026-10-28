"""Builder base: sail over, attack, and see both stages of the battle through.

A builder battle has two bases. Taking the first to 100% moves the attack on
to a second one, with a few more troops. Each stage is played the same way:

1. The hero goes down at the map corner farthest from the base.
2. ``troop_delay`` seconds later every troop follows at the same corner,
   like a player would: select the first card, then tap the map once per
   troop with no pause (the game moves on to the next card by itself).
3. Troop abilities fire in batches (``ability_batch`` cards at a time,
   ``ability_delay`` seconds apart).
4. The hero's ability fires ``hero_ability_after`` seconds after it landed,
   once it has charged (firing early only shows a "wait" message).

The band across the top of each card says what is going on: purple = ready
to deploy, green = on the field, grey = dead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from farmbot.config import BuilderConfig
from farmbot.tasks.search import Loot
from farmbot.game import VIEW_AREA, VIEW_NEAR, VIEW_STILL, Game, T, view_change, view_shift

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


# ------------------------------------------------------------------ getting there
def in_builder_base(game: Game) -> bool:
    if not game.has(T.BB_BATTLE):
        return False
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
FIRST_BASE_DRAGS = 8


def _find_scaled(game: Game, name: str) -> tuple[int, int] | None:
    """Centre of ``name`` on screen at whatever size it shows, or ``None``."""
    from farmbot.vision import best_scale, match_template, _resized

    game.refresh()
    match = game.find(name)
    if match is not None:
        return match.x, match.y
    for _, image in game.vision.library.get(name):
        scale, score = best_scale(game.screen, image, list(BOAT_SIZES))
        if score >= BOAT_THRESHOLD:
            found = match_template(game.screen, _resized(image, scale), BOAT_THRESHOLD)
            if found:
                return found[0].x, found[0].y
    return None


def go_to_builder_base(game: Game) -> bool:
    """Sail from the home village to the builder base. True once there."""
    if in_builder_base(game):
        return True
    game.zoom_out()
    boat = _find_scaled(game, T.BB_HOME_BOAT)
    if boat is None:
        raise BuilderError("boat to the builder base not visible; is the home village showing?")
    game.tap(*boat)
    if not game.wait_for(T.BB_BATTLE, timeout=20):
        raise BuilderError("builder base didn't load")
    log.info("Sailed to the builder base")
    return True


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
    if boat is None:
        log.info("Boat to the home village not visible")
        return False
    game.tap(*boat)
    if not game.wait_for(T.BATTLE, timeout=20):
        log.info("Home village didn't load")
        return False
    log.info("Sailed back to the home village")
    return True


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
                      kinds: tuple[str, ...] = ("gold", "elixir", "gems")) -> list[str]:
    """Empty the builder base's full gold mines, elixir collectors and gem
    mine (of ``kinds``: leave out a full storage's, whose collectors can't
    be emptied) by tapping their bubbles (never the elixir cart's tag), on
    the first base and then, with ``second_base``, the second one, after
    which the view is put back. Returns what was collected."""
    game.zoom_out()
    view = game.refresh().copy()
    collected = _collect_in_view(game, tries, kinds)
    if second_base and {"gold", "elixir"} & set(kinds):  # the gem mine is on the first base
        _to_second_base(game)
        collected += [k for k in _collect_in_view(game, tries, kinds) if k not in collected]
        restore_view(game, view)
    if collected:
        log.info("Collected builder base %s", ", ".join(collected))
    return collected


def _to_second_base(game: Game) -> None:
    """Drag the view up and left until it stops: the second base."""
    x1, y1, x2, y2, ms = FIRST_BASE_DRAG
    before = game.refresh().copy()
    for _ in range(FIRST_BASE_DRAGS):
        game.swipe(x2, y2, x1, y1, ms)
        game.wait(1)
        if view_change(before, game.refresh()) < VIEW_STILL:
            break
        before = game.screen.copy()


def _collect_in_view(game: Game, tries: int, kinds: tuple[str, ...] = ("gold", "elixir", "gems")) -> list[str]:
    collected = []
    for kind, name in (("gold", T.BB_COLLECT_GOLD), ("elixir", T.BB_COLLECT_ELIXIR), ("gems", T.BB_COLLECT_GEMS)):
        if kind not in kinds or not game.has(name):
            continue
        tapped: tuple[int, int] | None = None
        for _ in range(tries):
            game.refresh()
            spots = [m.point for m in game.find_all(name, threshold=BUBBLE_THRESHOLD)]
            spots += [(x, y) for tag, x, y in full_tags(game.screen) if tag == kind]  # full: a red tag
            spots = [p for p in spots if not _in_cart_area(*p)]
            if not spots:
                break
            if tapped and min(np.hypot(x - tapped[0], y - tapped[1]) for x, y in spots) <= BUBBLE_STAYED:
                # Still there after a tap: its storage is full (or it isn't a bubble). Tapping
                # again only selects the building, and a drag starting on it would move it.
                log.info("Builder base %s: the bubble stayed (storage full?)", kind)
                break
            tapped = spots[0]
            game.tap(*tapped, jitter=3)
            game.wait(1.0)
            if kind not in collected:
                collected.append(kind)
    return collected


def collect_elixir_cart(game: Game) -> bool:
    """Open the elixir cart by the dock and press Collect (only while it's
    green: grey means nothing to collect). True if it was collected."""
    if not (game.has(T.BB_COLLECT_ELIXIR) and game.has(T.BB_CART_COLLECT)):
        return False
    game.zoom_out()
    game.refresh()
    # Its tag: an elixir drop (as on a collector's bubble), or, once it's full, the drop on red.
    tags = [m.point for m in game.find_all(T.BB_COLLECT_ELIXIR, threshold=0.75) if _in_cart_area(m.x, m.y)]
    tags += [(x, y) for kind, x, y in full_tags(game.screen) if kind == "elixir" and _in_cart_area(x, y)]
    if not tags:
        log.info("Elixir cart: nothing to collect")
        return False
    game.tap(*tags[0], jitter=3)
    button = game.wait_for(T.BB_CART_COLLECT, timeout=4)
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


# ------------------------------------------------------------------ walls
BB_ICON_OFFSET = (39, -65)  # cost icon relative to the builder Upgrade button's text


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
                  keep: dict[str, int]) -> int:
    """Spend spare builder gold and elixir on builder base walls, all in one
    go as at home: Build → scroll to the cheapest "Wall x N" → Upgrade More
    until as many are selected as the spare covers at the row's price →
    Upgrade → Okay (one wall: the upgrade window's green price button);
    then again in the other resource, if it has some to spare too.
    ``keep``: how much of each resource stays. Returns how many walls were
    upgraded."""
    from farmbot.tasks.village import _wall_budget

    needed = (T.BUILD, T.WALL_SUGGESTION, T.BB_UPGRADE, T.BB_UPGRADE_TITLE, T.CLOSE)
    if not cfg.walls or not all(game.has(n) for n in needed):
        return 0
    game.zoom_out()
    loot = read_resources()
    budget = _wall_budget(cfg.walls_use, loot, keep) if loot else []
    if not budget:
        log.info("Builder walls: %s", "nothing to spare above what's kept" if keep else
                 "how much the storages hold isn't known yet")
        return 0
    view = game.refresh().copy()  # selecting a wall moves the camera onto it
    try:
        done = 0
        for i, kind in enumerate(budget):
            if i:  # the last pass spent some: read again with the list closed
                _close_all(game)
                game.zoom_out()
                loot = read_resources()
                if loot is None or getattr(loot, kind) <= keep[kind]:
                    continue
            done += _upgrade_walls(game, read_resources, loot, [kind], keep)
        return done
    finally:
        _close_all(game)
        # The map holds two bases: put the camera back, or the boat home
        # (and the elixir cart) can be out of sight.
        game.zoom_out()
        restore_view(game, view)


def _close_all(game: Game) -> None:
    """Leave nothing open, and no wall selected (see ``village.deselect``)."""
    from farmbot.tasks.village import deselect

    for _ in range(2):
        game.refresh()
        if not game.click(T.CLOSE):
            break
        game.wait(0.8)
    deselect(game)


def _upgrade_walls(game: Game, read_resources: Callable[[], Loot | None], loot: Loot, budget: list[str],
                   keep: dict[str, int]) -> int:
    from farmbot.tasks.village import MAX_WALLS, _cost_kind, _select_more, _wall_price, find_wall_entry

    if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
        log.info("Builder walls: the Build list didn't open")
        return 0
    game.wait(1.2)
    entry = find_wall_entry(game)
    if entry is None:
        log.info("No builder base wall to upgrade in the list")
        return 0
    price = _wall_price(game, entry)
    game.tap_match(entry)
    if not game.wait_for(T.BB_UPGRADE, timeout=4):
        log.info("Builder walls: no Upgrade button under the wall")
        return 0
    game.wait(0.6)  # the buttons fade in
    game.refresh()
    buttons = {_cost_kind(game.screen, m.x, m.y, BB_ICON_OFFSET): m for m in game.find_all(T.BB_UPGRADE)}
    kind = next((k for k in budget if k in buttons), None)
    if kind is None:
        log.info("Builder walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
        return 0
    spare = getattr(loot, kind) - keep[kind]
    count = min(MAX_WALLS, spare // price) if price else 1
    if count < 1:
        log.info("Builder walls: %s %s spare, a wall costs %s", f"{spare:,}", kind, f"{price:,}")
        return 0
    if count > 1 and game.has(T.UPGRADE_MORE) and game.has(T.ADD_WALL):
        count = _select_more(game, count)
    elif count > 1:
        count = 1
    game.refresh()
    buttons = {_cost_kind(game.screen, m.x, m.y, BB_ICON_OFFSET): m for m in game.find_all(T.BB_UPGRADE)}
    if kind not in buttons:
        log.info("Builder walls: the %s Upgrade button went away", kind)
        return 0
    game.tap_match(buttons[kind])
    if not _confirm_walls(game):
        log.info("Builder walls: the upgrade window didn't open")
        return 0
    game.wait(1.5)
    _close_all(game)  # the upgrade window and Buildings & Heroes, and the walls' selection
    game.zoom_out()
    after = read_resources()
    if after is None:
        log.info("Upgraded builder base walls with %s (couldn't read how many)", kind)
        return count
    spent = getattr(loot, kind) - getattr(after, kind)
    if spent <= 0:
        log.info("Builder wall upgrade didn't go through (not enough loot, or no free builder?)")
        return 0
    done = round(spent / price) if price else 1
    log.info("Upgraded %d builder base wall%s with %s (%s)", done, "" if done == 1 else "s", kind, f"{spent:,}")
    return done


def _confirm_walls(game: Game, timeout: float = 4) -> bool:
    """Accept the window Upgrade opened: after Upgrade More, an "Upgrade
    Walls" question with Okay; for one wall, the upgrade window's green
    price button."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        if game.has(T.OKAY) and game.click(T.OKAY):
            return True
        title = game.find(T.BB_UPGRADE_TITLE)
        if title is not None:
            button = _green_button(game.screen, (title.x - 360, title.y + 450, title.x + 600, title.y + 720))
            if button is not None:
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
    if 100 <= h <= 125 and s > 120 and 60 <= v <= 170:
        return "ready"  # survived the first base: carried over to the second
    return "empty"


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
def start_battle(game: Game) -> None:
    """Builder base → Battle → Find Now! → the battle screen."""
    from farmbot.tasks.village import clear_popups

    for attempt in range(3):
        game.refresh()
        if game.find(T.BB_FIND_NOW):  # the Start Attack window is already open
            game.click(T.BB_FIND_NOW)
            break
        clear_popups(game)  # the Star Bonus window shows after some battles
        game.refresh()
        # Bottom left only: the Start Attack window has the same axes on top.
        if not game.click(T.BB_BATTLE, region=game.region(BATTLE_AREA)):
            raise BuilderError("builder base Battle button not visible")
        if game.click_when_visible(T.BB_FIND_NOW, timeout=6):
            break
        log.info("Find Now! didn't show; trying again")
    else:
        raise BuilderError("Find Now! didn't show")
    from farmbot.tasks.village import attack_despite_obstructed_layout

    deadline = game.clock() + 60
    while True:
        game.refresh()
        if game.find(T.BB_BATTLE_STARTS) or game.find(T.BB_BATTLE_ENDS):
            break
        if attack_despite_obstructed_layout(game):
            continue
        if game.clock() >= deadline:
            raise BuilderError("no opponent found")
        game.wait(1.0)
    game.wait(1.0)


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
    game.zoom_out()
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
    if cfg.use_hero and not hero_out(screen, cfg):
        found = place_first(game, hero, point, centre, lambda s: hero_out(s, cfg), cfg)
        if found is None:
            log.warning("The hero couldn't be placed")
        else:
            point = found
        landed = game.clock()
        game.wait(max(cfg.troop_delay - 0.6, 0))
    elif troops:
        # No hero to find a good spot with: the first troop does it.
        first = troops[0]
        found = place_first(game, first, point, centre, lambda s: card_state(s, first.x, cfg) != "ready", cfg)
        if found is not None:
            point = found
        troops = ready_troops(game.refresh(), cfg)
    sent: list[Slot] = []
    for _ in range(2):  # a second pass for cards whose tap was dropped
        if not troops:
            break
        # Select the first card; each tap on the map sends the selected
        # troop and the game selects the next card. The pause is the troop speed.
        _tap_card(game, troops[0], cfg)
        game.tap_many([point] * len(troops), jitter=6, pause=cfg.tap_delay)
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


def _finish(game: Game, stages: int) -> BattleResult:
    """Keep the result screen, then Return Home."""
    result = BattleResult(stages, None if game.screen is None else game.screen.copy())
    game.click(T.BB_RETURN_HOME)
    game.wait_for(T.BB_BATTLE, timeout=15)
    return result


def attack(game: Game, cfg: BuilderConfig, on_stage: Callable[[int], None] | None = None) -> BattleResult:
    """One builder battle, every stage, back to the builder base.
    ``on_stage`` hears each base as it begins (1, 2)."""
    start_battle(game)
    stages = 0
    started = game.clock()
    while stages < MAX_STAGES:
        if on_stage:
            on_stage(stages + 1)
        landed, troops = deploy_stage(game, cfg, first=stages == 0)
        stages += 1
        batches = [troops[i:i + cfg.ability_batch] for i in range(0, len(troops), max(cfg.ability_batch, 1))]
        next_batch = game.clock() + cfg.ability_delay
        hero_fired = not (cfg.use_hero and cfg.hero_ability)
        new_stage = False
        while game.clock() - started < cfg.battle_timeout:
            now = game.clock()
            if cfg.troop_abilities and batches and now >= next_batch:
                batch = batches.pop(0)
                log.debug("Troop abilities: cards %s", [s.index for s in batch])
                _fire_abilities(game, batch, cfg)
                next_batch = now + cfg.ability_delay
            if not hero_fired and now - landed >= cfg.hero_ability_after:
                game.tap(cfg.hero_x, cfg.card_y)
                hero_fired = True
                log.info("Hero ability")
            screen = game.refresh()
            if cfg.speed_up and game.has(T.BB_SPEED) and game.click(T.BB_SPEED):
                log.info("Battle speed: 4x")
            if game.find(T.BB_RETURN_HOME):
                log.info("Builder battle over after %d stage(s)", stages)
                return _finish(game, stages)
            if now - landed > 5 and stage_reset(game):
                log.info("First base down: on to the next one")
                game.wait(cfg.stage_delay)
                new_stage = True
                break
            game.wait(0.5)
        if not new_stage:
            break
    # Time's up (or something unexpected): wait for the result, then go home.
    if game.wait_for(T.BB_RETURN_HOME, timeout=90, interval=2):
        return _finish(game, stages)
    raise BuilderError("builder battle result never showed")
