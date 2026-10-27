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
from dataclasses import dataclass, replace
from typing import Callable

import cv2
import numpy as np

from farmbot.config import BuilderConfig
from farmbot.tasks.search import Loot
from farmbot.game import Game, T

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
HOME_BOAT_DRAG = (500, 560, 640, 220, 600)  # pulls the home village's dock (bottom left) up into view


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
        # On short screens (phones) the dock, bottom left, can sit off screen
        # or under the bottom buttons: drag the map up into view.
        game.swipe(*HOME_BOAT_DRAG)
        game.wait(1)
        boat = _find_scaled(game, T.BB_HOME_BOAT)
    if boat is None:
        raise BuilderError("boat to the builder base not visible; is the home village showing?")
    game.tap(*boat)
    if not game.wait_for(T.BB_BATTLE, timeout=20):
        raise BuilderError("builder base didn't load")
    log.info("Sailed to the builder base")
    return True


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
        log.info("Boat to the home village not visible")
        return False
    game.tap(*boat)
    if not game.wait_for(T.BATTLE, timeout=20):
        log.info("Home village didn't load")
        return False
    log.info("Sailed back to the home village")
    return True


# ------------------------------------------------------------------ collecting
def _in_cart_area(x: int, y: int) -> bool:
    x0, y0, x1, y1 = CART_AREA
    return x0 <= x <= x1 and y0 <= y <= y1


def collect_resources(game: Game, tries: int = 4) -> list[str]:
    """Empty the builder base's full gold mines, elixir collectors and gem
    mine by tapping their bubbles (never the elixir cart's tag). Returns what
    was collected."""
    collected = []
    game.zoom_out()
    for kind, name in (("gold", T.BB_COLLECT_GOLD), ("elixir", T.BB_COLLECT_ELIXIR), ("gems", T.BB_COLLECT_GEMS)):
        if not game.has(name):
            continue
        for _ in range(tries):
            game.refresh()
            bubbles = [m for m in game.find_all(name, threshold=BUBBLE_THRESHOLD) if not _in_cart_area(m.x, m.y)]
            if not bubbles:
                break
            game.tap_match(bubbles[0])
            game.wait(1.0)
            if kind not in collected:
                collected.append(kind)
    if collected:
        log.info("Collected builder base %s", ", ".join(collected))
    return collected


def collect_elixir_cart(game: Game) -> bool:
    """Open the elixir cart by the dock and press Collect (only while it's
    green: grey means nothing to collect). True if it was collected."""
    if not (game.has(T.BB_COLLECT_ELIXIR) and game.has(T.BB_CART_COLLECT)):
        return False
    game.zoom_out()
    game.refresh()
    tags = [m for m in game.find_all(T.BB_COLLECT_ELIXIR, threshold=0.75) if _in_cart_area(m.x, m.y)]
    if not tags:
        log.info("Elixir cart: nothing to collect")
        return False
    game.tap_match(tags[0])
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


