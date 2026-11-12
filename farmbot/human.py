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
from datetime import date


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
    Habit("fatigue", "Slow down over a session",
          "Waits between taps grow a little the longer it has played without a break: up to a quarter longer "
          "after an hour. A break (When to play) makes it fresh again.", "touch"),
    Habit("chore_order", "Village jobs in a new order",
          "Collecting, the loot cart and gravestones (and the clan chat and Clan Capital) go in a different order "
          "each round; on the builder base, the Clock Tower and the elixir cart. The collectors are emptied "
          "every few rounds, give or take two, not on the dot.", "village"),
    Habit("odd_hours", "Start and stop off the hour",
          "With playing hours set (When to play), each day it starts 5-25 minutes into the first hour and stops "
          "5-25 minutes before the last one ends.", "village"),
    Habit("offline_breaks", "Close the game when not playing",
          "On a break, outside your playing hours and once the day's attacks are done, the game is closed (the "
          "account goes offline) and opened again when it plays on. Off: the game stays open through them. "
          "While it's closed your village can be attacked, as when you close the game yourself.",
          "village"),
    Habit("uneven_days", "Uneven days",
          "Some days it plays more, some less: each day's attack limit and how often breaks come are scaled by "
          "that day's own factor (0.6 to 1.2).", "village"),
    Habit("distracted", "Distracted now and then",
          "About one round in 25, the rest before the next round runs 10-60 s longer.", "village"),
    Habit("browse", "Look around the game",
          "Every 8-20 rounds it does something besides farming: opens the clan chat and reads a moment, opens "
          "the Build list and scrolls it, or drags around its own village. It never taps anything in them.",
          "village"),
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
    Habit("near_misses", "Imperfect choices",
          "A base up to 5% under your minimums is attacked one time in three, and one up to 5% over them is "
          "passed over one time in twenty.", "battle"),
    Habit("few_sides", "Fewer sides some battles",
          "About one battle in three goes in from one or two neighbouring sides, not all the sides set. Can "
          "take less loot from a spread-out base.", "battle"),
    Habit("hold_deploy", "Hold to deploy",
          "Troop cards of 8 or more units go down as a person mixes it: mostly a held finger that slides along the "
          "side, dropping them in a line, sometimes one held on a spot, and bursts of taps in between (held on one "
          "spot where the emulator can't slide it). Each card's last few units are always tapped. Needs the "
          "card's count to be readable.", "battle"),
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


# -- days, sessions and slips ---------------------------------------------
DAY_FACTOR = (0.6, 1.2)  # "Uneven days": a day's attack limit and break spacing, times this


def day_factor(seed: str, day: date) -> float:
    """That day's factor for ``seed`` (the account): the same whenever it's
    asked, so stopping and starting doesn't change the day."""
    return random.Random(f"{seed}:{day.isoformat()}:factor").uniform(*DAY_FACTOR)


def day_off(seed: str, day: date, every: int) -> bool:
    """Whether ``day`` is a day off: about one day in ``every`` (0: never),
    the same whenever it's asked."""
    return every > 0 and random.Random(f"{seed}:{day.isoformat()}:off").random() < 1.0 / every


TIRED_AFTER = 60.0  # minutes of play without a break until fully tired
TIRED_SLOWER = 0.25  # ...when waits run this much longer


def tiredness(minutes: float) -> float:
    """What waits are multiplied by after ``minutes`` of play: 1 fresh,
    rising to 1 + TIRED_SLOWER."""
    return 1.0 + TIRED_SLOWER * min(max(minutes, 0.0) / TIRED_AFTER, 1.0)


def tired(seconds: float, tiredness: float) -> float:
    """What being tired adds to a wait of ``seconds`` (as linger: a share of
    at most LINGER_UPTO of it, so a long wait for the game stays about as long)."""
    return min(seconds, LINGER_UPTO) * max(tiredness - 1.0, 0.0) if seconds > 0 else 0.0


def tired_extra(seconds: float) -> float:
    """What "Slow down over a session" adds to a wait, on average (half tired)."""
    return tired(seconds, 1.0 + TIRED_SLOWER / 2)


DISTRACTED = 1 / 25  # chance a round's rest runs long
DISTRACTED_SECONDS = (10.0, 60.0)
DISTRACTED_EXTRA = DISTRACTED * sum(DISTRACTED_SECONDS) / 2  # what it adds to a round, on average


def distraction(rng: random.Random) -> float:
    """Seconds this round's rest runs longer: mostly none."""
    return rng.uniform(*DISTRACTED_SECONDS) if rng.random() < DISTRACTED else 0.0


NEAR = 0.05  # a base this close to the minimums, under or over, is a near miss
TAKE_UNDER = 1 / 3  # chance one just under is attacked anyway
SKIP_OVER = 1 / 20  # chance one just over is passed over

COLLECT_SPREAD = 2  # rounds either way of bot.collect_every


def collect_gap(rng: random.Random, every: int, on: bool = True) -> int:
    """Rounds until the collectors are emptied again: ``every``, give or take."""
    return max(1, every + rng.randint(-COLLECT_SPREAD, COLLECT_SPREAD)) if on else every


BROWSE_EVERY = (8, 20)  # rounds between looks around the game
BROWSE_READ = (3.0, 10.0)  # seconds spent looking at what was opened
BROWSE_EXTRA = 9.0 / (sum(BROWSE_EVERY) / 2)  # what it adds to a round, on average


def browse_gap(rng: random.Random) -> int:
    return rng.randint(*BROWSE_EVERY)


