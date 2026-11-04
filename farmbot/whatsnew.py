"""What's new in each version, in a player's words: shown once after an
update, so a returning player learns what changed without reading the
release page.

The version last shown is kept in ``update.json`` (it survives updates).
"""

from __future__ import annotations

from pathlib import Path

from farmbot import __version__
from farmbot.update import STATE, _load, _save, parse_version

# Newest first. Each change is (kind, headline, detail): the kind is one of
# KINDS, the headline says what the player gets in a few words, and the
# detail (may be empty) explains it in one plain sentence, not how it's done.
KINDS = ["New", "Improved", "Fixed"]

NOTES: dict[str, list[tuple[str, str, str]]] = {
    "1.0.0": [
        ("New", "Farm several accounts at once (beta)",
         "Switch on a farm for each emulator instance on the Farms page (LDPlayer, MuMu Player or BlueStacks, "
         "mixed as you like) and start them all together. Each farm keeps to the account signed in there."),
        ("New", "Make and delete emulator instances",
         "New instance copies one on LDPlayer or MuMu Player, and the bin on a row deletes one, after asking."),
        ("New", "Each farm in the main window or its own",
         "View shows a farm in the main window while the others keep farming; the small button opens it in a "
         "window of its own."),
        ("New", "Stops on Clash of Clans' maintenance break",
         "When the game is down for maintenance, the bot stops and says why instead of retrying."),
        ("Fixed", "Google Play's \"Rate Clash of Clans\" box is closed",
         "It could sit over the game after a battle and stop the bot; COCFARM now answers Never."),
        ("Fixed", "Buttons found on every farm",
         "A farm added from the Farms page could look for buttons at the wrong size and stop with \"find-a-match "
         "button did not appear\"."),
        ("Fixed", "MuMu Player stays connected",
         "A dropped MuMu Player connection no longer loops on \"Lost the connection; reconnecting\"."),
        ("Fixed", "LDPlayer 14 starts with LDPlayer 9 installed",
         "COCFARM no longer mistakes LDPlayer 14 for LDPlayer 9 and stops with \"LDPlayer 9 did not close\"."),
        ("Improved", "Faster tabs and start-up",
         "Switching to the Farms page and opening COCFARM with several farms no longer lag."),
        ("Improved", "Clearer wall messages",
         "When a wall costs more than you have, the log says so instead of \"couldn't read the price\"."),
    ],
    "0.9.9": [
        ("Fixed", "LDPlayer connects every time",
         "LDPlayer's own background helper could take the connection away, so the bot stopped with \"did not "
         "answer over ADB\"."),
        ("New", "A stuck emulator is restarted",
         "If the emulator doesn't answer, COCFARM closes it and starts it again, up to three times, before "
         "stopping."),
        ("Improved", "The game is restarted after a failed round",
         "Something over the game (like an emulator's ad) is cleared by closing the game and opening it again. "
         "After three failed rounds in a row the bot stops and tells you why."),
        ("Fixed", "Messages fit in the window",
         "A long message, like a stop with its reason, no longer runs off the edge of the COCFARM window."),
        ("Improved", "A tidier log", "Technical details only the log file needs no longer show in the window."),
    ],
    "0.9.8": [
        ("Fixed", "Every hero goes out again",
         "The trees behind the troop bar could look like a hero's health bar, so a hero (often the Royal Champion) "
         "was taken as already placed and left out."),
        ("Fixed", "Fewer bases skipped as unreadable",
         "Loot numbers over snow, trees or a bright wall edge are read instead of skipped, and no longer misread "
         "(a gold of 1, or dark elixir taken as none)."),
        ("Fixed", "Requested spells are donated",
         "When a request names the units it wants, the spells it asks for are given along with the troops."),
        ("Fixed", "Donations by Clan Castle level on LDPlayer",
         "The requester's Clan Castle room (like 0/50) is read again, so your armies by Clan Castle level are used."),
        ("Improved", "Commas in amounts as you type", "10000000 shows as 10,000,000, so an extra zero stands out."),
        ("Improved", "Warning for loot minimums no base meets",
         "A minimum like 10,000,000 gold (an extra zero, say) is flagged in Settings, on the Dashboard and in the "
         "log, since it makes the bot skip every base."),
        ("Fixed", "Searching no longer stops with Next on screen",
         "The bot could give up waiting for the Next button while it was showing."),
        ("Fixed", "Battle is tapped again if the menu doesn't open",
         "After an attack, a tap on Battle that the game missed no longer ends the round."),
    ],
    "0.9.7": [
        ("Improved", "Troops are placed exactly",
         "Each card is tapped once per troop left, so spare taps no longer drop the next troop or a hero."),
        ("Fixed", "Troop counts are read correctly", "Counts like x68 and x7 on the cards were sometimes misread."),
        ("Fixed", "Heroes in big armies",
         "When the troop bar scrolls, a hero already out isn't tapped again (which used its ability early), "
         "and abilities still fire for heroes scrolled out of view."),
        ("Fixed", "Pop-up windows",
         "What's new and Accounts no longer stop you moving COCFARM, and What's new closes when you click outside it."),
    ],
    "0.9.6": [
        ("New", "Donations by Clan Castle level",
         "Each request gets troops that fit the requester's Clan Castle. Choose them in Home village → Clan → "
         "Armies by Clan Castle level."),
        ("Improved", "Donations fill what's on screen",
         "Only the requests you can see in chat are filled, and Fill the rest uses up the space left."),
        ("Improved", "The Dashboard explains itself", "It says what Start will do, and why and when the bot stopped."),
        ("Improved", "History shows the newest attacks first", ""),
        ("Improved", "Some settings moved",
         "Phone alerts are under Advanced → Alerts, and rarely used attack settings are folded away on "
         "Home village → Attacking."),
        ("Fixed", "No upgrades during Goblin events",
         "Nothing is upgraded while the Goblin Builder or Goblin Researcher is in the village."),
        ("Fixed", "LDPlayer sign-in and ports",
         "The sign-in screen is noticed, and another emulator using COCFARM's port is closed for you."),
    ],
    "0.9.5": [
        ("New", "Clan Capital raids (beta)", "Raid on its own Raid Weekend tab."),
        ("Fixed", "Zooming out", "The bot no longer zooms out when the view already is."),
    ],
    "0.9.4": [
        ("New", "Super troop boosts", "Boost super troops with dark elixir. Never with a Super Potion or gems."),
        ("New", "Gravestones are cleared", ""),
        ("Improved", "Loot minimums", "Minimums left at their defaults are set from your storage sizes."),
        ("Improved", "Discord stop messages", "They sum up the run."),
    ],
}


def unseen(state_path: Path | None = None, current: str = __version__,
           first_run: bool = False) -> list[tuple[str, list[tuple[str, str, str]]]]:
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


def latest(current: str = __version__) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """The newest notes up to this version (Advanced → Updates → What's new)."""
    now = parse_version(current)
    return [(version, lines) for version, lines in NOTES.items() if parse_version(version) <= now][:1]
