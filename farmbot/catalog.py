"""Every picture the bot can use: what it is, where to find it, what it enables.

The GUI's Pictures page, ``farmbot templates`` and the start-up check all
read this list, so a template added to :class:`farmbot.game.T` should get
an entry here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from farmbot.game import T
from farmbot.vision import TemplateLibrary


@dataclass(frozen=True)
class Picture:
    name: str  # template name, e.g. "buttons/return_home"
    title: str
    where: str  # which screen shows it, and what to drag a box around
    feature: str  # what it enables
    required: bool = False
    folder: bool = False  # a directory of several pictures (one per variant)

    def file_for(self, root: Path, variant: str = "") -> Path:
        if self.folder:
            return root / self.name / f"{variant or 'picture'}.png"
        return root / f"{self.name}.png"


QUICK_EXIT = "Leaving battles without restarting the game"

PICTURES: list[Picture] = [
    # Attacking: without these the bot cannot get into or out of a battle.
    Picture(T.BATTLE, "Battle", "Home village, bottom left: the word \"Battle\".", "Attacking", True, folder=True),
    Picture(T.FIND_MATCH, "Find a Match", "Battle menu: the words \"Find a Match\".", "Attacking", True),
    Picture(T.ARMY_ATTACK, "Attack!", "Army screen after Find a Match: the word \"Attack!\".", "Attacking", True),
    Picture(T.NEXT, "Next", "Scouting a base, bottom right: the word \"Next\".", "Attacking", True),
    Picture(T.END_BATTLE, "End Battle", "During a battle, bottom left: the words \"End Battle\" "
            "(\"Surrender\" once units are out).", "Attacking", True, folder=True),
    # Recommended: without these two the bot closes the game to leave a battle.
    Picture(T.SURRENDER_OK, "Okay (end battle)", "After pressing End Battle: the green \"Okay\" in the question box.",
            QUICK_EXIT),
    Picture(T.RETURN_HOME, "Return Home", "Battle result screen: the words \"Return Home\" "
            "(or \"Claim Reward\" when a star bonus is due).", QUICK_EXIT, folder=True),
    Picture(T.EVENT_CHEST, "Event chest room", "After some battles during events: the top half of the arched window "
            "above the chest.", "Opening event chests"),
    Picture(T.PICK_REWARD, "Pick a Reward", "During event battles (at 30%, 50% and 100% damage): the words "
            "\"Pick a Reward!\" on the red banner above the reward cards.", "Opening event chests"),
    Picture(T.CONTINUE, "Continue", "Under an event chest's reward: the word \"Continue\".", "Opening event chests"),
    # Optional extras
    Picture(T.CC_CARD, "Clan Castle card", "Troop bar while scouting: the castle icon on the Clan Castle card.",
            "Leaving Clan Castle troops out"),
    Picture(T.DEAD_BASE, "Full collector (enemy)", "Scouting a base: a full gold or elixir collector.",
            "Dead-base search", folder=True),
    Picture(T.COLLECT_GOLD, "Full gold mine bubble", "Home village: the gold coin bubble over a full gold mine.",
            "Collecting resources"),
    Picture(T.COLLECT_ELIXIR, "Full elixir collector bubble", "Home village: the pink drop bubble over a full "
            "elixir collector.", "Collecting resources"),
    Picture(T.COLLECT_DARK, "Full dark elixir drill bubble", "Home village: the black drop bubble over a full "
            "dark elixir drill.", "Collecting resources"),
    Picture(T.LOOT_CART, "Loot cart label", "Home village, zoomed out: the word \"Collect\" over the loot cart.",
            "Collecting the loot cart"),
    Picture(T.LOOT_CART_COLLECT, "Loot cart Collect", "After tapping the loot cart: the word \"Collect\" on "
            "the button under it.", "Collecting the loot cart"),
    Picture(T.BB_HOME_BOAT, "Boat to the builder base", "Home village, bottom left: the boat.", "Builder base"),
    Picture(T.BB_BUILDER_BOAT, "Boat home", "Builder base, top right: the boat's hull.", "Builder base"),
    Picture(T.BB_BATTLE, "Builder Battle", "Builder base, bottom left: the crossed axes.", "Builder base"),
    Picture(T.BB_FIND_NOW, "Find Now!", "Builder Start Attack window: the words \"Find Now!\".", "Builder base"),
    Picture(T.BB_BATTLE_STARTS, "Battle starts in", "Builder battle, top: \"Battle starts in:\".", "Builder base"),
    Picture(T.BB_BATTLE_ENDS, "Battle ends in", "Builder battle, top: \"Battle ends in:\".", "Builder base"),
    Picture(T.BB_RETURN_HOME, "Return Home (builder)", "Builder battle result: the words \"Return Home\".",
            "Builder base"),
    Picture(T.BB_OKAY, "Okay (Star Bonus)", "Builder base Star Bonus window: the word \"Okay\".", "Builder base"),
    Picture(T.BB_CHANGE_HEROES, "Change Heroes", "Builder battle, after the hero card's swap button: the words "
            "\"Change Heroes\".", "Builder base"),
    Picture(T.SPEED, "1x speed (home)", "Home battle, last minute, right side: the green \"1x\" button.",
            "Battle speed"),
    Picture(T.BB_HERO_MACHINE, "Battle Machine card", "Builder battle, bottom left: the Battle Machine's hero card "
            "(below its health bar).", "Builder base"),
    Picture(T.BB_HERO_COPTER, "Battle Copter card", "Builder battle, bottom left: the Battle Copter's hero card "
            "(below its health bar).", "Builder base"),
    Picture(T.BB_SPEED, "1x speed", "Builder battle, right side: the green \"1x\" button.", "Builder base"),
    Picture(T.BB_COLLECT_GOLD, "Builder gold bubble", "Builder base: the gold bar bubble over a full gold mine.",
            "Builder base"),
    Picture(T.BB_COLLECT_ELIXIR, "Builder elixir bubble", "Builder base: the purple drop bubble over a full "
            "elixir collector (the elixir cart shows the same).", "Builder base"),
    Picture(T.BB_COLLECT_GEMS, "Builder gem bubble", "Builder base: the green gem bubble over a full gem mine.",
            "Builder base"),
    Picture(T.BB_CART_COLLECT, "Elixir Cart Collect", "Elixir Cart window: the word \"Collect\".", "Builder base"),
    Picture(T.BB_UPGRADE, "Upgrade (builder)", "Builder base, a selected wall: the word \"Upgrade\".",
            "Builder base walls"),
    Picture(T.BB_UPGRADE_TITLE, "Upgrade to level", "Builder base upgrade window: the words \"Upgrade to\".",
            "Builder base walls"),
    Picture(T.BUILD, "Build", "Home village, bottom right: the word \"Build\" on the hammer button.", "Upgrading walls"),
    Picture(T.WALL_SUGGESTION, "Wall in Buildings & Heroes", "Build list: the words \"Wall x\" (leave out the "
            "number).", "Upgrading walls"),
    Picture(T.UPGRADE, "Upgrade", "A selected wall: the word \"Upgrade\" under the hammer.", "Upgrading walls"),
    Picture(T.UPGRADE_CONFIRM, "Confirm (upgrade)", "Upgrade window: the word \"Confirm\" on the green button.",
            "Upgrading walls"),
    Picture(T.CLOSE, "Close (X)", "Any window: the red X in the top right corner.", "Closing windows"),
    Picture(T.OPEN_CHAT, "Open chat", "Home village, left edge: the chat tab arrow.", "Donating"),
    Picture(T.CLOSE_CHAT, "Close chat", "Open chat, right edge: the arrow that closes it.", "Donating"),
    Picture(T.NO_CLAN, "Join A Clan", "Chat panel when you're not in a clan: the words \"Join A Clan\".",
            "Requesting troops"),
    Picture(T.DONATE, "Donate", "Clan chat: the green \"Donate\" button on a request.", "Donating"),
    Picture(T.DONATE_ITEM_DIR, "Unit to donate", "Donate window: the unit's picture. Save it under the unit's name.",
            "Donating", folder=True),
    Picture(T.REQUEST, "Request", "Clan chat, bottom (right of Challenge): the castle with a plus.", "Requesting troops"),
    Picture(T.REQUEST_SEND, "Send request", "Request window: the word \"Send\".", "Requesting troops"),
    Picture(T.TRY_AGAIN, "Try Again", "Connection lost popup: \"Try Again\".", "Recovering from disconnects"),
    Picture(T.RELOAD, "Reload", "\"Another device is connected\" popup: \"Reload\".", "Recovering from disconnects"),
]
BY_NAME = {p.name: p for p in PICTURES}


def present(library: TemplateLibrary, picture: Picture) -> bool:
    return library.has(picture.name)


def slow_exit(library: TemplateLibrary) -> bool:
    """True when battles will be left by closing the game."""
    return any(not present(library, p) for p in PICTURES if p.feature == QUICK_EXIT)


def missing(library: TemplateLibrary, required_only: bool = False) -> list[Picture]:
    return [p for p in PICTURES if (p.required or not required_only) and not present(library, p)]


def lookalike_score(screen: np.ndarray, crop: np.ndarray) -> float:
    """The best match for a fresh crop anywhere on ``screen`` other than
    where it was cut from. Near or above the bot's threshold (0.85) means
    the picture could be mistaken for something else: crop it tighter."""
    h, w = crop.shape[:2]
    scores = cv2.matchTemplate(screen, crop, cv2.TM_CCOEFF_NORMED)
    _, _, _, (bx, by) = cv2.minMaxLoc(scores)
    scores[max(0, by - h // 2):by + h // 2 + 1, max(0, bx - w // 2):bx + w // 2 + 1] = -1
    return float(scores.max())


def save_crop(root: Path, picture: Picture, crop: np.ndarray, variant: str = "") -> Path:
    path = picture.file_for(root, variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), crop):
        raise OSError(f"could not write {path}")
    return path
