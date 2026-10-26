"""Android device access over the stock ``adb`` command-line tool.

Everything goes through plain, documented ADB/shell commands
(``screencap``, ``input tap``, ``input swipe``, ``am``), so any emulator
that exposes ADB (BlueStacks 5, LDPlayer, MEmu, Nox, a real phone) works.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], bytes]


class DeviceError(RuntimeError):
    pass


class Device(Protocol):
    def screenshot(self) -> np.ndarray: ...
    def tap(self, x: int, y: int) -> None: ...
    # Optional: def tap_many(self, points, pause=0.0) -> None, for quick bursts.
    # Optional: def back(self) -> bool, Android's Back button.
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...
    def type_text(self, text: str) -> None: ...
    def start_app(self, package: str) -> None: ...
    def stop_app(self, package: str) -> None: ...
    def foreground_package(self) -> str: ...


# On Windows, stop each adb call from flashing a console window when farmbot
# runs without one (farmbot.exe, pythonw).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str]) -> bytes:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False, creationflags=NO_WINDOW)
    except FileNotFoundError as exc:
        raise DeviceError(f"adb not found: {cmd[0]!r}. Install Android platform-tools.") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"adb timed out: {' '.join(cmd)}") from exc
    if result.returncode != 0:
        raise DeviceError(f"adb failed ({result.returncode}): {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


RAW_FORMATS = {1, 2}  # RGBA_8888, RGBX_8888


def decode_raw(data: bytes) -> np.ndarray | None:
    """Uncompressed ``screencap`` output → BGR image, or ``None``.

    The header is width, height and pixel format (and, on newer Android,
    a colour space), each a little-endian uint32, followed by 4 bytes per
    pixel. Skipping PNG compression on the device makes screenshots several
    times faster.
    """
    if len(data) < 12:
        return None
    w, h, fmt = struct.unpack_from("<III", data)
    pixels = w * h * 4
    header = len(data) - pixels
    if fmt not in RAW_FORMATS or not w or not h or header not in (12, 16):
        return None
    rgba = np.frombuffer(data, np.uint8, pixels, header).reshape(h, w, 4)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)


@dataclass(frozen=True)
class TouchScreen:
    """The touchscreen's input device, for writing taps straight into it."""

    path: str  # e.g. /dev/input/event4
    max_x: int
    max_y: int
    event_size: int = 24  # struct input_event: 24 bytes on 64-bit Android, 16 on 32-bit
    # Multitouch protocol B (slots and tracking ids, code 002f), which many
    # emulators use; Android ignores protocol A events on such a device.
    slots: bool = False
    btn_touch: bool = False  # it reports BTN_TOUCH (key 014a) with each touch
    pressure: int = 0  # ABS_MT_PRESSURE's maximum (003a); 0: it has none


def parse_touchscreen(getevent: str, event_size: int = 24) -> TouchScreen | None:
    """The first device in ``getevent -p`` output that reports multitouch
    positions (ABS_MT_POSITION_X/Y, codes 0035/0036) with their ranges."""
    path: str | None = None
    max_x = max_y = 0
    slots = btn_touch = False
    pressure = 0
    for line in getevent.splitlines():
        added = re.match(r"add device \d+: (\S+)", line)
        if added:
            if path and max_x and max_y:
                break
            path, max_x, max_y, slots, btn_touch, pressure = added.group(1), 0, 0, False, False, 0
            continue
        if not path:
            continue
        axis = re.search(r"\b(0035|0036)\s*:.*\bmax (\d+)", line)
        if axis:
            if axis.group(1) == "0035":
                max_x = int(axis.group(2))
            else:
                max_y = int(axis.group(2))
        if re.search(r"\b002f\s*:", line):
            slots = True
        if re.search(r"\b014a\b", line):
            btn_touch = True
        most = re.search(r"\b003a\s*:.*\bmax (\d+)", line)
        if most:
            pressure = int(most.group(1))
    return (TouchScreen(path, max_x, max_y, event_size, slots, btn_touch, pressure)
            if path and max_x and max_y else None)


def _packer(touch: TouchScreen):
    fmt = "<qqHHi" if touch.event_size == 24 else "<iiHHi"

    def event(kind: int, code: int, value: int) -> bytes:
        return struct.pack(fmt, 0, 0, kind, code, value)

    return event


def _scaled(x: float, y: float, size: tuple[int, int], touch: TouchScreen) -> tuple[int, int]:
    w, h = size
    return (round(min(max(x, 0), w - 1) * touch.max_x / max(w - 1, 1)),
            round(min(max(y, 0), h - 1) * touch.max_y / max(h - 1, 1)))


