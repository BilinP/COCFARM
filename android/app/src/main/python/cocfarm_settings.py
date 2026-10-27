"""COCFARM's settings on Android: what the settings screen shows, read and
saved with the same rules as the Windows window (farmbot.form). The Java
side (SettingsActivity) draws whatever this describes, so a setting is
added here, once.

Each item: {"path", "kind", "title", "help"}, plus per kind:
  amount / number (+ "suffix") / text: a box to type in
  switch: on/off;  speed: 1 (slow) to 9 (fast)
  choice: "options" {value: label};  multi: "options", several can be picked
  ordered: like multi, but the order they're picked in counts (donations)
  hours: the 24 hours;  range: two numbers, "low" and "high" paths
and "depends": {"path": ..., "is": value or [values]} greys it out otherwise.
"""

from __future__ import annotations

import json
import os

from farmbot import __version__, catalog
from farmbot.config import RESOURCES, load_config, save_config
from farmbot.form import SIDE_LABELS, apply_form, form_values, validate
from farmbot.units import SIEGE_MACHINES, SPELLS, TROOPS

SETTINGS_FILE = "farmbot.toml"
MONEY = {"gold": "Gold", "elixir": "Elixir", "both": "Either"}

# A phone's first farmbot.toml. Not the Windows one (made for an 860 x 672
# emulator): every phone screen is different, so the bot measures the size
# of the game's buttons itself, and everything else starts at the defaults.
PHONE_PROFILE = """\
# COCFARM settings for this phone, written the first time it runs. Change
# them in the app's Settings, or here.

[device]
ui_scale = 0.0  # measured from the game's buttons on this screen
window_scale = 0.0

# Once screenshots are scaled to the buttons' size, the game's corners line
# up as they do on the PC display (measured at 2400 x 1080).
[regions]
own_gold = [-215, 25, 150, 32]  # wide enough for 9 digits
own_elixir = [-215, 78, 150, 32]
own_dark = [-160, 130, 95, 32]
# Left (empty) end of each resource bar: only coloured when that storage is full.
full_gold = [-284, 33, 24, 14]
full_elixir = [-284, 83, 24, 14]
full_dark = [-215, 133, 24, 14]
loot_gold = [63, 92, 150, 34]
loot_elixir = [63, 134, 150, 34]
loot_dark = [63, 175, 150, 34]

[bar]
band = [0, -130, 0, 110]
card_width = 96
"""


def ensure_settings(folder: str) -> str:
    """The settings file's path, written for a phone if there isn't one yet."""
    path = os.path.join(folder, SETTINGS_FILE)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PHONE_PROFILE)
    return path


def _item(path, kind, title, help_text="", **extra):
    return {"path": path, "kind": kind, "title": title, "help": help_text, **extra}


def _depends(path, value=True):
    return {"path": path, "is": value}


