"""What's new in each version, in a player's words: shown once after an
update, so a returning player learns what changed without reading the
release page.

The version last shown is kept in ``update.json`` (it survives updates).
"""

from __future__ import annotations

from pathlib import Path

from farmbot import __version__
from farmbot.update import STATE, _load, _save, parse_version

# Newest first. A line says what the player gets, not how it's done.
NOTES: dict[str, list[str]] = {
    "0.9.7": [
        "Troops go out exactly: each card gets as many taps as it has units left, so no spare taps drop the "
        "next troop or a hero once a card is empty.",
        "Troop counts on the cards are read right (x68 no longer reads as 50, x7 as 2).",
        "Big armies that fill more than one screen of the troop bar: a hero already placed isn't tapped again "
        "(which fired its ability early), and hero abilities are used even when the heroes scrolled out of sight.",
        "Windows like What's new and Accounts no longer block moving COCFARM, and What's new closes when you click "
        "outside it.",
    ],
    "0.9.6": [
        "Donations: each request gets an army that suits the requester's Clan Castle level (read from the "
        "request), with Super Yeti, Super Wizard and Super Archer first and Rage Spells. Set your own on "
        "Home village → Clan → Armies by Clan Castle level.",
        "Donations: only the requests on the first screen of chat are filled, and Fill the rest tops up the room left.",
        "Upgrades are never started while the Goblin Builder or Goblin Researcher shows.",
        "The Dashboard says what Start will do, and why the bot stopped (and when).",
        "History lists the newest attacks first.",
        "Alerts on your phone moved to Advanced → Alerts; Advanced shows only your emulator's settings.",
        "Rarely changed attack settings (speeds, ability timing, when exactly to leave) are folded under "
        "their own rows on Home village → Attacking.",
        "LDPlayer: sign-in is noticed, and an emulator holding COCFARM's port is closed by itself.",
    ],
    "0.9.5": [
        "Clan Capital raids (beta), on their own Raid Weekend tab.",
        "Zooming out knows when the view is already zoomed out.",
    ],
    "0.9.4": [
        "Super troops can be boosted with dark elixir (never a Super Potion or gems).",
        "Gravestones are cleared, and loot minimums left at the defaults are set from your storages.",
        "Discord stop messages sum up the run.",
    ],
}


def unseen(state_path: Path | None = None, current: str = __version__, first_run: bool = False) -> list[tuple[str, list[str]]]:
    """The notes of the versions since the one last shown, newest first, and
    remember this one as shown. A new player (``first_run``) gets none:
    everything is new to them."""
    state_path = state_path or STATE
    state = _load(state_path)
    seen = state.get("seen")
    if seen == current:
        return []
    state["seen"] = current
    _save(state_path, state)
    if first_run:
        return []
    now = parse_version(current)
    if not seen:  # updated from before these notes: only this version's
        return [(version, lines) for version, lines in NOTES.items() if parse_version(version) == now]
    since = parse_version(seen)
    return [(version, lines) for version, lines in NOTES.items() if since < parse_version(version) <= now]


def latest(current: str = __version__) -> list[tuple[str, list[str]]]:
    """The newest notes up to this version (Advanced → Updates → What's new)."""
    now = parse_version(current)
    return [(version, lines) for version, lines in NOTES.items() if parse_version(version) <= now][:1]
