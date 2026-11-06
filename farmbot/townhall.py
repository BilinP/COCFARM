"""What an account has unlocked, from the game's own village export
(profile → Settings → More Settings → Data Export: "Copy"): the Town Hall's
level, and every building's. It's kept beside the account's profile
(``profiles/<name>.village.json``), or in the farm's folder without
profiles (``village.json``), and read again once a day.

With it the bot leaves out what this account can't do yet (research
before the Laboratory, the clan before the Clan Castle is rebuilt...),
and the window says so on those settings."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

VILLAGE_FILE = "village.json"

# The game's ids for the buildings the export lists ("data").
TOWN_HALL = 1000001
LABORATORY = 1000007
WALL = 1000010
CLAN_CASTLE = 1000014

# Town Hall levels that open a feature (only those that are certain: a
# feature is left out only when it can't be there yet).
DARK_ELIXIR_AT = 7
HEROES_AT = 7
BUILDER_BASE_AT = 4
SPELLS_AT = 5  # the Spell Factory
WALL_ELIXIR_AT = 5  # walls take elixir from level 5, which Town Hall 5 opens
SUPER_TROOPS_AT = 11
SIEGE_AT = 12  # the Workshop
PETS_AT = 14

# What each feature is called, at the top of a page or tab it leaves out.
NAMES = {"research": "Research", "clan": "The clan", "heroes": "Heroes", "pets": "Pets",
         "dark": "Dark elixir", "spells": "Spells", "super_troops": "Super troops",
         "siege": "Siege machines", "builder_base": "The Builder Base", "wall_elixir": "Walls with elixir",
         "builder_heroes": "Builder base heroes"}


@dataclass
class Village:
    """The export, as far as the bot needs it."""

    town_hall: int
    buildings: dict[int, tuple[int, int]] = field(default_factory=dict)  # id -> (level, how many)
    heroes: int = 0
    pets: int = 0
    builder_heroes: int = -1  # the builder base's (-1: not in the export)
    timestamp: int = 0  # when the game made the export (seconds since 1970)
    read_on: str = ""  # the day the bot read it (YYYY-MM-DD)

    def has(self, building: int) -> bool:
        return self.buildings.get(building, (0, 0))[1] > 0


def parse(text: str | None) -> Village | None:
    """The export the game copied (its JSON), or None if ``text`` isn't one."""
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("buildings"), list):
        return None
    buildings: dict[int, tuple[int, int]] = {}
    for b in data["buildings"]:
        if not isinstance(b, dict) or not isinstance(b.get("data"), int):
            continue
        level, count = int(b.get("lvl", 0) or 0), int(b.get("cnt", 1) or 1)
        old_level, old_count = buildings.get(b["data"], (0, 0))
        buildings[b["data"]] = (max(level, old_level), old_count + count)
    hall = buildings.get(TOWN_HALL, (0, 0))[0]
    if hall < 1:
        return None
    count = lambda key: len(data.get(key) or []) if isinstance(data.get(key), list) else 0
    stamp = data.get("timestamp")
    return Village(hall, buildings, heroes=count("heroes"), pets=count("pets"),
                   builder_heroes=count("heroes2") if isinstance(data.get("heroes2"), list) else -1,
                   timestamp=stamp if isinstance(stamp, int) else 0)


def load(path: str | Path | None) -> Village | None:
    """The export kept at ``path`` (see :func:`save`), if any."""
    if path is None:
        return None
    try:
        kept = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, dict):
        return None
    village = parse(json.dumps(kept.get("export")))
    if village is not None:
        village.read_on = str(kept.get("read_on", ""))
    return village


def save(path: str | Path, text: str, read_on: str) -> None:
    """Keep the export the game copied (``text``) at ``path``, with the day."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {"read_on": read_on, "export": json.loads(text)}
    path.write_text(json.dumps(kept, indent=1), encoding="utf-8")


def locked(village: Village | None) -> dict[str, str]:
    """What this account can't do yet, each with why: "research", "clan"
    (donations, requests, the Clan Capital), "heroes", "pets", "dark",
    "spells", "super_troops", "siege", "builder_base", "wall_elixir",
    "builder_heroes".
    Nothing when it isn't known."""
    if village is None:
        return {}
    th = village.town_hall
    out: dict[str, str] = {}
    if not village.has(LABORATORY):
        out["research"] = ("the Laboratory comes at Town Hall 3" if th < 3 else
                           "there's no Laboratory in the village yet")
    if not village.has(CLAN_CASTLE):
        out["clan"] = "the Clan Castle isn't rebuilt yet (rebuild it in the game)"
    if village.heroes == 0:
        out["heroes"] = (f"the first hero comes at Town Hall {HEROES_AT}" if th < HEROES_AT else
                         "no hero in the village yet")
    if village.pets == 0:
        out["pets"] = (f"pets come at Town Hall {PETS_AT}" if th < PETS_AT else "no pet unlocked yet")
    if th < DARK_ELIXIR_AT:
        out["dark"] = f"dark elixir comes at Town Hall {DARK_ELIXIR_AT}"
    if th < SPELLS_AT:
        out["spells"] = f"the Spell Factory comes at Town Hall {SPELLS_AT}"
    if th < WALL_ELIXIR_AT:
        out["wall_elixir"] = f"walls take elixir from level 5, at Town Hall {WALL_ELIXIR_AT}: gold only until then"
    if th < SUPER_TROOPS_AT:
        out["super_troops"] = f"super troops come at Town Hall {SUPER_TROOPS_AT}"
    if th < SIEGE_AT:
        out["siege"] = f"the Workshop comes at Town Hall {SIEGE_AT}"
    if th < BUILDER_BASE_AT:
        out["builder_base"] = f"the Builder Base opens at Town Hall {BUILDER_BASE_AT}"
    elif village.builder_heroes == 0:
        out["builder_heroes"] = "no hero in the builder base yet (the Battle Machine comes with the Builder Hall)"
    return out


