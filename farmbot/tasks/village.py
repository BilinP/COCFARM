"""Home-village chores: popups, resource collection, training."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from farmbot.config import WallsConfig
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.tasks.build_list import (HUD_SHIFT, LIST_BOTTOM_FROM_BOTTOM, LIST_PAGE, LIST_STEP, LIST_SWIPE_FROM_BOTTOM,
                                      LIST_SWIPE_TO_Y, LIST_SWIPE_X, LIST_TOP, MAX_SORTED_SCROLLS, SLOW_SWIPE_MS,
                                      green_confirm, set_filter)
from farmbot.tasks.search import Loot

log = logging.getLogger(__name__)

# Buttons that dismiss interruptions, in the order they are tried.
POPUP_BUTTONS = (T.TRY_AGAIN, T.RELOAD, T.RETURN_HOME, T.BB_RETURN_HOME, T.BB_OKAY, T.CONTINUE, T.OKAY, T.CLOSE)

# The chest sits this far below the chest room's window picture (the top
# half of the window: a chest's glow covers the rest), in the picture's own heights. It opens after about four taps; tapping on once the
# reward shows skips past its Continue button, so taps stop as soon as it appears.
CHEST_BELOW_WINDOW = 3.0
CHEST_MAX_TAPS = 6
CHEST_MAX_REWARDS = 8


def open_event_chests(game: Game, limit: int = 3) -> int:
    """During events a chest can follow the battle result: tap it until the
    reward shows, then press Continue. Returns how many were opened."""
    if not game.has(T.EVENT_CHEST) or not game.has(T.CONTINUE):
        return 0
    opened = 0
    for _ in range(limit):
        room = game.wait_for(T.EVENT_CHEST, timeout=4)
        if room is None:
            break
        log.info("Opening an event chest")
        x, y = room.x, round(room.y + CHEST_BELOW_WINDOW * room.h)
        for _ in range(CHEST_MAX_TAPS):
            game.tap(x, y, jitter=5)
            if game.wait_for(T.CONTINUE, timeout=1.5):
                break
        if not game.click_when_visible(T.CONTINUE, timeout=10):
            log.info("No Continue button after the chest")
        # A chest can hold several rewards, each with its own Continue.
        for _ in range(CHEST_MAX_REWARDS):
            game.wait(1)
            if not game.click_when_visible(T.CONTINUE, timeout=3):
                break
        game.wait(2)
        opened += 1
    return opened


# "Pick a Reward!": event cards offered in battle (at 30%, 50% and 100%
# damage). The rightmost card is always taken; cards are found by their
# frames (light grey, or gold for a special one), which run their full
# height (the rewards themselves vary).
REWARD_CARDS = (150, 480)  # band below the banner centre the frames span, bot px
REWARD_FRAME_LIGHT = 190  # a frame pixel's lowest channel
REWARD_FRAME_SHARE = 0.9  # of the band a frame column is light for
REWARD_RIGHTMOST = (315, 315)  # fallback: rightmost card's centre from the banner's


def reward_cards(screen: np.ndarray, banner_x: int, banner_y: int) -> list[tuple[int, int]]:
    """Centres of the reward cards (left to right) under the banner."""
    top, bottom = banner_y + REWARD_CARDS[0], min(banner_y + REWARD_CARDS[1], screen.shape[0])
    if bottom - top < 50:
        return []
    band = screen[top:bottom]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    # Frames are light grey, or gold on a special reward.
    gold = (hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 220)
    light = ((band.min(axis=2) > REWARD_FRAME_LIGHT) | gold).mean(axis=0) >= REWARD_FRAME_SHARE
    edges = []
    xs = np.flatnonzero(light)
    for run in np.split(xs, np.flatnonzero(np.diff(xs) > 1) + 1) if xs.size else []:
        edges.append(int(run.mean()))
    # Frames come in pairs (a card's left and right side), cards 150+ px wide.
    cards = []
    i = 0
    while i + 1 < len(edges):
        if edges[i + 1] - edges[i] >= 150:
            cards.append(((edges[i] + edges[i + 1]) // 2, (top + bottom) // 2))
            i += 2
        else:
            i += 1
    return cards


REWARD_SCREENS = Path("screens/rewards")  # the last few reward screens, to check the pick
REWARD_SCREENS_KEPT = 10


def _keep_reward_screen(screen: np.ndarray) -> None:
    try:
        REWARD_SCREENS.mkdir(parents=True, exist_ok=True)
        old = sorted(REWARD_SCREENS.glob("*.png"))
        for path in old[:max(0, len(old) - REWARD_SCREENS_KEPT + 1)]:
            path.unlink()
        cv2.imwrite(str(REWARD_SCREENS / f"{datetime.now():%Y%m%d-%H%M%S}.png"), screen)
    except OSError:
        pass


def pick_reward(game: Game) -> bool:
    """On a "Pick a Reward!" screen, take the rightmost reward. True if it did."""
    if not game.has(T.PICK_REWARD):
        return False
    banner = game.find(T.PICK_REWARD)
    if banner is None:
        return False
    cards = reward_cards(game.screen, banner.x, banner.y)
    x, y = cards[-1] if cards else (banner.x + REWARD_RIGHTMOST[0], banner.y + REWARD_RIGHTMOST[1])
    log.info("Picking the rightmost reward (of %s) at x=%d", len(cards) or "?", x)
    _keep_reward_screen(game.screen)
    game.tap(x, y, jitter=10)
    game.wait(1.5)
    game.refresh()
    return True


def attack_despite_obstructed_layout(game: Game) -> bool:
    """"Defensive Layout obstructed" (now and then, after asking for a
    battle: obstacles block the defensive layout): press its Attack. True if
    it was showing."""
    if not game.has(T.LAYOUT_OBSTRUCTED) or not game.find(T.LAYOUT_OBSTRUCTED):
        return False
    log.info("Defensive layout obstructed; attacking anyway")
    if not game.click(T.OBSTRUCTED_ATTACK):
        log.warning("The obstructed-layout window's Attack button wasn't found")
        return False
    game.wait(1.0)
    return True


GEM_SCALES = (0.5, 0.6, 0.7, 0.85, 1.0, 1.2)  # gem icons in windows are drawn at many sizes
GEM_LIKENESS = 0.8
POPUP_AREA = (150, 1000)  # x range of a centred window: clear of the resource bar's own gem


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


def _confirm_is_free(game: Game) -> bool:
    """A "Confirm" window (e.g. "The Event has ended") may be pressed only
    when it costs nothing: no gem anywhere in it. Gem spends never go
    through the bot."""
    button = game.find(T.CONFIRM)
    if button is None:
        return False
    if not game.has(T.GEM):
        return False  # can't check for gems, so don't risk it
    [(_, gem)] = game.vision.library.get(T.GEM)[:1]
    if shows_gems(game.screen, 120, button.y + button.h, gem):
        log.warning("A Confirm window shows gems; leaving it alone")
        return False
    return True


def clear_popups(game: Game, rounds: int = 5) -> int:
    """Press any known dismiss/reconnect button until none is visible."""
    pressed = 0
    for _ in range(rounds):
        game.refresh()
        if pick_reward(game):
            pressed += 1
            continue
        if game.has(T.EVENT_CHEST) and game.find(T.EVENT_CHEST):
            pressed += open_event_chests(game)
            continue
        hit = next((b for b in POPUP_BUTTONS if game.has(b) and game.click(b)), None)
        if hit is None and game.has(T.CONFIRM) and _confirm_is_free(game):
            hit = T.CONFIRM if game.click(T.CONFIRM) else None
        if hit is None:
            break
        log.info("Dismissed popup via %s", hit)
        pressed += 1
        game.wait(1.5)
    return pressed


def leave_battle(game: Game) -> bool:
    """If a scouting or battle screen is still showing (say, after an error
    mid-search), end it and go home rather than failing until a restart."""
    game.refresh()
    if not game.find(T.END_BATTLE):
        return False
    log.warning("Still on a battle screen; ending it")
    game.click(T.END_BATTLE)
    # Scouting goes straight home; a started battle asks first, then shows results.
    if game.has(T.SURRENDER_OK):
        game.click_when_visible(T.SURRENDER_OK, timeout=4)
    if game.has(T.RETURN_HOME):
        game.click_when_visible(T.RETURN_HOME, timeout=10)
    game.wait(3)
    return True


COLLECTOR_PICTURES = {"gold": T.COLLECT_GOLD, "elixir": T.COLLECT_ELIXIR, "dark": T.COLLECT_DARK}
COLLECTOR_THRESHOLD = 0.8  # the bubbles sit over busy village art


def collect_resources(game: Game, kinds: tuple[str, ...] = ("gold", "elixir", "dark")) -> list[str]:
    """Empty full collectors: one tap on a bubble collects every collector
    of that kind, so each kind in ``kinds`` with a bubble showing is tapped
    once. Returns the kinds collected."""
    collected = []
    for kind in kinds:
        name = COLLECTOR_PICTURES[kind]
        if not game.has(name):
            continue
        game.refresh()
        bubble = game.find(name, threshold=COLLECTOR_THRESHOLD)
        if bubble is None:
            continue
        game.tap_match(bubble)
        game.wait(0.8)
        collected.append(kind)
    if collected:
        log.info("Collected %s", ", ".join(collected))
    return collected


WALL_ICON_OFFSET = (30, -50)  # cost icon (coin or drop) relative to an Upgrade button's text
UPGRADE_TEXT_HEIGHT = 14  # the Upgrade text's height that offset goes with (phones draw it bigger)


def _icon_offset(match, offset: tuple[int, int] = WALL_ICON_OFFSET, height: int = UPGRADE_TEXT_HEIGHT) -> tuple[int, int]:
    """``offset`` scaled to the size the Upgrade button was found at."""
    size = match.h / height
    return round(offset[0] * size), round(offset[1] * size)
GOLD_HUE, ELIXIR_HUE = (15, 35), (135, 165)


def _cost_kind(screen: np.ndarray, x: int, y: int, offset: tuple[int, int] = WALL_ICON_OFFSET) -> str | None:
    """"gold" or "elixir": the colour of the cost icon above an Upgrade button."""
    dx, dy = offset
    patch = screen[max(y + dy - 5, 0):y + dy + 5, max(x + dx - 5, 0):x + dx + 5]
    if patch.size == 0:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hue, sat, val = (float(np.median(hsv[:, :, i])) for i in range(3))
    if sat < 120 or val < 150:
        return None  # pale: a button's background, not a coin or drop ("Upgrade More" has no price)
    if GOLD_HUE[0] <= hue <= GOLD_HUE[1]:
        return "gold"
    if ELIXIR_HUE[0] <= hue <= ELIXIR_HUE[1]:
        return "elixir"
    return None


def _wall_budget(use: str, loot: Loot, keep: dict[str, int]) -> list[str]:
    """Resources that may go on walls (``use``), the one with most to spare
    above what's kept first; one without an amount to keep is left alone."""
    spare = {k: getattr(loot, k) - keep[k] for k in ("gold", "elixir") if k in keep}
    allowed = [k for k in spare if use in (k, "both") and spare[k] > 0]
    return sorted(allowed, key=lambda k: -spare[k])


