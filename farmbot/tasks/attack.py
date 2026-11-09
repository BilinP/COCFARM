"""Deploy every troop in the bar around the base and see the battle through."""

from __future__ import annotations

import itertools
import logging
import math
from pathlib import Path
from typing import Iterator

import numpy as np

from farmbot import armybar, deployzone, human, redline as rl, text
from farmbot.armybar import Card
from farmbot.config import Config
from farmbot.game import Game, T
from farmbot.geometry import Point, Rect, points_along
from farmbot.tasks.search import Loot, loot_value, read_loot
from farmbot.tasks.village import open_event_chests, pick_reward

log = logging.getLogger(__name__)


def deploy_edges(game: Game, cfg: Config) -> tuple[dict[str, list[Point]], Point | None]:
    """Points to drop troops on, per attacked side, and the base centre.

    Tries the red line, then the ring around the building area, and fills
    any side still empty from the fixed ``deploy_lines``.
    """
    screen = game.refresh()
    sides = cfg.attack.sides
    found: dict[str, list[Point]] = {}
    sources: dict[str, str] = {}
    center: Point | None = None
    detectors = (
        ("red line", cfg.redline.enabled, lambda: rl.detect(screen, cfg.redline), cfg.redline.min_side_points),
        ("building ring", cfg.zone.enabled, lambda: deployzone.detect(screen, cfg.zone), 1),
    )
    def take(name, result, minimum, on):
        nonlocal center
        if result is None:
            return
        blocked = deployzone.open_ground(on, cfg.zone) if name == "red line" else None
        for side in sides:
            points = result.sides.get(side, [])
            if name == "red line":
                points = on_open_ground(on, points, cfg, blocked, result.rims)
            if side not in found and len(points) >= minimum:
                found[side] = points
                sources[side] = name
                center = center or result.center  # (not from a line none of whose sides were used)

    for name, enabled, detect, minimum in detectors:
        if not enabled or all(s in found for s in sides):
            continue
        take(name, detect(), minimum, screen)
        if name == "red line" and not all(s in found for s in sides):
            # the line pulses: a side too faint on one frame shows on the next
            again = game.refresh()
            take(name, rl.detect(again, cfg.redline), minimum, again)
    lines = fixed_lines(screen.shape[1], cfg)
    for side in sides:
        if side not in found:
            found[side] = lines[side]
            sources[side] = "fixed line"
    floor = button_row_top(game)
    if floor is not None:
        found = {s: [p for p in pts if p[1] < floor] or lines[s] for s, pts in found.items()}
    log.info("Deploy points: %s", ", ".join(f"{s} {len(found[s])} ({sources[s]})" for s in sides))
    return found, center


BUTTON_ROW_MARGIN = 12  # px kept clear above the battle buttons


def _rhythm(cfg: Config) -> bool:
    """The "Battle rhythm varies" habit is on."""
    return cfg.human.uses("battle_rhythm")


def _wait(game: Game, cfg: Config, seconds: float, shortest: float = human.SHORTEST, note: bool = True) -> None:
    """Wait about ``seconds``: exactly, or, with the battle rhythm varying,
    give or take (``shortest`` 1: a wait for the game to show something,
    which may run long but never short). ``note``: count what it adds to
    the battle (not for a poll while the battle plays itself out)."""
    varied = human.pause(game.rng, seconds, _rhythm(cfg), shortest=shortest)
    if note:
        game.note("battle_rhythm", human.pause_extra(seconds, shortest=shortest), varied - seconds)
    game.wait(varied, exact=True)


def _beat(game: Game, cfg: Config, seconds: float, plain: float = 0.0, shortest: float = human.SHORTEST) -> None:
    """A person's pause of about ``seconds`` with the battle rhythm varying;
    ``plain`` (often none) without it."""
    on = _rhythm(cfg)
    varied = human.pause(game.rng, seconds, shortest=shortest) if on else plain
    game.note("battle_rhythm", seconds + human.pause_extra(seconds, shortest=shortest) - plain, varied - plain)
    if varied:
        game.wait(varied, exact=True)


def _burst(game: Game, cfg: Config, taps: int, gap: float) -> float | list[float]:
    """The pauses between a burst's ``taps``: ``gap`` each, or each its own."""
    pause = human.pauses(game.rng, taps, gap, _rhythm(cfg))
    added = sum(pause[:-1]) - gap * (taps - 1) if isinstance(pause, list) else 0.0
    game.note("battle_rhythm", human.pauses_extra(taps, gap), added)
    return pause


SETTLE = 1.0  # shortest: a wait the game needs (a card's count, a hero landing)


def button_row_top(game: Game) -> int | None:
    """Top of the row of battle buttons (End Battle, Boost Army, Boost
    Heroes, Next): taps at or below it could press one of them, so no troop
    is dropped there. ``None`` if neither End Battle nor Next is seen."""
    tops = [m.y - m.h // 2 for name in (T.END_BATTLE, T.NEXT) if game.has(name)
            for m in game.find_all(name)[:1]]
    return min(tops) - BUTTON_ROW_MARGIN if tops else None


POINT_MAX_BLOCKED = 0.5  # a red-line point with more non-ground around it is dropped
SIDE_MAX_BLOCKED = 0.2  # and a side whose points typically have more is not trusted
GROUND_UNKNOWN = 0.8  # share of the screen that is not grass or dirt above which the check is skipped


