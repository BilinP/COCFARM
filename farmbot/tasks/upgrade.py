"""Upgrading once a storage is full: pets, heroes, research and buildings.

Only a full storage's resource is spent. The kinds of upgrade take their
turn in the order chosen (by default pets, heroes, research, then
buildings), each spending only what it may:

- pets: dark elixir (the pet house's one slot),
- heroes: elixir or dark elixir (builders),
- research: elixir or dark elixir (the laboratory's one researcher),
- buildings: gold, elixir or dark elixir (builders; the kind of building
  chosen first, then the others).

Each picks the dearest upgrade the resource left covers, then the dearest
the rest covers, and so on (going from the dearest down spends nearly all
of it). One builder stays free for the walls when they're upgraded too.

Buildings not built yet come before the upgrades (the "New" filter): the
game puts each down at a free spot, and it's built there. New walls cost
nothing and take no builder: they (and any other free new building) are
put down first, one list trip each.
The Town Hall comes first among the buildings (after the new ones, which
it may need built): it's under none of the list's Defense, Resource or Army
types, so it's looked for on its own. While it can't be upgraded (the game
wants the buildings it lists built first), the next upgrade is started.
Everything is picked in the Build list (see build_list.py), sorted Cost
Max and filtered to the resource. Gems are never spent: an upgrade window
showing a gem (some events make upgrades cost gems too) is closed unpaid.

The builder base works the same way with its gold and elixir, its builders
and its Star Laboratory (no pets there): by default research, heroes, then
buildings; heroes and research take elixir, buildings gold or elixir.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable

import numpy as np

from farmbot.config import BuilderConfig, Config
from farmbot.game import Game, T
from farmbot.tasks.build_list import (BUILDER, FAILED, HOME, HUD_SHIFT_PX, LOCKED, PLACED, BuildList, Row, busy,
                                      first_affordable, forget_filters, free_builders, free_rows, goblins,
                                      leaving_town_hall, list_shown, open_tab, place_free, rows, set_filter, started,
                                      tab_free,
                                      top_row_affordable, town_hall_row, upgrade_row)
from farmbot.tasks.builder import restore_view
from farmbot.tasks.search import Loot
from farmbot.tasks.village import _close_windows, deselect

log = logging.getLogger(__name__)

NEEDED = (T.BUILD, T.CLOSE, T.LIST_FILTER, T.SORT_COST_MAX, T.UPGRADE_CONFIRM, T.COST_GOLD, T.COST_ELIXIR,
          T.COST_DARK)
BUILDER_NEEDED = (T.BUILD, T.CLOSE, T.LIST_FILTER, T.SORT_COST_MAX, T.BB_FILTER_GOLD, T.BB_FILTER_ELIXIR,
                  T.BB_COST_GOLD, T.BB_COST_ELIXIR)
KIND_FILTER = {"hero": T.FILTER_HERO, "defense": T.FILTER_DEFENSE, "resource": T.FILTER_RESOURCE,
               "army": T.FILTER_ARMY, "new": T.FILTER_NEW}
BUILDING_KINDS = ("defense", "resource", "army")  # the filter's types, heroes aside
MAX_FAILED_ROWS = 2  # rows a list may have that won't upgrade, before moving on (locked ones aside)
PASS_OVER_FOR = 3600.0  # seconds a row that wouldn't upgrade is passed over (not tapped again every round)
TOWN_HALL_MONEY = ("gold",)  # what the Town Hall's upgrade costs
MAX_FREE_BUILDINGS = 5  # free new buildings (walls, mostly) put down in a round, a list trip each:
# the rest wait for later rounds, so the farm keeps attacking
# What each kind of upgrade may spend, in the order it's tried.
HOME_SPEND = {"pets": ("dark",), "hero": ("dark", "elixir"), "research": ("elixir", "dark"),
              "building": ("gold", "elixir", "dark")}
BUILDER_SPEND = {"hero": ("elixir",), "research": ("elixir",), "building": ("gold", "elixir")}

Reader = Callable[..., Loot | None]


def building_kinds(first: str, heroes: bool) -> list[str | None]:
    """The kinds of building in the order they get builders; ``None``: any
    kind, the dearest of anything (heroes too, so only while heroes are
    upgraded)."""
    if first == "none":
        return [None] if heroes else list(BUILDING_KINDS)
    rest = [k for k in BUILDING_KINDS if k != first]
    return [first, *rest] + ([None] if heroes else [])


def plan(order: list[str], enabled: dict[str, bool]) -> list[str]:
    """The kinds of upgrade that are on, in their order."""
    return [k for k in order if enabled.get(k)]


def upgrade(game: Game, cfg: Config, read_resources: Reader, full: set[str], last: bool = False,
            hand_over: dict | None = None, locked: dict[str, str] | None = None, total: int = 0) -> int:
    """Spend the home village's ``full`` storages' resources on upgrades.
    What was upgraded is left zoomed in on (the walls, or the battle,
    next zoom out), and dropped unless ``last`` (a battle comes next, which
    drops it). ``hand_over``: the walls come next, so Buildings & Heroes
    is left open for them (one trip to the list, not two), and this gets
    what they need: "loot", the resources left (read before the upgrades,
    less what they spent), and "builders", the free builders read before
    (one of them was kept for the walls). Left empty if the list wasn't
    left open. ``locked``: what this account hasn't unlocked yet
    (``townhall.locked``): left out. ``total``: the village's builders,
    from its data (0: not known); with it, upgrades keep at most
    ``upgrade.builders`` of them busy (see :func:`_within`). Returns how
    many were started."""
    ucfg = cfg.upgrade
    full = {k for k in full if k in HOME.resources}
    if not ucfg.enabled or not full or not all(game.has(n) for n in NEEDED):
        return 0
    game.zoom_out()
    loot = read_resources()
    if loot is None:
        return 0
    # No dark elixir before Town Hall 7: nothing is looked for in it (the research filter's dark button).
    budget = {k: getattr(loot, k) for k in full if not (k == "dark" and "dark" in (locked or {}))}
    kinds = plan(ucfg.order, {"pets": ucfg.pets, "hero": ucfg.heroes, "research": ucfg.lab,
                              "building": ucfg.buildings})
    for kind, feature in (("pets", "pets"), ("hero", "heroes"), ("research", "research")):
        if kind in kinds and feature in (locked or {}):
            log.info("Upgrades: no %s: %s", "heroes" if kind == "hero" else kind, locked[feature])
            kinds.remove(kind)
    if "research" in kinds and "lab" in goblins(game):
        log.info("Upgrades: the laboratory is busy (only the Goblin Researcher, for gems)")
        kinds = [k for k in kinds if k != "research"]
    uses_builders = bool({"hero", "building"} & set(kinds))
    free = free_builders(game) if uses_builders or hand_over is not None else None
    if not uses_builders:
        builders = 0
    elif total:
        builders = _within(free, total, ucfg.builders)
    else:
        builders = _spare(free, cfg.walls.enabled)
    done, handed = 0, False
    lst = HOME if ucfg.town_hall else leaving_town_hall(HOME)
    try:
        forget_filters(game)  # a new round: its filters as the game has them, not known
        if not _open_list(game):
            return 0
        left = {"builders": builders}
        done = _in_order(game, kinds, HOME_SPEND, ucfg.first, ucfg.heroes, budget, builders, read_resources, lst,
                         new=ucfg.new_buildings, left=left)
        if hand_over is not None:
            # The walls need one free: what was free less what these upgrades just put to work.
            still_free = None if free is None else max(free - (builders - left["builders"]), 0)
            hand_over.update(loot=replace(loot, **budget), builders=still_free)
            handed = True
    finally:
        if not handed:
            _close_windows(game)
            if not last:
                deselect(game)  # a pinch on what's still selected would move it
    return done


def upgrade_builder_base(game: Game, bcfg: BuilderConfig, read_resources: Reader, full: set[str],
                         last: bool = False, total: int = 0) -> int:
    """The same in the builder base, for its ``full`` gold or elixir
    storages. Unless ``last`` (a battle comes next, which zooms out by
    itself), what was upgraded is dropped and the camera put back. Returns
    how many upgrades were started. ``total``: the builder base's builders,
    from the village data (0: not known); with it, upgrades keep at most
    ``builder.upgrade_builders`` of them busy."""
    full = {k for k in full if k in BUILDER.resources}
    if not bcfg.upgrade or not full or not all(game.has(n) for n in BUILDER_NEEDED):
        return 0
    budget = _open(game, read_resources, full)
    if budget is None:
        return 0
    view = game.refresh().copy()  # put back afterwards (there's no middle to centre on)
    done = 0
    try:
        forget_filters(game)  # a new round: its filters as the game has them, not known
        if not _open_list(game):
            return 0
        kinds = plan(bcfg.upgrade_order, {"hero": bcfg.upgrade_heroes, "research": bcfg.upgrade_lab,
                                          "building": bcfg.upgrade_buildings})
        builders = 0
        if {"hero", "building"} & set(kinds):
            # The builder base's counters are on the list's tabs.
            open_tab(game, "buildings", BUILDER)
            free = tab_free(game, "buildings", BUILDER)
            builders = (_within(free, total, bcfg.upgrade_builders) if total else _spare(free, bcfg.walls))
        done = _in_order(game, kinds, BUILDER_SPEND, bcfg.upgrade_first, bcfg.upgrade_heroes, budget, builders,
                         read_resources, BUILDER)
    finally:
        _close_windows(game)
        if not last:
            _leave(game, view)
    return done


def _in_order(game: Game, kinds: list[str], spend: dict[str, tuple[str, ...]], first: str, heroes: bool,
              budget: dict[str, int], builders: int, read_resources: Reader, lst: BuildList,
              new: bool = False, left: dict | None = None) -> int:
    """Each kind of upgrade in turn, with the resources it may spend that
    are still in ``budget``; with ``new``, buildings not built yet come
    first among the buildings (free ones, like new walls, before anything). Returns
    how many were started."""
    done = 0
    if new and "building" in kinds:
        done += _free_buildings(game, lst)
    for kind in kinds:
        money = [r for r in spend[kind] if r in budget]
        if not money:
            continue
        if kind == "pets":
            done += _pets(game, budget, read_resources)
        elif kind == "research":
            done += _research(game, money, budget, read_resources, lst)
        elif builders > 0:
            types = ["hero"] if kind == "hero" else building_kinds(first, heroes)
            if kind == "building" and _upgrades_town_hall(lst):
                # Not under Defense, Resource or Army: looked for on its own, before those take the builders.
                types = ["town hall", *types]
            if new and kind == "building":
                types = ["new", *types]
            started = _buildings(game, types, money, budget, builders, read_resources, lst)
            builders -= started
            done += started
    if left is not None:
        left["builders"] = builders  # (of those it was given)
    return done


def _open(game: Game, read_resources: Reader, full: set[str]) -> dict[str, int] | None:
    """The budget: what's in the full storages."""
    game.zoom_out()
    loot = read_resources()
    return None if loot is None else {k: getattr(loot, k) for k in full}


