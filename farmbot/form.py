"""The GUI's settings model, kept free of tkinter so it can be tested.

Each :class:`Field` maps one control to a dotted config path. Values from
the window are validated field by field so the GUI can show the error
next to the control that caused it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from farmbot.config import RESOURCES, SIDES, Config

SIDE_LABELS = {"nw": "Top-left (NW)", "ne": "Top-right (NE)", "sw": "Bottom-left (SW)", "se": "Bottom-right (SE)"}


@dataclass(frozen=True)
class Field:
    path: str  # e.g. "search.min_gold"
    kind: str  # amount | int | float | percent | bool | text | sides | resources | hours | units
    low: float | None = None
    high: float | None = None


FIELDS = [
    # Search
    Field("search.min_gold", "amount"),
    Field("search.min_elixir", "amount"),
    Field("search.min_dark", "amount"),
    Field("search.match_any", "bool"),
    Field("search.max_searches", "int", 1, 1000),
    Field("search.sound_on_match", "bool"),
    Field("search.min_own_gold", "amount"),
    Field("search.maintain_only", "bool"),
    # Attack
    Field("attack.sides", "sides"),
    Field("attack.waves", "int", 1, 3),
    Field("attack.troop_speed", "int", 1, 9),
    Field("attack.wave_speed", "int", 1, 9),
    Field("attack.spell_placement", "text"),
    Field("attack.use_siege", "bool"),
    Field("attack.hero_abilities", "bool"),
    Field("attack.hero_ability_delay", "float", 0, 120),
    Field("attack.ability_health", "percent", 0, 95),
    Field("attack.ability_max_wait", "float", 5, 180),
    Field("attack.speed_up", "bool"),
    Field("attack.end_when_no_loot", "bool"),
    Field("attack.no_loot_seconds", "float", 3, 180),
    Field("attack.end_at_loot_share", "percent", 0, 100),
    Field("redline.enabled", "bool"),
    Field("zone.enabled", "bool"),
    # Army & village
    Field("bot.collect_resources", "bool"),
    Field("bot.collect_gold", "bool"),
    Field("bot.collect_elixir", "bool"),
    Field("bot.collect_dark", "bool"),
    Field("bot.collect_loot_cart", "bool"),
    Field("bot.stop_when_full", "resources"),
    Field("bot.mode", "text"),
    Field("bot.theme", "text"),
    Field("bot.auto_first", "text"),
    Field("builder.corner", "text"),
    Field("builder.use_hero", "bool"),
    Field("builder.troop_delay", "float", 0, 30),
    Field("builder.hero_ability", "bool"),
    Field("builder.hero_ability_after", "float", 0, 120),
    Field("builder.troop_abilities", "bool"),
    Field("builder.ability_batch", "int", 1, 10),
    Field("builder.ability_delay", "float", 0, 60),
    Field("builder.stage_delay", "float", 0, 30),
    Field("builder.first_hero", "text"),
    Field("builder.switch_hero", "bool"),
    Field("builder.switch_below", "percent", 1, 100),
    Field("builder.return_home_after", "bool"),
    Field("builder.speed_up", "bool"),
    Field("builder.collect_resources", "bool"),
    Field("builder.collect_cart", "bool"),
    Field("builder.cart_every_min", "int", 1, 100),
    Field("builder.cart_every_max", "int", 1, 100),
    Field("builder.walls", "bool"),
    Field("builder.walls_use", "text"),
    Field("builder.walls_min_gold", "amount"),
    Field("builder.walls_min_elixir", "amount"),
    Field("builder.walls_per_round", "int", 1, 50),
    Field("walls.enabled", "bool"),
    Field("walls.use", "text"),
    Field("walls.min_gold", "amount"),
    Field("walls.min_elixir", "amount"),
    Field("walls.max_per_round", "int", 1, 50),
    # Clan
    Field("clan.donate", "bool"),
    Field("clan.donate_main", "units"),
    Field("clan.donate_siege", "units"),
    Field("clan.donate_spells", "units"),
    Field("clan.donate_fill", "bool"),
    Field("clan.max_donations_per_request", "int", 1, 50),
    Field("clan.request_troops", "bool"),
    Field("clan.request_every_minutes", "int", 1, 600),
    # Schedule
    Field("schedule.enabled", "bool"),
    Field("schedule.active_hours", "hours"),
    Field("schedule.daily_attacks_min", "int", 0, 1000),
    Field("schedule.daily_attacks_max", "int", 0, 1000),
    Field("schedule.breaks", "bool"),
    Field("schedule.break_every_min", "int", 1, 500),
    Field("schedule.break_every_max", "int", 1, 500),
    Field("schedule.break_minutes_min", "int", 1, 600),
    Field("schedule.break_minutes_max", "int", 1, 600),
    Field("notify.discord_webhook", "text"),
    Field("notify.on_storage_full", "bool"),
    Field("notify.on_stopped", "bool"),
    Field("notify.on_emulator_restart", "bool"),
    Field("notify.daily_summary", "bool"),
    # Device
    Field("device.mode", "text"),
    Field("device.bluestacks_instance", "text"),
    Field("device.launch_emulator", "bool"),
    Field("device.enable_adb", "bool"),
    Field("device.set_display", "bool"),
    Field("device.display_width", "int", 540, 3840),
    Field("device.display_height", "int", 540, 2160),
    Field("device.display_dpi", "int", 120, 640),
    Field("device.set_resources", "bool"),
    Field("device.cpus", "int", 2, 4),
    Field("device.memory", "int", 2048, 8192),
    Field("device.ldplayer", "text"),
    Field("device.ldplayer_index", "int", 0, 99),
    Field("device.serial", "text"),
    Field("device.adb_path", "text"),
]
BY_PATH = {f.path: f for f in FIELDS}


def parse_amount(text: str) -> int:
    """'1 500 000', '1,500,000', '1.5m' or '300k' → int."""
    cleaned = str(text).strip().lower().replace(",", "").replace(" ", "").replace("_", "")
    if not cleaned:
        return 0
    multiplier = 1
    if cleaned[-1] in "km":
        multiplier = 1_000 if cleaned[-1] == "k" else 1_000_000
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned) * multiplier
    except ValueError:
        raise ValueError("enter a number, e.g. 500k or 1,500,000") from None
    if value < 0:
        raise ValueError("cannot be negative")
    return int(round(value))


def _get(config: Config, path: str) -> Any:
    section, name = path.split(".")
    return getattr(getattr(config, section), name)


def _set(config: Config, path: str, value: Any) -> None:
    section, name = path.split(".")
    setattr(getattr(config, section), name, value)


def _display(field: Field, value: Any) -> Any:
    if field.kind == "amount":
        return f"{value:,}"
    if field.kind in ("sides", "resources", "hours", "units"):
        return list(value)
    if field.kind == "float":
        return f"{value:g}"
    if field.kind == "percent":  # stored as a share, shown as 0-100
        return f"{value * 100:g}"
    return value


def form_values(config: Config) -> dict[str, Any]:
    """Current config as the values the window shows."""
    return {f.path: _display(f, _get(config, f.path)) for f in FIELDS}


def _parse(field: Field, raw: Any) -> Any:
    if field.kind == "bool":
        return bool(raw)
    if field.kind == "text":
        return str(raw).strip()
    if field.kind == "amount":
        return parse_amount(raw)
    if field.kind == "sides":
        sides = [s for s in SIDES if s in raw]
        if not sides:
            raise ValueError("pick at least one side")
        return sides
    if field.kind == "resources":
        return [k for k in RESOURCES if k in raw]
    if field.kind == "units":
        from farmbot.units import BY_NAME

        return [name for name in dict.fromkeys(raw) if name in BY_NAME]  # in the order given
    if field.kind == "hours":
        hours = sorted({int(h) for h in raw})
        if any(not 0 <= h <= 23 for h in hours):
            raise ValueError("hours must be 0-23")
        return hours
    if field.kind == "percent":
        raw = str(raw).strip().rstrip("%")
    try:
        value = int(str(raw).strip()) if field.kind == "int" else float(str(raw).strip())
    except ValueError:
        raise ValueError("enter a whole number" if field.kind == "int" else "enter a number") from None
    if field.low is not None and value < field.low or field.high is not None and value > field.high:
        raise ValueError(f"use {field.low:g} to {field.high:g}")
    return round(value / 100, 4) if field.kind == "percent" else value


def validate(form: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Parsed values and a message for each invalid field."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for path, raw in form.items():
        field = BY_PATH.get(path)
        if field is None:
            continue
        try:
            values[path] = _parse(field, raw)
        except ValueError as exc:
            errors[path] = str(exc)
    if values.get("schedule.daily_attacks_max") and \
            values.get("schedule.daily_attacks_min", 0) > values["schedule.daily_attacks_max"]:
        errors["schedule.daily_attacks_max"] = "must be at least the 'from' number (0 to 0 = no limit)"
    for low, high in (("schedule.break_every_min", "schedule.break_every_max"),
                      ("schedule.break_minutes_min", "schedule.break_minutes_max")):
        if values.get(low, 0) > values.get(high, 10**9):
            errors[high] = "must be at least the 'from' number"
    if values.get("builder.cart_every_min", 0) > values.get("builder.cart_every_max", 10**9):
        errors["builder.cart_every_max"] = "must be at least the 'from' number"
    if values.get("bot.mode") == "auto" and not values.get("bot.stop_when_full", ["gold"]):
        errors["bot.stop_when_full"] = "pick the storages that make Auto switch to the other village"
    if values.get("schedule.enabled") and not values.get("schedule.active_hours", [1]):
        errors["schedule.active_hours"] = "pick at least one hour, or turn the schedule off"
    return values, errors


def apply_form(config: Config, form: dict[str, Any]) -> None:
    """Copy validated values into ``config`` in place, so a running bot sees
    them. Raises ``ValueError`` naming the first bad field."""
    values, errors = validate(form)
    if errors:
        path, message = next(iter(errors.items()))
        raise ValueError(f"{path}: {message}")
    for path, value in values.items():
        if path == "device.serial" and not value:
            continue
        _set(config, path, value)
    config.validate()
