"""Settings, loaded from a TOML file (see ``farmbot.example.toml``).

All coordinates are for a screen scaled to 920 px tall (the bot does the
scaling). In ``[x, y, w, h]`` rectangles a negative ``x``/``y`` counts from
the right/bottom edge and a ``w``/``h`` of 0 (or less) reaches that edge,
so the same settings fit phones and 16:9 emulators alike.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from farmbot.geometry import Rect

SIDES = ("nw", "ne", "sw", "se")


@dataclass
class DeviceConfig:
    # "bluestacks": find BlueStacks 5 and its ADB port automatically.
    # "ldplayer": find LDPlayer 9+ and its instance automatically.
    # "manual": connect to ``serial`` (any emulator or phone).
    mode: str = "bluestacks"
    ldplayer: str = ""  # install folder or name ("LDPlayer 9"); empty = the newest found
    ldplayer_index: int = 0  # which LDPlayer instance
    bluestacks_instance: str = ""  # display or internal name; empty = the main instance
    launch_emulator: bool = True  # start BlueStacks if it is not running
    launch_timeout: float = 120.0  # seconds to wait for it to boot
    # When ADB is off in BlueStacks, turn it on (this restarts BlueStacks once),
    # as ClashFarmer does. Off: drive the BlueStacks window instead, which
    # needs it in front and moves the mouse.
    enable_adb: bool = True
    # Set the BlueStacks instance's display to this resolution and DPI when
    # connecting (BlueStacks restarts once if it had another). The bot's
    # pictures and the starting profile were made at 860 x 672, 240 DPI.
    set_display: bool = True
    display_width: int = 860
    display_height: int = 672
    display_dpi: int = 240
    # Give the emulator this many CPU cores and this much memory (MB): the
    # least the game runs well on, leaving the rest of the PC free.
    set_resources: bool = True
    cpus: int = 2
    memory: int = 2048
    serial: str = "127.0.0.1:5555"
    adb_path: str = "adb"  # "adb" = BlueStacks' own HD-Adb.exe when available
    package: str = "com.supercell.clashofclans"
    # How much bigger the game draws its buttons than the pictures in
    # templates/ (about 1.2 on a 16:9 BlueStacks window). 0 = work it out.
    ui_scale: float = 0.0
    # Size of game windows (battle menu, army screen) relative to the HUD.
    # They shrink to fit narrow screens. 0 = 1 / ui_scale (right for wide screens).
    window_scale: float = 0.0
    # Pictures whose window is sized differently again on this screen:
    # picture name -> size relative to the HUD, e.g. "buttons/attack" = 0.66.
    picture_scales: dict[str, float] = field(default_factory=dict)


@dataclass
class RegionsConfig:
    """Where the "Available Loot" numbers are while scouting."""

    loot_gold: list[int] = field(default_factory=lambda: [195, 92, 150, 34])
    loot_elixir: list[int] = field(default_factory=lambda: [195, 134, 150, 34])
    loot_dark: list[int] = field(default_factory=lambda: [195, 175, 150, 34])
    # Your own resources, top-right of the home village.
    own_gold: list[int] = field(default_factory=lambda: [-320, 25, 130, 32])
    own_elixir: list[int] = field(default_factory=lambda: [-320, 78, 130, 32])
    own_dark: list[int] = field(default_factory=lambda: [-285, 130, 95, 32])
    # A patch at the empty end (left) of each resource bar: bars fill from
    # the right, so this only turns the resource's colour when it's full.
    full_gold: list[int] = field(default_factory=lambda: [-409, 33, 24, 14])
    full_elixir: list[int] = field(default_factory=lambda: [-409, 83, 24, 14])
    full_dark: list[int] = field(default_factory=lambda: [-340, 133, 24, 14])

    def rect(self, name: str) -> Rect:
        return Rect.from_list(getattr(self, name))


@dataclass
class SearchConfig:
    min_gold: int = 300_000
    min_elixir: int = 300_000
    min_dark: int = 0
    match_any: bool = False  # accept a base if any one minimum is met
    max_searches: int = 150
    next_delay: float = 2.5  # seconds to wait for the next base to load
    sound_on_match: bool = False
    # Skip searching while your own gold is below this (0 = always search).
    min_own_gold: int = 50_000
    maintain_only: bool = False  # never search; just look after the village


@dataclass
class AttackConfig:
    sides: list[str] = field(default_factory=lambda: ["nw", "ne", "sw", "se"])
    waves: int = 1  # split each troop type over 1-3 waves
    troop_speed: int = 7  # 1 (slow) - 9 (fast): pause between single taps
    wave_speed: int = 8  # 1 (slow) - 9 (fast): pause between waves
    # Siege machines let the Clan Castle troops out when destroyed; off leaves them in the bar.
    use_siege: bool = False
    hero_abilities: bool = True
    hero_ability_delay: float = 10.0
    # Fire each hero's ability once its health drops to this share (0.5 =
    # half), instead of after hero_ability_delay; 0 uses the timer. Heroes
    # still unhurt after ability_max_wait seconds fire anyway.
    ability_health: float = 0.0
    ability_max_wait: float = 45.0
    speed_up: bool = True  # press the 1x button (last minute of a battle) so it plays at 4x
    # End the battle once the available loot has not dropped for this long.
    end_when_no_loot: bool = True
    no_loot_seconds: float = 15.0
    # End once this share of the base's reported loot is taken (0 = off).
    end_at_loot_share: float = 0.9
    taps_per_batch: int = 6  # deploy taps between checks of the troop card
    max_batches_per_card: int = 12
    battle_timeout: float = 180.0
    # "together": every spell in one burst on one spot (the base centre);
    # "spread": spread over the base, spell_reach of the way from its centre
    # towards the edge (0-1).
    spell_placement: str = "together"
    spell_reach: float = 0.45
    # Last-resort deploy lines per side, [x1, y1, x2, y2], used when neither
    # the red line nor the building area can be found. They were measured on
    # a screen deploy_lines_width wide and shift with the screen's width.
    deploy_lines_width: int = 2000
    deploy_lines: dict[str, list[int]] = field(default_factory=lambda: {
        "nw": [190, 380, 620, 90],
        "ne": [1380, 90, 1800, 380],
        "sw": [240, 560, 520, 690],
        "se": [1480, 690, 1790, 520],
    })

    @property
    def tap_delay(self) -> float:
        return 0.03 + (9 - _speed(self.troop_speed)) * 0.04

    @property
    def wave_delay(self) -> float:
        return 0.5 + (9 - _speed(self.wave_speed)) * 0.75


def _speed(value: int) -> int:
    return min(9, max(1, int(value)))


@dataclass
class BarConfig:
    """Troop bar detection (see ``farmbot/armybar.py``)."""

    band: list[int] = field(default_factory=lambda: [0, -160, 0, 110])  # card rows
    card_width: int = 94
    width_tolerance: float = 0.12
    edge_contrast: int = 40
    edge_share: float = 0.5  # share of the band's rows an edge must span
    header_share: float = 0.2  # top part of a card holding the "xN" count
    count_white_share: float = 0.08
    spell_hue: list[int] = field(default_factory=lambda: [118, 150])
    # A one-unit card on a light cyan background is a siege machine (heroes: blue to purple).
    siege_hue: list[int] = field(default_factory=lambda: [85, 100])
    # Card frames are blue to purple; an empty slot shows the map instead.
    frame_hue: list[int] = field(default_factory=lambda: [85, 160])
    min_frame_share: float = 0.3  # of a card's left or right border in frame colour
    spent_ratio: float = 0.55  # greyed out below this share of the start colour
    count_font: str = "fonts/loot"  # digit glyphs used to read the "xN" counts


@dataclass
class ZoneConfig:
    """Deploy ring around the building area (see ``farmbot/deployzone.py``)."""

    enabled: bool = True
    grass_hue: list[int] = field(default_factory=lambda: [30, 55])
    grass_min_saturation: int = 110
    grass_min_value: int = 160
    open_px: int = 5
    close_px: int = 15
    min_patch: int = 1500
    min_patches: int = 4
    offset_px: int = 30  # distance outside the building area, about a tile
    step_px: int = 24  # spacing of candidate points along the ring
    ground_window: int = 25
    max_blocked: float = 0.35  # skip points with more non-ground around them
    # UI to keep clear of: loot panel, resources, End Battle, boosts, Next, troop bar.
    exclude: list[list[int]] = field(default_factory=lambda: [
        [0, 0, 360, 260],
        [-440, 0, 0, 180],
        [0, -270, 330, 90],
        [300, -260, 470, 70],
        [-370, -310, 250, 120],
        [0, -180, 0, 0],
    ])


@dataclass
class RedLineConfig:
    """Colour-based red-line detection (see ``farmbot/redline.py``)."""

    enabled: bool = True
    # Where the base can be: everything above the troop bar.
    battlefield: list[int] = field(default_factory=lambda: [0, 60, 0, -190])
    # Red UI that is not the line (End Battle button, etc.), as [x, y, w, h].
    ignore: list[list[int]] = field(default_factory=lambda: [[0, -280, 340, 110], [-370, -310, 250, 120]])
    # OpenCV HSV: hue 0-179. Red wraps around, hence two ranges.
    hue_ranges: list[list[int]] = field(default_factory=lambda: [[0, 8], [172, 179]])
    min_saturation: int = 150
    min_value: int = 150
    # Newer versions always draw the boundary as a faint, thin orange line
    # along the tile grid. Thin diagonal strokes of this colour count too
    # (an empty hue range turns this off).
    line_hue: list[int] = field(default_factory=lambda: [4, 24])
    line_min_saturation: int = 110
    line_value: list[int] = field(default_factory=lambda: [60, 230])
    line_length: int = 12  # px a stroke must run along the grid
    line_min_stroke: int = 30  # px a joined-up stroke must span
    join_px: int = 5  # closing kernel that merges the dots into strokes
    min_blob: int = 8  # ignore red specks smaller than this many pixels
    min_points: int = 40  # red pixels needed before trusting the result
    bins_per_side: int = 24  # deploy points sampled along each edge
    min_side_points: int = 6  # fewer than this on a side → try the next method
    offset_px: int = 12  # how far outside the line to tap
    iso_ratio: float = 0.75  # base height / width on screen (isometric squash)
    # Only red pixels between these shares of the typical line distance
    # from the centre count as line: closer is red stuff inside the base,
    # farther is red UI or effects outside it.
    min_radius_ratio: float = 0.6
    max_radius_ratio: float = 1.5
    # A side only counts if its points form a smooth curve: the 90th
    # percentile jump between neighbours, as a share of the typical
    # distance, must stay below this. Scattered red buildings fail it.
    max_jump: float = 0.2
    # The line wraps the base, so it must span at least this share of
    # the battlefield's width.
    min_extent: float = 0.3
    center: list[int] = field(default_factory=list)  # [x, y]; empty = auto
    # Tap the base centre with a troop selected so the game flashes the
    # red line. Only safe if the centre is always covered by buildings.
    reveal_tap: bool = False


@dataclass
class ClanConfig:
    """Troop requests and donations (needs the clan templates, see templates/README.md)."""

    request_troops: bool = False
    request_every_minutes: int = 10  # only when the cooldown on the button can't be read
    donate: bool = False
    # Troops to give first, in this order (names from farmbot/units.py, e.g.
    # "Super Barbarian"); then, with donate_fill, the space left goes to the
    # biggest troops that still fit. Siege machines and spells: only the ones
    # picked, in order.
    donate_main: list[str] = field(default_factory=list)
    donate_siege: list[str] = field(default_factory=list)
    donate_spells: list[str] = field(default_factory=list)
    donate_fill: bool = True
    max_donations_per_request: int = 15  # units given to one request at most


@dataclass
class ScheduleConfig:
    """When the bot may play."""

    enabled: bool = False
    active_hours: list[int] = field(default_factory=lambda: list(range(24)))  # 0-23, local time
    # Attacks per day: each day a random number between the two (0 and 0 = no limit).
    daily_attacks_min: int = 0
    daily_attacks_max: int = 0
    # Breaks, to play more like a person: after a random number of attacks
    # (between the two "every" numbers), rest a random number of minutes.
    breaks: bool = False
    break_every_min: int = 15
    break_every_max: int = 25
    break_minutes_min: int = 5
    break_minutes_max: int = 20


@dataclass
class NotifyConfig:
    """Messages to your phone through a Discord webhook (see ``farmbot/notify.py``)."""

    discord_webhook: str = ""
    on_storage_full: bool = True
    on_stopped: bool = True  # the bot stopped: by an error, or after a run
    on_emulator_restart: bool = True
    daily_summary: bool = True  # yesterday's attacks and loot, sent after midnight


@dataclass
class BotConfig:
    # What to farm: "home" (the home village), "builder" (the builder base) or
    # "auto" (start in auto_first; once that village's stop_when_full storages
    # are full, farm the other one; stop when both are full).
    mode: str = "home"
    auto_first: str = "home"  # mode "auto": the village it starts with ("home" or "builder")
    theme: str = "auto"  # the window's look: "auto" (follow Windows), "light" or "dark"
    templates: str = "templates"
    threshold: float = 0.85
    collect_resources: bool = True
    # Which full collectors to empty (one tap on a bubble empties every
    # collector of that kind).
    collect_gold: bool = True
    collect_elixir: bool = True
    collect_dark: bool = True
    collect_loot_cart: bool = True  # loot left over from defences, shown as a cart
    # Stop farming once all of these storages are full ("gold", "elixir", "dark").
    stop_when_full: list[str] = field(default_factory=list)
    max_attacks: int = 0  # 0 = unlimited
    loop_delay: float = 3.0
    history: str = "attacks.csv"  # one line per attack; "" turns it off
    error_screens: str = "screens/errors"  # screenshot saved when a round fails; "" turns it off
    # Look for a newer COCFARM on GitHub when the window opens ("" = never).
    update_repo: str = "BilinP/COCFARM"


@dataclass
class WallsConfig:
    """Spending spare gold or elixir on walls (see ``farmbot/tasks/village.py``)."""

    enabled: bool = False
    use: str = "both"  # "gold", "elixir" or "both"
    # Only spend while you have more than this; the rest is kept.
    min_gold: int = 15_000_000
    min_elixir: int = 15_000_000
    max_per_round: int = 3  # walls upgraded between two attacks at most


WALL_RESOURCES = ("gold", "elixir", "both")
MODES = ("home", "builder", "auto")  # auto: one village until its storages are full, then the other
THEMES = ("auto", "light", "dark")
BUILDER_CORNERS = ("auto", "left", "top", "right")
BUILDER_HEROES = ("any", "machine", "copter")
SPELL_PLACEMENTS = ("together", "spread")


@dataclass
class BuilderConfig:
    """Builder base battles (see ``farmbot/tasks/builder.py``)."""

    corner: str = "auto"  # where the army goes in: auto = farthest from the base
    use_hero: bool = True
    # Swap to the other builder hero before a base when the current one's
    # health is below this share (it carries its damage into the second base).
    first_hero: str = "any"  # hero for the first base: "any" (keep the one set), "machine" or "copter"
    switch_hero: bool = True
    switch_below: float = 0.5
    troop_delay: float = 3.0  # seconds between the hero landing and the troops
    hero_ability: bool = True
    hero_ability_after: float = 15.0  # seconds after the hero lands (fully charged)
    troop_abilities: bool = True
    ability_batch: int = 2  # troop abilities fired together
    ability_delay: float = 5.0  # seconds between batches
    stage_delay: float = 2.0  # wait for the second base to settle before deploying
    speed_up: bool = True  # press the 1x button so the battle plays at 4x
    collect_resources: bool = True  # empty the builder base gold mines, elixir collectors and gem mine
    # Empty the elixir cart (defense rewards) every N battles, N picked at
    # random between these two each time.
    collect_cart: bool = True
    cart_every_min: int = 5
    cart_every_max: int = 5
    # Spend spare builder gold or elixir on builder base walls between battles.
    walls: bool = False
    walls_use: str = "both"  # "gold", "elixir" or "both"
    walls_min_gold: int = 1_000_000  # keep at least this much; walls get the rest
    walls_min_elixir: int = 1_000_000
    walls_per_round: int = 3
    battle_timeout: float = 400.0  # both stages together
    return_home_after: bool = False  # sail back to the home village when the bot stops
    # The troop bar (bot coordinates at 860 x 672).
    card_y: int = 830
    hero_x: int = 68
    first_troop_x: int = 185
    card_step: float = 103.6
    max_troop_cards: int = 10
    tap_gap: float = 0.3  # seconds after each card tap (quicker taps get dropped)
RESOURCES = ("gold", "elixir", "dark")


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    regions: RegionsConfig = field(default_factory=RegionsConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    attack: AttackConfig = field(default_factory=AttackConfig)
    bar: BarConfig = field(default_factory=BarConfig)
    zone: ZoneConfig = field(default_factory=ZoneConfig)
    redline: RedLineConfig = field(default_factory=RedLineConfig)
    clan: ClanConfig = field(default_factory=ClanConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    walls: WallsConfig = field(default_factory=WallsConfig)
    builder: BuilderConfig = field(default_factory=BuilderConfig)

    def validate(self) -> None:
        bad = [s for s in self.attack.sides if s not in SIDES]
        if bad:
            raise ValueError(f"unknown attack side(s) {bad}; use {SIDES}")
        if not self.attack.sides:
            raise ValueError("attack.sides needs at least one side")
        if self.device.mode not in ("bluestacks", "ldplayer", "manual"):
            raise ValueError('device.mode must be "bluestacks", "ldplayer" or "manual"')
        bad_kinds = [k for k in self.bot.stop_when_full if k not in RESOURCES]
        if bad_kinds:
            raise ValueError(f"bot.stop_when_full: unknown {bad_kinds}; use {RESOURCES}")
        if self.bot.mode not in MODES:
            raise ValueError(f"bot.mode must be one of {MODES}")
        if self.bot.mode == "auto" and not self.bot.stop_when_full:
            raise ValueError('bot.mode "auto" needs bot.stop_when_full: the storages that trigger the switch')
        if self.bot.auto_first not in ("home", "builder"):
            raise ValueError('bot.auto_first must be "home" or "builder"')
        if self.bot.theme not in THEMES:
            raise ValueError(f"bot.theme must be one of {THEMES}")
        if self.schedule.daily_attacks_max and self.schedule.daily_attacks_min > self.schedule.daily_attacks_max:
            raise ValueError("schedule.daily_attacks_min can't be more than schedule.daily_attacks_max")
        if self.schedule.break_every_min > self.schedule.break_every_max:
            raise ValueError("schedule.break_every_min can't be more than schedule.break_every_max")
        if self.schedule.break_minutes_min > self.schedule.break_minutes_max:
            raise ValueError("schedule.break_minutes_min can't be more than schedule.break_minutes_max")
        if self.builder.cart_every_min > self.builder.cart_every_max:
            raise ValueError("builder.cart_every_min can't be more than builder.cart_every_max")
        if self.builder.walls_use not in WALL_RESOURCES:
            raise ValueError(f"builder.walls_use must be one of {WALL_RESOURCES}")
        if self.attack.spell_placement not in SPELL_PLACEMENTS:
            raise ValueError(f"attack.spell_placement must be one of {SPELL_PLACEMENTS}")
        if self.builder.first_hero not in BUILDER_HEROES:
            raise ValueError(f"builder.first_hero must be one of {BUILDER_HEROES}")
        if self.builder.corner not in BUILDER_CORNERS:
            raise ValueError(f"builder.corner must be one of {BUILDER_CORNERS}")
        if self.walls.use not in WALL_RESOURCES:
            raise ValueError(f"walls.use must be one of {WALL_RESOURCES}")
        if not 1 <= self.attack.waves <= 3:
            raise ValueError("attack.waves must be 1, 2 or 3")
        bad_hours = [h for h in self.schedule.active_hours if not 0 <= h <= 23]
        if bad_hours:
            raise ValueError(f"schedule.active_hours must be 0-23, not {bad_hours}")
        missing = [s for s in self.attack.sides if s not in self.attack.deploy_lines]
        if missing:
            raise ValueError(f"no deploy_lines entry for side(s) {missing}")


# Settings that existed once and were removed (the game dropped Quick Train;
# dead-base search is out for now; Clan Castle troops come with the siege machine): older farmbot.toml files may still have
# them, so they're skipped quietly.
REMOVED_SETTINGS = {"bot.quick_train", "bot.army_recipe", "clan.request_text", "search.dead_base",
                    "search.dead_base_collectors", "attack.use_cc",
                    "search.min_total", "search.dark_weight"}


# One unit each to donate became lists: older files' choices carry over.
DONATE_MOVED = {"clan.donate_troop": "donate_main", "clan.donate_spell": "donate_spells"}


def _merge(obj: Any, data: dict[str, Any], path: str) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if f"{path}{key}" == "schedule.daily_attack_limit":  # one number, from before the range
            obj.daily_attacks_min = obj.daily_attacks_max = int(value)
            continue
        if f"{path}{key}" == "clan.donate_siege" and isinstance(value, str):
            value = [value]  # one name, from before lists
        if f"{path}{key}" in DONATE_MOVED:
            from farmbot.units import BY_NAME, BY_SLUG

            unit = BY_SLUG.get(str(value).strip().lower()) or BY_NAME.get(str(value).strip())
            target = getattr(obj, DONATE_MOVED[f"{path}{key}"])
            if unit and unit.name not in target:
                target.append(unit.name)
            continue
        if f"{path}{key}" in REMOVED_SETTINGS:
            continue
        if key not in known:
            raise ValueError(f"unknown setting {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{path}{key} must be a table")
            _merge(current, value, f"{path}{key}.")
        else:
            setattr(obj, key, value)


PROFILE = Path(__file__).parent / "profiles" / "bluestacks-860x672.toml"


def ensure_config(path: str | Path) -> bool:
    """Give a new user a working ``farmbot.toml``: if ``path`` doesn't exist,
    write the starting profile there (made for the display COCFARM sets
    up). True if it was created."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PROFILE.read_bytes())
    return True


