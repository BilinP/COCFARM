"""COCFARM's bot on Android: the same farmbot package, with a device that
sees the screen through screen capture and taps through the app's
accessibility service (see Bridge.java). Called from BotService.java."""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from datetime import date
from logging.handlers import TimedRotatingFileHandler

import cv2
import numpy as np
from java import jarray, jclass, jint

from farmbot import __version__, catalog, history
from farmbot.bot import Bot
from farmbot.config import load_config
from farmbot.friendly import describe_status, explain
from farmbot.game import Game, Stopped
from farmbot.vision import TemplateLibrary, Vision

from cocfarm_settings import ensure_settings

Bridge = jclass("com.cocfarm.app.Bridge")
log = logging.getLogger("farmbot.android")
_stop = threading.Event()
_pause = threading.Event()  # set: wait at the next safe point (the floating button)
running_config = None  # the farming bot's settings: the settings screen changes them as it saves
# The UI scale measured on each screen size. It is measured from the village's
# buttons, so a run that starts mid-battle or on the results screen needs it.
SCALES_FILE = "ui_scales.json"
CACHE = "cache"  # pictures for the home screen: the last base scouted
OWN_PACKAGE = "com.cocfarm.app"
LOG_LINES = 400  # the newest log lines the app can show

# What this run has done, for the home screen (see snapshot()).
_session: dict = {}
_lines: collections.deque = collections.deque(maxlen=LOG_LINES)
_line_count = 0  # lines logged so far: the app asks for the ones after the last it has


class _Lines(logging.Handler):
    """Keeps the newest log lines for the app's log screen."""

    def emit(self, record: logging.LogRecord) -> None:
        global _line_count
        try:
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            _lines.append((record.levelno, f"{stamp}  {record.getMessage()}"))
            _line_count += 1
        except Exception:  # never let the log break the bot
            pass


class AndroidDevice:
    """farmbot's Device, on the phone itself."""

    def __init__(self, package: str = "com.supercell.clashofclans"):
        self.package = package
        self.played = False  # the game has been opened this run (until then, COCFARM's screen is where Start was pressed)

    def _in_game(self) -> None:
        """Touch only the game. While another app is in front (COCFARM's own
        screen, say), its taps would land on that app: wait for the game."""
        def elsewhere() -> bool:  # "": not known yet (the tap service just started), taken to be the game
            return str(Bridge.foregroundPackage()) not in ("", self.package)

        if not elsewhere():
            return
        log.info("Clash of Clans isn't in front: waiting for it before tapping")
        _status("Waiting for Clash of Clans to be in front")
        while elsewhere():
            if _stop.is_set():
                raise Stopped
            time.sleep(0.5)
        log.info("Clash of Clans is back: carrying on")
        _status("Resuming")

    def screenshot(self) -> np.ndarray:
        frame = Bridge.screenshot()
        rows = np.frombuffer(bytes(frame.data), np.uint8).reshape(frame.height, frame.rowStride)
        rgba = rows[:, :frame.width * 4].reshape(frame.height, frame.width, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)

    def tap(self, x: int, y: int) -> None:
        self._in_game()
        Bridge.tap(int(x), int(y))

    def tap_many(self, points: list[tuple[int, int]], pause: float = 0.0) -> None:
        self._in_game()
        if points:
            xs = jarray(jint)([int(x) for x, _ in points])
            ys = jarray(jint)([int(y) for _, y in points])
            Bridge.tapMany(xs, ys, int(pause * 1000))

    def zoom_out(self, times: int = 2) -> bool:
        self._in_game()
        return all(Bridge.pinch() for _ in range(times))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._in_game()
        Bridge.swipe(int(x1), int(y1), int(x2), int(y2), int(duration_ms))

    def back(self) -> bool:
        self._in_game()
        return bool(Bridge.back())

    def type_text(self, text: str) -> None:
        raise NotImplementedError("typing isn't needed on Android")

    def start_app(self, package: str) -> None:
        Bridge.startApp(package)
        self.played = True

    def game_shown(self) -> None:
        """The screenshots show the game (after start_app): it's in front."""
        Bridge.gameInFront(self.package)

    def stop_app(self, package: str) -> None:
        Bridge.stopApp(package)

    def foreground_package(self) -> str:
        app = str(Bridge.foregroundPackage())
        self.played |= app == self.package
        if app == OWN_PACKAGE and self.played:  # the player is looking at COCFARM: no reason to open the game over it
            log.info("COCFARM's screen is open: waiting for it to close")
            _status("Waiting while COCFARM's screen is open")
            while str(Bridge.foregroundPackage()) == OWN_PACKAGE:
                if _stop.is_set():
                    raise Stopped
                time.sleep(0.5)
            app = str(Bridge.foregroundPackage())
            _status("Resuming")
        return app

    def screen_size(self) -> tuple[int, int]:
        width, height = Bridge.screenSize()
        return int(width), int(height)


def _logging(folder: str) -> None:
    root = logging.getLogger()
    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        return
    os.makedirs(os.path.join(folder, "logs"), exist_ok=True)
    handler = TimedRotatingFileHandler(os.path.join(folder, "logs", "farmbot.log"), when="midnight", backupCount=14,
                                       encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())  # to logcat
    root.addHandler(_Lines())
    root.setLevel(logging.INFO)