# The walls in the Buildings & Heroes list (its geometry is in build_list.py).
# Unsorted, they're one block in it, dearest first ("Wall x 10 3 000 000" ...
# "Wall x 85 1 000 000"), with other buildings above and below.
MAX_WALLS = 100  # selected at once at most (as many as the spare covers, up to this)
ROW_PITCH = 4.1  # rows are this many "Wall x" heights apart (82 px at 860 x 672)
MAX_SCROLLS = 8  # long swipes to reach the walls
MAX_STEPS = 6  # short swipes to reach the block's first or last wall
ROW_BELOW = 1.6  # a row's price ends this many "Wall x" heights below its text
# A row's price, in "Wall x" heights from the middle of its "Wall x" text.
PRICE_X, PRICE_Y, PRICE_W, PRICE_H = 9.5, 0.4, 7, 1.4


def _walls_in_view(game: Game) -> list:
    """The "Wall x N" rows on show once the list has stopped gliding, top
    to bottom (two looks a moment apart agree)."""
    game.refresh()
    before = sorted(game.find_all(T.WALL_SUGGESTION), key=lambda m: m.y)
    for _ in range(4):
        game.wait(0.6)
        game.refresh()
        walls = sorted(game.find_all(T.WALL_SUGGESTION), key=lambda m: m.y)
        if [m.y // 4 for m in walls] == [m.y // 4 for m in before]:
            return walls
        before = walls
    return before


def find_wall_entry(game: Game, pick: str = "cheapest"):
    """Scroll the Buildings & Heroes list to the walls and return the row to
    tap: the block's last wall ("cheapest") or its first ("first"). ``None``
    if there are no walls or the block's end couldn't be brought into view."""
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    top, bottom = LIST_TOP, h - LIST_BOTTOM_FROM_BOTTOM
    walls = _walls_in_view(game)
    for _ in range(MAX_SCROLLS):
        if walls:
            break
        game.swipe(x, y, x, LIST_SWIPE_TO_Y)
        walls = _walls_in_view(game)
    else:
        return None
    for _ in range(MAX_STEPS):
        pitch = ROW_PITCH * walls[0].h
        if pick == "first":
            # A whole row above the top wall that isn't a wall: it's the first.
            if walls[0].y - pitch - walls[0].h > top:
                return walls[0]
            game.swipe(x, y - LIST_STEP, x, y)  # back up the list
        else:
            # A whole row below the bottom wall that isn't a wall: it's the last.
            if walls[-1].y + pitch + walls[-1].h < bottom:
                return walls[-1]
            game.swipe(x, y, x, y - LIST_STEP)
        moved = _walls_in_view(game)
        if not moved:
            return None  # went past the block: never tap where a wall used to be
        walls = moved
    return None


def first_wall(game: Game):
    """In a sorted list, scroll down to the first "Wall x N" row and return
    it once its whole row shows; ``None`` if the list ends without one."""
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    bottom = h - LIST_BOTTOM_FROM_BOTTOM
    walls = _walls_in_view(game)
    for _ in range(MAX_SORTED_SCROLLS):
        if walls and walls[0].y + round(ROW_BELOW * walls[0].h) < bottom:
            return walls[0]
        before = game.screen
        # Short swipes, slow enough not to glide: a single wall row can't be skipped.
        game.swipe(x, y, x, y - (LIST_STEP if walls else LIST_PAGE), duration_ms=SLOW_SWIPE_MS)
        walls = _walls_in_view(game)
        if before is not None and float(np.abs(game.screen.astype(np.int16) - before.astype(np.int16)).mean()) < 1:
            return walls[0] if walls else None  # the list's end
    return None


def _wall_price(game: Game, entry) -> int | None:
    """The price on a "Wall x N" row of the list (right of it, a little lower)."""
    h = entry.h
    return game.read_number(Rect(entry.x + round(PRICE_X * h), entry.y + round(PRICE_Y * h),
                                 round(PRICE_W * h), round(PRICE_H * h)), T.LOOT_FONT)


def upgrade_walls(game: Game, cfg: WallsConfig, read_resources: Callable[..., Loot | None],
                  keep: dict[str, int], full: set[str] | None = None) -> int:
    """Spend spare gold or elixir on walls, all in one go: Build → scroll to
    the cheapest "Wall x N", or the first one the list shows (``cfg.pick``;
    the game selects one of those walls) → Upgrade More until as many are
    selected as the spare covers at the row's price → Upgrade → Confirm
    (or Okay); then again in the other resource, if it has some to spare
    too (most to spare first). ``keep``: how
    much of each resource stays (not known: none of it goes). ``full``: the
    full storages, for ``only_when_full``. Returns how many walls were
    upgraded."""
    needed = (T.BUILD, T.WALL_SUGGESTION, T.UPGRADE, T.UPGRADE_CONFIRM, T.CLOSE)
    if not cfg.enabled or not all(game.has(n) for n in needed):
        return 0
    # Selecting a wall zooms in on it, and over busy buildings the resource
    # numbers can't be read: zoom out first.
    game.zoom_out()
    loot = read_resources()
    budget = _wall_budget(cfg.use, loot, keep) if loot else []
    if cfg.only_when_full:
        budget = [k for k in budget if k in (full or set())]
    if not budget:
        unknown = [k for k in ("gold", "elixir") if cfg.use in (k, "both") and k not in keep]
        log.info("Walls: %s", f"how much {' and '.join(unknown)} the storages hold isn't known yet" if unknown
                 else "nothing to spare above what's kept" + (" (or not full)" if cfg.only_when_full else ""))
        return 0
    try:
        done = 0
        for i, kind in enumerate(budget):
            if i:  # the last pass spent some: read again with the list closed
                _close_windows(game)
                game.zoom_out()
                loot = read_resources()
                if loot is None or getattr(loot, kind) <= keep[kind]:
                    continue
            done += _upgrade_walls(game, cfg, read_resources, loot, [kind], keep)
        return done
    finally:
        # Selecting a wall moved the camera to it and zoomed in: the next
        # battle's view starts from the village's, so put it back.
        _close_windows(game)
        game.zoom_out()
        game.center_view()


def _upgrade_walls(game: Game, cfg: WallsConfig, read_resources: Callable[..., Loot | None],
                   loot: Loot, budget: list[str], keep: dict[str, int]) -> int:
    game.refresh()
    if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
        log.info("Walls: the Build list didn't open")
        return 0
    game.wait(1.2)
    # "first": whichever wall the list shows first, as it is (no time spent
    # sorting it). "cheapest": sorted Cost Min, the first wall down the list
    # is the cheapest; without the filter's pictures, it's the last of the
    # walls where the game lists them by itself.
    if cfg.pick == "first":
        entry = first_wall(game)
    elif set_filter(game, sort=T.SORT_COST_MIN):
        entry = first_wall(game)
    else:
        entry = find_wall_entry(game, "cheapest")
    if entry is None:
        log.info("No wall to upgrade in the list")
        return 0
    price = _wall_price(game, entry)
    game.tap_match(entry)
    if not game.wait_for(T.UPGRADE, timeout=4):
        log.info("Walls: no Upgrade button under the wall")
        return 0
    game.wait(0.6)  # the buttons fade in
    game.refresh()
    buttons ={_cost_kind(game.screen, m.x, m.y, _icon_offset(m)): m for m in game.find_all(T.UPGRADE)}
    kind = next((k for k in budget if k in buttons), None)
    if kind is None:
        log.info("Walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
        return 0
    spare = getattr(loot, kind) - keep[kind]
    if price:
        count = min(MAX_WALLS, spare // price)
    else:  # price unread: one wall can't take much below the minimum
        log.info("Walls: couldn't read the price; upgrading one wall")
        count = 1
    if count < 1:
        log.info("Walls: %s %s spare, a wall costs %s", f"{spare:,}", kind, f"{price:,}")
        return 0
    if count > 1 and not (game.has(T.UPGRADE_MORE) and game.has(T.ADD_WALL)):
        count = 1
    if count > 1:
        count = _select_more(game, count)
    game.refresh()
    buttons = {_cost_kind(game.screen, m.x, m.y, _icon_offset(m)): m for m in game.find_all(T.UPGRADE)}
    if kind not in buttons:
        log.info("Walls: the %s Upgrade button went away", kind)
        return 0
    game.tap_match(buttons[kind])
    if not _confirm_walls(game):
        log.info("Walls: the upgrade window didn't open (Confirm %.2f, Okay %.2f alike)",
                 game.likeness(T.UPGRADE_CONFIRM), game.likeness(T.OKAY))
        return 0
    game.wait(1.5)
    # Buildings & Heroes stays open and pushes the numbers over; if they
    # can't be read there, close it (a pinch over it would land on one of
    # its rows) and read them zoomed out.
    after = read_resources(round(HUD_SHIFT * entry.h))
    if after is None:
        _close_windows(game)
        game.zoom_out()
        after = read_resources()
    if after is None:
        log.info("Upgraded walls with %s (couldn't read how many)", kind)
        return count
    spent = getattr(loot, kind) - getattr(after, kind)
    if spent <= 0:
        log.info("Wall upgrade didn't go through (no free builder?)")
        return 0
    done = round(spent / price) if price else 1
    log.info("Upgraded %d wall%s with %s (%s)", done, "" if done == 1 else "s", kind, f"{spent:,}")
    return done


def _select_more(game: Game, count: int) -> int:
    """Upgrade More switches to picking more walls of the same level: Add
    Wall +10 (left; only while ten more are left) and +1 each add that
    many to the selection. Returns how many are selected (1 if the Add Wall
    buttons didn't show: Upgrade then upgrades the one selected)."""
    if not game.click_when_visible(T.UPGRADE_MORE, timeout=3):
        log.info("Walls: no Upgrade More button (%.2f alike); upgrading one wall", game.likeness(T.UPGRADE_MORE))
        return 1
    if not game.wait_for(T.ADD_WALL, timeout=4):
        log.info("Walls: the Add Wall buttons didn't show after Upgrade More (%.2f alike); upgrading one wall",
                 game.likeness(T.ADD_WALL))
        return 1
    game.wait(0.4)
    selected = 1
    while selected < count:
        game.refresh()
        # In reading order (row by row): the buttons come after Remove Wall, +10 then +1,
        # in one row at home and over two rows in the builder base (+10 above, +1 below).
        buttons = _reading_order(game.find_all(T.ADD_WALL))
        if not buttons:
            break
        # Two buttons: +10 first, +1 last; one: +1 (fewer than ten left).
        ten = len(buttons) > 1 and count - selected >= 10
        game.tap_match(buttons[0] if ten else buttons[-1])
        selected += 10 if ten else 1
        game.wait(0.4)
    return selected


def _reading_order(matches: list) -> list:
    """Row by row (matches less than a height apart share a row), left to right."""
    rows: list[list] = []
    for m in sorted(matches, key=lambda m: m.y):
        if rows and m.y - rows[-1][0].y < m.h:
            rows[-1].append(m)
        else:
            rows.append([m])
    return [m for row in rows for m in sorted(row, key=lambda m: m.x)]


def _confirm_walls(game: Game, timeout: float = 4) -> bool:
    """Accept the window Upgrade opens: for one wall, its green Confirm
    (never the grey one that spends a Hammer); after Upgrade More, an
    "Upgrade Walls" question with Okay. Whichever shows is pressed, so it
    doesn't matter how many walls ended up selected."""
    okay = game.has(T.OKAY)
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        confirm = green_confirm(game) if game.has(T.UPGRADE_CONFIRM) else None
        if confirm:
            game.tap_match(confirm)
            return True
        if okay and game.click(T.OKAY):
            return True
        if game.clock() >= deadline:
            return False
        game.wait(0.5)


def _close_windows(game: Game) -> None:
    """Close whatever is still open: the confirm window, then Buildings & Heroes."""
    for _ in range(2):
        game.refresh()
        if not game.click(T.CLOSE):
            break
        game.wait(0.8)


CART_BELOW_LABEL = 1.9  # the cart sits this many label heights below its label


def collect_loot_cart(game: Game) -> bool:
    """Empty the loot cart (what defences won back) if its "Collect" label
    shows: tap the cart, then the Collect button under it. What doesn't
    fit in full storages stays in the cart. True if it was collected."""
    if not (game.has(T.LOOT_CART) and game.has(T.LOOT_CART_COLLECT)):
        return False
    game.refresh()
    label = game.find(T.LOOT_CART, threshold=COLLECTOR_THRESHOLD)
    if label is None:
        return False
    game.tap(label.x, label.y + round(CART_BELOW_LABEL * label.h))
    if not game.click_when_visible(T.LOOT_CART_COLLECT, timeout=3):
        log.info("Loot cart tapped but its Collect button didn't show")
        return False
    game.wait(1.5)
    log.info("Collected the loot cart")
    return True
