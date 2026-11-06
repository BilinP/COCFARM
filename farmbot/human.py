"""A player's hand rather than a metronome: the small irregularities of a
person playing a battle, kept inside what still works.

Nothing here makes a tap less accurate than the game needs: offsets stay
within the jitter each caller already allows, and the deploy points come
from the same detected line. What changes is the rhythm: pauses vary
around their usual length (a person is a little quicker or slower each
time, and now and then hesitates), batches of taps aren't all the same
size, each battle starts its sides and points somewhere new, and heroes
and their abilities each get their own moment.

Every function takes the game's ``random.Random``, so tests can seed it.
With ``on`` False (that habit switched off on the "Like a person" page)
each one gives the plain value back. Each habit that takes time also says
how much it adds on average (the ``*_extra`` functions), so the page can
tell what switching it on would cost, even while it's off.
"""

from __future__ import annotations

import functools
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Habit:
    key: str  # its switch: human.<key>
    title: str
    text: str
    group: str  # "touch", "village" or "battle"


HABITS = (
    Habit("tap_spread", "Taps land around each button",
          "A finger never hits the same pixel twice: taps land near the middle of what they aim at, now and then "
          "further out, never off it.", "touch"),
    Habit("tap_hold", "Each tap held a moment",
          "Each tap is held down 40-140 ms (about 75), not let go at once.", "touch"),
    Habit("waits", "Waits run a touch longer",
          "After a tap, the wait for the game runs a little longer, never shorter, and now and then a moment "
          "longer still. The rest between rounds varies too.", "touch"),
    Habit("swipes", "Swipes vary",
          "Swipes start a few pixels off and are up to a fifth slower, never quicker (lists still scroll the "
          "same distance).", "touch"),
    Habit("curved_swipes", "Swipes curve",
          "Swipes follow a slight curve, quick in the middle and slow at each end, as a finger moves "
          "(written to the touchscreen; a plain swipe where that isn't possible).", "touch"),
    Habit("chore_order", "Village jobs in a new order",
          "Collecting, the loot cart and gravestones (and the clan chat and Clan Capital) go in a different order "
          "each round; on the builder base, the Clock Tower and the elixir cart.", "village"),
    Habit("odd_hours", "Start and stop off the hour",
          "With playing hours set (When to play), each day it starts 5-25 minutes into the first hour and stops "
          "5-25 minutes before the last one ends.", "village"),
    Habit("battle_rhythm", "Battle rhythm varies",
          "Pauses and batch sizes vary, each battle starts somewhere new along a side, and heroes and their "
          "abilities each go at their own moment. Builder base: troop pauses, the hero's ability and the pause "
          "between the two bases.", "battle"),
    Habit("look_at_bases", "Look at each base",
          "A look at each base before Next: quick at a poor base, longer at one close to your minimums, and a "
          "moment before attacking the one picked.", "battle"),
    Habit("result_pause", "Look at the result",
          "A look at the stars and loot (about 2 s) before Return Home, in both villages.", "battle"),
    Habit("end_spread", "End battles at different moments",
          "The wait with no loot coming in before a battle is ended varies (0.9-1.4 times the time set), and so "
          "does the share of loot that ends it.", "battle"),
    Habit("scout_look", "Look around before deploying",
          "Drags the view a little, looks, and drags it back before the first troop goes down, in both villages "
          "(checked, and put right if it didn't come back).", "battle"),
)
HABIT_KEYS = tuple(h.key for h in HABITS)

SPREAD = 0.25  # pauses vary by about this share either way
HESITATE = 0.06  # chance a pause runs long (a look at the screen)
HESITATE_MORE = (1.5, 2.5)  # ...by this many times
SHORTEST = 0.6  # a pause never drops below this share of its usual length
LONGEST = 3.0  # ...nor runs past this many times it


