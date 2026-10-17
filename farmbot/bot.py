"""Main farming loop."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from farmbot import history
from farmbot.config import Config
from farmbot.game import Game, Stopped, T
from farmbot.history import AttackRecord
from farmbot.tasks import attack, builder, clan, search, village
from farmbot.tasks.search import Loot

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3
IDLE_WAIT = 60.0  # seconds between village rounds when not attacking
STARTUP_TIMEOUT = 90  # seconds to wait for the home village after starting the game


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
        self._next_request: float | None = None  # clock time the next troop request is due
        self._in_clan: bool | None = None  # checked once per run, in the chat
        self._on_resources = on_resources
        self._on_attack = on_attack
        self._on_builder = on_builder
        self.resources: Loot | None = None  # last reading of your resource bar
        self.gained = Loot(0, 0, 0)
        self._pending: AttackRecord | None = None  # attack waiting for its "after" reading
        self.storage_full = False  # set when the storages in bot.stop_when_full are all full
        self._since_cart = 0  # builder battles since the elixir cart was last emptied
        self._cart_due: int | None = None  # battles between cart visits, picked at random

    def status(self, text: str) -> None:
        log.debug("Status: %s", text)
        if self._on_status:
            self._on_status(text)

    def _base_seen(self, loot: Loot, accepted: bool) -> None:
        self.searched += 1
        if self._on_base:
            self._on_base(loot, accepted)

    # -- schedule -------------------------------------------------------
    def pause_reason(self) -> str | None:
        """Why the bot should not play right now, or ``None``."""
        sched = self.config.schedule
        now = self._now()
        if self._today != now.date():
            self._today, self.attacks_today = now.date(), 0
        if not sched.enabled:
            return None
        if now.hour not in sched.active_hours:
            upcoming = next(((now.hour + i) % 24 for i in range(1, 25) if (now.hour + i) % 24 in sched.active_hours), None)
            return "outside active hours" + (f" (resumes {upcoming:02d}:00)" if upcoming is not None else "")
        if sched.daily_attack_limit and self.attacks_today >= sched.daily_attack_limit:
            return f"daily limit of {sched.daily_attack_limit} attacks reached"
        return None

    # -- game -----------------------------------------------------------
    def ensure_game_running(self) -> None:
        device, package = self.game.device, self.config.device.package
        if device.foreground_package() != package:
            log.info("Starting %s", package)
            device.start_app(package)
            self.game.wait(5)
            # Loading takes 10-60 s depending on the PC; wait for the home
            # village rather than a fixed time (popups are cleared next).
            if not self.game.wait_for(T.BATTLE, timeout=STARTUP_TIMEOUT, interval=2):
                log.info("Home village not seen after %ds; carrying on", STARTUP_TIMEOUT)

    def restart_game(self) -> None:
        log.warning("Restarting the game after repeated errors")
        self.game.device.stop_app(self.config.device.package)
        self.game.wait(3)
        self.ensure_game_running()

    def read_resources(self) -> Loot | None:
        self.game.refresh()
        loot = search.own_resources(self.game, self.config.regions)
        if loot is not None:
            self.resources = loot
            if self._on_resources:
                self._on_resources(loot)
        return loot

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
        self.game.zoom_out()
        if self._pending:
            # Before collecting or training, which would blur the gain.
            self._settle_attack(self.read_resources())
        if cfg.bot.collect_resources:
            kinds = tuple(k for k in ("gold", "elixir", "dark") if getattr(cfg.bot, f"collect_{k}"))
            village.collect_resources(self.game, kinds)
        if cfg.bot.collect_loot_cart:
            village.collect_loot_cart(self.game)
        if cfg.walls.enabled:
            self.status("Upgrading walls")
            village.upgrade_walls(self.game, cfg.walls, self.read_resources)
        if (cfg.clan.donate or cfg.clan.request_troops) and self._in_clan is None:
            self._in_clan = clan.in_clan(self.game)
            if self._in_clan is False:
                log.info("Not in a clan: skipping troop requests and donations (join one in the game)")
        clan_ok = self._in_clan is not False
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

    def builder_cycle(self) -> bool:
        """One builder base battle (sailing over first if needed)."""
        village.leave_battle(self.game)
        village.clear_popups(self.game)
        self.status("Sailing to the builder base")
        builder.go_to_builder_base(self.game)
        village.clear_popups(self.game)
        bcfg = self.config.builder
        if bcfg.collect_resources:
            builder.collect_resources(self.game)
        walls = 0
        if bcfg.walls:
            self.status("Upgrading builder base walls")
            walls = builder.upgrade_walls(self.game, bcfg, self.read_resources)
        cart = False
        if bcfg.collect_cart:
            if self._cart_due is None:
                self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
            if self._since_cart >= self._cart_due:
                cart = builder.collect_elixir_cart(self.game)
                self._since_cart = 0
                self._cart_due = self.game.rng.randint(bcfg.cart_every_min, bcfg.cart_every_max)
        self.game.zoom_out()
        before = self.read_resources()
        started = self.game.clock()
        self.status("Attacking (builder base)")
        result = builder.attack(self.game, bcfg,
                                on_stage=lambda n: self.status(f"Attacking (builder base): base {n}"))
        seconds = self.game.clock() - started
        self._since_cart += 1
        self.attacks += 1
        self.attacks_today += 1
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
        if cfg.bot.mode == "builder":
            return self.builder_cycle()
        if builder.in_builder_base(self.game):
            builder.go_to_home_village(self.game)
        self.maintain_village()
        before = self.read_resources()
        wanted = cfg.bot.stop_when_full
        if wanted and set(wanted) <= search.full_storages(self.game, cfg.regions):
            self.storage_full = True
            log.info("Storage full (%s): stopping", ", ".join(wanted))
            self.status(f"Stopped: storage full ({', '.join(wanted)})")
            return False
        if cfg.search.maintain_only:
            return False
        if cfg.search.min_own_gold:
            gold = before.gold if before else search.own_gold(self.game, cfg.regions)
            if gold is not None and gold < cfg.search.min_own_gold:
                self.status(f"Saving up: {gold:,} gold is below {cfg.search.min_own_gold:,}")
                return False
        self.status("Searching for a base")
        search.start_search(self.game)
        target = search.search(self.game, cfg.search, cfg.regions, self._base_seen)
        self.status("Attacking")
        self._pending = AttackRecord(self._now(), target, before)
        self._pending.taken = attack.attack(self.game, cfg, target)
        self.attacks += 1
        self.attacks_today += 1
        log.info("Attack #%d done", self.attacks)
        # Read the gain as soon as we're home, so the last attack of a run
        # is logged with it too.
        if self.game.wait_for(T.BATTLE, timeout=20, interval=1):
            village.clear_popups(self.game)
            self._settle_attack(self.read_resources())
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
            cv2.imwrite(str(path), screen)
            log.info("Saved the screen to %s", path)
        except OSError as exc:
            log.warning("Could not save %s: %s", path, exc)

    def run(self) -> None:
        limit = self.config.bot.max_attacks
        try:
            while not limit or self.attacks < limit:
                reason = self.pause_reason()
                if reason:
                    self.status(f"Paused: {reason}")
                    self.game.wait(IDLE_WAIT)
                    continue
                attacked = False
                try:
                    attacked = self.cycle()
                    self.errors = 0
                    if self.storage_full:
                        break
                except Stopped:
                    raise
                except Exception as exc:  # keep farming through recoverable failures
                    self.errors += 1
                    log.error("Cycle failed (%d in a row): %s", self.errors, exc)
                    self._save_error_screen()
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        self.restart_game()
                        self.errors = 0
                self.status("Waiting")
                self.game.wait(self.config.bot.loop_delay if attacked else max(self.config.bot.loop_delay, IDLE_WAIT))
        except Stopped:
            log.info("Stopped")
        self._settle_attack(None)
        if self.config.bot.mode == "builder" and self.config.builder.return_home_after:
            try:
                builder.go_to_home_village(self.game)
            except Exception as exc:  # the bot is stopping anyway
                log.info("Couldn't sail home: %s", exc)
        if not self.storage_full:
            self.status("Stopped")
        log.info("Finished after %d attack(s)", self.attacks)
