"""Clan Capital raids on Raid Weekends.

Raid Weekend runs from Friday 07:00 to Monday 07:00 UTC. While raid attacks
are left, a red badge with a sword shows over the capital airship (bottom
left of the home village, under the event buttons, which the arrow at their
end folds away). The airship opens the capital, or the last raid's summary
whose Next Raid leads straight to the raid map. There the district furthest
along is picked, and its Attack opens "Attack Now?". The army is the
player's own, set in the game (it must be full to attack), and is read from
the troop bar once the raid starts:

* the troops are dropped in one clump; Super Miners are healed where they
  are heading when their health bars fall under ``miner_heal_at``, Flying
  Fortresses once one falls under ``fortress_heal_at``;
* skeleton spells go in the middle of the base at the start;
* lightning strikes at the start where the most buildings stand close
  together (overlapping ones, often packed in a corner), so its splash hits
  them all; so does any spell it has no picture of.

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

COLOURFUL = 70  # mean saturation of a card in colour; greyed ones are near 0

# The raid's troop bar: card slots from the left, troops then spells, in the
# order they were added to the army. The cards' pictures (from the army
# editor's list) show smaller there.
BAR_Y = 850
BAR_FIRST_X = 88
BAR_STEP = 100
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
HEAL_LEAD = 1.0  # s: a heal lands where the units will be this much later
HEAL_GAP = 5.0  # s between two heals
HEAL_FALLBACK = 40.0  # s after the drop: a heal still unused goes where the units were last seen
BATTLE_LENGTH = 200.0  # s: a raid battle is 3 minutes
LIGHTNING_REACH = 45  # px: the splash's radius at the raid's zoom
MAX_STRIKES = 6  # of one spell card: more than an army holds


class NotAllowed(Exception):
    """The game refused the raid: joined the clan after the weekend began."""


@dataclass
class Army:
    troops: list[tuple[str, int]] = field(default_factory=list)  # (card, how many), in the bar's order
    spells: list[tuple[str, int]] = field(default_factory=list)

    @property
    def main(self) -> str:
        """The troop the heals are timed for."""
        names = [n for n, _ in self.troops]
        return next((n for n in MAIN_TROOPS if n in names), names[0] if names else "")

    def slot(self, name: str) -> int | None:
        names = [n for n, _ in self.troops + self.spells]
        return names.index(name) if name in names else None

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
        log.info("Clan Capital: no raid attacks left")
        return 0
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


def bar_card(screen: np.ndarray, slot: int, cards: dict[str, np.ndarray]) -> str | None:
    """The card in a troop bar slot: one of ``cards`` (name to picture),
    "" for a card it has no picture of, None for an empty slot."""
    x = BAR_FIRST_X + slot * BAR_STEP
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
    army = Army()
    for slot in range(BAR_SLOTS):
        name = bar_card(screen, slot, cards)
        if name is None:
            break
        unknown = not name
        if unknown:
            name = f"unit{slot}"
        spell = name in SPELLS or (unknown and bool(army.spells))  # the spells come after the troops
        (army.spells if spell else army.troops).append((name, ALL))
    return army


def _cards(game: Game) -> dict[str, np.ndarray]:
    if not game.has(T.CAPITAL_CARD_DIR):
        return {}
    return {name.rsplit("/", 1)[-1]: image for name, image in game.vision.library.get(T.CAPITAL_CARD_DIR)}


# --- the battle -------------------------------------------------------------

def deploy_area(screen: np.ndarray) -> tuple[np.ndarray, tuple[int, int] | None]:
    """The light green ground troops may be dropped on (a mask), and the
    middle of the enemy base (its darker grass and buildings)."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    zone = ((h >= 45) & (h <= 65) & (s >= 80) & (s <= 118) & (v >= 165) & (v <= 205)).astype(np.uint8)
    top, bottom = BAR_AREA
    zone[:top], zone[bottom:] = 0, 0
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