def _scales(folder: str) -> dict[str, float]:
    try:
        with open(os.path.join(folder, SCALES_FILE), encoding="utf-8") as fh:
            return {str(k): float(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _remember_scale(folder: str, size: str, scale: float) -> None:
    scales = _scales(folder)
    if scales.get(size) != scale:
        scales[size] = scale
        with open(os.path.join(folder, SCALES_FILE), "w", encoding="utf-8") as fh:
            json.dump(scales, fh, indent=1)


def _new_session() -> None:
    _session.clear()
    _session.update(started=time.time(), state="running", headline="Starting", detail="", bases=0, attacks=0,
                    gained=[0, 0, 0], base=None, attack=None, problem=None,
                    builder={"battles": 0, "second": 0, "gold": 0, "elixir": 0, "last": None})


def _status(text: str) -> None:
    shown = describe_status(text)
    _session.update(state=shown.state, headline=shown.headline, detail=shown.detail)
    Bridge.status(shown.headline)


def _base_seen(folder: str, game: Game, cfg, loot, accepted: bool) -> None:
    _session["bases"] += 1
    search = cfg.search
    _session["base"] = {"gold": loot.gold, "elixir": loot.elixir, "dark": loot.dark, "accepted": accepted,
                        "minimums": [search.min_gold, search.min_elixir, search.min_dark], "time": time.time()}
    screen = game.screen
    if screen is not None:  # for the home screen, and "What the bot saw"
        try:
            os.makedirs(os.path.join(folder, CACHE), exist_ok=True)
            cv2.imwrite(os.path.join(folder, CACHE, "base.png"), screen)
            small = cv2.resize(screen, (720, round(screen.shape[0] * 720 / screen.shape[1])), interpolation=cv2.INTER_AREA)
            cv2.imwrite(os.path.join(folder, CACHE, "base.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception as exc:
            log.debug("Couldn't keep the base's picture: %s", exc)
    if accepted and search.sound_on_match:
        Bridge.ding()


def _attacked(record) -> None:
    _session["attacks"] += 1
    gained = record.gained
    if gained:
        _session["gained"] = [a + b for a, b in zip(_session["gained"], (gained.gold, gained.elixir, gained.dark))]
    _session["attack"] = {"time": time.time(), "target": [record.target.gold, record.target.elixir, record.target.dark],
                          "taken": record.taken, "gained": [gained.gold, gained.elixir, gained.dark] if gained else None}


def _builder_battle(battle: dict) -> None:
    b = _session["builder"]
    b["battles"] += 1
    b["second"] += battle.get("stages", 0) >= 2
    gained = battle.get("gained")
    if gained:
        b["gold"] += gained.gold
        b["elixir"] += gained.elixir
    b["last"] = {"stages": battle.get("stages", 0), "seconds": int(battle.get("seconds", 0)),
                 "gained": [gained.gold, gained.elixir] if gained else None}


def run(folder: str) -> None:
    """Farm until stop() (or a problem). ``folder`` holds farmbot.toml,
    templates/ and the logs, as COCFARM's folder does on Windows."""
    global running_config
    _stop.clear()
    _pause.clear()
    _new_session()
    os.chdir(folder)
    _logging(folder)
    log.info("COCFARM %s on Android", __version__)
    cfg = load_config(ensure_settings(folder))
    library = TemplateLibrary(cfg.bot.templates)
    missing = [] if cfg.search.maintain_only else catalog.missing(library, required_only=True)
    if missing:
        _problem("pictures missing: " + ", ".join(p.title for p in missing))
        Bridge.status("Add the button pictures first: " + ", ".join(p.title for p in missing) + ".")
        return
    _status("Starting")
    running_config = cfg
    device = AndroidDevice(cfg.device.package)
    size = "%dx%d" % device.screen_size()
    game = Game(device, Vision(library, cfg.bot.threshold), stop_event=_stop,
                ui_scale=cfg.device.ui_scale or _scales(folder).get(size, 0.0),
                window_scale=cfg.device.window_scale, picture_scales=cfg.device.picture_scales)
    try:
        Bot(game, cfg, on_status=_status, pause_event=_pause,
            on_base=lambda loot, ok: _base_seen(folder, game, cfg, loot, ok),
            on_attack=_attacked, on_builder=_builder_battle).run()
    except Exception as exc:  # the bot gave up: say why, in plain words
        log.exception("The bot stopped")
        problem = _problem(str(exc))
        Bridge.status(problem.title)
        return
    finally:
        running_config = None
        _session["ended"] = time.time()
        if not cfg.device.ui_scale and game.ui_scale:
            _remember_scale(folder, size, game.ui_scale)
    _session.update(state="stopped", headline="Stopped", detail="")
    Bridge.status("Stopped")


def _problem(message: str):
    problem = explain(message, "the game")
    _session.update(state="problem", headline=problem.title, detail=problem.why,
                    problem={"title": problem.title, "why": problem.why, "action": problem.action,
                             "details": problem.details})
    return problem


def snapshot(folder: str) -> str:
    """What this run has done, and today's totals, for the home screen."""
    out = dict(_session)
    out["paused"] = _pause.is_set()
    out["seconds"] = int((_session.get("ended") or time.time()) - _session["started"]) if _session.get("started") else 0
    out["lines"] = _line_count
    cfg = running_config
    path = os.path.join(folder, cfg.bot.history if cfg else "attacks.csv")
    count, gained = history.totals_for(path, date.today())
    out["today"] = {"attacks": count, "gained": [gained.gold, gained.elixir, gained.dark]}
    return json.dumps(out)


def log_lines(after: int) -> str:
    """Log lines after the first ``after`` (as many as are still kept): [count, [[level, text], ...]]."""
    kept = list(_lines)
    first = _line_count - len(kept)  # the number of the oldest line kept
    new = kept[max(0, after - first):]
    return json.dumps([_line_count, new])


def toggle_pause() -> bool:
    """Pause at the next safe point (never mid-battle), or carry on. True if now paused."""
    if _pause.is_set():
        _pause.clear()
    else:
        _pause.set()
        Bridge.status("Pausing after what it's doing now…")
    return _pause.is_set()


def stop() -> None:
    _stop.set()
    Bridge.status("Stopping…")