RING = ("nw", "ne", "se", "sw")  # the sides, going round the base
FEW_SIDES = 1 / 3  # chance a battle goes in from fewer sides


def fewer_sides(rng: random.Random, sides: list[str]) -> list[str]:
    """The sides this battle goes in from: mostly all of ``sides``, some
    battles one of them or two neighbours."""
    known = [s for s in RING if s in sides]
    if len(known) < 3 or rng.random() >= FEW_SIDES:
        return list(sides)
    start = rng.randrange(len(known))
    return [known[start]] if rng.random() < 0.4 else [known[start], known[(start + 1) % len(known)]]


HOLD_SHARE = 0.65  # of a big card's goes are a held finger; the rest bursts of taps, as a person mixes them
HOLD_STILL = 0.25  # ...and of those, this share stay on one spot instead of sliding along the side
HOLD_FROM = 8  # units a card needs for a held finger to be worth it
HOLD_KEEP = 4  # ...and its last units are always tapped (a hold a moment too long drops the next card)
HOLD_UNITS = (5, 12)  # units a hold aims to drop
# A held finger drops one unit at once, then, after a moment, a steady stream. Measured on MuMu
# (2026-11-10): 1 unit in 0.4 s, 9 in 1.6 s: the stream starts about 0.4 s in, at 6-7 units a second.
HOLD_START = 0.3  # s before the stream starts, taken a little short (so holds come out short, not long)
HOLD_RATE = 8.0  # units a second once it streams, until measured: taken a little fast, for the same reason
HOLD_LEARN_FROM = 4  # units a hold must drop to say how fast the stream is (fewer: mostly its start)
HOLD_RATE_MOST = 12.0  # units a second a stream is believed to reach at most
HOLD_RATE_LEAST = 4.5  # ...and at least (5.6-6.7 measured on MuMu and LDPlayer): a slide the game cut short
# reads as a slow stream (1.9 seen live), and the next hold, sized by it, ran on and emptied the card
HOLD_SAFETY = 0.8  # a hold lasts this share of what its units would take: short rather than long
HOLD_LIMITS_MS = (250, 2500)


def hold_units(rng: random.Random, left: int) -> int:
    """How many of a card's ``left`` units the next hold aims to drop (0: tap the rest)."""
    spare = left - HOLD_KEEP
    return 0 if spare < 2 else min(spare, rng.randint(*HOLD_UNITS))


def hold_length(units: int, rate: float | None) -> int:
    """Milliseconds to hold for ``units``: the first at once, the rest at
    ``rate`` units a second once the stream starts (None: not measured yet)."""
    rate = rate if rate and rate > 0 else HOLD_RATE
    seconds = (HOLD_START + (units - 1) / rate) * HOLD_SAFETY
    return round(min(max(seconds * 1000, HOLD_LIMITS_MS[0]), HOLD_LIMITS_MS[1]))


SLIDE_PER_UNIT = 28  # px of the side a unit gets as the finger slides along it
SLIDE_JUMP = 130  # px: two of a side's points farther apart than this aren't joined (the side's two ends)
SLIDE_FRAME_MS = 70  # about a point of the finger's path for each of these
# Seen live on MuMu (2026-11-10): a finger resting 0.6-1.0 s and then moving at up to ~175 px a second
# streams its troops out along the way; one moving at ~400 px a second dropped nothing (and the points
# were off after: the game took it for a drag of the view).
SLIDE_REST = 0.6  # s the finger rests before it moves: until the stream has started
SLIDE_SPEED = 150  # px a second it moves at, at most
SLIDE_FRAMES = (4, 18)


def slide_path(points: list[tuple[int, int]], ms: int) -> list[tuple[int, int]]:
    """The finger's path for a slide of ``ms`` through ``points``: still on
    the first for SLIDE_REST, then along them at an even pace, no quicker
    than SLIDE_SPEED (so only as far along them as the time allows: the
    game takes a finger that moves early or fast for a drag of the view)."""
    frames = min(max(round(ms / SLIDE_FRAME_MS), SLIDE_FRAMES[0]), SLIDE_FRAMES[1])
    gap = ms / 1000 / (frames - 1)  # s from one point of the path to the next
    still = min(max(math.ceil(SLIDE_REST / gap) + 1, 2), frames - 2)  # (points on the first: the rest between them)
    moving = frames - still
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
    total = min(sum(lengths), SLIDE_SPEED * gap * moving)
    path = [points[0]] * still
    for i in range(1, moving + 1):
        at, k = total * i / moving, 0
        while k < len(lengths) - 1 and at > lengths[k]:  # (the stretch of the path this point is on)
            at -= lengths[k]
            k += 1
        t = min(at / lengths[k], 1.0) if lengths[k] else 1.0
        (x1, y1), (x2, y2) = points[k], points[k + 1]
        path.append((round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t)))
    return path


def hold_rate(dropped: int, ms: int, known: float | None) -> float | None:
    """The stream's rate after a hold of ``ms`` dropped ``dropped`` units:
    a quicker one than ``known`` is taken at once (the next hold must not
    run long), a slower one only half way. A hold that dropped just the
    first unit says nothing about the stream."""
    if dropped < HOLD_LEARN_FROM:
        return known
    seen = (dropped - 1) / max(ms / 1000 - HOLD_START, 0.1)
    # (Never past HOLD_RATE_MOST: a count misread after a hold, 49 as 40 seen live, looks like a
    # stream of 90 a second, and every hold after it would be cut to nothing.)
    seen = max(min(seen, HOLD_RATE_MOST), HOLD_RATE_LEAST)
    return seen if known is None or seen > known else (known + seen) / 2
