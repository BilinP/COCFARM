"""Looking around the game, as a person does between battles ("Look around
the game", farmbot/human.py): the clan chat opened and read a moment, the
Build list opened and scrolled, or a drag around the village. Nothing in
them is ever tapped: each is opened by its own button, looked at, and
closed again."""

from __future__ import annotations

import logging

from farmbot import human
from farmbot.game import Game, T
from farmbot.tasks import clan
from farmbot.tasks.build_list import LIST_STEP, LIST_SWIPE_FROM_BOTTOM, LIST_SWIPE_X, SLOW_SWIPE_MS
from farmbot.tasks.village import close_all_windows

log = logging.getLogger(__name__)

WANDER_DRAG = (90, 220)  # px a drag around the village moves the view, each way
WANDER_DRAGS = (1, 3)
WANDER_Y = 0.45  # the drags go through the middle of the village


def _read(game: Game) -> None:
    game.wait(game.rng.uniform(*human.BROWSE_READ), exact=True)


def chat(game: Game) -> bool:
    """The clan chat: opened, looked at, closed."""
    if not (game.has(T.OPEN_CHAT) and game.has(T.CLOSE_CHAT)) or not clan._open_chat(game):
        return False
    _read(game)
    clan.close_chat(game)
    return True


def build_list(game: Game) -> bool:
    """The Build list: opened, scrolled a little, closed (which also drops
    anything selected)."""
    if not (game.has(T.BUILD) and game.has(T.CLOSE)):
        return False
    game.refresh()
    if not game.click(T.BUILD) or not game.wait_for(T.CLOSE, timeout=4):
        return False
    game.wait(1.0)
    w, h = game.size
    x, y = w - LIST_SWIPE_X, h - LIST_SWIPE_FROM_BOTTOM
    game.swipe(x, y, x, y - LIST_STEP, duration_ms=SLOW_SWIPE_MS)
    _read(game)
    close_all_windows(game)
    return True


def wander(game: Game) -> bool:
    """A drag or a few around the village, then the view zoomed out again.
    Nothing may be selected (a finger on a selected building moves it): the
    Build list is opened and closed first, which drops whatever is, even a
    building being upgraded (it shows no Upgrade button to tell by)."""
    if getattr(game.device, "zoom_out", None) is None:
        return False  # (no way to put the view back)
    game.deselect(always=True)
    w, h = game.size
    cx, cy = w // 2, round(h * WANDER_Y)
    for _ in range(game.rng.randint(*WANDER_DRAGS)):
        dx = game.rng.randint(*WANDER_DRAG) * game.rng.choice((-1, 1))
        dy = game.rng.randint(*WANDER_DRAG) * game.rng.choice((-1, 1)) // 2
        game._drag_view(cx, cy, dx, dy)
        game.wait(game.rng.uniform(0.8, 2.5), exact=True)
    game.zoom_out(force=True)
    return True


KINDS = (("the clan chat", chat), ("the Build list", build_list), ("the village", wander))


def look_around(game: Game) -> str | None:
    """One of them, picked at random; the next if that one can't be done
    here. What was looked at, or None."""
    for name, look in human.order(game.rng, list(KINDS)):
        if look(game):
            return name
    return None
