"""Activating (boosting) super troops on the army screen, before a battle.

A super troop in the army that isn't boosted shows a green Activate under
its card. Activate opens a "Boost?" window: a Super Potion (a magic item:
never used) or dark elixir (the green button). Only dark elixir is spent,
only when there's enough of it, and never a gem.
"""

from __future__ import annotations

import logging

from farmbot.game import Game, T
from farmbot.tasks.build_list import asks_for_gems

log = logging.getLogger(__name__)

BOOST_PRICE = 25_000  # dark elixir to boost a super troop for 3 days (events can make it less)
MAX_BOOSTS = 2  # super troops boosted at a time
NEEDED = (T.SUPER_ACTIVATE, T.SUPER_BOOST_TITLE, T.SUPER_BOOST_DARK, T.CLOSE)


def activate_super_troops(game: Game, dark: int | None) -> int:
    """Boost each super troop showing Activate on the army screen with dark
    elixir, ``dark`` being what you have (``None``: unknown, so none are
    boosted). Returns how many were boosted."""
    if not all(game.has(n) for n in NEEDED):
        return 0
    done = 0
    for _ in range(MAX_BOOSTS):
        game.refresh()
        activate = game.find(T.SUPER_ACTIVATE)
        if activate is None:
            break
        if dark is None or dark < BOOST_PRICE:
            log.info("Super troops: %s; not boosting", "couldn't read the dark elixir" if dark is None else
                     f"{dark:,} dark elixir isn't enough")
            break
        game.tap_match(activate)
        if not game.wait_for(T.SUPER_BOOST_TITLE, timeout=3):
            log.info("Super troops: the Boost window didn't open")
            break
        if not _pay(game):
            _close(game)
            break
        dark -= BOOST_PRICE
        done += 1
    return done


def _pay(game: Game) -> bool:
    """Press the Boost window's dark elixir button. True if it boosted."""
    game.wait(0.5)  # the window grows in
    game.refresh()
    pay = game.find(T.SUPER_BOOST_DARK)
    if pay is None:
        log.info("Super troops: no dark elixir button in the Boost window")
        return False
    if asks_for_gems(game, pay.y):
        log.warning("Super troops: the boost asks for gems; leaving it alone")
        return False
    game.tap_match(pay)
    game.wait(2)
    game.refresh()
    if game.find(T.SUPER_BOOST_TITLE) is not None or asks_for_gems(game, pay.y):
        # Still open, or offering gems for missing dark elixir: never paid.
        log.warning("Super troops: the boost didn't go through")
        return False
    log.info("Boosted a super troop for %s dark elixir", f"{BOOST_PRICE:,}")
    return True


def _close(game: Game) -> None:
    """Close what the boost left open (the Boost window, or a gem offer),
    leaving the army screen."""
    for _ in range(2):
        game.refresh()
        if game.find(T.SUPER_BOOST_TITLE) is None and not asks_for_gems(game, game.screen.shape[0] - 80):
            return
        close = game.find_all(T.CLOSE)
        if not close:
            return
        # The window on top is the middle one; the army screen's X is further right.
        game.tap_match(min(close, key=lambda m: abs(m.x - game.screen.shape[1] // 2)))
        game.wait(1)
