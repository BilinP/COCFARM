"""The game's village export: profile (the badge at the top left) →
Settings → More Settings → down to Data Export → "Copy". The game copies
the village (every building's level, as JSON) to the emulator's
clipboard, which the emulator passes on to Windows' own: read from there.
Whatever was on the PC's clipboard before is put back.

Taps nothing but those: the list scrolls with swipes over its left side,
where the settings' names are, never its buttons (Delete Account is at
its top), and "Copy" is only tapped in the Data Export row."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import time
from typing import Callable, Iterator, Protocol

from farmbot import townhall
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.tasks.village import close_all_windows

log = logging.getLogger(__name__)

PROFILE_AT = (68, 75)  # the player's level badge, top left
# A swipe up over the list's left half: the settings' names, no buttons.
LIST_SWIPE = (411, 684, 411, 274)
MAX_SWIPES = 10
COPY_WAIT = 8.0  # seconds for the copy to reach Windows' clipboard
FRESH = 300  # an export older than this (seconds) is an earlier one
LOCK_WAIT = 60.0  # another farm reading its export: wait for it

NEEDED = (T.SETTINGS_TAB, T.MORE_SETTINGS, T.DATA_EXPORT, T.EXPORT_COPY, T.CLOSE)


class Clipboard(Protocol):
    def available(self) -> bool: ...
    def text(self) -> str | None: ...
    def set_text(self, text: str) -> bool: ...


class WindowsClipboard:
    """The PC's clipboard (text only)."""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def available(self) -> bool:
        return sys.platform == "win32"

    def _api(self):
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        return ctypes, user32, kernel32

    @contextlib.contextmanager
    def _open(self, user32) -> Iterator[bool]:
        for _ in range(20):  # another program may have it open for a moment
            if user32.OpenClipboard(None):
                try:
                    yield True
                finally:
                    user32.CloseClipboard()
                return
            time.sleep(0.05)
        yield False

    def text(self) -> str | None:
        if not self.available():
            return None
        ctypes, user32, kernel32 = self._api()
        with self._open(user32) as opened:
            if not opened or not user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                return None
            handle = user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)

    def set_text(self, text: str) -> bool:
        if not self.available():
            return False
        ctypes, user32, kernel32 = self._api()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        with self._open(user32) as opened:
            if not opened:
                return False
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(self.GMEM_MOVEABLE, size)
            pointer = kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                return False
            ctypes.memmove(pointer, data, size)
            kernel32.GlobalUnlock(handle)
            return bool(user32.SetClipboardData(self.CF_UNICODETEXT, handle))


@contextlib.contextmanager
def _one_at_a_time(path: str | None = None, wait: float = LOCK_WAIT) -> Iterator[bool]:
    """Farms playing side by side share the PC's clipboard: one export at a
    time. Yields False if another farm's took too long."""
    path = path or os.path.join(tempfile.gettempdir(), "cocfarm-clipboard.lock")
    end = time.monotonic() + wait
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 2 * wait:  # left by a farm that crashed
                    os.unlink(path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= end:
                break
            time.sleep(0.5)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(path)


def copy_village(game: Game, clipboard: Clipboard | None = None, now: Callable[[], float] = time.time,
                 lock: str | None = None) -> str | None:
    """The village export (the game's JSON), or None if it couldn't be had.
    Leaves the village as it found it, with the windows closed."""
    if not all(game.has(n) for n in NEEDED):
        return None
    clipboard = clipboard or WindowsClipboard()
    if not clipboard.available():
        return None
    with _one_at_a_time(lock) as mine:
        if not mine:
            log.info("Village data: another farm is reading its own; next round")
            return None
        before = clipboard.text()
        text = None
        try:
            text = _copy(game, clipboard, now)
        finally:
            close_all_windows(game)
            if before is not None and clipboard.text() != before:
                clipboard.set_text(before)  # what was copied on the PC, back
        return text


def _copy(game: Game, clipboard: Clipboard, now: Callable[[], float]) -> str | None:
    tab = None
    for _ in range(2):  # (a popup may take the first tap)
        game.tap(*PROFILE_AT)
        tab = game.wait_for(T.SETTINGS_TAB, timeout=4)
        if tab is not None:
            break
    if tab is None:
        log.info("Village data: the profile didn't open")
        return None
    game.tap_match(tab)
    more = game.wait_for(T.MORE_SETTINGS, timeout=4)
    if more is None:
        log.info("Village data: no More Settings button in Settings")
        return None
    game.tap_match(more)
    game.wait(2)
    row = None
    for i in range(MAX_SWIPES + 1):
        game.refresh()
        row = game.find(T.DATA_EXPORT)
        if row is not None or i == MAX_SWIPES:
            break
        game.swipe(*LIST_SWIPE, 600)
        game.wait(0.8)
    if row is None:
        log.info("Village data: no Data Export in More Settings")
        return None
    game.wait(0.6)  # the list coming to rest
    game.refresh()
    row = game.find(T.DATA_EXPORT) or row
    width = game.size[0] - row.x
    copy = game.find(T.EXPORT_COPY, region=Rect(row.x, max(row.y - row.h, 0), width, 2 * row.h))
    if copy is None:
        log.info("Village data: no Copy button in the Data Export row")
        return None
    asked = now()
    game.tap_match(copy)
    deadline = game.clock() + COPY_WAIT
    while True:
        game.wait(0.5, exact=True)
        text = clipboard.text()
        village = townhall.parse(text)
        if village is not None and (not village.timestamp or village.timestamp >= asked - FRESH):
            return text
        if game.clock() >= deadline:
            log.info("Village data: the copy didn't reach the PC's clipboard (the emulator's clipboard "
                     "sharing may be off)")
            return None
