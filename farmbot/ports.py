"""Which program is listening on a port (Windows).

An emulator picks its ADB port itself: LDPlayer's instance N always uses
5555 + 2N, and BlueStacks takes 5555 when it's free. With two emulators
open, the one that started first holds the port and the other has no
connection, so before connecting the bot looks at who holds its port.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from farmbot.device import NO_WINDOW


def listening_pid(port: int, netstat: str | None = None) -> int | None:
    """The process listening on TCP ``port``, from ``netstat -ano``."""
    if netstat is None:
        if sys.platform != "win32":
            return None
        try:
            netstat = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                     timeout=15, check=False, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    for line in netstat.splitlines():
        parts = line.split()
        # TCP  0.0.0.0:5555  0.0.0.0:0  LISTENING  38652
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and re.search(rf":{port}$", parts[1]):
            return int(parts[4]) if parts[4].isdigit() else None
    return None


def process_path(pid: int) -> Path | None:
    """The program file of process ``pid``."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def owner(port: int) -> Path | None:
    """The program listening on ``port``, or ``None`` if nothing is (or it
    can't be told)."""
    pid = listening_pid(port)
    return process_path(pid) if pid else None