def builder_limit(total: int, chosen: int) -> int:
    """How many of ``total`` builders upgrades may keep busy: ``chosen``
    (``upgrade.builders``), or all but one when it's 0; none when it's
    negative (the slider at 0)."""
    if chosen < 0:
        return 0
    return min(chosen, total) if chosen > 0 else max(total - 1, 1)


def _within(free: int | None, total: int, chosen: int) -> int:
    """The free builders upgrades may use, keeping the ones over the limit
    free (for the walls, or for you): with 6 builders and a limit of 5, 3
    free -> 2."""
    if free is None:
        log.info("Upgrades: couldn't read the free builders")
        return 0
    limit = builder_limit(total, chosen)
    use = max(min(free, total) - (total - limit), 0)
    working = max(total - free, 0)
    kept = min(free, total) - use  # free, but over the limit
    log.info("Upgrades: %d of %d builders already working, %d free: %s%s", working, total, free,
             f"{use} more can start" if use else "none can start",
             f" ({kept} kept free: the limit is {limit} working)" if kept else
             f" (the limit is {limit} working)" if not use and free else "")
    return use


def _spare(builders: int | None, walls: bool) -> int:
    if builders is None:
        log.info("Upgrades: couldn't read the free builders")
        return 0
    if walls:
        builders -= 1  # one for the walls
    builders = max(builders, 0)
    log.info("Upgrades: %d builder%s to use", builders, "" if builders == 1 else "s")
    return builders


