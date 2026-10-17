"""Clan Castle requests and troop donations.

These screens differ between game versions, so every step goes through a
template you crop from your own screenshots (see templates/README.md).
Missing templates switch the feature off with a log message instead of
guessing where to tap.
"""

from __future__ import annotations

import logging

from farmbot.config import ClanConfig
from farmbot.game import Game, T

log = logging.getLogger(__name__)


def _missing(game: Game, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not game.has(n)]


def in_clan(game: Game) -> bool | None:
    """Open the chat and look: the "Join A Clan" panel means not in a clan.
    ``None`` if the chat couldn't be opened. The chat is closed again."""
    if _missing(game, (T.OPEN_CHAT, T.CLOSE_CHAT, T.NO_CLAN)):
        return None
    game.refresh()
    if not game.click(T.OPEN_CHAT):
        return None
    if not game.wait_for(T.CLOSE_CHAT, timeout=4):
        return None
    game.wait(0.8)  # the panel slides in
    game.refresh()
    member = game.find(T.NO_CLAN) is None
    game.click(T.CLOSE_CHAT)
    game.wait(0.8)
    return member


# Where the Request button sits in the open chat, and its cooldown text
# ("10m") under the castle once a request is out (bot coordinates).
REQUEST_AREA = [380, 850, 120, 60]  # [x, y, w, h]
COOLDOWN_TEXT = [428, 880, 52, 26]
MAX_COOLDOWN_MINUTES = 20  # the game's longest; a bigger number must be seconds ("45s")


def _open_chat(game: Game) -> bool:
    game.refresh()
    if game.find(T.CLOSE_CHAT):
        return True  # already open
    if not game.click(T.OPEN_CHAT) or not game.wait_for(T.CLOSE_CHAT, timeout=4):
        return False
    game.wait(0.8)  # the panel slides in
    game.refresh()
    return True


def _close_chat(game: Game) -> None:
    game.refresh()
    if game.click(T.CLOSE_CHAT):
        game.wait(0.8)


def request_cooldown(game: Game) -> float | None:
    """Seconds until the Request button is free again, read from the timer
    on it ("10m", or "45s" near the end); ``None`` if it can't be read."""
    from farmbot import text

    font = game.vision.font(T.LOOT_FONT)
    digits = ""
    for _, mask in text.pieces(game._current(), game.region(COOLDOWN_TEXT)):
        char, score = font.classify(mask)
        if score < 0.45 or not char.isdigit():
            break  # the unit after the number ("m" or "s")
        digits += char
    value = int(digits) if digits else 0
    if not value:
        return None
    return float(value) * 60 if value <= MAX_COOLDOWN_MINUTES else float(value)


def request_troops(game: Game, cfg: ClanConfig) -> tuple[bool, float | None]:
    """Clan chat → Request (next to Challenge) → Send. Returns whether a
    request went out, and how many seconds until the next one can (from the
    button's cooldown timer; ``None`` if unknown). A Request button with a
    gem price is on cooldown and is never pressed."""
    missing = _missing(game, (T.OPEN_CHAT, T.CLOSE_CHAT, T.REQUEST, T.REQUEST_SEND))
    if missing:
        log.warning("Cannot request troops; missing pictures: %s", ", ".join(missing))
        return False, None
    if not _open_chat(game):
        log.info("Couldn't open the clan chat")
        return False, None
    if game.has(T.NO_CLAN) and game.find(T.NO_CLAN):
        log.info("Not in a clan: can't request troops")
        _close_chat(game)
        return False, None
    sent = False
    if game.click(T.REQUEST, region=game.region(REQUEST_AREA)):
        if game.click_when_visible(T.REQUEST_SEND, timeout=4):
            sent = True
            log.info("Requested Clan Castle troops")
            game.wait(2.0)
            game.refresh()
        else:
            log.info("The request window didn't show its Send button")
            game.refresh()
            game.click(T.CLOSE)
    wait = request_cooldown(game)
    if not sent:
        log.info("Request is on cooldown%s", f" for {wait / 60:.0f} more min" if wait else "")
    _close_chat(game)
    return sent, wait


def donate(game: Game, cfg: ClanConfig) -> int:
    """Fill open requests in clan chat with the default troop, siege machine
    and spell. Returns how many units were donated."""
    wanted = [f"{T.DONATE_ITEM_DIR}/{name}" for name in (cfg.donate_troop, cfg.donate_siege, cfg.donate_spell) if name]
    missing = _missing(game, (T.OPEN_CHAT, T.CLOSE_CHAT, T.DONATE, *wanted))
    if missing:
        log.warning("Cannot donate; missing templates: %s", ", ".join(missing))
        return 0
    game.refresh()
    if not game.click(T.OPEN_CHAT):
        return 0
    game.wait(1.0)
    donated = 0
    game.refresh()
    requests = sorted(game.find_all(T.DONATE), key=lambda m: m.y)
    for request in requests:
        game.tap_match(request)
        game.wait(1.0)
        for item in wanted:
            for _ in range(cfg.max_donations_per_request):
                game.refresh()
                if not game.click(item):
                    break  # none left, or the request is full
                donated += 1
                game.wait(0.3)
        game.refresh()
        game.click(T.CLOSE)  # close the donate window
        game.wait(0.5)
    game.refresh()
    game.click(T.CLOSE_CHAT)
    if donated:
        log.info("Donated %d unit(s) to %d request(s)", donated, len(requests))
    return donated
