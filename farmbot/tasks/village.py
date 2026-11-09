"""Home-village chores: popups, resource collection, training."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from farmbot import imagefile
from farmbot.config import WallsConfig
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.tasks.build_list import (LIST_BOTTOM_FROM_BOTTOM, LIST_PAGE, LIST_STEP, LIST_SWIPE_FROM_BOTTOM,
                                      LIST_SWIPE_TO_Y, LIST_SWIPE_X, LIST_TOP, MAX_SORTED_SCROLLS, SLOW_SWIPE_MS,
                                      asks_for_gems, free_builders, green_confirm, list_still, open_tab, set_filter,
                                      shows_gems)
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
WALL_SCREENS = Path("screens/walls")  # the last few walls that showed no Upgrade button
SCREENS_KEPT = 10


def _keep_screen(folder: Path, screen: np.ndarray | None) -> None:
    """Keep ``screen`` in ``folder`` (the last SCREENS_KEPT of them), for a look later."""
    if screen is None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        old = sorted(folder.glob("*.png"))
        for path in old[:max(0, len(old) - SCREENS_KEPT + 1)]:
            path.unlink()
        imagefile.write(str(folder / f"{datetime.now():%Y%m%d-%H%M%S}.png"), screen)
    except OSError:
        pass


def _keep_reward_screen(screen: np.ndarray) -> None:
    _keep_screen(REWARD_SCREENS, screen)


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


def close_rate_dialog(game: Game) -> bool:
    """Google Play's "Rate Clash of Clans" dialog covers the game, whose
    buttons don't answer while it's open: NEVER closes it for good."""
    if not (game.has(T.RATE_TITLE) and game.has(T.RATE_NEVER) and game.find(T.RATE_TITLE)):
        return False
    if not game.click(T.RATE_NEVER):
        return False
    log.info("Closed Google Play's \"Rate Clash of Clans\" dialog (Never)")
    game.wait(1)
    return True


AD_CLOSE_RIGHT = 0.74  # an ad's X: this many "Remove Ads" badge widths right of the badge's middle


def close_ad(game: Game) -> bool:
    """An ad in the game ("Remove Ads" at its top right): close it with the
    small X beside that badge. Its Okay and its picture both open the
    advertised game in the Play Store. True if one was closed."""
    if not game.has(T.REMOVE_ADS):
        return False
    badge = game.find(T.REMOVE_ADS)
    if badge is None:
        return False
    log.info("Closing an ad with its X (never its Okay, which opens the ad)")
    game.tap(badge.x + round(AD_CLOSE_RIGHT * badge.w), badge.y)
    game.wait(1.5)
    return True


def clear_popups(game: Game, rounds: int = 5) -> int:
    """Press any known dismiss/reconnect button until none is visible."""
    pressed = 0
    for _ in range(rounds):
        game.refresh()
        if close_ad(game):  # before Okay, which is on ads too
            pressed += 1
            continue
        if pick_reward(game):
            pressed += 1
            continue
        if close_rate_dialog(game):
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
PRICE_RECHECK = 10  # rounds skipped for the price last seen before the list is looked at again
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
    for _ in range(5):
        game.wait(0.4)  # (and ~0.2 s for the screenshot)
        game.refresh()
        walls = sorted(game.find_all(T.WALL_SUGGESTION), key=lambda m: m.y)
        if [m.y // 4 for m in walls] == [m.y // 4 for m in before]:
            return walls
        before = walls
    return before


def find_wall_entry(game: Game):
    """Scroll the Buildings & Heroes list to the walls and return the row to
    tap: the block's last wall, the cheapest. ``None`` if there are no walls
    or the block's end couldn't be brought into view."""
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
        if list_still(before, game.screen):
            return walls[0] if walls else None  # the list's end
    return None


def _price_rect(entry) -> Rect:
    h = entry.h
    return Rect(entry.x + round(PRICE_X * h), entry.y + round(PRICE_Y * h), round(PRICE_W * h), round(PRICE_H * h))


def _wall_price(game: Game, entry) -> int | None:
    """The price on a "Wall x N" row of the list (right of it, a little lower)."""
    return game.read_number(_price_rect(entry), T.LOOT_FONT)


RED_PRICE_SHARE = 0.05  # of the price's box in red digits: more than there is to pay with


def price_is_red(game: Game, entry) -> bool:
    """True if the row's price shows in the game's red: there isn't enough
    to pay it (the digits are white when there is)."""
    r = _price_rect(entry)
    box = game.screen[max(r.y, 0):r.y + r.h, max(r.x, 0):r.x + r.w].astype(np.int16)
    if box.size == 0:
        return False
    b, g, red = box[..., 0], box[..., 1], box[..., 2]
    return float(((red > 200) & (red - g > 80) & (red - b > 80)).mean()) > RED_PRICE_SHARE


def upgrade_walls(game: Game, cfg: WallsConfig, read_resources: Callable[..., Loot | None],
                  keep: dict[str, int], full: set[str] | None = None, known: dict | None = None,
                  last: bool = False, handed: dict | None = None) -> int:
    """Spend spare gold or elixir on walls, all in one go: Build → scroll to
    the cheapest "Wall x N" (the game selects one of those walls) → Upgrade More until as many are
    selected as the spare covers at the row's price → Upgrade → Confirm
    (or Okay); then again in the other resource, if it has some to spare
    too (most to spare first), in the list still open. ``keep``: how
    much of each resource stays (not known: none of it goes). ``full``: the
    full storages, for ``only_when_full``. ``known``: kept between calls,
    it learns the wall's price, so the list isn't opened while the spare
    can't pay for one wall. ``last``: a battle comes next, which zooms out
    and centres its own view, so the walls are left as they are (selected,
    zoomed in on). ``handed``: the upgrades just before left Buildings &
    Heroes open (see upgrade.upgrade), with the resources left and the free
    builders: the walls go on in it, rather than closing it and opening it
    again. Returns how many walls were upgraded."""
    list_open = bool(handed)
    needed = (T.BUILD, T.WALL_SUGGESTION, T.UPGRADE, T.UPGRADE_CONFIRM, T.CLOSE)

    def skip() -> int:
        if list_open:
            _close_windows(game)
        return 0

    if not cfg.enabled or not all(game.has(n) for n in needed):
        return skip()
    # A wall needs a free builder: with the counter above Build at "0/7",
    # or "1/7" under a goblin (the Goblin Builder, for gems), opening the list
    # would only end in an upgrade that doesn't go through. (The upgrades
    # read it before opening the list, and kept one builder for the walls.)
    if (handed["builders"] if list_open else free_builders(game)) == 0:
        log.info("Walls: no free builder")
        return skip()
    known = {} if known is None else known
    if list_open:
        loot = handed["loot"]
    else:
        # Selecting a wall zooms in on it, and over busy buildings the resource
        # numbers can't be read: zoom out first (a pinch finger on a building
        # left selected would move it).
        game.deselect()
        game.zoom_out()
        loot = read_resources()
    budget = _wall_budget(cfg.use, loot, keep) if loot else []
    if cfg.only_when_full:
        budget = [k for k in budget if k in (full or set())]
    if not budget:
        unknown = [k for k in ("gold", "elixir") if cfg.use in (k, "both") and k not in keep]
        log.info("Walls: %s", f"how much {' and '.join(unknown)} the storages hold isn't known yet" if unknown
                 else "nothing to spare above what's kept" + (" (or not full)" if cfg.only_when_full else ""))
        return skip()
    pays = known.get("pays")
    if pays and not set(budget) <= pays:
        # Low walls take gold only: elixir comes at a higher wall level.
        # Looked at again every PRICE_RECHECK rounds.
        if known.get("pays_skips", 0) >= PRICE_RECHECK:
            known["pays_skips"] = 0
        else:
            known["pays_skips"] = known.get("pays_skips", 0) + 1
            left = [k for k in budget if k in pays]
            log.info("Walls: they take only %s so far, not %s", " or ".join(sorted(pays)),
                     " or ".join(k for k in budget if k not in pays))
            if not left:
                return skip()
            budget = left
    if not _payable(loot, budget, keep, known):
        return skip()
    in_list = False
    if list_open:
        # The upgrades left another tab or filter on: the walls' tab, sorted
        # Cost Min (the cheapest wall first). Without the filter's pictures,
        # the list is opened afresh, unfiltered.
        open_tab(game, "buildings")
        in_list = set_filter(game, sort=T.SORT_COST_MIN)
        if not in_list:
            _close_windows(game)
    start, upgraded = loot, {}
    try:
        for i, kind in enumerate(budget):
            if i and not _payable(loot, [kind], keep, known):
                continue
            count, price, loot, in_list = _upgrade_walls(game, cfg, loot, [kind], keep, known, in_list)
            if count:
                upgraded[kind] = (count, price)
    finally:
        # Selecting a wall moved the camera to it and zoomed in, and the
        # walls just upgraded stay selected: close the list and, unless a
        # battle is next, drop them and zoom out.
        _close_windows(game)
        if not last:
            game.deselect()
            game.zoom_out()
    if not upgraded:
        return 0
    game.wait(1.5)  # the resource counters run down to what's left
    return _walls_done(start, read_resources(), upgraded)


def _walls_done(before: Loot, after: Loot | None, upgraded: dict[str, tuple[int, int | None]],
                what: str = "wall") -> int:
    """Log the walls upgraded with each resource (``upgraded``: how many
    were selected, at what price), from what the storages lost once the
    list was closed; if that can't be read, or doesn't add up, as many as
    were selected. Returns how many."""
    spent = {k: getattr(before, k) - getattr(after, k) for k in upgraded} if after else {}
    believable = bool(spent) and all(0 <= spent[k] <= count * (price or spent[k]) * 1.05
                                     for k, (count, price) in upgraded.items())
    done = 0
    for kind, (count, price) in upgraded.items():
        if not believable:
            log.info("Upgraded %d %s%s with %s (couldn't read what was spent)", count, what,
                     "" if count == 1 else "s", kind)
        elif spent[kind] == 0:
            log.info("%s upgrade with %s didn't go through (no free builder?)", what.capitalize(), kind)
            count = 0
        else:
            count = round(spent[kind] / price) if price else count
            log.info("Upgraded %d %s%s with %s (%s)", count, what, "" if count == 1 else "s", kind,
                     f"{spent[kind]:,}")
        done += count
    return done


def _payable(loot: Loot | None, budget: list[str], keep: dict[str, int], known: dict,
             what: str = "Walls") -> bool:
    """Whether one of ``budget``'s resources pays for a wall at the price
    last seen (not seen yet: it may). Every PRICE_RECHECK rounds skipped,
    the list is looked at anyway: new walls can be cheaper."""
    if loot is None:
        return False
    price = known.get("price")
    spare = {k: getattr(loot, k) - keep[k] for k in budget}
    if not price or any(v >= price for v in spare.values()) or known.get("skips", 0) >= PRICE_RECHECK:
        known["skips"] = 0
        return any(v > 0 for v in spare.values())
    known["skips"] = known.get("skips", 0) + 1
    log.info("%s: %s spare, a wall costs %s", what,
             " and ".join(f"{v:,} {k}" for k, v in spare.items()), f"{price:,}")
    return False


def _upgrade_walls(game: Game, cfg: WallsConfig, loot: Loot, budget: list[str], keep: dict[str, int],
                   known: dict, in_list: bool = False) -> tuple[int, int | None, Loot, bool]:
    """One resource's walls. ``in_list``: Buildings & Heroes is still open
    from the last pass (its filter set). Returns how many walls were
    selected and paid for, at what price, the resources left after (as
    paid: the numbers beside the open list can't be read reliably), and
    whether the list is still open."""
    if not in_list:
        _close_windows(game)
        game.refresh()
        if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
            log.info("Walls: the Build list didn't open")
            return 0, None, loot, False
        game.wait(1.2)
    # Sorted Cost Min, the first wall down the list is the cheapest; without
    # the filter's pictures, it's the last of the walls where the game lists
    # them by itself. A list still open is still sorted.
    if in_list and game.has(T.SORT_COST_MIN):
        entry = first_wall(game)
    elif set_filter(game, sort=T.SORT_COST_MIN):
        entry = first_wall(game)
    else:
        entry = find_wall_entry(game)
    if entry is None:
        log.info("No wall to upgrade in the list")
        return 0, None, loot, in_list
    price = _wall_price(game, entry)
    red = not price and price_is_red(game, entry)
    if price:
        known["price"] = price
        short = {k: getattr(loot, k) - keep[k] for k in budget}
        if all(v < price for v in short.values()):
            # Not enough for one: don't select it (the list would close).
            log.info("Walls: %s spare, a wall costs %s",
                     " and ".join(f"{v:,} {k}" for k, v in short.items()), f"{price:,}")
            return 0, None, loot, True
    if not _select_wall(game, entry):
        log.info("Walls: no Upgrade button under the wall (%.2f alike)", game.likeness(T.UPGRADE))
        _keep_screen(WALL_SCREENS, game.screen)
        return 0, None, loot, False
    game.wait(0.6)  # the buttons fade in
    game.refresh()
    buttons ={_cost_kind(game.screen, m.x, m.y, _icon_offset(m)): m for m in game.find_all(T.UPGRADE)}
    takes = {k for k in buttons if k in ("gold", "elixir")}
    if takes:
        known["pays"] = takes  # what this wall's buttons cost
    kind = next((k for k in budget if k in buttons), None)
    if kind is None:
        log.info("Walls: no upgrade payable with %s (buttons cost %s)", budget, list(buttons))
        return 0, None, loot, False
    spare = getattr(loot, kind) - keep[kind]
    if not price:
        # Unread: the price last seen. Never tap Upgrade without knowing it's
        # paid for (short of loot, the game offers to make it up in gems).
        price = known.get("price")
        if not price:
            log.info("Walls: %s", "not enough to pay for one (its price shows red)" if red
                     else "couldn't read the price; trying again next round")
            return 0, None, loot, False
        log.info("Walls: couldn't read the price; it was %s", f"{price:,}")
    count = min(MAX_WALLS, spare // price)
    if count < 1:
        log.info("Walls: %s %s spare, a wall costs %s", f"{spare:,}", kind, f"{price:,}")
        return 0, None, loot, False
    if count > 1 and not (game.has(T.UPGRADE_MORE) and game.has(T.ADD_WALL)):
        count = 1
    if count > 1:
        count = _select_more(game, count)
    game.refresh()
    buttons = {_cost_kind(game.screen, m.x, m.y, _icon_offset(m)): m for m in game.find_all(T.UPGRADE)}
    if kind not in buttons:
        log.info("Walls: the %s Upgrade button went away", kind)
        return 0, None, loot, False
    game.tap_match(buttons[kind])
    confirmed = _confirm_walls(game)
    if confirmed is None:
        return 0, None, loot, False  # it asked for gems (logged): left unpaid
    if not confirmed:
        log.info("Walls: the upgrade window didn't open (Confirm %.2f, Okay %.2f alike)",
                 game.likeness(T.UPGRADE_CONFIRM), game.likeness(T.OKAY))
        return 0, None, loot, False
    game.wait(1.5)
    # Buildings & Heroes stays open, for the other resource's walls.
    left = getattr(loot, kind) - count * price if price else getattr(loot, kind)
    return count, price, replace(loot, **{kind: left}), True


def _select_wall(game: Game, entry, button: str = T.UPGRADE) -> bool:
    """Tap a "Wall x N" row: the list closes and the game selects one of
    those walls, its Upgrade button (``button``) under it. A tap the list
    took as the end of a glide leaves it open: the row is tapped once more."""
    game.tap_match(entry)
    if game.wait_for(button, timeout=4):
        return True
    rows = game.find_all(T.WALL_SUGGESTION)
    row = min(rows, key=lambda m: abs(m.y - entry.y), default=None)
    if row is None or abs(row.y - entry.y) > row.h:
        return False  # the list closed (or moved): not some other row
    log.info("Walls: the list stayed open; tapping the wall again")
    game.tap_match(row)
    return game.wait_for(button, timeout=4) is not None


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


def _confirm_walls(game: Game, timeout: float = 4) -> bool | None:
    """Accept the window Upgrade opens: for one wall, its green Confirm
    (never the grey one that spends a Hammer); after Upgrade More, an
    "Upgrade Walls" question with Okay. Whichever shows is pressed, so it
    doesn't matter how many walls ended up selected. ``None``: it asked
    for gems, and was left alone."""
    okay = game.has(T.OKAY)
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        confirm = green_confirm(game) if game.has(T.UPGRADE_CONFIRM) else None
        if confirm is None and okay:
            confirm = game.find(T.OKAY)
        if confirm is not None:
            if asks_for_gems(game, confirm.y):
                log.warning("Walls: the upgrade asks for gems; leaving it alone")
                _keep_screen(WALL_SCREENS, game.screen)
                return None
            game.tap_match(confirm)
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


def deselect(game: Game) -> None:
    """Leave nothing selected, even a building being upgraded (no Upgrade
    button shows under it): see ``Game.deselect``."""
    game.deselect(always=True)


CART_BELOW_LABEL = 1.9  # the cart sits this many label heights below its label


def clear_gravestones(game: Game) -> bool:
    """Tap a gravestone (left by an attacker's troops) if one shows: one
    tap clears them all. True if one was tapped."""
    if not game.has(T.GRAVESTONE):
        return False
    game.refresh()
    stone = game.find(T.GRAVESTONE, threshold=COLLECTOR_THRESHOLD)
    if stone is None:
        return False
    game.tap_match(stone)
    game.wait(3)  # they fade out
    game.refresh()
    left = len(game.find_all(T.GRAVESTONE, threshold=COLLECTOR_THRESHOLD))
    if left:
        log.info("Tapped a gravestone; %d still show", left)
    else:
        log.info("Cleared the gravestones")
    deselect(game)  # a building next to it may have been tapped instead
    return True


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