def _siege_out_of_main(clan: ClanConfig) -> None:
    """Siege machines picked among the main troops (before they had their own
    list) move to the siege list; old slugs ("battle_blimp") become names."""
    from farmbot.units import BY_NAME, BY_SLUG

    def is_siege(name: str) -> bool:
        return name in BY_NAME and BY_NAME[name].kind == "siege"

    siege = []
    for name in clan.donate_siege:
        unit = BY_SLUG.get(str(name).strip().lower()) or BY_NAME.get(str(name).strip())
        siege.append(unit.name if unit else name)
    siege += [n for n in clan.donate_main if is_siege(n)]
    clan.donate_main = [n for n in clan.donate_main if not is_siege(n)]
    clan.donate_siege = list(dict.fromkeys(siege))


def load_config(path: str | Path | None) -> Config:
    config = Config()
    if path is not None:
        with open(path, "rb") as fh:
            _merge(config, tomllib.load(fh), "")
    _siege_out_of_main(config.clan)
    config.validate()
    return config


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _toml_key(key: str) -> str:
    return key if key and all(c.isalnum() or c in "_-" for c in key) else _toml_value(key)


def dump_config(config: Config) -> str:
    """The whole config as TOML; it round-trips through :func:`load_config`."""
    lines: list[str] = []

    def table(name: str, data: dict[str, Any]) -> None:
        lines.append(f"[{name}]")
        lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in data.items() if not isinstance(v, dict))
        lines.append("")
        for key, sub in data.items():
            if isinstance(sub, dict):
                table(f"{name}.{_toml_key(key)}", sub)

    for name, data in asdict(config).items():
        table(name, data)
    return "\n".join(lines)


