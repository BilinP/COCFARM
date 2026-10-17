"""Is Clash of Clans installed on the emulator, and is an account logged in?

Installed: the package is on the device. Logged in: the game reaches a
village (its Battle button shows), which only happens once an account is
signed in; a fresh install stops at the login screens instead.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from farmbot.device import Device, DeviceError
from farmbot.game import Game, T

log = logging.getLogger(__name__)

LOGIN_TIMEOUT = 90.0  # seconds to wait for a village after starting the game


def installed(device: Device, package: str) -> bool:
    try:
        return f"package:{package}" in device.shell("pm", "list", "packages", package).split()
    except DeviceError:
        return False


def in_a_village(game: Game) -> bool:
    game.refresh()
    return any(game.has(n) and game.find(n) is not None for n in (T.BATTLE, T.BB_BATTLE))


def logged_in(game: Game, package: str, timeout: float = LOGIN_TIMEOUT,
              clock: Callable[[], float] = time.monotonic) -> bool:
    """Start the game if needed and wait for a village. False if none shows
    within ``timeout`` (not logged in, or a popup/update in the way)."""
    if game.device.foreground_package() != package:
        game.device.start_app(package)
    deadline = clock() + timeout
    while True:
        if in_a_village(game):
            return True
        if clock() >= deadline:
            return False
        game.wait(3)
