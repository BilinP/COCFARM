"""Main farming loop."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from farmbot import accounts, history, screens
from farmbot.notify import Notifier
from farmbot.config import Config
from farmbot.device import DeviceError
from farmbot.game import Game, Stopped, T
from farmbot.history import AttackRecord
from farmbot.tasks import attack, builder, clan, search, upgrade, village
from farmbot.tasks.search import Loot

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3
ERROR_SCREENS_KEPT = 200  # newest failed-round screens kept; older ones are deleted
MAX_GAME_RESTARTS = 1  # game restarts without a good round before the emulator is restarted
STORAGE_MAX_TRIES = 3  # readings a day of what a village's storages hold, until one works
IDLE_WAIT = 60.0  # seconds between village rounds when not attacking
STARTUP_TIMEOUT = 90  # seconds to wait for the home village after starting the game
# An emulator answers ADB before Android has finished starting up, and a game
# started then is closed again: start it once Android has been up this long.
BOOT_SETTLE = 20.0
START_TRIES = 3  # starts of the game, if it closes while loading


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
        on_account: Callable[[object], str | None] | None = None,
    ):
        self.game = game
        self.config = config
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
        self._pending: AttackRecord | None = None  # attack waiting for its "after" reading
        self.storage_full = False  # set when the storages in bot.stop_when_full are all full
        # Mode "auto": the village being farmed now, and the ones whose storages are already full.
        self.auto_village: str | None = None
        self.full_villages: set[str] = set()
        self._since_cart = 0  # builder battles since the elixir cart was last emptied
        self._since_collect: int | None = None  # builder battles since the collectors were emptied
        self._since_home_collect: int | None = None  # home village rounds since the collectors were emptied
        self._cart_due: int | None = None  # battles between cart visits, picked at random
        # Watchdog: restarting the game is tried first; if the errors carry on
        # (or ADB itself fails), the emulator is closed and opened again.
        self._reopen_device = reopen_device
        self._game_restarts = 0  # game restarts since the last good round
        self.notifier = notifier or Notifier(config.notify)
        self._since_break = 0  # attacks since the last break
        self._view_centred = False  # the village's view, centred once a run
        self._break_due: int | None = None  # attacks between breaks, picked at random
        # What each village's storages hold (config.storage) is read the first
        # time the bot is in that village each day (upgraded storages hold
        # more); a failed reading is tried again the next rounds, a few times.
        self._storage_max_reads: dict[str, tuple[date, int, bool]] = {}  # village -> (day, tries, read)

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
        name = self._on_account(mask)
        if name is None:
            return False
        self._account_mask = mask
        if name == self._account:
            return False
        first, self._account = self._account is None, name
        log.info("Account: profile %s", name)
        self.status(f"Playing with profile {name}")
        if not first:  # what the bot learnt about the last account
            self.resources = None
            self.storage_full = False
            self.auto_village = None
            self.full_villages = set()
            self._storage_max_reads = {}
            self._since_collect = self._since_home_collect = None
            self._in_clan = self._next_request = None
            self._view_centred = False
        return True

    def status(self, text: str) -> None:
        log.debug("Status: %s", text)
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
            self.game.wait(min(1.0, end - self.game.clock()))

    # -- schedule -------------------------------------------------------
    def pause_reason(self) -> str | None:
        """Why the bot should not play right now, or ``None``."""
        sched = self.config.schedule
        now = self._now()
        if self._today != now.date():
            if self._today is not None:
                self._daily_summary(self._today)
            self._today, self.attacks_today, self._daily_limit = now.date(), 0, None
        if not sched.enabled:
            return None
        if now.hour not in sched.active_hours:
            upcoming = next(((now.hour + i) % 24 for i in range(1, 25) if (now.hour + i) % 24 in sched.active_hours), None)
            return "outside active hours" + (f" (resumes {upcoming:02d}:00)" if upcoming is not None else "")
        limit = self.daily_limit()
        if limit and self.attacks_today >= limit:
            return f"daily limit of {limit} attacks reached"
        return None

    def daily_limit(self) -> int:
        """Today's attack limit, picked at random in the range once a day (0 = none)."""
        sched = self.config.schedule
        if not sched.daily_attacks_max:
            return 0
        if self._daily_limit is None:
            low = max(1, min(sched.daily_attacks_min, sched.daily_attacks_max))
            self._daily_limit = self.game.rng.randint(low, sched.daily_attacks_max)
            log.info("Attacks today: up to %d", self._daily_limit)
        return self._daily_limit

    # -- game -----------------------------------------------------------
    def ensure_game_running(self) -> None:
        device, package = self.game.device, self.config.device.package
        if device.foreground_package() == package:
            return
        self._wait_for_android()
        # Phones: Android doesn't always report the game coming to the front,
        # so there only the screenshots tell whether it is still loading.
        watch_closing = not hasattr(device, "game_shown")
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
                if watch_closing and device.foreground_package() != package:
                    break  # it closed while loading
                self.game.wait(2)
            else:
                log.info("Game loaded: %s", screen.value)
                shown = getattr(device, "game_shown", None)  # phones: the screenshots prove it's in front
                if shown:
                    shown()
                return
            log.info("The game closed while loading; starting it again")
            self.game.wait(5)

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

    def _daily_summary(self, day: date) -> None:
        if not self.config.bot.history:
            return
        count, gained = history.totals_for(self.config.bot.history, day)
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
            self._break_due = rng.randint(sched.break_every_min, sched.break_every_max)
        if self._since_break < self._break_due:
            return
        minutes = rng.randint(sched.break_minutes_min, sched.break_minutes_max)
        log.info("Taking a %d-minute break after %d attacks", minutes, self._since_break)
        self.status(f"Taking a break: {minutes} min")
        self._since_break, self._break_due = 0, None
        self._rest(minutes * 60)

    def restart_game(self) -> None:
        log.warning("Restarting the game after repeated errors")
        self.game.device.stop_app(self.config.device.package)
        self.game.wait(3)
        self.ensure_game_running()

    def recover(self) -> None:
        """After repeated failed rounds: restart the game, or, if that was
        already tried (or the device doesn't answer), the emulator."""
        if self._game_restarts < MAX_GAME_RESTARTS:
            self._game_restarts += 1
            try:
                self.restart_game()
                return
            except Stopped:
                raise
            except Exception as exc:  # ADB gone, say: the emulator needs restarting
                log.error("Couldn't restart the game: %s", exc)
        if self._reopen_device is None:
            log.error("The game keeps failing and the emulator can't be restarted from here")
            return
        log.warning("Restarting the emulator")
        self.status("Restarting the emulator")
        self.notifier.send("Restarting the emulator", "The game kept failing, so COCFARM is restarting the "
                           "emulator. It carries on by itself once the game is back.", "on_emulator_restart", "warn")
        self.game.device = self._reopen_device()
        self.game.screen = None
        self._game_restarts = 0
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

    def read_storage_max(self, village: str = "home") -> None:
        """What the ``village``'s storages hold, if not read yet today or one
        of the amounts is missing (cleared after switching accounts, say),
        and not set by hand: storage.read / storage.builder_read off."""
        storage = self.config.storage
        if not (storage.read if village == "home" else storage.builder_read):
            return
        today = self._now().date()
        read_on = "read_on" if village == "home" else "builder_read_on"
        if getattr(storage, read_on) == today.isoformat() and {"gold", "elixir"} <= set(storage.of(village)):
            return  # read today (kept in the settings, so a new start doesn't read it again)
        day, tries, read = self._storage_max_reads.get(village, (None, 0, False))
        if day != today:
            tries, read = 0, False
        if tries >= STORAGE_MAX_TRIES:
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
        if maxes:
            storage.set(village, maxes)
            if self._on_storage_max:
                self._on_storage_max(village, maxes)
        where = "Storages" if village == "home" else "Builder storages"
        if maxes:
            log.info("%s hold %s", where, ", ".join(f"{v:,} {k}" for k, v in maxes.items()))
        if not read:
            log.info("Couldn't read what all the %s hold; %s", where.lower(),
                     "trying again next round" if tries + 1 < STORAGE_MAX_TRIES else
                     "trying again tomorrow")

    def full_storages(self, village: str = "home", loot: Loot | None = None) -> set[str]:
        """Which of the ``village``'s storages are full: what's in them
        (``loot`` if just read, else read now) against what they hold (a
        storage whose size isn't known never is)."""
        maxes = self.config.storage.of(village)
        if not maxes:
            return set()
        loot = loot or self.read_resources()
        return set() if loot is None else search.full_by_amount(loot, maxes)

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
                history.append(self.config.bot.history, record)
            except OSError as exc:
                log.warning("Could not write %s: %s", self.config.bot.history, exc)
        if self._on_attack:
            self._on_attack(record)

    def maintain_village(self) -> None:
        cfg = self.config
        self.status("Looking after the village")
        village.leave_battle(self.game)
        village.clear_popups(self.game)
        # The village comes back from battles zoomed in a little, and the
        # next battle's view starts from the village's zoom.
        if self.game.zoom_out() and not self._view_centred:
            # Once a run: the camera stays where it is left, and the next
            # battle's view starts from the village's.
            self._view_centred = self.game.center_view()
        if self._pending:
            # Before collecting or training, which would blur the gain.
            self._settle_attack(self.read_resources())
        self.read_storage_max()
        self.checkpoint()
        if cfg.bot.collect_resources:
            # The first round of a session always collects.
            if self._since_home_collect is None or self._since_home_collect + 1 >= cfg.bot.collect_every:
                kinds = tuple(k for k in ("gold", "elixir", "dark") if getattr(cfg.bot, f"collect_{k}"))
                village.collect_resources(self.game, kinds)
                self._since_home_collect = 0
            else:
                self._since_home_collect += 1
                log.info("Collectors: next in %d round(s)", cfg.bot.collect_every - self._since_home_collect)
        if cfg.bot.collect_loot_cart:
            village.collect_loot_cart(self.game)
        self.checkpoint()
        if cfg.upgrade.enabled:
            # Before walls: big upgrades get a full storage first, and walls
            # get what's left (one builder is kept for them).
            full = self.full_storages()
            if full:
                self.status("Upgrading")
                upgrade.upgrade(self.game, cfg, self.read_resources, full)
        if cfg.walls.enabled:
            self.status("Upgrading walls")
            full = self.full_storages() if cfg.walls.only_when_full else None
            keep = cfg.storage.keep("home", {"gold": cfg.walls.keep_gold, "elixir": cfg.walls.keep_elixir})
            village.upgrade_walls(self.game, cfg.walls, self.read_resources, keep, full)
        if (cfg.clan.donate or cfg.clan.request_troops) and self._in_clan is None:
            self._in_clan = clan.in_clan(self.game)
            if self._in_clan is False:
                log.info("Not in a clan: skipping troop requests and donations (join one in the game)")
        clan_ok = self._in_clan is not False
        self.checkpoint()
        if cfg.clan.donate and clan_ok:
            self.status("Donating")
            clan.donate(self.game, cfg.clan)
        if cfg.clan.request_troops and clan_ok:
            now = self.game.clock()
            if self._next_request is None or now >= self._next_request:
                self.status("Requesting troops")
                _, wait = clan.request_troops(self.game, cfg.clan)
                # Wait out the game's cooldown (read off the button), or the
                # configured interval when it couldn't be read.
                self._next_request = now + (wait + 30 if wait else cfg.clan.request_every_minutes * 60)

    def playing(self) -> str:
        """The village being farmed: "home" or "builder" (mode "auto" starts
        at home and moves to the builder base once the storages are full)."""
        mode = self.config.bot.mode
        if mode == "auto":
            return self.auto_village or self.config.bot.auto_first
        return mode

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
                               attacks=self.attacks, loot=self.gained)
            self.status("Stopped: storage full in both villages")
            return True
        self.auto_village = other
        log.info("Storage full in the %s: switching to the %s", names[village], names[other])
        self.notifier.send("Switching villages", f"Storage is full in the {names[village]}. Now farming the "
                           f"{names[other]}.", "on_storage_full", "info", attacks=self.attacks, loot=self.gained)
        self.status(f"Storage full: switching to the {names[other]}")
        return True

    def builder_cycle(self) -> bool:
        """One builder base battle (sailing over first if needed)."""
        if screens.settle(self.game) is not screens.Screen.BUILDER:
            self.status("Sailing to the builder base")
            builder.go_to_builder_base(self.game)
            self.game.refresh()
        if self.check_account():
            return self.cycle()
        village.clear_popups(self.game)
        bcfg = self.config.builder
        self.read_storage_max("builder")
        self.checkpoint()
        if bcfg.collect_resources:
            # The first battle of a session always collects.
            if self._since_collect is None or self._since_collect >= bcfg.collect_every:
                # A full storage's collectors can't be emptied (tapping them selects them instead).
                full = self.full_storages("builder")
                kinds = tuple(k for k in ("gold", "elixir", "gems") if k not in full)
                if {"gold", "elixir"} & set(kinds):
                    builder.collect_resources(self.game, kinds=kinds)
                else:
                    log.info("Builder storages full: not collecting")
                self._since_collect = 0
            else:
                log.info("Builder collectors: next in %d battle(s)", bcfg.collect_every - self._since_collect)
        if bcfg.upgrade:
            # Before walls (one builder is kept for them), and before mode
            # "auto" moves on: spending a full storage keeps this village going.
            full = self.full_storages("builder")
            if full & {"gold", "elixir"}:
                self.status("Upgrading (builder base)")
                upgrade.upgrade_builder_base(self.game, bcfg, self.read_resources, full)
        if self.config.bot.mode == "auto":
            # The builder base has gold and elixir storages (no dark elixir).
            wanted = [k for k in self.config.bot.stop_when_full if k in ("gold", "elixir")]
            if wanted and set(wanted) <= self.full_storages("builder"):
                self._village_full("builder")
                return False
        walls = 0
        if bcfg.walls:
            self.status("Upgrading builder base walls")
            keep = self.config.storage.keep("builder", {"gold": bcfg.walls_keep_gold,
                                                         "elixir": bcfg.walls_keep_elixir})
            walls = builder.upgrade_walls(self.game, bcfg, self.read_resources, keep)
        cart = False
        if bcfg.collect_cart:
            if self._cart_due is None:
                self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
            if self._since_cart >= self._cart_due:
                cart = builder.collect_elixir_cart(self.game)
                self._since_cart = 0
                self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
        self.checkpoint()
        self.game.zoom_out()
        before = self.read_resources()
        started = self.game.clock()
        self.status("Attacking (builder base)")
        result = builder.attack(self.game, bcfg,
                                on_stage=lambda n: self.status(f"Attacking (builder base): base {n}"))
        seconds = self.game.clock() - started
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

    def cycle(self) -> bool:
        """One round: look after the village, then (unless told not to)
        find a base and attack it. Returns True if an attack happened."""
        cfg = self.config
        self.ensure_game_running()
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
        self.maintain_village()
        before = self.read_resources()
        wanted = cfg.bot.stop_when_full
        if wanted and set(wanted) <= self.full_storages(loot=before):
            if cfg.bot.mode == "auto":  # on to the builder base instead of stopping
                self._village_full("home")
                return False
            self.storage_full = True
            log.info("Storage full (%s): stopping", ", ".join(wanted))
            full = {"gold": "gold", "elixir": "elixir", "dark": "dark elixir"}
            self.notifier.send("Storage full", f"Your {' and '.join(full.get(w, w) for w in wanted)} storage"
                               f"{'s are' if len(wanted) > 1 else ' is'} full, so the bot stopped.",
                               "on_storage_full", "good", attacks=self.attacks, loot=self.gained)
            self.status(f"Stopped: storage full ({', '.join(wanted)})")
            return False
        if cfg.search.maintain_only:
            return False
        least = cfg.storage.keep("home", {"gold": cfg.search.min_own_gold_share}).get("gold", 0)
        if least:
            gold = before.gold if before else search.own_gold(self.game, cfg.regions)
            if gold is not None and gold < least:
                self.status(f"Saving up: {gold:,} gold is below {least:,}")
                return False
        self.checkpoint()
        self.status("Searching for a base")
        search.start_search(self.game)
        target = search.search(self.game, cfg.search, cfg.regions, self._base_seen)
        self.status("Attacking")
        self._pending = AttackRecord(self._now(), target, before)
        self._pending.taken = attack.attack(self.game, cfg, target)
        self.attacks += 1
        self.attacks_today += 1
        self._since_break += 1
        log.info("Attack #%d done", self.attacks)
        # Read the gain as soon as we're home, so the last attack of a run
        # is logged with it too.
        if self.game.wait_for(T.BATTLE, timeout=20, interval=1):
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

        path = Path(folder) / f"{self._now():%Y%m%d-%H%M%S}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = sorted(path.parent.glob("[0-9]" * 8 + "-" + "[0-9]" * 6 + ".png"))  # only the bot's own
            for stale in old[:max(0, len(old) - ERROR_SCREENS_KEPT + 1)]:
                stale.unlink()
            cv2.imwrite(str(path), screen)
            log.info("Saved the screen to %s", path)
        except OSError as exc:
            log.warning("Could not save %s: %s", path, exc)

    def run(self) -> None:
        limit = self.config.bot.max_attacks
        try:
            while not limit or self.attacks < limit:
                self.checkpoint()
                reason = self.pause_reason()
                if reason:
                    self.status(f"Paused: {reason}")
                    self._rest(IDLE_WAIT)
                    continue
                attacked = False
                try:
                    attacked = self.cycle()
                    self.errors = 0
                    self._game_restarts = 0
                    if self.storage_full:
                        break
                except Stopped:
                    raise
                except Exception as exc:  # keep farming through recoverable failures
                    self.errors += 1
                    log.error("Cycle failed (%d in a row): %s", self.errors, exc)
                    self._save_error_screen()
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        self.errors = 0
                        try:
                            self.recover()
                        except Stopped:
                            raise
                        except Exception as exc:  # try again after the next failures
                            log.error("Recovery failed: %s", exc)
                self.take_break()
                self.status("Waiting")
                self._rest(self.config.bot.loop_delay if attacked else max(self.config.bot.loop_delay, IDLE_WAIT))
        except Stopped:
            log.info("Stopped")
        except Exception as exc:
            from farmbot.friendly import explain

            problem = explain(str(exc))
            self.notifier.send("Stopped by a problem", f"**{problem.title}**\n{problem.why}\n```{str(exc)[:900]}```",
                               "on_stopped", "error", attacks=self.attacks, loot=self.gained)
            raise
        self._settle_attack(None)
        if self.playing() == "builder" and self.config.builder.return_home_after:
            try:
                builder.go_to_home_village(self.game)
            except Exception as exc:  # the bot is stopping anyway
                log.info("Couldn't sail home: %s", exc)
        if not self.storage_full:
            self.status("Stopped")
        log.info("Finished after %d attack(s)", self.attacks)
        if not self.storage_full:
            g = self.gained
            self.notifier.send("Bot stopped", f"Finished after {self.attacks:,} "
                               f"attack{'' if self.attacks == 1 else 's'}.", "on_stopped", "summary",
                               attacks=self.attacks, loot=g)
