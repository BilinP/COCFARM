"""Main farming loop."""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Callable

from farmbot import accounts, diag, history, human, imagefile, instances, language, screens, townhall
from farmbot.timings import Timings
from farmbot.notify import Notifier
from farmbot.config import Config, search_minimums_for, unreachable_minimums, unreachable_note
from farmbot.device import DeviceError, GameNotInstalled, GaveUp
from farmbot.game import Game, Stopped, T
from farmbot.history import AttackRecord
from farmbot.tasks import attack, browse, builder, capital, clan, export, ranked, search, super_troops, upgrade, village, war
from farmbot.tasks.search import Loot

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3  # failed rounds in a row, the game restarted after each, before the bot stops
# Extras (collecting, the clan, the Clan Capital, upgrades; the builder base
# in mode "auto") failing this many rounds in a row are left alone a while,
# and the farm goes on attacking: only the battles are the farm.
EXTRA_TRIES = 2
MAINTENANCE_LOOK = 5 * 60  # seconds between looks while the game's on its maintenance break
MAINTENANCE_MOST = timedelta(hours=6)  # waited out at most: longer, and something else is wrong
EXTRA_PAUSE = timedelta(hours=2)
WAR_LOOK = timedelta(minutes=30)  # between looks at the clan war (battle day may have started)
RANKED_LOOK = timedelta(hours=1)  # between looks for ranked attacks, once there are none to make
TOWN_HALL_ROUNDS = 3  # rounds in a row with full storages and nothing to upgrade, before resting for the Town Hall
TOWN_HALL_LOOK = timedelta(hours=2)  # the rest is cut into these: a potion or a book ends the upgrade sooner
EXTRA_NAMES = {"_collect": "collecting", "_loot_cart": "the loot cart", "_gravestones": "gravestones",
               "_clan_visit": "the clan (donations, requests)", "_capital": "Clan Capital raids",
               "_upgrades": "upgrades", "builder": "the Builder Base", "_browse": "looking around the game",
               "ranked_attacks": "ranked battles", "war_attacks": "clan war attacks"}
ERROR_SCREENS_KEPT = 200  # newest failed-round screens kept; older ones are deleted
STORAGE_MAX_TRIES = 3  # readings a day of what a village's storages hold, until one works
STORAGE_RECHECK = 3600.0  # seconds: a storage that looks full is measured again before spending, at most this often
IDLE_WAIT = 60.0  # seconds between village rounds when not attacking
STAR_BONUS_WAIT = 4.0  # seconds, after an attack, for the Star Bonus window to open over the village
STARTUP_TIMEOUT = 90  # seconds to wait for the home village after starting the game
# An emulator answers ADB before Android has finished starting up, and a game
# started then is closed again: start it once Android has been up this long.
BOOT_SETTLE = 20.0
START_TRIES = 3  # starts of the game, if it closes while loading
BLOCKED_WAIT = 180  # seconds the game stays closed after "Unable to attack Village!", at least...
BLOCKED_WAIT_MAX = 300  # ...and at most (a different wait each time)


class RoundsFailed(RuntimeError):
    """Rounds kept failing though the game was restarted after each: the bot stops."""


class SameAccount(RuntimeError):
    """Another farm already plays the account this emulator's game opened:
    two farms on one account log each other out, so this one stops."""


VILLAGE_TRIES = 2  # a day, for the village export (failed reads in a row)
YOUNG_REREAD = timedelta(hours=2)  # a village still unlocking things is read again this often
YOUNG_LOCKS = {"research", "heroes", "pets", "clan"}  # what a new building or Town Hall level unlocks