def _open_list(game: Game) -> bool:
    game.refresh()
    if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
        log.info("Upgrades: the Build list didn't open")
        return False
    game.wait(1.2)
    return True


def _leave(game: Game, view: np.ndarray) -> None:
    """Upgrading moved the camera to what was upgraded and zoomed in: zoom
    out again, and drag it back to ``view``, a picture of the builder
    base's (it holds two bases)."""
    deselect(game)  # a pinch on what's still selected would move it
    game.zoom_out()
    restore_view(game, view)


def _spend(game: Game, row: Row, budget: dict[str, int], read_resources: Reader, what: str,
           lst: BuildList) -> str:
    """Upgrade ``row`` (or build it, if it's new) and check it was paid
    for; ``budget`` follows. What ``upgrade_row`` said, or FAILED if it
    wasn't paid for."""
    before = budget[row.kind]
    outcome = upgrade_row(game, row, lst)
    if not started(outcome):
        return outcome
    # The open list pushes the numbers over; building something new closed it.
    after = read_resources() if outcome == PLACED else read_resources(HUD_SHIFT_PX)
    if after is None:
        budget[row.kind] = before - row.price
    else:
        if getattr(after, row.kind) >= before:
            log.info("Upgrades: the %s didn't go through", what)
            return FAILED
        budget[row.kind] = getattr(after, row.kind)
    log.info("%s %s for %s %s", "Built" if outcome == PLACED else "Upgraded", what, f"{row.price:,}", row.kind)
    if outcome == PLACED:
        game.built_new += 1
    return outcome


