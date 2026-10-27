"""Find a base worth attacking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from farmbot.config import RegionsConfig, SearchConfig
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.text import TextStyle

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


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


def read_loot(game: Game, regions: RegionsConfig) -> Loot | None:
    gold = game.read_number(game.region(regions.rect("loot_gold")), T.LOOT_FONT)
    elixir = game.read_number(game.region(regions.rect("loot_elixir")), T.LOOT_FONT)
    if gold is None or elixir is None:
        return None
    # Dark elixir is absent on low town halls; treat unreadable as zero.
    dark = game.read_number(game.region(regions.rect("loot_dark")), T.LOOT_FONT) or 0
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
    dark = _own(game, regions, "own_dark", shift) or 0
    return Loot(gold, elixir, dark)


# Tapping a resource bar opens a box under it, "Max: 21 000 000" first; tapping
# it again closes it (in the builder base too). The "Max:" label is
# smaller than the number, so pieces shorter than the digits are left out.
MAX_STYLE = TextStyle(min_height_ratio=0.85)
MAX_ROW = (33, 30)  # top and height of the "Max:" row, below the bar's middle
MAX_SPAN = (190, 40)  # the row reaches this far left of the bar's number and right of it


def _max_region(game: Game, regions: RegionsConfig, kind: str) -> Rect:
    r = game.region(regions.rect(f"own_{kind}"))
    middle = r.y + r.h // 2
    return Rect(r.x + r.w - MAX_SPAN[0], middle + MAX_ROW[0], MAX_SPAN[0] + MAX_SPAN[1], MAX_ROW[1])


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
        game.wait(0.8)
        if is_open():
            most = game.vision.read_number(game._current(), where, T.LOOT_FONT, MAX_STYLE)
            break
    for _ in range(2):  # close it
        if not is_open():
            break
        game.tap(*bar)
        game.wait(0.6)
    return most


def storage_maxes(game: Game, regions: RegionsConfig, kinds: tuple[str, ...] = ("gold", "elixir", "dark")
                  ) -> dict[str, int]:
    """What each storage holds (no dark elixir on low town halls). The
    builder base has only ``("gold", "elixir")``: its third bar is gems."""
    game.refresh()
    maxes = {}
    for kind in kinds:
        if _own(game, regions, f"own_{kind}") is None:
            continue  # no such bar
        most = storage_max(game, regions, kind)
        if most:
            maxes[kind] = most
    return maxes


FULL_AMOUNT = 0.99  # of what a storage holds: a little short of it is full enough


def full_by_amount(loot: Loot, maxes: dict[str, int]) -> set[str]:
    """Which storages are full, from what's in them and what they hold."""
    return {k for k, most in maxes.items() if getattr(loot, k) >= most * FULL_AMOUNT}


def start_search(game: Game) -> None:
    """Home village → Battle → Find a Match → Attack! (army screen)."""
    game.refresh()
    # The battle menu may already be open (e.g. after a failed round).
    if not game.find(T.FIND_MATCH) and not game.click(T.BATTLE):
        raise SearchError("battle button not visible; is the home village showing?")
    # The menu has two "Find a Match" buttons; the regular one is on the left.
    if not game.click_when_visible(T.FIND_MATCH, timeout=5, leftmost=True):
        raise SearchError("find-a-match button did not appear")
    # Newer versions confirm the army first; older ones go straight to scouting.
    if game.has(T.ARMY_ATTACK) and game.click_when_visible(T.ARMY_ATTACK, timeout=4):
        log.debug("Confirmed army")
    if game.has(T.LAYOUT_OBSTRUCTED):
        from farmbot.tasks.village import attack_despite_obstructed_layout

        game.wait(1.0)
        game.refresh()
        attack_despite_obstructed_layout(game)


MAX_UNREADABLE = 3  # bases in a row whose loot can't be read before giving up


def search(game: Game, criteria: SearchConfig, regions: RegionsConfig, on_base: BaseCallback | None = None) -> Loot:
    """Press Next until a base meets ``criteria``; returns its loot.

    Must be called after :func:`start_search`. Raises :class:`SearchError`
    if the search gets stuck or exceeds ``criteria.max_searches``.
    """
    last: Loot | None = None
    repeats = unreadable = 0
    for count in range(1, criteria.max_searches + 1):
        if not game.wait_for(T.NEXT, timeout=20):
            raise SearchError("next button never appeared; search screen not loaded")
        game.wait(0.5)  # let the loot counters finish animating
        game.refresh()
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