def describe(village: Village | None) -> str:
    """"Town Hall 2", for the window."""
    return f"Town Hall {village.town_hall}" if village else ""


# The game's ids (less 1000000) for buildings, as the export lists them.
BUILDING_NAMES = {0: "Army Camp", 1: "Town Hall", 2: "Elixir Collector", 3: "Elixir Storage", 4: "Gold Mine",
                  5: "Gold Storage", 6: "Barracks", 7: "Laboratory", 8: "Cannon", 9: "Archer Tower", 10: "Wall",
                  11: "Wizard Tower", 12: "Air Defense", 13: "Mortar", 14: "Clan Castle", 15: "Builder's Hut",
                  19: "Hidden Tesla", 20: "Spell Factory", 21: "X-Bow", 23: "Dark Elixir Drill",
                  24: "Dark Elixir Storage", 26: "Dark Barracks", 27: "Inferno Tower", 28: "Air Sweeper",
                  29: "Dark Spell Factory"}
TROOP_NAMES = {0: "Barbarian", 1: "Archer", 2: "Goblin", 3: "Giant", 4: "Wall Breaker", 5: "Balloon",
               6: "Wizard", 7: "Healer", 8: "Dragon", 9: "P.E.K.K.A"}


def load_export(path: str | Path | None) -> tuple[dict, str] | None:
    """The export kept at ``path`` as the game gave it, and the day it was
    read; None if there's none."""
    if path is None:
        return None
    try:
        kept = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, dict) or parse(json.dumps(kept.get("export"))) is None:
        return None
    return kept["export"], str(kept.get("read_on", ""))


def overview(export: dict) -> dict[str, list[tuple[str, str]]]:
    """What the export holds, for people to read: section -> (name, detail)
    rows. Never the player tag."""

    def items(key: str) -> list[dict]:
        value = export.get(key)
        return [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []

    def named(table: dict[int, str], data: int, base: int, kind: str) -> str:
        return table.get(data - base, f"{kind} #{data - base}")

    buildings: dict[str, list[tuple[int, int]]] = {}
    for b in items("buildings"):
        if isinstance(b.get("data"), int):
            name = named(BUILDING_NAMES, b["data"], 1_000_000, "Building")
            buildings.setdefault(name, []).append((int(b.get("lvl", 0) or 0), int(b.get("cnt", 1) or 1)))
    rows = []
    for name, levels in sorted(buildings.items(), key=lambda kv: (kv[0] != "Town Hall", kv[0])):
        total = sum(c for _, c in levels)
        by_level = ", ".join(f"{c} at level {lvl}" if c > 1 or len(levels) > 1 else f"level {lvl}"
                             for lvl, c in sorted(levels, reverse=True))
        rows.append((f"{name}" + (f" ×{total}" if total > 1 else ""), by_level))
    army = [(named(TROOP_NAMES, u["data"], 4_000_000, "Troop"), f"level {u.get('lvl', 0)}")
            for u in items("units") if isinstance(u.get("data"), int)]
    counts = [(label, str(len(items(key)))) for key, label in
              (("spells", "Spells"), ("heroes", "Heroes"), ("pets", "Pets"), ("siege_machines", "Siege machines"),
               ("equipment", "Hero equipment"), ("traps", "Traps"), ("obstacles", "Obstacles (trees, rocks...)"))]
    builder = [("Buildings", str(sum(int(b.get("cnt", 1) or 1) for b in items("buildings2")))),
               ("Heroes", str(len(items("heroes2")))), ("Troops", str(len(items("units2"))))]
    return {"Buildings": rows, "Troops": army, "Unlocked": counts, "Builder base": builder}