def pause(rng: random.Random, seconds: float, on: bool = True, spread: float = SPREAD,
          shortest: float = SHORTEST) -> float:
    """``seconds``, give or take: log-normal around it (a person is more
    often a bit slow than a bit quick), with the odd longer hesitation.
    ``shortest``: never under this share of ``seconds`` (1: a wait for the
    game to show something may run long, never short)."""
    if not on or seconds <= 0:
        return seconds
    value = seconds * math.exp(rng.gauss(0.0, spread))
    if rng.random() < HESITATE:
        value *= rng.uniform(*HESITATE_MORE)
    return min(max(value, seconds * shortest), seconds * LONGEST)


def pauses(rng: random.Random, count: int, seconds: float, on: bool = True) -> list[float] | float:
    """The pauses between ``count`` taps of a burst: each its own, around
    ``seconds`` (a plain float when off, or when the burst is meant to be
    as quick as the device goes)."""
    if not on or seconds <= 0 or count <= 1:
        return seconds
    return [pause(rng, seconds, spread=0.3) for _ in range(count)]


def offset(rng: random.Random, jitter: int) -> tuple[int, int]:
    """Where a tap lands around its point: mostly near the middle, now
    and then out towards ``jitter`` (a finger, not a uniform square), never
    past it."""
    if jitter <= 0:
        return 0, 0

    def one() -> int:
        return max(-jitter, min(jitter, round(rng.gauss(0.0, jitter / 2))))

    return one(), one()