PAGES = [
    {"title": "Farming", "groups": [
        {"title": "What to farm", "items": [
            _item("bot.mode", "choice", "Village", "Which village the bot plays. Auto switch: one village until its "
                  "storages are full, then the other.",
                  options={"home": "Home village", "builder": "Builder base", "auto": "Auto switch"}),
            _item("bot.auto_first", "choice", "Start with", "Auto switch farms this village first, then the other "
                  "one once the storages below are full. It stops when both are full.",
                  options={"home": "Home village", "builder": "Builder base"}, depends=_depends("bot.mode", "auto")),
            _item("bot.stop_when_full", "multi", "Stop when these storages are full", "It stops once every one you "
                  "picked is full; with Auto switch it moves on to the other village first. None: never stops for "
                  "this.", options={k: k.title() if k != "dark" else "Dark elixir" for k in RESOURCES}),
        ]},
        {"title": "Alerts on Discord", "items": [
            _item("notify.discord_webhook", "text", "Discord webhook", "Server Settings → Integrations → Webhooks → "
                  "New Webhook → Copy Webhook URL, then paste it here."),
            _item("notify.on_storage_full", "switch", "When storage is full"),
            _item("notify.on_stopped", "switch", "When the bot stops", "After a run, or when a problem stops it."),
            _item("notify.daily_summary", "switch", "Daily summary", "Yesterday's attacks and loot, after midnight."),
            _item("", "button", "Send a test alert", "Posts a message to the webhook, to check it works.",
                  action="test_alert"),
        ]},
        {"title": "COCFARM", "items": [
            _item("bot.check_updates", "switch", "Check for updates",
                  "Looks for a new version on GitHub now and then (at most hourly) and offers it on the home screen."),
            _item("", "button", "Check now", "", action="check_update"),
        ]},
    ]},
    {"title": "Home village", "groups": [
        {"title": "Bases to attack", "items": [
            _item("search.maintain_only", "switch", "Only look after the village",
                  "On: it only collects, donates and looks after the village, and never attacks."),
            _item("search.min_gold", "amount", "Gold", "Bases offering less gold are skipped. 0 ignores gold. "
                  "Accepts 500k or 1.5m."),
            _item("search.min_elixir", "amount", "Elixir", "Bases offering less elixir are skipped. 0 ignores it."),
            _item("search.min_dark", "amount", "Dark elixir", "Bases offering less dark elixir are skipped. 0 ignores it."),
            _item("search.match_any", "switch", "Any one is enough",
                  "On: attack when any one amount above is met. Off: all of them must be met."),
            _item("search.min_own_gold", "amount", "Don't search while my gold is below",
                  "Each Next costs gold. 0 always searches."),
            _item("search.sound_on_match", "switch", "Sound on match",
                  "Plays the notification sound when it finds a base worth attacking."),
            _item("search.max_searches", "number", "Give up searching after",
                  "Bases to skip before going back to the village and starting over.", suffix="bases"),
        ]},
        {"title": "Attacking", "items": [
            _item("attack.sides", "multi", "Deploy from", "Troops spread over the sides you pick.",
                  options=SIDE_LABELS),
            _item("attack.waves", "choice", "Waves", "Split each troop type into this many waves.",
                  options={"1": "1", "2": "2", "3": "3"}),
            _item("attack.troop_speed", "speed", "Troop speed", "How quickly single troops go down."),
            _item("attack.wave_speed", "speed", "Wave speed", "How quickly one wave follows another."),
            _item("attack.spells", "choice", "Spells to cast", "Bat Spells only: other spells stay in the bar.",
                  options={"bat": "Bat Spells only", "all": "All spells"}),
            _item("attack.spell_placement", "choice", "Spells go", "One spot: every spell at once, on one spot. "
                  "Spread: over the base.", options={"together": "One spot", "spread": "Spread"}),
            _item("attack.spell_spot", "choice", "The spot", "Middle: the centre of the base. Random: a new spot "
                  "each battle, within the middle half of the base.", options={"middle": "Middle", "random": "Random"},
                  depends=_depends("attack.spell_placement", "together")),
            _item("attack.use_siege", "switch", "Use siege machines", "Off: siege machines stay in the bar (and the "
                  "Clan Castle troops riding in them with them)."),
            _item("attack.hero_abilities", "switch", "Use hero abilities",
                  "Fires each hero's special ability once during the battle."),
            _item("attack.hero_ability_delay", "number", "Ability after", "Seconds after the heroes land.",
                  suffix="s", depends=_depends("attack.hero_abilities")),
            _item("attack.ability_health", "number", "Or when health drops to", "Fires a hero's ability once it's "
                  "this hurt instead. 0% uses the time.", suffix="%", depends=_depends("attack.hero_abilities")),
            _item("attack.ability_max_wait", "number", "At the latest after", "Heroes still unhurt by then fire anyway.",
                  suffix="s", depends=_depends("attack.hero_abilities")),
            _item("attack.speed_up", "switch", "Play the last minute at 4x",
                  "Presses the 1x button when it shows, with a minute of the battle left."),
            _item("attack.end_when_no_loot", "switch", "Leave when the loot stops",
                  "Ends the battle early once no more loot is coming in."),
            _item("attack.no_loot_seconds", "number", "No loot for", "Seconds without any loot before leaving.",
                  suffix="s", depends=_depends("attack.end_when_no_loot")),
            _item("attack.end_at_loot_share", "number", "Leave once this much loot is taken",
                  "Share of the base's loot to take before leaving. 0 waits for the rules above.", suffix="%"),
            _item("redline.enabled", "switch", "Follow the red line",
                  "Deploy along the thin boundary the game draws around a base."),
            _item("zone.enabled", "switch", "Otherwise deploy around the buildings",
                  "Off: use the fixed deploy lines from farmbot.toml."),
        ]},
        {"title": "Village", "items": [
            _item("bot.collect_resources", "switch", "Collect resources",
                  "Empties full collectors between attacks: one tap on a bubble collects them all."),
            _item("bot.collect_gold", "switch", "Gold", "From the gold mines.", depends=_depends("bot.collect_resources")),
            _item("bot.collect_elixir", "switch", "Elixir", "From the elixir collectors.",
                  depends=_depends("bot.collect_resources")),
            _item("bot.collect_dark", "switch", "Dark elixir", "From the dark elixir drills.",
                  depends=_depends("bot.collect_resources")),
            _item("bot.collect_loot_cart", "switch", "Collect the loot cart",
                  "Empties the loot cart when it shows \"Collect\"."),
            _item("walls.enabled", "switch", "Upgrade walls with spare loot",
                  "Between attacks it upgrades the cheapest suggested wall. Off by default: it spends loot."),
            _item("walls.use", "choice", "Pay with", "Which resource may go on walls.", options=MONEY,
                  depends=_depends("walls.enabled")),
            _item("walls.min_gold", "amount", "Keep at least this much gold", "Walls only get what's above it.",
                  depends=_depends("walls.enabled")),
            _item("walls.min_elixir", "amount", "Keep at least this much elixir", "Walls only get what's above it.",
                  depends=_depends("walls.enabled")),
            _item("walls.max_per_round", "number", "Walls per round", "At most this many between two attacks.",
                  depends=_depends("walls.enabled")),
        ]},
        {"title": "Clan", "items": [
            _item("clan.donate", "switch", "Donate to requests", "Fills every open request in clan chat. Each unit "
                  "costs the elixir or dark elixir shown; it never pays with gems."),
            _item("clan.donate_main", "ordered", "Main troops", "Pick the ones to give first, in order: they go "
                  "first, as many as fit.", options={u.name: u.name for u in TROOPS}),
            _item("clan.donate_siege", "ordered", "Siege machines", "In order. A request has room for one.",
                  options={u.name: u.name for u in SIEGE_MACHINES}),
            _item("clan.donate_spells", "ordered", "Main spells", "The spells to give, in order.",
                  options={u.name: u.name for u in SPELLS}),
            _item("clan.donate_fill", "switch", "Fill the rest", "After the main troops, fills the space left with "
                  "the biggest troops that still fit.", depends=_depends("clan.donate")),
            _item("clan.max_donations_per_request", "number", "Up to", "Units given to one request at most.",
                  suffix="units", depends=_depends("clan.donate")),
            _item("clan.request_troops", "switch", "Request Clan Castle troops",
                  "Asks the clan for troops, then again as soon as the game lets you."),
            _item("clan.request_every_minutes", "number", "Request again after",
                  "Minutes between requests when the Request button's timer can't be read.", suffix="min",
                  depends=_depends("clan.request_troops")),
        ]},
    ]},
    {"title": "Builder base", "groups": [
        {"title": "Attacking", "items": [
            _item("builder.corner", "choice", "Go in from", "Auto picks the map corner farthest from the enemy "
                  "buildings.", options={"auto": "Auto", "left": "Left", "top": "Top", "right": "Right"}),
            _item("builder.use_hero", "switch", "Hero goes first", "Off: troops only."),
            _item("builder.troop_speed", "speed", "Troop speed", "How quickly the troops go down, one after another."),
            _item("builder.troop_delay", "number", "Troops follow after", "Seconds after the hero lands.", suffix="s"),
            _item("builder.first_hero", "choice", "First hero", "The hero for the first base. Keep: whichever is set "
                  "in the game.", options={"any": "Keep", "machine": "Battle Machine", "copter": "Battle Copter"}),
            _item("builder.switch_hero", "switch", "Switch hero when hurt",
                  "Before a base, swaps to your other builder hero if this one's health is low."),
            _item("builder.switch_below", "number", "Switch below", "Hero health that triggers the swap.", suffix="%",
                  depends=_depends("builder.switch_hero")),
            _item("builder.hero_ability", "switch", "Hero ability", "Fired once, when it has charged."),
            _item("builder.hero_ability_after", "number", "Fire it after",
                  "Seconds after the hero lands: how long its ability takes to charge.", suffix="s",
                  depends=_depends("builder.hero_ability")),
            _item("builder.troop_abilities", "switch", "Troop abilities",
                  "Taps the cards of troops still on the field, a few at a time."),
            _item("builder.ability_delay", "number", "Between taps", "Seconds from one round of ability taps to the "
                  "next.", suffix="s", depends=_depends("builder.troop_abilities")),
            _item("builder.ability_batch", "number", "Troop abilities per tap round", "How many troop cards are "
                  "tapped together.", depends=_depends("builder.troop_abilities")),
            _item("builder.stage_delay", "number", "Wait before the second base",
                  "Seconds after the first base falls, while the second loads.", suffix="s"),
            _item("builder.speed_up", "switch", "Play at 4x speed", "Presses the 1x button whenever it shows."),
            _item("builder.return_home_after", "switch", "Sail home when the bot stops",
                  "Ends in the home village rather than the builder base."),
        ]},
        {"title": "Village", "items": [
            _item("builder.collect_resources", "switch", "Collect the gold mines, elixir collectors and gem mine",
                  "Before each builder battle."),
            _item("builder.collect_cart", "switch", "Collect the elixir cart",
                  "The cart by the dock that holds your defense rewards."),
            _item("", "range", "Every", "A random number of battles in this range each time.",
                  low="builder.cart_every_min", high="builder.cart_every_max", suffix="battles",
                  depends=_depends("builder.collect_cart")),
            _item("builder.walls", "switch", "Upgrade builder base walls with spare loot",
                  "Before each battle it upgrades the cheapest wall. Off by default: it spends loot."),
            _item("builder.walls_use", "choice", "Pay with", "Which resource may go on walls.", options=MONEY,
                  depends=_depends("builder.walls")),
            _item("builder.walls_min_gold", "amount", "Keep at least this much gold", "Walls only get what's above "
                  "it.", depends=_depends("builder.walls")),
            _item("builder.walls_min_elixir", "amount", "Keep at least this much elixir", "Walls only get what's "
                  "above it.", depends=_depends("builder.walls")),
            _item("builder.walls_per_round", "number", "Walls per round", "At most this many between two battles.",
                  depends=_depends("builder.walls")),
        ]},
    ]},
    {"title": "When to play", "groups": [
        {"title": "Playing hours", "items": [
            _item("schedule.enabled", "switch", "Only play at certain hours",
                  "Off: plays whenever it's running. On: only in the hours you pick (this device's time)."),
            _item("schedule.active_hours", "hours", "Hours", "", depends=_depends("schedule.enabled")),
        ]},
        {"title": "Breaks", "items": [
            _item("schedule.breaks", "switch", "Take breaks", "Rests now and then, at random, to play more like a person."),
            _item("", "range", "Every", "A new random number of attacks after each break.",
                  low="schedule.break_every_min", high="schedule.break_every_max", suffix="attacks",
                  depends=_depends("schedule.breaks")),
            _item("", "range", "For", "Minutes, picked at random.", low="schedule.break_minutes_min",
                  high="schedule.break_minutes_max", suffix="min", depends=_depends("schedule.breaks")),
        ]},
        {"title": "When to stop", "items": [
            _item("", "range", "Attacks per day", "Picks a number in this range each day, then waits for the next "
                  "day. 0 to 0 = no limit.", low="schedule.daily_attacks_min", high="schedule.daily_attacks_max",
                  suffix="attacks"),
        ]},
    ]},
]