def lightning_spots(screen: np.ndarray, n: int) -> list[tuple[int, int]]:
    """Where n lightning spells hit the most buildings: the base's colourful
    roofs and towers (not its grass, walls or water), counted within the
    splash's reach; each next spot away from the ones before."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = (hsv[..., i].astype(np.int16) for i in range(3))
    area = cv2.morphologyEx(_base_grass(hsv), cv2.MORPH_CLOSE, np.ones((61, 61), np.uint8))  # over the buildings
    warm = ((h <= 25) | (h >= 160)) & (s >= 130) & (v >= 120)  # gold, orange and red roofs and towers
    purple = (h >= 125) & (h < 160) & (s >= 80) & (v >= 80)
    buildings = ((warm | purple) & (area > 0)).astype(np.float32)
    r = LIGHTNING_REACH
    reach = np.zeros((2 * r + 1, 2 * r + 1), np.float32)
    cv2.circle(reach, (r, r), r, 1, -1)
    hit = cv2.filter2D(buildings, -1, reach)
    spots = []
    for _ in range(n):
        _, most, _, (x, y) = cv2.minMaxLoc(hit)
        if most < reach.sum() * 0.05:  # nothing much left to hit
            break
        spots.append((int(x), int(y)))
        cv2.circle(hit, (x, y), 2 * r, 0, -1)
    return spots


def drop_point(screen: np.ndarray) -> tuple[int, int] | None:
    """Where to drop the troops in one clump: the light green ground
    nearest the middle of the base, a little way in from its edge."""
    zone, middle = deploy_area(screen)
    zone = cv2.erode(zone, np.ones((31, 31), np.uint8))  # off the edge, so every tap lands inside
    ys, xs = np.nonzero(zone)
    if not len(xs):
        return None
    if middle is None:
        middle = (screen.shape[1] // 2, screen.shape[0] // 2)
    i = int(np.argmin((xs - middle[0]) ** 2 + (ys - middle[1]) ** 2))
    return int(xs[i]), int(ys[i])


def health_bars(screen: np.ndarray) -> list[tuple[int, int, float]]:
    """``(x, y, health)`` of the green health bars over our units."""
    top, bottom = BAR_AREA
    a = screen[top:bottom].astype(np.int16)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    fill = ((g > 165) & (b < 55) & (g - r > 60)).astype(np.uint8)
    empty = a.max(axis=2) < 80  # the lost part: dim and see-through, then the bar's dark end
    count, _, stats, _ = cv2.connectedComponentsWithStats(fill, connectivity=8)
    found = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (5 <= w <= 60 and h <= 3 and area >= w * h * 0.6):
            continue
        end = x + w
        while end < a.shape[1] and empty[y + h // 2, end] and end - x < 64:
            end += 1
        health = min(1.0, w / max(end - x - BAR_BORDER, 1))
        found.append((int(x + (end - x) // 2), int(y + top), health))
    return found


def battle(game: Game, cfg) -> float:
    """Drops the player's army and heals it. Returns when the troops were dropped."""
    game.wait(1)
    screen = game.refresh()
    army = read_army(screen, _cards(game))
    log.info("Clan Capital: army %s", ", ".join(name.replace("_", " ") for name, _ in army.troops + army.spells)
             or "not recognised")
    point = drop_point(screen)
    if point is None:
        log.warning("Clan Capital: no ground to drop troops on found; dropping at the left")
        point = (200, 330)
    _, middle = deploy_area(screen)
    log.info("Clan Capital: dropping the troops at %s", point)
    for name, n in army.troops:
        _drop(game, army.slot(name), point, n)
    dropped = game.clock()
    for name, _ in army.spells:
        if name == "heal" or (name == "skeleton" and middle is None):
            continue
        if name == "skeleton":
            spots = [(middle[0] + random.randint(-40, 40), middle[1] + random.randint(-40, 40))
                     for _ in range(MAX_STRIKES)]
        else:  # lightning, and spells it has no picture of: each next on the next most packed buildings
            spots = lightning_spots(game.refresh(), MAX_STRIKES)  # as the view is now
        for spot in spots:  # one at a time while the card lasts
            if not slot_ready(game.refresh(), army.slot(name)):
                break
            log.info("Clan Capital: %s at %s", name.replace("_", " "), spot)
            _drop(game, army.slot(name), spot, 1)
    heal_at = cfg.miner_heal_at if army.main == "super_miner" else cfg.fortress_heal_at
    _heal(game, army, heal_at, dropped)
    return dropped


def slot_ready(screen: np.ndarray, slot: int) -> bool:
    """Whether the bar's card in that slot still has units (it greys out at x0)."""
    x = BAR_FIRST_X + slot * BAR_STEP
    hsv = cv2.cvtColor(screen[BAR_Y - 40:BAR_Y + 40, x - 35:x + 35], cv2.COLOR_BGR2HSV)
    return hsv.size > 0 and hsv[..., 1].mean() > COLOURFUL


def _drop(game: Game, slot: int | None, point: tuple[int, int], n: int) -> int:
    """Drops up to n of a card's units at a point, stopping as soon as the
    card runs out (the game then picks the next card by itself, so a tap
    more would drop that). Returns how many were dropped."""
    if slot is None or n <= 0 or not slot_ready(game.refresh(), slot):
        return 0
    game.tap(BAR_FIRST_X + slot * BAR_STEP, BAR_Y)
    game.wait(0.3)
    dropped = 0
    for _ in range(n):
        if dropped and not slot_ready(game.refresh(), slot):
            break
        game.tap(point[0] + random.randint(-6, 6), point[1] + random.randint(-6, 6))
        dropped += 1
        game.wait(0.3)  # the card greys a moment after its last unit; sooner, a tap more drops the next card
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
    slot = army.slot("heal")
    seen = game.find(T.CAPITAL_BATTLE_HEAL) if game.has(T.CAPITAL_BATTLE_HEAL) else None
    if seen is not None:  # its card, found in the bar
        slot = round((seen.x - BAR_FIRST_X) / BAR_STEP)
    groups = 2 if army.main == "flying_fortress" else 1
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
        if (spot is None and last_seen is not None and now - dropped > HEAL_FALLBACK and now - last_heal > 15
                and last_seen[2] > last_heal):  # seen again since the last heal: not the same stale spot twice
            spot = (int(last_seen[0]), int(last_seen[1]) + 20)
            log.info("Clan Capital: no unit hurt enough seen in a while; healing where they were last")
        if spot is not None and now - last_heal >= HEAL_GAP:
            log.info("Clan Capital: heal at %s (health %s)", spot, ", ".join(f"{b[2]:.0%}" for b in bars) or "?")
            if not _drop(game, slot, spot, 1):
                return  # no heals left after all
            left -= 1
            last_heal = game.clock()
        game.wait(0.3)
