"""Find a base worth attacking."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Callable

from farmbot import human
from farmbot.config import RegionsConfig, SearchConfig
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.text import SNOW, TextStyle

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


class SearchBlocked(SearchError):
    """"Unable to attack Village! Try again later.": a battle left without
    surrendering still runs on the server. Only closing the game for a few
    minutes clears it."""


class NextNotFound(SearchError):
    """No base showed: its Next button never came (or went away)."""


class LootUnreadable(SearchError):
    """The bases showed, but their loot couldn't be read."""


class NextDidNothing(SearchError):
    """Next was tapped (twice) and the same base is still showing."""


@dataclass(frozen=True)
class Loot:
    gold: int
    elixir: int
    dark: int


# Called for every scouted base with its loot and whether it was accepted.
BaseCallback = Callable[[Loot, bool], None]


DARK_WORTH = 100  # one dark elixir counts as this much gold or elixir


def loot_value(loot: Loot, criteria: SearchConfig | None = None) -> int:
    """Gold + elixir + dark elixir, dark counted ``DARK_WORTH`` times."""
    return loot.gold + loot.elixir + loot.dark * DARK_WORTH


def is_worth_attacking(loot: Loot, criteria: SearchConfig) -> bool:
    checks = [
        loot.gold >= criteria.min_gold,
        loot.elixir >= criteria.min_elixir,
        loot.dark >= criteria.min_dark,
    ]
    enough = any(checks) if criteria.match_any else all(checks)
    return enough


def near_miss(rng, loot: Loot, criteria: SearchConfig, accepted: bool) -> bool | None:
    """"Imperfect choices": True to attack a base up to NEAR under the
    minimums after all, False to pass over one up to NEAR over them, None
    to leave the choice as it is (nearly always)."""
    def scaled(by: float) -> SearchConfig:
        return replace(criteria, min_gold=int(criteria.min_gold * by), min_elixir=int(criteria.min_elixir * by),
                       min_dark=int(criteria.min_dark * by))

    if not (criteria.min_gold or criteria.min_elixir or criteria.min_dark):
        return None  # no minimums: every base is taken
    if accepted:
        just = not is_worth_attacking(loot, scaled(1 + human.NEAR))
        return False if just and rng.random() < human.SKIP_OVER else None
    just = is_worth_attacking(loot, scaled(1 - human.NEAR))
    return True if just and rng.random() < human.TAKE_UNDER else None


def closeness(loot: Loot, criteria: SearchConfig) -> float:
    """How near ``loot`` comes to the minimums (1: it meets them): what a
    person weighs when deciding how long to look at a base."""
    shares = [have / least for have, least in ((loot.gold, criteria.min_gold), (loot.elixir, criteria.min_elixir),
                                                (loot.dark, criteria.min_dark)) if least > 0]
    if not shares:
        return 1.0
    return max(shares) if criteria.match_any else min(shares)


def look_at_base(game: Game, loot: Loot, criteria: SearchConfig, picked: bool, on: bool) -> None:
    """"Look at each base": a quick look at a poor one, longer at one close
    to the minimums, a moment at the one picked."""
    centre = human.look_centre(closeness(loot, criteria), picked)
    seconds = human.look(game.rng, centre) if on else 0.0
    game.note("look_at_bases", human.look_extra(centre), seconds)
    if seconds:
        game.wait(seconds, exact=True)


def _loot_number(game: Game, regions: RegionsConfig, name: str) -> int | None:
    region = game.region(regions.rect(name))
    found = game.read_number(region, T.LOOT_FONT)
    # Snow behind the digits passes for their white and joins them: again without its blue.
    return found if found is not None else game.read_number(region, T.LOOT_FONT, SNOW)


def read_loot(game: Game, regions: RegionsConfig) -> Loot | None:
    gold = _loot_number(game, regions, "loot_gold")
    elixir = _loot_number(game, regions, "loot_elixir")
    if gold is None or elixir is None:
        return None
    # Dark elixir is absent on low town halls; treat unreadable as zero.
    dark = _loot_number(game, regions, "loot_dark") or 0
    return Loot(gold, elixir, dark)


SETTLE_READS = 4  # tries at a steady reading of a base's loot