def tap_events(touch: TouchScreen) -> int:
    """How many events :func:`touch_events` writes per tap."""
    return (11 + (2 if touch.pressure else 0) if touch.slots else 6) + (2 if touch.btn_touch else 0)


def _position(event, touch: TouchScreen, x: int, y: int) -> bytes:
    """A finger's position. On a slots (protocol B) touchscreen each value
    goes twice, one off first: the kernel drops a value equal to the slot's
    last one, and Android 14 forgets the slot's position when a new touch
    starts, so a repeated value (both pinch fingers level) would be lost."""
    if not touch.slots:
        return event(EV_ABS, ABS_MT_POSITION_X, x) + event(EV_ABS, ABS_MT_POSITION_Y, y)

    def nudged(v: int, most: int) -> int:
        return v + 1 if v < most else v - 1

    data = (event(EV_ABS, ABS_MT_POSITION_X, nudged(x, touch.max_x)) + event(EV_ABS, ABS_MT_POSITION_X, x)
            + event(EV_ABS, ABS_MT_POSITION_Y, nudged(y, touch.max_y)) + event(EV_ABS, ABS_MT_POSITION_Y, y))
    if touch.pressure:
        # Android 14 counts a finger with no pressure as hovering, not
        # touching: a pinch's second finger was ignored and it panned.
        data += event(EV_ABS, ABS_MT_PRESSURE, nudged(1, touch.pressure)) + event(EV_ABS, ABS_MT_PRESSURE, 1)
    return data


def touch_events(points: list[tuple[int, int]], size: tuple[int, int], touch: TouchScreen) -> bytes:
    """One finger down-and-up per point, as raw ``input_event`` structs in
    the device's multitouch protocol, one fixed-size block per tap."""
    event = _packer(touch)
    sync, mt_sync = event(EV_SYN, SYN_REPORT, 0), event(EV_SYN, SYN_MT_REPORT, 0)
    data = bytearray()
    for i, (x, y) in enumerate(points):
        tx, ty = _scaled(x, y, size, touch)
        down = up = b""
        if touch.btn_touch:
            down, up = event(EV_KEY, BTN_TOUCH, 1), event(EV_KEY, BTN_TOUCH, 0)
        if touch.slots:
            data += event(EV_ABS, ABS_MT_SLOT, 0) + event(EV_ABS, ABS_MT_TRACKING_ID, i % 60000 + 1)
            data += _position(event, touch, tx, ty)
            data += down + sync  # finger down
            data += event(EV_ABS, ABS_MT_SLOT, 0) + event(EV_ABS, ABS_MT_TRACKING_ID, -1)
            data += up + sync  # finger up
            data += sync  # keeps the block the same size as protocol A's
        else:
            data += event(EV_ABS, ABS_MT_POSITION_X, tx) + event(EV_ABS, ABS_MT_POSITION_Y, ty)
            data += down + mt_sync + sync  # finger down
            data += up + mt_sync + sync  # finger up: a report with no fingers
    return bytes(data)


def pinch_events(center: tuple[int, int], start: int, end: int, steps: int, size: tuple[int, int],
                 touch: TouchScreen) -> tuple[bytes, bytes]:
    """Two fingers level with ``center``, ``start`` px either side of it,
    moving to ``end`` px (closer together zooms out). Kept level so neither
    finger starts on a button (a finger on Next makes the game see a
    one-finger drag, which pans instead). Returns the frames, each one
    fixed-size block, and the release that lifts both fingers."""
    event = _packer(touch)
    w, h = size
    cx, cy = center
    frames = bytearray()
    for i in range(steps + 1):
        d = start + (end - start) * i / steps
        for finger, sign in enumerate((-1, 1)):
            tx, ty = _scaled(cx + sign * d, cy, size, touch)
            if touch.slots:
                # The tracking id is set on every frame so each block stands
                # alone; repeating it for a finger already down is harmless.
                frames += event(EV_ABS, ABS_MT_SLOT, finger) + event(EV_ABS, ABS_MT_TRACKING_ID, 60001 + finger)
            frames += _position(event, touch, tx, ty)
            if not touch.slots:
                frames += event(EV_SYN, SYN_MT_REPORT, 0)
        if touch.btn_touch:
            frames += event(EV_KEY, BTN_TOUCH, 1)
        frames += event(EV_SYN, SYN_REPORT, 0)
    if touch.slots:
        release = b"".join(event(EV_ABS, ABS_MT_SLOT, f) + event(EV_ABS, ABS_MT_TRACKING_ID, -1) for f in (0, 1))
    else:
        release = event(EV_SYN, SYN_MT_REPORT, 0)
    if touch.btn_touch:
        release += event(EV_KEY, BTN_TOUCH, 0)
    release += event(EV_SYN, SYN_REPORT, 0)
    return bytes(frames), release


EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, SYN_MT_REPORT = 0, 2
ABS_MT_SLOT, ABS_MT_TRACKING_ID, ABS_MT_PRESSURE = 0x2F, 0x39, 0x3A
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
BTN_TOUCH = 0x14A
TAPS_FILE = "/data/local/tmp/farmbot_taps"
PINCH_FILE = "/data/local/tmp/farmbot_pinch"
RELEASE_FILE = "/data/local/tmp/farmbot_release"
PINCH_STEPS = 12
INPUT_TAPS_PER_SHELL = 40  # keeps the fallback command short enough for old adb


class AdbDevice:
    """A device reachable as ``serial`` (e.g. ``127.0.0.1:5555``)."""

    def __init__(self, serial: str, adb_path: str = "adb", runner: Runner = _run):
        self.serial = serial
        self.adb_path = adb_path
        self._run = runner
        self._raw_screencap = True  # until the device sends something we can't decode
        self._touch: TouchScreen | None | bool = None  # None: not looked for yet; False: none usable
        self._dd = True  # dd works in the shell (Android 14's toybox has none: tail and head stand in)
        self._size: tuple[int, int] | None = None  # screen size, from the last screenshot

    def _adb(self, *args: str) -> bytes:
        try:
            return self._run([self.adb_path, "-s", self.serial, *args])
        except DeviceError as exc:
            # Another emulator's adb (a different version) restarting the
            # adb server drops our connection: reconnect once and retry.
            if ":" not in self.serial or not any(s in str(exc) for s in ("not found", "offline", "closed")):
                raise
            log.info("Lost the connection to %s; reconnecting", self.serial)
            time.sleep(1)
            self.connect()
            return self._run([self.adb_path, "-s", self.serial, *args])

    def shell(self, *args: str) -> str:
        return self._adb("shell", *args).decode(errors="replace")

    def connect(self) -> None:
        """``adb connect`` for TCP serials; a no-op for USB serials."""
        if ":" in self.serial:
            out = self._run([self.adb_path, "connect", self.serial]).decode(errors="replace")
            if "connected" not in out:
                raise DeviceError(f"could not connect to {self.serial}: {out.strip()}")
        log.info("Using device %s", self.serial)

    def screenshot(self) -> np.ndarray:
        image = self._screencap()
        self._size = (image.shape[1], image.shape[0])
        return image

    def _screencap(self) -> np.ndarray:
        if self._raw_screencap:
            image = decode_raw(self._adb("exec-out", "screencap"))
            if image is not None:
                return image
            log.info("Raw screenshots not understood; using PNG")
            self._raw_screencap = False
        png = self._adb("exec-out", "screencap", "-p")
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DeviceError("screencap returned no decodable image")
        return image

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def back(self) -> bool:
        self.shell("input", "keyevent", "KEYCODE_BACK")
        return True

    def _touchscreen(self) -> TouchScreen | None:
        if self._touch is None:
            try:
                abi = self.shell("getprop", "ro.product.cpu.abi")
                found = parse_touchscreen(self.shell("getevent", "-p"), 24 if "64" in abi else 16)
                if found:
                    self._dd = "no such tool" not in self.shell("echo | dd of=/dev/null 2>&1; true").lower()
            except DeviceError:
                found = None
            log.info("Touchscreen: %s%s", found, "" if self._dd else " (no dd: writing with tail and head)")
            self._touch = found or False
        return self._touch or None

    def stop_fast_taps(self) -> bool:
        """Tap with ``input tap`` from now on, for when taps written to the
        touchscreen don't reach the game. False if it already did."""
        if not self._touch:
            self._touch = False
            return False
        log.warning("Taps written to the touchscreen (%s) are not reaching the game; using input tap",
                    self._touch.path)
        self._touch = False
        return True

    def tap_many(self, points: list[tuple[int, int]], pause: float = 0.0) -> None:
        """Tap every point quickly, ``pause`` seconds apart at most.

        Taps are written straight into the touchscreen device (as
        ClashFarmer does): one file pushed and played back in one shell
        call, about 50 ms a tap, against 100-150 ms for each ``input tap``.
        Falls back to chained ``input tap`` commands.
        """
        points = [(int(x), int(y)) for x, y in points]
        if not points:
            return
        touch = self._touchscreen()
        if touch and self._size:
            try:
                self._write_taps(points, touch, pause)
                return
            except DeviceError as exc:
                log.info("Writing taps to the touchscreen failed (%s); using input tap", exc)
                self._touch = False
        between = f"; sleep {pause:.2f}; " if pause >= 0.02 else "; "
        for i in range(0, len(points), INPUT_TAPS_PER_SHELL):
            chunk = points[i:i + INPUT_TAPS_PER_SHELL]
            self.shell(between.join(f"input tap {x} {y}" for x, y in chunk))

    def zoom_out(self, times: int = 2) -> bool:
        """Pinch the view all the way out (the game remembers its zoom, and
        a zoomed-in battle hides the edges troops are dropped on). False if
        the touchscreen can't be written to."""
        touch = self._touchscreen()
        if not touch:
            return False
        if not self._size:
            self.screenshot()
        w, h = self._size
        # The fingers stop well apart: closing them on one spot reads as a tap
        # and selects whatever building is there.
        frames, release = pinch_events((w // 2, round(h * 0.45)), w // 3, w // 8, PINCH_STEPS, (w, h), touch)
        block = len(frames) // (PINCH_STEPS + 1)
        for name, data in ((PINCH_FILE, frames), (RELEASE_FILE, release)):
            with tempfile.NamedTemporaryFile(prefix="farmbot_pinch", delete=False) as fh:
                fh.write(data)
            try:
                self._adb("push", fh.name, name)
            finally:
                os.unlink(fh.name)
        play = (f"i=0; while [ $i -le {PINCH_STEPS} ]; do {self._write_block(PINCH_FILE, touch.path, block)}; "
                f"sleep 0.02; i=$((i+1)); done; "
                f"cat {RELEASE_FILE} > {touch.path}; sleep 0.3")
        self.shell("; ".join([play] * times))
        return True

    def _write_taps(self, points: list[tuple[int, int]], touch: TouchScreen, pause: float) -> None:
        data = touch_events(points, self._size, touch)
        block = tap_events(touch) * touch.event_size
        with tempfile.NamedTemporaryFile(prefix="farmbot_taps", delete=False) as fh:
            fh.write(data)
        try:
            self._adb("push", fh.name, TAPS_FILE)
        finally:
            os.unlink(fh.name)
        # Starting dd takes ~50 ms already, so only longer pauses need a sleep.
        sleep = f"sleep {pause - 0.05:.2f}; " if pause - 0.05 >= 0.02 else ""
        self.shell(f"i=0; while [ $i -lt {len(points)} ]; do "
                   f"{self._write_block(TAPS_FILE, touch.path, block)}; "
                   f"{sleep}i=$((i+1)); done")

    def _write_block(self, source: str, device: str, block: int) -> str:
        """Shell to copy block number ``$i`` of ``source`` into ``device``,
        in one write (the input device takes whole events only)."""
        if self._dd:
            return f"dd if={source} of={device} bs={block} skip=$i count=1 2>/dev/null"
        return f"tail -c +$((i*{block}+1)) {source} | head -c {block} > {device}"

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell("input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, duration_ms)))

    def type_text(self, text: str) -> None:
        """Type into the focused field (``input text`` needs spaces as %s)."""
        safe = "".join(c for c in text if c.isalnum() or c in " .,!?-")
        self.shell("input", "text", safe.replace(" ", "%s"))

    def start_app(self, package: str) -> None:
        # `am start` on the launcher activity: LDPlayer's Android has no
        # `monkey`. Android 6 and older can't resolve it, so fall back there.
        try:
            out = self.shell("cmd", "package", "resolve-activity", "--brief",
                             "-c", "android.intent.category.LAUNCHER", package)
        except DeviceError:
            out = ""
        activity = next((line.strip() for line in reversed(out.splitlines())
                         if line.strip().startswith(package + "/")), None)
        if activity:
            self.shell("am", "start", "-n", activity)
        else:
            self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    def stop_app(self, package: str) -> None:
        self.shell("am", "force-stop", package)

    def foreground_package(self) -> str:
        out = self.shell("dumpsys", "window")
        for line in out.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                for token in line.replace("}", " ").split():
                    if "/" in token:
                        return token.split("/")[0]
        return ""

    def screen_size(self) -> tuple[int, int]:
        out = self.shell("wm", "size")
        size = out.strip().rsplit(" ", 1)[-1]
        w, h = size.split("x")
        return int(w), int(h)
