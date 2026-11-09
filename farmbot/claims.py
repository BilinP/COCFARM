"""What each COCFARM program on this PC plays: an emulator instance or a
Clash of Clans account is claimed by one program at a time.

Within one COCFARM, farms never share an instance or an account
(farmbot.instances). Two COCFARM programs open at once (a downloaded copy
and another, say) don't see each other's farms, so each claim is also a
small file in one folder for the whole PC, holding the claiming program's
process id and start time: a claim whose program has ended is taken over
(also when Windows has since given its id to another program).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from farmbot.config import DeviceConfig

log = logging.getLogger(__name__)

FOLDER = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "COCFARM" / "claims"


def _alive(pid: int) -> bool:
    """True if a program with this process id runs."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _started(pid: int) -> int | None:
    """When the program with this process id started (Windows' creation
    time), or None when it can't be told."""
    if sys.platform != "win32" or pid <= 0:
        return None
    import ctypes

    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, ended, kernel_time, user_time = (ctypes.c_ulonglong() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return created.value
    finally:
        kernel.CloseHandle(handle)


def _holder(held: dict, pid: int) -> bool:
    """True if the claim ``held`` is another program's that still runs."""
    holder = int(held.get("pid", 0) or 0)
    if not holder or holder == pid or not _alive(holder):
        return False
    started = held.get("started")
    return started is None or _started(holder) in (None, started)


def instance_name(cfg: DeviceConfig) -> str:
    """The claim's name for the emulator instance ``cfg`` plays."""
    from farmbot.instances import key_of

    which = cfg.ldplayer if cfg.mode == "ldplayer" else ""
    return f"instance {cfg.mode} {which} {key_of(cfg)}".lower()


def account_name(account: str) -> str:
    return f"account {account}".lower()


def _path(name: str, folder: Path) -> Path:
    return folder / (re.sub(r"[^\w.-]+", "_", name).strip("_") + ".json")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def claim(name: str, farm: int, folder: Path | None = None, pid: int | None = None) -> int | None:
    """Claim ``name`` for this program's farm ``farm``. Returns the farm of
    another running COCFARM program that holds it (and claims nothing), or
    None once it's this program's."""
    folder = folder or FOLDER
    pid = os.getpid() if pid is None else pid
    path = _path(name, folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        held = _read(path)
        if _holder(held, pid):
            return int(held.get("farm", 0) or 0)
        path.write_text(json.dumps({"pid": pid, "started": _started(pid), "farm": farm, "name": name}),
                        encoding="utf-8")
    except OSError as exc:  # (claims are a safety net: never stop a farm over one)
        log.info("Couldn't claim %s: %s", name, exc, extra={"file_only": True})
    return None


def release(name: str, folder: Path | None = None, pid: int | None = None) -> None:
    """Let go of ``name``, if this program holds it."""
    path = _path(name, folder or FOLDER)
    pid = os.getpid() if pid is None else pid
    if int(_read(path).get("pid", 0) or 0) == pid:
        try:
            path.unlink()
        except OSError:
            pass


def played_elsewhere(mode: str, folder: Path | None = None, pid: int | None = None) -> int | None:
    """The farm of another running COCFARM program playing an instance of
    emulator ``mode`` ("mumu", "bluestacks", "ldplayer"), or None."""
    folder = folder or FOLDER
    pid = os.getpid() if pid is None else pid
    try:
        paths = list(folder.glob(f"instance_{mode}_*.json"))
    except OSError:
        return None
    for path in paths:
        held = _read(path)
        if _holder(held, pid):
            return int(held.get("farm", 0) or 0)
    return None


def held_elsewhere(name: str, folder: Path | None = None, pid: int | None = None) -> bool:
    """True if another running COCFARM program holds ``name``."""
    pid = os.getpid() if pid is None else pid
    return _holder(_read(_path(name, folder or FOLDER)), pid)