def _paths() -> list[str]:
    out = []
    for page in PAGES:
        for group in page["groups"]:
            for item in group["items"]:
                if item["kind"] == "range":
                    out += [item["low"], item["high"]]
                elif item["kind"] != "button":  # a button does something; it has no value
                    out.append(item["path"])
    return out


def _json(value):
    return value if isinstance(value, (bool, int, float, str, list, type(None))) else str(value)


def _ordered(pages: list) -> list:
    """Options as [value, label] pairs: the screen keeps their order (a JSON
    object's keys may come out in any order)."""
    return [{**page, "groups": [{**group, "items": [
        {**item, "options": list(item["options"].items())} if "options" in item else item
        for item in group["items"]]} for group in page["groups"]]} for page in pages]


def info(folder: str) -> str:
    """For the home screen: COCFARM's version, and the button pictures still missing."""
    from farmbot.vision import TemplateLibrary

    cfg = load_config(ensure_settings(folder))
    library = TemplateLibrary(os.path.join(folder, cfg.bot.templates))
    missing = [] if cfg.search.maintain_only else catalog.missing(library, required_only=True)
    return json.dumps({"version": __version__, "missing": [p.title for p in missing]})


UPDATE_ASSET = "COCFARM-android.apk"  # the Android app, attached to COCFARM's GitHub releases
UPDATE_DIR = "_update"