def _list_open(game: Game, lst: BuildList) -> bool:
    """Open the list again if building something closed it (opening it
    also drops what was just built, which stays selected). It opens on
    the tab and filter it had, so the tab is tapped only if it isn't lit.
    False if it won't open."""
    game.refresh()
    if list_shown(game):
        return True
    if not _open_list(game):
        return False
    open_tab(game, "buildings", lst)
    return True


def _free_buildings(game: Game, lst: BuildList) -> int:
    """Put down what the list offers for nothing ("FREE": new walls, which
    take no builder, and some new buildings), up to MAX_FREE_BUILDINGS, one
    list trip each. Returns how many."""
    if not game.has(T.FREE_ROW):
        return 0
    open_tab(game, "buildings", lst)
    # Free is the cheapest: at the top, sorted Cost Min.
    sort = T.SORT_COST_MIN if game.has(T.SORT_COST_MIN) else T.SORT_COST_MAX
    if not set_filter(game, None, T.FILTER_NEW, sort, lst):
        return 0  # nothing new to build
    placed = 0
    while placed < MAX_FREE_BUILDINGS:
        game.refresh()
        free = free_rows(game, lst)
        if not free:
            break
        built = place_free(game, free[0])
        if not _list_open(game, lst) or not built:
            break
        placed += 1
    if placed:
        log.info("Upgrades: built %d free new building%s (walls and the like)", placed, "" if placed == 1 else "s")
    return placed


def _upgrades_town_hall(lst: BuildList) -> bool:
    return lst is not None and not lst.builder_base and T.TOWN_HALL_ROW not in lst.left_out


def _town_hall(game: Game, money: list[str], budget: dict[str, int], read_resources: Reader,
               lst: BuildList) -> int:
    """The Town Hall's upgrade, when ``budget`` covers it: 1 if started."""
    paid = [r for r in TOWN_HALL_MONEY if r in money]
    if not paid or not game.has(T.TOWN_HALL_ROW):
        return 0
    if not set_filter(game, lst.resource_filter[paid[0]], None, T.SORT_COST_MAX, lst):
        return 0
    row, seen = town_hall_row(game, tuple(paid), lst)
    if row is None:
        log.info("Upgrades: %s", "the Town Hall costs more than the gold there is" if seen else
                 "no Town Hall upgrade in the list (under way already, or the top level)")
        return 0
    if (row.kind, row.price) in _passed_over(game):
        return 0  # locked a moment ago: the game wants other buildings first
    if row.price > budget[row.kind]:
        log.info("Upgrades: the Town Hall costs %s %s, more than the %s there is", f"{row.price:,}", row.kind,
                 f"{budget[row.kind]:,}")
        return 0
    outcome = _spend(game, row, budget, read_resources, "Town Hall", lst)
    if started(outcome):
        return 1
    _pass_over(game, row)
    return 0


def _buildings(game: Game, kinds: list[str | None], money: list[str], budget: dict[str, int], builders: int,
               read_resources: Reader, lst: BuildList) -> int:
    """Up to ``builders`` upgrades of the ``kinds`` of building (``None``:
    any), paid with ``money``. Returns how many were started."""
    open_tab(game, "buildings", lst)
    done = 0
    for kind in kinds:
        if kind == "town hall":
            if builders > 0:
                started_ = _town_hall(game, money, budget, read_resources, lst)
                builders -= started_
                done += started_
                if not _list_open(game, lst):
                    return done
            continue
        # New buildings are few: one list of them in every resource (one
        # trip to the filter menu, not one per resource). Upgrades: a list
        # per resource, each sorted by its own price.
        groups = [tuple(money)] if kind == "new" and len(money) > 1 else [(r,) for r in money]
        for group in groups:
            if builders <= 0:
                return done
            alone = group[0] if len(group) == 1 else None
            what_for = alone or " or ".join(group)
            if not set_filter(game, lst.resource_filter[alone] if alone else None, KIND_FILTER.get(kind),
                              T.SORT_COST_MAX, lst):
                if kind == "new":
                    log.info("Upgrades: nothing new to build for %s", what_for)
                else:
                    log.info("Upgrades: no %s upgrade%s for %s in the list", kind or "building",
                             "" if kind else "s", what_for)
                continue  # nothing of that kind to upgrade
            spent = False
            skip = _passed_over(game)  # rows that wouldn't upgrade (lately): the next one
            failed = 0  # of those, the ones that weren't locked (a locked Town Hall doesn't count)
            while builders > 0 and failed < MAX_FAILED_ROWS:
                if spent and alone and not _may_afford(game, alone, budget[alone], kind, lst):
                    break
                row = first_affordable(game, {r: budget[r] for r in group}, lst, skip)
                if row is None:
                    break
                what = "a new building" if kind == "new" else kind or "a building"
                outcome = _spend(game, row, budget, read_resources, what, lst)
                if not _list_open(game, lst):
                    return done
                if not started(outcome):
                    skip.add((row.kind, row.price))
                    _pass_over(game, row)
                    failed += outcome != LOCKED
                    continue
                spent = True
                builders -= 1
                done += 1
    return done


