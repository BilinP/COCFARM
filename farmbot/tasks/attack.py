"""Deploy every troop in the bar around the base and see the battle through."""

from __future__ import annotations

import itertools
import logging
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np

from farmbot import armybar, buildings, deployzone, human, lootspots, plan, redline as rl, result, text
from farmbot.armybar import Card
from farmbot.config import Config
from farmbot.game import VIEW_STILL, Game, T, view_change
from farmbot.geometry import Point, Rect, points_along
from farmbot.tasks.search import Loot, loot_value, read_loot
from farmbot.tasks.village import open_event_chests, pick_reward

log = logging.getLogger(__name__)


def deploy_edges(game: Game, cfg: Config, war_map: bool = False) -> tuple[dict[str, list[Point]], Point | None]:
    """Points to drop troops on, per attacked side, and the base centre.

    Tries the red line, then the ring around the building area (by its
    grass or its cluster of buildings, then, on busy scenery, the busy blob
    round the middle: :func:`deployzone.detect_busy`), and fills
    any side still empty from the fixed ``deploy_lines``. ``war_map``: a
    clan war base, whose ground ends at the line (rock and lava beyond
    it): the line isn't judged by the ground around it there. Where each
    side's points came from ("red line", "building ring" or "fixed line")
    is kept in ``game.deploy_origins``.
    """
    screen = game.refresh()
    sides = cfg.attack.sides
    found: dict[str, list[Point]] = {}
    sources: dict[str, str] = {}
    center: Point | None = None
    outline: list[Point] = []  # the whole base's edge, as first found (see :func:`loot_front`)
    detectors = (
        ("red line", cfg.redline.enabled, lambda: rl.detect(screen, cfg.redline), cfg.redline.min_side_points),
        ("building ring", cfg.zone.enabled, lambda: deployzone.detect(screen, cfg.zone), 1),
        # busy scenery round the map: the ring round the base's clutter, cut loose from it
        ("building ring", cfg.zone.enabled, lambda: deployzone.detect_busy(screen, cfg.zone), 1),
    )
    def take(name, result, minimum, on):
        nonlocal center
        if result is None:
            return
        blocked = deployzone.open_ground(on, cfg.zone) if name == "red line" and not war_map else None
        if not outline:
            outline.extend(p for pts in result.sides.values() for p in pts)  # every side's, in use or not
        for side in sides:
            points = result.sides.get(side, [])
            if name == "red line" and not war_map:
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
        for s, pts in found.items():
            found[s] = [p for p in pts if p[1] < floor]
            if not found[s]:
                found[s], sources[s] = lines[s], "fixed line"
    w, h = screen.shape[1], screen.shape[0]
    for s, pts in found.items():
        # The speed button and the Overall Damage on the right: a troop tap there toggles 4x.
        kept = [p for p in pts if not in_corner(p, w, h)]
        found[s] = kept or pts
    log.info("Deploy points: %s", ", ".join(f"{s} {len(found[s])} ({sources[s]})" for s in sides))
    game.deploy_origins = dict(sources)
    game.deploy_outline = outline
    return found, center


BUTTON_ROW_MARGIN = 12  # px kept clear above the battle buttons
RIGHT_CORNER = (0.88, 0.56, 0.86)  # the speed button and the Overall Damage: x from, y from, y to (screen shares)


def in_corner(point: Point, width: int, height: int) -> bool:
    """``point`` is on the speed button or the Overall Damage, right of the map."""
    x0, y0, y1 = RIGHT_CORNER
    return point[0] >= x0 * width and y0 * height <= point[1] <= y1 * height


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


SPEED_SETTLE = 2.5  # s after tapping the speed button before it's looked at again (it changes a moment later)
SPEED_GREEN = 0.3  # share of the button that's green, at least (grey: just pressed, not to be tapped)


