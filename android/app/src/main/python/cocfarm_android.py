"""COCFARM's bot on Android: the same farmbot package, with a device that
sees the screen through screen capture and taps through the app's
accessibility service (see Bridge.java). Called from BotService.java."""

from __future__ import annotations

import logging
import os
import threading
from logging.handlers import TimedRotatingFileHandler

import cv2
import numpy as np
from java import jarray, jclass, jint

from farmbot import __version__, catalog
from farmbot.bot import Bot
from farmbot.config import ensure_config, load_config
from farmbot.game import Game
from farmbot.vision import TemplateLibrary, Vision

Bridge = jclass("com.cocfarm.app.Bridge")
log = logging.getLogger("farmbot.android")
_stop = threading.Event()


class AndroidDevice:
    """farmbot's Device, on the phone itself."""

    def screenshot(self) -> np.ndarray:
        frame = Bridge.screenshot()
        rows = np.frombuffer(bytes(frame.data), np.uint8).reshape(frame.height, frame.rowStride)
        rgba = rows[:, :frame.width * 4].reshape(frame.height, frame.width, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)

    def tap(self, x: int, y: int) -> None:
        Bridge.tap(int(x), int(y))

    def tap_many(self, points: list[tuple[int, int]], pause: float = 0.0) -> None:
        if points:
            xs = jarray(jint)([int(x) for x, _ in points])
            ys = jarray(jint)([int(y) for _, y in points])
            Bridge.tapMany(xs, ys, int(pause * 1000))

    def zoom_out(self, times: int = 2) -> bool:
        return all(Bridge.pinch() for _ in range(times))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        Bridge.swipe(int(x1), int(y1), int(x2), int(y2), int(duration_ms))

    def type_text(self, text: str) -> None:
        raise NotImplementedError("typing isn't needed on Android")

    def start_app(self, package: str) -> None:
        Bridge.startApp(package)

    def stop_app(self, package: str) -> None:
        Bridge.stopApp(package)

    def foreground_package(self) -> str:
        return str(Bridge.foregroundPackage())

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
    root.setLevel(logging.INFO)


def run(folder: str) -> None:
    """Farm until stop() (or a problem). ``folder`` holds farmbot.toml,
    templates/ and the logs, as COCFARM's folder does on Windows."""
    _stop.clear()
    os.chdir(folder)
    _logging(folder)
    log.info("COCFARM %s on Android", __version__)
    ensure_config("farmbot.toml")
    cfg = load_config("farmbot.toml")
    library = TemplateLibrary(cfg.bot.templates)
    missing = [] if cfg.search.maintain_only else catalog.missing(library, required_only=True)
    if missing:
        Bridge.status("Add the button pictures first: " + ", ".join(p.title for p in missing)
                      + ". They go in the templates folder shown above.")
        return
    Bridge.status("Starting…")
    game = Game(AndroidDevice(), Vision(library, cfg.bot.threshold), stop_event=_stop, ui_scale=cfg.device.ui_scale,
                window_scale=cfg.device.window_scale, picture_scales=cfg.device.picture_scales)
    Bot(game, cfg, on_status=lambda text: Bridge.status(text)).run()
    Bridge.status("Stopped")


def stop() -> None:
    _stop.set()
    Bridge.status("Stopping…")
