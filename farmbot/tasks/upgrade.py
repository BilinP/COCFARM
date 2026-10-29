"""Upgrading once a storage is full: buildings and heroes, research, pets.

Only a full storage's resource is spent, as far as the builders go: the
dearest upgrade it covers, then the dearest the rest covers, and so on
(going from the dearest down spends nearly all of it). Builders go to the
kinds of building in the chosen order first (heroes, say), then to anything
else. One builder stays free for the walls when they're upgraded too.
Research takes the laboratory's one researcher; pets come last, with the
dark elixir left. Everything is picked in the Build list (see
build_list.py), sorted Cost Max and filtered to the resource.

The builder base works the same way with its gold and elixir, its builders
and its Star Laboratory (no pets there).
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
ORDER = ("hero", "defense", "resource", "army")
LAB_RESOURCES = ("elixir", "dark")  # elixir first: pets want the dark elixir

Reader = Callable[..., Loot | None]


def priority(first: str) -> list[str | None]:
    """The kinds of building in the order they get builders; ``None``:
    any kind (last, for what the kinds before it don't cover)."""
    if first == "none":
        return [None]
    return [first, *(k for k in ORDER if k != first), None]


def upgrade(game: Game, cfg: Config, read_resources: Reader, full: set[str]) -> int:
    """Spend the home village's ``full`` storages' resources on upgrades.
    Returns how many were started."""
    ucfg = cfg.upgrade
    full = {k for k in full if k in HOME.resources}
    if not ucfg.enabled or not full or not all(game.has(n) for n in NEEDED):
        return 0
    budget = _open(game, read_resources, full)
    if budget is None:
        return 0
    builders = _spare(free_builders(game), cfg.walls.enabled)
    done = 0
    try:
        if not _open_list(game):
            return 0
        if ucfg.buildings and builders > 0:
            done += _buildings(game, ucfg.first, budget, builders, read_resources, HOME)
        if ucfg.lab and any(k in budget for k in LAB_RESOURCES):
            done += _research(game, budget, read_resources, HOME)
        if ucfg.pets and "dark" in budget:
            done += _pets(game, budget, read_resources)
    finally:
        _leave(game)
    return done


def upgrade_builder_base(game: Game, bcfg: BuilderConfig, read_resources: Reader, full: set[str]) -> int:
    """The same in the builder base, for its ``full`` gold or elixir
    storages. Returns how many upgrades were started."""
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
        # The builder base's counters are on the list's tabs.
        open_tab(game, "buildings", BUILDER)
        builders = _spare(tab_free(game, "buildings", BUILDER), bcfg.walls)
        if builders > 0:
            done += _buildings(game, bcfg.upgrade_first, budget, builders, read_resources, BUILDER)
        if bcfg.upgrade_lab:
            done += _research(game, budget, read_resources, BUILDER)
    finally:
        _leave(game, view)
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


def _leave(game: Game, view: np.ndarray | None = None) -> None:
    """Upgrading moved the camera to what was upgraded: the next battle's
    view starts from the village's, so put it back (to ``view``, a picture
    of it, if given; else to the middle)."""
    _close_windows(game)
    deselect(game)  # a pinch on what's still selected would move it
    game.zoom_out()
    if view is None:
        game.center_view()
    else:
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


def _buildings(game: Game, first: str, budget: dict[str, int], builders: int, read_resources: Reader,
               lst: BuildList) -> int:
    open_tab(game, "buildings", lst)
    done = 0
    for kind in priority(first):
        for resource in list(budget):
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


def _research(game: Game, budget: dict[str, int], read_resources: Reader, lst: BuildList) -> int:
    open_tab(game, "army", lst)
    if lst.builder_base:
        # The Star Laboratory's tab counts its researcher ("0/1": busy).
        free = tab_free(game, "army", lst)
        if not free:
            log.info("Upgrades: the star laboratory is busy" if free == 0 else
                     "Upgrades: couldn't read the star laboratory's researcher")
            return 0
    elif busy(game):
        log.info("Upgrades: the laboratory is busy")
        return 0
    for resource in LAB_RESOURCES:
        if resource not in budget:
            continue
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
