"""Plain words for the window: what the bot is doing, and what went wrong.

The bot reports short status lines ("Paused: outside active hours (resumes
17:00)") and raises errors written for developers ("could not connect to
127.0.0.1:5555: ..."). This turns both into something a player can act on:
a state, a headline, one line of detail, and for problems a next step.
Kept free of Qt so it can be tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# state -> (word shown in the big status, symbol: colour is never the only signal)
STATES = {
    "ready": ("Ready", "●"),
    "running": ("Running", "▶"),
    "attacking": ("Attacking", "⚔"),
    "paused": ("Paused", "⏸"),
    "waiting": ("Waiting", "⏳"),
    "stopped": ("Stopped", "■"),
    "problem": ("Problem", "⚠"),
}


@dataclass(frozen=True)
class Status:
    state: str  # a key of STATES
    headline: str  # what it's doing, in a few words
    detail: str = ""  # why, or what happens next

    @property
    def word(self) -> str:
        return STATES[self.state][0]

    @property
    def symbol(self) -> str:
        return STATES[self.state][1]


RUNNING_LINES = {  # status line -> (headline, what comes next)
    "Searching for a base": ("Looking for a base worth attacking", "Skips bases below your loot minimums."),
    "Looking after the village": ("Collecting loot and looking after the village", "Next: look for a base to attack."),
    "Upgrading walls": ("Upgrading walls with spare loot", ""),
    "Upgrading builder base walls": ("Upgrading builder base walls with spare loot", ""),
    "Donating": ("Donating troops to the clan", ""),
    "Requesting troops": ("Asking the clan for Clan Castle troops", ""),
    "Sailing to the builder base": ("Sailing to the builder base", "Next: a builder battle."),
    "Sailing to the home village": ("Sailing back to the home village", ""),
    "Waiting": ("Short pause before the next round", ""),
    "Resuming": ("Carrying on", ""),
    "Connecting to the device…": ("Connecting to the emulator", "Opening it first if it isn't open."),
}


def describe_status(text: str, now: datetime | None = None) -> Status:
    """A bot status line as a :class:`Status`."""
    now = now or datetime.now()
    if text.startswith("Attacking (builder base)"):
        stage = re.search(r"base (\d)", text)
        return Status("attacking", "Builder battle" + (f": base {stage.group(1)} of 2" if stage else ""),
                      "Hero first, then the troops.")
    if text.startswith("Attacking"):
        return Status("attacking", "Attacking a base", "Deploying the army; it leaves once the loot stops coming.")
    if text.startswith("Paused by you"):
        return Status("paused", "Paused by you", "Press Resume to carry on, or Stop now to end.")
    if text.startswith("Pausing"):
        return Status("paused", "Pausing after this step", "It finishes what it's doing first. Stop now ends at once.")
    if text.startswith("Stopping"):
        return Status("paused", "Stopping now", "")
    if m := re.match(r"Paused: outside active hours(?: \(resumes (\d\d:\d\d)\))?", text):
        until = m.group(1)
        return Status("waiting", f"Waiting until {until}" if until else "Outside your playing hours",
                      "Outside the hours you picked on When to play.")
    if m := re.match(r"Paused: daily limit of (\d+) attacks reached", text):
        return Status("waiting", "Waiting until midnight",
                      f"Today's limit of {m.group(1)} attacks is reached (When to play).")
    if m := re.match(r"Taking a break: (\d+) min", text):
        back = now + timedelta(minutes=int(m.group(1)))
        return Status("waiting", f"On a break until {back:%H:%M}", "Resting now and then, like a person would.")
    if m := re.match(r"Saving up: ([\d,]+) gold is below ([\d,]+)", text):
        return Status("waiting", "Saving up gold",
                      f"You have {m.group(1)} gold, below your {m.group(2)} limit for searching. "
                      "It keeps collecting and looks again soon.")
    if m := re.match(r"Storage full: switching to the (builder base|home village)", text):
        other = "home village" if m.group(1) == "builder base" else "builder base"
        return Status("running", f"Storage full: on to the {m.group(1)}",
                      f"The {other} storages you picked are full, so it plays the {m.group(1)} now.")
    if text.startswith("Stopped: storage full in both villages"):
        return Status("stopped", "Storage full in both villages", "Both villages' storages you picked are full, "
                      "so it stopped. Spend some loot, then press Start.")
    if m := re.match(r"Stopped: storage full \((.+)\)", text):
        return Status("stopped", "Storage full", f"Your {m.group(1)} storage is full, so it stopped. "
                      "Spend some loot, then press Start.")
    if text in ("Stopped", "Stopped. Press Start to go again."):
        return Status("stopped", "Stopped", "Press Start to go again.")
    if m := re.match(r"Attack (\d+) done", text):
        return Status("running", f"Attack {m.group(1)} done", "Getting ready for the next one.")
    if text.startswith("Restarting the emulator"):
        return Status("running", "Restarting the emulator", "The game kept failing, so it's starting it fresh.")
    if text.startswith("Starting"):
        return Status("running", "Starting", "Connecting to the emulator and opening the game.")
    headline, detail = RUNNING_LINES.get(text, (text, ""))
    return Status("running", headline, detail)


@dataclass(frozen=True)
class Problem:
    title: str  # what happened
    why: str  # why it matters
    action: str  # what the fix button does: setup | retry | autostart | pictures | logs
    action_text: str  # the button's label
    details: str  # the original message, for "Technical details"


# (pattern, title, why, action, button). {emulator} becomes the emulator's name.
RULES = [
    (r"instance .* not found|has no instance",
     "The emulator instance picked in Setup doesn't exist",
     "COCFARM doesn't know which emulator window to play in.", "setup", "Open Setup"),
    (r"(BlueStacks|LDPlayer)[^.]* (was )?not found|not installed",
     "COCFARM can't find {emulator}",
     "The game runs inside the emulator, so nothing can be played. Install it, or pick the other one.",
     "setup", "Open Setup"),
    (r"is not running",
     "{emulator} isn't open",
     "The bot plays the game inside it.", "autostart", "Start it automatically"),
    (r"adb not found",
     "A part COCFARM needs to talk to the emulator is missing",
     "Without it the bot can't control the game.", "setup", "Open Setup"),
    (r"timed out",
     "{emulator} took too long to answer",
     "It may be restarting after COCFARM set it up, or your PC is busy. Wait a moment, then try again.",
     "retry", "Try again"),
    (r"did not open|did not close",
     "{emulator} took too long to open",
     "Your PC may be busy. It usually works on a second try.", "retry", "Try again"),
    (r"did not accept|did not answer|could not connect|offline|refused|adb failed|no devices",
     "Can't connect to {emulator}",
     "COCFARM controls the game through the emulator. Make sure it's open and the game has loaded.",
     "retry", "Try again"),
    (r"screencap|could not capture|pixels|has no size|minimised",
     "Can't see the emulator's screen",
     "The bot needs to see the game. Make sure the emulator window isn't minimised.", "retry", "Try again"),
    (r"couldn't get back to a village",
     "The game is stuck on a screen COCFARM doesn't recognise",
     "It needs the village showing to carry on. Close whatever is open in the game, then try again.",
     "retry", "Try again"),
    (r"battle button|find-a-match|next button|search screen|could not read available loot",
     "COCFARM lost its way in the game",
     "Usually a popup or an update screen is in the way. Check the game, then try again.", "retry", "Try again"),
    (r"pictures? missing|no template named|cannot read template",
     "A button picture is missing",
     "The bot finds buttons by their pictures; without this one it can't go on.", "pictures", "Open Pictures"),
]


def explain(message: str, emulator: str = "the emulator") -> Problem:
    """What went wrong, in plain words, with a next step."""
    for pattern, title, why, action, button in RULES:
        if re.search(pattern, message, re.IGNORECASE):
            return Problem(title.format(emulator=emulator), why, action, button, message)
    return Problem("Something went wrong", "The bot stopped so it doesn't do anything unexpected. "
                   "The technical details below can help if it keeps happening.", "logs", "Open the log folder",
                   message)