def read_settled_loot(game: Game, regions: RegionsConfig, interval: float = 0.3) -> Loot | None:
    """The base's loot once two readings in a row agree: the counters
    count up when a base loads, and a reading mid-way is wrong. ``None``
    if it never reads the same twice."""
    last = None
    for attempt in range(SETTLE_READS):
        if attempt:
            game.wait(interval)
            game.refresh()
        loot = read_loot(game, regions)
        if loot is not None and loot == last:
            return loot
        last = loot
    return None


def _own(game: Game, regions: RegionsConfig, name: str, shift: int = 0) -> int | None:
    r = game.region(regions.rect(name))
    return game.read_number(Rect(r.x - shift, r.y, r.w, r.h), T.LOOT_FONT)


def own_gold(game: Game, regions: RegionsConfig, shift: int = 0) -> int | None:
    """Your gold, read from the home village's resource bar (``shift``
    pixels further left: a window open on the right pushes the bar over)."""
    return _own(game, regions, "own_gold", shift)


def own_resources(game: Game, regions: RegionsConfig, shift: int = 0) -> Loot | None:
    """Your gold, elixir and dark elixir from the home village's resource bar."""
    gold = own_gold(game, regions, shift)
    elixir = _own(game, regions, "own_elixir", shift)
    if gold is None or elixir is None:
        return None
    dark = (_own(game, regions, "own_dark", shift) or 0) if has_dark(game, regions, shift) else 0
    return Loot(gold, elixir, dark)


# Tapping a resource bar opens a box under it, "Max: 21 000 000" first; tapping
# it again closes it (in the builder base too). The "Max:" label is
# smaller than the number, so pieces shorter than the digits are left out.
MAX_STYLE = TextStyle(min_height_ratio=0.85)
MAX_ROW = (33, 30)  # top and height of the "Max:" row, below the bar's middle
MAX_SPAN = (190, 40)  # the row reaches this far left of the bar's number and right of it
LEAST_STORAGE = 1000  # no storage holds less (a town hall 1's gold storage holds 1 500)
ROUND_SIZE = 500  # what storages hold comes in steps of this at least
OVER_FULL = 3  # a storage holding more than its size holds at most this many times it


def _max_region(game: Game, regions: RegionsConfig, kind: str) -> Rect:
    r = game.region(regions.rect(f"own_{kind}"))
    middle = r.y + r.h // 2
    return Rect(r.x + r.w - MAX_SPAN[0], middle + MAX_ROW[0], MAX_SPAN[0] + MAX_SPAN[1], MAX_ROW[1])


