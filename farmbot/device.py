"""Android device access over the stock ``adb`` command-line tool.

Everything goes through plain, documented ADB/shell commands
(``screencap``, ``input tap``, ``input swipe``, ``am``), so any emulator
that exposes ADB (BlueStacks 5, LDPlayer, MEmu, Nox, a real phone) works.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], bytes]


class DeviceError(RuntimeError):
    pass


class NoAnswer(DeviceError):
    """The emulator was found but its ADB never answered: closing it and
    starting it again may help."""


class GaveUp(DeviceError):
    """The emulator was restarted and still didn't answer: the bot stops."""


class Device(Protocol):
    def screenshot(self) -> np.ndarray: ...
    def tap(self, x: int, y: int) -> None: ...
    # Optional: def tap_many(self, points, pause=0.0) -> None, for quick bursts.
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


class _NoServer(Exception):
    """No adb server is listening (yet): adb.exe starts one."""


SERVER_TIMEOUT = 30  # seconds, as adb.exe's calls get


def _read_exact(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise DeviceError("adb failed: closed by the adb server")
        data += chunk
    return bytes(data)


def _request(sock: socket.socket, message: str) -> None:
    sock.sendall(f"{len(message):04x}{message}".encode())
    status = _read_exact(sock, 4)
    if status == b"OKAY":
        return
    if status != b"FAIL":
        raise DeviceError(f"adb failed: the server said {status!r}")
    reason = _read_exact(sock, int(_read_exact(sock, 4), 16)).decode(errors="replace")
    raise DeviceError(f"adb failed: {reason}")


def _server_features(port: int, serial: str) -> set[str]:
    """What the device's adbd can do (e.g. "shell_v2"), as the server knows it."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=SERVER_TIMEOUT)
    except OSError as exc:
        raise _NoServer() from exc
    with sock:
        try:
            _request(sock, f"host-serial:{serial}:features")
            text = _read_exact(sock, int(_read_exact(sock, 4), 16)).decode(errors="replace")
        except OSError as exc:
            raise DeviceError(f"adb failed: connection closed ({exc})") from exc
    return set(text.strip().split(","))


def _server_call(port: int, serial: str, service: str, v2: bool = False) -> bytes:
    """Run ``service`` on ``serial`` through the adb server on ``port``:
    its output (raising like adb.exe on a failed command with shell v2)."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=SERVER_TIMEOUT)
    except OSError as exc:
        raise _NoServer() from exc
    with sock:
        try:
            _request(sock, f"host:transport:{serial}")
            _request(sock, service)
            if not v2:
                chunks = []
                while chunk := sock.recv(1 << 20):
                    chunks.append(chunk)
                return b"".join(chunks)
            out, err = bytearray(), bytearray()
            while True:
                head = sock.recv(5)
                if not head:
                    return bytes(out)  # closed without an exit code
                if len(head) < 5:
                    head += _read_exact(sock, 5 - len(head))
                kind, size = head[0], struct.unpack("<I", head[1:])[0]
                data = _read_exact(sock, size)
                if kind == 1:
                    out += data
                elif kind == 2:
                    err += data
                elif kind == 3:
                    code = data[0] if data else 0
                    if code:
                        raise DeviceError(f"adb failed ({code}): {err.decode(errors='replace').strip()}")
                    return bytes(out)
        except socket.timeout as exc:
            raise DeviceError(f"adb timed out: {service}") from exc
        except OSError as exc:
            raise DeviceError(f"adb failed: connection closed ({exc})") from exc


SYNC_CHUNK = 64 * 1024  # the most adb's sync protocol takes in one DATA packet


def _server_push(port: int, serial: str, data: bytes, remote: str, mode: int = 0o644) -> None:
    """``adb push`` of ``data`` to ``remote``, spoken to the adb server (its
    sync protocol: SEND, DATA chunks, DONE, then OKAY or FAIL back)."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=SERVER_TIMEOUT)
    except OSError as exc:
        raise _NoServer() from exc
    with sock:
        try:
            _request(sock, f"host:transport:{serial}")
            _request(sock, "sync:")
            target = f"{remote},{mode}".encode()
            packets = [b"SEND" + struct.pack("<I", len(target)) + target]
            for i in range(0, len(data), SYNC_CHUNK):
                chunk = data[i:i + SYNC_CHUNK]
                packets.append(b"DATA" + struct.pack("<I", len(chunk)) + chunk)
            packets.append(b"DONE" + struct.pack("<I", int(time.time())))
            sock.sendall(b"".join(packets))
            status, size = _read_exact(sock, 4), struct.unpack("<I", _read_exact(sock, 4))[0]
            if status == b"FAIL":
                raise DeviceError(f"adb failed: push {remote}: {_read_exact(sock, size).decode(errors='replace')}")
            if status != b"OKAY":
                raise DeviceError(f"adb failed: push {remote}: the server said {status!r}")
            sock.sendall(b"QUIT" + struct.pack("<I", 0))
        except socket.timeout as exc:
            raise DeviceError(f"adb timed out: push {remote}") from exc
        except OSError as exc:
            raise DeviceError(f"adb failed: connection closed ({exc})") from exc


# COCFARM's own adb server, away from the usual 5037: every emulator ships
# its own adb, and an adb of another version finding the shared server
# restarts it, dropping every connection ("device 'emulator-5554' not
# found"). On its own port, no other adb ever talks to COCFARM's server.
SERVER_PORT = 15037
_announced: set[str] = set()  # the serials "Using device" was logged for
_ports: dict[str, int] = {}
_ports_lock = threading.Lock()


def switch_off_ad_apps(device: "Device", packages: tuple[str, ...], emulator: str) -> None:
    """Switch off the emulator's own apps that put ads over the game (each
    emulator's store). Done on every connect: the emulator's updates and new
    instances bring them back, and Android saves the change a few seconds
    late, so one made just before the emulator closed is lost. Never stops
    the bot: an app it can't switch off is only logged."""
    try:
        off = set(device.shell("pm", "list", "packages", "-d").split())
    except DeviceError as exc:
        log.info("Couldn't list %s's switched-off apps: %s", emulator, exc, extra={"file_only": True})
        return
    for package in packages:
        if f"package:{package}" in off:
            continue
        try:
            out = device.shell("pm", "disable-user", "--user", "0", package)
        except DeviceError as exc:
            out = str(exc)
        if "disabled" in out:
            log.info("Switched off %s's %s, which puts ads over the game", emulator, package)
        else:
            log.info("Couldn't switch off %s's %s: %s", emulator, package, out.strip() or "no answer",
                     extra={"file_only": True})


def server_port_for(adb_path: str) -> int:
    """COCFARM's adb server for this adb: 15037 for the first, the next port
    for each other one (farms on two emulators run two adbs of their own
    versions, which mustn't share a server either)."""
    if not SERVER_PORT:  # the usual shared server
        return 0
    key = os.path.normcase(os.path.abspath(adb_path)) if os.path.dirname(adb_path) else adb_path.lower()
    with _ports_lock:
        if key not in _ports:
            _ports[key] = SERVER_PORT + len(_ports)
        return _ports[key]

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
    # The display's rotation (1: 90 degrees, 3: 270) when the touchscreen's
    # axes are the device's natural portrait ones and the screen shows
    # landscape (MuMu): positions are turned to match. 0: as the screen.
    rotation: int = 0


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
    x, y = min(max(x, 0), w - 1), min(max(y, 0), h - 1)
    if touch.rotation == 1:  # the screen's x runs down the touchscreen, its y right to left
        return round((h - 1 - y) * touch.max_x / max(h - 1, 1)), round(x * touch.max_y / max(w - 1, 1))
    if touch.rotation == 3:
        return round(y * touch.max_x / max(h - 1, 1)), round((w - 1 - x) * touch.max_y / max(w - 1, 1))
    return round(x * touch.max_x / max(w - 1, 1)), round(y * touch.max_y / max(h - 1, 1))


def _rotation(dumpsys: str) -> int:
    """The display's rotation (0-3) from ``dumpsys window displays``."""
    found = re.search(r"mCurrentRotation=(?:ROTATION_)?(\d+)", dumpsys)
    if not found:
        return 1
    value = int(found.group(1))
    return value // 90 if value >= 90 else value


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


def _gaps(pause: float | list[float], count: int) -> list[float]:
    """The pause after each of ``count`` taps (the last one's isn't waited)."""
    gaps = [float(pause)] * count if isinstance(pause, (int, float)) else [float(g) for g in pause][:count]
    gaps += [gaps[-1] if gaps else 0.0] * (count - len(gaps))
    if gaps:
        gaps[-1] = 0.0
    return gaps


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


def drag_events(points: list[tuple[int, int]], size: tuple[int, int], touch: TouchScreen) -> tuple[bytes, bytes]:
    """One finger through ``points``: a frame per point, each one fixed-size
    block (the first puts the finger down), and the release that lifts it."""
    event = _packer(touch)
    frames = bytearray()
    for x, y in points:
        tx, ty = _scaled(x, y, size, touch)
        if touch.slots:
            # The tracking id on every frame, so each block stands alone (as the pinch's).
            frames += event(EV_ABS, ABS_MT_SLOT, 0) + event(EV_ABS, ABS_MT_TRACKING_ID, 60010)
        frames += _position(event, touch, tx, ty)
        if not touch.slots:
            frames += event(EV_SYN, SYN_MT_REPORT, 0)
        if touch.btn_touch:
            frames += event(EV_KEY, BTN_TOUCH, 1)
        frames += event(EV_SYN, SYN_REPORT, 0)
    if touch.slots:
        release = event(EV_ABS, ABS_MT_SLOT, 0) + event(EV_ABS, ABS_MT_TRACKING_ID, -1)
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
DRAG_FILE = "/data/local/tmp/farmbot_drag"
RELEASE_FILE = "/data/local/tmp/farmbot_release"
PINCH_STEPS = 12
INPUT_TAPS_PER_SHELL = 40  # keeps the fallback command short enough for old adb


class AdbDevice:
    """A device reachable as ``serial`` (e.g. ``127.0.0.1:5555``)."""

    def __init__(self, serial: str, adb_path: str = "adb", runner: Runner = _run,
                 server_port: int | None = None):
        self.serial = serial
        self.adb_path = adb_path
        server_port = server_port_for(adb_path) if server_port is None else server_port  # 0: the usual shared one
        self._server = ["-P", str(server_port)] if server_port else []
        self._run = runner
        self._raw_screencap = True  # until the device sends something we can't decode
        self._touch: TouchScreen | None | bool = None  # None: not looked for yet; False: none usable
        self._dd = True  # dd works in the shell (Android 14's toybox has none: tail and head stand in)
        self._size: tuple[int, int] | None = None  # screen size, from the last screenshot
        # Shell commands and screenshots go straight to the adb server over
        # its socket: starting adb.exe for each costs ~0.5 s on Windows, more
        # than most commands take. Not with a runner standing in for adb (tests).
        self._port = server_port or 5037
        self._direct = runner is _run
        self._shell_v2: bool | None = None  # the device's adbd has shell v2 (exit codes); old Android hasn't

    def _adb(self, *args: str) -> bytes:
        command = [self.adb_path, *self._server, "-s", self.serial, *args]
        try:
            return self._call(command, args)
        except DeviceError as exc:
            # The connection dropped (the emulator restarting its network, or an
            # adb server started afresh still finding it): reconnect once and retry.
            if not any(s in str(exc) for s in ("not found", "offline", "closed")):
                raise
            log.info("Lost the connection to %s; reconnecting", self.serial)
            time.sleep(1 if ":" in self.serial else 3)  # an emulator-NNNN serial is found again by itself
            self.connect(afresh="offline" in str(exc))
            return self._call(command, args)

    def _call(self, command: list[str], args: tuple[str, ...]) -> bytes:
        if self._direct and args and args[0] in ("shell", "exec-out"):
            try:
                return self._via_server(args)
            except _NoServer:
                pass  # adb.exe starts the server; the next call finds it
        return self._run(command)

    def _via_server(self, args: tuple[str, ...]) -> bytes:
        """``adb shell``/``adb exec-out`` spoken straight to the adb server."""
        line = " ".join(args[1:])
        if args[0] == "exec-out":
            return _server_call(self._port, self.serial, "exec:" + line)
        if self._shell_v2 is None:
            self._shell_v2 = "shell_v2" in _server_features(self._port, self.serial)
            if not self._shell_v2:
                log.info("%s has no shell v2; using the plain shell", self.serial, extra={"file_only": True})
        if self._shell_v2:
            return _server_call(self._port, self.serial, "shell,v2,raw:" + line, v2=True)
        return _server_call(self._port, self.serial, "shell:" + line)

    def shell(self, *args: str) -> str:
        return self._adb("shell", *args).decode(errors="replace")

    def connect(self, afresh: bool = False) -> None:
        """``adb connect`` for TCP serials; a no-op for USB serials. With
        ``afresh``, the old connection is dropped first: one adb thinks is
        offline stays so ("already connected") until it's disconnected."""
        if ":" in self.serial:
            if afresh:
                with contextlib.suppress(DeviceError):
                    self._run([self.adb_path, *self._server, "disconnect", self.serial])
            out = self._run([self.adb_path, *self._server, "connect", self.serial]).decode(errors="replace")
            if "connected" not in out:
                raise DeviceError(f"could not connect to {self.serial}: {out.strip()}")
        if self.serial not in _announced:  # not again on every try while an emulator starts
            _announced.add(self.serial)
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

    def press(self, x: int, y: int, hold_ms: int) -> None:
        """A tap held down ``hold_ms`` (a swipe that doesn't move: every
        Android has it, and the game takes it as a tap)."""
        x, y = str(int(x)), str(int(y))
        self.shell("input", "swipe", x, y, x, y, str(int(hold_ms)))

    def drag(self, points: list[tuple[int, int]], duration_ms: int) -> bool:
        """One finger down on the first point, through the others, and up
        on the last, over about ``duration_ms`` (a swipe that needn't be
        straight). Written to the touchscreen; False where it can't be (the
        caller swipes plainly instead)."""
        touch = self._touchscreen()
        if not touch or len(points) < 2:
            return False
        if not self._size:
            self.screenshot()
        try:
            frames, release = drag_events(points, self._size, touch)
            block = len(frames) // len(points)
            self.push_bytes(frames, DRAG_FILE)
            self.push_bytes(release, RELEASE_FILE)
            # Writing a frame takes ~50 ms of its own: only a longer step needs a sleep.
            gap = duration_ms / 1000 / (len(points) - 1) - 0.05
            sleep = f"sleep {gap:.2f}; " if gap >= 0.02 else ""
            self.shell(f"i=0; while [ $i -lt {len(points)} ]; do "
                       f"{self._write_block(DRAG_FILE, touch.path, block)}; {sleep}i=$((i+1)); done; "
                       f"cat {RELEASE_FILE} > {touch.path}")
        except DeviceError as exc:
            log.info("Writing a swipe to the touchscreen failed (%s); swiping plainly", exc)
            return False
        return True

    def _touchscreen(self) -> TouchScreen | None:
        if self._touch is None:
            try:
                abi = self.shell("getprop", "ro.product.cpu.abi")
                found = parse_touchscreen(self.shell("getevent", "-p"), 24 if "64" in abi else 16)
                if found:
                    self._dd = "no such tool" not in self.shell("echo | dd of=/dev/null 2>&1; true").lower()
                    if not self._size:
                        self.screenshot()
                    w, h = self._size
                    if (found.max_x > found.max_y) != (w > h) and found.max_x != found.max_y:
                        found = replace(found, rotation=_rotation(self.shell("dumpsys", "window", "displays")))
            except DeviceError:
                found = None
            log.info("Touchscreen: %s%s", found, "" if self._dd else " (no dd: writing with tail and head)",
                     extra={"file_only": True})
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

    def tap_many(self, points: list[tuple[int, int]], pause: float | list[float] = 0.0) -> None:
        """Tap every point quickly, ``pause`` seconds apart at most (a list:
        the pause after each tap, one per point, so a burst needn't keep
        one even beat).

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
        gaps = _gaps(pause, len(points))
        for i in range(0, len(points), INPUT_TAPS_PER_SHELL):
            chunk = range(i, min(i + INPUT_TAPS_PER_SHELL, len(points)))
            self.shell("".join(f"input tap {points[j][0]} {points[j][1]}"
                               + (f"; sleep {gaps[j]:.2f}; " if gaps[j] >= 0.02 else "; ") for j in chunk).rstrip("; "))

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
            self.push_bytes(data, name)
        play = (f"i=0; while [ $i -le {PINCH_STEPS} ]; do {self._write_block(PINCH_FILE, touch.path, block)}; "
                f"sleep 0.02; i=$((i+1)); done; "
                f"cat {RELEASE_FILE} > {touch.path}; sleep 0.3")
        self.shell("; ".join([play] * times))
        return True

    def _write_taps(self, points: list[tuple[int, int]], touch: TouchScreen, pause: float | list[float]) -> None:
        data = touch_events(points, self._size, touch)
        block = tap_events(touch) * touch.event_size
        self.push_bytes(data, TAPS_FILE)
        if not isinstance(pause, (int, float)):
            # A pause of its own after each tap: the taps written one by one.
            # Starting dd takes ~50 ms already, so only longer pauses need a sleep.
            steps = []
            for i, gap in enumerate(_gaps(pause, len(points))):
                sleep = f"; sleep {gap - 0.05:.2f}" if gap - 0.05 >= 0.02 else ""
                steps.append(f"i={i}; {self._write_block(TAPS_FILE, touch.path, block)}{sleep}")
            self.shell("; ".join(steps))
            return
        # Starting dd takes ~50 ms already, so only longer pauses need a sleep.
        sleep = f"sleep {pause - 0.05:.2f}; " if pause - 0.05 >= 0.02 else ""
        self.shell(f"i=0; while [ $i -lt {len(points)} ]; do "
                   f"{self._write_block(TAPS_FILE, touch.path, block)}; "
                   f"{sleep}i=$((i+1)); done")

    def push_bytes(self, data: bytes, remote: str) -> None:
        """Put ``data`` in the file ``remote`` on the device: straight to the
        adb server when it's there (~30 ms), else with ``adb push``."""
        if self._direct:
            try:
                self._push_direct(data, remote)
                return
            except _NoServer:
                pass
        with tempfile.NamedTemporaryFile(prefix="farmbot_push", delete=False) as fh:
            fh.write(data)
        try:
            self._adb("push", fh.name, remote)
        finally:
            os.unlink(fh.name)

    def _push_direct(self, data: bytes, remote: str) -> None:
        try:
            _server_push(self._port, self.serial, data, remote)
        except DeviceError as exc:
            if not any(s in str(exc) for s in ("not found", "offline", "closed")):
                raise
            log.info("Lost the connection to %s; reconnecting", self.serial)
            time.sleep(1 if ":" in self.serial else 3)
            self.connect(afresh="offline" in str(exc))
            _server_push(self._port, self.serial, data, remote)

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

    def back(self) -> None:
        """Android's back button: closes the game's open window."""
        self.shell("input", "keyevent", "4")

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
