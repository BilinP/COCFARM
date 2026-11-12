"""Clan Capital raids on Raid Weekends.

Raid Weekend runs from Friday 07:00 to Monday 07:00 UTC. While raid attacks
are left, a red badge with a sword shows over the capital airship (bottom
left of the home village, under the event buttons, which the arrow at their
end folds away). The airship opens the capital, or the last raid's summary
whose Next Raid leads straight to the raid map. There the district furthest
along is picked, and its Attack opens "Attack Now?". The army is the
player's own, set in the game (it must be full to attack), and is read from
the troop bar once the raid starts:

* the troops are dropped in one clump, quickly once a card's count is
  known (from the raid before), so they arrive together, inside one heal;
* Super Miners (and other ground troops) are healed on a schedule: the
  first heal 25 s after the drop (sooner if one falls under
  ``miner_heal_at``), the next 17 s later, each just ahead of them over the
  buildings they'll dig through (they stay there longest). A heal waits a
  little while they read healthy. They're mostly underground, so their bars
  show only a quarter of the time: in between they're taken to be under
  the buildings whose own (purple) bars show, the ones being hit. When they
  near the screen's edge, or are lost near it, the view is dragged after
  them;
* Flying Fortresses are healed once one falls under ``fortress_heal_at``;
* skeleton spells go in the middle of the base at the start;
* lightning goes at the start where the most buildings stand close
  together (the walled middle, where the defences are), so its splash hits
  them all; of two such places, the one nearer the troops. So does any
  spell it has no picture of.

Capital Gold is never spent.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np

from farmbot.game import Game, T

log = logging.getLogger(__name__)

# Friday 07:00 UTC to Monday 07:00 UTC.
WEEKEND_START = (4, 7)  # weekday (Monday 0), hour
WEEKEND_HOURS = 72
MAX_RAIDS = 6  # raid attacks a weekend (5, and a bonus one)

# The raid map is drawn the same for every clan: each district's stars (the
# middle one) and the district's building, which is tapped to pick it.
DISTRICTS = {
    "Capital Peak": ((585, 222), (555, 310)),
    "Barbarian Camp": ((760, 364), (730, 460)),
    "Wizard Valley": ((588, 440), (560, 530)),
    "Balloon Lagoon": ((440, 578), (410, 670)),
    "Builder's Workshop": ((700, 605), (670, 700)),
    "Dragon Cliffs": ((885, 537), (855, 630)),
    "Golem Quarry": ((281, 694), (255, 790)),
    "Skeleton Park": ((542, 738), (515, 830)),
    "Goblin Mines": ((826, 735), (800, 830)),
}
STAR_GAP = 33  # px between a district's stars
STAR_HALF = 11  # px: half the box looked at for each star

AIRSHIP_LIKENESS = 0.8  # the village's zoom varies a little: ~0.93 seen, other places under 0.7

COLOURFUL = 70  # mean saturation of a card in colour; greyed ones are near 0

# The raid's troop bar: card slots from the left, troops then spells, in the
# order they were added to the army. The cards' pictures (from the army
# editor's list) show smaller there.
# The cards are ~89 px wide, ~99 apart, with ~11 px more between the
# troops and the spells: where each is, is read off the bar (``card_xs``).
BAR_Y = 850
BAR_FIRST_X = 88  # where the cards would be when the bar can't be read
BAR_STEP = 100
CARD_WIDTH = (70, 110)  # px: a lit card header's width (the count "x2" splits it: joined over gaps)
CARD_SPLIT = 8  # px: widest gap within one header
BAR_SLOTS = 10
BAR_CARD_SCALES = (0.6, 0.65, 0.7, 0.75, 0.8)
CARD_HEADER = 120  # brightness of a card's top (~195); an empty slot's ~55
BAR_CARD_LIKENESS = 0.75  # the right card ~0.95, another ~0.55, an empty slot under 0.4
ALL = 30  # "every one the card has": more than any army holds
MAIN_TROOPS = ("super_miner", "flying_fortress")
SPELLS = ("heal", "skeleton", "lightning")

# Health bars over the units: a thin bright green fill (and a dark rest).
BAR_AREA = (60, 760)  # y range looked at: under the top banner, above the troop bar
BAR_BORDER = 2  # px of dark end a full health bar still has after its fill
BAR_EDGE = 45  # drop in green from a smoky bar's fill to its frame (seen 45-100; grass to a ring's glow is softer)
BAR_LOST = 100  # brightest a bar's lost part gets (clear ~50, under smoke up to ~90; grass and smoke 150+)
PALE_BLUE = 62  # % of green a smoky bar's blue stays under (seen 49-59; the rings' glow 67-83)
HEAL_LEAD = 1.0  # s: a heal lands where the units will be this much later
HEAL_GAP = 5.0  # s between two heals
# Super Miners (raids of 2026-11-08): heals ahead over the buildings, on a
# schedule, took Barbarian Camp from 68% to 3 stars and Wizard Valley from
# 49% to 98%; healing only on low readings left them unhealed (rarely seen).
FIRST_HEAL_BY = 25.0  # s after the drop
HEAL_EVERY = 17.0  # s between heals (10 s apart, the second landed on a fresh clump)
SEEN_LATELY = 6.0  # s: a sighting older than this is too stale to heal on (raid 4's guess was wasted)
STILL_FINE = 0.9  # an on-time heal waits while every recent reading is this healthy...
FINE_FOR = 3.0  # ...over this many s...
FINE_WAIT = 8.0  # ...for at most this long past its time
HEAL_REACH = 70  # px: a heal's circle at the raid's zoom
MINER_REACH = 150  # px from the miners: how far ahead a heal may go
# The buildings the miners are hitting show their own (purple) health bars:
# seen three times as often as the miners' (recordings of 2026-11-08).
FIGHT_NEAR = 260  # px from where the miners were last: bars further off are another fight (or the lightning's)
FIGHT_GROUP = 120  # px: bars this near each other are one fight
FIGHT_AFTER = 10.0  # s after the drop before bars count with no miner seen yet (the lightning's marks fade)
LIGHTNING_FAR = 0.35  # how much less a spot the whole screen away from the troops counts
BURST_GAP = 0.06  # s between the quick taps of a card whose count is known
PAN_EDGE = 110  # px: miners seen this near the screen's edge are followed
PAN_LOST = 8.0  # s unseen, last seen off-centre: followed too
PAN_GAP = 6.0  # s between two drags of the view
MAX_PANS = 3  # drags a battle
PAN_MOST = 260  # px: the longest drag
PAN_MS = 700  # slow, so the view doesn't fling on
BATTLE_LENGTH = 200.0  # s: a raid battle is 3 minutes
DROP_MARGINS = (31, 15)  # px across kept off the deploy ground's edge (the second for a narrow strip)
DROP_TRIES = 4  # spots tried before giving up on dropping a card
DROP_APART = 80  # px between two spots tried
# Somewhere on the usual raid map when no deploy ground is seen: under,
# left and right of the base (a tap that drops nothing costs only a look).
GUESSES = ((590, 660), (230, 520), (950, 520))
DEPLOYED = 4.0  # mean change of the card's count corner when a unit left it (the same card ~1)
LIGHTNING_REACH = 45  # px: the splash's radius at the raid's zoom
MAX_STRIKES = 6  # of one spell card: more than an army holds


class NotAllowed(Exception):
    """The game refused the raid: joined the clan after the weekend began."""


@dataclass
class Army:
    troops: list[tuple[str, int]] = field(default_factory=list)  # (card, how many), in the bar's order
    spells: list[tuple[str, int]] = field(default_factory=list)
    xs: list[int] = field(default_factory=list)  # each card's middle in the bar, as read

    @property
    def main(self) -> str:
        """The troop the heals are timed for."""
        names = [n for n, _ in self.troops]
        return next((n for n in MAIN_TROOPS if n in names), names[0] if names else "")

    def slot(self, name: str) -> int | None:
        names = [n for n, _ in self.troops + self.spells]
        return names.index(name) if name in names else None

    def x(self, name: str) -> int | None:
        """The middle of that card in the bar, None without one."""
        slot = self.slot(name)
        if slot is None:
            return None
        return self.xs[slot] if slot < len(self.xs) else slot_x(slot)

    def count(self, name: str) -> int:
        return dict(self.troops + self.spells).get(name, 0)


def raid_weekend(now: datetime) -> bool:
    """True from Friday 07:00 to Monday 07:00 UTC."""
    return weekend_end(now) is not None


def weekend_end(now: datetime) -> datetime | None:
    """When the Raid Weekend ``now`` falls in ends, or None outside one."""
    now = now.astimezone(timezone.utc)
    day, hour = WEEKEND_START
    start = (now - timedelta(days=(now.weekday() - day) % 7)).replace(hour=hour, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=7)
    end = start + timedelta(hours=WEEKEND_HOURS)
    return end if now < end else None


def ready(game: Game) -> bool:
    names = (T.CAPITAL_BADGE, T.CAPITAL_LEAVE_RAID, T.CAPITAL_DISTRICT_ATTACK, T.CAPITAL_ATTACK_NOW,
             T.CAPITAL_ATTACK_GO, T.CAPITAL_RESULT_MAP)
    return all(game.has(n) for n in names)


def raid_capital(game: Game, cfg) -> int:
    """Raid with every raid attack left (the home village showing).
    Returns how many raids were made; raises NotAllowed when the game
    refuses them for this weekend."""
    if not ready(game):
        return 0
    badge = _find_badge(game)
    if badge is None:
        if not _start_raid(game):
            log.info("Clan Capital: no raid attacks left")
            return 0
    else:
        log.info("Clan Capital: raid attacks left; opening the capital")
        game.tap(badge.x - 15, badge.y + 50)  # the airship under the badge
    done = 0
    try:
        if not _open_raid_map(game):
            log.warning("Clan Capital: the raid map didn't open")
            return 0
        while done < MAX_RAIDS and _raid_once(game, cfg):
            done += 1
    finally:
        _go_home(game)
    return done


def _find_badge(game: Game):
    game.refresh()
    fold = game.find(T.CAPITAL_COLLAPSE) if game.has(T.CAPITAL_COLLAPSE) else None
    if fold is not None:  # the event buttons cover the airship
        game.tap_match(fold)
        game.wait(1)
    for _ in range(2):
        game.refresh()
        badge = game.find(T.CAPITAL_BADGE)
        if badge is not None and badge.y < 760:
            return badge
        # Low on the screen the airship sits behind the buttons: drag the village up.
        game.swipe(700, 500, 700, 300, 600)
        game.wait(1.5)
    return None


def _start_raid(game: Game) -> bool:
    """Without a badge the clan's raid may not have begun yet: the capital
    then shows Start Raid, which begins it (for the whole clan). The badge
    doesn't always show while the raid is going, though (seen 2026-11-07
    with an attack left): the capital then shows Raid Map, whose window
    tells. True with the capital left open; otherwise back home."""
    if not (game.has(T.CAPITAL_AIRSHIP) and game.has(T.CAPITAL_START_RAID)):
        return False
    game.refresh()
    ship = game.find(T.CAPITAL_AIRSHIP, threshold=AIRSHIP_LIKENESS)
    if ship is None or ship.y > 800:
        return False
    game.tap_match(ship)
    deadline = game.clock() + 10
    while game.clock() < deadline:
        game.wait(1)
        game.refresh()
        if game.find(T.CAPITAL_START_RAID) is not None:
            log.info("Clan Capital: the clan's raid hasn't begun; starting it")
            game.click(T.CAPITAL_START_RAID)
            game.wait(2)
            return True
        if any(game.has(n) and game.find(n) is not None for n in (T.CAPITAL_RAID_MAP, T.CAPITAL_NEXT_RAID)):
            log.info("Clan Capital: no badge, but the clan's raid is going; looking on its map")
            return True  # with no attacks left, Attack Now won't open there
    _go_home(game)
    return False


def _open_raid_map(game: Game) -> bool:
    deadline = game.clock() + 15
    while game.clock() < deadline:
        game.refresh()
        if game.find(T.CAPITAL_LEAVE_RAID) is not None:
            return True
        for button in (T.CAPITAL_NEXT_RAID, T.CAPITAL_RAID_MAP):
            if game.has(button) and game.click(button):
                game.wait(2)
                break
        else:
            game.wait(1)
    return False


def _go_home(game: Game) -> None:
    """Back to the home village from the raid map or the capital."""
    for _ in range(6):
        game.refresh()
        if game.has(T.CAPITAL_RETURN_HOME) and game.click(T.CAPITAL_RETURN_HOME):
            game.wait(4)
            return
        for button in (T.CAPITAL_RESULT_MAP, T.CAPITAL_LEAVE_RAID):
            if game.click(button):
                game.wait(2.5)
                break
        else:
            if game.click(T.CLOSE):
                game.wait(1.5)
            else:
                return


def stars(screen: np.ndarray, middle: tuple[int, int]) -> int | None:
    """Stars won in a district (0-3), or None when it shows no stars
    (locked). Won stars are white; the rest dark, see-through ones."""
    won = 0
    for i in (-1, 0, 1):
        x, y = middle[0] + i * STAR_GAP, middle[1]
        box = screen[y - STAR_HALF:y + STAR_HALF, x - STAR_HALF:x + STAR_HALF]
        if box.size == 0:
            return None
        hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV)
        white = ((hsv[..., 1] < 40) & (hsv[..., 2] > 215)).mean()
        dark = (hsv[..., 2] < 75).mean()
        if white > 0.3:
            won += 1
        elif not 0.3 <= dark <= 0.95:  # an empty star still shows the map through it; all dark is a black screen
            return None
    return won


def pick_district(screen: np.ndarray) -> tuple[str, tuple[int, int]] | None:
    """The open district furthest along (most stars, under three)."""
    best = None
    for name, (middle, building) in DISTRICTS.items():
        won = stars(screen, middle)
        if won is None or won >= 3:
            continue
        if best is None or won > best[0]:
            best = (won, name, building)
    return (best[1], best[2]) if best else None


def _raid_once(game: Game, cfg) -> bool:
    game.refresh()
    picked = pick_district(game.screen)
    if picked is None:
        log.info("Clan Capital: no district to attack")
        return False
    name, building = picked
    log.info("Clan Capital: attacking %s", name)
    game.tap(*building)
    if not game.click_when_visible(T.CAPITAL_DISTRICT_ATTACK, timeout=4):
        log.info("Clan Capital: %s has no Attack button", name)
        return False
    if not game.wait_for(T.CAPITAL_ATTACK_NOW, timeout=5):
        log.info("Clan Capital: the Attack Now window didn't open (no raid attacks left?)")
        return False
    game.wait(0.5)
    game.refresh()
    go = game.find(T.CAPITAL_ATTACK_GO)
    if go is None or not _coloured(game.screen, go, hue=(35, 85)):
        log.warning("Clan Capital: the capital army isn't full, so the game won't attack with it; "
                    "fill it in the game (Edit army)")
        game.click(T.CLOSE)
        return False
    game.tap_match(go)
    if not _battle_starts(game):
        return False
    dropped = battle(game, cfg)
    # Out of heals the units may still fight on: the battle runs out its timer.
    left = BATTLE_LENGTH - (game.clock() - dropped)
    if not game.wait_for(T.CAPITAL_RESULT_MAP, timeout=max(left, 0) + 30, interval=1):
        log.warning("Clan Capital: no result after the raid; ending it")
        game.click(T.END_BATTLE)
        if game.has(T.SURRENDER_OK):
            game.click_when_visible(T.SURRENDER_OK, timeout=4)
        if not game.wait_for(T.CAPITAL_RESULT_MAP, timeout=15, interval=1):
            return False
    game.click(T.CAPITAL_RESULT_MAP)
    game.wait_for(T.CAPITAL_LEAVE_RAID, timeout=10)
    return True


def _battle_starts(game: Game) -> bool:
    deadline = game.clock() + 15
    while game.clock() < deadline:
        game.refresh()
        if game.find(T.END_BATTLE) is not None:
            return True
        if game.has(T.CAPITAL_NOT_MEMBER) and game.find(T.CAPITAL_NOT_MEMBER) is not None:
            raise NotAllowed("joined the clan after the Raid Weekend began")
        sure = game.find(T.CAPITAL_SURE_ATTACK) if game.has(T.CAPITAL_SURE_ATTACK) else None
        if sure is not None:  # Capital Gold storage full: the raid still earns medals
            game.tap_match(sure)
        game.wait(1)
    log.warning("Clan Capital: the raid didn't start")
    return False


# --- the army ---------------------------------------------------------------

def _coloured(screen: np.ndarray, match, hue: tuple[int, int] | None = None, least: int = COLOURFUL) -> bool:
    """Whether a match is in colour (not greyed out), and of that hue."""
    x0, y0 = max(0, match.x - match.w // 2), max(0, match.y - match.h // 2)
    hsv = cv2.cvtColor(screen[y0:y0 + match.h, x0:x0 + match.w], cv2.COLOR_BGR2HSV)
    if hsv.size == 0 or hsv[..., 1].mean() < least:
        return False
    if hue is None:
        return True
    strong = hsv[..., 1] > least
    low, high = hsv[..., 0] >= hue[0], hsv[..., 0] <= hue[1]
    inside = (low | high) if hue[0] > hue[1] else (low & high)  # red wraps round: (170, 10)
    return (inside & strong).sum() > 0.3 * strong.sum()


def slot_x(slot: int) -> int:
    """Where a slot's card would be, evenly spaced (when the bar can't be read)."""
    return BAR_FIRST_X + slot * BAR_STEP


def card_xs(screen: np.ndarray) -> list[int]:
    """The middles of the lit cards in the troop bar, left to right (a
    greyed one doesn't light up: read them before the battle)."""
    hsv = cv2.cvtColor(screen[BAR_Y - 52:BAR_Y - 38], cv2.COLOR_BGR2HSV)
    lit = np.r_[0, (hsv[..., 2].mean(axis=0) > CARD_HEADER).astype(np.int8), 0]
    edges = np.flatnonzero(np.diff(lit))
    runs: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if runs and start - runs[-1][1] <= CARD_SPLIT:
            runs[-1][1] = int(end)
        else:
            runs.append([int(start), int(end)])
    return [(a + b) // 2 for a, b in runs if CARD_WIDTH[0] <= b - a <= CARD_WIDTH[1]]


def bar_card(screen: np.ndarray, x: int, cards: dict[str, np.ndarray]) -> str | None:
    """The card in the troop bar around x: one of ``cards`` (name to
    picture), "" for a card it has no picture of, None for an empty slot."""
    header = cv2.cvtColor(screen[BAR_Y - 52:BAR_Y - 38, x - 35:x + 35], cv2.COLOR_BGR2HSV)
    if header.size == 0 or header[..., 2].mean() < CARD_HEADER:  # the dark bar through an empty slot's outline
        return None
    crop = screen[BAR_Y - 65:BAR_Y + 65, max(x - 55, 0):x + 55]
    best, likeness = "", BAR_CARD_LIKENESS
    for name, image in cards.items():
        for scale in BAR_CARD_SCALES:
            small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if small.shape[0] > crop.shape[0] or small.shape[1] > crop.shape[1]:
                continue
            score = float(cv2.matchTemplate(crop, small, cv2.TM_CCOEFF_NORMED).max())
            if score > likeness:
                best, likeness = name, score
    return best


def read_army(screen: np.ndarray, cards: dict[str, np.ndarray]) -> Army:
    """The player's army, from the troop bar: troops first, then spells.
    How many of each isn't read; each card is used until it greys out."""
    army = Army(xs=card_xs(screen))
    for slot in range(BAR_SLOTS):
        if slot >= len(army.xs):
            army.xs.append(slot_x(slot) if not army.xs else army.xs[-1] + BAR_STEP)
        name = bar_card(screen, army.xs[slot], cards)
        if name is None:
            break
        unknown = not name
        if unknown:
            name = f"unit{slot}"
        spell = name in SPELLS or (unknown and bool(army.spells))  # the spells come after the troops
        (army.spells if spell else army.troops).append((name, ALL))
    del army.xs[len(army.troops) + len(army.spells):]
    return army


def _cards(game: Game) -> dict[str, np.ndarray]:
    if not game.has(T.CAPITAL_CARD_DIR):
        return {}
    return {name.rsplit("/", 1)[-1]: image for name, image in game.vision.library.get(T.CAPITAL_CARD_DIR)}


# --- the battle -------------------------------------------------------------

def deploy_area(screen: np.ndarray) -> tuple[np.ndarray, tuple[int, int] | None]:
    """The light green ground troops may be dropped on (a mask), and the
    middle of the enemy base (its darker grass and buildings). Before the
    battle starts the ground is lit up (it fades to nearly the base's grass
    after), sometimes only a narrow strip, flecked with flowers."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    zone = ((h >= 40) & (h <= 65) & (s >= 70) & (s <= 125) & (v >= 160) & (v <= 215)).astype(np.uint8)
    top, bottom = BAR_AREA
    zone[:top], zone[bottom:] = 0, 0
    zone = cv2.morphologyEx(zone, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))  # over the flowers
    zone = cv2.morphologyEx(zone, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    ys, xs = np.nonzero(_base_grass(hsv))
    middle = (int(xs.mean()), int(ys.mean())) if len(xs) > 500 else None
    return zone, middle


def _base_grass(hsv: np.ndarray) -> np.ndarray:
    """The enemy base's darker grass (a mask), the buildings left out."""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    base = ((h >= 38) & (h <= 66) & (s >= 120) & (v >= 120) & (v <= 165)).astype(np.uint8)
    top, bottom = BAR_AREA
    base[:top], base[bottom:] = 0, 0
    return cv2.morphologyEx(base, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))


def base_area(hsv: np.ndarray) -> np.ndarray:
    """All of the enemy base (a 0/1 mask): everything within its grass's
    outline, so its water and its walled, paved middle too."""
    points = cv2.findNonZero(cv2.morphologyEx(_base_grass(hsv), cv2.MORPH_OPEN, np.ones((15, 15), np.uint8)))
    area = np.zeros(hsv.shape[:2], np.uint8)
    if points is not None:
        cv2.fillConvexPoly(area, cv2.convexHull(points), 1)
    return area


def buildings(screen: np.ndarray) -> np.ndarray:
    """What stands in the base, as a 0/1 float mask: whatever there isn't
    grass or trees (green), water, a wall (pale stone), or rock and rubble
    (grey). Until 1.5.4 only red and gold roofs outside the walls counted:
    the houses on the outskirts, and none of the defences."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = (hsv[..., i].astype(np.int16) for i in range(3))
    green = (h >= 30) & (h <= 90)
    water = (h >= 95) & (h <= 118) & (s >= 110)
    wall = (h >= 8) & (h <= 30) & (s >= 25) & (s <= 125) & (v >= 150)
    grey = s < 40
    found = (~(green | water | wall | grey) & (base_area(hsv) > 0)).astype(np.uint8)
    top, bottom = BAR_AREA
    found[:top], found[bottom:] = 0, 0
    return cv2.morphologyEx(found, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(np.float32)


def enemy_bars(screen: np.ndarray) -> list[tuple[int, int]]:
    """``(x, y)`` of the purple health bars over the base's buildings: each
    shows while its building is being hit, and a few seconds after."""
    top, bottom = BAR_AREA
    hsv = cv2.cvtColor(screen[top:bottom], cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    fill = ((h >= 138) & (h <= 162) & (s >= 150) & (v >= 170)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(fill, connectivity=8)
    return [(int(x + w // 2), int(y + top)) for x, y, w, hh, area in stats[1:]
            if 5 <= w <= 60 and hh <= 4 and area >= w * hh * 0.6]


def fight(bars: list[tuple[int, int]], last: tuple[float, float] | None) -> tuple[float, float] | None:
    """Where the troops are fighting, from the buildings' bars: the middle
    of the biggest group of them (near where the troops were ``last``, when
    that's known), a little under the bars (the buildings)."""
    if last is not None:
        bars = [b for b in bars if np.hypot(b[0] - last[0], b[1] - last[1]) <= FIGHT_NEAR]
    if not bars:
        return None
    groups = [[o for o in bars if np.hypot(o[0] - b[0], o[1] - b[1]) <= FIGHT_GROUP] for b in bars]
    group = max(groups, key=len)
    return float(np.mean([b[0] for b in group])), float(np.mean([b[1] for b in group])) + 25


def _disk(r: int) -> np.ndarray:
    disk = np.zeros((2 * r + 1, 2 * r + 1), np.float32)
    cv2.circle(disk, (r, r), r, 1, -1)
    return disk


def _distance(shape, point: tuple[float, float]) -> np.ndarray:
    ys, xs = np.mgrid[0:shape[0], 0:shape[1]]
    return np.hypot(xs - point[0], ys - point[1])


def lightning_spots(screen: np.ndarray, n: int, by: tuple[float, float] | None = None) -> list[tuple[int, int]]:
    """Where n lightning spells hit the most buildings, counted within the
    splash's reach; each next spot away from the ones before. With ``by``
    (where the troops are), a spot nearer it counts a little more: of two
    packed places, the one they get to."""
    disk = _disk(LIGHTNING_REACH)
    hit = cv2.filter2D(buildings(screen), -1, disk)
    reach_sum = disk.sum()
    worth = hit
    if by is not None:
        worth = hit * (1 - LIGHTNING_FAR * _distance(hit.shape, by) / float(np.hypot(*hit.shape)))
    r = LIGHTNING_REACH
    spots = []
    for _ in range(n):
        _, _, _, (x, y) = cv2.minMaxLoc(worth)
        if hit[y, x] < reach_sum * 0.05:  # nothing much left to hit
            break
        spots.append((int(x), int(y)))
        cv2.circle(worth, (x, y), 2 * r, 0, -1)
        if worth is not hit:
            cv2.circle(hit, (x, y), 2 * r, 0, -1)
    return spots


def drop_points(screen: np.ndarray, n: int = DROP_TRIES) -> list[tuple[int, int]]:
    """Where to drop the troops in one clump, best first: the light green
    ground nearest the middle of the base, a little way in from its edge;
    then other spots on it, each away from those before (in case a tap
    there drops nothing)."""
    zone, middle = deploy_area(screen)
    for margin in DROP_MARGINS:  # off the edge, so taps land inside: wide where the ground allows
        ys, xs = np.nonzero(cv2.erode(zone, np.ones((margin, margin), np.uint8)))
        if len(xs):
            break
    if middle is None:
        middle = (screen.shape[1] // 2, screen.shape[0] // 2)
    far = (xs - middle[0]) ** 2 + (ys - middle[1]) ** 2
    points: list[tuple[int, int]] = []
    for i in np.argsort(far, kind="stable"):
        x, y = int(xs[i]), int(ys[i])
        if all(abs(x - px) + abs(y - py) >= DROP_APART for px, py in points):
            points.append((x, y))
            if len(points) >= n:
                break
    return points


def drop_point(screen: np.ndarray) -> tuple[int, int] | None:
    """The best of ``drop_points``, or None without any ground to drop on."""
    points = drop_points(screen, 1)
    return points[0] if points else None


def health_bars(screen: np.ndarray) -> list[tuple[int, int, float]]:
    """``(x, y, health)`` of the green health bars over our units. Under a
    fortress's smoke a bar is washed out to pale green, its dark frame
    barely darker than the smoke: found then as a thin line of strong green
    with a sharp drop in green just above and below it (the pale green rings
    around spells and defences fade softly, and are whiter)."""
    top, bottom = BAR_AREA
    # In 8 bits with OpenCV (a few times quicker than numpy in 16): its
    # subtraction stops at 0, which the "more than" tests below don't mind.
    a = screen[top:bottom]
    b, g, r = cv2.split(a)
    g_r = cv2.subtract(g, r)
    clean = (g > 165) & (b < 55) & (g_r > 60)
    above, below = np.zeros_like(clean), np.zeros_like(clean)
    for d in (1, 2):  # the frame, 1-2 px off after scaling
        above[d:] |= cv2.subtract(g[d:], g[:-d]) > BAR_EDGE
        below[:-d] |= cv2.subtract(g[:-d], g[d:]) > BAR_EDGE
    pale = (g > 130) & (g_r > 20) & above & below
    ys, xs = np.nonzero(pale)  # few pixels: the blue test only there
    pale[ys, xs] = b[ys, xs].astype(np.int32) * 100 < g[ys, xs].astype(np.int32) * PALE_BLUE
    fill = (clean | pale).view(np.uint8)
    empty = cv2.max(cv2.max(b, g), r) < BAR_LOST  # the lost part: dim and see-through, then the bar's dark end
    count, _, stats, _ = cv2.connectedComponentsWithStats(fill, connectivity=8)
    found = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (5 <= w <= 60 and h <= 3 and area >= w * h * 0.6):
            continue
        filled, end = w, x + w
        for row in range(y, y + h):  # the row showing most of the lost part
            lost = x + w
            while lost < min(a.shape[1], x + w + 2) and not empty[row, lost]:
                lost += 1  # the fill's blurred end
            stop = lost
            while stop < a.shape[1] and empty[row, stop] and stop - x < 64:
                stop += 1
            if stop > lost and stop > end:
                filled, end = lost - x, stop
        health = float(min(1.0, filled / max(end - x - BAR_BORDER, 1)))
        found.append((int(x + (end - x) // 2), int(y + top), health))
    return found


def battle(game: Game, cfg) -> float:
    """Drops the player's army and heals it. Returns when the troops were dropped."""
    game.wait(1)
    screen = game.refresh()
    army = read_army(screen, _cards(game))
    log.info("Clan Capital: army %s", ", ".join(name.replace("_", " ") for name, _ in army.troops + army.spells)
             or "not recognised")
    points = drop_points(screen)
    if not points:
        log.warning("Clan Capital: found no ground to drop troops on; trying the usual places")
        points = list(GUESSES)
    _, middle = deploy_area(screen)
    log.info("Clan Capital: dropping the troops at %s", points[0])
    for name, n in army.troops:
        _drop_somewhere(game, army.x(name), points, n, name)
    dropped = game.clock()
    for name, _ in army.spells:
        if name == "heal" or (name == "skeleton" and middle is None):
            continue
        if name == "skeleton":
            spots = [(middle[0] + random.randint(-40, 40), middle[1] + random.randint(-40, 40))
                     for _ in range(MAX_STRIKES)]
        else:  # lightning, and spells it has no picture of: each next on the next most packed buildings
            spots = lightning_spots(game.refresh(), MAX_STRIKES, by=points[0])  # as the view is now
        for spot in spots:  # one at a time while the card lasts
            if not slot_ready(game.refresh(), army.x(name)):
                break
            log.info("Clan Capital: %s at %s", name.replace("_", " "), spot)
            _drop(game, army.x(name), spot, 1)
    heal_at = cfg.miner_heal_at if army.main == "super_miner" else cfg.fortress_heal_at
    _heal(game, army, heal_at, dropped)
    return dropped


def slot_ready(screen: np.ndarray, x: int | None) -> bool:
    """Whether the bar's card around x still has units (it greys out at x0)."""
    if x is None:
        return False
    hsv = cv2.cvtColor(screen[BAR_Y - 40:BAR_Y + 40, x - 35:x + 35], cv2.COLOR_BGR2HSV)
    return hsv.size > 0 and hsv[..., 1].mean() > COLOURFUL


def _corner(screen: np.ndarray, x: int) -> np.ndarray:
    """The card's top right, where its count ("x2") shows."""
    return screen[BAR_Y - 65:BAR_Y - 38, x:x + 45].astype(np.int16)


# What a card's count corner looked like at each count, learned on a drop made
# one tap at a time: the raids after it (the same army, the same card) drop
# all but the last unit in quick taps. Kept by the card's place in the bar.
_counts: dict[int, list[tuple[np.ndarray, int]]] = {}


def _units_left(card: int, corner: np.ndarray) -> int:
    """How many units the card has, when its corner is one seen before; else 0."""
    for image, left in _counts.get(card, []):
        if image.shape == corner.shape and float(np.abs(image - corner).mean()) < DEPLOYED / 2:
            return left
    return 0


def _drop(game: Game, card: int | None, point: tuple[int, int], n: int) -> int:
    """Drops up to n of a card's units at a point, stopping as soon as the
    card runs out (the game then picks the next card by itself, so a tap
    more would drop that), or when a tap dropped nothing (off the deploy
    ground: its count didn't change). Returns how many were dropped.

    One tap at a time (half a second each) the first time, which strings
    ten miners out over five seconds: they arrive one by one. Once the
    card's count is known, all but its last unit go in quick taps."""
    if card is None or n <= 0 or not slot_ready(game.refresh(), card):
        return 0
    game.tap(card, BAR_Y)
    game.wait(0.3)
    before = _corner(game.refresh(), card)  # selected (raised), as it stays
    seen = [(before, 0)]  # the corner, and how many had been dropped by then
    dropped = 0
    learn = True
    known = _units_left(card, before)
    if n >= known >= 3:
        quick = known - 1
        game.tap_many([(point[0] + random.randint(-6, 6), point[1] + random.randint(-6, 6)) for _ in range(quick)],
                      pause=BURST_GAP)
        game.wait(0.4)
        screen = game.refresh()
        now = _corner(screen, card)
        change = float(np.abs(now - before).mean())
        log.info("Clan Capital: card at x %d, %d quick taps at %s: its count changed by %.1f", card, quick, point,
                 change)
        learn = False  # only a drop made one at a time is counted from
        if not slot_ready(screen, card):  # fewer than last time after all
            _counts.pop(card, None)
            return quick
        if change < DEPLOYED:
            return 0  # not ground it may go on
        if _units_left(card, now) != 1:
            # Some taps were too quick for the game: the rest one at a time, and the next raid too.
            log.info("Clan Capital: the quick taps didn't all land; dropping the rest one at a time")
            _counts.pop(card, None)
        dropped, before = quick, now
    for _ in range(n - dropped):
        game.tap(point[0] + random.randint(-6, 6), point[1] + random.randint(-6, 6))
        game.wait(0.3)  # the card greys a moment after its last unit; sooner, a tap more drops the next card
        screen = game.refresh()
        if not slot_ready(screen, card):
            if learn:
                total = dropped + 1
                _counts[card] = [(image, total - done) for image, done in seen]
            return dropped + 1
        now = _corner(screen, card)
        change = float(np.abs(now - before).mean())
        if not dropped:
            log.info("Clan Capital: card at x %d, dropped at %s: its count changed by %.1f", card, point, change)
        if change < DEPLOYED:
            break  # nothing left the card: not ground it may go on
        dropped += 1
        before = now
        seen.append((now, dropped))
    return dropped


def _drop_somewhere(game: Game, card: int | None, points: list[tuple[int, int]], n: int, name: str) -> int:
    """Drops a card's units at the first of ``points`` that takes them."""
    dropped = 0
    for point in points:
        if card is None or dropped >= n or not slot_ready(game.refresh(), card):
            break
        got = _drop(game, card, point, n - dropped)
        if not got:
            log.info("Clan Capital: %s can't go at %s; trying elsewhere", name.replace("_", " "), point)
        dropped += got
    if not dropped:
        log.warning("Clan Capital: couldn't drop %s anywhere", name.replace("_", " "))
    return dropped


def heal_spot(bars: list[tuple[int, int, float]], heal_at: float, groups: int = 1) -> tuple[int, int] | None:
    """Where a heal helps most now, or None while the units are healthy
    enough: the hurt units' middle (those near the most hurt one)."""
    hurt = [b for b in bars if b[2] < heal_at]
    if not hurt:
        return None
    if groups > 1:
        # Big units (fortresses) may be apart: the most hurt one, and any near it.
        worst = min(hurt, key=lambda b: b[2])
        hurt = [b for b in hurt if abs(b[0] - worst[0]) < 120 and abs(b[1] - worst[1]) < 90]
    elif sum(b[2] for b in bars) / len(bars) >= heal_at:
        return None
    return (int(np.mean([b[0] for b in hurt])), int(np.mean([b[1] for b in hurt])) + 20)  # the unit is under its bar


def _heal(game: Game, army: Army, heal_at: float, dropped: float) -> None:
    left = MAX_STRIKES if army.count("heal") else 0  # the card greys out sooner
    card = army.x("heal")
    seen = game.find(T.CAPITAL_BATTLE_HEAL) if game.has(T.CAPITAL_BATTLE_HEAL) else None
    if seen is not None:  # its card, found in the bar
        card = seen.x
    groups = 2 if army.main == "flying_fortress" else 1
    if groups == 1:
        _heal_miners(game, card if left else None, heal_at, dropped)
        return
    # Fortresses: their bar shows mostly while they're hit, so one out of
    # sight is mostly unhurt (seen 2026-11-07): healed only on a low reading,
    # never on a guess.
    last_seen, last_heal = None, -HEAL_GAP
    while left and game.clock() - dropped < BATTLE_LENGTH:
        screen = game.refresh()
        if game.find(T.CAPITAL_RESULT_MAP) is not None:
            return
        now = game.clock()
        bars = health_bars(screen)
        spot = None
        if bars:
            where = (float(np.mean([b[0] for b in bars])), float(np.mean([b[1] for b in bars])), now)
            spot = heal_spot(bars, heal_at, groups)
            if spot is not None and last_seen is not None and now - last_seen[2] < 3:
                # Heading on: lead the heal by how far they moved lately.
                dt = max(now - last_seen[2], 0.2)
                spot = (int(spot[0] + (where[0] - last_seen[0]) / dt * HEAL_LEAD),
                        int(spot[1] + (where[1] - last_seen[1]) / dt * HEAL_LEAD))
            last_seen = where
        if spot is not None and now - last_heal >= HEAL_GAP:
            if not slot_ready(screen, card):
                return  # the heals are used up
            log.info("Clan Capital: heal at %s (health %s)", spot, ", ".join(f"{b[2]:.0%}" for b in bars))
            if not _drop(game, card, spot, 1):
                return  # no heals left after all
            left -= 1
            last_heal = game.clock()
        game.wait(0.3)


# --- Super Miners ------------------------------------------------------------

def heal_ahead(screen: np.ndarray, where: tuple[float, float],
               heading: tuple[float, float] | None = None) -> tuple[int, int]:
    """Where a heal helps the miners at ``where`` most: over the buildings
    nearest them, ahead of them when they're heading somewhere (they dig
    under a building until it falls, so they stay in a heal there longest),
    yet close enough that they're in its circle now."""
    x0, y0 = where
    disk = _disk(HEAL_REACH)
    density = cv2.filter2D(buildings(screen), -1, disk / disk.sum())
    ys, xs = np.mgrid[0:density.shape[0], 0:density.shape[1]]
    dx, dy = xs - x0, ys - y0
    dist = np.hypot(dx, dy)
    score = density * (1 - 0.5 * dist / MINER_REACH)  # nearer is surer: they must get there
    score[dist > MINER_REACH] = 0
    top, bottom = BAR_AREA
    score[:top], score[bottom:] = 0, 0
    if heading is not None:
        n = float(np.hypot(*heading))
        if n > 0:
            score *= 1 + 0.6 * (dx * heading[0] + dy * heading[1]) / (n * np.maximum(dist, 1))
    if score.max() <= 0:
        return int(x0), int(y0)  # no buildings near: on them
    y, x = np.unravel_index(int(np.argmax(score)), score.shape)
    d = float(np.hypot(x - x0, y - y0))
    most = HEAL_REACH * 0.8  # the miners still inside the circle
    if d > most:
        x, y = x0 + (x - x0) * most / d, y0 + (y - y0) * most / d
    return int(x), int(y)


def _heading(track: list[tuple[float, float, float]]) -> tuple[float, float] | None:
    """Which way the miners moved over the last second or more, or None."""
    if not track:
        return None
    t, x, y = track[-1]
    older = [p for p in track if 1.0 <= t - p[0] < SEEN_LATELY]
    if not older:
        return None
    vx, vy = x - older[-1][1], y - older[-1][2]
    return (vx, vy) if np.hypot(vx, vy) > 5 else None


def _pan(game: Game, towards: tuple[float, float], screen: np.ndarray, cards: list[int]) -> None:
    """Drags the view so ``towards`` moves toward the screen's middle (at
    most PAN_MOST), slowly, inside the battlefield. Warns if a card's count
    changed meanwhile: a drag that cast the selected spell (not seen yet)."""
    top, bottom = BAR_AREA
    cx, cy = screen.shape[1] / 2, (top + bottom) / 2
    vx, vy = cx - towards[0], cy - towards[1]
    n = float(np.hypot(vx, vy))
    if n < 1:
        return
    k = min(1.0, PAN_MOST / n)
    vx, vy = vx * k, vy * k
    before = {x: _corner(screen, x) for x in cards if slot_ready(screen, x)}
    log.info("Clan Capital: dragging the view after the miners (last seen at %d, %d)", *towards)
    game.swipe(int(cx - vx / 2), int(cy - vy / 2), int(cx + vx / 2), int(cy + vy / 2), PAN_MS)
    game.wait(0.5)
    after = game.refresh()
    for x, was in before.items():
        if not slot_ready(after, x) or float(np.abs(_corner(after, x) - was).mean()) >= DEPLOYED:
            log.warning("Clan Capital: the card at x %d changed while dragging the view: the drag may have cast it", x)


def _heal_miners(game: Game, card: int | None, heal_at: float, dropped: float) -> None:
    """Heals for Super Miners, and the view kept on them."""
    # (t, x, y): where they were seen (under their bars), or where buildings were being hit
    track: list[tuple[float, float, float]] = []
    readings: list[tuple[float, float]] = []  # (t, the lowest health read)
    last_heal: float | None = None
    used, pans, last_pan = 0, 0, -PAN_GAP
    while game.clock() - dropped < BATTLE_LENGTH:
        screen = game.refresh()
        if game.find(T.CAPITAL_RESULT_MAP) is not None:
            return
        if card is None or not slot_ready(screen, card):
            return
        now = game.clock()
        bars = health_bars(screen)
        if bars:
            track.append((now, float(np.mean([b[0] for b in bars])), float(np.mean([b[1] for b in bars])) + 20))
            readings.append((now, min(b[2] for b in bars)))
        elif track or now - dropped >= FIGHT_AFTER:
            # Underground: they're under the buildings being hit.
            at = fight(enemy_bars(screen), track[-1][1:] if track else None)
            if at is not None:
                track.append((now, *at))
        recent = [p for p in track if now - p[0] < SEEN_LATELY]
        where = (recent[-1][1], recent[-1][2]) if recent else None

        if where is not None:
            since = now - (last_heal if last_heal is not None else dropped)
            due = FIRST_HEAL_BY if last_heal is None else HEAL_EVERY
            low = bool(bars) and readings[-1][1] < heal_at
            lately = [h for t, h in readings if now - t < FINE_FOR]
            fine = bool(lately) and min(lately) >= STILL_FINE
            why = None
            if low and (last_heal is None or since >= HEAL_EVERY):
                why = "hurt"
            elif since >= due and not (fine and since < due + FINE_WAIT):
                why = "on time"
            if why:
                spot = heal_ahead(screen, where, _heading(recent))
                log.info("Clan Capital: heal %d (%s) at %s, miners at %.0f, %.0f, health %s", used + 1, why, spot,
                         where[0], where[1], ", ".join(f"{b[2]:.0%}" for b in bars) or "not seen (by the buildings hit)")
                if _drop(game, card, spot, 1):
                    used += 1
                    last_heal = game.clock()
                else:
                    card = None  # none left after all
                continue

        if track and pans < MAX_PANS and now - last_pan >= PAN_GAP:
            t, x, y = track[-1]
            top, bottom = BAR_AREA
            w = screen.shape[1]
            edge = x < PAN_EDGE or x > w - PAN_EDGE or y < top + PAN_EDGE or y > bottom - PAN_EDGE
            off = abs(x - w / 2) > w / 4 or abs(y - (top + bottom) / 2) > (bottom - top) / 4
            if (edge and now - t < SEEN_LATELY) or (off and now - t > PAN_LOST):
                _pan(game, (x, y), screen, [card])
                pans, last_pan = pans + 1, game.clock()
                track.clear()  # the view moved (how far, the map's edges decide): old sightings are off
                continue
        game.wait(0.3)
