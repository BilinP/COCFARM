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


def _wall_budget(cfg: WallsConfig, loot: Loot) -> list[str]:
    """Resources that may go on walls, the one with most to spare first."""
    spare = {"gold": loot.gold - cfg.min_gold, "elixir": loot.elixir - cfg.min_elixir}
    allowed = [k for k in ("gold", "elixir") if cfg.use in (k, "both") and spare[k] > 0]
    return sorted(allowed, key=lambda k: -spare[k])


# Where to scroll the Buildings & Heroes list: it hangs on the screen's right
# edge, so on wider screens (phones) it's measured from there (x from the
# right edge, y from the bottom; it ends 250 from the top).
LIST_SWIPE_X = 247
LIST_SWIPE_FROM_BOTTOM = 220
LIST_SWIPE_TO_Y = 250
MAX_SCROLLS = 8


def find_wall_entry(game: Game):
    """Scroll the Buildings & Heroes list to the walls and return the lowest
    "Wall x N" entry in view: the list runs from dearest to cheapest."""
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    for _ in range(MAX_SCROLLS):
        walls = game.find_all(T.WALL_SUGGESTION)
        if walls:
            game.swipe(x, y, x, y - 150)  # a little further, for cheaper walls below
            for _ in range(3):  # the list glides on: find the walls where they stopped
                game.wait(1.0)
                game.refresh()
                walls = game.find_all(T.WALL_SUGGESTION)
                if walls:
                    return max(walls, key=lambda m: m.y)
            return None  # never tap where a wall used to be: that's another building now
        game.swipe(x, y, x, LIST_SWIPE_TO_Y)
        game.wait(1.0)
        game.refresh()
    return None


def upgrade_walls(game: Game, cfg: WallsConfig, read_resources: Callable[[], Loot | None]) -> int:
    """Spend spare gold or elixir on walls: Build → scroll to the cheapest
    "Wall x N" (the game selects one of those walls) → Upgrade in the
    resource with most to spare → Confirm. Stops at ``max_per_round``,
    when nothing is spare, or when a step doesn't show. Returns how many
    walls were upgraded."""
    needed = (T.BUILD, T.WALL_SUGGESTION, T.UPGRADE, T.UPGRADE_CONFIRM, T.CLOSE)
    if not cfg.enabled or not all(game.has(n) for n in needed):
        return 0
    done = 0
    while done < cfg.max_per_round:
        # Selecting a wall zooms in on it, and over busy buildings the
        # resource numbers can't be read: zoom back out first.
        game.zoom_out()
        loot = read_resources()
        budget = _wall_budget(cfg, loot) if loot else []
        if not budget:
            break
        game.refresh()
        if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
            log.info("Walls: the Build list didn't open")
            break
        game.wait(1.2)
        game.refresh()
        entry = find_wall_entry(game)
        if entry is None:
            log.info("No wall to upgrade in the list")
            break
        game.tap_match(entry)
        if not game.wait_for(T.UPGRADE, timeout=4):
            log.info("Walls: no Upgrade button under the wall")
            break
        buttons = {_cost_kind(game.screen, m.x, m.y, _icon_offset(m)): m for m in game.find_all(T.UPGRADE)}
        kind = next((k for k in budget if k in buttons), None)
        if kind is None:
            log.info("Walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
            break
        game.tap_match(buttons[kind])
        if not game.click_when_visible(T.UPGRADE_CONFIRM, timeout=4):
            log.info("Walls: the upgrade window didn't open")
            break
        game.wait(1.5)
        # Buildings & Heroes stays open: close it before zooming out, or the
        # pinch (a real touch on phones) lands on one of its rows.
        _close_windows(game)
        game.zoom_out()
        after = read_resources()
        if loot and after and getattr(after, kind) >= getattr(loot, kind):
            log.info("Wall upgrade didn't go through (no free builder?)")
            break
        done += 1
        log.info("Upgraded a wall with %s", kind)
    _close_windows(game)
    return done


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