def save_config(config: Config, path: str | Path) -> None:
    """Write ``config`` to ``path``. An existing file is edited in place:
    only changed values are rewritten, so its comments and layout stay."""
    config.validate()
    path = Path(path)
    text = None
    if path.is_file():
        try:
            text = _updated_toml(path.read_text(encoding="utf-8"), load_config(path), config)
        except (ValueError, TypeError, tomllib.TOMLDecodeError):
            text = None  # something the simple editor can't handle: write it all out
    # Keep the file's own line endings (Python would write CRLF on Windows).
    ending = "\r\n" if path.is_file() and b"\r\n" in path.read_bytes() else "\n"
    path.write_text(text if text is not None else dump_config(config), encoding="utf-8", newline=ending)


def _leaves(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_leaves(value, prefix + (key,)))
        else:
            out[prefix + (key,)] = value
    return out


def _updated_toml(text: str, saved: Config, config: Config) -> str | None:
    """``text`` (which loads as ``saved``) edited to load as ``config``;
    ``None`` if it can't be done line by line."""
    before, after = _leaves(asdict(saved)), _leaves(asdict(config))
    if set(before) - set(after):
        return None  # an entry was removed from a table such as picture_scales
    changes = {k: v for k, v in after.items() if before.get(k) != v}
    lines = text.splitlines()
    for path, value in changes.items():
        _set_line(lines, path[:-1], path[-1], value)
    result = "\n".join(lines) + "\n"
    check = Config()
    _merge(check, tomllib.loads(result), "")
    return result if check == config else None


def _table_of(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")) or stripped.startswith("[["):
        return None
    path, node = [], tomllib.loads(stripped)
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return tuple(path)


def _key_of(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "[")):
        return None
    parsed = tomllib.loads(stripped)  # a value spread over several lines raises: fall back
    if len(parsed) != 1 or isinstance(next(iter(parsed.values())), dict):
        raise ValueError("dotted keys are not edited in place")
    return next(iter(parsed))


def _set_line(lines: list[str], table: tuple[str, ...], key: str, value: Any) -> None:
    entry = f"{_toml_key(key)} = {_toml_value(value)}"
    current, start, last = None, None, None
    for i, line in enumerate(lines):
        header = _table_of(line)
        if header is not None:
            current = header
            if header == table:
                start = last = i
            continue
        if current == table and _key_of(line) is not None:
            last = i
            if _key_of(line) == key:
                lines[i] = entry
                return
    if start is None:
        lines.extend(["", "[" + ".".join(_toml_key(k) for k in table) + "]", entry])
    else:
        lines.insert(last + 1, entry)
