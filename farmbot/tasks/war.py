"""Clan war attacks, fought for stars.

From the home village the war button (crossed swords, on the left) opens the
war, if its red badge says there are attacks to make. Only on battle day does
anything happen: the enemy map is shown, a base's panel is opened (the one the game marks "Recommended
target") and the panel's arrows step through every base, one at a time, so
each base's place on the map and the stars taken from it so far are known
without reading any text. The base to attack is then picked
(``pick_target``), its Attack! tapped, the war army's own Attack! and the
"Confirm Attack" question answered, and the battle fought to its end
(``attack.attack(for_stars=True)``): a war battle counts by its stars and
percentage, not its loot. Its result leads back to the war map ("War Map",
one of the Return Home pictures).

The game doesn't show which enemy base is the account's own number, so
"mirror" starts from the game's recommended target (a base of about the
account's strength) and takes the nearest base to it that qualifies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from farmbot import screens
from farmbot.config import Config
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.tasks import attack

log = logging.getLogger(__name__)

BUTTON_AREA = Rect(0, 440, 220, 260)  # the left edge of the home village, where the war button sits
BADGE = (-57, -18, -22, 8)  # the red badge (attacks left), from the middle of the war button's swords
BADGE_RED = 0.25  # of it red at least (0.6 with the badge, next to none without)
BUTTON_LOOKS = 8  # looks for the war button before it's taken as not there
OPEN_WAIT = 12  # s for the war to open
BATTLE_WAIT = 45  # s from Attack! to the base on screen
CONFIRM = Rect(280, 250, 620, 410)  # the "Confirm Attack" question
STEP_WAIT = 1.0  # s for the panel to show the next base
MOST_BASES = 50  # a war has at most 50 a side
MOST_ATTACKS = 2

# A base's panel (opened by a tap on the base), at the bottom of the war map.
PANEL = Rect(315, 600, 560, 310)
TITLE = Rect(330, 640, 520, 34)  # "18. name"
PREVIOUS, NEXT = (265, 770), (910, 770)  # its arrows: one base up, one base down
STARS = ((360, 792), (408, 792), (456, 792))  # "Clan best attack": the three stars' middles
STAR_HALF = 7
EARNED, UNEARNED = 150, 50  # a star's brightness: earned above, not earned below; between: no stars shown
ORANGE = (160, 60)  # Attack! is orange (its white letters included): red at least, and red over blue by at least
LABEL_TO_BASE = 70  # px from "Recommended target" up to the base it's under
# Tried for any base when nothing is recommended: the map above the panel, the middle first
# (a tap on the bare map does nothing).
BASE_TAPS = tuple(sorted(((x, y) for y in range(270, 600, 75) for x in range(300, 920, 85)),
                         key=lambda p: abs(p[0] - 590) + abs(p[1] - 420)))


@dataclass(frozen=True)
class Base:
    """An enemy base on the war map."""

    number: int  # its place on the map: 1 at the top
    stars: int  # the most stars any attack has taken from it so far (0-3)


def pick_target(bases: list[Base], mine: int, target: str = "mirror", max_stars: int = 0,
                attacked: frozenset[int] | set[int] = frozenset()) -> Base | None:
    """The base to attack next, or None if none qualifies.

    Only bases with at most ``max_stars`` stars so far qualify (0: ones not
    starred yet), and never one in ``attacked`` (those this account already
    attacked in this war: the game allows each only once). ``target``:
    "mirror" takes the base numbered ``mine``, then the nearest to it, the
    one below before the one above (a little easier); "bottom" takes the
    lowest on the map; "gain" takes the base with the most stars likely
    still to win (``star_gain``; ``max_stars`` doesn't apply), the nearest
    to ``mine`` among equals."""
    if target == "gain":
        open_ = [b for b in bases if b.number not in attacked and star_gain(b, mine) > 0]
        if not open_:
            return None
        return min(open_, key=lambda b: (-star_gain(b, mine), abs(b.number - mine), -b.number))
    open_ = [b for b in bases if b.stars <= max_stars and b.number not in attacked]
    if not open_:
        return None
    if target == "bottom":
        return max(open_, key=lambda b: b.number)
    return min(open_, key=lambda b: (abs(b.number - mine), -b.number))


GAIN_REACH = 3  # bases from the recommended target before one counts as well above or below it


def likely_stars(number: int, mine: int) -> int:
    """The stars an attack is likely to take from base ``number``, judged by
    its place on the map against ``mine`` (the recommended target, about the
    account's strength): 1 well above it, 2 near it, 3 well below it."""
    if number <= mine - GAIN_REACH:
        return 1
    return 3 if number >= mine + GAIN_REACH else 2


def star_gain(base: Base, mine: int) -> int:
    """The new stars an attack on ``base`` is likely to add to the war."""
    return likely_stars(base.number, mine) - base.stars


class WarError(RuntimeError):
    pass


# -- the war screen ----------------------------------------------------------
def on_war_screen(game: Game) -> bool:
    """The war map: its Return Home (the Clan Capital has the same one) and
    the button that switches between the two sides' maps."""
    if not (game.has(T.WAR_RETURN_HOME) and game.find(T.WAR_RETURN_HOME)):
        return False
    return game.find(T.WAR_TO_ENEMY) is not None or game.find(T.WAR_TO_HOME) is not None


def war_button(game: Game):
    """The war button on the home village (on screen), or None: no clan,
    or no war."""
    # The swords clash now and then while attacks are left: a look can fall on that.
    for look in range(BUTTON_LOOKS):
        match = game.find(T.WAR_BUTTON, region=game.region(BUTTON_AREA))
        if match is not None:
            return match
        if look < BUTTON_LOOKS - 1:
            game.wait(0.4, exact=True)
            game.refresh()
    return None


def attacks_waiting(game: Game, button) -> bool:
    """The red badge at the war button's corner: attacks this account can
    still make (it shows only on battle day, and goes with the last one)."""
    x0, y0, x1, y1 = (button.x + BADGE[0], button.y + BADGE[1], button.x + BADGE[2], button.y + BADGE[3])
    patch = game.screen[max(y0, 0):y1, max(x0, 0):x1].astype(int)
    if not patch.size:
        return False
    red = (patch[..., 2] > 170) & (patch[..., 1] < 110) & (patch[..., 0] < 130)
    return float(red.mean()) >= BADGE_RED


def open_war(game: Game) -> bool | None:
    """The war, from the home village. False if there's no war button (no
    clan, or no war) or it didn't open; None if the button shows no attacks
    to make (the war isn't opened)."""
    game.refresh()
    if on_war_screen(game):
        return True
    button = war_button(game)
    if button is None:
        return False
    if not attacks_waiting(game, button):
        return None
    game.tap_match(button)
    game.screen = None
    deadline = game.clock() + OPEN_WAIT
    while game.clock() < deadline:
        game.wait(1.0, exact=True)
        game.refresh()
        if on_war_screen(game):
            game.wait(1.0, exact=True)  # (the map scrolls to its place)
            game.refresh()
            return True
    return False


def leave(game: Game) -> None:
    """Back to the home village from the war."""
    game.refresh()
    if on_war_screen(game):
        game.click(T.WAR_RETURN_HOME)
        game.wait(3)
    screens.settle(game)


def battle_day(game: Game) -> bool:
    return game.find(T.WAR_BATTLE_DAY) is not None


def show_enemy_map(game: Game) -> None:
    """The enemy's side of the map. The side button names the side it
    leads to: "Enemy" on the home map, "Home" on the enemy's (told apart by
    which of the two it looks more like)."""
    if game.best_score(T.WAR_TO_ENEMY) > game.best_score(T.WAR_TO_HOME) and game.click(T.WAR_TO_ENEMY):
        game.wait(2.0, exact=True)
        game.refresh()


# -- a base's panel ----------------------------------------------------------
def panel_open(game: Game) -> bool:
    return game.find(T.WAR_SCOUT, region=game.region(PANEL)) is not None


def open_panel(game: Game) -> str:
    """An enemy base's panel: "recommended" if it's the base the game marks
    "Recommended target", "any" if it's whichever base a tap on the map
    landed on (no base is marked on the part of the map showing), "" if
    none opened."""
    if panel_open(game):
        return "any"
    label = game.find(T.WAR_RECOMMENDED)
    spots = ([(label.x, label.y - LABEL_TO_BASE)] if label else []) + list(BASE_TAPS)
    for i, (x, y) in enumerate(spots):
        game.tap(x, y)
        game.wait(1.2, exact=True)
        game.refresh()
        if panel_open(game):
            return "recommended" if label and i == 0 else "any"
        if not on_war_screen(game):
            raise WarError("a tap on the war map led somewhere else")
    return ""


def _title(game: Game) -> np.ndarray:
    r = TITLE
    return game.refresh()[r.y:r.y + r.h, r.x:r.x + r.w].astype(np.int16)


def step(game: Game, to: tuple[int, int]) -> bool:
    """One base up or down with a panel arrow. False at the end of the map
    (the panel still shows the same base)."""
    before = _title(game)
    game.tap(*to)
    game.wait(STEP_WAIT, exact=True)
    after = _title(game)
    if not panel_open(game):
        raise WarError("the base's panel closed while stepping through the bases")
    change = np.abs(after - before)
    return float(change.mean()) > 4.0 or int((change.max(axis=2) > 80).sum()) >= 12  # (or just its number)


def stars(screen: np.ndarray) -> int:
    """The stars taken from the base whose panel shows (its "Clan best
    attack"), 0 if nobody attacked it yet."""
    got = 0
    for x, y in STARS:
        patch = screen[y - STAR_HALF:y + STAR_HALF, x - STAR_HALF:x + STAR_HALF]
        value = float(patch.max(axis=2).mean())
        if UNEARNED <= value <= EARNED:  # neither kind of star: the war win bonus shows there instead
            return 0
        got += value > EARNED
    return got


def survey(game: Game) -> tuple[list[Base], int]:
    """Every enemy base with its stars, and the number of the base whose
    panel was open (the recommended target). Ends with the last base's
    panel open."""
    start = 1
    while step(game, PREVIOUS):
        start += 1
        if start > MOST_BASES:
            raise WarError("the bases never came to an end")
    bases = [Base(1, stars(game.screen))]
    while len(bases) < MOST_BASES and step(game, NEXT):
        bases.append(Base(len(bases) + 1, stars(game.screen)))
    return bases, start


def attack_button(game: Game):
    """The panel's orange Attack!, or None: greyed (no attacks left, or this
    account already attacked the base) or not there."""
    match = game.find(T.WAR_ATTACK, region=game.region(PANEL))
    if match is None:
        return None
    patch = game.screen[match.y - 14:match.y + 14, match.x - 50:match.x + 50]
    blue, red = float(patch[..., 0].mean()), float(patch[..., 2].mean())
    return match if red >= ORANGE[0] and red - blue >= ORANGE[1] else None


def go_to(game: Game, at: int, number: int) -> None:
    """Step from base ``at`` to base ``number``."""
    for _ in range(abs(number - at)):
        if not step(game, NEXT if number > at else PREVIOUS):
            raise WarError(f"couldn't step to base {number}")


# -- the attacks -------------------------------------------------------------
def to_battle(game: Game) -> bool:
    """From the tap on a base's Attack! to the base on screen: the war
    army's window (its own Attack!), then "Confirm Attack" (this is where
    one of the war's attacks is spent). True once the battle's End Battle
    shows."""
    deadline = game.clock() + BATTLE_WAIT
    while game.clock() < deadline:
        game.wait(1.0, exact=True)
        game.refresh()
        if game.find(T.WAR_CONFIRM_TITLE):
            # Only its own green Attack!: a tap beside the question closes it.
            if game.click(T.WAR_CONFIRM_ATTACK, region=game.region(CONFIRM)):
                log.debug("War: confirmed the attack")
            continue
        if game.click(T.WAR_ARMY_ATTACK) or (game.has(T.ARMY_ATTACK) and game.click(T.ARMY_ATTACK)):
            log.debug("War: the war army's Attack!")
            continue
        if game.find(T.END_BATTLE):
            return True
    return False


def attacks(game: Game, cfg: Config, dry_run: bool = False) -> int:
    """Every war attack there is to make, from the home village and back to
    it. Returns how many were made. ``dry_run``: everything but the tap on
    Attack! (to look at what would be attacked)."""
    war = cfg.war
    done, attacked = 0, set()
    first = None  # the recommended target at the start: the game moves it on after an attack
    try:
        while done < MOST_ATTACKS:
            opened = open_war(game)
            if not opened:
                log.info("War: %s", "no more attacks to make" if done else "no attacks to make now (the war "
                         "button shows none)" if opened is None else "no war (no war button on the home village)")
                break
            if not battle_day(game):
                log.info("War: it isn't battle day")
                break
            show_enemy_map(game)
            opened = open_panel(game)
            if not opened:
                raise WarError("no enemy base's panel opened on the war map")
            bases, at = survey(game)
            first = first or (at if opened == "recommended" else None)
            target = war.target if first or war.target == "bottom" else "bottom"
            log.info("War: %d bases, stars so far: %s; %s", len(bases),
                     " ".join(f"{b.number}:{b.stars}" for b in bases),
                     f"the recommended target is {first}" if first else "no recommended target was on screen"
                     + ("" if war.target == "bottom" else ": taking the lowest base that qualifies instead"))
            fought = tried = False
            while not fought:
                pick = pick_target(bases, first or len(bases), target, war.max_stars, attacked)
                if pick is None:
                    if target == "gain":
                        log.info("War: no base left where an attack is likely to add a star")
                    else:
                        log.info("War: no base left with %d star%s or fewer to attack", war.max_stars,
                                 "" if war.max_stars == 1 else "s")
                    return done
                go_to(game, len(bases), pick.number)
                button = attack_button(game)
                if button is None:
                    if not tried:  # the first grey one this time: most likely no attacks are left at all
                        tried = True
                        other = next((b for b in bases if b.number != pick.number and b.stars < 3), None)
                        if other is not None:
                            go_to(game, pick.number, other.number)
                            if attack_button(game) is None:
                                log.info("War: no attacks left in this war")
                                return done
                            go_to(game, other.number, pick.number)
                    log.info("War: base %d can't be attacked (already attacked by this account)", pick.number)
                    attacked.add(pick.number)
                    go_to(game, pick.number, len(bases))
                    continue
                log.info("War attack %d: base %d (%d star%s so far), for stars", done + 1, pick.number,
                         pick.stars, "" if pick.stars == 1 else "s")
                if dry_run:
                    return done
                game.tap_match(button)
                game.screen = None
                attacked.add(pick.number)
                if not to_battle(game):
                    raise WarError(f"Attack! on base {pick.number} led to no battle")
                attack.attack(game, cfg, for_stars=True, war_map=True)
                done += 1
                fought = True
                # Home between attacks: the war button there says whether another is left.
                leave(game)
    finally:
        leave(game)
    return done
