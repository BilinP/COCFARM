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
Everything is picked in the Build list (see build_list.py), sorted Cost
Max and filtered to the resource. Gems are never spent: an upgrade window
showing a gem (some events make upgrades cost gems too) is closed unpaid.

The builder base works the same way with its gold and elixir, its builders
and its Star Laboratory (no pets there): by default research, heroes, then
buildings; heroes and research take elixir, buildings gold or elixir.
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from farmbot.config import BuilderConfig, Config
from farmbot.game import Game, T
from farmbot.tasks.build_list import (BUILDER, HOME, HUD_SHIFT_PX, BuildList, Row, busy, first_affordable,
                                      free_builders, open_tab, rows, set_filter, tab_free, upgrade_row)
from farmbot.tasks.builder import restore_view
from farmbot.tasks.search import Loot
from farmbot.tasks.village import _close_windows, deselect

log = logging.getLogger(__name__)

NEEDED = (T.BUILD, T.CLOSE, T.LIST_FILTER, T.SORT_COST_MAX, T.UPGRADE_CONFIRM, T.COST_GOLD, T.COST_ELIXIR,
          T.COST_DARK)
BUILDER_NEEDED = (T.BUILD, T.CLOSE, T.LIST_FILTER, T.SORT_COST_MAX, T.BB_FILTER_GOLD, T.BB_FILTER_ELIXIR,
                  T.BB_COST_GOLD, T.BB_COST_ELIXIR)
KIND_FILTER = {"hero": T.FILTER_HERO, "defense": T.FILTER_DEFENSE, "resource": T.FILTER_RESOURCE,
               "army": T.FILTER_ARMY}
BUILDING_KINDS = ("defense", "resource", "army")  # the filter's types, heroes aside
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


def upgrade(game: Game, cfg: Config, read_resources: Reader, full: set[str], last: bool = False) -> int:
    """Spend the home village's ``full`` storages' resources on upgrades.
    What was upgraded is left zoomed in on (the walls, or the battle,
    next zoom out), and dropped unless ``last`` (a battle comes next, which
    drops it). Returns how many were started."""
    ucfg = cfg.upgrade
    full = {k for k in full if k in HOME.resources}
    if not ucfg.enabled or not full or not all(game.has(n) for n in NEEDED):
        return 0
    budget = _open(game, read_resources, full)
    if budget is None:
        return 0
    kinds = plan(ucfg.order, {"pets": ucfg.pets, "hero": ucfg.heroes, "research": ucfg.lab,
                              "building": ucfg.buildings})
    builders = _spare(free_builders(game), cfg.walls.enabled) if {"hero", "building"} & set(kinds) else 0
    done = 0
    try:
        if not _open_list(game):
            return 0
        done = _in_order(game, kinds, HOME_SPEND, ucfg.first, ucfg.heroes, budget, builders, read_resources, HOME)
    finally:
        _close_windows(game)
        if not last:
            deselect(game)  # a pinch on what's still selected would move it
    return done


def upgrade_builder_base(game: Game, bcfg: BuilderConfig, read_resources: Reader, full: set[str],
                         last: bool = False) -> int:
    """The same in the builder base, for its ``full`` gold or elixir
    storages. Unless ``last`` (a battle comes next, which zooms out by
    itself), what was upgraded is dropped and the camera put back. Returns
    how many upgrades were started."""
    full = {k for k in full if k in BUILDER.resources}
    if not bcfg.upgrade or not full or not all(game.has(n) for n in BUILDER_NEEDED):
        return 0
    budget = _open(game, read_resources, full)
    if budget is None:
        return 0
    view = game.refresh().copy()  # put back afterwards (there's no middle to centre on)
    done = 0
    try:
        if not _open_list(game):
            return 0
        kinds = plan(bcfg.upgrade_order, {"hero": bcfg.upgrade_heroes, "research": bcfg.upgrade_lab,
                                          "building": bcfg.upgrade_buildings})
        builders = 0
        if {"hero", "building"} & set(kinds):
            # The builder base's counters are on the list's tabs.
            open_tab(game, "buildings", BUILDER)
            builders = _spare(tab_free(game, "buildings", BUILDER), bcfg.walls)
        done = _in_order(game, kinds, BUILDER_SPEND, bcfg.upgrade_first, bcfg.upgrade_heroes, budget, builders,
                         read_resources, BUILDER)
    finally:
        _close_windows(game)
        if not last:
            _leave(game, view)
    return done


def _in_order(game: Game, kinds: list[str], spend: dict[str, tuple[str, ...]], first: str, heroes: bool,
              budget: dict[str, int], builders: int, read_resources: Reader, lst: BuildList) -> int:
    """Each kind of upgrade in turn, with the resources it may spend that
    are still in ``budget``. Returns how many were started."""
    done = 0
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
            started = _buildings(game, types, money, budget, builders, read_resources, lst)
            builders -= started
            done += started
    return done


def _open(game: Game, read_resources: Reader, full: set[str]) -> dict[str, int] | None:
    """The budget: what's in the full storages."""
    game.zoom_out()
    loot = read_resources()
    return None if loot is None else {k: getattr(loot, k) for k in full}


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
           lst: BuildList) -> bool:
    """Upgrade ``row`` and check it was paid for; ``budget`` follows."""
    before = budget[row.kind]
    if not upgrade_row(game, row, lst):
        return False
    after = read_resources(HUD_SHIFT_PX)  # the open list pushes the numbers over
    if after is None:
        budget[row.kind] = before - row.price
    else:
        if getattr(after, row.kind) >= before:
            log.info("Upgrades: the %s didn't go through", what)
            return False
        budget[row.kind] = getattr(after, row.kind)
    log.info("Upgraded %s for %s %s", what, f"{row.price:,}", row.kind)
    return True


def _buildings(game: Game, kinds: list[str | None], money: list[str], budget: dict[str, int], builders: int,
               read_resources: Reader, lst: BuildList) -> int:
    """Up to ``builders`` upgrades of the ``kinds`` of building (``None``:
    any), paid with ``money``. Returns how many were started."""
    open_tab(game, "buildings", lst)
    done = 0
    for kind in kinds:
        for resource in money:
            if builders <= 0:
                return done
            if not set_filter(game, lst.resource_filter[resource], KIND_FILTER.get(kind), T.SORT_COST_MAX, lst):
                continue  # nothing of that kind to upgrade
            while builders > 0:
                row = first_affordable(game, {resource: budget[resource]}, lst)
                if row is None or not _spend(game, row, budget, read_resources, kind or "a building", lst):
                    break
                builders -= 1
                done += 1
    return done


def _research(game: Game, money: list[str], budget: dict[str, int], read_resources: Reader, lst: BuildList) -> int:
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
            return int(_spend(game, row, budget, read_resources, "research", lst))
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
    return int(_spend(game, max(affordable, key=lambda r: r.price), budget, read_resources, "pet", HOME))
