"""BlueStacks without ADB: read and click its window from Windows.

With ADB turned off in BlueStacks, the bot sees the game by capturing the
BlueStacks game view (``PrintWindow``, which works even while the window
is covered). BlueStacks ignores mouse messages sent to its window, so taps
are real mouse clicks: the bot brings BlueStacks to the front, clicks,
and puts the cursor back. While it plays, keep the BlueStacks window on
screen and leave the mouse alone. The app is started with BlueStacks' own
``HD-Player.exe --cmd launchApp``.

Android has no way to be told to close an app this way, so leaving a
battle needs the Return Home and Okay pictures (see ``farmbot pictures``).
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
from ctypes import wintypes as W

import numpy as np

from farmbot.device import NO_WINDOW, DeviceError

log = logging.getLogger(__name__)

GAME_VIEW_CLASS = "BlueStacksApp"  # the child window BlueStacks draws Android into
PLAYER_TITLE = "BlueStacks App Player"

PW_RENDERFULLCONTENT = 0x2
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_ESCAPE = 0x1B


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


_api = None


def _win32():
    """user32/gdi32 with argument types declared (handles are 64-bit)."""
    global _api
    if _api is None:
        if sys.platform != "win32":
            raise DeviceError("connecting without ADB works on Windows only")
        user32, gdi32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32")
        try:  # real pixels, not ones scaled for the Windows display zoom
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
        enum_proc = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        for name, args, res in [
            ("EnumWindows", [enum_proc, W.LPARAM], W.BOOL),
            ("EnumChildWindows", [W.HWND, enum_proc, W.LPARAM], W.BOOL),
            ("GetWindowTextW", [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetClassNameW", [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetClientRect", [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
            ("IsWindow", [W.HWND], W.BOOL),
            ("IsWindowVisible", [W.HWND], W.BOOL),
            ("IsIconic", [W.HWND], W.BOOL),
            ("GetAncestor", [W.HWND, W.UINT], W.HWND),
            ("ShowWindow", [W.HWND, ctypes.c_int], W.BOOL),
            ("GetForegroundWindow", [], W.HWND),
            ("SetForegroundWindow", [W.HWND], W.BOOL),
            ("ClientToScreen", [W.HWND, ctypes.POINTER(W.POINT)], W.BOOL),
            ("GetCursorPos", [ctypes.POINTER(W.POINT)], W.BOOL),
            ("SetCursorPos", [ctypes.c_int, ctypes.c_int], W.BOOL),
            ("mouse_event", [W.DWORD, W.DWORD, W.DWORD, W.DWORD, ctypes.c_void_p], None),
            ("keybd_event", [W.BYTE, W.BYTE, W.DWORD, ctypes.c_void_p], None),
            ("MapVirtualKeyW", [W.UINT, W.UINT], W.UINT),
            ("GetDC", [W.HWND], W.HDC),
            ("ReleaseDC", [W.HWND, W.HDC], ctypes.c_int),
            ("PrintWindow", [W.HWND, W.HDC, W.UINT], W.BOOL),
        ]:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, res
        for name, args, res in [
            ("CreateCompatibleDC", [W.HDC], W.HDC),
            ("CreateCompatibleBitmap", [W.HDC, ctypes.c_int, ctypes.c_int], W.HBITMAP),
            ("SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ),
            ("DeleteObject", [W.HGDIOBJ], W.BOOL),
            ("DeleteDC", [W.HDC], W.BOOL),
            ("GetDIBits", [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p,
                           ctypes.POINTER(_BitmapInfoHeader), W.UINT], ctypes.c_int),
        ]:
            fn = getattr(gdi32, name)
            fn.argtypes, fn.restype = args, res
        _api = (user32, gdi32, enum_proc)
    return _api


def _text(hwnd) -> str:
    user32, _, _ = _win32()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _class(hwnd) -> str:
    user32, _, _ = _win32()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_game_view(title: str = PLAYER_TITLE) -> int | None:
    """The window BlueStacks draws the game into, inside the player window
    called ``title`` (an instance's window is named after the instance)."""
    user32, _, enum_proc = _win32()
    players: list[int] = []
    user32.EnumWindows(enum_proc(lambda h, _: players.append(h) or True), 0)
    for player in players:
        if _text(player) != title or not user32.IsWindowVisible(player):
            continue
        children: list[int] = []
        user32.EnumChildWindows(player, enum_proc(lambda h, _: children.append(h) or True), 0)
        for child in children:
            if _class(child) == GAME_VIEW_CLASS:
                return child
    return None


class WindowDevice:
    """A BlueStacks instance driven through its window instead of ADB.

    Coordinates are in the window's pixels; the bot scales screenshots
    itself, so it never needs to know the Android resolution.
    """

    def __init__(self, title: str = PLAYER_TITLE, player_exe: str = "", instance: str = "",
                 package: str = "com.supercell.clashofclans"):
        self.title = title
        self.player_exe = player_exe
        self.instance = instance
        self.package = package
        self.hwnd: int | None = None

    def connect(self) -> None:
        self.hwnd = find_game_view(self.title)
        if self.hwnd is None:
            raise DeviceError(f"no BlueStacks window called {self.title!r} is open")
        log.info("Using the BlueStacks window %r (no ADB)", self.title)

    def _view(self) -> int:
        user32, _, _ = _win32()
        if self.hwnd is None or not user32.IsWindow(self.hwnd):
            self.connect()
        return self.hwnd

    def size(self) -> tuple[int, int]:
        user32, _, _ = _win32()
        rect = W.RECT()
        user32.GetClientRect(self._view(), ctypes.byref(rect))
        return rect.right, rect.bottom

    def screenshot(self) -> np.ndarray:
        user32, gdi32, _ = _win32()
        hwnd = self._view()
        w, h = self.size()
        if w <= 0 or h <= 0:
            raise DeviceError("the BlueStacks window has no size; is it minimised?")
        window_dc = user32.GetDC(hwnd)
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, w, h)
        old = gdi32.SelectObject(memory_dc, bitmap)
        try:
            if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
                raise DeviceError("could not capture the BlueStacks window")
            header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            pixels = np.empty((h, w, 4), np.uint8)
            if not gdi32.GetDIBits(memory_dc, bitmap, 0, h, pixels.ctypes.data, ctypes.byref(header), 0):
                raise DeviceError("could not read the BlueStacks window's pixels")
        finally:
            gdi32.SelectObject(memory_dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
        image = np.ascontiguousarray(pixels[:, :, :3])
        if not image.any():
            raise DeviceError("the BlueStacks window captured as black; is it minimised?")
        return image

    def _front(self) -> None:
        """Bring BlueStacks to the front so real input lands on it."""
        user32, _, _ = _win32()
        player = user32.GetAncestor(self._view(), 2)  # GA_ROOT
        if user32.IsIconic(player):
            user32.ShowWindow(player, 9)  # SW_RESTORE
        if user32.GetForegroundWindow() != player:
            user32.SetForegroundWindow(player)
            time.sleep(0.15)

    def _screen_point(self, x: int, y: int) -> tuple[int, int]:
        user32, _, _ = _win32()
        point = W.POINT(int(x), int(y))
        user32.ClientToScreen(self._view(), ctypes.byref(point))
        return point.x, point.y

    def _with_mouse(self, action) -> None:
        """Run ``action`` with BlueStacks in front, then put the cursor back."""
        user32, _, _ = _win32()
        self._front()
        old = W.POINT()
        user32.GetCursorPos(ctypes.byref(old))
        try:
            action(user32)
        finally:
            user32.SetCursorPos(old.x, old.y)

    def tap(self, x: int, y: int) -> None:
        sx, sy = self._screen_point(x, y)

        def click(user32) -> None:
            user32.SetCursorPos(sx, sy)
            time.sleep(0.02)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
            time.sleep(0.04)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)

        self._with_mouse(click)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        (sx1, sy1), (sx2, sy2) = self._screen_point(x1, y1), self._screen_point(x2, y2)
        steps = max(5, duration_ms // 20)

        def drag(user32) -> None:
            user32.SetCursorPos(sx1, sy1)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
            for i in range(1, steps + 1):
                time.sleep(duration_ms / 1000 / steps)
                user32.SetCursorPos(sx1 + (sx2 - sx1) * i // steps, sy1 + (sy2 - sy1) * i // steps)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)

        self._with_mouse(drag)

    def _key(self, vk: int = 0, char: str = "") -> None:
        user32, _, _ = _win32()
        if char:  # unicode characters go through keybd_event's scan-code slot
            user32.keybd_event(0, ord(char) & 0xFF, KEYEVENTF_UNICODE, None)
            user32.keybd_event(0, ord(char) & 0xFF, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, None)
        else:  # BlueStacks reads the hardware scan code, not just the key code
            scan = user32.MapVirtualKeyW(vk, 0)
            user32.keybd_event(vk, scan, 0, None)
            time.sleep(0.05)
            user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, None)

    def type_text(self, text: str) -> None:
        self._front()
        for char in text:
            self._key(char=char)
            time.sleep(0.03)

    def back(self) -> None:
        """Android's back button (BlueStacks maps Esc to it)."""
        self._front()
        self._key(VK_ESCAPE)

    def start_app(self, package: str) -> None:
        if not self.player_exe:
            raise DeviceError(f"can't start {package} without ADB: open it in BlueStacks yourself")
        args = [self.player_exe, "--cmd", "launchApp", "--package", package]
        if self.instance:
            args[1:1] = ["--instance", self.instance]
        subprocess.Popen(args, creationflags=NO_WINDOW)

    def stop_app(self, package: str) -> None:
        # Without ADB there is no "force stop". Restarting the app is only a
        # way of recovering, so going back to it is the closest thing.
        log.info("Can't close the game without ADB; pressing Back instead")
        self.back()

    def foreground_package(self) -> str:
        # Without ADB the running app can't be asked for; assume the game is open.
        return self.package