def upgrade_walls(game: Game, cfg: BuilderConfig, read_resources: Callable[[], Loot | None]) -> int:
    """Spend spare builder gold or elixir on builder base walls: Build →
    scroll to the cheapest "Wall x N" → Upgrade → the green price button.
    Returns how many walls were upgraded."""
    from farmbot.config import WallsConfig
    from farmbot.tasks.village import _cost_kind, _wall_budget, find_wall_entry

    needed = (T.BUILD, T.WALL_SUGGESTION, T.BB_UPGRADE, T.BB_UPGRADE_TITLE, T.CLOSE)
    if not cfg.walls or not all(game.has(n) for n in needed):
        return 0
    budget_cfg = WallsConfig(enabled=True, use=cfg.walls_use, min_gold=cfg.walls_min_gold,
                             min_elixir=cfg.walls_min_elixir)
    done = 0
    while done < cfg.walls_per_round:
        game.zoom_out()
        loot = read_resources()
        budget = _wall_budget(budget_cfg, loot) if loot else []
        if not budget:
            break
        game.refresh()
        if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
            log.info("Builder walls: the Build list didn't open")
            break
        game.wait(1.2)
        game.refresh()
        entry = find_wall_entry(game)
        if entry is None:
            log.info("No builder base wall to upgrade in the list")
            break
        game.tap_match(entry)
        if not game.wait_for(T.BB_UPGRADE, timeout=4):
            log.info("Builder walls: no Upgrade button under the wall")
            break
        game.wait(0.6)  # the buttons fade in
        game.refresh()
        buttons = {_cost_kind(game.screen, m.x, m.y, BB_ICON_OFFSET): m for m in game.find_all(T.BB_UPGRADE)}
        kind = next((k for k in budget if k in buttons), None)
        if kind is None:
            log.info("Builder walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
            break
        game.tap_match(buttons[kind])
        title = game.wait_for(T.BB_UPGRADE_TITLE, timeout=4)
        if title is None:
            log.info("Builder walls: the upgrade window didn't open")
            break
        button = _green_button(game.screen, (title.x - 360, title.y + 450, title.x + 600, title.y + 720))
        if button is None:
            log.info("Builder walls: no price button in the upgrade window")
            break
        game.tap(*button)
        game.wait(1.5)
        for _ in range(2):  # the upgrade window and Buildings & Heroes
            game.refresh()
            if not game.click(T.CLOSE):
                break
            game.wait(0.8)
        game.zoom_out()
        after = read_resources()
        if loot and after and getattr(after, kind) >= getattr(loot, kind):
            log.info("Builder wall upgrade didn't go through (not enough loot, or no free builder?)")
            break
        done += 1
        log.info("Upgraded a builder base wall with %s", kind)
    for _ in range(2):  # leave nothing open
        game.refresh()
        if not game.click(T.CLOSE):
            break
        game.wait(0.8)
    return done


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
# starts this far left of the card's centre and is this long when full. It sits lower before the hero is
# placed (between the two bases) than once it's on the field.
BAR_INSET, BAR_LENGTH = 35, 72
SWAP_BUTTON = (17, 58)  # the green swap button, relative to (hero_x, card_y)
PICKER_SLOTS = ((-35, 83), (63, 83), (161, 83))  # hero portraits relative to the "Change Heroes" title


def hero_health(screen: np.ndarray, cfg: BuilderConfig) -> float | None:
    """The hero's health (0-1) from the bar over its card: 0 if the card is
    greyed out (dead), ``None`` if there's no bar (not hurt yet)."""
    x, y = cfg.hero_x, cfg.card_y
    card = cv2.cvtColor(screen[y - 10:y + 40, x - 30:x + 30], cv2.COLOR_BGR2HSV)
    if card.size and float(np.median(card[:, :, 1])) < 30:
        return 0.0
    left = x - BAR_INSET
    band = cv2.cvtColor(screen[y - 70:y - 18, max(left - 6, 0):left + BAR_LENGTH + 6], cv2.COLOR_BGR2HSV)
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


# ------------------------------------------------------------------ the screen
PC_SIZE = (1177, 920)  # the 860 x 672 screen the positions here were measured on (bot coordinates)
HERO_ART_DROP = 7.5  # the hero card's art sits this far below the card's centre (card_y)
CARD_SIZES = np.arange(0.9, 1.21, 0.01)
CARD_MATCH = 0.8  # the hero's card art scores 0.96+ on phones and tablets
# Phones and tablets: the map's corners, from the base's centre (bot
# coordinates at 860 x 672, times the map's size), kept inside the part of
# the screen that holds no buttons (Surrender bottom left, the damage
# bottom right, the timer and names at the top).
BASE_TO_CORNER = {"left": (-395, 13), "top": (0, -290), "right": (400, 0)}
SAFE_SIDES, SAFE_TOP, SAFE_ABOVE_BAR = (200, 230), 100, 40


@dataclass
class Layout:
    """Where a builder battle's parts are on this screen. The constants
    here were measured on the PC's 860 x 672 screen; phones and tablets draw
    the troop bar a little bigger (1.03-1.06x, centred) and show the map at
    their own size and place, which moves with each zoom."""

    cfg: BuilderConfig  # with the troop bar where it is on this screen
    size: tuple[int, int]  # the screen (bot coordinates)
    map_scale: float = 1.0  # the map's size relative to the PC's
    bar_top: int = 780  # the troop bar's top edge

    @property
    def pc(self) -> bool:
        """The PC's screen: the map's corners are where they were measured."""
        w, h = self.size
        return abs(w / h - PC_SIZE[0] / PC_SIZE[1]) < 0.05


def battle_layout(game: Game, cfg: BuilderConfig) -> Layout:
    """This screen's layout, from the hero's card art in the troop bar."""
    screen = game.refresh()
    h, w = screen.shape[:2]
    layout = Layout(cfg, (w, h))
    if layout.pc:
        return layout
    best = (0.0, 1.0, (0.0, 0.0))
    for _, art in [i for key in HEROES if game.has(HEROES[key]) for i in game.vision.library.get(HEROES[key])]:
        area = screen[h // 2:, :w // 2]
        for k in CARD_SIZES:
            t = cv2.resize(art, None, fx=k, fy=k)
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(area, t, cv2.TM_CCOEFF_NORMED))
            if score > best[0]:
                best = (score, float(k), (x + t.shape[1] / 2, h // 2 + y + t.shape[0] / 2))
    score, k, (x, y) = best
    if score >= CARD_MATCH:
        hero_x, card_y = x, y - HERO_ART_DROP * k
    else:  # no hero card (none picked): as phones draw the bar, centred
        k = 1.05
        hero_x, card_y = w / 2 + k * (cfg.hero_x - PC_SIZE[0] / 2), h - k * (PC_SIZE[1] - cfg.card_y)
    log.info("Troop bar at %.2fx, the hero's card at (%d, %d)", k, hero_x, card_y)
    layout.cfg = replace(cfg, hero_x=round(hero_x), card_y=round(card_y), card_step=cfg.card_step * k,
                         first_troop_x=round(hero_x + k * (cfg.first_troop_x - cfg.hero_x)))
    layout.map_scale = max(h / PC_SIZE[1], w / 1605)  # how far the game zooms out: by height, or for wide screens by width
    layout.bar_top = round(card_y - k * (cfg.card_y - 780))
    return layout


# ------------------------------------------------------------------ placement
def base_centre(screen: np.ndarray, layout: Layout | None = None) -> tuple[int, int]:
    """Where the enemy buildings are thickest: bright or warm pixels on the
    dull teal ground, inside the map."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((v > 150) | ((s > 120) & ((h < 35) | (h > 130)))).astype(np.uint8) * 255
    inside = np.zeros_like(mask)
    if layout is None or layout.pc:
        cv2.fillPoly(inside, [MAP], 255)
        boost = 650
    else:  # where the map is isn't known: all of it that holds no buttons
        (left, right), top = SAFE_SIDES, SAFE_TOP
        inside[top:layout.bar_top, left:screen.shape[1] - right] = 255
        boost = layout.bar_top - round(140 * layout.map_scale)
    mask &= inside
    mask[boost:] = 0  # the Boost buttons
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    density = cv2.blur(mask.astype(np.float32), (121, 91))
    y, x = np.unravel_index(int(np.argmax(density)), density.shape)
    return int(x), int(y)


def corners(screen: np.ndarray, layout: Layout | None = None) -> dict[str, tuple[int, int]]:
    """Where to go in at each corner. On phones and tablets: out from the
    base, inside the part of the screen without buttons (a point off the
    map's ground is refused and moved in towards the base)."""
    if layout is None or layout.pc:
        return CORNERS
    cx, cy = base_centre(screen, layout)
    w = screen.shape[1]
    (left, right), top = SAFE_SIDES, SAFE_TOP
    return {name: (int(np.clip(cx + dx * layout.map_scale, left, w - right)),
                   int(np.clip(cy + dy * layout.map_scale, top, layout.bar_top - SAFE_ABOVE_BAR)))
            for name, (dx, dy) in BASE_TO_CORNER.items()}


def pick_corner(screen: np.ndarray, cfg: BuilderConfig, layout: Layout | None = None) -> tuple[str, tuple[int, int]]:
    """The configured corner, or (``auto``) the one farthest from the base."""
    points = corners(screen, layout)
    if cfg.corner in points:
        return cfg.corner, points[cfg.corner]
    cx, cy = base_centre(screen, layout)
    name = max(points, key=lambda k: np.hypot(points[k][0] - cx, (points[k][1] - cy) / ISO_RATIO))
    return name, points[name]


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
        if banner(game, T.BB_BATTLE_STARTS) or banner(game, T.BB_BATTLE_ENDS):
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


def deploy_stage(game: Game, cfg: BuilderConfig, first: bool = False,
                 layout: Layout | None = None) -> tuple[float, list[Slot]]:
    """Hero at the far corner, then (``troop_delay`` s later) every ready
    troop there too. Returns when the hero landed and the troops sent.
    On the ``first`` base, the hero is set to ``cfg.first_hero`` first."""
    game.zoom_out()
    if first and cfg.use_hero and cfg.first_hero in HEROES:
        choose_hero(game, cfg, cfg.first_hero)
    screen = game.refresh()
    side, point = pick_corner(screen, cfg, layout)
    centre = base_centre(screen, layout)
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
# The timer's caption, top middle: a long line of text, which only matches
# within a hundredth or two of its size (0.97x on one phone scores 0.91,
# 0.99x 0.58), so its sizes are tried closely, around the top middle only,
# and (until it has shown once and its size is known) every few seconds.
BANNER_AREA = (80, 260)  # px from the top; px either side of the middle
BANNER_SIZES = np.arange(0.84, 1.33, 0.01)
BANNER_SEARCH_EVERY = 4.0  # seconds
_banner_sizes: dict[str, float] = {}  # picture -> the size it showed at
_banner_searched: dict[str, float] = {}  # banner -> when its sizes were last all tried


def banner(game: Game, name: str, threshold: float | None = None) -> bool:
    """``name`` ("Battle starts in" or "Battle ends in") is showing."""
    from farmbot.vision import _resized, _score

    if not game.has(name):
        return False
    threshold = game.vision.default_threshold if threshold is None else threshold
    screen = game.screen if game.screen is not None else game.refresh()
    middle = screen.shape[1] // 2
    strip = screen[:BANNER_AREA[0], max(middle - BANNER_AREA[1], 0):middle + BANNER_AREA[1]]
    pictures = game.vision.library.get(name)
    for picture, image in pictures:
        known = _banner_sizes.get(picture)
        if known is not None and _score(strip, _resized(image, known)) >= threshold:
            return True
    if any(picture in _banner_sizes for picture, _ in pictures)             or game.clock() - _banner_searched.get(name, -BANNER_SEARCH_EVERY) < BANNER_SEARCH_EVERY:
        return False
    _banner_searched[name] = game.clock()
    for picture, image in pictures:
        score, size = max((_score(strip, _resized(image, k)), float(k)) for k in BANNER_SIZES)
        if score >= threshold:
            _banner_sizes[picture] = size
            return True
    return False


def stage_reset(game: Game) -> bool:
    """The first base fell and the second began: its "Battle starts in"
    countdown is up. (The cards alone can't tell: between the bases the
    survivors' cards look much like ready ones.)"""
    return banner(game, T.BB_BATTLE_STARTS, BANNER_THRESHOLD)


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
    layout = battle_layout(game, cfg)
    cfg = layout.cfg
    stages = 0
    started = game.clock()
    while stages < MAX_STAGES:
        if on_stage:
            on_stage(stages + 1)
        landed, troops = deploy_stage(game, cfg, first=stages == 0, layout=layout)
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
