"""Which screen the game is showing, and getting back to a village from
whatever it is.

Each round starts by asking where the game is instead of assuming, so a
chest room, a reward picker, a results screen or the builder base at
startup are dealt with in one place.
"""

from __future__ import annotations

import enum
import logging

from farmbot.game import Game, T

log = logging.getLogger(__name__)


class Screen(enum.Enum):
    HOME = "home village"
    BUILDER = "builder base"
    BATTLE = "battle"  # scouting or fighting (home or builder)
    RESULTS = "battle results"
    CHEST = "event chest room"
    REWARD = "reward picker"
    POPUP = "popup"
    UNKNOWN = "unknown screen"  # loading, an animation, or something new


VILLAGES = (Screen.HOME, Screen.BUILDER)

# Buttons that only show on a window over the village (tried in this order
# when clearing them). Continue sits under a chest's reward.
POPUP_BUTTONS = (T.TRY_AGAIN, T.RELOAD, T.BB_OKAY, T.CONTINUE, T.OKAY, T.CLOSE)


def _seen(game: Game, name: str) -> bool:
    return game.has(name) and game.find(name) is not None


def identify(game: Game, refresh: bool = True) -> Screen:
    """Name the screen on show. The order matters: the reward picker
    covers a battle, popups cover a village, and the home village's
    Battle button also matches the builder base's."""
    from farmbot.tasks import builder  # tasks import this module's callers

    if refresh:
        game.refresh()
    if _seen(game, T.PICK_REWARD):
        return Screen.REWARD
    if _seen(game, T.EVENT_CHEST):
        return Screen.CHEST
    if _seen(game, T.RETURN_HOME) or _seen(game, T.BB_RETURN_HOME):
        return Screen.RESULTS
    if any(_seen(game, name) for name in (T.END_BATTLE, T.BB_BATTLE_STARTS, T.BB_BATTLE_ENDS)):
        return Screen.BATTLE
    if any(_seen(game, name) for name in POPUP_BUTTONS):
        return Screen.POPUP
    if builder.in_builder_base(game):
        return Screen.BUILDER
    if _seen(game, T.BATTLE):
        return Screen.HOME
    return Screen.UNKNOWN


class StuckError(RuntimeError):
    """The game stayed on a screen the bot can't get past."""


SETTLE_STEPS = 10
UNKNOWN_WAIT = 3.0  # seconds: loading screens and animations pass by themselves


def settle(game: Game, steps: int | None = None) -> Screen:
    """Deal with whatever is on screen until a village shows: pick the
    reward, open the chest, leave the results or the battle, close popups,
    wait out loading. Returns the village; raises StuckError if it never
    gets there (the caller saves the screen and, after a few, restarts)."""
    from farmbot.tasks import village

    seen = []
    for _ in range(steps or SETTLE_STEPS):
        screen = identify(game)
        if screen in VILLAGES:
            if seen:
                log.info("Back in the %s (after: %s)", screen.value, ", ".join(s.value for s in seen))
            return screen
        seen.append(screen)
        if screen is Screen.REWARD:
            village.pick_reward(game)
        elif screen is Screen.CHEST:
            if not village.open_event_chests(game):
                game.wait(UNKNOWN_WAIT)
        elif screen is Screen.RESULTS:
            if not (game.click(T.RETURN_HOME) or game.click(T.BB_RETURN_HOME)):
                game.wait(1)
            game.wait(3)
        elif screen is Screen.BATTLE:
            if not village.leave_battle(game):
                game.wait(UNKNOWN_WAIT)  # a builder battle without End Battle: let it play out
        elif screen is Screen.POPUP:
            village.clear_popups(game, rounds=1)
        else:
            game.wait(UNKNOWN_WAIT)
    raise StuckError(f"couldn't get back to a village (saw: {', '.join(s.value for s in seen[-4:])})")