class Bot:
    def __init__(
        self,
        game: Game,
        config: Config,
        on_status: Callable[[str], None] | None = None,
        on_base: Callable[[Loot, bool], None] | None = None,
        now: Callable[[], datetime] = datetime.now,
        on_resources: Callable[[Loot], None] | None = None,
        on_attack: Callable[[AttackRecord], None] | None = None,
        on_builder: Callable[[dict], None] | None = None,
        pause_event: threading.Event | None = None,
        reopen_device: Callable[[], object] | None = None,
        notifier: Notifier | None = None,
        on_storage_max: Callable[[str, dict[str, int]], None] | None = None,
        on_account: Callable[[object, object], str | None] | None = None,
        on_village: Callable[[str | None, townhall.Village], None] | None = None,
        village_path: Callable[[str | None], object] | None = None,
    ):
        self.game = game
        game.habits = config.human.active()
        self.config = config
        self.timings = Timings()  # what the human-like habits add to a round: typical numbers
        home = getattr(config, "home", None)
        # The game's village export (what the account has unlocked), kept
        # beside the profile: ``village_path(profile)`` says where.
        self._on_village = on_village
        self._village_path = village_path or (
            (lambda name: instances.resolve(config, townhall.VILLAGE_FILE)) if home else (lambda name: None))
        self.village: townhall.Village | None = None
        self._village_tries: dict[str | None, tuple[str, int]] = {}
        self._village_read_at: dict[str | None, datetime] = {}  # this session's last reading, per account
        self._said_locked: set[str] = set()
        self._edges: tuple[date, int, int] | None = None  # today's playing-hours edges (odd_hours)
        game.package = config.device.package  # taps stop if the game crashes to the emulator's home screen
        self.attacks = 0
        self.searched = 0
        self.errors = 0
        self._on_status = on_status
        self._on_base = on_base
        self._now = now
        self._today: date | None = None
        self.attacks_today = 0
        self._daily_limit: int | None = None  # today's attack limit, picked at random in the range
        self._next_request: float | None = None  # clock time the next troop request is due
        self._in_clan: bool | None = None  # checked once per run, in the chat
        self._on_resources = on_resources
        # Profiles: given the player name's picture, puts that account's
        # settings in place and returns its profile (None: not sure).
        self._on_account = on_account
        self._account: str | None = None
        self._account_mask = None  # the name seen when the profile was last picked
        self._on_attack = on_attack
        self._on_builder = on_builder
        self._on_storage_max = on_storage_max
        self.pause_event = pause_event or threading.Event()  # set: wait at the next safe point
        self.resources: Loot | None = None  # last reading of your resource bar
        self.gained = Loot(0, 0, 0)
        # This run's work besides battles, for the summary sent when it stops.
        self.session: Counter[str] = Counter()
        self._session_start: datetime | None = None
        self._progress_at = 0  # attacks at the last progress update (notify.progress_every)
        self._pending: AttackRecord | None = None  # attack waiting for its "after" reading
        self.storage_full = False  # set when the storages in bot.stop_when_full are all full
        # Mode "auto": the village being farmed now, and the ones whose storages are already full.
        self.auto_village: str | None = None
        self.full_villages: set[str] = set()
        # Mode "auto": no boat to sail to the builder base (it isn't repaired yet), so home only.
        self._no_boat = False
        self._maintenance_since: datetime | None = None  # when this maintenance break was first seen
        # Resting while the Town Hall upgrades: what this round's upgrades found, how many walls went up,
        # the rounds in a row with nothing to spend on, and whether a rest was taken.
        self._upgrade_idle = False
        self._round_walls = 0
        self._idle_rounds = 0
        self._town_hall_rested = False
        self._town_hall_asked: datetime | None = None  # when the village data was last asked for, for its timer
        self._extra_fails: Counter = Counter()  # extras' failed rounds in a row, by name
        self._extra_paused: dict[str, datetime] = {}  # extras left alone until then
        self._since_cart = 0  # builder battles since the elixir cart was last emptied
        self._since_collect: int | None = None  # builder battles since the collectors were emptied
        self._since_home_collect: int | None = None  # home village rounds since the collectors were emptied
        self._cart_due: int | None = None  # battles between cart visits, picked at random
        self._clock_off = False  # set if a Clock Tower boost ever cost gems
        # Watchdog: restarting the game is tried first; if the errors carry on
        # (or ADB itself fails), the emulator is closed and opened again.
        self._reopen_device = reopen_device
        self.notifier = notifier or Notifier(config.notify)
        if not self.notifier.who and config.profiles.active:
            self.notifier.who = config.profiles.active
        self._since_break = 0  # attacks since the last break
        self._walls_known: dict = {}  # what the wall upgrades learnt (the price), per account
        self._builder_walls_known: dict = {}
        self._capital_after: datetime | None = None  # when to look for Clan Capital raids again
        self._ranked_after: datetime | None = None  # ...and for ranked attacks
        self._war_after: datetime | None = None  # ...and at the clan war
        # Upgrades, last before a battle, leave what they upgraded selected
        # (the battle drops it); if no battle came, it's dropped next round.
        self._left_selected = False
        self._left_zoomed_in = False  # the same: the battle's view needs zooming out
        self._warned_minimums: dict[str, int] = {}  # loot minimums no base meets, once logged
        self._break_due: int | None = None  # attacks between breaks, picked at random
        # The human-like habits that span rounds (farmbot/human.py):
        self._closed_for: str | None = None  # the pause the game was closed for ("Close the game when not playing")
        self._fresh_at: float | None = None  # the clock when it last was fresh: the start, or a break's end
        self._collect_due: int | None = None  # rounds until the collectors are emptied again
        self._since_browse = 0  # rounds since it last looked around the game
        self._browse_due: int | None = None
        # What each village's storages hold (config.storage) is read the first
        # time the bot is in that village each day (upgraded storages hold
        # more); a failed reading is tried again the next rounds, a few times.
        self._storage_max_reads: dict[str, tuple[date, int, bool]] = {}  # village -> (day, tries, read)
        self._storages_checked: dict[str, float] = {}  # village -> clock: measured last

    def check_account(self) -> bool:
        """With profiles on, after ``screens.settle``: if another account
        is in the game, switch to its profile. True if the settings changed."""
        if self._on_account is None:
            return False
        mask = accounts.name_mask(self.game.screen)
        if mask is None:
            return False
        if self._account_mask is not None and accounts.likeness(mask, self._account_mask) >= accounts.SAME:
            return False  # the same account as before
        self.game.wait(1)  # a name half drawn, or a window opening over it, reads differently a second later
        again = accounts.name_mask(self.game.refresh())
        if again is None or accounts.likeness(mask, again) < accounts.SAME:
            return False
        name = self._on_account(mask, accounts.name_picture(self.game.screen))
        if name is None:
            return False
        self._account_mask = mask
        self._claim(name)
        if name == self._account:
            return False
        first, self._account = self._account is None, name
        log.info("Account: profile %s", name)
        self.notifier.who = name
        self.status(f"Playing with profile {name}")
        if not first:  # what the bot learnt about the last account
            self.resources = None
            self.storage_full = False
            self.auto_village = None
            self.full_villages = set()
            self._no_boat = False
            self._idle_rounds, self._town_hall_rested, self._town_hall_asked = 0, False, None
            self._storage_max_reads = {}
            self._storages_checked = {}
            self._since_collect = self._since_home_collect = None
            self._in_clan = self._next_request = self._capital_after = self._ranked_after = self._war_after = None
            self._walls_known, self._builder_walls_known = {}, {}
            self.village, self._said_locked = None, set()
        return True

    def _claim(self, name: str) -> None:
        """This farm plays account ``name`` (read in the game): unless another
        farm already does. Then this one closes the game, which hands the
        account back to that farm, and stops."""
        from farmbot import instances

        other = instances.claim_account(name)
        if other is None:
            return
        log.error("Farm %d is already playing %s: closing the game here so it keeps the account", other, name)
        try:
            self.game.device.stop_app(self.config.device.package)
        except Exception as exc:  # noqa: BLE001 - stopping anyway
            log.info("Couldn't close the game: %s", exc)
        raise SameAccount(f"farm {other} (in this or another COCFARM window) is already playing the account {name}, "
                          "and this emulator opened it too "
                          "(two farms on one account log each other out). Sign this emulator's game in to another "
                          "account, or farm it from one farm only.")

    def _check_account_first(self) -> bool:
        """Before the round: whose account the game opened, so another farm's
        is found straight away. True if the settings changed."""
        if self._on_account is None or self._account is not None:
            return False
        where = screens.settle(self.game)
        return where in (screens.Screen.HOME, screens.Screen.BUILDER) and self.check_account()

    def status(self, text: str) -> None:
        log.debug("Status: %s", text)
        self._doing = text  # (for a failure's log line: what the bot was at)
        if self._on_status:
            self._on_status(text)

    def _base_seen(self, loot: Loot, accepted: bool) -> None:
        self.searched += 1
        if self._on_base:
            self._on_base(loot, accepted)

    def checkpoint(self) -> None:
        """A safe point between actions (never mid-battle): while paused,
        wait here. Stop still works, since ``game.wait`` raises it."""
        if not self.pause_event.is_set():
            return
        log.info("Paused")
        self.status("Paused by you: press Resume to carry on")
        while self.pause_event.is_set():
            self.game.wait(0.5)
        log.info("Resumed")
        self.status("Resuming")

    def _rest(self, seconds: float) -> None:
        """Wait between rounds, cut short when Pause is pressed."""
        end = self.game.clock() + seconds
        while not self.pause_event.is_set() and self.game.clock() < end:
            self.game.wait(min(1.0, end - self.game.clock()), exact=True)

    def _rest_after_round(self, seconds: float) -> None:
        """The rest between rounds: a little longer some times than others
        ("Waits run a touch longer"), never shorter than set."""
        varied = human.pause(self.game.rng, seconds, self.game.uses("waits"), shortest=1.0)
        ledger = self.game.ledger
        ledger.begin(ledger.village, "rest")
        self.game.note("waits", human.pause_extra(seconds, shortest=1.0), varied - seconds)
        away = human.distraction(self.game.rng) if self.game.uses("distracted") else 0.0
        self.game.note("distracted", human.DISTRACTED_EXTRA, away)
        if away:
            log.info("Distracted: the next round starts %d s later", away)
        self._rest(varied + away)
        ledger.end()

    def _round_over(self, attacked: bool) -> None:
        """The round's ledger is dropped: the "Like a person" page shows
        typical times, not ones measured here (nothing is kept in a file)."""
        self.game.ledger.take()

    # -- schedule -------------------------------------------------------
    def pause_reason(self) -> str | None:
        """Why the bot should not play right now, or ``None``."""
        sched = self.config.schedule
        now = self._now()
        if self._today != now.date():
            if self._today is not None:
                self._daily_summary(self._today)
            self._today, self.attacks_today, self._daily_limit = now.date(), 0, None
        if human.day_off(self._seed(), now.date(), sched.day_off_every):
            return "a day off today (plays again tomorrow)"
        if not sched.enabled:
            return None
        if now.hour not in sched.active_hours:
            upcoming = next(((now.hour + i) % 24 for i in range(1, 25) if (now.hour + i) % 24 in sched.active_hours), None)
            return "outside active hours" + (f" (resumes {upcoming:02d}:00)" if upcoming is not None else "")
        edge = self._off_the_hour(now)
        if edge:
            return edge
        limit = self.daily_limit()
        if limit and self.attacks_today >= limit:
            return f"daily limit of {limit} attacks reached"
        return None

    def _off_the_hour(self, now: datetime) -> str | None:
        """"Start and stop off the hour": in the first hour of a stretch of
        playing hours, wait until today's minute; in the last, stop at
        today's minute before it ends. None: play."""
        sched = self.config.schedule
        hours = set(sched.active_hours)
        if not self.game.uses("odd_hours") or len(hours) >= 24:
            return None
        if self._edges is None or self._edges[0] != now.date():
            late, early = human.edge_minutes(self.game.rng)
            self._edges = (now.date(), late, early)
            log.info("Playing hours today: starting %d min into the hour, stopping %d min before it ends",
                     late, early)
        _, late, early = self._edges
        if (now.hour - 1) % 24 not in hours and now.minute < late:
            return f"playing hours start at {now.hour:02d}:{late:02d} today"
        if (now.hour + 1) % 24 not in hours and now.minute >= 60 - early:
            return f"playing hours ended at {now.hour:02d}:{60 - early:02d} today"
        return None

    def daily_limit(self) -> int:
        """Today's attack limit, picked at random in the range once a day (0 = none)."""
        sched = self.config.schedule
        if not sched.daily_attacks_max:
            return 0
        if self._daily_limit is None:
            low = max(1, min(sched.daily_attacks_min, sched.daily_attacks_max))
            self._daily_limit = max(1, round(self.game.rng.randint(low, sched.daily_attacks_max)
                                             * self._day_factor()))
            log.info("Attacks today: up to %d", self._daily_limit)
        return self._daily_limit

    def _seed(self) -> str:
        """What this farm's days are drawn from: its own folder, which never
        changes while it runs (the account isn't known before the first
        round), so each farm has its own days and keeps them."""
        return str(instances.resolve(self.config, "farm").parent)

    def _day_factor(self) -> float:
        """"Uneven days": today's factor (1 with the habit off)."""
        if not self.game.uses("uneven_days"):
            return 1.0
        return human.day_factor(self._seed(), self._now().date())

    def _go_offline(self, why: str) -> None:
        """"Close the game when not playing": close it for a break or a
        pause (once for each); the next round opens it again."""
        if not self.game.uses("offline_breaks") or self._closed_for == why:
            return
        self._closed_for = why
        log.info("Closing the game (%s)", why)
        try:
            self.game.device.stop_app(self.config.device.package)
        except DeviceError as exc:
            log.info("Couldn't close the game: %s", exc)
            return
        self._left_selected = self._left_zoomed_in = False  # (the game opens afresh)

    def _tire(self) -> None:
        """"Slow down over a session": how tired the hand is this round."""
        now = self.game.clock()
        if self._fresh_at is None:
            self._fresh_at = now
        on = self.game.uses("fatigue")
        self.game.tiredness = human.tiredness((now - self._fresh_at) / 60) if on else 1.0

    # -- game -----------------------------------------------------------
    def ensure_game_running(self) -> None:
        device, package = self.game.device, self.config.device.package
        if device.foreground_package() == package:
            return
        self._wait_for_android()
        for _ in range(START_TRIES):
            log.info("Starting %s", package)
            device.start_app(package)
            self.game.wait(5)
            # Loading takes 10-60 s depending on the PC: wait until the game
            # shows something known (a village, wherever it was left, or a
            # chest room or popup, which the round deals with next).
            deadline = self.game.clock() + STARTUP_TIMEOUT
            while (screen := screens.identify(self.game)) is screens.Screen.UNKNOWN:
                if self.game.clock() >= deadline:
                    log.info("Nothing known on screen after %ds; carrying on", STARTUP_TIMEOUT)
                    return
                if device.foreground_package() != package and not self._game_alive(package):
                    break  # it closed while loading
                self.game.wait(1)  # a look takes ~0.3 s: the village is seen soon after it shows
            else:
                log.info("Game loaded: %s", screen.value)
                return
            log.info("The game closed while loading; starting it again")

    def _game_alive(self, package: str) -> bool:
        """Whether the game's process still runs: another window (a system
        dialog) can have focus for a moment while it loads."""
        shell = getattr(self.game.device, "shell", None)
        if shell is None:
            return False
        try:
            return bool(shell("pidof", package, "||", "true").strip())
        except DeviceError:
            return False

    def _wait_for_android(self) -> None:
        """Until Android has finished starting up and been up ``BOOT_SETTLE``
        seconds (an emulator that was just opened)."""
        shell = getattr(self.game.device, "shell", None)
        if shell is None:
            return
        deadline = self.game.clock() + STARTUP_TIMEOUT
        try:
            while shell("getprop", "sys.boot_completed").strip() != "1" and self.game.clock() < deadline:
                self.game.wait(2)
            up = float(shell("cat", "/proc/uptime").split()[0])
        except (DeviceError, ValueError, IndexError):
            return
        if up < BOOT_SETTLE:
            log.info("Android started %ds ago: waiting %ds before starting the game", up, BOOT_SETTLE - up)
            self.status("Waiting for the emulator to finish starting up")
            self.game.wait(BOOT_SETTLE - up)

    def session_fields(self) -> list[tuple[str, str]]:
        """This run, beside its attacks and loot, for the message sent when
        it stops: how long it ran, and what it upgraded and boosted."""
        fields = []
        if self._session_start is not None:
            minutes = int((self._now() - self._session_start).total_seconds() // 60)
            fields.append(("⏱️ Ran for", f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"))
        for key, label in (("walls", "🧱 Walls upgraded"), ("upgrades", "🔨 Upgrades started"),
                           ("super troops", "⭐ Super troops boosted"), ("raids", "🏰 Capital raids"),
                           ("ranked", "🏆 Ranked attacks"), ("war", "⚔️ War attacks")):
            if self.session[key]:
                fields.append((label, f"{self.session[key]:,}"))
        return fields

    def _progress_update(self) -> None:
        """Every notify.progress_every attacks: the run so far, as the stop
        message has it."""
        every = self.config.notify.progress_every
        if every <= 0 or self.attacks - self._progress_at < every:
            return
        self._progress_at = self.attacks - self.attacks % every
        self.notifier.send(f"Progress · {self.attacks:,} attacks", "Still farming: the run so far.", kind="summary",
                           attacks=self.attacks, loot=self.gained, fields=self.session_fields())

    def _deploy_alert(self) -> None:
        """The last battle's army wouldn't go down (attack.attack set why)."""
        why, self.game.deploy_failed = getattr(self.game, "deploy_failed", None), None
        if why:
            self.notifier.alert("deploy_failed", "Troops won't deploy", f"{why}. If this keeps happening, "
                                "look at the emulator.", "on_deploy_failed", "warn",
                                attacks=self.attacks)

    def _daily_summary(self, day: date) -> None:
        if not self.config.bot.history:
            return
        count, gained = history.totals_for(instances.resolve(self.config, self.config.bot.history), day)
        text = "What COCFARM farmed yesterday." if count else "No attacks yesterday."
        self.notifier.send(f"Daily summary · {day:%a %d %b}", text, "daily_summary", "summary",
                           attacks=count, loot=gained)

    def take_break(self) -> None:
        """After a random number of attacks, rest a random number of minutes
        (schedule.breaks), as a person would."""
        sched = self.config.schedule
        if not sched.breaks:
            return
        rng = self.game.rng
        if self._break_due is None:
            # ("Uneven days": further apart on a day it plays more.)
            self._break_due = max(1, round(rng.randint(sched.break_every_min, sched.break_every_max)
                                           * self._day_factor()))
        if self._since_break < self._break_due:
            return
        minutes = rng.randint(sched.break_minutes_min, sched.break_minutes_max)
        log.info("Taking a %d-minute break after %d attacks", minutes, self._since_break)
        self.status(f"Taking a break: {minutes} min")
        self._since_break, self._break_due = 0, None
        self._go_offline("a break")
        self._rest(minutes * 60)
        self._closed_for = None
        self._fresh_at = self.game.clock()  # rested

    def restart_game(self) -> None:
        log.warning("Restarting the game after a failed round")
        self.game.device.stop_app(self.config.device.package)
        self.game.wait(3)
        self.ensure_game_running()

    def _wait_out_blocked(self, exc: Exception) -> None:
        """"Unable to attack Village! Try again later.": a battle left without
        surrendering still runs, and the game won't start another until it's
        over. Close the game, wait 3 to 5 minutes, start it again: the next
        round searches again from the village."""
        seconds = self.game.rng.uniform(BLOCKED_WAIT, BLOCKED_WAIT_MAX)
        log.warning("%s: closing the game for %.1f minutes [%s]", exc, seconds / 60, self._diagnostics())
        self._save_error_screen()
        self.status(f"Can't attack yet: the game is closed for {round(seconds / 60)} minutes")
        self.game.device.stop_app(self.config.device.package)
        self._rest(seconds)
        self.checkpoint()
        self.ensure_game_running()
        log.info("The game is back after the lockout: searching again")

    def _wait_out_maintenance(self, exc: Exception) -> None:
        """Supercell's maintenance break: the game is closed and looked at
        again every few minutes, until it opens (said once, not every look).
        Stops only after MAINTENANCE_MOST."""
        now = self._now()
        if self._maintenance_since is None:
            self._maintenance_since = now
            log.warning("%s: waiting it out, looking again every %d minutes", exc, MAINTENANCE_LOOK // 60)
            self._save_error_screen()
            self.notifier.send("Maintenance break", "Clash of Clans is on a maintenance break: the farm waits and "
                               "carries on when it's over.", "on_stopped", "info")
        elif now - self._maintenance_since >= MAINTENANCE_MOST:
            hours = MAINTENANCE_MOST.total_seconds() / 3600
            raise screens.MaintenanceBreak() from RuntimeError(f"still on it after {hours:g} hours")
        self.status(f"Maintenance break: looking again in {MAINTENANCE_LOOK // 60} min")
        try:
            self.game.device.stop_app(self.config.device.package)
        except DeviceError as stop:
            log.info("Couldn't close the game: %s", stop)
        self._rest(MAINTENANCE_LOOK)
        self.checkpoint()

    def _log_device(self) -> None:
        """The game's version and the device's display, once a run: what a
        failure later on may come down to (a game update, a changed display)."""
        found = diag.device_facts(self.game.device, self.config.device.package)
        if found:
            log.info("Playing on %s", diag.describe_facts(found))

    def _diagnostics(self) -> str:
        """The emulator and the scales the bot is using, the screen's size and
        what it last made of it, and what it was at: for a failure's log line."""
        g = self.game
        said = [f"emulator {self.config.device.mode}", f"UI scale {g.ui_scale or 1.0:.2f}",
                f"window scale {g.window_scale or g.vision.window_scale:.2f}"]
        screen = getattr(g, "screen", None)
        if screen is not None and hasattr(screen, "shape"):
            said.append(f"screen {screen.shape[1]}x{screen.shape[0]}")
        seen = getattr(g, "last_seen", None)
        if seen is not None:
            said.append(f"last seen {getattr(seen, 'value', seen)}")
        doing = getattr(self, "_doing", "")
        if doing:
            said.append(f"while: {doing}")
        return ", ".join(said)

    def recover(self) -> None:
        """After a failed round: close the game and start it again, or, if
        that can't be done (the device doesn't answer), the emulator."""
        try:
            self.restart_game()
            return
        except (Stopped, GameNotInstalled):  # a new emulator won't install the game
            raise
        except Exception as exc:  # ADB gone, say: the emulator needs restarting
            log.error("Couldn't restart the game: %s", diag.failure(exc))
        if self._reopen_device is None:
            log.error("The game couldn't be restarted, and the emulator can't be restarted from here")
            return
        log.warning("Restarting the emulator")
        self.status("Restarting the emulator")
        self.notifier.send("Restarting the emulator", "The game kept failing, so COCFARM is restarting the "
                           "emulator. It carries on by itself once the game is back.", "on_emulator_restart", "warn")
        self.game.device = self._reopen_device()
        self.game.screen = None
        self.ensure_game_running()

    def read_resources(self, shift: int = 0) -> Loot | None:
        """Your resources; ``shift``: the bar is that many pixels further
        left (pushed over by a window open on the right)."""
        self.game.refresh()
        loot = search.own_resources(self.game, self.config.regions, shift)
        if loot is not None:
            self.resources = loot
            if self._on_resources:
                self._on_resources(loot)
        return loot

    def read_storage_max(self, village: str = "home", again: bool = False) -> None:
        """What the ``village``'s storages hold, if not read yet today or one
        of the amounts is missing (cleared after switching accounts, say),
        and not set by hand: storage.read / storage.builder_read off.
        ``again``: read even if read today (a storage looks full: a new or
        upgraded storage holds more)."""
        storage = self.config.storage
        if not (storage.read if village == "home" else storage.builder_read):
            return
        today = self._now().date()
        read_on = "read_on" if village == "home" else "builder_read_on"
        if (not again and getattr(storage, read_on) == today.isoformat()
                and {"gold", "elixir"} <= set(storage.of(village))):
            return  # read today (kept in the settings, so a new start doesn't read it again)
        day, tries, read = self._storage_max_reads.get(village, (None, 0, False))
        if day != today:
            tries, read = 0, False
        if tries >= STORAGE_MAX_TRIES and not (again and read):  # (a check again after a good read: allowed)
            return
        # Only in that village: the home village's third bar is dark elixir, the builder base's is gems,
        # and tapping the gem bar opens the Shop.
        wanted = screens.Screen.HOME if village == "home" else screens.Screen.BUILDER
        if screens.identify(self.game) is not wanted:
            log.info("Not reading what the storages hold: not in the %s", wanted.value)
            return
        kinds =("gold", "elixir", "dark") if village == "home" else ("gold", "elixir")  # then gems
        maxes = search.storage_maxes(self.game, self.config.regions, kinds)
        read = {"gold", "elixir"} <= set(maxes)  # dark elixir only from town hall 7
        self._storage_max_reads[village] = (today, tries + 1, read)
        if read:
            setattr(storage, read_on, today.isoformat())
            self._storages_checked[village] = self.game.clock()
        if maxes:
            storage.set(village, maxes)
            if self._on_storage_max:
                self._on_storage_max(village, maxes)
        if village == "home" and read:
            s = self.config.search
            self._fit_search_minimums()
        where = "Storages" if village == "home" else "Builder storages"
        if maxes:
            log.info("%s hold %s", where, ", ".join(f"{v:,} {k}" for k, v in maxes.items()))
        if not read:
            log.info("Couldn't read what all the %s hold; %s", where.lower(),
                     "trying again next round" if tries + 1 < STORAGE_MAX_TRIES else
                     "trying again tomorrow")

    def _recheck_storages(self, village: str) -> bool:
        """Before a full storage is spent: measure the storages again, if
        not in the last STORAGE_RECHECK seconds. A size read before a
        storage was built or upgraded makes it look full early, and each
        round would spend it before it holds enough for a big upgrade
        (the Town Hall's). Whether the sizes changed."""
        last = self._storages_checked.get(village)
        if last is not None and self.game.clock() - last < STORAGE_RECHECK:
            return False
        self._storages_checked[village] = self.game.clock()
        before = dict(self.config.storage.of(village))
        self.read_storage_max(village, again=True)
        after = dict(self.config.storage.of(village))
        if after != before:
            log.info("The storages hold more now: %s", ", ".join(f"{v:,} {k}" for k, v in after.items()))
        return after != before

    def full_storages(self, village: str = "home", loot: Loot | None = None) -> set[str]:
        """Which of the ``village``'s storages are full: what's in them
        (``loot`` if just read, else read now) against what they hold (a
        storage whose size isn't known never is)."""
        maxes = self._storage_sizes(village)
        if not maxes:
            return set()
        loot = loot or self.read_resources()
        return set() if loot is None else search.full_by_amount(loot, maxes)

    def _storage_sizes(self, village: str = "home") -> dict[str, int]:
        """The storage sizes known, less a dark elixir storage the account
        can't have yet (a size copied over with another farm's settings)."""
        maxes = self.config.storage.of(village)
        if village == "home" and "dark" in self.locked():
            maxes.pop("dark", None)
        return maxes

    def _room(self, before: Loot | None, wanted: list[str]) -> dict[str, int] | None:
        """What the home storages that count (``wanted``, or all whose size
        is known) can still take: the battle ends once that's taken."""
        maxes = self._storage_sizes("home")
        if not self.config.attack.end_when_full or before is None or not maxes:
            return None
        kinds = [k for k in (wanted or maxes) if k in maxes]
        return {k: max(round(maxes[k] * search.FULL_AMOUNT) - getattr(before, k), 0) for k in kinds} or None

    def _settle_attack(self, after: Loot | None) -> None:
        """Log the last attack once we know what it brought in."""
        record, self._pending = self._pending, None
        if record is None:
            return
        record.after = after
        if record.gained:
            self.gained = history.add(self.gained, record.gained)
            g = record.gained
            log.info("Gained gold %s, elixir %s, dark %s", f"{g.gold:+,}", f"{g.elixir:+,}", f"{g.dark:+,}")
        if self.config.bot.history:
            try:
                history.append(instances.resolve(self.config, self.config.bot.history), record)
            except OSError as exc:
                log.warning("Could not write %s: %s", self.config.bot.history, exc)
        if self._on_attack:
            self._on_attack(record)

    def maintain_village(self) -> None:
        cfg = self.config
        self.game.ledger.begin("home", "village")
        self.status("Looking after the village")
        village.leave_battle(self.game)
        village.clear_popups(self.game)
        # A pinch finger on a building left selected would move it (one
        # being upgraded shows no Upgrade button to tell).
        if self._left_selected:
            village.deselect(self.game)
        else:
            self.game.deselect()
        self._left_selected = False
        # The village comes back from battles zoomed in a little, and the
        # next battle's view starts from the village's zoom.
        self.game.zoom_out()
        self._left_zoomed_in = False
        if self._pending:
            # Before collecting or training, which would blur the gain.
            self._settle_attack(self.read_resources())
        self.read_storage_max()
        self.know_village()
        self.checkpoint()
        # "Village jobs in a new order": none of these needs another first.
        for job in human.order(self.game.rng, [self._collect, self._loot_cart, self._gravestones],
                               self.game.uses("chore_order")):
            self._extra(job)
        self.checkpoint()
        for job in human.order(self.game.rng, [self._clan_visit, self._capital], self.game.uses("chore_order")):
            self._extra(job)
        self._extra(self._browse)
        self._extra(self._upgrades)

    def _extra_off(self, name: str) -> bool:
        """True while the extra ``name`` is left alone after failing."""
        until = self._extra_paused.get(name)
        if until is None:
            return False
        if self._now() < until:
            return True
        del self._extra_paused[name]
        log.info("Trying %s again", EXTRA_NAMES.get(name, name))
        return False

    def _extra_failed(self, name: str, exc: Exception) -> None:
        """An extra failed: after EXTRA_TRIES rounds in a row it's left
        alone for EXTRA_PAUSE (said once), so the farm keeps attacking."""
        what = EXTRA_NAMES.get(name, name)
        self._extra_fails[name] += 1
        log.warning("%s failed (%s); the farm goes on [%s]", what[0].upper() + what[1:], diag.failure(exc),
                    self._diagnostics())
        diag.traceback_to_file(log, exc, what)
        self._save_error_screen()
        if self._extra_fails[name] >= EXTRA_TRIES:
            self._extra_fails[name] = 0
            self._extra_paused[name] = self._now() + EXTRA_PAUSE
            hours = EXTRA_PAUSE.total_seconds() / 3600
            log.warning("Leaving %s alone for %g hours: it failed %d rounds in a row (%s)", what, hours,
                        EXTRA_TRIES, exc)

    def _extra(self, job: Callable[[], None]) -> None:
        """Run one of the village's extras; its failure doesn't fail the
        round (the village is put back on screen), unless the game is gone."""
        name = job.__name__
        if self._extra_off(name):
            return
        try:
            job()
        except (Stopped, screens.NotLoggedIn, screens.MaintenanceBreak, SameAccount, DeviceError, GameNotInstalled):
            raise
        except Exception as exc:  # noqa: BLE001 - extras are extras
            self._extra_failed(name, exc)
            self._left_selected = self._left_zoomed_in = True  # (whatever it left open or zoomed)
            if screens.settle(self.game) is not screens.Screen.HOME:  # (raises StuckError if it can't)
                raise
            self.game.deselect()
            self.game.zoom_out()
        else:
            self._extra_fails[name] = 0

    def know_village(self) -> None:
        """What this account has unlocked: the game's village export, read
        once a day (kept beside the profile). Only in the home village."""
        if not self.config.bot.read_village:
            return
        account = self._account
        path = self._village_path(account)
        now = self._now()
        if self.village is None and path is not None:
            self.village = townhall.load(path)
            if self.village is not None:
                self._village_read_at.setdefault(account, now)  # (kept: as good as read now)
        today = now.date().isoformat()
        if self._village_stale(account, now):
            day, tries = self._village_tries.get(account, ("", 0))
            tries = tries if day == today else 0
            if tries < VILLAGE_TRIES and screens.identify(self.game) is screens.Screen.HOME:
                self._village_tries[account] = (today, tries + 1)
                self.status("Reading the village data (Settings → Data Export)")
                text = export.copy_village(self.game)
                fresh = townhall.parse(text)
                if fresh is not None:
                    fresh.read_on = today
                    self._village_tries.pop(account, None)
                    self._village_read_at[account] = now
                    if path is not None:
                        try:
                            townhall.save(path, text, today)
                        except OSError as exc:
                            log.info("Couldn't keep the village data: %s", exc)
                    if self.village is None or self.village.town_hall != fresh.town_hall:
                        log.info("Village: Town Hall %d", fresh.town_hall)
                    self.village = fresh
        if self.village is not None:
            self._fit_search_minimums()
            if self._on_village:
                self._on_village(account, self.village)

    def _village_stale(self, account: str | None, now: datetime) -> bool:
        """Read the village data again: not read today, or, while the
        village is young (research, heroes... still locked), not read in
        YOUNG_REREAD this session; a Town Hall level or a Laboratory that
        finished since unlocks them the same day."""
        if self.village is None or self.village.read_on != now.date().isoformat():
            return True
        if not YOUNG_LOCKS & set(self.locked()):
            return False
        last = self._village_read_at.get(account)
        return last is None or now - last >= YOUNG_REREAD

    def _village_changed(self) -> None:
        """A new building went up (a Laboratory, say, unlocking research):
        the village data is read again next round, not tomorrow."""
        self.game.built_new = 0
        # Only while a building could still unlock something (each reading takes a trip to Settings).
        if self.village is not None and YOUNG_LOCKS & set(self.locked()):
            log.info("Upgrades: a new building went up; the village data is read again next round")
            self.village.read_on = ""
            self._village_tries.pop(self._account, None)
            self._village_read_at.pop(self._account, None)

    def _fit_search_minimums(self) -> None:
        """Search minimums never set by hand: from the Town Hall if it's
        known, else from the storages (``config.search_minimums_for``)."""
        s = self.config.search
        town_hall = self.village.town_hall if self.village is not None else None
        fitted = search_minimums_for(self._storage_sizes("home"),
                                     {k: getattr(s, f"min_{k}") for k in ("gold", "elixir", "dark")}, town_hall,
                                     s.minimums_by_hand)
        for kind, least in fitted.items():
            setattr(s, f"min_{kind}", least)
        if fitted:
            log.info("Search minimums set for %s: %s",
                     f"Town Hall {town_hall}" if town_hall else "your storages",
                     ", ".join(f"{v:,} {k}" for k, v in fitted.items()))

    def locked(self) -> dict[str, str]:
        """What this account can't do yet (``townhall.locked``)."""
        return townhall.locked(self.village)

    def _not_yet(self, feature: str, what: str) -> bool:
        """True if ``feature`` isn't unlocked; said once per account."""
        why = self.locked().get(feature)
        if why and feature + what not in self._said_locked:
            self._said_locked.add(feature + what)
            log.info("Skipping %s: %s", what, why)
        return bool(why)

    def _collect(self) -> None:
        cfg = self.config
        if cfg.bot.collect_resources:
            # The first round of a session always collects.
            due = self._collect_due or cfg.bot.collect_every
            if self._since_home_collect is None or self._since_home_collect + 1 >= due:
                kinds = tuple(k for k in ("gold", "elixir", "dark") if getattr(cfg.bot, f"collect_{k}")
                              and not (k == "dark" and self._not_yet("dark", "collecting dark elixir drills")))
                full = self.full_storages() if self._storage_sizes("home") else set()
                if full & set(kinds):
                    # A full storage takes nothing more: its collectors keep theirs.
                    log.info("Collectors: leaving the %s ones (storage full)", " and ".join(sorted(full & set(kinds))))
                    kinds = tuple(k for k in kinds if k not in full)
                if kinds:
                    village.collect_resources(self.game, kinds)
                self._since_home_collect = 0
                # ("Village jobs in a new order": not every N rounds on the dot.)
                self._collect_due = (human.collect_gap(self.game.rng, cfg.bot.collect_every)
                                     if self.game.uses("chore_order") and cfg.bot.collect_every > 1 else None)
            else:
                self._since_home_collect += 1
                log.info("Collectors: next in %d round(s)", due - self._since_home_collect)

    def _all_full(self) -> bool:
        """Whether every home storage is full (their sizes known): what a
        loot cart or collector holds would have nowhere to go."""
        sizes = self._storage_sizes("home")
        return {"gold", "elixir"} <= set(sizes) and set(sizes) <= self.full_storages()

    def _loot_cart(self) -> None:
        if self.config.bot.collect_loot_cart:
            if self._all_full():
                # It keeps what's in it until it's collected.
                log.info("Loot cart: left for later (the storages are full)")
                return
            village.collect_loot_cart(self.game)

    def _gravestones(self) -> None:
        if self.config.bot.clear_gravestones:
            village.clear_gravestones(self.game)

    def _clan_visit(self) -> None:
        cfg = self.config
        if (cfg.clan.donate or cfg.clan.request_troops) and self._not_yet("clan", "donations and troop requests"):
            return
        # One visit to the clan chat: the membership check, donations and the
        # request all run in it, and it's closed once at the end.
        if (cfg.clan.donate or cfg.clan.request_troops) and self._in_clan is None:
            self._in_clan = clan.in_clan(self.game, close=False)
            if self._in_clan is False:
                log.info("Not in a clan: skipping troop requests and donations (join one in the game)")
        clan_ok = self._in_clan is not False
        self.checkpoint()
        if cfg.clan.donate and clan_ok:
            self.status("Donating")
            clan.donate(self.game, cfg.clan, close=False)
        if cfg.clan.request_troops and clan_ok:
            now = self.game.clock()
            if self._next_request is None or now >= self._next_request:
                self.status("Requesting troops")
                _, wait = clan.request_troops(self.game, cfg.clan, close=False)
                # Wait out the game's cooldown (read off the button), or the
                # configured interval when it couldn't be read.
                self._next_request = now + (wait + 30 if wait else cfg.clan.request_every_minutes * 60)
        if cfg.clan.donate or cfg.clan.request_troops:
            clan.close_chat(self.game)

    def _browse(self) -> None:
        """"Look around the game": every few rounds, something besides farming."""
        self.game.note("browse", human.BROWSE_EXTRA)
        if not self.game.uses("browse"):
            return
        if self._browse_due is None:
            self._browse_due = human.browse_gap(self.game.rng)
        self._since_browse += 1
        if self._since_browse < self._browse_due:
            return
        self._since_browse, self._browse_due = 0, None
        began = self.game.clock()
        self.status("Looking around")
        what = browse.look_around(self.game)
        self.game.note("browse", 0.0, self.game.clock() - began)
        if what:
            log.info("Looked around: %s", what)

    def _capital(self) -> None:
        if self.config.capital.raid and not self._not_yet("clan", "Clan Capital raids"):
            self.raid_capital()

    def _upgrades(self) -> None:
        cfg = self.config
        self.game.ledger.begin("home", "upgrades")
        # Upgrades last, just before the battle: they leave the camera zoomed
        # in on what they upgraded, and a battle zooms out and centres its
        # view by itself, so nothing needs putting back.
        handed: dict = {}  # Buildings & Heroes, left open by the upgrades for the walls
        if cfg.upgrade.enabled:
            # Before walls: big upgrades get a full storage first, and walls
            # get what's left (one builder is kept for them).
            full = self.full_storages()
            if full and self._recheck_storages("home"):
                full = self.full_storages()  # bigger now (a storage built or upgraded): maybe not full
            if not full:
                self._why_no_upgrades()
            if full:
                self.status("Upgrading")
                report: dict = {}
                started = upgrade.upgrade(self.game, cfg, self.read_resources, full, last=not cfg.walls.enabled,
                                          hand_over=handed if cfg.walls.enabled else None, locked=self.locked(),
                                          total=self.village.builders if self.village else 0, report=report)
                self.session["upgrades"] += started
                self._upgrade_idle = bool(report.get("idle")) and cfg.upgrade.buildings
                if report.get("town_hall"):
                    self._read_village_again()  # for when it'll be done
                if started:
                    self._left_selected = self._left_zoomed_in = not cfg.walls.enabled
                if self.game.built_new:
                    self._village_changed()
        if cfg.walls.enabled:
            self.status("Upgrading walls")
            # (With the list still open, the numbers beside it aren't read: the upgrades say what's left.)
            full = self.full_storages(loot=handed.get("loot")) if cfg.walls.only_when_full else None
            # Walls leave the gold searching needs: spent below it, there'd be no more battles to earn it back.
            search_gold = 0.0 if cfg.search.maintain_only else cfg.search.min_own_gold_share
            keep = cfg.storage.keep("home", {"gold": max(cfg.walls.keep_gold, search_gold),
                                             "elixir": cfg.walls.keep_elixir})
            walls = cfg.walls
            if walls.use != "gold" and self._not_yet("wall_elixir", "elixir on walls"):
                walls = dataclasses.replace(walls, use="gold")
            self._round_walls = village.upgrade_walls(self.game, walls, self.read_resources, keep, full,
                                                      self._walls_known, last=True, handed=handed)
            self.session["walls"] += self._round_walls
            # Even with none paid for, walls tapped are left selected and zoomed in on.
            self._left_selected = self._left_zoomed_in = True

    def _read_village_again(self) -> None:
        """The village data is read again next round, not tomorrow."""
        if self.village is not None:
            self.village.read_on = ""
        self._village_tries.pop(self._account, None)

    def _town_hall_done_at(self, now: datetime) -> datetime | None:
        """When the Town Hall's upgrade under way ends, by the village data; None if none is."""
        until = self.village.town_hall_until if self.village is not None else 0
        if not until:
            return None
        done = datetime.fromtimestamp(until, now.tzinfo)
        return done if done > now else None

    def _rest_for_town_hall(self, loot: Loot | None) -> bool:
        """The Town Hall is being upgraded, gold and elixir are full, and
        for TOWN_HALL_ROUNDS rounds the upgrades found nothing to start
        (a builder free, the list looked through) and no wall went up:
        more loot has nowhere to go. The game is closed until the Town
        Hall is done, or TOWN_HALL_LOOK if that's sooner (then the round
        after looks again: a potion or a book ends the upgrade early, a
        laboratory that finished takes elixir). True if it rested."""
        cfg = self.config
        idle, walls = self._upgrade_idle, self._round_walls
        self._upgrade_idle, self._round_walls = False, 0
        alone = cfg.bot.mode != "auto" or "builder_base" in self.locked() or self._no_boat
        if not (cfg.upgrade.enabled and cfg.upgrade.wait_for_town_hall and alone and idle and not walls
                and {"gold", "elixir"} <= self.full_storages(loot=loot)):
            if self._town_hall_rested:
                log.info("Town Hall: there's something to spend loot on again: farming again")
                self.notifier.send("Farming again", "The Town Hall is done (or loot has somewhere to go again), "
                                   "so the farm carries on.", "on_stopped", "good")
            self._idle_rounds, self._town_hall_rested = 0, False
            return False
        self._idle_rounds += 1
        now = self._now()
        done = self._town_hall_done_at(now)
        if done is None:
            # Not known to be upgrading: the data may be from before it began (or it has ended since).
            # Asked for at most every TOWN_HALL_LOOK (each reading is a trip to Settings, and may fail).
            last = max(filter(None, (self._village_read_at.get(self._account), self._town_hall_asked)),
                       default=None)
            if self.village is not None and (last is None or now - last >= TOWN_HALL_LOOK):
                self._town_hall_asked = now
                self._read_village_again()
            return False
        if self._idle_rounds < TOWN_HALL_ROUNDS:
            return False
        back = min(done + timedelta(minutes=1), now + TOWN_HALL_LOOK)
        if not self._town_hall_rested:
            log.info("Town Hall: it's being upgraded until %s, gold and elixir are full and there's nothing left "
                     "to upgrade: resting until then (looking in every %g hours)", f"{done:%a %H:%M}",
                     TOWN_HALL_LOOK.total_seconds() / 3600)
            self.notifier.send("Resting while the Town Hall upgrades", "Gold and elixir are full and nothing is "
                               f"left to upgrade, so the farm rests until the Town Hall is done ({done:%a %H:%M}).",
                               "on_stopped", "info")
        self._town_hall_rested = True
        self.status(f"Town Hall upgrading (done {done:%a %H:%M}), nothing left to upgrade: "
                    f"looking again at {back:%H:%M}")
        try:
            self.game.device.stop_app(cfg.device.package)
        except DeviceError as stop:
            log.info("Couldn't close the game: %s", stop)
        self._left_selected = self._left_zoomed_in = False  # (the game opens afresh)
        self._rest((back - now).total_seconds())
        self.checkpoint()
        self._read_village_again()  # is it done? (and the new Town Hall's level)
        return True

    def _why_no_upgrades(self) -> None:
        """Say why no upgrade starts this round (they wait for a full
        storage): how full each is, or that their sizes aren't known yet,
        and when walls are what keeps them from filling."""
        maxes = self._storage_sizes("home")
        if not maxes:
            log.info("Upgrades: waiting to read how much the storages hold (from their \"Max\" box)")
            return
        loot = self.read_resources()
        if loot is None:
            return
        shares = ", ".join(f"{k} {min(getattr(loot, k) / most, 1):.0%}" for k, most in maxes.items() if most)
        walls = self.config.walls
        hint = ("; walls spend it down each round: turn on Walls → \"Only when storage is full\" to let it fill"
                if walls.enabled and not walls.only_when_full else "")
        log.info("Upgrades: they start once a storage is full (%s)%s", shares, hint)

    def raid_capital(self) -> None:
        """Clan Capital raids, with every raid attack left, on Raid Weekends
        (looked for again an hour later)."""
        now = self._now()
        if not capital.raid_weekend(now) or (self._capital_after and now < self._capital_after):
            return
        self.checkpoint()
        self.status("Raiding the Clan Capital")
        self._capital_after = now + timedelta(hours=1)
        try:
            raids = capital.raid_capital(self.game, self.config.capital)
        except capital.NotAllowed:
            log.info("Clan Capital: you joined the clan after this Raid Weekend began, so the game allows no "
                     "raids until the next one")
            end = capital.weekend_end(now)
            if end is not None:
                self._capital_after = end.astimezone(now.tzinfo) if now.tzinfo else end.astimezone().replace(tzinfo=None)
            raids = 0
        self.session["raids"] += raids
        if raids:
            log.info("Clan Capital: %d raid(s) made", raids)
        # Back home the village is shown at its default zoom.
        self.game.zoom_out()

    def ranked_attacks(self) -> None:
        """"Make the day's ranked attacks": every one left, before farming,
        fought for stars. Once there are none (or the account isn't signed
        up), looked for again an hour later. A failure doesn't fail the
        round: the farming goes on."""
        now = self._now()
        if not self.config.ranked.enabled or (self._ranked_after and now < self._ranked_after):
            return
        self.checkpoint()
        self.status("Ranked battles")
        self._ranked_after = now + RANKED_LOOK
        self.game.ledger.begin("home", "battle")
        made = ranked.attacks(self.game, self.config)
        self.session["ranked"] += made
        if made:
            log.info("Ranked: %d attack(s) made", made)
            # Back home the village is shown at its default zoom, and the army is spent.
            self.game.zoom_out()

    def war_attacks(self) -> None:
        """"Make your war attacks": each one left, as soon as battle day has
        started, fought for stars. The war is looked at every half hour. A
        failure doesn't fail the round: the farming goes on."""
        now = self._now()
        if not self.config.war.enabled or (self._war_after and now < self._war_after):
            return
        self.checkpoint()
        self.status("Clan war")
        self._war_after = now + WAR_LOOK
        self.game.ledger.begin("home", "battle")
        made = war.attacks(self.game, self.config)
        self.session["war"] += made
        if made:
            log.info("War: %d attack(s) made", made)
        # Back home the village is shown at its default zoom.
        self.game.zoom_out()

    def playing(self) -> str:
        """The village being farmed: "home" or "builder" (mode "auto" starts
        at home and moves to the builder base once the storages are full)."""
        mode = self.config.bot.mode
        village = (self.auto_village or self.config.bot.auto_first) if mode == "auto" else mode
        if village == "builder" and self._not_yet("builder_base", "the Builder Base (farming the home village)"):
            return "home"
        if village == "builder" and mode == "auto" and (self._no_boat or self._extra_off("builder")):
            return "home"
        return village

    def _village_full(self, village: str) -> bool:
        """Mode "auto": this village's storages are full. Moves on to the
        other village, or, once both are full, stops. Returns True if this
        round should end here."""
        self.full_villages.add(village)
        other = "builder" if village == "home" else "home"
        names = {"home": "home village", "builder": "builder base"}
        if other in self.full_villages:
            self.storage_full = True
            log.info("Storage full in both villages: stopping")
            self.notifier.send("Both villages are full", "Storage is full in the home village and the builder "
                               "base, so the bot stopped.", "on_storage_full", "good",
                               attacks=self.attacks, loot=self.gained, fields=self.session_fields())
            self.status("Stopped: storage full in both villages")
            return True
        self.auto_village = other
        log.info("Storage full in the %s: switching to the %s", names[village], names[other])
        self.notifier.send("Switching villages", f"Storage is full in the {names[village]}. Now farming the "
                           f"{names[other]}.", "on_storage_full", "info", attacks=self.attacks, loot=self.gained)
        self.status(f"Storage full: switching to the {names[other]}")
        return True

    def _clock_tower(self) -> None:
        bcfg = self.config.builder
        if bcfg.clock_tower and not self._clock_off:
            gems = self.read_resources()
            if builder.boost_clock_tower(self.game):
                after = self.read_resources()
                if gems is not None and after is not None and after.dark < gems.dark:
                    log.error("Clock Tower boost: gems went from %d to %d; not boosting again this session",
                              gems.dark, after.dark)
                    self._clock_off = True

    def _elixir_cart(self) -> bool:
        bcfg = self.config.builder
        if not bcfg.collect_cart:  # (looked for at a fixed spot: before upgrades move the camera)
            return False
        if self._cart_due is None:
            self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
        if self._since_cart < self._cart_due:
            return False
        cart = builder.collect_elixir_cart(self.game)
        self._since_cart = 0
        self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
        return cart

    def builder_cycle(self) -> bool:
        """One builder base battle (sailing over first if needed)."""
        if screens.settle(self.game) is not screens.Screen.BUILDER:
            if self.village is None and self.config.bot.read_village:
                # Not known yet whether this account has the builder base (it opens at Town Hall 6):
                # the village data says, before looking for a boat that may not be repaired yet.
                self.know_village()
                if self.playing() != "builder":
                    return self.cycle()
            self.status("Sailing to the builder base")
            try:
                builder.go_to_builder_base(self.game)
            except builder.BuilderError as exc:
                if self.config.bot.mode != "auto" or "not visible" not in str(exc):
                    raise
                # Auto: the home village is farmed too, so farm it rather than stop.
                log.warning("No boat to the builder base (%s): farming the home village only", exc)
                self._no_boat = True
                return self.cycle()
            self.game.refresh()
        if self.check_account():
            return self.cycle()
        self.game.ledger.begin("builder", "village")
        village.clear_popups(self.game)
        if self._left_selected:  # the last round's upgrades, if its battle didn't happen
            village.deselect(self.game)
            self._left_selected = False
        bcfg = self.config.builder
        self.read_storage_max("builder")
        self.checkpoint()
        # The Clock Tower and the elixir cart are looked for on the first base,
        # where the boat lands: before collecting takes the view to the second
        # base. Nothing after needs the view put back (walls and upgrades go
        # by the Build list, the battle by its button, the boat home finds
        # its way to the first base), which saves dragging it back.
        # Both on the first base ("Village jobs in a new order": either first).
        carted: list[bool] = []
        for job in human.order(self.game.rng, [self._clock_tower, lambda: carted.append(self._elixir_cart())],
                               self.game.uses("chore_order")):
            job()
        cart = bool(carted and carted[0])
        self.checkpoint()
        if bcfg.collect_resources:
            # The first battle of a session always collects.
            if self._since_collect is None or self._since_collect >= bcfg.collect_every:
                # A full storage's collectors can't be emptied (tapping them selects them instead).
                full = self.full_storages("builder")
                kinds = tuple(k for k in ("gold", "elixir", "gems") if k not in full)
                if {"gold", "elixir"} & set(kinds):
                    builder.collect_resources(self.game, kinds=kinds, restore=False)
                else:
                    log.info("Builder storages full: not collecting")
                self._since_collect = 0
            else:
                log.info("Builder collectors: next in %d battle(s)", bcfg.collect_every - self._since_collect)
        self.checkpoint()
        self.game.ledger.begin("builder", "upgrades")
        # Upgrades last, just before the battle: they leave the camera zoomed
        # in on what they upgraded, and the battle zooms out by itself, so
        # it isn't put back. Unless mode "auto" may sail home first.
        wanted = [k for k in self.config.bot.stop_when_full if k in ("gold", "elixir")]  # (no dark elixir here)
        may_leave = self.config.bot.mode == "auto" and bool(wanted)
        if bcfg.upgrade:
            # Before walls (one builder is kept for them), and before mode
            # "auto" moves on: spending a full storage keeps this village going.
            full = self.full_storages("builder")
            if full & {"gold", "elixir"}:
                self.status("Upgrading (builder base)")
                last = not bcfg.walls and not may_leave
                self.session["upgrades"] += upgrade.upgrade_builder_base(self.game, bcfg, self.read_resources, full,
                                                                         last=last, total=self.village.builder_base_builders
                                                                         if self.village else 0)
                self._left_selected = last
        walls = 0
        if bcfg.walls:
            # Before mode "auto" moves on, as the upgrades: walls spend a full
            # storage too (left full, this village would never be farmed).
            self.status("Upgrading builder base walls")
            keep = self.config.storage.keep("builder", {"gold": bcfg.walls_keep_gold,
                                                         "elixir": bcfg.walls_keep_elixir})
            walls = builder.upgrade_walls(self.game, bcfg, self.read_resources, keep, self._builder_walls_known,
                                          last=not may_leave)
            self.session["walls"] += walls
            self._left_selected = not may_leave
        if may_leave and set(wanted) <= self.full_storages("builder"):
            self._village_full("builder")
            return False
        self.checkpoint()
        if not self._left_selected:  # (a pinch finger on what's selected would move it)
            self.game.zoom_out()
        self.game.ledger.begin("builder", "battle")
        before = self.read_resources()
        started = self.game.clock()
        self.status("Attacking (builder base)")
        if (bcfg.use_hero or bcfg.hero_ability) and self._not_yet("builder_heroes", "the builder base hero"):
            bcfg = replace(bcfg, use_hero=False, hero_ability=False)
        result = builder.attack(self.game, bcfg,
                                on_stage=lambda n: self.status(f"Attacking (builder base): base {n}"))
        seconds = self.game.clock() - started
        self._left_selected = False  # the battle dropped it
        self._since_cart += 1
        if self._since_collect is not None:
            self._since_collect += 1
        self.attacks += 1
        self.attacks_today += 1
        self._since_break += 1
        log.info("Builder attack #%d done (%d stage(s))", self.attacks, result.stages)
        village.clear_popups(self.game)  # the Star Bonus window
        self.game.zoom_out()
        after = self.read_resources()
        gained = None
        if before and after:
            gained = Loot(after.gold - before.gold, after.elixir - before.elixir, 0)
            log.info("Builder gained gold %s, elixir %s", f"{gained.gold:+,}", f"{gained.elixir:+,}")
        if self._on_builder:
            self._on_builder({
                "stages": result.stages, "seconds": seconds, "gained": gained, "screen": result.screen,
                "walls": walls, "cart": cart,
                "cart_in": (self._cart_due - self._since_cart) if bcfg.collect_cart and self._cart_due else None,
            })
        self.status(f"Attack {self.attacks} done")
        return True

    def _no_battle(self) -> None:
        """No battle follows the upgrades and walls, which left the view
        zoomed in on them (a battle would zoom out by itself): drop what's
        selected and zoom out."""
        if self._left_selected or self._left_zoomed_in:
            village.deselect(self.game)
            self.game.zoom_out()
        self._left_selected = self._left_zoomed_in = False

    def cycle(self) -> bool:
        """One round: look after the village, then (unless told not to)
        find a base and attack it. Returns True if an attack happened."""
        cfg = self.config
        self.game.habits = cfg.human.active()  # a profile switched in may differ
        self._closed_for = None  # playing: whatever it was closed for is over
        self._tire()
        self.ensure_game_running()
        if self._check_account_first():
            return self.cycle()  # with that account's settings
        if self.playing() == "builder":
            return self.builder_cycle()
        where = screens.settle(self.game)  # deal with chests, rewards, results, popups...
        if where in (screens.Screen.HOME, screens.Screen.BUILDER) and self.check_account():
            return self.cycle()  # with that account's settings (which may farm the other village)
        if where is screens.Screen.BUILDER:
            # The game can open in the builder base (it opens where it was left).
            self.status("Sailing to the home village")
            if not builder.go_to_home_village(self.game):
                raise builder.BuilderError("couldn't sail back to the home village")
        self._upgrade_idle, self._round_walls = False, 0
        self.maintain_village()
        self._extra(self.war_attacks)  # before farming: the clan's war comes first
        self._extra(self.ranked_attacks)  # ...then the day's ranked attacks
        before = self.read_resources()
        if self._rest_for_town_hall(before):
            return False
        wanted = cfg.bot.stop_when_full
        if "dark" in wanted and self._not_yet("dark", "waiting for a full dark elixir storage"):
            wanted = [k for k in wanted if k != "dark"]  # (there's none to fill yet)
        if wanted and set(wanted) <= self.full_storages(loot=before):
            self._no_battle()
            if cfg.bot.mode == "auto" and "builder_base" not in self.locked() and not self._no_boat:  # on to the builder base
                self._village_full("home")
                return False
            self.storage_full = True
            log.info("Storage full (%s): stopping", ", ".join(wanted))
            full = {"gold": "gold", "elixir": "elixir", "dark": "dark elixir"}
            self.notifier.send("Storage full", f"Your {' and '.join(full.get(w, w) for w in wanted)} storage"
                               f"{'s are' if len(wanted) > 1 else ' is'} full, so the bot stopped.",
                               "on_storage_full", "good", attacks=self.attacks, loot=self.gained,
                               fields=self.session_fields())
            self.status(f"Stopped: storage full ({', '.join(wanted)})")
            return False
        if cfg.search.maintain_only:
            self._no_battle()
            return False
        least = cfg.storage.keep("home", {"gold": cfg.search.min_own_gold_share}).get("gold", 0)
        if least:
            gold = before.gold if before else search.own_gold(self.game, cfg.regions)
            if gold is not None and gold < least:
                self.status(f"Saving up: {gold:,} gold is below {least:,}")
                log.info("Not searching: %s gold is below %s", f"{gold:,}", f"{least:,}")
                self._no_battle()
                return False
        self.checkpoint()
        self.game.ledger.begin("home", "search")
        self.status("Searching for a base")
        boosts = cfg.attack.super_troops and not self._not_yet("super_troops", "super troop boosts")
        boosted = search.start_search(self.game, boosts, before.dark if before else None)
        self.session["super troops"] += boosted
        if boosted and before is not None:
            # What the boosts cost isn't the battle's: it's counted from after them.
            before = replace(before, dark=before.dark - boosted * super_troops.BOOST_PRICE)
        too_high = unreachable_minimums({k: getattr(cfg.search, f"min_{k}") for k in ("gold", "elixir", "dark")})
        if too_high and too_high != self._warned_minimums:
            for k, v in too_high.items():
                log.warning("Search minimums: %s", unreachable_note(k, v, cfg.search.match_any))
        self._warned_minimums = too_high
        wants = cfg.search
        if wants.min_dark and self._not_yet("dark", "the dark elixir minimum (no base offers any yet)"):
            wants = replace(wants, min_dark=0)
        target = search.search(self.game, wants, cfg.regions, self._base_seen, zoom_out=self._left_zoomed_in,
                               look=self.game.uses("look_at_bases"), near=self.game.uses("near_misses"))
        self._left_zoomed_in = False
        self.game.ledger.begin("home", "battle")
        self.status("Attacking")
        self._pending = AttackRecord(self._now(), target, before)
        self._pending.taken = attack.attack(self.game, cfg, target, room=self._room(before, wanted))
        self._left_selected = False  # the battle dropped it
        self.attacks += 1
        self.attacks_today += 1
        self._since_break += 1
        log.info("Attack #%d done", self.attacks)
        # Read the gain as soon as we're home, so the last attack of a run
        # is logged with it too.
        if self.game.wait_for(T.BATTLE, timeout=20, interval=1):
            # The Star Bonus window opens a moment after the village shows.
            if self.game.has(T.OKAY):
                self.game.wait_for(T.OKAY, timeout=STAR_BONUS_WAIT, interval=1)
            village.clear_popups(self.game)
            after = self.read_resources()
            if after is not None:  # otherwise the next round's reading settles it
                self._settle_attack(after)
        self.status(f"Attack {self.attacks} done")
        return True

    def _save_error_screen(self) -> None:
        """Keep what the bot was looking at when a round failed, for debugging."""
        folder, screen = self.config.bot.error_screens, self.game.screen
        if not folder or screen is None:
            return
        import cv2

        path = instances.resolve(self.config, folder) / f"{self._now():%Y%m%d-%H%M%S}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = sorted(path.parent.glob("[0-9]" * 8 + "-" + "[0-9]" * 6 + ".png"))  # only the bot's own
            for stale in old[:max(0, len(old) - ERROR_SCREENS_KEPT + 1)]:
                stale.unlink()
            imagefile.write(str(path), screen)
            log.info("Saved the screen to %s", path)
        except OSError as exc:
            log.warning("Could not save %s: %s", path, exc)

    def run(self) -> None:
        limit = self.config.bot.max_attacks
        self._session_start = self._now()
        language.warn_once(self.game.device)
        self._log_device()
        try:
            while not limit or self.attacks < limit:
                self.checkpoint()
                reason = self.pause_reason()
                if reason:
                    self.status(f"Paused: {reason}")
                    self.game.habits = self.config.human.active()
                    self._go_offline(reason.split(" (")[0].split(" at ")[0])
                    self._fresh_at = None  # fresh when it plays again
                    self._rest(IDLE_WAIT)
                    continue
                attacked = False
                builder_extra = self.config.bot.mode == "auto" and self.playing() == "builder"
                try:
                    attacked = self.cycle()
                    self._deploy_alert()
                    self._progress_update()
                    self.errors = 0
                    if self._maintenance_since is not None:
                        log.info("The maintenance break is over: farming again")
                        self.notifier.send("Back after the maintenance break", "The game opened again, so the "
                                           "farm carries on.", "on_stopped", "good")
                        self._maintenance_since = None
                    if builder_extra:
                        self._extra_fails["builder"] = 0
                    if self.storage_full:
                        break
                except Stopped:
                    raise
                except search.SearchBlocked as exc:  # a battle left midway: closing the game a while clears it
                    self._wait_out_blocked(exc)
                except screens.MaintenanceBreak as exc:  # over in an hour or so: waited out
                    self._wait_out_maintenance(exc)
                    continue
                except (screens.NotLoggedIn, SameAccount, GameNotInstalled) as exc:  # restarting won't help
                    log.error("%s: stopping", exc)
                    self._save_error_screen()
                    raise
                except Exception as exc:  # keep farming through recoverable failures
                    if builder_extra and not isinstance(exc, DeviceError):
                        # Mode "auto": the builder base is an extra, the home village goes on.
                        self._extra_failed("builder", exc)
                        self.errors = 0
                        self.notifier.alert("round_failed", "A builder base round failed: restarting the game",
                                            f"**{type(exc).__name__}**\n```{str(exc)[:900]}```\nThe home "
                                            "village carries on.", "on_critical_error", "error",
                                            attacks=self.attacks)
                        try:
                            self.recover()
                        except (Stopped, GaveUp, GameNotInstalled):
                            raise
                        except Exception as again:  # noqa: BLE001 - the next round tries again
                            log.error("Recovery failed: %s", diag.failure(again))
                        self._rest_after_round(max(self.config.bot.loop_delay, IDLE_WAIT))
                        continue
                    self.errors += 1
                    log.error("Cycle failed (%d in a row): %s [%s]", self.errors, diag.failure(exc),
                              self._diagnostics())
                    diag.traceback_to_file(log, exc, "the round")
                    self._save_error_screen()
                    self._deploy_alert()  # a battle that failed after its army was refused
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        raise RoundsFailed(f"{self.errors} rounds in a row failed, though the game was restarted "
                                           f"after each: {exc}") from exc
                    self.notifier.alert("round_failed", "A round failed: restarting the game",
                                        f"**{type(exc).__name__}**\n```{str(exc)[:900]}```\nFailure {self.errors} "
                                        f"of {MAX_CONSECUTIVE_ERRORS} in a row before the bot stops.",
                                        "on_critical_error", "error", attacks=self.attacks)
                    self.status("Restarting the game")
                    try:
                        self.recover()
                    except (Stopped, GaveUp, GameNotInstalled):  # GaveUp: the emulator was started 3 times, never answering
                        raise
                    except Exception as exc:  # the next round tries again
                        log.error("Recovery failed: %s", diag.failure(exc))
                self.take_break()
                self.status("Waiting")
                rest = self.config.bot.loop_delay if attacked else max(self.config.bot.loop_delay, IDLE_WAIT)
                self._rest_after_round(rest)
                self._round_over(attacked)
        except Stopped:
            log.info("Stopped")
        except Exception as exc:
            from farmbot.friendly import explain

            problem = explain(str(exc))
            title, text = "Stopped by a problem", f"**{problem.title}**\n{problem.why}\n```{str(exc)[:900]}```"
            summary = dict(attacks=self.attacks, loot=self.gained, fields=self.session_fields())
            if self.config.notify.on_stopped:  # as always: every stop
                self.notifier.send(title, text, "on_stopped", "error", **summary)
            else:  # the alert alone: limited, as the window may start the farm again and again
                self.notifier.alert("stopped", title, text, "on_critical_error", "error", **summary)
            raise
        self._settle_attack(None)
        if self.playing() == "builder" and self.config.builder.return_home_after:
            try:
                builder.go_to_home_village(self.game)
            except Exception as exc:  # the bot is stopping anyway
                log.info("Couldn't sail home: %s", exc)
        if not self.storage_full:
            self.status(f"Stopped: all {limit} attacks played" if limit and self.attacks >= limit else "Stopped")
        log.info("Finished after %d attack(s)", self.attacks)
        if not self.storage_full:
            g = self.gained
            self.notifier.send("Bot stopped", f"Finished after {self.attacks:,} "
                               f"attack{'' if self.attacks == 1 else 's'}.", "on_stopped", "summary",
                               attacks=self.attacks, loot=g, fields=self.session_fields())
