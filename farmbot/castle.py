"""Clan Castle levels: the room a request has, and the units given to it.

A request's bubble in clan chat says how much room the requester's Clan
Castle has ("0/55" troops, "0/3" spells, "1/2" siege machines), and that
tells its level. The level caps the units' level (the Laboratory Level Cap: a unit
donated above it arrives at the cap), and picks the preset army given to a
request that names no units when no main troops are picked.
"""

from __future__ import annotations

from dataclasses import dataclass

from farmbot.units import BY_NAME


@dataclass(frozen=True)
class Castle:
    level: int
    troops: int  # housing space
    spells: int
    siege: int
    lab_cap: int  # donated units come at this level at most


CASTLES = [Castle(*row) for row in (
    (1, 10, 0, 0, 5),
    (2, 15, 0, 0, 6),
    (3, 20, 0, 0, 7),
    (4, 25, 1, 0, 8),
    (5, 30, 1, 0, 9),
    (6, 35, 1, 1, 10),
    (7, 35, 2, 1, 11),
    (8, 40, 2, 1, 12),
    (9, 45, 2, 1, 13),
    (10, 45, 3, 1, 14),
    (11, 50, 3, 1, 15),
    (12, 50, 3, 2, 16),
    (13, 55, 3, 2, 16),
    (14, 55, 4, 2, 16),
)]


def castle_for(troops: int, spells: int | None = None, siege: int | None = None) -> Castle | None:
    """The Clan Castle with room for ``troops`` (and ``spells`` and
    ``siege``, if known). Where that leaves two levels, the lower is taken
    (its units come at a lower level)."""
    for castle in CASTLES:
        if castle.troops == troops and spells in (None, castle.spells) and siege in (None, castle.siege):
            return castle
    return None


# The army given to a request when no main troops are picked, by Clan
# Castle level: in order, the first in colour goes first. "1 Super Yeti"
# gives one at most; a name alone gives as many as fit. With donate_fill, the
# room left afterwards goes to the biggest troops that fit. Super troops (in
# colour only while boosted) and Electro Dragons go to castles of level 7
# and up (Town Hall 11, where they start), Dragons from level 3; spells are
# Rage, and Totem where only one space is left.
SUPERS = ["1 Super Yeti", "1 Super Wizard", "1 Super Archer", "Electro Dragon", "Dragon"]
SPELLS = ["Rage Spell", "Totem Spell"]
PRESETS: dict[str, list[str]] = {
    str(castle.level): (SUPERS if castle.level >= 7 else ["Dragon"] if castle.level >= 3 else [])
    + ["Wizard", "Archer"] * (castle.level < 7) + SPELLS * (castle.spells > 0)
    for castle in CASTLES
}


def parse_entry(entry: str) -> tuple[str, int | None] | None:
    """``"3 Valkyrie"`` → ``("Valkyrie", 3)``, ``"Valkyrie"`` → ``("Valkyrie",
    None)`` (as many as fit); ``None`` for an unknown unit."""
    count, _, name = entry.strip().partition(" ")
    if not count.isdigit():
        count, name = "", entry.strip()
    unit = next((u for u in BY_NAME.values() if u.name.lower() == name.strip().lower()), None)
    if unit is None or count == "0":
        return None
    return unit.name, int(count) if count else None


def preset(entries: list[str]) -> dict[str, int | None]:
    """A preset's units and counts (``None``: as many as fit), in order
    (unknown names left out)."""
    out: dict[str, int | None] = {}
    for entry in entries:
        parsed = parse_entry(entry)
        if parsed:
            name, count = parsed
            had = out.get(name, 0)
            out[name] = None if count is None or (name in out and had is None) else had + count
    return out
