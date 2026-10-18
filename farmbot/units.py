"""Every unit that can be donated, with its housing space.

Housing space decides what still fits in a Clan Castle request: the donate
window greys out what doesn't, and the bot fills the space that's left with
the biggest troop that fits. Siege machines take the request's one siege
slot; spells have their own space.

A unit's picture (for telling the cards in the donate window apart) is
``templates/donate/<slug>.png``; see :func:`slug`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    name: str
    kind: str  # troop | siege | spell
    housing: int


def _units(kind: str, table: str) -> list[Unit]:
    out = []
    for line in table.strip().splitlines():
        name, housing = line.rsplit(",", 1)
        out.append(Unit(name.strip(), kind, int(housing)))
    return out


TROOPS = _units("troop", """
Barbarian, 1
Archer, 1
Giant, 5
Goblin, 1
Wall Breaker, 2
Balloon, 5
Wizard, 4
Healer, 14
Dragon, 20
P.E.K.K.A, 25
Baby Dragon, 10
Miner, 6
Electro Dragon, 30
Yeti, 18
Dragon Rider, 25
Electro Titan, 32
Root Rider, 20
Thrower, 16
Minion, 2
Hog Rider, 5
Valkyrie, 8
Golem, 30
Witch, 12
Lava Hound, 30
Bowler, 6
Ice Golem, 15
Headhunter, 6
Apprentice Warden, 20
Druid, 16
Furnace, 18
Ruin Witch, 26
Meteor Golem, 40
Super Barbarian, 5
Super Archer, 12
Super Giant, 10
Sneaky Goblin, 3
Super Wall Breaker, 8
Rocket Balloon, 8
Super Wizard, 10
Super Dragon, 40
Inferno Dragon, 15
Super Miner, 24
Super Yeti, 35
Super Minion, 12
Super Valkyrie, 20
Super Witch, 40
Ice Hound, 40
Super Bowler, 30
Super Hog Rider, 12
""")

SIEGE_MACHINES = _units("siege", """
Wall Wrecker, 1
Battle Blimp, 1
Stone Slammer, 1
Siege Barracks, 1
Log Launcher, 1
Flame Flinger, 1
Battle Drill, 1
Troop Launcher, 1
Sky Wagon, 1
""")

SPELLS = _units("spell", """
Lightning Spell, 1
Healing Spell, 2
Rage Spell, 2
Jump Spell, 2
Freeze Spell, 1
Clone Spell, 3
Invisibility Spell, 1
Recall Spell, 2
Revive Spell, 2
Totem Spell, 1
Poison Spell, 1
Earthquake Spell, 1
Haste Spell, 1
Skeleton Spell, 1
Bat Spell, 1
Overgrowth Spell, 2
Ice Block Spell, 1
Angry Spell, 1
""")

UNITS = TROOPS + SIEGE_MACHINES + SPELLS
BY_NAME = {u.name: u for u in UNITS}


def slug(name: str) -> str:
    """"P.E.K.K.A" -> "pekka", "Super Wall Breaker" -> "super_wall_breaker"."""
    return "".join(c if c.isalnum() else "_" for c in name.lower().replace(".", "")).strip("_")


BY_SLUG = {slug(u.name): u for u in UNITS}
