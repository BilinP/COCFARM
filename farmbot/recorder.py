"""Records what the bot sees and does, to replay it offline.

``RecordingDevice`` wraps a device: every call goes through unchanged (it
adds no input of its own), while screenshots are saved as ``.webp`` frames
and taps, swipes and the log lines of the loggers it listens to are
appended to ``events.jsonl``, each with the clock time and the frame
showing then. A screenshot is only saved when it differs from the last one
saved, or a second has gone by, so a battle polled every 0.3 s stays at a
few hundred frames.

The frames are the device's own (not scaled to the bot's coordinates), so a
replay goes through the same scaling as the live run.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger(__name__)

SAME = 2.0  # mean abs difference (0-255) under which a frame counts as unchanged
KEEP_EVERY = 1.0  # s: an unchanged frame is still saved this often
QUALITY = 90  # webp quality: high enough for template matching
EVENTS = "events.jsonl"


class _Handler(logging.Handler):
    def __init__(self, recorder: "RecordingDevice"):
        super().__init__(logging.INFO)
        self.recorder = recorder

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.recorder._event("log", level=record.levelname, text=record.getMessage())
        except Exception:  # never let the recording break the run
            pass


class RecordingDevice:
    """A device that records itself (see the module)."""

    def __init__(self, device, folder: str | Path, clock: Callable[[], float] = time.monotonic,
                 loggers: tuple[str, ...] = ("farmbot.tasks.capital",)):
        self.device = device
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.start = clock()
        self.frames = 0  # saved so far
        self.frame = -1  # the one showing (the last saved)
        self._last: np.ndarray | None = None
        self._last_t = -KEEP_EVERY
        self._events = open(self.folder / EVENTS, "a", encoding="utf-8")
        self._handler = _Handler(self)
        self._loggers = [logging.getLogger(name) for name in loggers]
        self._levels = [logger.level for logger in self._loggers]
        for logger in self._loggers:
            logger.addHandler(self._handler)
            if not logger.isEnabledFor(logging.INFO):  # its info lines are what the recording is for
                logger.setLevel(logging.INFO)

    # -- recording --------------------------------------------------------
    def _now(self) -> float:
        return round(self.clock() - self.start, 3)

    def _event(self, kind: str, **data) -> None:
        if self._events.closed:
            return
        self._events.write(json.dumps({"t": self._now(), "kind": kind, "frame": self.frame, **data}) + "\n")
        self._events.flush()

    def _keep(self, image: np.ndarray) -> None:
        t = self._now()
        last = self._last
        if last is not None and last.shape == image.shape and t - self._last_t < KEEP_EVERY:
            small = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA).astype(np.int16)
            if float(np.abs(small - last).mean()) < SAME:
                return
        name = f"{self.frames:05d}_{t:09.3f}.webp"
        try:
            cv2.imwrite(str(self.folder / name), image, [cv2.IMWRITE_WEBP_QUALITY, QUALITY])
        except cv2.error as exc:
            log.debug("Could not save frame %s: %s", name, exc)
            return
        self._last = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA).astype(np.int16)
        self._last_t = t
        self.frame = self.frames
        self.frames += 1
        self._event("frame", file=name, size=[int(image.shape[1]), int(image.shape[0])])

    def close(self) -> None:
        for logger, level in zip(self._loggers, self._levels):
            logger.removeHandler(self._handler)
            logger.setLevel(level)
        if not self._events.closed:
            self._events.close()

    def __enter__(self) -> "RecordingDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the device -------------------------------------------------------
    def screenshot(self) -> np.ndarray:
        image = self.device.screenshot()
        if image is not None and getattr(image, "size", 0):
            self._keep(image)
        return image

    def tap(self, x: int, y: int) -> None:
        self._event("tap", x=int(x), y=int(y))
        self.device.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._event("swipe", x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2), ms=int(duration_ms))
        self.device.swipe(x1, y1, x2, y2, duration_ms)

    def __getattr__(self, name: str):
        # Everything else (start_app, tap_many, drag, ...) goes straight through,
        # logged when it's an input; a missing one stays missing (hasattr checks).
        attr = getattr(self.device, name)
        if name in ("tap_many", "press", "drag", "zoom_out", "type_text") and callable(attr):
            def logged(*args, **kwargs):
                self._event(name, args=_plain(args), kwargs=_plain(kwargs))
                return attr(*args, **kwargs)
            return logged
        return attr


def _plain(value):
    """``value`` as JSON can hold it."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, np.generic):
        return value.item()
    return repr(value)


def load(folder: str | Path) -> list[dict]:
    """A recording's events, in order."""
    with open(Path(folder) / EVENTS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
