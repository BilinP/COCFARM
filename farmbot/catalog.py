"""Every picture the bot can use: what it is, where to find it, what it enables.

The window's Pictures tool (Advanced → Pictures), ``farmbot templates`` and the start-up check all
read this list, so a template added to :class:`farmbot.game.T` should get
an entry here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from farmbot import imagefile
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
BAT_SPELLS = "Casting only Bat Spells"
WARDEN_SIDE = "Putting the Grand Warden with the most troops"
SUPER_TROOPS = "Activating super troops"
CAPITAL = "Clan Capital raids"

PICTURES: list[Picture] = [
    # Attacking: without these the bot cannot get into or out of a battle.
    Picture(T.BATTLE, "Battle", "Home village, bottom left: the word \"Battle\".", "Attacking", True, folder=True),
    Picture(T.FIND_MATCH, "Find a Match", "Battle menu: the words \"Find a Match\".", "Attacking", True),
    Picture(T.ARMY_ATTACK, "Attack!", "Army screen after Find a Match: the word \"Attack!\".", "Attacking", True),
    Picture(T.NEXT, "Next", "Scouting a base, bottom right: the word \"Next\".", "Attacking", True),
    Picture(T.BAT_SPELL_CARD, "Bat Spell card", "Troop bar during a battle: the bats and bottle on the "
            "Bat Spell card (not its count or level).", BAT_SPELLS),
    Picture(T.WARDEN_CARD, "Grand Warden card", "Troop bar during a battle: the Grand Warden's face on his "
            "card (not his level).", WARDEN_SIDE),
    Picture(T.END_BATTLE, "End Battle", "During a battle, bottom left: the words \"End Battle\" "
            "(\"Surrender\" once units are out).", "Attacking", True, folder=True),
    # Recommended: without these two the bot closes the game to leave a battle.
    Picture(T.SURRENDER_OK, "Okay (end battle)", "After pressing End Battle: the green \"Okay\" in the question box.",
            QUICK_EXIT),
    Picture(T.RETURN_HOME, "Return Home", "Battle result screen: the words \"Return Home\" "
            "(or \"Claim Reward\" when a star bonus is due).", QUICK_EXIT, folder=True),
    Picture(T.EVENT_CHEST, "Event chest room", "After some battles during events: the top half of the arched window "
            "above the chest.", "Opening event chests"),
    Picture(T.REMOVE_ADS, "Remove Ads", "An ad shown in the game (\"Welcome Back Chief!\" with another game's "
            "picture): the \"Remove Ads\" badge at its top right, without the X beside it.",
            "Closing ads with their X instead of Okay, which opens the ad"),
    Picture(T.PICK_REWARD, "Pick a Reward", "During event battles (at 30%, 50% and 100% damage): the words "
            "\"Pick a Reward!\" on the red banner above the reward cards.", "Opening event chests"),
    Picture(T.CONTINUE, "Continue", "Under an event chest's reward: the word \"Continue\".", "Opening event chests"),
    # Optional extras
    Picture(T.COLLECT_GOLD, "Full gold mine bubble", "Home village: the gold coin bubble over a full gold mine.",
            "Collecting resources"),
    Picture(T.COLLECT_ELIXIR, "Full elixir collector bubble", "Home village: the pink drop bubble over a full "
            "elixir collector.", "Collecting resources"),
    Picture(T.COLLECT_DARK, "Full dark elixir drill bubble", "Home village: the black drop bubble over a full "
            "dark elixir drill.", "Collecting resources"),
    Picture(T.LOOT_CART, "Loot cart label", "Home village, zoomed out: the word \"Collect\" over the loot cart.",
            "Collecting the loot cart"),
    Picture(T.LOOT_CART_COLLECT, "Loot cart Collect", "After tapping the loot cart: the word \"Collect\" on "
            "the button under it.", "Collecting the loot cart", folder=True),
    Picture(T.GRAVESTONE, "Gravestone", "Home village, zoomed out, after someone attacked you: one grey "
            "gravestone, tight around the stone.", "Clearing gravestones"),
    Picture(T.SUPER_ACTIVATE, "Activate (super troop)", "Army screen after Find a Match, with a super troop that "
            "isn't boosted: the word \"Activate\" under its card.", SUPER_TROOPS),
    Picture(T.SUPER_BOOST_TITLE, "Boost? (super troop)", "The window Activate opens: the title \"Boost?\".",
            SUPER_TROOPS),
    Picture(T.SUPER_BOOST_DARK, "Boost with dark elixir", "The same window: the dark elixir drop on the green "
            "button (not the Super Potion).", SUPER_TROOPS),
    Picture(T.BB_HOME_BOAT, "Boat to the builder base", "Home village, bottom left: the boat.", "Builder base"),
    Picture(T.BB_BUILDER_BOAT, "Boat home", "Builder base, top right: the boat's hull.", "Builder base"),
    Picture(T.BB_BATTLE, "Builder Battle", "Builder base, bottom left: the crossed axes.", "Builder base"),
    Picture(T.BB_FIND_NOW, "Find Now!", "Builder Start Attack window: the words \"Find Now!\".", "Builder base"),
    Picture(T.CONFIRM, "Confirm", "A notice such as \"The Event has ended\": the word \"Confirm\" on its green "
            "button.", "Closing notices"),
    Picture(T.GEM, "Gem", "The home village's resource bar: the green gem icon beside your gem count.",
            "Closing notices"),
    Picture(T.STORAGE_MAX, "Max:", "Tap your gold at the top right: the word \"Max:\" in the box that opens.",
            "Storage size"),
    Picture(T.LAYOUT_OBSTRUCTED, "Defensive Layout obstructed", "The window that sometimes shows after asking "
            "for a battle: its title \"Defensive Layout obstructed\".", "Attacking with an obstructed layout"),
    Picture(T.OBSTRUCTED_ATTACK, "Attack (obstructed layout)", "The same window: the word \"Attack\" on its "
            "green button.", "Attacking with an obstructed layout"),
    Picture(T.UNABLE_TO_ATTACK, "Unable to attack", "The white screen after Find a Match when the last "
            "battle was left without surrendering: the words \"Unable to attack\".", "Searching"),
    Picture(T.LOCKOUT_RETURN_HOME, "Return Home (Unable to attack)", "The same white screen: the words "
            "\"Return Home\" on the portrait button at its bottom left.", "Leaving the Unable to attack screen"),
    Picture(T.ATTACK_COOLDOWN, "Attack available in", "Battle → the battle menu while a battle left midway "
            "still runs: the words \"Attack available in:\" on the grey box where the Battle card's Find a "
            "Match would be.", "Waiting out a battle left midway"),
    Picture(T.SETTINGS_TAB, "Settings tab", "Tap your level badge at the top left: the \"Settings\" tab of the "
            "window that opens (picked, or not).", "Reading the village data", folder=True),
    Picture(T.MORE_SETTINGS, "More Settings", "That Settings tab: the words \"More Settings\" on its button.",
            "Reading the village data"),
    Picture(T.DATA_EXPORT, "Export Village data", "More Settings, at the bottom: the words \"Export Village data "
            "in JSON format\".", "Reading the village data"),
    Picture(T.EXPORT_COPY, "Copy (Data Export)", "The same row: the word \"Copy\" on its green button.",
            "Reading the village data"),
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
    Picture(T.BB_CLOCK_READY, "Clock Tower ready", "Builder base: the clock bubble over the Clock Tower.",
            "Builder base"),
    Picture(T.BB_CLOCK_FREE_BOOST, "Free Boost (Clock Tower)", "The selected Clock Tower: the words \"Free "
            "Boost!\" (leave out the time above them).", "Builder base"),
    Picture(T.BB_FREE_BOOST_WINDOW, "Free Boost window", "The window it opens: its title, \"Free Boost!\".",
            "Builder base"),
    Picture(T.BB_FREE_BOOST_CONFIRM, "Free Boost confirm", "That window: the word \"Boost\" on the green button.",
            "Builder base"),
    Picture(T.BB_UPGRADE, "Upgrade (builder)", "Builder base, a selected wall: the word \"Upgrade\".",
            "Builder base walls"),
    Picture(T.BB_UPGRADE_TITLE, "Upgrade to level", "Builder base upgrade window: the words \"Upgrade to\".",
            "Builder base walls"),
    Picture(T.BUILD, "Build", "Home village, bottom right: the word \"Build\" on the hammer button.", "Upgrading walls"),
    Picture(T.GOBLIN, "Goblin Builder", "Home village, over the builder counter above Build: the goblin's face "
            "(shown when only the Goblin Builder, for gems, is left).", "Not paying gems for the Goblin Builder or Researcher"),
    Picture(T.WALL_SUGGESTION, "Wall in Buildings & Heroes", "Build list: the words \"Wall x\" (leave out the "
            "number).", "Upgrading walls"),
    Picture(T.TOWN_HALL_ROW, "Town Hall in Buildings & Heroes", "Build list: the words \"Town Hall\" on its "
            "row (the name only).", "Leaving the Town Hall to you (when that's chosen)"),
    Picture(T.UPGRADE_LOCKED, "Upgrade Locked", "The window a Town Hall that can't be upgraded yet opens: its "
            "title, \"Upgrade Locked\".", "Upgrading the Town Hall"),
    Picture(T.FREE_ROW, "FREE in Buildings & Heroes", "Build list: the word \"FREE\" on a new wall's row.",
            "Building new walls"),
    Picture(T.PLACE_OK, "Build here (tick)", "A new building being put down: the white tick inside its green "
            "button (not the button's edge).", "Building new buildings"),
    Picture(T.PLACE_CANCEL, "Don't build (cross)", "A new building being put down: the white cross inside its "
            "red button (not the button's edge).", "Building new buildings"),
    Picture(T.UPGRADE, "Upgrade", "A selected wall: the word \"Upgrade\" under the hammer.", "Upgrading walls"),
    Picture(T.UPGRADE_CONFIRM, "Confirm (upgrade)", "Upgrade window: the word \"Confirm\" on the green button.",
            "Upgrading walls"),
    Picture(T.UPGRADE_MORE, "Upgrade More", "A selected wall: the words \"Upgrade More\" (both lines).",
            "Upgrading walls"),
    Picture(T.ADD_WALL, "Add Wall", "After Upgrade More: the words \"Add Wall\" under a button (the same on "
            "both, +10 and +1, at every wall level).", "Upgrading walls"),
    Picture(T.LIST_FILTER, "List filter", "Build list, left of its title: the slider button.", "Upgrading walls"),
    Picture(T.SORT_COST_MIN, "Cost Min", "Build list filter menu: the word \"Min\" of Cost Min.", "Upgrading walls"),
    Picture(T.SORT_COST_MAX, "Cost Max", "Build list filter menu: the word \"Max\" of Cost Max.", "Upgrading walls"),
    Picture(T.FILTER_GOLD, "Filter: gold", "Build list filter menu: the gold coin.", "Upgrades"),
    Picture(T.FILTER_ELIXIR, "Filter: elixir", "Build list filter menu: the elixir drop.", "Upgrades"),
    Picture(T.FILTER_DARK, "Filter: dark elixir", "Build list filter menu: the dark elixir drop.", "Upgrades"),
    Picture(T.FILTER_ARMY, "Filter: Army", "Build list filter menu: the word \"Army\".", "Upgrades"),
    Picture(T.FILTER_DEFENSE, "Filter: Defense", "Build list filter menu: the word \"Defense\".", "Upgrades"),
    Picture(T.FILTER_HERO, "Filter: Hero", "Build list filter menu: the word \"Hero\".", "Upgrades"),
    Picture(T.FILTER_RESOURCE, "Filter: Resource", "Build list filter menu: the word \"Resource\".", "Upgrades"),
    Picture(T.FILTER_NEW, "Filter: New", "Build list filter menu: the word \"New\".", "Upgrades"),
    Picture(T.FILTER_PERMANENT, "Filter: Permanent", "Build list filter menu: the word \"Permanent\".", "Upgrades"),
    Picture(T.FILTER_TROOPS, "Filter: Troops", "Army Upgrades filter menu: the word \"Troops\".", "Upgrades"),
    Picture(T.FILTER_SPELLS, "Filter: Spells", "Army Upgrades filter menu: the word \"Spells\".", "Upgrades"),
    Picture(T.FILTER_SIEGE, "Filter: Siege", "Army Upgrades filter menu: the word \"Siege\".", "Upgrades"),
    Picture(T.COST_GOLD, "Price: gold", "Build list, right of a row's price: the small gold coin.", "Upgrades"),
    Picture(T.COST_ELIXIR, "Price: elixir", "Build list, right of a row's price: the small elixir drop.", "Upgrades"),
    Picture(T.COST_DARK, "Price: dark elixir", "Build list, right of a row's price: the small dark elixir drop.",
            "Upgrades"),
    Picture(T.BB_FILTER_GOLD, "Filter: builder gold", "Builder base Build list filter menu: the gold bar.",
            "Builder base upgrades"),
    Picture(T.BB_FILTER_ELIXIR, "Filter: builder elixir", "Builder base Build list filter menu: the elixir drop.",
            "Builder base upgrades"),
    Picture(T.BB_COST_GOLD, "Price: builder gold", "Builder base Build list, right of a row's price: the small "
            "gold bar.", "Builder base upgrades"),
    Picture(T.BB_COST_ELIXIR, "Price: builder elixir", "Builder base Build list, right of a row's price: the small "
            "elixir drop.", "Builder base upgrades"),
    Picture(T.CLOSE, "Close (X)", "Any window: the red X in the top right corner.", "Closing windows"),
    Picture(T.OPEN_CHAT, "Open chat", "Home village, left edge: the chat tab arrow.", "Donating"),
    Picture(T.CLOSE_CHAT, "Close chat", "Open chat, right edge: the arrow that closes it.", "Donating"),
    Picture(T.NO_CLAN, "Join A Clan", "Chat panel when you're not in a clan: the words \"Join A Clan\".",
            "Requesting troops"),
    Picture(T.DONATE, "Donate", "Clan chat: the green \"Donate\" button on a request.", "Donating"),
    Picture(T.DONATE_WINDOW, "Donate window", "Clan chat → Donate: the words \"Donation Resource\" at the top of "
            "the window.", "Donating"),
    Picture(T.DONATE_ITEM_DIR, "Unit to donate", "Donate window: the top half of the unit's card (its picture, "
            "without the level or price). Save it under the unit's name, e.g. super_yeti.",
            "Donating", folder=True),
    Picture(T.REQUEST, "Request", "Clan chat, bottom (right of Challenge): the castle with a plus.", "Requesting troops"),
    Picture(T.REQUEST_SEND, "Send request", "Request window: the word \"Send\".", "Requesting troops"),
    Picture(T.CAPITAL_BADGE, "Raid badge", "Home village, over the capital airship: the sword on the red badge "
            "(raids left; a gold sword for the bonus raid).", CAPITAL, folder=True),
    Picture(T.CAPITAL_COLLAPSE, "Fold event buttons", "Home village, bottom: the \"<\" at the end of the event "
            "buttons.", CAPITAL),
    Picture(T.CAPITAL_AIRSHIP, "Capital airship", "Home village, bottom left (event buttons folded): the airship's "
            "three orange balloons, without the badge.", CAPITAL),
    Picture(T.CAPITAL_START_RAID, "Start Raid", "Clan Capital before the clan's raid began, bottom right: the "
            "crossed swords and \"Start Raid\".", CAPITAL),
    Picture(T.CAPITAL_NEXT_RAID, "Next Raid", "Clan Capital, after a raid's summary: the words \"Next Raid\".",
            CAPITAL),
    Picture(T.CAPITAL_RAID_MAP, "Raid Map", "Clan Capital, bottom right: the words \"Raid Map\".", CAPITAL),
    Picture(T.CAPITAL_LEAVE_RAID, "Raid map: Map", "Raid map, bottom left: the airship \"Map\" button.", CAPITAL),
    Picture(T.CAPITAL_RETURN_HOME, "Capital: Return Home", "Clan Capital, bottom left: the words \"Return Home\".",
            CAPITAL),
    Picture(T.CAPITAL_DISTRICT_ATTACK, "District Attack", "Raid map, after tapping a district: the red \"Attack\".",
            CAPITAL),
    Picture(T.CAPITAL_ATTACK_NOW, "Attack Now?", "Raid map → Attack: the title \"Attack Now?\".", CAPITAL),
    Picture(T.CAPITAL_ATTACK_GO, "Raid Attack", "\"Attack Now?\" window: the green \"Attack\".", CAPITAL),
    Picture(T.CAPITAL_SURE_ATTACK, "Attack anyway", "\"Capital Gold storage is full\" question: \"Attack\".", CAPITAL),
    Picture(T.CAPITAL_NOT_MEMBER, "Not a member yet", "\"Only members who were in the Clan before the Raid Weekend "
            "began can attack\": its words.", CAPITAL),
    Picture(T.CAPITAL_CARD_DIR, "Capital unit", "Change Capital Army list: the unit's picture, in colour (it tells the "
            "cards in the raid's troop bar apart). Save it under the unit's name, e.g. super_miner.", CAPITAL, folder=True),
    Picture(T.CAPITAL_BATTLE_HEAL, "Raid Heal card", "Troop bar during a raid: the Heal Spell card's bottle.",
            CAPITAL),
    Picture(T.CAPITAL_RESULT_MAP, "Raid result: Map", "Raid result screen: the word \"Map\".", CAPITAL),
    Picture(T.TRY_AGAIN, "Try Again", "Connection lost popup: \"Try Again\".", "Recovering from disconnects"),
    Picture(T.RELOAD, "Reload", "\"Another device is connected\" popup: \"Reload\".", "Recovering from disconnects"),
    Picture(T.LOGIN, "Not logged in", "The game's start screen with no account logged in: \"Log in with Supercell ID\".",
            "Stopping when the game isn't logged in"),
    Picture(T.RATE_TITLE, "Rate Clash of Clans", "The title of Google Play's \"Rate Clash of Clans\" dialog, "
            "which can pop up over the game.", "Closing the rate-the-game dialog"),
    Picture(T.RATE_NEVER, "Rate dialog: Never", "NEVER, at the bottom of Google Play's \"Rate Clash of Clans\" "
            "dialog.", "Closing the rate-the-game dialog"),
    Picture(T.MAINTENANCE, "Maintenance break", "The yellow \"Maintenance break\" banner at the top of the "
            "news screen the game shows during a maintenance break.", "Stopping during a maintenance break"),
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
    if not imagefile.write(str(path), crop):
        raise OSError(f"could not write {path}")
    return path
