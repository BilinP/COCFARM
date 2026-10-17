"""A CSV log of every attack: the base's loot and what you actually gained.

Gains come from your own resource bar, read just before the search and
again when the bot is back home, so they include the star bonus and
anything the base had that the scouting numbers didn't show.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from farmbot.tasks.search import Loot

COLUMNS = ["time", "target_gold", "target_elixir", "target_dark",
           "gold_before", "elixir_before", "dark_before",
           "gained_gold", "gained_elixir", "gained_dark"]


@dataclass
class AttackRecord:
    time: datetime
    target: Loot  # the "Available Loot" of the base attacked
    before: Loot | None  # your resources before the search
    after: Loot | None = None  # and once back home
    taken: float | None = None  # share of ``target`` taken, read during the battle

    @property
    def gained(self) -> Loot | None:
        if self.before is None or self.after is None:
            return None
        return Loot(self.after.gold - self.before.gold, self.after.elixir - self.before.elixir,
                    self.after.dark - self.before.dark)

    def row(self) -> list[str]:
        def triple(loot: Loot | None) -> list[str]:
            return [str(v) for v in (loot.gold, loot.elixir, loot.dark)] if loot else ["", "", ""]

        return [self.time.isoformat(timespec="seconds"), *triple(self.target), *triple(self.before), *triple(self.gained)]


def append(path: str | Path, record: AttackRecord) -> None:
    path = Path(path)
    new = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(COLUMNS)
        writer.writerow(record.row())


def add(a: Loot, b: Loot) -> Loot:
    return Loot(a.gold + b.gold, a.elixir + b.elixir, a.dark + b.dark)


def totals_for(path: str | Path, day: date) -> tuple[int, Loot]:
    """Attacks logged on ``day`` and their summed gains (unknown gains count as 0)."""
    path = Path(path)
    count, total = 0, Loot(0, 0, 0)
    if not path.is_file():
        return count, total
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                if datetime.fromisoformat(row["time"]).date() != day:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            count += 1
            gained = [row.get(f"gained_{k}") or "0" for k in ("gold", "elixir", "dark")]
            if all(v.lstrip("-").isdigit() for v in gained):
                total = add(total, Loot(*(int(v) for v in gained)))
    return count, total