def on_open_ground(screen, points: list[Point], cfg: Config, blocked=None, rims: bool = False) -> list[Point]:
    """Red-line points that sit on open ground. The real boundary runs over
    grass; wall edges that look like it (golden walls do) sit among
    buildings, so a side whose points are mostly cluttered is dropped.
    ``blocked``: the screen's ``deployzone.open_ground``, if already made.
    ``rims``: a line found along the rims of ground of its colour
    (``RedLine.rims``): wall edges inside a base can look like that too,
    so with no ground to judge by (city scenery, say) it isn't used."""
    if not points:
        return points
    if blocked is None:
        blocked = deployzone.open_ground(screen, cfg.zone)
    if float(blocked.mean()) > GROUND_UNKNOWN:
        return [] if rims else points  # hardly any grass or dirt (snow, desert themes): can't judge
    shares = [float(blocked[y, x]) for x, y in points]
    kept = [p for p, b in zip(points, shares) if b <= POINT_MAX_BLOCKED]
    if not kept or sorted(b for b in shares if b <= POINT_MAX_BLOCKED)[len(kept) // 2] > SIDE_MAX_BLOCKED:
        return []
    return kept


def fixed_lines(width: int, cfg: Config) -> dict[str, list[Point]]:
    """Points along the configured ``deploy_lines``. They are for a screen
    ``deploy_lines_width`` wide; the map is centred, so they shift with the
    screen's width, and points that end up off screen are dropped."""
    shift = (width - cfg.attack.deploy_lines_width) // 2
    lines = {}
    for side, (x1, y1, x2, y2) in cfg.attack.deploy_lines.items():
        line = points_along((x1 + shift, y1), (x2 + shift, y2), 12)
        lines[side] = [p for p in line if 10 <= p[0] < width - 10] or line
    return lines


class DeployPoints:
    """Endless deploy points that alternate between sides and walk along
    each side, so troops spread around the base. A batch can be taken from
    one side (:meth:`take`), so a side whose points the game refuses (a
    guessed line running through a base that fills the map, say) shows up,
    and is dropped: troops then go only where they land."""

    def __init__(self, edges: dict[str, list[Point]], sides: list[str], rng=None, human_like: bool = False):
        # Human-like: each side's points from a random one, in a random
        # direction, and the sides in a new order each battle (the same
        # points: only where along them, and which side first).
        self.rng, self.human_like = rng, human_like and rng is not None
        on = self.human_like
        self.streams = {s: itertools.cycle(human.walk(rng, edges[s], on)) for s in sides if edges.get(s)}
        if not self.streams:
            raise RuntimeError("no deploy points on any side")
        self.live = human.order(rng, list(self.streams), on) if on else list(self.streams)
        self.misses: dict[str, int] = {}
        self.turn = 0
        self.seen_land: set[str] = set()  # sides where troops have landed
        self.redrawn: set[str] = set()  # sides moved onto the red line a refusal showed
        self.dropped: dict[str, int] = {}  # troop taps per side

    def __iter__(self) -> DeployPoints:
        return self

    def _side(self) -> str:
        side = self.live[self.turn % len(self.live)]
        self.turn += 1
        return side

    def __next__(self) -> Point:
        return next(self.streams[self._side()])

    def take(self, count: int) -> tuple[str, list[Point]]:
        """``count`` points along one side, the sides taking turns. Human-
        like, now and then one is passed over: a person's taps along a side
        aren't evenly spaced."""
        side = self._side()
        stream = self.streams[side]
        batch = []
        for _ in range(count):
            if self.human_like and self.rng.random() < SKIP_POINT:
                next(stream)
            batch.append(next(stream))
        self.dropped[side] = self.dropped.get(side, 0) + count
        return side, batch

    def round(self, count: int) -> list[Point]:
        """``count`` troop taps round the sides, a point each."""
        batch = []
        for _ in range(count):
            side = self._side()
            self.dropped[side] = self.dropped.get(side, 0) + 1
            batch.append(next(self.streams[side]))
        return batch

    def busiest(self) -> str | None:
        """The side most troops went to (of those still in use), if any did."""
        sides = [s for s in self.live if self.dropped.get(s)]
        return max(sides, key=lambda s: self.dropped[s]) if sides else None

    def on_side(self, side: str) -> Iterator[Point]:
        """Points along just ``side``."""
        stream = self.streams[side]
        while True:
            yield next(stream)

    def refused(self, side: str) -> bool:
        """A batch from ``side`` went nowhere: after SIDE_REFUSALS of them
        in a row the side is dropped. True while there are other sides to
        try (False: this is the last one)."""
        if len(self.live) == 1:
            return False
        self.misses[side] = self.misses.get(side, 0) + 1
        if self.misses[side] >= SIDE_REFUSALS and side in self.live:
            self.live.remove(side)
            log.info("Deploy points: the %s side is refused; using %s", side, ", ".join(self.live))
        return True

    def landed(self, side: str) -> None:
        self.misses[side] = 0
        self.seen_land.add(side)

    def redraw(self, side: str, screen: np.ndarray, cfg: Config) -> bool:
        """A batch from ``side`` was refused, and a refused tap makes the
        game show the red line: read it off ``screen`` and move the sides
        it shows (that haven't landed troops yet) onto it, once each. True
        if ``side`` moved."""
        if side in self.redrawn or not cfg.redline.enabled:
            return False
        self.redrawn.add(side)
        found = rl.detect(screen, cfg.redline)
        if found is None:
            return False
        moved = []
        for s, points in found.sides.items():
            if s not in self.streams or (s != side and (s in self.seen_land or s in self.redrawn)):
                continue
            points = on_open_ground(screen, points, cfg, rims=found.rims)
            if len(points) >= cfg.redline.min_side_points:
                self.streams[s] = itertools.cycle(human.walk(self.rng, points, self.human_like))
                self.redrawn.add(s)
                self.misses[s] = 0
                moved.append(s)
        if moved:
            log.info("Deploy points: the red line showed; %s moved onto it", ", ".join(moved))
        return side in moved

    def restore(self) -> None:
        """Every side back (the taps were lost, not refused)."""
        self.live = [s for s in self.live if s in self.streams] + [s for s in self.streams if s not in self.live]
        self.misses.clear()


SIDE_REFUSALS = 2  # batches in a row a side's points go nowhere before it's dropped
SKIP_POINT = 0.2  # human-like: chance a point along a side is passed over


def cycle_points(edges: dict[str, list[Point]], sides: list[str], rng=None,
                 human_like: bool = False) -> DeployPoints:
    return DeployPoints(edges, sides, rng, human_like)


def spell_points(edges: dict[str, list[Point]], center: Point | None, reach: float) -> Iterator[Point]:
    """Points part-way from the centre towards the edge, i.e. over the base."""
    every = [p for pts in edges.values() for p in pts]
    cx, cy = center or base_center(edges)
    return itertools.cycle([(round(cx + (x - cx) * reach), round(cy + (y - cy) * reach)) for x, y in every])


SPOT_REACH = 0.33  # random spell spot: this share of the way from the centre to the deploy ring, at most


def spell_spot(edges: dict[str, list[Point]], center: Point | None, how: str, rng) -> Point:
    """The one spot for spells cast together: the middle of the base, or a
    random spot within its middle half. The deploy ring lies outside the
    walls, so "half the base" is about a third of the way out to it: the
    spot stays well inside the walls, never out at an edge or corner."""
    cx, cy = center or base_center(edges)
    if how != "random":
        return cx, cy
    every = [p for pts in edges.values() for p in pts]
    x, y = rng.choice(every)
    reach = SPOT_REACH * math.sqrt(rng.random())  # sqrt: spread evenly over the area, not bunched at the centre
    return round(cx + (x - cx) * reach), round(cy + (y - cy) * reach)


def spells_to_cast(game: Game, spells: list[Card], cfg: Config) -> list[Card]:
    """The spell cards to use: all of them, or only the Bat Spells."""
    if cfg.attack.spells == "all" or not spells:
        return spells
    if not game.vision.library.has(T.BAT_SPELL_CARD):
        log.warning("No Bat Spell picture (%s.png): casting no spells", T.BAT_SPELL_CARD)
        return []
    [(_, picture)] = game.vision.library.get(T.BAT_SPELL_CARD)[:1]
    screen = game.refresh()
    bats = [c for c in spells if armybar.card_is(screen, c, picture)]
    if len(bats) < len(spells):
        log.info("Casting %d Bat Spell card(s); leaving %d other spell card(s)", len(bats), len(spells) - len(bats))
    return bats


def base_center(edges: dict[str, list[Point]]) -> Point:
    every = [p for pts in edges.values() for p in pts]
    return sum(p[0] for p in every) // len(every), sum(p[1] for p in every) // len(every)


SPELL_BURST = 11  # taps in one go when a spell card's count can't be read
SPELL_GAP = 0.06  # human-like: about this long between one spell and the next (else as quick as it goes)


def cast_together(game: Game, card: Card, spot: Point, count: int | None, cfg: Config) -> None:
    """Every spell on ``card`` at once, on one spot: one burst of as many
    taps as the card holds, then more only if some are left."""
    taps = count or SPELL_BURST
    game.tap(*card.center)
    _wait(game, cfg, 0.1, SETTLE)
    if _rhythm(cfg):  # spells one after another, not all in one instant
        pause = _burst(game, cfg, taps, SPELL_GAP)
        game.note("battle_rhythm", SPELL_GAP * (taps - 1), SPELL_GAP * (taps - 1))
    else:
        pause = 0.0
        game.note("battle_rhythm", SPELL_GAP * (taps - 1) + human.pauses_extra(taps, SPELL_GAP))
    game.tap_many([spot] * taps, jitter=4, pause=pause)
    _wait(game, cfg, 0.3, SETTLE)
    if not armybar.is_spent(game.refresh(), card, cfg.bar):
        deploy_card(game, card, itertools.repeat(spot), cfg)


def batches_for(count: int | None, cfg: Config) -> int:
    """How many batches a card of ``count`` units may take: enough for half
    its taps to be refused (too close to the base, or lost in a burst), and
    never fewer than max_batches_per_card."""
    acfg = cfg.attack
    if not count:
        return acfg.max_batches_per_card
    needed = math.ceil(count * TAPS_PER_UNIT / max(acfg.taps_per_batch, 1)) + 2
    return min(max(acfg.max_batches_per_card, needed), MAX_BATCHES)


TAPS_PER_UNIT = 1.5  # taps allowed per unit on a card
MAX_BATCHES = 60  # a card's batches at most, whatever its count


def deploy_card(game: Game, card: Card, points: Iterator[Point], cfg: Config,
                fallback: Iterator[Point] | None = None, count: int | None = None) -> bool | None:
    """Select ``card`` and deploy until it greys out. True if it emptied,
    False if it was still deploying when its batches ran out, ``None`` if
    the game wouldn't take any more of it. If the game refuses ``points``,
    the ``fallback`` points are tried. ``count``: its units, when read (big
    cards get more batches).

    A batch is never more taps than the card has units left (its count is
    read again after each one), so the last units don't come with spare
    taps that would drop whatever the game selects once the card is empty."""
    acfg = cfg.attack
    if card.kind in ("single", "siege"):  # a hero or siege machine: one unit
        return place_single(game, card, points, cfg, fallback)
    stalled = 0
    progressed = False  # any of it went out
    before = game.refresh()
    left = count or read_count(game, card, cfg)
    batches = batches_for(left, cfg)
    for _ in range(batches):
        game.tap(*card.center)
        _wait(game, cfg, 0.15, SETTLE)
        size = human.batch(game.rng, acfg.taps_per_batch, _rhythm(cfg))
        taps = min(size, left) if left else size
        if isinstance(points, DeployPoints):
            side, batch = points.take(taps)
        else:
            side, batch = None, [next(points) for _ in range(taps)]
        game.tap_many(batch, jitter=3, pause=_burst(game, cfg, taps, acfg.tap_delay))
        _wait(game, cfg, 0.3, SETTLE)
        after = game.refresh()
        if pick_reward(game):
            after = game.screen
        if armybar.is_spent(after, card, cfg.bar):
            return True
        # The count on the card changes as units go out; if it stays the
        # same, the game refused that side's points: the side is dropped,
        # and once there's none left to drop, twice in a row means the game
        # is rejecting our points.
        now = read_count(game, card, cfg)
        if armybar.card_changed(before, after, card) or (now and left and now < left):
            stalled, progressed = 0, True
            if side is not None:
                points.landed(side)
            # Unread, it's at most the taps fewer (some may have been refused).
            left = now or (max(left - taps, 1) if left else None)
        elif side is not None and (points.redraw(side, after, cfg) or points.refused(side)):
            continue  # before stays: nothing changed
        else:
            stalled += 1
        if stalled >= 2:
            # Heroes go down with single taps, troops and spells with fast
            # bursts; on emulators where the bursts don't get through,
            # nothing but the card selection happens. Retry with plain taps.
            if game.stop_fast_taps():
                stalled = 0
                if isinstance(points, DeployPoints):
                    points.restore()  # the sides dropped lost their bursts, not their points
                continue
            if fallback is not None and points is not fallback:
                log.info("Card at x=%d is not deploying; trying the fixed deploy lines", card.left)
                points, stalled = fallback, 0
                continue
            log.warning("Card at x=%d is not deploying; moving on", card.left)
            return None
        before = after
    if not progressed:
        log.warning("Card at x=%d is not deploying; moving on", card.left)
        return None
    log.warning("Card at x=%d still has units after %d batches", card.left, batches)
    return False


SINGLE_TRIES = 4  # deploy points tried per hero before giving up on it


def place_single(game: Game, card: Card, points: Iterator[Point], cfg: Config,
                 fallback: Iterator[Point] | None = None) -> bool:
    """Put a hero or siege machine down, checking that it went out.

    Once a unit is on the field its card shows the unit's green health bar
    (or greys out). If neither happens the point was refused (say, too close
    to the base), so the next point is tried. True if it was placed.
    """
    before = game.refresh()
    for attempt in range(SINGLE_TRIES):
        # Half the tries on the detected points, half on the fixed lines.
        source = fallback if fallback is not None and attempt >= SINGLE_TRIES // 2 else points
        game.tap(*card.center)
        _wait(game, cfg, 0.15, SETTLE)
        game.tap(*next(source), jitter=3)
        _wait(game, cfg, 0.6, SETTLE)
        after = game.refresh()
        if pick_reward(game):
            after = game.screen
        if armybar.unit_on_field(before, after, card) or armybar.is_spent(after, card, cfg.bar):
            return True
    log.warning("Hero/siege card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
    return False


def warden_points(game: Game, singles: list[Card], points: Iterator[Point]) -> dict[Card, Iterator[Point]]:
    """The Grand Warden's card (from its picture), with points on the side
    most troops went to: a person puts him with the troops he's to guard."""
    if not singles or not isinstance(points, DeployPoints) or not game.vision.library.has(T.WARDEN_CARD):
        return {}
    side = points.busiest()
    if side is None:
        return {}
    [(_, picture)] = game.vision.library.get(T.WARDEN_CARD)[:1]
    screen = game.refresh()
    warden = next((c for c in singles if armybar.card_is(screen, c, picture)), None)
    if warden is None:
        return {}
    log.info("Grand Warden goes on the %s side, with the most troops (%d)", side, points.dropped[side])
    return {warden: points.on_side(side)}


HERO_BEAT = 0.6  # human-like: about this long between the last troops and the first hero
HERO_GAP = 0.35  # ...and between one hero and the next (else 0.08 s)


def place_singles(game: Game, cards: list[Card], points: Iterator[Point], cfg: Config,
                  fallback: Iterator[Point] | None = None,
                  own: dict[Card, Iterator[Point]] | None = None) -> list[Card]:
    """Put every hero and siege machine down in one quick pass (card, point,
    next card...), then check them all on one screenshot; any the game
    refused get another point. ``own``: points for some cards alone (the
    Warden's, where most troops went). Returns the cards that went out."""
    own = own or {}
    before = game.refresh()
    placed: list[Card] = []
    on = _rhythm(cfg)
    waiting = human.order(game.rng, cards, on)  # human-like: not always left to right
    if waiting:
        _beat(game, cfg, HERO_BEAT)  # a moment after the troops, as a person looks where to put them
    for attempt in range(SINGLE_TRIES):
        if not waiting:
            break
        late = fallback is not None and attempt >= SINGLE_TRIES // 2
        for card in waiting:
            source = fallback if late else own.get(card, points)
            game.tap(*card.center)
            _wait(game, cfg, 0.08, SETTLE)
            game.tap(*next(source), jitter=3)
            _beat(game, cfg, HERO_GAP, plain=0.08)
        _wait(game, cfg, 0.5, SETTLE)
        after = game.refresh()
        if pick_reward(game):
            after = game.refresh()
        out = [c for c in waiting if armybar.unit_on_field(before, after, c) or armybar.is_spent(after, c, cfg.bar)]
        placed += out
        waiting = [c for c in waiting if c not in out]
    for card in waiting:
        log.warning("Hero/siege card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
    return placed


def read_count(game: Game, card: Card, cfg: Config) -> int | None:
    """The "xN" on a troop or spell card, or ``None`` if it cannot be read."""
    width = card.right - card.left
    region = Rect(card.left + int(width * 0.45), card.top - 6, int(width * 0.55), 28)
    pieces = text.segment(game._current(), region)
    if len(pieces) < 2:
        return None
    font = game.vision.font(cfg.bar.count_font)
    digits = []
    for mask in pieces[1:]:  # the first piece is the "x"
        char, score = font.classify(mask)
        if score < 0.45 or not char.isdigit():
            return None
        digits.append(char)
    count = int("".join(digits))
    return count if 0 < count <= 400 else None


def deploy_taps(game: Game, card: Card, points: Iterator[Point], taps: int, cfg: Config) -> None:
    game.tap(*card.center)
    _wait(game, cfg, 0.15, SETTLE)
    # (round the sides, a point each)
    batch = points.round(taps) if isinstance(points, DeployPoints) else [next(points) for _ in range(taps)]
    game.tap_many(batch, jitter=3, pause=_burst(game, cfg, taps, cfg.attack.tap_delay))


def loot_left(game: Game, cfg: Config) -> Loot | None:
    return read_loot(game, cfg.regions)


def taken_share(target: Loot, left: Loot, cfg: Config) -> float:
    """Share of the base's reported loot already taken (0-1)."""
    total = loot_value(target, cfg.search)
    return 1.0 - loot_value(left, cfg.search) / total if total > 0 else 0.0


def storages_filled(target: Loot, left: Loot, room: dict[str, int]) -> bool:
    """Whether the loot taken (``target``, what scouting reported, less
    what's ``left``) fills the ``room`` left in every storage that counts."""
    return bool(room) and all(getattr(target, k) - getattr(left, k) >= space for k, space in room.items())


def finish_battle(game: Game, cfg: Config, target: Loot | None = None,
                  room: dict[str, int] | None = None) -> float | None:
    """Wait for the battle to end. End it early once the available loot has
    stopped dropping, or once ``end_at_loot_share`` of the ``target`` loot
    (what scouting reported) is taken, or once what's taken fills the
    ``room`` left in the storages, or at the time limit; then go home.
    Returns the share of ``target`` taken, if it could be read."""
    acfg = cfg.attack
    started = last_change = game.clock()
    last_loot = None
    can_return = game.has(T.RETURN_HOME) and game.has(T.SURRENDER_OK)
    # Human-like: how long without loot before leaving, and the share of
    # loot that's enough, vary a little each battle.
    spread = cfg.human.uses("end_spread")
    no_loot = human.end_wait(game.rng, acfg.no_loot_seconds, spread)
    enough_share = human.end_share(game.rng, acfg.end_at_loot_share, spread)
    room = room if target and acfg.end_when_full else None
    filled_once = False  # full on two readings in a row: one misread doesn't end it
    while True:
        # Nothing is tapped while the battle plays out, so the check made before
        # taps is made here: a game that crashed or closed shows no loot, and
        # the battle would otherwise be watched to its time limit.
        game.check_in_front()
        game.refresh()
        if pick_reward(game):
            continue
        if can_return and game.find(T.RETURN_HOME):
            break
        if acfg.speed_up and game.has(T.SPEED) and game.click(T.SPEED):
            log.info("Battle speed: 4x")
            game.refresh()
        now = game.clock()
        loot = (loot_left(game, cfg) if acfg.end_when_no_loot or (target and acfg.end_at_loot_share) or room
                else None)
        # Loot only goes down in a battle: a higher reading is a misread.
        if loot is not None and loot != last_loot and (last_loot is None or _not_more(loot, last_loot)):
            last_loot, last_change = loot, now
        stalled = acfg.end_when_no_loot and last_loot is not None and now - last_change >= no_loot
        share = taken_share(target, last_loot, cfg) if target and last_loot else 0.0
        enough = bool(acfg.end_at_loot_share) and share >= enough_share
        filled_now = bool(room) and loot is not None and storages_filled(target, last_loot, room)
        full, filled_once = filled_once and filled_now, filled_now
        if stalled or enough or full or now - started >= acfg.battle_timeout:
            reason = ("the storages are full" if full else
                      "%d%% of the loot taken" % (share * 100) if enough else
                      "no loot gained for %ds" % no_loot if stalled else "time limit")
            log.info("Ending battle: %s", reason)
            if stalled:
                game.note("end_spread", human.end_wait_extra(acfg.no_loot_seconds), no_loot - acfg.no_loot_seconds)
            if not can_return:
                # Without the Return Home / Okay pictures, closing the game is
                # the way out: the attack ends with what it has already won,
                # and the next round starts the game again at home.
                log.info("Closing the game to leave the battle (add the Return Home and Okay pictures to skip this)")
                game.device.stop_app(cfg.device.package)
                game.wait(3)
                return
            game.click(T.END_BATTLE)
            _beat(game, cfg, 0.5)  # a moment to read the question
            game.click_when_visible(T.SURRENDER_OK, timeout=5)
            break
        _wait(game, cfg, 2, note=False)  # (a longer look doesn't make the battle longer)
    taken = taken_share(target, last_loot, cfg) if target and last_loot else None
    if taken is not None:
        log.info("Took %d%% of the base's loot", taken * 100)
    game.wait(1.0)
    if not game.find(T.RETURN_HOME) and not _wait_for_result(game, RESULT_TIMEOUT):
        _keep_result_screen(game)
        raise ResultNotFound(f"result screen not detected: return-home button never appeared {RESULT_TIMEOUT}s "
                             f"after the battle "
                             f"({game.scores(T.RETURN_HOME, T.END_BATTLE, T.SURRENDER_OK)}; UI scale "
                             f"{game.ui_scale or 1.0:.2f})")
    look_at_result(game)
    if not return_home(game):
        # Tapped, and still showing: the next round's popup check tries again.
        log.warning("Return Home was tapped %d times and still shows (%s)", RETURN_TRIES,
                    game.scores(T.RETURN_HOME))
        _keep_result_screen(game)
    open_event_chests(game)
    return taken


class ResultNotFound(RuntimeError):
    """After a battle, no way home was found on its result screen."""


RESULT_TIMEOUT = 25  # s for the result screen after a battle (slow emulators, delayed results)
RESULT_SCREENS = Path("screens/results")  # the last few result screens the bot couldn't leave


def _keep_result_screen(game: Game) -> None:
    from farmbot.tasks.village import _keep_screen

    _keep_screen(RESULT_SCREENS, game.screen)
    log.warning("No way home on the result screen; kept it in %s", RESULT_SCREENS)


def _wait_for_result(game: Game, timeout: float) -> bool:
    """Until Return Home shows, taking any reward offered over the results
    on the way (a "Pick a Reward!" after a star bonus, say). True if it did."""
    deadline = game.clock() + timeout
    while True:
        game.check_in_front()  # a closed game is that, not a missing result screen
        game.refresh()
        if game.find(T.RETURN_HOME):  # (its pictures include "Claim Reward")
            return True
        if pick_reward(game):
            continue
        if game.clock() >= deadline:
            return False
        game.wait(1.0, exact=True)


RETURN_TRIES = 3


def return_home(game: Game) -> bool:
    """Press Return Home (or Claim Reward) on the result screen until it's
    gone. True once it is."""
    for _ in range(RETURN_TRIES):
        game.refresh()
        if pick_reward(game):  # the 100% reward can show over the results
            continue
        if not game.click(T.RETURN_HOME):
            return True  # gone already
        if _gone(game):
            return True
    return False


def _gone(game: Game) -> bool:
    """Return Home went away within a few seconds of a tap."""
    deadline = game.clock() + 4
    while game.clock() < deadline:
        game.wait(0.5, exact=True)
        game.refresh()
        if not game.find(T.RETURN_HOME):
            return True
    return False


def look_at_result(game: Game) -> None:
    """A look at the stars and loot before going home ("Look at the
    result"), in either village."""
    seconds = human.result_look(game.rng) if game.uses("result_pause") else 0.0
    game.note("result_pause", human.result_extra(), seconds)
    if seconds:
        game.wait(seconds, exact=True)


def _not_more(loot: Loot, before: Loot) -> bool:
    return loot.gold <= before.gold and loot.elixir <= before.elixir and loot.dark <= before.dark


BAR_PAGES = 6  # screenfuls of the bar at most
BAR_SAME = 0.6  # the middle of the bar still matches this well where it was: it didn't scroll


def _bar_strip(game: Game, cfg: Config) -> np.ndarray:
    """The lower half of the troop bar in grey: the cards' level badges and
    pictures, which stay put when a card is selected, placed or hurt."""
    import cv2

    w, h = game.size
    band = Rect.from_list(cfg.bar.band).resolve(w, h)
    lower = game.refresh()[band.y + band.h // 2:band.y + band.h]
    return cv2.cvtColor(lower, cv2.COLOR_BGR2GRAY)


def bar_scrolled(before: np.ndarray, after: np.ndarray) -> bool:
    """True if the bar's cards slid sideways between the two strips (not just
    changed: a placed hero's card shows its health, a spent one greys out)."""
    import cv2

    if before.shape != after.shape or float(np.abs(before.astype(np.int16) - after).mean()) < 1:
        return False  # nothing changed at all
    w = before.shape[1]
    middle = before[:, int(w * 0.3):int(w * 0.7)]
    found = cv2.matchTemplate(after, middle, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, _) = cv2.minMaxLoc(found)
    return not (score >= BAR_SAME and abs(x - int(w * 0.3)) <= 10)


def scroll_bar(game: Game, cfg: Config, forward: bool = True) -> bool:
    """Swipe the troop bar on to the next cards (or back). True if it moved:
    a big army doesn't fit on the screen, and the bar scrolls."""
    w, h = game.size
    band = Rect.from_list(cfg.bar.band).resolve(w, h)
    y = band.y + band.h // 2
    before = _bar_strip(game, cfg)
    if forward:
        game.swipe(int(w * 0.85), y, int(w * 0.25), y, 400)
    else:
        game.swipe(int(w * 0.17), y, int(w * 0.93), y, 400)
    game.wait(0.8)
    return bar_scrolled(before, _bar_strip(game, cfg))


BAR_ROOM = 1.5  # card widths of room right of the last card: the bar can't scroll
BAR_GAP = 0.6  # of a card: more between cards (or before the first) is one not found


def bar_fits(game: Game, cfg: Config) -> bool:
    """The whole army is on show: its cards end well before the screen's
    right edge (empty slots after them), so there is nothing to scroll to
    and no test swipe is needed."""
    cards = sorted(armybar.find_cards(game.refresh(), cfg.bar), key=lambda c: c.left)
    if not cards or cards[0].left > BAR_GAP * cfg.bar.card_width:
        return False
    # a gap in the row: a card not found, so maybe more past the edge too
    if any(b.left - a.right > BAR_GAP * cfg.bar.card_width for a, b in zip(cards, cards[1:])):
        return False
    return cards[-1].right < game.size[0] - BAR_ROOM * cfg.bar.card_width


def rewind_bar(game: Game, cfg: Config) -> None:
    for _ in range(BAR_PAGES):
        if not scroll_bar(game, cfg, forward=False):
            return


PLACED_HEALTH = 0.5  # a hero/siege card with this much green bar on it is out already


def _out_already(screen, card: Card) -> bool:
    """A hero or siege machine placed on an earlier screenful: once the bar
    scrolls on, its card can still show at the edge. Tapping it again would
    fire its ability early. (An unplaced siege card shows a little green of
    its own, well under a placed unit's bar.)"""
    health = armybar.hero_health(screen, card)
    return health is not None and health >= PLACED_HEALTH


def _placed_on_show(game: Game, cfg: Config) -> list[Card]:
    screen = game.refresh()
    return [c for c in armybar.find_cards(screen, cfg.bar)
            if c.kind in ("single", "siege") and _out_already(screen, c)]


def _bar_cards(game: Game, cfg: Config) -> list[Card]:
    acfg = cfg.attack
    cards = armybar.find_cards(game.refresh(), cfg.bar)
    if not acfg.use_siege and any(c.kind == "siege" for c in cards):
        log.info("Leaving the siege machine out (turn on Use siege machines to deploy it)")
        cards = [c for c in cards if c.kind != "siege"]
    return cards


def attack(game: Game, cfg: Config, target: Loot | None = None, room: dict[str, int] | None = None) -> float | None:
    """Attack the base on screen; ``target`` is its loot as scouted, and
    ``room`` what the storages still hold (the battle ends once that's taken).
    Returns the share of it taken, when the loot could be read.

    The bar goes troops, siege machines, heroes, spells. When it holds more
    than fits on the screen, it's deployed a screenful at a time, in that
    order, scrolling on until its end."""
    acfg = cfg.attack
    game.look_around()  # "Look around before deploying": before the view is measured
    # The game centres a battle's view by itself, at the village's zoom: a
    # zoom-out is all it needs.
    game.zoom_out()
    edges, center = deploy_edges(game, cfg)
    if center is None and (cfg.redline.enabled or cfg.zone.enabled):
        # Neither found the base: most likely still zoomed in (a pinch the
        # game missed, from a village left zoomed in on an upgrade).
        log.info("Deploy points: the base wasn't found; zooming out again")
        # Pinched for real: the view may only look like the last zoom-out's.
        game.zoom_out(force=True)
        edges, center = deploy_edges(game, cfg)
        if center is None:
            log.warning("Deploy points: the base still wasn't found after zooming out (the view may be zoomed "
                        "in); dropping troops on the fixed lines")
    edge_points = cycle_points(edges, acfg.sides, game.rng, _rhythm(cfg))
    fixed_points = cycle_points(fixed_lines(game.size[0], cfg), acfg.sides, game.rng, _rhythm(cfg))
    over_base = spell_points(edges, center, acfg.spell_reach)
    spot = spell_spot(edges, center, acfg.spell_spot, game.rng)
    if acfg.spell_placement == "together":
        log.info("Spells go on %s (%s)", spot, acfg.spell_spot)

    scrolls = not bar_fits(game, cfg) and scroll_bar(game, cfg)
    if scrolls:
        log.info("The troop bar scrolls: deploying it a screenful at a time")
        rewind_bar(game, cfg)
    waves = 1 if scrolls else acfg.waves  # waves only when the whole army is on show

    placed: list[Card] = []
    singles_seen = placed_total = 0
    for page in range(BAR_PAGES):
        cards = _bar_cards(game, cfg)
        if not cards and page == 0:
            raise RuntimeError("no cards found in the troop bar")
        counts = {card: read_count(game, card, cfg) for card in cards if card.kind not in ("single", "siege")}
        log.info("Troop bar%s: %s", f" (screen {page + 1})" if scrolls else "", ", ".join(
            f"{c.kind} x{counts[c]}" if counts.get(c) else c.kind for c in cards))
        troops = [c for c in cards if c.kind == "troop"]
        singles = [c for c in cards if c.kind in ("single", "siege") and not _out_already(game.screen, c)]
        spells = spells_to_cast(game, [c for c in cards if c.kind == "spell"], cfg)

        # Troops in waves: earlier waves send a share of each card (by its
        # count, or one batch when unreadable); the last wave empties them.
        remaining = dict(counts)
        unfinished: list[Card] = []
        for wave in range(waves):
            if waves > 1:
                log.info("Wave %d of %d", wave + 1, waves)
            last = wave == waves - 1
            for card in troops:
                if last:
                    if deploy_card(game, card, edge_points, cfg, fixed_points, remaining.get(card)) is False:
                        unfinished.append(card)
                    continue
                left = remaining.get(card)
                share = (human.share(game.rng, left, waves - wave, _rhythm(cfg)) if left else
                         human.batch(game.rng, acfg.taps_per_batch, _rhythm(cfg)))
                deploy_taps(game, card, edge_points, share, cfg)
                if left:
                    remaining[card] = left - share
            if not last:
                _wait(game, cfg, acfg.wave_delay)

        own = warden_points(game, singles, edge_points)
        here = place_singles(game, singles, edge_points, cfg, fixed_points, own)
        for card in unfinished:
            # Troops a card still holds: another go, now the others are down.
            if not armybar.is_spent(game.refresh(), card, cfg.bar):
                log.info("Card at x=%d still has units; deploying the rest", card.left)
                deploy_card(game, card, edge_points, cfg, fixed_points)
        if spells:
            _beat(game, cfg, SPELL_BEAT)
        for card in spells:
            if acfg.spell_placement == "together":
                cast_together(game, card, spot, counts.get(card), cfg)
            else:
                deploy_card(game, card, over_base, cfg)
        # One more go for any hero that was refused, now on other points.
        here += place_singles(game, [c for c in singles if c not in here], edge_points, cfg, fixed_points, own)
        singles_seen += len(singles)
        placed_total += len(here)
        # Abilities are fired from the cards on show at the end: earlier
        # screens' cards have scrolled away.
        placed = here if scrolls else placed + here
        if not scrolls or not scroll_bar(game, cfg):
            break
    if singles_seen:
        log.info("Placed %d of %d hero/siege card(s)", placed_total, singles_seen)
    if scrolls and len(placed) < placed_total:
        # Abilities need the cards on show: the bar's end may hold only some
        # of them (or none), so look one screenful back too.
        placed = _placed_on_show(game, cfg)
        if len(placed) < placed_total and scroll_bar(game, cfg, forward=False):
            back = _placed_on_show(game, cfg)
            if len(back) > len(placed):
                placed = back

    if placed and acfg.hero_abilities:
        # Heroes: a second tap fires the ability. Siege machines: it
        # releases the troops inside.
        if acfg.ability_health > 0:
            fire_abilities_when_hurt(game, placed, cfg)
        else:
            fire_abilities_on_time(game, placed, cfg)
        log.info("Activated %d hero/siege card(s)", len(placed))
    return finish_battle(game, cfg, target, room)


ABILITY_CHECK = 0.5  # seconds between looks at the heroes' health
SPELL_BEAT = 0.8  # human-like: about this long between the heroes and the spells


def fire_abilities_on_time(game: Game, cards: list[Card], cfg: Config) -> None:
    """Fire each ability ``hero_ability_delay`` after the heroes landed:
    all together, or, human-like, each at its own time around it."""
    acfg = cfg.attack
    times = human.ability_times(game.rng, len(cards), acfg.hero_ability_delay, _rhythm(cfg))
    start = game.clock()
    for card, at in zip(human.order(game.rng, cards, _rhythm(cfg)), times):
        wait = at - (game.clock() - start)
        if wait > 0:
            game.wait(wait, exact=True)  # (to the moment: the battle goes on meanwhile)
        game.tap(*card.center)
        game.wait(0.2)


def fire_abilities_when_hurt(game: Game, cards: list[Card], cfg: Config) -> None:
    """Watch each placed hero's health bar and fire its ability once it drops
    to ``ability_health``, or when its bar disappears (about to fall), or at
    ``ability_max_wait`` at the latest."""
    acfg = cfg.attack
    on = _rhythm(cfg)
    waiting = list(cards)
    # Human-like: each hero's own threshold, and its own last moment.
    limit = {card: human.threshold(game.rng, acfg.ability_health, on) for card in cards}
    start = game.clock()
    # "At the latest" stays the latest: a hero's own deadline is a little
    # before it, never after.
    deadline = {card: start + acfg.ability_max_wait * (game.rng.uniform(0.85, 1.0) if on else 1.0)
                for card in cards}
    while waiting:
        screen = game.refresh()
        if pick_reward(game):
            continue
        now = game.clock()
        for card in list(waiting):
            health = armybar.hero_health(screen, card)
            late = now >= deadline[card]
            if late or health is None or health <= limit[card]:
                if on and not late and health is not None:
                    game.wait(human.reaction(game.rng), exact=True)  # seen, then tapped
                game.tap(*card.center)
                game.wait(0.15)
                waiting.remove(card)
                log.info("Ability at x=%d: %s", card.left, "time's up" if late else
                         "health gone" if health is None else f"health {health:.0%}")
        if waiting:
            game.wait(ABILITY_CHECK)