def _passed_over(game: Game) -> set[tuple[str, int]]:
    """The rows that wouldn't upgrade in the last PASS_OVER_FOR seconds."""
    rows_out = getattr(game, "unupgradable", None)
    if rows_out is None:
        return set()
    now = game.clock()
    for key in [k for k, until in rows_out.items() if until <= now]:
        del rows_out[key]
    return set(rows_out)


def _pass_over(game: Game, row: Row) -> None:
    rows_out = getattr(game, "unupgradable", None)
    if rows_out is not None:
        rows_out[(row.kind, row.price)] = game.clock() + PASS_OVER_FOR


def _may_afford(game: Game, resource: str, left: int, kind: str | None, lst: BuildList) -> bool:
    """After an upgrade has spent most of the budget: the cheapest upgrade
    (sorted Cost Min, at the top) can be paid for. Saves scrolling the
    whole Cost Max list (a minute, at a big town hall) to find there is
    none. The list is sorted Cost Max again if there may be one."""
    if not game.has(T.SORT_COST_MIN):
        return True
    if not set_filter(game, lst.resource_filter[resource], KIND_FILTER.get(kind), T.SORT_COST_MIN, lst):
        return True
    if top_row_affordable(game, (resource,), lst) is False:
        log.info("Upgrades: even the cheapest costs more than the %s %s left", f"{left:,}", resource)
        return False
    set_filter(game, lst.resource_filter[resource], KIND_FILTER.get(kind), T.SORT_COST_MAX, lst)
    return True


def _research(game: Game, money: list[str], budget: dict[str, int], read_resources: Reader, lst: BuildList) -> int:
    if not lst.builder_base and tab_free(game, "army") == 0:
        # The researcher's count under the tab ("0/1") shows from any tab:
        # a busy laboratory isn't opened.
        log.info("Upgrades: the laboratory is busy")
        return 0
    open_tab(game, "army", lst)
    if lst.builder_base:
        # The Star Laboratory's tab counts its researcher ("0/1": busy).
        free = tab_free(game, "army", lst)
        if not free:
            log.info("Upgrades: the star laboratory is busy" if free == 0 else
                     "Upgrades: couldn't read the star laboratory's researcher")
            return 0
    elif busy(game) or tab_free(game, "army") == 0:
        # The tab's counter ("0/1") says so even when the research under
        # way has a green bar rather than a grey one.
        log.info("Upgrades: the laboratory is busy")
        return 0
    for resource in money:
        # The Star Laboratory's research is all elixir: its filter only sorts.
        button = None if lst.builder_base else lst.resource_filter[resource]
        if not set_filter(game, button, None, T.SORT_COST_MAX, lst):
            continue
        row = first_affordable(game, {resource: budget[resource]}, lst)
        if row is not None:
            # One researcher: whatever happened, that's it for this round.
            return int(started(_spend(game, row, budget, read_resources, "research", lst)))
    return 0


def _pets(game: Game, budget: dict[str, int], read_resources: Reader) -> int:
    open_tab(game, "pets")
    # The tab's counter ("0/1") says so even when the pet's bar under way is
    # green rather than grey.
    if busy(game) or tab_free(game, "pets") == 0:
        log.info("Upgrades: the pet house is busy")
        return 0
    # The pets tab has no filter, and only a few rows: the dearest that fits.
    affordable = [r for r in rows(game, ("dark",)) if r.price <= budget["dark"]]
    if not affordable:
        return 0
    best = max(affordable, key=lambda r: r.price)
    return int(started(_spend(game, best, budget, read_resources, "pet", HOME)))
