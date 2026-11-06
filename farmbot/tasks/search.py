"""Find a base worth attacking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
        if most and most >= max(held, LEAST_STORAGE):
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
    # The menu has two "Find a Match" buttons; the regular one is on the left.
    if not game.click_when_visible(T.FIND_MATCH, timeout=5, leftmost=True):
        # A tap on Battle can go unanswered (the village just back from an
        # attack, say): still on the village, tap it once more.
        if not (game.click(T.BATTLE) and game.click_when_visible(T.FIND_MATCH, timeout=5, leftmost=True)):
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


def _wait_for_base(game: Game, timeout: float) -> bool:
    """Until a base shows (its Next button). Raises :class:`SearchBlocked`
    if the game says it can't attack."""
    deadline = game.clock() + timeout
    while True:
        game.refresh()
        if game.find(T.NEXT):
            return True
        if game.has(T.UNABLE_TO_ATTACK) and game.find(T.UNABLE_TO_ATTACK):
            raise SearchBlocked("Clash of Clans says \"Unable to attack Village! Try again later.\" "
                                "(a battle left without surrendering still runs)")
        if game.clock() >= deadline:
            return False
        game.wait(0.5)


MAX_UNREADABLE = 3  # bases in a row whose loot can't be read before giving up


def search(game: Game, criteria: SearchConfig, regions: RegionsConfig, on_base: BaseCallback | None = None,
           zoom_out: bool = False, look: bool = False) -> Loot:
    """Press Next until a base meets ``criteria``; returns its loot.
    ``zoom_out``: the village was left zoomed in (on an upgrade), and the
    battle opens at its zoom: zoom out on the first base. ``look``: look at
    each base a while first, as a person would ("Look at each base").

    Must be called after :func:`start_search`. Raises :class:`SearchError`
    if the search gets stuck or exceeds ``criteria.max_searches``.
    """
    last: Loot | None = None
    repeats = unreadable = 0
    zoomed = zoom_out
    for count in range(1, criteria.max_searches + 1):
        if not _wait_for_base(game, timeout=20):
            raise SearchError("next button never appeared; search screen not loaded")
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
                raise SearchError("could not read available loot; check regions and the loot font")
            log.warning("Base %d: could not read its loot; skipping it", count)
            if not game.click(T.NEXT):
                raise SearchError("next button disappeared")
            game.wait(criteria.next_delay)
            continue
        unreadable = 0
        accepted = is_worth_attacking(loot, criteria)
        log.info("Base %d: gold %s, elixir %s, dark %s%s", count, f"{loot.gold:,}", f"{loot.elixir:,}",
                 f"{loot.dark:,}", "  → attack" if accepted else "")
        if on_base:
            on_base(loot, accepted)
        look_at_base(game, loot, criteria, accepted, look)
        if accepted:
            return loot
        if loot == last:
            repeats += 1
            if repeats >= 2:
                raise SearchError("loot did not change after pressing next; search is stuck")
        else:
            repeats = 0
        last = loot
        if not game.click(T.NEXT):
            raise SearchError("next button disappeared")
        game.wait(criteria.next_delay)
    raise SearchError(f"no base matched after {criteria.max_searches} searches")