def batch(rng: random.Random, taps: int, on: bool = True) -> int:
    """A batch of about ``taps`` deploy taps: a couple more or fewer each
    time, never under 2 (or ``taps`` when it's smaller)."""
    if not on or taps <= 2:
        return taps
    spread = max(1, taps // 3)
    return max(2, taps + rng.randint(-spread, spread))


def share(rng: random.Random, left: int, waves_left: int, on: bool = True) -> int:
    """How many of a card's ``left`` units go in this wave, with
    ``waves_left`` waves to go (this one included): an even split, give or
    take a fifth. At least one; the last wave takes the rest."""
    even = math.ceil(left / waves_left)
    if not on or waves_left <= 1:
        return even
    return max(1, min(left, round(even * rng.uniform(0.8, 1.2))))


def walk(rng: random.Random, points: list, on: bool = True) -> list:
    """A side's points in the order they're used: from a random point, in a
    random direction along the side (so each battle doesn't start at the
    same corner and walk it the same way)."""
    if not on or len(points) < 2:
        return list(points)
    start = rng.randrange(len(points))
    ordered = points[start:] + points[:start]
    return ordered[::-1] if rng.random() < 0.5 else ordered


def order(rng: random.Random, items: list, on: bool = True) -> list:
    """``items`` in the order a person might take them this time."""
    items = list(items)
    if on:
        rng.shuffle(items)
    return items


def ability_times(rng: random.Random, count: int, delay: float, on: bool = True) -> list[float]:
    """When each of ``count`` heroes fires its ability, in seconds from now:
    all at ``delay`` when off; otherwise each its own time around it (a
    person fires them one by one as they look over the battle), earliest
    first."""
    if not on:
        return [delay] * count
    times = [max(0.5, delay * rng.uniform(0.7, 1.35)) for _ in range(count)]
    return sorted(times)


def threshold(rng: random.Random, health: float, on: bool = True) -> float:
    """The health at which one hero's ability goes: about ``health``, a
    little more or less for each hero."""
    if not on or health <= 0:
        return health
    return min(0.95, max(0.05, health + rng.uniform(-0.08, 0.08)))


LINGER_SPREAD = 0.15  # a wait outside battle runs about this share longer, give or take
LINGER_UPTO = 2.0  # ...of at most this many seconds of it (a minute's wait runs no longer than a 2 s one)
LINGER_LOOK = 0.03  # chance of a longer look at the screen first
LINGER_LOOK_SECONDS = (0.3, 1.0)  # ...this much longer


def linger(rng: random.Random, seconds: float, on: bool = True) -> float:
    """A wait between taps on menus and buttons: never shorter than
    ``seconds`` (the game may need all of it), usually a touch longer, now
    and then a moment longer still. The extra is a fraction of a second,
    whatever the wait, so a long wait for the game to load stays about as
    long."""
    if not on or seconds <= 0:
        return seconds
    extra = min(seconds, LINGER_UPTO) * min(abs(rng.gauss(0.0, LINGER_SPREAD)), 3 * LINGER_SPREAD)
    if rng.random() < LINGER_LOOK:
        extra += rng.uniform(*LINGER_LOOK_SECONDS)
    return seconds + extra


SWIPE_SHIFT = 4  # a swipe starts up to this many pixels from where it aims (both ends move together)
SWIPE_SLOWER = (1.0, 1.2)  # ...and takes this much longer, never less (a quicker one would fling)


def swipe(rng: random.Random, x1: int, y1: int, x2: int, y2: int, duration_ms: int,
          on: bool = True) -> tuple[int, int, int, int, int]:
    """A swipe as a finger makes it: the same distance and direction (so a
    list scrolls the same amount), from a little off where it aims, a
    little slower some times than others."""
    if not on:
        return x1, y1, x2, y2, duration_ms
    dx, dy = offset(rng, SWIPE_SHIFT)
    return x1 + dx, y1 + dy, x2 + dx, y2 + dy, round(duration_ms * rng.uniform(*SWIPE_SLOWER))


TAP_JITTER = 2  # a tap aimed at a point (no button match to size it) lands up to this far off


def reaction(rng: random.Random, on: bool = True) -> float:
    """Seconds between seeing a hero hurt and tapping its ability."""
    return pause(rng, 0.45, on=on, spread=0.35) if on else 0.0


# -- what each habit adds, on average ------------------------------------
@functools.lru_cache(maxsize=None)
def _factor(spread: float, shortest: float) -> float:
    """How much longer than ``seconds`` a :func:`pause` is, on average."""
    r = random.Random(20261105)
    n = 6000
    return sum(pause(r, 1.0, True, spread, shortest) for _ in range(n)) / n


def pause_extra(seconds: float, spread: float = SPREAD, shortest: float = SHORTEST) -> float:
    """What a :func:`pause` of ``seconds`` adds to it, on average."""
    return seconds * (_factor(spread, shortest) - 1.0) if seconds > 0 else 0.0


def pauses_extra(count: int, seconds: float) -> float:
    """What :func:`pauses` adds to a burst of ``count`` taps, on average."""
    return (count - 1) * pause_extra(seconds, 0.3) if count > 1 and seconds > 0 else 0.0


def linger_extra(seconds: float) -> float:
    """What :func:`linger` adds to a wait of ``seconds``, on average."""
    if seconds <= 0:
        return 0.0
    half_normal = LINGER_SPREAD * math.sqrt(2 / math.pi)
    return min(seconds, LINGER_UPTO) * half_normal + LINGER_LOOK * sum(LINGER_LOOK_SECONDS) / 2


HOLD_MS = 75  # a tap's hold, about
HOLD_RANGE = (40, 140)


def hold_ms(rng: random.Random) -> int:
    """How long one tap is held down, in ms."""
    return round(min(max(HOLD_MS * math.exp(rng.gauss(0.0, 0.3)), HOLD_RANGE[0]), HOLD_RANGE[1]))


@functools.lru_cache(maxsize=None)
def hold_extra() -> float:
    """Seconds a held tap adds, on average."""
    r = random.Random(7)
    return sum(hold_ms(r) for _ in range(4000)) / 4000 / 1000


def swipe_extra(duration_ms: int) -> float:
    """Seconds :func:`swipe` adds to a swipe of ``duration_ms``, on average."""
    return duration_ms * (sum(SWIPE_SLOWER) / 2 - 1.0) / 1000


CURVE_BEND = (0.04, 0.10)  # a curved swipe bows out this share of its length, to one side


def curve(rng: random.Random, x1: int, y1: int, x2: int, y2: int, steps: int) -> list[tuple[int, int]]:
    """``steps`` + 1 points from (x1, y1) to (x2, y2) along a slight bow,
    bunched at both ends (a finger speeds up, then slows to a stop: at the
    end, slow enough that a list doesn't fling on)."""
    length = math.hypot(x2 - x1, y2 - y1)
    bend = length * rng.uniform(*CURVE_BEND) * rng.choice((-1, 1))
    nx, ny = ((y1 - y2) / length, (x2 - x1) / length) if length else (0.0, 0.0)
    # A quadratic curve's control point twice as far out as the bow's peak.
    cx, cy = (x1 + x2) / 2 + nx * bend * 2, (y1 + y2) / 2 + ny * bend * 2
    points = []
    for i in range(steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep: slow, quick, slow
        x = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cx + t * t * x2
        y = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cy + t * t * y2
        points.append((round(x), round(y)))
    points[0], points[-1] = (x1, y1), (x2, y2)  # exactly where it aims to start and end
    return points


# A look at a base, in seconds, by how close its loot is to the minimums.
LOOK_POOR, LOOK_CLOSE, LOOK_NEAR, LOOK_PICKED = 0.25, 0.7, 1.3, 1.5
LOOK_SPREAD, LOOK_SHORTEST = 0.4, 0.4


def look_centre(closeness: float, picked: bool) -> float:
    """How long a person looks at a base, about: ``closeness`` is its loot
    over the minimums (1: meets them)."""
    if picked:
        return LOOK_PICKED
    return LOOK_POOR if closeness < 0.5 else LOOK_CLOSE if closeness < 0.8 else LOOK_NEAR


def look(rng: random.Random, centre: float) -> float:
    return pause(rng, centre, spread=LOOK_SPREAD, shortest=LOOK_SHORTEST)


def look_extra(centre: float) -> float:
    return centre * _factor(LOOK_SPREAD, LOOK_SHORTEST)


RESULT_LOOK = 2.0  # seconds on the result screen, about
RESULT_SPREAD, RESULT_SHORTEST = 0.35, 0.5


def result_look(rng: random.Random) -> float:
    return pause(rng, RESULT_LOOK, spread=RESULT_SPREAD, shortest=RESULT_SHORTEST)


def result_extra() -> float:
    return RESULT_LOOK * _factor(RESULT_SPREAD, RESULT_SHORTEST)


END_WAIT = (0.9, 1.4)  # the no-loot wait, times the one set
END_SHARE = 0.04  # the loot share that ends a battle, give or take


def end_wait(rng: random.Random, seconds: float, on: bool = True) -> float:
    return seconds * rng.uniform(*END_WAIT) if on else seconds


def end_wait_extra(seconds: float) -> float:
    return seconds * (sum(END_WAIT) / 2 - 1.0)


def end_share(rng: random.Random, share: float, on: bool = True) -> float:
    if not on or share <= 0:
        return share
    return min(1.0, max(0.05, share + rng.uniform(-END_SHARE, END_SHARE)))


EDGE_MINUTES = (5, 25)  # playing hours start this many minutes late, and stop this many early


def edge_minutes(rng: random.Random) -> tuple[int, int]:
    """Today's (start late, stop early) minutes."""
    return rng.randint(*EDGE_MINUTES), rng.randint(*EDGE_MINUTES)


SCOUT_DRAG = (90, 200)  # px the view is dragged to look around
SCOUT_LOOK = 0.9  # seconds looking, about, before dragging it back
SCOUT_DRAG_MS = 450


def scout_drag(rng: random.Random) -> tuple[int, int]:
    """Which way, and how far, to drag the view for a look around."""
    angle = rng.uniform(0, 2 * math.pi)
    d = rng.uniform(*SCOUT_DRAG)
    return round(d * math.cos(angle)), round(d * math.sin(angle) * 0.6)  # the map is wider than it's tall


def scout_extra() -> float:
    """Seconds a look around adds: two drags, the look, and the view settling."""
    return 2 * SCOUT_DRAG_MS / 1000 + SCOUT_LOOK * _factor(0.3, 0.5) + 2 * 0.4