def history_summary(folder: str) -> str:
    """The attack history for the History screen: totals for today, the last
    7 days and all time, gains per hour, the last 14 days, and recent attacks."""
    from datetime import date, timedelta

    from farmbot import history
    from farmbot.tasks.search import Loot

    cfg = load_config(ensure_settings(folder))
    rows = history.read_all(os.path.join(folder, cfg.bot.history)) if cfg.bot.history else []
    days = history.daily_totals(rows)
    today = date.today()

    def total(since):
        count, loot = 0, Loot(0, 0, 0)
        for day, (n, gained) in days.items():
            if since is None or day >= since:
                count, loot = count + n, history.add(loot, gained)
        return {"attacks": count, "gained": [loot.gold, loot.elixir, loot.dark]}

    hourly = history.per_hour([r for r in rows if r.time.date() >= today - timedelta(days=6)])
    chart = []
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        n, gained = days.get(day, (0, Loot(0, 0, 0)))
        chart.append({"day": day.isoformat(), "attacks": n, "gained": [gained.gold, gained.elixir, gained.dark]})
    recent = [{"time": r.time.isoformat(timespec="minutes"), "target": [r.target.gold, r.target.elixir, r.target.dark],
               "gained": [r.gained.gold, r.gained.elixir, r.gained.dark] if r.gained else None}
              for r in reversed(rows[-100:])]
    return json.dumps({"today": total(today), "week": total(today - timedelta(days=6)), "all": total(None),
                       "hour": [hourly.gold, hourly.elixir, hourly.dark] if hourly else None,
                       "days": chart, "recent": recent})