def has_dark(game: Game, regions: RegionsConfig, shift: int = 0) -> bool:
    """Whether the home village's third bar is dark elixir. Below town hall
    7 there's none, and the gems' bar is there (with a gem by its number):
    read as dark elixir it'd be the gems, and a tap on it opens the Shop."""
    if not game.has(T.GEM):
        return True
    r = game.region(regions.rect("own_dark"))
    # The bar's icon, right of its number (a window open on the right pushes it ``shift`` over),
    # and no taller than its bar: the next bar down (the gems', from town hall 7) isn't it.
    x = r.x - shift
    icons = game.find_all(T.GEM, region=Rect(x, max(r.y - r.h, 0), game.size[0] - x, 3 * r.h))
    return not any(r.y - r.h // 4 <= m.y <= r.y + r.h + r.h // 4 for m in icons)  # (its middle in the bar)


def storage_max(game: Game, regions: RegionsConfig, kind: str) -> int | None:
    """How much the ``kind`` storages hold, from the box tapping its bar
    opens (closed again after). ``None`` without the "Max:" picture."""
    if not game.has(T.STORAGE_MAX):
        return None
    r = game.region(regions.rect(f"own_{kind}"))
    bar = (r.x + r.w // 2, r.y + r.h // 2)
    where = _max_region(game, regions, kind)

    def is_open() -> bool:
        game.refresh()
        return game.find(T.STORAGE_MAX, region=where) is not None

    most = None
    for _ in range(2):  # a second tap if the game missed the first
        game.tap(*bar)
        if _soon(game, is_open, MAX_OPENS):
            most = game.vision.read_number(game._current(), where, T.LOOT_FONT, MAX_STYLE)
            break
    for _ in range(2):  # close it
        if not is_open():
            break
        game.tap(*bar)
        if _soon(game, lambda: not is_open(), MAX_CLOSES):
            break
    return most


MAX_OPENS = 1.2  # seconds the storage's box takes to open at most (it's looked for as it does)
MAX_CLOSES = 0.9


def _soon(game: Game, done, timeout: float, first: float = 0.25, interval: float = 0.2) -> bool:
    """``done()`` comes true within ``timeout`` seconds, looked at every
    ``interval`` (a screenshot takes ~0.2 s itself)."""
    deadline = game.clock() + timeout
    game.wait(first)
    while not done():
        if game.clock() >= deadline:
            return False
        game.wait(interval)
    return True


def storage_maxes(game: Game, regions: RegionsConfig, kinds: tuple[str, ...] = ("gold", "elixir", "dark")
                  ) -> dict[str, int]:
    """What each storage holds (no dark elixir on low town halls). The
    builder base has only ``("gold", "elixir")``: its third bar is gems."""
    game.refresh()
    maxes = {}
    if "dark" in kinds and not has_dark(game, regions):
        log.info("No dark elixir in this village yet (town hall 7): not reading its storages")
        kinds = tuple(k for k in kinds if k != "dark")
    for kind in kinds:
        held = _own(game, regions, f"own_{kind}")
        if held is None:
            continue  # no such bar
        most = storage_max(game, regions, kind)
        # A misread size (say, numbers read at the wrong place before the
        # UI's scale is known) would make that storage look full, or keep
        # nothing of it: only a size that can hold what's in it is kept.
        # A storage can hold more than its size, though (rewards go in over
        # it: 7 071 000 gold seen in 6 500 000, 12 724 973 elixir too): a
        # round size, as storages' are, not far under what's in it is then
        # believed all the same.
        over = most and most % ROUND_SIZE == 0 and held <= most * OVER_FULL
        if most and most >= LEAST_STORAGE and (most >= held or over):
            maxes[kind] = most
        elif most:
            log.info("Storage size read as %s for %s holding %s: not believed", f"{most:,}", kind, f"{held:,}")
    return maxes


FULL_AMOUNT = 0.99  # of what a storage holds: a little short of it is full enough


def full_by_amount(loot: Loot, maxes: dict[str, int]) -> set[str]:
    """Which storages are full, from what's in them and what they hold."""
    return {k for k, most in maxes.items() if getattr(loot, k) >= most * FULL_AMOUNT}


def start_search(game: Game, super_troops: bool = False, dark: int | None = None) -> int:
    """Home village → Battle → Find a Match → Attack! (army screen).
    ``super_troops``: on the army screen, boost the super troops showing
    Activate first, with ``dark``, the dark elixir you have. Returns how
    many were boosted."""
    game.refresh()
    # The battle menu may already be open (e.g. after a failed round).
    if not game.find(T.FIND_MATCH) and not game.click(T.BATTLE):
        raise SearchError("battle button not visible; is the home village showing?")
    if not _find_a_match(game, timeout=5):
        # A tap on Battle can go unanswered (the village just back from an
        # attack, say): still on the village, tap it once more.
        if not (game.click(T.BATTLE) and _find_a_match(game, timeout=5)):
            raise SearchError("find-a-match button did not appear")
    # Newer versions confirm the army first; older ones go straight to scouting.
    boosted = 0
    if super_troops and game.has(T.ARMY_ATTACK) and game.wait_for(T.ARMY_ATTACK, timeout=4):
        from farmbot.tasks.super_troops import activate_super_troops

        boosted = activate_super_troops(game, dark)
    if game.has(T.ARMY_ATTACK) and game.click_when_visible(T.ARMY_ATTACK, timeout=4):
        log.debug("Confirmed army")
    if game.has(T.LAYOUT_OBSTRUCTED):
        from farmbot.tasks.village import attack_despite_obstructed_layout

        game.wait(1.0)
        game.refresh()
        attack_despite_obstructed_layout(game)
    return boosted


# The battle menu has two "Find a Match" buttons: the regular Battle card's
# (x about 227 on the bot's screen) and Ranked Battle's (about 586), which
# spends one of the day's ranked attacks. Only one left of this is tapped:
# while a battle left midway still runs, the regular card shows a countdown
# instead, and as the menu opens Ranked Battle's can be drawn first, so
# "the leftmost one" alone would be Ranked Battle's.
REGULAR_MATCH_RIGHT = 407


def _find_a_match(game: Game, timeout: float) -> bool:
    """Tap the regular Battle's Find a Match, never Ranked Battle's. Raises
    :class:`SearchBlocked` if its card shows "Attack available in" (the
    lockout, shown in the menu). True if it was tapped."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        if game.has(T.ATTACK_COOLDOWN) and game.find(T.ATTACK_COOLDOWN):
            log.warning("Attack countdown in the battle menu: %s", _screen_state(game))
            raise SearchBlocked("lockout screen detected (attack countdown): the Battle card says \"Attack "
                                "available in\" (a battle left without surrendering still runs)")
        if game.click(T.FIND_MATCH, region=Rect(0, 0, REGULAR_MATCH_RIGHT, game.size[1]), leftmost=True):
            return True
        if game.clock() >= deadline:
            return False
        game.wait(0.5, exact=True)


NEXT_TIMEOUT = 40  # s for a base to show (slow emulators and networks take a while on the clouds)
RECALIBRATE_AFTER = 10  # s without Next before the UI scale is measured again on this screen
# "Unable to attack Village!" is drawn a little differently on some emulators:
# looked for less strictly than other pictures (it's only looked for here).
UNABLE_THRESHOLD = 0.72
# ...and the screen it's on is nearly all white: what's left when the picture misses.
WHITE_PIXEL = (40, 215)  # saturation at most, value at least
LOCKOUT_WHITE = 0.6  # share of the screen that white


def white_share(screen) -> float:
    """Share of ``screen`` that is plain white (the "Unable to attack" screen)."""
    import cv2

    small = cv2.resize(screen, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    white = (hsv[..., 1] <= WHITE_PIXEL[0]) & (hsv[..., 2] >= WHITE_PIXEL[1])
    return float(white.mean())


def _unable_to_attack(game: Game) -> bool:
    return game.has(T.UNABLE_TO_ATTACK) and game.find(T.UNABLE_TO_ATTACK, threshold=UNABLE_THRESHOLD) is not None


def _screen_state(game: Game) -> str:
    """What the screen looks like, for the log: each picture's score, the UI
    scale and how much of it is white."""
    return (f"{game.scores(T.NEXT, T.UNABLE_TO_ATTACK, T.RETURN_HOME)}; UI scale {game.ui_scale or 1.0:.2f}; "
            f"white {white_share(game.screen):.0%}")


def _blocked(game: Game, why: str) -> SearchBlocked:
    """The lockout: Return Home first if it's there, then the error that
    closes the game for a few minutes."""
    log.warning("Lockout screen (%s): %s", why, _screen_state(game))
    for button in (T.LOCKOUT_RETURN_HOME, T.RETURN_HOME):
        if game.has(button) and game.click(button):
            log.info("Pressed Return Home on the lockout screen")
            game.wait(1.0)
            break
    return SearchBlocked(f"lockout screen detected ({why}): Clash of Clans says \"Unable to attack Village! "
                         "Try again later.\" (a battle left without surrendering still runs)")


def _wait_for_base(game: Game, timeout: float = NEXT_TIMEOUT) -> bool:
    """Until a base shows (its Next button). Raises :class:`SearchBlocked`
    if the game says it can't attack. Halfway through a long wait the UI
    scale is measured again, in case Next is there at another size."""
    start = game.clock()
    deadline = start + timeout
    recalibrated = False
    while True:
        game.refresh()
        if game.find(T.NEXT):
            return True
        if _unable_to_attack(game):
            raise _blocked(game, "its words")
        if game.has(T.RETURN_HOME) and game.find(T.RETURN_HOME):
            # A result screen instead of a base: the lockout's own Return
            # Home (its words drawn too differently to match), or a battle
            # left on its result.
            if white_share(game.screen) >= LOCKOUT_WHITE:
                raise _blocked(game, "a white screen with Return Home")
            log.warning("A battle result showed instead of a base: going home (%s)", _screen_state(game))
            game.click(T.RETURN_HOME)
            game.wait(1.0)
            raise NextNotFound("next button never appeared: a battle result showed instead of a base")
        if not recalibrated and game.clock() - start >= RECALIBRATE_AFTER:
            recalibrated = True
            if game.recalibrate(T.NEXT) and game.find(T.NEXT):
                return True
        if game.clock() >= deadline:
            if white_share(game.screen) >= LOCKOUT_WHITE:
                raise _blocked(game, "a white screen where a base should be")
            log.warning("Next not detected after %ds: %s", timeout, _screen_state(game))
            return False
        game.wait(0.5, exact=True)


def _press_next(game: Game, delay: float) -> None:
    """Tap Next (looked for once more if it's gone a moment), then wait."""
    if not game.click(T.NEXT):
        game.wait(1.0, exact=True)
        game.refresh()
        if not game.click(T.NEXT):
            log.warning("Next not detected to tap: %s", _screen_state(game))
            raise NextNotFound("next button disappeared")
    game.wait(delay)


SAME_VIEW = 8.0  # mean grey difference (of 255) below which two looks at a base are the same base


def _view(screen):
    """A small grey picture of the screen: enough to tell two bases apart."""
    import cv2

    small = cv2.resize(screen, None, fx=0.125, fy=0.125, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype("int16")


def same_base(before, after) -> bool:
    """Whether two small views (see :func:`_view`) show the same base: the
    loot alone can be the same on two bases."""
    if before is None or after is None or before.shape != after.shape:
        return False
    return float(abs(after - before).mean()) < SAME_VIEW


MAX_UNREADABLE = 3  # bases in a row whose loot can't be read before giving up


def search(game: Game, criteria: SearchConfig, regions: RegionsConfig, on_base: BaseCallback | None = None,
           zoom_out: bool = False, look: bool = False, near: bool = False) -> Loot:
    """Press Next until a base meets ``criteria``; returns its loot.
    ``zoom_out``: the village was left zoomed in (on an upgrade), and the
    battle opens at its zoom: zoom out on the first base. ``look``: look at
    each base a while first, as a person would ("Look at each base").
    ``near``: "Imperfect choices": a base just under the minimums is
    sometimes attacked, one just over them sometimes passed over.

    Must be called after :func:`start_search`. Raises :class:`SearchError`
    if the search gets stuck or exceeds ``criteria.max_searches``.
    """
    last: Loot | None = None
    last_view = None
    repeats = unreadable = 0
    zoomed = zoom_out
    for count in range(1, criteria.max_searches + 1):
        if not _wait_for_base(game):
            raise NextNotFound(f"next button never appeared in {NEXT_TIMEOUT}s; search screen not loaded")
        game.wait(0.5)  # let the loot counters finish animating
        if count == 1 and zoom_out:
            game.zoom_out()
        game.refresh()
        loot = read_settled_loot(game, regions)
        if loot is None and not zoomed:
            # The battle opens at the village's zoom: left zoomed in on an
            # upgrade, the base's health bars can sit over the numbers.
            zoomed = True
            game.zoom_out()
            loot = read_settled_loot(game, regions)
        if loot is None:
            unreadable += 1
            if unreadable >= MAX_UNREADABLE:
                raise LootUnreadable(f"could not read available loot on {unreadable} bases in a row (UI scale "
                                     f"{game.ui_scale or 1.0:.2f}); check regions and the loot font")
            log.warning("Base %d: loot OCR failed; skipping it", count)
            _press_next(game, criteria.next_delay)
            continue
        unreadable = 0
        accepted = is_worth_attacking(loot, criteria)
        slip = near_miss(game.rng, loot, criteria, accepted) if near else None
        if slip is not None:
            accepted = slip
        log.info("Base %d: gold %s, elixir %s, dark %s%s%s", count, f"{loot.gold:,}", f"{loot.elixir:,}",
                 f"{loot.dark:,}", "  → attack" if accepted else "",
                 "" if slip is None else " (a little under the minimums: taken anyway)" if slip else
                 " (meets the minimums, only just: passed over)")
        if on_base:
            on_base(loot, accepted)
        look_at_base(game, loot, criteria, accepted, look)
        if accepted:
            return loot
        view = _view(game.screen)
        if loot == last and same_base(last_view, view):
            repeats += 1
            if repeats >= 2:
                raise NextDidNothing("next tap did not change the base (tapped again, still the same); "
                                     "search is stuck")
            log.info("Base %d is the one before: the Next tap didn't take; tapping it again", count)
        else:
            repeats = 0
        last, last_view = loot, view
        _press_next(game, criteria.next_delay)
    raise SearchError(f"no base matched after {criteria.max_searches} searches")