def speed_up(game: Game, cfg: Config) -> bool:
    """Tap the speed button if it shows "1x" on the screen last taken, so the
    battle plays at 4x. It usually shows only in a battle's last minute;
    during some events, from the start. Asked once the whole army is down,
    heroes and siege too, and their abilities fired (sped up sooner, the
    troops walk in before the rest are placed), then on each look while the
    battle runs. The button toggles: tapped again it goes back to 1x. So it
    is tapped only when it reads "1x" (not "4x", which the 1x picture
    matches nearly as well), is green (grey: just pressed), and wasn't
    tapped in the last SPEED_SETTLE seconds."""
    if not cfg.attack.speed_up or not game.has(T.SPEED):
        return False
    if game.clock() - getattr(game, "speed_tapped_at", -SPEED_SETTLE) < SPEED_SETTLE:
        return False
    one = game.find(T.SPEED)
    if one is None:
        return False
    if game.has(T.SPEED_ON):
        four = game.find(T.SPEED_ON, threshold=0.3, region=Rect(one.x - one.w, one.y - one.h, one.w * 2, one.h * 2))
        if four is not None and four.score >= one.score:
            return False  # already 4x
    box = game.screen[max(one.y - one.h // 2, 0):one.y + one.h // 2, max(one.x - one.w // 2, 0):one.x + one.w // 2]
    hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV) if box.size else None
    if hsv is None or float(((hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 90) & (hsv[:, :, 1] >= 90)).mean()) < SPEED_GREEN:
        return False  # grey: pressed a moment ago, changing
    game.tap_match(one)
    game.speed_tapped_at = game.clock()
    log.info("Battle speed: 4x")
    return True


DAMAGE_JUMP = 30  # league: an Overall Damage reading this many points above the last one is a misread
LEAGUE_TAPS = 12  # league bonus farming: taps in one go, at least (fewer looks at the card between)
LEAGUE_LOOK = 0.5  # s between looks at the Overall Damage while waiting for it
LEAGUE_READS = 2  # readings in a row at the damage wanted before leaving (one misread doesn't end it)
DAMAGE_FONT = "fonts/capital"  # the Overall Damage's digits: the same white outlined ones as the Capital map's %


def league_settings(cfg: Config) -> Config:
    """The settings a league bonus battle is fought with: everything down at
    once, as fast as it goes, round the whole base (heroes too, their
    abilities straight away, the Earthquakes anywhere); no waves, walks,
    funnels or waits that only pay off in a long battle. The battle is left
    on the Overall Damage (see :func:`finish_battle`), not the loot."""
    a = cfg.attack
    fast = replace(a, placement="spread", waves=1, troop_speed=9, wave_speed=9, hero_walk="off", funnel="off",
                   quake_aim="spots", spell_placement="spread", hero_ability_delay=0.0, ability_health=0.0,
                   taps_per_batch=max(a.taps_per_batch, LEAGUE_TAPS),
                   end_at_loot_share=0.0)  # (the bonus wants the damage: not left once most of the loot is in)
    return replace(cfg, attack=fast)


def damage_font(game: Game):
    try:
        return game.vision.font(DAMAGE_FONT)
    except (FileNotFoundError, ValueError, AttributeError):
        return None


def button_row_top(game: Game) -> int | None:
    """Top of the row of battle buttons (End Battle, Boost Army, Boost
    Heroes, Next): taps at or below it could press one of them, so no troop
    is dropped there. ``None`` if neither End Battle nor Next is seen."""
    tops = [m.y - m.h // 2 for name in (T.END_BATTLE, T.NEXT) if game.has(name)
            for m in game.find_all(name)[:1]]
    return min(tops) - BUTTON_ROW_MARGIN if tops else None


POINT_MAX_BLOCKED = 0.5  # a red-line point with more non-ground around it is dropped
SIDE_MAX_BLOCKED = 0.2  # and a side whose points typically have more is not trusted
TEXTURE_SIDE_MAX = 0.2  # with no grass to judge by: a side whose points are typically busier with edges is a wall's
# Grass in shade (the skull jungle's forest) reads partly as non-ground: a
# side just over SIDE_MAX_BLOCKED is still trusted where its points sit on
# plain ground, hardly any edges round them.
SHADED_SIDE_MAX = 0.35
PLAIN_SIDE_MAX = 0.1


def on_open_ground(screen, points: list[Point], cfg: Config, blocked=None, rims: bool = False) -> list[Point]:
    """Red-line points that sit on open ground. The real boundary runs over
    grass; wall edges that look like it (golden walls do) sit among
    buildings, so a side whose points are mostly cluttered is dropped.
    ``blocked``: the screen's ``deployzone.open_ground``, if already made.
    ``rims``: a line found along the rims of ground of its colour
    (``RedLine.rims``): wall edges inside a base can look like that too,
    so with no grass to judge by it isn't used.

    With hardly any grass or dirt on screen (a city's paving, a stone
    floor, snow, ice) the ground's colour tells nothing; there a point is
    judged by how busy with edges it is round about instead
    (:func:`deployzone.texture`): the line runs over plain ground, a
    (golden) wall's edge through a clutter of buildings. Texture also
    rescues a side on grass in shade, just over the grass limit
    (``SHADED_SIDE_MAX``), whose points are plain round about."""
    if not points:
        return points
    if blocked is None:
        blocked = deployzone.open_ground(screen, cfg.zone)
    most, side_most = POINT_MAX_BLOCKED, SIDE_MAX_BLOCKED
    if deployzone.unknown_ground(blocked):
        if rims:
            return []
        blocked, most, side_most = deployzone.texture(screen), deployzone.TEXTURE_BUSY, TEXTURE_SIDE_MAX
    shares = [float(blocked[y, x]) for x, y in points]
    kept = [p for p, b in zip(points, shares) if b <= most]
    if not kept:
        return []
    typical = sorted(b for b in shares if b <= most)[len(kept) // 2]
    if typical > side_most:
        if side_most != SIDE_MAX_BLOCKED or typical > SHADED_SIDE_MAX:
            return []
        busy = deployzone.texture(screen)
        if sorted(float(busy[y, x]) for x, y in kept)[len(kept) // 2] > PLAIN_SIDE_MAX:
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

    def __init__(self, edges: dict[str, list[Point]], sides: list[str], rng=None, human_like: bool = False,
                 spare: list[str] | tuple[str, ...] = ()):
        # ``spare``: sides kept back ("Fewer sides some battles"), brought in
        # if the game refuses the ones in use, so troops always have somewhere to go.
        # Human-like: each side's points from a random one, in a random
        # direction, and the sides in a new order each battle (the same
        # points: only where along them, and which side first).
        self.rng, self.human_like = rng, human_like and rng is not None
        on = self.human_like
        self.streams = {s: itertools.cycle(human.walk(rng, edges[s], on)) for s in [*sides, *spare]
                        if edges.get(s)}
        if not self.streams:
            raise RuntimeError("no deploy points on any side")
        first = [s for s in self.streams if s in sides] or list(self.streams)
        self.reserve = [s for s in self.streams if s not in first]
        self.live = human.order(rng, first, on) if on else first
        self.misses: dict[str, int] = {}
        self.turn = 0
        self.seen_land: set[str] = set()  # sides where troops have landed
        self.redrawn: set[str] = set()  # sides moved onto the red line a refusal showed
        self.war_map = False  # a clan war base: the red line isn't judged by the ground around it
        self.dropped: dict[str, int] = {}  # troop taps per side
        self.spots: list[tuple[str, Point, float]] = []  # where units went out: side, point, how many
        self.all_round = False  # league bonus farming: each batch round every side, a tap each (not one side's)

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

    def line(self, units: int) -> tuple[str, list[Point]]:
        """A stretch of one side for a finger to slide along with ``units``
        streaming out: its next points, as far as they follow on from one
        another (not across the gap between the side's two ends), about
        SLIDE_PER_UNIT of it for each unit. One point: no room to slide."""
        side = self._side()
        stream = self.streams[side]
        path = [next(stream)]
        length = 0.0
        while length < units * human.SLIDE_PER_UNIT and len(path) < LINE_POINTS:
            point = next(stream)
            step = math.hypot(point[0] - path[-1][0], point[1] - path[-1][1])
            if step > human.SLIDE_JUMP or point in path:
                break
            path.append(point)
            length += step
        self.dropped[side] = self.dropped.get(side, 0) + 1
        return side, path

    def round(self, count: int) -> list[Point]:
        """``count`` troop taps round the sides, a point each."""
        batch = []
        for _ in range(count):
            side = self._side()
            self.dropped[side] = self.dropped.get(side, 0) + 1
            batch.append(next(self.streams[side]))
        return batch

    def went(self, side: str, points: list[Point], units: int) -> None:
        """``units`` went out on ``points`` of ``side`` (seen on the card's count)."""
        if units > 0 and points:
            self.spots += [(side, point, units / len(points)) for point in points]

    def heart(self) -> tuple[str, Point, int] | None:
        """Where the troops are thickest: the point units went out on with
        the most of them within HEART_REACH of it, as (side, point, how
        many), or None if none were seen to go out."""
        best = None
        for side, (x, y), _ in self.spots:
            if side not in self.live:
                continue
            near = sum(n for _, (px, py), n in self.spots if math.hypot(px - x, py - y) <= HEART_REACH)
            if best is None or near > best[2]:
                best = (side, (x, y), near)
        return None if best is None else (best[0], best[1], round(best[2]))

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
        if len(self.live) == 1 and self.reserve:
            log.info("Deploy points: the %s side is refused; bringing in %s", side, ", ".join(self.reserve))
            self.live += self.reserve
            self.reserve = []
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
            if not self.war_map:
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
        self.reserve = []
        self.misses.clear()


SIDE_REFUSALS = 2  # batches in a row a side's points go nowhere before it's dropped
SKIP_POINT = 0.2  # human-like: chance a point along a side is passed over


def cycle_points(edges: dict[str, list[Point]], sides: list[str], rng=None,
                 human_like: bool = False, spare: list[str] | tuple[str, ...] = ()) -> DeployPoints:
    return DeployPoints(edges, sides, rng, human_like, spare)


GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))  # each spread spell this far round from the last: evenly round, any count
SPREAD_JITTER = 0.15  # spread spells: this share in or out of the reach, and of a radian round, at random
RING_GAP = math.radians(15)  # no deploy point this near a direction (cut off by the buttons): the ring's ellipse there


def _ring(edges: dict[str, list[Point]], center: Point | None):
    """The base's centre and a function giving, for a direction (in the
    base's own proportions: radians round an ellipse), the point of the
    deploy ring that way."""
    every = [p for pts in edges.values() for p in pts]
    cx, cy = center or base_center(edges)
    rx = max(max(abs(x - cx) for x, _ in every), 1)
    ry = max(max(abs(y - cy) for _, y in every), 1)
    angles = [(math.atan2((y - cy) / ry, (x - cx) / rx), (x, y)) for x, y in every]

    def toward(angle: float) -> tuple[float, float]:
        def off(a: float) -> float:
            return abs((a - angle + math.pi) % math.tau - math.pi)

        gap, point = min(((off(a), p) for a, p in angles), key=lambda t: t[0])
        if gap > RING_GAP:
            return cx + rx * math.cos(angle), cy + ry * math.sin(angle)
        return point

    return (cx, cy), toward, min(rx, ry)


def spell_points(edges: dict[str, list[Point]], center: Point | None, reach: float, rng=None) -> Iterator[Point]:
    """Spread spells: points ``reach`` of the way from the centre to the
    deploy ring, evenly round the centre (each one a golden angle on from
    the last, from a random start), a little in or out at random. However
    many spells there are, they go all round the base, not in a row."""
    (cx, cy), toward, _ = _ring(edges, center)
    start = rng.random() * math.tau if rng is not None else 0.0
    for k in itertools.count():
        angle, out = start + k * GOLDEN_ANGLE, reach
        if rng is not None:
            angle += rng.uniform(-SPREAD_JITTER, SPREAD_JITTER)
            out *= 1 + rng.uniform(-SPREAD_JITTER, SPREAD_JITTER)
        x, y = toward(angle)
        yield round(cx + (x - cx) * out), round(cy + (y - cy) * out)


SPOT_REACH = 0.4  # random spell spot: this share of the way from the centre to the deploy ring, at most (the middle half)
SPOT_MOVE = 0.5  # a random spot is at least this share of SPOT_REACH from the last battle's
SPOT_TRIES = 30


def spell_spot(edges: dict[str, list[Point]], center: Point | None, how: str, rng,
               last: Point | None = None) -> Point:
    """The one spot for spells cast together: the middle of the base, or a
    random spot within its middle half, a new one each battle (well away
    from ``last``, the last battle's). The deploy ring lies outside the
    walls, so "half the base" is under half the way out to it."""
    (cx, cy), toward, near = _ring(edges, center)
    if how != "random":
        return round(cx), round(cy)
    spot = (round(cx), round(cy))
    for _ in range(SPOT_TRIES):
        x, y = toward(rng.random() * math.tau)
        reach = SPOT_REACH * math.sqrt(rng.random())  # sqrt: spread evenly over the area, not bunched at the centre
        spot = round(cx + (x - cx) * reach), round(cy + (y - cy) * reach)
        if last is None or math.dist(spot, last) >= SPOT_MOVE * SPOT_REACH * near:
            break
    return spot


def spell_aim(edges: dict[str, list[Point]], center: Point | None, focus: list[str] | None, acfg, rng,
              last: Point | None = None) -> Point:
    """The spells' one spot this battle. Fought for stars, they follow the
    push (``focus``: its sides): its spot, or with "random" somewhere
    round it, new each battle. Else :func:`spell_spot`."""
    if not focus:
        return spell_spot(edges, center, acfg.spell_spot, rng, last)
    px, py = plan.push_spot(edges, center, focus, acfg.star_spell_depth)
    if acfg.spell_spot != "random":
        return px, py
    _, _, near = _ring(edges, center)
    spot = (px, py)
    for _ in range(SPOT_TRIES):
        angle, reach = rng.random() * math.tau, SPOT_REACH * near * 0.5 * math.sqrt(rng.random())
        spot = round(px + reach * math.cos(angle)), round(py + reach * math.sin(angle))
        if last is None or math.dist(spot, last) >= SPOT_MOVE * SPOT_REACH * near * 0.25:
            break
    return spot


def _cards_like(game: Game, spells: list[Card], name: str) -> list[Card] | None:
    """The cards of ``spells`` that show the picture ``name`` (None: there's no such picture)."""
    if not game.vision.library.has(name):
        return None
    [(_, picture)] = game.vision.library.get(name)[:1]
    screen = game.refresh()
    return [c for c in spells if armybar.card_is(screen, c, picture)]


def spells_to_cast(game: Game, spells: list[Card], cfg: Config) -> list[Card]:
    """The spell cards to use: all of them, only the Bat Spells, or the
    Bat Spells and Earthquakes (see :func:`quake_cards` for which those are).
    Never the Invisibility Spells: spread round the base they hide nothing
    (a hero walk casts its own on the walker)."""
    if not spells:
        return spells
    hidden = _cards_like(game, spells, T.INVISIBILITY_SPELL_CARD) or []
    if hidden:
        log.info("Leaving %d Invisibility Spell card(s) out: they only help on a hero walk", len(hidden))
        spells = [c for c in spells if c not in hidden]
    if cfg.attack.spells == "all" or not spells:
        return spells
    bats = _cards_like(game, spells, T.BAT_SPELL_CARD)
    if bats is None:
        log.warning("No Bat Spell picture (%s.png): casting no Bat Spells", T.BAT_SPELL_CARD)
        bats = []
    quakes = quake_cards(game, spells) if cfg.attack.spells == "earthquake" else []
    if cfg.attack.spells == "earthquake" and not game.vision.library.has(T.EARTHQUAKE_SPELL_CARD):
        log.warning("No Earthquake Spell picture (%s.png): casting no Earthquakes", T.EARTHQUAKE_SPELL_CARD)
    chosen = [c for c in spells if c in bats or c in quakes]
    if len(chosen) < len(spells):
        log.info("Casting %d Bat Spell card(s)%s; leaving %d other spell card(s)", len([c for c in chosen if c in bats]),
                 f" and {len(quakes)} Earthquake card(s)" if quakes else "", len(spells) - len(chosen))
    return chosen


def quake_cards(game: Game, spells: list[Card]) -> list[Card]:
    """The Earthquake Spell cards among ``spells`` (none without their picture)."""
    return (_cards_like(game, spells, T.EARTHQUAKE_SPELL_CARD) or []) if spells else []


QUAKE_SPOTS = 5  # Earthquake spots at most: a card whose count isn't read stops there


def quake_spots(first: Point, edges: dict[str, list[Point]], center: Point | None, rng) -> Iterator[Point]:
    """Where Earthquakes go, a few at a time: the spells' spot first, then
    new random spots in the middle half, each well away from the last."""
    spot = first
    while True:
        yield spot
        spot = spell_spot(edges, center, "random", rng, spot)


def cast_quakes(game: Game, card: Card, spots: Iterator[Point], count: int | None, cfg: Config) -> None:
    """The Earthquakes on ``card``, ``quakes_per_spot`` on a spot, then the
    next ones on the next spot, until the card's spent. Half a spot's worth
    or more left over get a spot of their own (11 by 4: 4, 4 and 3, not 4 and
    7 piled up on one); fewer join the last spot: too few alone to do much."""
    per = cfg.attack.quakes_per_spot
    left = count
    groups = QUAKE_SPOTS if count is None else max(1, count // per + (2 * (count % per) >= per))
    for group in range(groups):
        spot = next(spots)
        taps = per if left is None else (left if group == groups - 1 else min(per, left))
        game.tap(*card.center)
        _wait(game, cfg, 0.1, SETTLE)
        game.tap_many([spot] * taps, jitter=3, pause=max(SPELL_GAP, QUAKE_GAP) if _rhythm(cfg) else QUAKE_GAP)
        _wait(game, cfg, 0.3, SETTLE)
        if left is None:
            log.info("Earthquakes: %d on %s", taps, spot)
        else:
            left -= taps
            went = taps - _top_up_quakes(game, card, spot, left, taps, cfg)
            log.info("Earthquakes: %d of %d on %s", went, taps, spot)
        # (a known count was just checked on a screen taken after the taps)
        if (left is not None and left <= 0) or armybar.is_spent(
                game.screen if left is not None else game.refresh(), card, cfg.bar):
            return


QUAKE_GAP = 0.12  # s between Earthquakes on one spot at the least: taps on one spot closer than
# that, the game took some as one (issue 28: 2 or 3 of 4 went down)
QUAKE_TOP_UPS = 2  # times the Earthquakes a spot missed are tapped again


def _settled_count(game: Game, card: Card, cfg: Config) -> int | None:
    """The card's count once it reads the same twice running (the number
    can lag the last spell by a moment), or ``None`` if it won't settle or
    can't be read."""
    first = read_count(game, card, cfg)
    for _ in range(3):
        game.wait(0.25, exact=True)
        game.refresh()
        now = read_count(game, card, cfg)
        if now == first:
            return now
        first = now
    return None


def _top_up_quakes(game: Game, card: Card, spot: Point, left: int, taps: int, cfg: Config) -> int:
    """After ``taps`` Earthquakes on ``spot``: the card should hold ``left``.
    Any it still holds beyond that didn't go down, so they're tapped there
    again, slower, a few times at most; never more than the spot was meant
    to get (the rest are the next spot's). Returns how many still didn't go."""
    missing = 0
    for attempt in range(QUAKE_TOP_UPS + 1):
        if armybar.is_spent(game.refresh(), card, cfg.bar):
            return 0  # all gone (no count to read on a spent card)
        now = read_count(game, card, cfg)
        if now is not None and now > left:  # (a count still catching up reads high, never low)
            now = _settled_count(game, card, cfg)
        if now is None or now <= left:
            return 0 if now is not None else missing  # all went down, or there's no telling
        missing = min(now - left, taps)
        if attempt == QUAKE_TOP_UPS:
            break
        log.info("Earthquakes: %d of %d didn't go down on %s; again", missing, taps, spot)
        game.tap(*card.center)
        _wait(game, cfg, 0.15, SETTLE)
        game.tap_many([spot] * missing, jitter=3, pause=QUAKE_GAP * 2)
        _wait(game, cfg, 0.3, SETTLE)
    log.warning("Earthquakes: %d of %d still didn't go down on %s", missing, taps, spot)
    return missing


# Earthquakes on the troops' way in ("path"): each spot this share of the way
# from the stretch the troops go down on to the base's centre. The deploy
# line lies a little outside the walls: the first opens the outer walls in
# front of them, the next the walls round the core.
QUAKE_DEPTHS = (0.3, 0.55, 0.8)


def path_spots(edges: dict[str, list[Point]], center: Point | None, sides: list[str]) -> Iterator[Point]:
    """Where Earthquakes go on the troops' way in from ``sides`` (their
    stretches in ``edges``): the outer walls in front of them first, then
    deeper toward the core; any more spots on the deepest."""
    for depth in itertools.chain(QUAKE_DEPTHS, itertools.repeat(QUAKE_DEPTHS[-1])):
        yield plan.push_spot(edges, center, sides, depth)


def quake_front(edges: dict[str, list[Point]], troop_edges: dict[str, list[Point]], center: Point | None,
                sides: list[str], push: list[str] | None, origins: dict[str, str], rng, width: int
                ) -> tuple[list[str], dict[str, list[Point]]]:
    """Where the troops go in behind the Earthquakes: the push's sides when
    fought for stars, else one side (the widest found on the base itself; the
    one with the most points if none was), and on each only its stretch
    facing the quakes' line, so the whole army goes through the opening
    together. Returns those sides and the troops' points with the stretches."""
    have = [s for s in sides if troop_edges.get(s)]
    if not have:
        return sides, troop_edges
    entry = [s for s in push or [] if s in have]
    if not entry:
        entry = plan.focus_sides(troop_edges, origins, have, "side", rng) or [
            max(have, key=lambda s: len(troop_edges[s]))]
    at = plan.push_spot(troop_edges, center, entry, 0.0)  # the middle of the entry's points
    return entry, {**troop_edges, **{s: lootspots.front(troop_edges[s], at, width) for s in entry}}


FUNNEL_REACH = 1.6  # a funnel's two ends: this many FRONT_REACHes either way of the middle of the way in
FUNNEL_TROOPS = 2  # funnel "troops": this many units of the first troop card at each end
FUNNEL_WAIT = 6.0  # s for the funnel to clear the outside before the main army goes in


def funnel_ends(edges: dict[str, list[Point]], sides: list[str], at: Point, width: int) -> tuple[Point, Point] | None:
    """The two ends of a funnel round ``at`` (the middle of the way in), on
    the deploy points of ``sides`` (their whole length): the point nearest
    FUNNEL_REACH away on one side of it, then the one nearest that far on the
    other (well away from the first). None without points both ways."""
    points = [p for s in sides for p in edges.get(s, [])]
    if not points:
        return None
    reach = lootspots.scaled(lootspots.FRONT_REACH * FUNNEL_REACH, width)
    first = min(points, key=lambda p: abs(math.dist(p, at) - reach))
    other = [p for p in points if math.dist(p, first) >= reach]
    if not other:
        return None
    second = min(other, key=lambda p: abs(math.dist(p, at) - reach))
    return first, second


def warden_card(game: Game, singles: list[Card]) -> Card | None:
    """The Grand Warden's card among ``singles`` (by his picture), if known."""
    if not singles or not game.vision.library.has(T.WARDEN_CARD):
        return None
    pictures = [picture for _, picture in game.vision.library.get(T.WARDEN_CARD)]
    screen = game.refresh()
    return next((c for c in singles if any(armybar.card_is(screen, c, p) for p in pictures)), None)


def open_funnel(game: Game, cfg: Config, ends: tuple[Point, Point], troops: list[Card], singles: list[Card],
                counts: dict[Card, int | None], fallback: Iterator[Point]) -> list[Card]:
    """Before the main army: a few units at each end of the way in, to clear
    the buildings outside it, so the army behind them walks straight in
    rather than round the base. "troops": FUNNEL_TROOPS of the first troop
    card at each end (taken off its count); "heroes": every hero but the
    Grand Warden (he stays with the main army), shared between the ends.
    Then FUNNEL_WAIT s for them to get at it. Returns the heroes that went."""
    how = cfg.attack.funnel
    went: list[Card] = []
    if how == "heroes":
        warden = warden_card(game, singles)
        heroes = [c for c in singles if c.kind == "single" and c is not warden]
        if heroes:
            log.info("Funnel: %d hero(es) at the ends of the way in, %s and %s%s", len(heroes), ends[0], ends[1],
                     "; the Grand Warden stays with the army" if warden else "")
            went = place_singles(game, heroes, itertools.cycle(ends), cfg, fallback)
    elif how == "troops" and troops:
        card = troops[0]
        log.info("Funnel: %d troop(s) at each end of the way in, %s and %s", FUNNEL_TROOPS, ends[0], ends[1])
        deploy_taps(game, card, iter([ends[0]] * FUNNEL_TROOPS + [ends[1]] * FUNNEL_TROOPS), 2 * FUNNEL_TROOPS, cfg)
        if counts.get(card):
            counts[card] = max(counts[card] - 2 * FUNNEL_TROOPS, 0)
        went = [card]
    if went:
        _wait(game, cfg, FUNNEL_WAIT, SETTLE)
    return [c for c in went if c.kind == "single"]


def quakes_ready(game: Game, cfg: Config) -> bool:
    """Earthquakes to cast first, on the troops' way in: chosen, on show on
    the bar's first screenful (a bar that scrolls shows its spells last), and
    enough to open a wall (``quakes_per_spot``; a count not read is taken as
    enough)."""
    acfg = cfg.attack
    if acfg.spells not in ("earthquake", "all") or acfg.quake_aim != "path":
        return False
    quakes = quake_cards(game, [c for c in _bar_cards(game, cfg) if c.kind == "spell"])
    if not quakes:
        log.info("Earthquakes: none on show at the start (or none brought): the troops go in as usual")
        return False
    counts = [read_count(game, c, cfg) for c in quakes]
    if None not in counts and sum(counts) < acfg.quakes_per_spot:
        log.info("Earthquakes: only %d, too few to open a wall: the troops go in as usual", sum(counts))
        return False
    return True


def base_center(edges: dict[str, list[Point]]) -> Point:
    """The middle of the deploy points' extent (not their average: a side
    found with more points than another would pull that towards it)."""
    every = [p for pts in edges.values() for p in pts]
    xs, ys = [x for x, _ in every], [y for _, y in every]
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


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


RIM_REACH = 0.45  # league spells: this share of the way from the base's centre out to its deploy diamond (the outer buildings)
RIM_ISO = 0.75  # the map's diamond: its half-height at most this share of its half-width
RIM_FINGERS = 2  # league spells: this many go down together, as with two fingers
RIM_PACE = (0.06, 0.14)  # s between one pair and the next, at random
RIM_GAP = 0.04  # s between the two fingers of a pair


def rim_points(edges: dict[str, list[Point]], center: Point | None, count: int, rng=None,
               reach: float = RIM_REACH) -> list[Point]:
    """``count`` spots evenly round the base's outer part (not its middle),
    from a random start, a little in or out and round at random."""
    # A diamond, as the map is (an oval through the deploy points reaches out
    # past the base's corners, onto the grass the troops went down on). Its
    # width from the side points; its height no more than the map's shape
    # allows (the top and bottom points are cut short by the screen).
    every = [p for pts in edges.values() for p in pts]
    cx, cy = center or base_center(edges)
    left = [p for side in ("nw", "sw") for p in edges.get(side, [])]
    right = [p for side in ("ne", "se") for p in edges.get(side, [])]
    if left and right:
        # The diamond's left and right corners are level with its middle
        # (the top and bottom points are cut short by the screen's edges).
        low, high = min(left), max(right)
        cx, cy = (low[0] + high[0]) / 2, (low[1] + high[1]) / 2
    rx = max(max(abs(x - cx) for x, _ in every), 1)
    ry = rx * RIM_ISO
    start = rng.random() * math.tau if rng is not None else 0.0
    step = math.tau / max(count, 1)
    spots = []
    for k in range(count):
        angle, out = start + k * step, reach
        if rng is not None:
            angle += rng.uniform(-SPREAD_JITTER, SPREAD_JITTER) * step / 2
            out *= 1 + rng.uniform(-SPREAD_JITTER, SPREAD_JITTER) / 2
        c, s = math.cos(angle), math.sin(angle)
        edge = abs(c) + abs(s)  # (on the diamond |x|/rx + |y|/ry = 1)
        spots.append((round(cx + rx * c / edge * out), round(cy + ry * s / edge * out)))
    return spots


def cast_round_rim(game: Game, card: Card, edges: dict[str, list[Point]], center: Point | None,
                   count: int | None, cfg: Config) -> None:
    """Every spell on ``card`` (the Totems of a league bonus battle) evenly
    round the base's outer part, two at a time as with two fingers, a
    short pause between pairs: quick, not a burst in one blink."""
    taps = count or SPELL_BURST
    spots = rim_points(edges, center, taps, game.rng)
    game.tap(*card.center)
    _wait(game, cfg, 0.1, SETTLE)
    for i in range(0, len(spots), RIM_FINGERS):
        game.tap_many(spots[i:i + RIM_FINGERS], jitter=4, pause=RIM_GAP)
        if i + RIM_FINGERS < len(spots):
            game.wait(game.rng.uniform(*RIM_PACE), exact=True)
    _wait(game, cfg, 0.3, SETTLE)
    if not armybar.is_spent(game.refresh(), card, cfg.bar):
        deploy_card(game, card, itertools.cycle(rim_points(edges, center, max(taps, 4), game.rng)), cfg)


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


Rescue = Callable[[], "tuple[Iterator[Point], Iterator[Point] | None] | None"]


def deploy_card(game: Game, card: Card, points: Iterator[Point], cfg: Config,
                fallback: Iterator[Point] | None = None, count: int | None = None,
                rescue: Rescue | None = None) -> bool | None:
    """Select ``card`` and deploy until it greys out. True if it emptied,
    False if it was still deploying when its batches ran out, ``None`` if
    the game wouldn't take any more of it. If the game refuses ``points``,
    the ``fallback`` points are tried. ``count``: its units, when read (big
    cards get more batches). ``rescue``: asked once, the first time a
    batch goes nowhere: new points and fallback points to carry on with
    (the view turned out zoomed in, and was zoomed out), or None.

    A batch is never more taps than the card has units left (its count is
    read again after each one), so the last units don't come with spare
    taps that would drop whatever the game selects once the card is empty."""
    acfg = cfg.attack
    if card.kind in ("single", "siege", "castle"):  # one deployment, including standalone Castle troops
        return place_single(game, card, points, cfg, fallback)
    stalled = 0
    progressed = False  # any of it went out
    before = game.refresh()
    left = count or read_count(game, card, cfg)
    # "Hold to deploy": a big card goes down in a mix of held fingers and bursts of taps.
    round_sides = isinstance(points, DeployPoints) and points.all_round
    holding = (card.kind == "troop" and isinstance(points, DeployPoints) and not round_sides
               and game.uses("hold_deploy"))
    batches = batches_for(left, cfg)
    for _ in range(batches):
        game.tap(*card.center)
        _wait(game, cfg, 0.15, SETTLE)
        if (holding and left and left >= human.HOLD_FROM and game.hold_misses < HOLD_MISSES
                and game.rng.random() < human.HOLD_SHARE):
            now = hold_out(game, card, points, cfg, left)
            if now is None:
                return True  # all of it went
            if now < left:
                left, progressed, stalled = now, True, 0
                before = game.refresh()
                continue
            holding = False  # nothing went: taps from here (they show which side is refused)
        size = human.batch(game.rng, acfg.taps_per_batch, _rhythm(cfg))
        if round_sides and left:
            size = max(size, left)  # league bonus farming: the whole card in one go, round every side
        taps = min(size, left) if left else size
        if round_sides:
            side, batch = None, points.round(taps)  # every side in turn, a tap each: round the whole base
        elif isinstance(points, DeployPoints):
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
        went = armybar.card_changed(before, after, card) or (now and left and now < left)
        if not went and rescue is not None:
            fresh, rescue = rescue(), None
            if fresh is not None:
                points, fallback = fresh
                stalled = 0
                before = game.refresh()
                continue
        if round_sides and taps and left and now and (left - now) < taps * ROUND_LANDED:
            # Some sides' points are in the red area (a base that fills the map): a side at a
            # time from here, so the refused ones show up and are dropped.
            round_sides = False
        if went:
            stalled, progressed = 0, True
            if side is not None:
                points.landed(side)
                points.went(side, batch, (left - now) if now and left else len(batch))
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


LINE_POINTS = 12  # of a side's points a slide goes through at most
ROUND_LANDED = 0.8  # a round-every-side burst with fewer of its taps landing: a side at a time after it
HOLD_MISSES = 3  # holds in a session that dropped nothing before it goes back to taps for good


def hold_out(game: Game, card: Card, points: DeployPoints, cfg: Config, left: int) -> int | None:
    """"Hold to deploy": stream some of a big card's units (it is selected)
    with one held finger: mostly sliding along a stretch of a side, now
    and then on one spot (always, where the device can't slide it), and
    read the card's count after. Returns the units left (never fewer
    than its last few, which are tapped: a hold a moment too long would
    drop the next card), ``left`` itself if nothing went, or None if the
    card emptied. How fast a held finger streams units is measured from
    each hold and kept (until then a quick stream is assumed, so the
    holds come out short); after HOLD_MISSES holds that dropped nothing,
    there are no more this session."""
    for _ in range(1):
        units = human.hold_units(game.rng, left)
        if not units:
            return left
        ms = human.hold_length(units, game.hold_rate)
        side, path = points.line(units)
        if game.rng.random() < human.HOLD_STILL:
            path = path[:1]
        began = time.monotonic()
        if not game.slide(path, ms):
            path = path[:1]
            if not game.hold(path[0][0], path[0][1], ms):
                game.hold_misses = HOLD_MISSES  # this device can't hold
                return left
        took = time.monotonic() - began
        _wait(game, cfg, 0.3, SETTLE)
        after = game.refresh()
        if pick_reward(game):
            after = game.screen
        if armybar.is_spent(after, card, cfg.bar):
            # More went than the hold was for (the device held longer than asked, or the card reads as empty).
            log.info("Card at x=%d: held %d ms for %d of its %d (the hold took %.1f s); the card emptied",
                     card.left, ms, units, left, took)
            return None
        now = read_count(game, card, cfg)
        if not now:
            # Not read (the number still settling, seen live after a slide): one more look.
            _wait(game, cfg, 0.3, SETTLE)
            game.refresh()
            now = read_count(game, card, cfg)
        if not now or now >= left:
            # Refused there, or not read: the taps take it from here (and show which side is refused).
            game.hold_misses += 1
            log.info("Card at x=%d: a held finger dropped nothing (%d ms, %s; the count read %s after %d); "
                     "tapping instead", card.left, ms,
                     f"slid {path[0]} to {path[-1]}" if len(path) > 1 else f"on {path[0]}", now, left)
            return left
        game.hold_rate = human.hold_rate(left - now, ms, game.hold_rate)
        game.hold_misses = 0
        log.debug("Card at x=%d: held %d ms for %d (took %.1f s, %s), %d went (stream: %s a second)", card.left, ms,
                  units, took, f"slid {path[0]} to {path[-1]}" if len(path) > 1 else f"on {path[0]}", left - now,
                  f"{game.hold_rate:.1f}" if game.hold_rate else "not measured")
        points.landed(side)
        points.went(side, path, left - now)
        points.dropped[side] = points.dropped.get(side, 0) + (left - now) - 1  # (take counted one)
        left = now
    return left


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
    log.warning("Hero/siege/Clan Castle card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
    return False


HEART_REACH = 130  # px: troops this near a point count as with it (the Warden's aura reaches about as far)


def warden_points(game: Game, singles: list[Card], points: Iterator[Point]) -> dict[Card, Iterator[Point]]:
    """The Grand Warden's card (from its picture), with the point most
    troops went out at (then others on that side, should the game refuse
    it): a person puts him right with the troops he's to guard. Where
    they went wasn't seen (waves, an unread card): the side with the most."""
    if not singles or not isinstance(points, DeployPoints) or not game.vision.library.has(T.WARDEN_CARD):
        return {}
    heart = points.heart()
    side = heart[0] if heart else points.busiest()
    if side is None:
        return {}
    # His picture is just the mouth and beard: the card's corners show his equipment and the air/ground
    # button, which differ from one account to the next (with them in, it matched 0.77 on another account).
    pictures = [picture for _, picture in game.vision.library.get(T.WARDEN_CARD)]  # (his looks: one per portrait)
    screen = game.refresh()
    warden = next((c for c in singles if any(armybar.card_is(screen, c, p) for p in pictures)), None)
    if warden is None:
        log.debug("Grand Warden: his card isn't among the %d hero card(s) (not brought, or a look not known)", len(singles))
        return {}
    if heart is None:
        log.info("Grand Warden goes on the %s side, with the most troops (%d)", side, points.dropped[side])
        return {warden: points.on_side(side)}
    log.info("Grand Warden goes where most troops went out: (%d, %d) on the %s side, %d of them",
             heart[1][0], heart[1][1], side, heart[2])
    return {warden: itertools.chain([heart[1]], points.on_side(side))}


HERO_BEAT = 0.6  # human-like: about this long between the last troops and the first hero
HERO_GAP = 0.35  # ...and between one hero and the next (else 0.08 s)
QUICK_HERO_GAP = 0.09  # league bonus farming: s between the taps of the heroes' one burst


def place_singles(game: Game, cards: list[Card], points: Iterator[Point], cfg: Config,
                  fallback: Iterator[Point] | None = None,
                  own: dict[Card, Iterator[Point]] | None = None,
                  tapped: dict[int, float] | None = None, quick: bool = False) -> list[Card]:
    """Put every hero and siege machine down in one quick pass (card, point,
    next card...), then check them all on one screenshot; any the game
    refused get another point. ``own``: points for some cards alone (the
    Warden's, where most troops went). ``tapped``: filled with when each
    card (by id) last went onto the field. Returns the cards that went out."""
    own = own or {}
    before = game.refresh()
    placed: list[Card] = []
    on = _rhythm(cfg)
    waiting = human.order(game.rng, cards, on)  # human-like: not always left to right
    # The Warden first, straight after the troops he's to guard (they're already walking in).
    waiting = [c for c in waiting if c in own] + [c for c in waiting if c not in own]
    if waiting and not quick:
        _beat(game, cfg, HERO_BEAT)  # a moment after the troops, as a person looks where to put them
    for attempt in range(SINGLE_TRIES):
        if not waiting:
            break
        late = fallback is not None and attempt >= SINGLE_TRIES // 2
        if quick:
            # League bonus farming: every hero in one go, card then spot, card then spot.
            taps = [p for card in waiting for p in (card.center, next(fallback if late else own.get(card, points)))]
            game.tap_many(taps, jitter=3, pause=QUICK_HERO_GAP)
            now = game.clock()
            for card in waiting:
                if tapped is not None:
                    tapped[id(card)] = now
        for card in ([] if quick else waiting):
            source = fallback if late else own.get(card, points)
            game.tap(*card.center)
            _wait(game, cfg, 0.08, SETTLE)
            game.tap(*next(source), jitter=3)
            if tapped is not None:
                tapped[id(card)] = game.clock()
            _beat(game, cfg, HERO_GAP, plain=0.08)
        _wait(game, cfg, 0.5, SETTLE)
        after = game.refresh()
        if pick_reward(game):
            after = game.refresh()
        out = [c for c in waiting if armybar.unit_on_field(before, after, c) or armybar.is_spent(after, c, cfg.bar)]
        placed += out
        waiting = [c for c in waiting if c not in out]
    for card in waiting:
        log.warning("Hero/siege/Clan Castle card at x=%d was not placed after %d tries", card.left, SINGLE_TRIES)
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


STAR_BATTLE = 240.0  # s a battle fought for stars may take: its 3 min, the 30 s before it, and a margin


def finish_battle(game: Game, cfg: Config, target: Loot | None = None,
                  room: dict[str, int] | None = None, for_stars: bool = False,
                  league: bool = False) -> float | None:
    """Wait for the battle to end. End it early once the available loot has
    stopped dropping, or once ``end_at_loot_share`` of the ``target`` loot
    (what scouting reported) is taken, or once what's taken fills the
    ``room`` left in the storages, or at the time limit; then go home.
    Returns the share of ``target`` taken, if it could be read.
    ``for_stars``: none of that: the battle is left to end by itself (all
    destroyed, the troops gone, or the game's own clock), however the
    loot stands. ``league``: league bonus farming: it ends as soon as the
    Overall Damage reaches ``league_bonus_damage`` too, looked at often
    (the battle plays at 4x)."""
    acfg = cfg.attack
    if for_stars:
        acfg = replace(acfg, end_when_no_loot=False, end_at_loot_share=0.0, end_when_full=False,
                       battle_timeout=max(acfg.battle_timeout, STAR_BATTLE))
        target = room = None
    started = last_change = game.clock()
    if for_stars and game.star_plan:
        game.star_plan.setdefault("times", {})["battle"] = started
    last_loot = None
    can_return = game.has(T.RETURN_HOME) and game.has(T.SURRENDER_OK)
    # Human-like: how long without loot before leaving, and the share of
    # loot that's enough, vary a little each battle.
    spread = cfg.human.uses("end_spread")
    no_loot = human.end_wait(game.rng, acfg.no_loot_seconds, spread)
    enough_share = human.end_share(game.rng, acfg.end_at_loot_share, spread)
    room = room if target and acfg.end_when_full else None
    filled_once = False  # full on two readings in a row: one misread doesn't end it
    font = damage_font(game) if league and not for_stars else None
    if league and not for_stars and font is None:
        log.warning("League bonus farming: the Overall Damage can't be read (no %s font); the battle ends by "
                    "the loot rules instead", DAMAGE_FONT)
    wanted = round(acfg.league_bonus_damage * 100)
    at_damage = 0  # readings in a row at the damage wanted
    damage = None
    best_damage = -1  # (league: the highest Overall Damage read, and when it last went up)
    loot_read_at = None  # when the loot was last read (league: not on every look)
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
        if speed_up(game, cfg):
            game.refresh()
        now = game.clock()
        if font is not None:
            damage = result.overall_damage(game.screen, font)
            at_damage = at_damage + 1 if damage is not None and damage >= wanted else 0
            if damage is not None and damage > best_damage and (best_damage < 0 or damage - best_damage <= DAMAGE_JUMP):
                # Still rising (defences give damage but no loot): the battle isn't stalled.
                best_damage, last_change = damage, now
        looks_at_loot = acfg.end_when_no_loot or (target and acfg.end_at_loot_share) or room
        if font is not None and loot_read_at is not None and now - loot_read_at < 2:
            looks_at_loot = False  # (league: the damage is looked at often, the loot as before)
        loot = loot_left(game, cfg) if looks_at_loot else None
        if looks_at_loot:
            loot_read_at = now
        # Loot only goes down in a battle: a higher reading is a misread.
        if loot is not None and loot != last_loot and (last_loot is None or _not_more(loot, last_loot)):
            last_loot, last_change = loot, now
        stalled = acfg.end_when_no_loot and last_loot is not None and now - last_change >= no_loot
        share = taken_share(target, last_loot, cfg) if target and last_loot else 0.0
        enough = bool(acfg.end_at_loot_share) and share >= enough_share
        full = False
        if looks_at_loot:  # (league: two readings in a row, not two looks at one)
            filled_now = bool(room) and loot is not None and storages_filled(target, last_loot, room)
            full, filled_once = filled_once and filled_now, filled_now
        bonus = at_damage >= LEAGUE_READS
        if bonus or stalled or enough or full or now - started >= acfg.battle_timeout:
            reason = ("%d%% damage: the league bonus is paid in full" % damage if bonus else
                      "the storages are full" if full else
                      "%d%% of the loot taken" % (share * 100) if enough else
                      ("no loot or damage gained for %ds" if font is not None else "no loot gained for %ds") % no_loot
                      if stalled else "time limit")
            log.info("Ending battle: %s%s", reason,
                     " (to keep attacking, turn off Attacking → End battle when storages are full)" if full else "")
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
        if font is not None:
            game.wait(LEAGUE_LOOK, exact=True)  # (at 4x, 70% can come in a few seconds)
        else:
            _wait(game, cfg, 2, note=False)  # (a longer look doesn't make the battle longer)
    if for_stars and game.star_plan:
        game.star_plan.setdefault("times", {})["end"] = game.clock()
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
    read_result(game)
    look_at_result(game)
    if not return_home(game):
        # Tapped, and still showing: the next round's popup check tries again.
        log.warning("Return Home was tapped %d times and still shows (%s)", RETURN_TRIES,
                    game.scores(T.RETURN_HOME))
        _keep_result_screen(game)
    if getattr(game, "note_wanted", False):
        # A Clan Games challenge going: the note saying it's completed shows only a few seconds.
        from farmbot.tasks import clan_games

        game.games_note = clan_games.catch_note(game)
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


RESULT_READS = 6  # tries at a steady reading of the stars and damage (they count up)


def read_result(game: Game) -> result.Result | None:
    """The battle's stars and Total damage off its result screen, once two
    readings agree (kept in ``game.last_result``); None if not read."""
    try:
        font = game.vision.font(T.LOOT_FONT)
    except Exception:  # noqa: BLE001 (no font: the stars alone)
        font = None
    last, found = object(), None
    for attempt in range(RESULT_READS):
        if attempt:
            game.wait(0.6, exact=True)
            game.refresh()
        now = result.read(game.screen, font)
        if now == last and (now is None or now.damage is not None):
            found = now
            break
        last = now
    else:
        found = last if isinstance(last, result.Result) else None
    game.last_result = found
    if found is not None:
        log.info("Result: %d star%s, %s damage", found.stars, "" if found.stars == 1 else "s",
                 "?" if found.damage is None else f"{found.damage}%")
    return found


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


BASE_SCREENS = Path("screens/bases")  # the last few war and ranked bases: to cut building pictures from
LOOT_SCREENS = Path("screens/loot_bases")  # the last few bases troops went toward the loot on: the same


class BaseRead:
    """What a battle fought for stars knows of its army and the base."""

    def __init__(self, kind: str | None = None, lure: Card | None = None, castle: Point | None = None,
                 air_defenses: list[Point] | None = None):
        self.kind = kind  # "air", "ground" or None (not known)
        self.lure = lure  # the lure troop's card, when there's one and the Clan Castle was found
        self.castle = castle
        self.air_defenses = air_defenses or []

    @property
    def toward(self) -> list[Point]:
        """What the push faces: the Clan Castle when lured, else the Air Defenses for an air army."""
        if self.lure is not None and self.castle is not None:
            return [self.castle]
        return self.air_defenses if self.kind == "air" else []


def read_army_and_base(game: Game, cfg: Config) -> BaseRead:
    """The army's kind and lure troop (by their card pictures) and where the
    Clan Castle and Air Defenses stand (by their building pictures). Only
    what the user taught pictures of is ever found; the rest stays unknown."""
    from farmbot.tasks.village import _keep_screen

    library = game.vision.library
    screen = game.refresh()
    _keep_screen(BASE_SCREENS, screen)

    def pictures(name: str) -> list[np.ndarray]:
        return [p for _, p in library.get(name)] if library.has(name) else []

    cards = _bar_cards(game, cfg)
    units = [c for c in cards if c.kind != "spell"]
    kind = plan.army_kind(screen, units, pictures(T.AIR_CARDS), pictures(T.GROUND_CARDS), armybar.card_is)
    lures = pictures(T.LURE_CARDS)
    lure = next((c for c in cards if c.kind == "troop" and any(armybar.card_is(screen, c, p) for p in lures)), None)
    castle = next(iter(buildings.find(game.vision, screen, T.CLAN_CASTLE)), None) if lure else None
    if lure is not None and castle is None:
        log.info("Lure troop brought, but the Clan Castle wasn't found: no lure this battle")
    air = buildings.find(game.vision, screen, T.AIR_DEFENSE, buildings.AIR_DEFENSE_LIKENESS) if kind == "air" else []
    if kind:
        log.info("The army is a%s %s army", "n" if kind == "air" else "", kind)
    return BaseRead(kind, lure if castle else None, castle, air)


@dataclass
class LootFront:
    """Where troops go toward the loot (see :func:`loot_front`)."""
    sides: list[str]  # the sides nearest the heaps of loot, troops split between them
    stretches: dict[str, list[Point]]  # each usable side's stretch nearest the loot (spare ones too)
    heaps: list[Point]  # where each heap of loot sits, biggest first


def loot_front(game: Game, cfg: Config, edges: dict[str, list[Point]], target: Loot | None = None
               ) -> LootFront | None:
    """For troops going toward the loot: each big heap of loot on the base
    (storages spread round it make several), the side nearest it and the
    stretch of that side's points nearest it, troops split between those
    sides. The other usable sides get their stretch nearest the loot too,
    for when the game refuses the ones picked. None (spread as usual) when
    no loot, or no side on the base itself, was found. Looked for on the
    screen the deploy points were measured on, inside them (see
    ``farmbot/lootspots.py``); ``target``, the base's loot as scouted,
    weighs gold against elixir."""
    from farmbot.tasks.village import _keep_screen

    origins = game.deploy_origins or {}
    usable = [s for s in cfg.attack.sides if origins.get(s) in plan.TRUSTED and edges.get(s)]
    if not usable:
        log.info("Toward the loot: the base's edge wasn't found; troops spread as usual")
        return None
    screen = game.screen if game.screen is not None else game.refresh()
    _keep_screen(LOOT_SCREENS, screen)  # (to cut loot building pictures from)
    outline = game.deploy_outline or [p for s in usable for p in edges[s]]  # (the whole base)
    taught = buildings.find(game.vision, screen, T.LOOT_BUILDINGS)
    spots = lootspots.find(screen, outline, cfg.zone.exclude, taught)
    width = screen.shape[1]
    found = [at for at, _ in lootspots.heaps(spots, width, target)]
    if not found:
        log.info("Toward the loot: no storage or collector was seen plainly; troops spread as usual")
        return None
    # A stream of points per heap, so each heap gets an even share of the
    # troops: named for its side ("nw"), or "nw-2" for a second heap nearest
    # the same side (the side's name is what the red line moves onto).
    stretches: dict[str, list[Point]] = {}
    sides, names = [], []
    for at in found:
        side = plan.nearest_side(edges, usable, at)
        name = side if side not in stretches else f"{side}-{sum(s.split('-')[0] == side for s in sides) + 1}"
        stretches[name] = lootspots.front(edges[side], at, width)
        sides.append(name)
        names.append(side)
    for s in usable:  # spares: toward the heap nearest them
        if s not in stretches:
            at = min(found, key=lambda h: min(math.dist(h, p) for p in edges[s]))
            stretches[s] = lootspots.front(edges[s], at, width)
    log.info("Toward the loot: %d heap(s) of loot seen, at %s (%d elixir patch(es), %d taught building(s)); "
             "troops go in from the %s side%s, on the points nearest each", len(found),
             ", ".join(map(str, found)), sum(s.kind == "elixir" for s in spots),
             sum(s.kind == "taught" for s in spots), ", ".join(names), "" if len(found) == 1 else "s")
    return LootFront(sides, stretches, found)


CONFIRM = Rect(280, 250, 620, 410)  # the "Confirm Attack" question (war and ranked alike)


def into_battle(game: Game, timeout: float, who: str) -> bool:
    """From a war base's or Ranked Battle's Find a Match to the base on
    screen, as both go: the army's window (its own Attack!), then "Confirm
    Attack" (this is where one of the day's or the war's attacks is spent),
    and an obstructed layout's question if the game asks. True once the
    battle's End Battle shows. ``who``: "War" or "Ranked", for the log."""
    deadline = game.clock() + timeout
    while game.clock() < deadline:
        game.wait(1.0, exact=True)
        game.refresh()
        if game.find(T.WAR_CONFIRM_TITLE):
            # Only its own green Attack!: a tap beside the question closes it.
            if game.click(T.WAR_CONFIRM_ATTACK, region=game.region(CONFIRM)):
                log.debug("%s: confirmed the attack", who)
            continue
        if game.click(T.WAR_ARMY_ATTACK) or (game.has(T.ARMY_ATTACK) and game.click(T.ARMY_ATTACK)):
            log.debug("%s: the army's Attack!", who)
            continue
        if game.has(T.LAYOUT_OBSTRUCTED) and game.find(T.LAYOUT_OBSTRUCTED):
            from farmbot.tasks.village import attack_despite_obstructed_layout

            attack_despite_obstructed_layout(game)
            continue
        if game.find(T.END_BATTLE):
            return True
    return False


def lure_out(game: Game, cfg: Config, card: Card, at: Point) -> None:
    """One lure troop by the Clan Castle, then a wait for its troops to come out to it."""
    log.info("Lure: one troop at %s to pull out the Clan Castle's troops; about %.0f s for them",
             at, cfg.attack.star_lure_wait)
    deploy_taps(game, card, iter([at]), 1, cfg)
    _wait(game, cfg, cfg.attack.star_lure_wait, SETTLE)



BATTLE_REFOCUS_SETTLE = 3.0  # back in front, the game takes a moment before it answers a pinch


def mend_slid_pinch(game: Game) -> bool:
    """A zoom-out that only dragged the map (the game thinks a finger is
    still down) leaves every tap of the battle unanswered too: before
    anything is deployed, the game is put in the background and brought
    back (it stays on this base, its timer running) and zoomed out again.
    True if the pinch works again; the bot reopens the game after the
    battle when it doesn't."""
    refocus = getattr(game.device, "refocus_app", None)
    if not game.pinch_pans or refocus is None or not game.package:
        return False
    log.info("Zooming out isn't working: putting the game in the background and back before deploying")
    try:
        refocus(game.package)
    except Exception as exc:  # noqa: BLE001 (the battle goes on as it is)
        log.info("Couldn't put the game in the background and back (%s)", exc)
        return False
    game.wait(BATTLE_REFOCUS_SETTLE)
    game.pinch_pans = False
    game.zoom_out(force=True)
    if game.pinch_pans:
        log.warning("Zooming out still moves the view; deploying as it is")
        return False
    log.info("Zooming out works again")
    return True


def attack(game: Game, cfg: Config, target: Loot | None = None, room: dict[str, int] | None = None,
           for_stars: bool = False, war_map: bool = False, village=None) -> float | None:
    """Attack the base on screen; ``target`` is its loot as scouted, and
    ``room`` what the storages still hold (the battle ends once that's taken).
    Returns the share of it taken, when the loot could be read.
    ``for_stars``: a ranked or war battle, fought for stars and percentage:
    it is never ended early over loot (see :func:`finish_battle`).
    ``war_map``: the base is on the clan war map (see :func:`deploy_edges`).
    ``village``: the account's village export already kept, if any (a hero
    walk goes by its Rocket Spear's level).

    The bar goes troops, siege machines, heroes, spells. When it holds more
    than fits on the screen, it's deployed a screenful at a time, in that
    order, scrolling on until its end."""
    league = cfg.attack.league_bonus and not for_stars
    if league:
        cfg = league_settings(cfg)
        log.info("League bonus farming: the whole army down at once, leaving at %d%% damage",
                 cfg.attack.league_bonus_damage * 100)
    acfg = cfg.attack
    game.star_plan = {"times": {"start": game.clock()}} if for_stars else {}
    game.look_around()  # "Look around before deploying": before the view is measured
    # The game centres a battle's view by itself, at the village's zoom: a
    # zoom-out is all it needs.
    game.zoom_out()
    mend_slid_pinch(game)
    game.deploy_origins = {}  # (filled by deploy_edges)
    game.deploy_outline = []
    edges, center = deploy_edges(game, cfg, war_map)
    if center is None and (cfg.redline.enabled or cfg.zone.enabled):
        # Neither found the base: most likely still zoomed in (a pinch the
        # game missed, from a village left zoomed in on an upgrade).
        log.info("Deploy points: the base wasn't found; zooming out again")
        # Pinched for real: the view may only look like the last zoom-out's.
        game.zoom_out(force=True)
        edges, center = deploy_edges(game, cfg, war_map)
        if center is None and getattr(game.device, "refocus_app", None) is not None and game.package:
            # Still not found: the pinches changed nothing, so the game most
            # likely isn't taking them (its taps go unanswered then too).
            game.pinch_pans = True
            if mend_slid_pinch(game):
                edges, center = deploy_edges(game, cfg, war_map)
        if center is None:
            log.warning("Deploy points: the base still wasn't found after zooming out (the view may be zoomed "
                        "in); dropping troops on the fixed lines")
    sides = acfg.sides
    # Fought for stars: the whole army in from one side (or corner), the rest kept as spares,
    # facing what the bot knows of the base (the Clan Castle to lure, an air army's Air Defenses).
    read = read_army_and_base(game, cfg) if for_stars and acfg.star_focus != "spread" else BaseRead()
    focus = plan.focus_sides(edges, game.deploy_origins, acfg.sides, acfg.star_focus, game.rng,
                             read.toward) if for_stars else None
    toward_loot = not for_stars and acfg.placement == "loot"
    troop_edges = edges  # where troops and heroes go: toward the loot, each side's stretch nearest it
    loot_pick = loot_front(game, cfg, edges, target) if toward_loot else None
    if for_stars:
        kept = sorted(BASE_SCREENS.glob("*.png")) if acfg.star_focus != "spread" else []  # (read_army_and_base's)
        game.star_plan.update(push=list(focus or []), kind=read.kind, air_defenses=list(read.air_defenses),
                              castle=read.castle, lure=read.lure is not None, center=center,
                              outline=list(game.deploy_outline or []), origins=dict(game.deploy_origins or {}),
                              base_screen=kept[-1].name if kept else "")
    if focus:
        sides = focus
        log.info("Fought for stars: the army goes in from the %s side%s", " and ".join(sides),
                 "" if len(sides) == 1 else "s")
    elif loot_pick:
        sides = loot_pick.sides
        troop_edges = {**edges, **loot_pick.stretches}
    elif game.uses("few_sides"):
        # (Only sides that have points: one with none would leave nowhere to go.)
        sides = human.fewer_sides(game.rng, [s for s in acfg.sides if edges.get(s)]) or acfg.sides
        if len(sides) < len(acfg.sides):
            log.info("This battle goes in from the %s side%s", " and ".join(sides), "" if len(sides) == 1 else "s")
    quakes_first = quakes_ready(game, cfg)  # Earthquakes open the way, and the troops go in through it
    if quakes_first:
        sides, troop_edges = quake_front(edges, troop_edges, center, sides, focus, game.deploy_origins,
                                         game.rng, game.size[0])
        log.info("Earthquakes first, on the walls in from the %s side; the army goes in behind them",
                 " and ".join(sides))
    from farmbot.tasks import walk as hero_walk

    walking = hero_walk.ready(game, cfg)  # a Queen or Champion walk opens the battle: the army follows her in
    if walking and not quakes_first:
        sides, troop_edges = quake_front(edges, troop_edges, center, sides, focus, game.deploy_origins,
                                         game.rng, game.size[0])
    if walking:
        log.info("%s walk from the %s side; the army follows her in", hero_walk.NAMES[acfg.hero_walk], sides[0])
    ends = (funnel_ends(edges, sides, plan.push_spot(troop_edges, center, sides, 0.0), game.size[0])
            if quakes_first and acfg.funnel != "off" else None)  # (where the funnel opens the way in)
    spare = [s for s in acfg.sides if s not in sides]  # brought in if the ones picked are refused
    edge_points = cycle_points(troop_edges, sides, game.rng, _rhythm(cfg), spare)
    fixed_points = cycle_points(fixed_lines(game.size[0], cfg), sides, game.rng, _rhythm(cfg), spare)
    edge_points.war_map = fixed_points.war_map = war_map
    edge_points.all_round = league
    def aim() -> Point:
        """The spells' one spot: following the push when there is one; a new one each battle."""
        game.spell_spot = spell_aim(edges, center, focus, acfg, game.rng, getattr(game, "spell_spot", None))
        return game.spell_spot

    over_base = spell_points(edges, center, acfg.spell_reach, game.rng)
    spot = aim()
    quake_at = (path_spots(troop_edges, center, sides) if quakes_first  # (Earthquakes: on the way in,
                else quake_spots(spot, edges, center, game.rng))  # or the spells' spot, then others)
    if acfg.spell_placement == "together":
        log.info("Spells go on %s (%s)", spot, "following the push" if focus else acfg.spell_spot)
    spells_held = 0.0  # s the spells waited for the push (once, not again on each screenful)

    rescued = False

    def rescue():
        """A troop's first taps went nowhere: if nothing has landed yet in
        this battle, pinch out for real. A view that moves was zoomed in
        (the points were measured on it, and sat inside the base): the
        deploy points are found again on the zoomed-out one."""
        nonlocal edges, center, edge_points, fixed_points, over_base, spot, rescued, troop_edges, sides, spare, quake_at
        if rescued or edge_points.seen_land or fixed_points.seen_land:
            return None
        rescued = True
        was = game.refresh()
        if not game.zoom_out(force=True) or view_change(was, game.refresh()) < VIEW_STILL:
            return None  # all the way out already: the game refuses those points for another reason
        log.warning("No troop was going out and the view was still zoomed in: zoomed out, finding the deploy "
                    "points again")
        edges, center = deploy_edges(game, cfg, war_map)
        troop_edges = edges
        if toward_loot:
            # (looked for again on the zoomed-out view)
            pick = loot_front(game, cfg, edges, target)
            if pick:
                sides, troop_edges = pick.sides, {**edges, **pick.stretches}
            else:
                sides = list(acfg.sides)
            spare = [s for s in acfg.sides if s not in sides]
        if quakes_first or walking:
            sides, troop_edges = quake_front(edges, troop_edges, center, sides, focus, game.deploy_origins,
                                             game.rng, game.size[0])
            spare = [s for s in acfg.sides if s not in sides]
        edge_points = cycle_points(troop_edges, sides, game.rng, _rhythm(cfg), spare)
        fixed_points = cycle_points(fixed_lines(game.size[0], cfg), sides, game.rng, _rhythm(cfg), spare)
        edge_points.war_map = fixed_points.war_map = war_map
        edge_points.all_round = league
        over_base = spell_points(edges, center, acfg.spell_reach, game.rng)
        spot = aim()
        quake_at = path_spots(troop_edges, center, sides) if quakes_first else quake_spots(spot, edges, center,
                                                                                         game.rng)
        return edge_points, fixed_points

    if focus and read.lure is not None:
        side = plan.nearest_side(edges, [s for s in focus if edges.get(s)], read.castle)
        lure_out(game, cfg, read.lure, plan.nearest_point(edges, side, read.castle))
    # The heroes and siege open the way for a ground army, and behind the Earthquakes they clear the
    # outside of the way in before the troops go down (so the troops go on inside, not round the edge).
    lead = (bool(focus) and read.kind == "ground") or quakes_first
    led_at: float | None = None  # when they went (their abilities' clocks start there)

    scrolls = not bar_fits(game, cfg) and scroll_bar(game, cfg)
    if scrolls:
        log.info("The troop bar scrolls: deploying it a screenful at a time")
        rewind_bar(game, cfg)
    waves = 1 if scrolls else acfg.waves  # waves only when the whole army is on show

    placed: list[Card] = []
    singles_seen = placed_total = 0
    troops_seen = 0
    troops_out = False  # a troop card's units went (some or all)
    for page in range(BAR_PAGES):
        cards = _bar_cards(game, cfg)
        if not cards and page == 0:
            raise RuntimeError("no cards found in the troop bar")
        counts = {card: read_count(game, card, cfg) for card in cards
                  if card.kind not in ("single", "siege", "castle")}
        log.info("Troop bar%s: %s", f" (screen {page + 1})" if scrolls else "", ", ".join(
            f"{c.kind} x{counts[c]}" if counts.get(c) else c.kind for c in cards))
        troops = [c for c in cards if c.kind == "troop"]
        singles = [c for c in cards if c.kind == "castle"
                   or (c.kind in ("single", "siege") and not _out_already(game.screen, c))]
        spells = spells_to_cast(game, [c for c in cards if c.kind == "spell"], cfg)
        quakes = quake_cards(game, spells) if acfg.spells in ("earthquake", "all") else []

        if quakes_first and quakes and page == 0:
            # Before anyone goes down: the walls are open when the troops get there.
            for card in quakes:
                cast_quakes(game, card, quake_at, counts.get(card), cfg)
            spells = [c for c in spells if c not in quakes]
            _beat(game, cfg, SPELL_BEAT)

        walked: list[Card] = []  # the walker: out already, her ability still to fire (if it didn't on the walk)
        if walking and page == 0:
            went = hero_walk.walk(game, cfg, abilities_to_fire(singles), troops,
                                  [c for c in cards if c.kind == "spell"], counts,
                                  troop_edges, sides, fixed_points, spear=hero_walk.spear_level(village))
            if went is not None:
                singles = [c for c in singles if c is not went.hero]
                troops = [c for c in troops if c not in went.healers]
                spells = [c for c in spells if c not in went.invisibility]
                walked = [] if went.fired else [went.hero]
                singles_seen += 1
                placed_total += went.fired  # (one still to fire is counted with the others, in ``here``)

        here: list[Card] = []
        if ends and page == 0:
            here = open_funnel(game, cfg, ends, troops, singles, counts, edge_points)
            if here:
                led_at = game.clock()
        if lead and singles and not here:  # (heroes that opened a funnel led already: the Warden stays with the army)
            log.info("%s: the heroes and siege go first, to open the way",
                     "Behind the Earthquakes" if quakes_first else "Ground army")
            here = place_singles(game, singles, edge_points, cfg, fixed_points)
            if here:
                led_at = game.clock() if led_at is None else led_at
                _wait(game, cfg, acfg.star_ground_lead, SETTLE)

        def cast_spells() -> None:
            nonlocal spells
            if spells and not league:  # (league: straight after the squad, no pause)
                _beat(game, cfg, SPELL_BEAT)
            if spells and league:
                log.info("Spells: %d card(s) round the base's outer part, straight after the squad", len(spells))
            for card in spells:
                if card in quakes:
                    cast_quakes(game, card, quake_at, counts.get(card), cfg)
                elif acfg.spell_placement == "together":
                    cast_together(game, card, spot, counts.get(card), cfg)
                elif league:
                    cast_round_rim(game, card, edges, center, counts.get(card), cfg)
                else:
                    deploy_card(game, card, over_base, cfg)
            spells = []

        if league and troops:
            # The biggest card (the squad) first, then the Totems that tank for it, then the rest.
            troops = sorted(troops, key=lambda c: -(counts.get(c) or 0))
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
                    went = deploy_card(game, card, edge_points, cfg, fixed_points, remaining.get(card),
                                       rescue=rescue)
                    troops_out = troops_out or went is not None
                    if went is False:
                        unfinished.append(card)
                    if league and card is troops[0]:
                        cast_spells()
                    continue
                left = remaining.get(card)
                share = (human.share(game.rng, left, waves - wave, _rhythm(cfg)) if left else
                         human.batch(game.rng, acfg.taps_per_batch, _rhythm(cfg)))
                deploy_taps(game, card, edge_points, share, cfg)
                if left:
                    remaining[card] = left - share
            if not last:
                _wait(game, cfg, acfg.wave_delay)

        if league:
            # The squad, its Totems, the other troops, then the heroes (4x once all is down).
            cast_spells()
            spells = []
        rest = [c for c in singles if c not in here]
        own = warden_points(game, rest, edge_points)
        here += place_singles(game, rest, edge_points, cfg, fixed_points, own, quick=league)
        for card in unfinished:
            # Troops a card still holds: another go, now the others are down.
            if not armybar.is_spent(game.refresh(), card, cfg.bar):
                log.info("Card at x=%d still has units; deploying the rest", card.left)
                deploy_card(game, card, edge_points, cfg, fixed_points)
        if spells and focus and acfg.star_spell_delay > 0 and not spells_held:
            # The troops are walking in: the spells go once they're at it, not ahead of them.
            log.info("Spells: about %.0f s for the push to get going", acfg.star_spell_delay)
            began = game.clock()
            _wait(game, cfg, acfg.star_spell_delay, SETTLE)
            spells_held = game.clock() - began
        cast_spells()
        # One more go for any hero that was refused, now on other points.
        here += place_singles(game, [c for c in singles if c not in here], edge_points, cfg, fixed_points, own)
        here += walked
        singles_seen += len(singles)
        troops_seen += len(troops)
        placed_total += len(here)
        # Abilities are fired from the cards on show at the end: earlier
        # screens' cards have scrolled away.
        placed = here if scrolls else placed + here
        if not scrolls or not scroll_bar(game, cfg):
            break
    if singles_seen:
        log.info("Placed %d of %d hero/siege/Clan Castle card(s)", placed_total, singles_seen)
    landed = troops_out or placed_total or edge_points.seen_land or fixed_points.seen_land
    if (troops_seen or singles_seen) and not landed:
        # Every card was given up on: plain taps, the fixed lines, a zoom-out, 4 points per hero.
        # (Only ever set here: the bot reads and clears it after the round, so a later battle in the
        # same round, ranked or war, doesn't wipe it.)
        game.deploy_failed = (f"No troop or hero went down: {troops_seen} troop card(s) and {singles_seen} "
                              "hero/siege/Clan Castle card(s) were refused after every retry")
        log.warning("%s", game.deploy_failed)
    if scrolls and len(placed) < placed_total:
        # Abilities need the cards on show: the bar's end may hold only some
        # of them (or none), so look one screenful back too.
        placed = _placed_on_show(game, cfg)
        if len(placed) < placed_total and scroll_bar(game, cfg, forward=False):
            back = _placed_on_show(game, cfg)
            if len(back) > len(placed):
                placed = back

    placed = abilities_to_fire(placed)
    if placed and acfg.hero_abilities:
        # Heroes: a second tap fires the ability.
        # (Their clocks leave out the spells' wait for the push: the heroes were out meanwhile.)
        since = game.clock() - spells_held if led_at is None else led_at  # (heroes that led: from then)
        if acfg.ability_health > 0:
            fire_abilities_when_hurt(game, placed, cfg, since)
        else:
            fire_abilities_on_time(game, placed, cfg, since)
        log.info("Activated %d hero ability(s)", len(placed))
    game.refresh()
    speed_up(game, cfg)  # everything is down: troops, spells, heroes and siege (and their abilities)
    return finish_battle(game, cfg, target, room, for_stars, league)


ABILITY_CHECK = 0.5  # seconds between looks at the heroes' health
SPELL_BEAT = 0.8  # human-like: about this long between the heroes and the spells


def abilities_to_fire(placed: list[Card]) -> list[Card]:
    """The placed cards tapped again for their ability: the heroes. Not the
    siege machines: a second tap releases the troops inside and ends the
    machine's own damage (they come out anyway once it falls). Standalone
    Clan Castle troops have no hero ability either."""
    return [c for c in placed if c.kind == "single"]


def fire_abilities_on_time(game: Game, cards: list[Card], cfg: Config, since: float | None = None) -> None:
    """Fire each ability ``hero_ability_delay`` after the heroes landed:
    all together, or, human-like, each at its own time around it.
    ``since``: when the clock started (now, if not given)."""
    acfg = cfg.attack
    times = human.ability_times(game.rng, len(cards), acfg.hero_ability_delay, _rhythm(cfg))
    start = game.clock() if since is None else since
    for card, at in zip(human.order(game.rng, cards, _rhythm(cfg)), times):
        wait = at - (game.clock() - start)
        if wait > 0:
            game.wait(wait, exact=True)  # (to the moment: the battle goes on meanwhile)
        game.tap(*card.center)
        game.wait(0.2)


def fire_abilities_when_hurt(game: Game, cards: list[Card], cfg: Config, since: float | None = None) -> None:
    """Watch each placed hero's health bar and fire its ability once it drops
    to ``ability_health``, or when its bar disappears (about to fall), or at
    ``ability_max_wait`` at the latest (counted from ``since``, or now).

    A bar that has never shown (just landed and not drawn yet, or a card
    with no bar the bot can read) is no sign of a fall: that card fires
    on the timer instead, ``hero_ability_delay`` from ``since``. Firing it
    at once wasted the ability (15 Nov trials: 68 of 125 went off so)."""
    acfg = cfg.attack
    on = _rhythm(cfg)
    waiting = list(cards)
    seen: set[int] = set()  # cards whose bar has shown (by id: a Card may not hash)
    # Human-like: each hero's own threshold, and its own last moment.
    limit = {card: human.threshold(game.rng, acfg.ability_health, on) for card in cards}
    start = game.clock() if since is None else since
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
            if health is not None:
                seen.add(id(card))
            gone = health is None and id(card) in seen
            unread = health is None and id(card) not in seen and now - start >= acfg.hero_ability_delay
            if late or gone or unread or (health is not None and health <= limit[card]):
                if on and not late and health is not None:
                    game.wait(human.reaction(game.rng), exact=True)  # seen, then tapped
                game.tap(*card.center)
                game.wait(0.15)
                waiting.remove(card)
                log.info("Ability at x=%d: %s", card.left, "time's up" if late else
                         "health gone" if gone else "no bar seen, on the timer" if unread
                         else f"health {health:.0%}")
        if waiting:
            game.wait(ABILITY_CHECK)