def test_alert(folder: str) -> str:
    """Send a test message to the Discord webhook: what happened, in a few words."""
    from farmbot.notify import Notifier

    notifier = Notifier(load_config(ensure_settings(folder)).notify)
    if not notifier.configured:
        return "Paste a Discord webhook first."
    return "✓ Sent: check the channel." if notifier.test() else "✗ Discord didn't take it. Check the webhook."


def look_at_base(folder: str) -> str:
    """What the bot made of the last base it scouted: lines of text, and the
    picture with what it found drawn on (saved next to it)."""
    import cv2

    from farmbot.inspect import describe
    from farmbot.vision import TemplateLibrary, Vision

    cfg = load_config(ensure_settings(folder))
    screen = cv2.imread(os.path.join(folder, "cache", "base.png"))
    if screen is None:
        return json.dumps({"lines": ["No base yet: start farming, and the last base it scouts shows here."]})
    vision = Vision(TemplateLibrary(os.path.join(folder, cfg.bot.templates)), cfg.bot.threshold)
    lines, overlay = describe(screen, cfg, vision)
    cv2.imwrite(os.path.join(folder, "cache", "seen.jpg"), overlay, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return json.dumps({"lines": lines, "picture": os.path.join(folder, "cache", "seen.jpg")})


def check_update(folder: str, force: bool = False) -> str:
    """Is a newer COCFARM for Android out? (GitHub is asked at most hourly,
    or once a minute when ``force``.)"""
    from pathlib import Path

    from farmbot import update

    cfg = load_config(ensure_settings(folder))
    if not (cfg.bot.check_updates or force):
        return json.dumps({"version": None, "note": "Update checks are off."})
    found = update.check(cfg.bot.update_repo, force=force, state_path=Path(folder) / "update.json", asset=UPDATE_ASSET)
    release = found.release
    if release and not release.download:  # a release for Windows only
        release = None
    return json.dumps({"version": release.version if release else None, "note": found.note,
                       "page": release.url if release else ""})


def download_update(folder: str) -> str:
    """Download the newer app (resuming a part download) and check it: its path."""
    from pathlib import Path

    from farmbot import update

    cfg = load_config(ensure_settings(folder))
    found = update.check(cfg.bot.update_repo, state_path=Path(folder) / "update.json", asset=UPDATE_ASSET)
    if not found.release or not found.release.download:
        raise update.UpdateError("There's no newer version to download.")
    from java import jclass

    bridge = jclass("com.cocfarm.app.Bridge")
    path = update.download(found.release, Path(folder) / UPDATE_DIR, suffix=".apk",
                           progress=lambda done, size: bridge.updateProgress(int(done), int(size)))
    return str(path)


def load(folder: str) -> str:
    """The settings screen, with this device's current values, as JSON."""
    values = form_values(load_config(ensure_settings(folder)))
    return json.dumps({"pages": _ordered(PAGES), "values": {p: _json(values[p]) for p in _paths()}})


def save(folder: str, values_json: str) -> str:
    """Check the values the screen sends; save them if they're all right.
    Returns {path: message} for each mistake (empty once saved). A running
    bot gets the new values too."""
    form = json.loads(values_json)
    _, errors = validate(form)
    if errors:
        return json.dumps(errors)
    path = ensure_settings(folder)
    config = load_config(path)
    apply_form(config, form)
    save_config(config, path)
    import cocfarm_android

    if cocfarm_android.running_config is not None:
        apply_form(cocfarm_android.running_config, form)
    return "{}"
