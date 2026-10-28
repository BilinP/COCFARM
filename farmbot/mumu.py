"""Finding, setting up and connecting to MuMu Player (MuMu 12 and later).

MuMu ships a command-line manager, ``MuMuManager.exe`` (in ``nx_main``, or
``shell`` on older versions), with JSON output. With it we list the
instances, read and change an instance's settings (screen, CPU cores and
memory), and launch or shut one down. A running instance reports its ADB
address; ADB is always on in MuMu.

    MuMuManager info -v all            ->  {"0": {"name": ..., "is_android_started": true, "adb_port": 16384, ...}}
    MuMuManager setting -v 0 -a        ->  {"resolution_width.custom": "860.000000", ...}
    MuMuManager control -v 0 launch    (shutdown, restart)
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from farmbot.config import DeviceConfig
from farmbot.device import NO_WINDOW, AdbDevice, DeviceError

log = logging.getLogger(__name__)

MANAGER_EXE = "MuMuManager.exe"
ADB_EXE = "adb.exe"
TOOL_DIRS = ("nx_main", "shell")  # where the manager and adb live (MuMu 12's newer and older layouts)
DEFAULT_ROOTS = (Path("C:/Program Files/Netease"), Path("D:/Program Files/Netease"), Path("C:/Netease"),
                 Path("D:/Netease"))
ADB_BASE_PORT, ADB_PORT_STEP = 16384, 32  # instance N: 127.0.0.1:16384 + 32 N (when it doesn't say)

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, check=False, creationflags=NO_WINDOW)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise DeviceError(f"MuMuManager failed: {exc}") from exc
    return result.stdout.decode("utf-8", errors="replace")


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DeviceError(f"MuMuManager said something unexpected: {text.strip()[:200]}") from exc
    return data if isinstance(data, dict) else {}


@dataclass
class Instance:
    index: int
    name: str
    running: bool  # Android has started
    started: bool  # its window/process is up (Android may still be starting)
    adb_port: int | None = None

    @property
    def serial(self) -> str:
        return f"127.0.0.1:{self.adb_port or ADB_BASE_PORT + ADB_PORT_STEP * self.index}"


@dataclass
class Install:
    tools: Path  # the folder with MuMuManager.exe and adb.exe

    @property
    def name(self) -> str:
        return "MuMu Player"

    @property
    def manager(self) -> Path:
        return self.tools / MANAGER_EXE

    @property
    def adb(self) -> Path:
        return self.tools / ADB_EXE


def _tools(folder: Path) -> Path | None:
    for sub in TOOL_DIRS:
        if (folder / sub / MANAGER_EXE).is_file():
            return folder / sub
    return folder if (folder / MANAGER_EXE).is_file() else None


def _registry_dirs() -> list[Path]:
    """Install folders from the Windows "Apps" list."""
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            root = winreg.OpenKey(hive, key)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        icon = str(winreg.QueryValueEx(sub, "DisplayIcon")[0])
                except OSError:
                    continue
                if "mumu" in name.lower():
                    found.append(Path(icon.strip('"').split(",")[0]).parent)
    return found


def find_install(extra: list[Path] | None = None) -> Install | None:
    """MuMu Player, if installed."""
    dirs = list(extra or []) + _registry_dirs()
    for root in DEFAULT_ROOTS:
        if root.is_dir():
            dirs += [d for d in root.iterdir() if d.is_dir() and "mumu" in d.name.lower()]
    for d in dirs:
        for folder in (d, d.parent):  # the icon can be in the tools folder itself
            tools = _tools(folder)
            if tools is not None:
                return Install(tools)
    return None


def parse_info(text: str) -> list[Instance]:
    """``info -v all``: one instance's fields, or {index: fields} for several."""
    data = _json(text)
    entries = [data] if "index" in data else [v for v in data.values() if isinstance(v, dict)]
    found = []
    for e in entries:
        try:
            port = e.get("adb_port")
            found.append(Instance(int(e["index"]), str(e.get("name", "")), bool(e.get("is_android_started")),
                                  bool(e.get("is_process_started")), int(port) if port else None))
        except (KeyError, ValueError):
            continue
    return sorted(found, key=lambda i: i.index)


def instances(install: Install, run: Runner = _run) -> list[Instance]:
    return parse_info(run([str(install.manager), "info", "-v", "all"]))


def read_settings(install: Install, index: int, run: Runner = _run) -> dict:
    """The screen, CPU cores and memory the instance is set to."""
    data = _json(run([str(install.manager), "setting", "-v", str(index), "-a"]))

    def number(key: str) -> float | None:
        try:
            return float(data[key])
        except (KeyError, ValueError):
            return None

    custom = str(data.get("resolution_mode", "")) == "custom"
    return {"width": number("resolution_width.custom") if custom else None,
            "height": number("resolution_height.custom") if custom else None,
            "dpi": number("resolution_dpi.custom") if custom else None,
            "cpus": number("performance_cpu.custom") if data.get("performance_mode") == "custom" else None,
            "memory": number("performance_mem.custom") if data.get("performance_mode") == "custom" else None}


def _gb(mb: int) -> float:
    return round(mb / 1024, 2)


def setup_needed(settings: dict, cfg: DeviceConfig) -> list[str]:
    """What has to change on the instance, in words (empty: nothing)."""
    changes = []
    if cfg.set_display and (settings.get("width"), settings.get("height"), settings.get("dpi")) != (
            cfg.display_width, cfg.display_height, cfg.display_dpi):
        changes.append(f"display -> {cfg.display_width}x{cfg.display_height} {cfg.display_dpi} DPI")
    if cfg.set_resources and (settings.get("cpus") != cfg.cpus or settings.get("memory") != _gb(cfg.memory)):
        changes.append(f"CPU {cfg.cpus}, memory {_gb(cfg.memory):g} GB")
    return changes


def set_up(install: Install, inst: Instance, cfg: DeviceConfig, run: Runner = _run,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Shut the instance down if needed, then apply the display, CPU and
    memory settings. It is started again by :func:`open_device`."""
    manager, index = str(install.manager), str(inst.index)
    if inst.started:
        log.info("Closing %s to change its settings", install.name)
        run([manager, "control", "-v", index, "shutdown"])
        for _ in range(30):
            sleep(1)
            current = next((i for i in instances(install, run) if i.index == inst.index), None)
            if current is None or not current.started:
                break
    args = [manager, "setting", "-v", index]
    if cfg.set_display:
        args += ["-k", "resolution_mode", "-val", "custom",
                 "-k", "resolution_width.custom", "-val", str(cfg.display_width),
                 "-k", "resolution_height.custom", "-val", str(cfg.display_height),
                 "-k", "resolution_dpi.custom", "-val", str(cfg.display_dpi)]
    if cfg.set_resources:
        args += ["-k", "performance_mode", "-val", "custom",
                 "-k", "performance_cpu.custom", "-val", str(cfg.cpus),
                 "-k", "performance_mem.custom", "-val", f"{_gb(cfg.memory):g}"]
    if len(args) > 4:
        run(args)


def open_device(cfg: DeviceConfig, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic,
                find: Callable[[], Install | None] = find_install,
                make: Callable[[str, str], AdbDevice] = AdbDevice) -> AdbDevice:
    """Find MuMu, set the instance up the way the bot needs it (once), start
    it if allowed, and connect over MuMu's own ADB."""
    install = find()
    if install is None:
        raise DeviceError("MuMu Player was not found. Install MuMu Player, or pick another emulator in Setup.")
    found = instances(install, run)
    inst = next((i for i in found if i.index == cfg.mumu_index), None)
    if inst is None:
        raise DeviceError(f"{install.name} has no instance {cfg.mumu_index} "
                          f"(have: {', '.join(str(i.index) for i in found) or 'none'}).")
    changes = setup_needed(read_settings(install, inst.index, run), cfg)
    if changes:
        log.warning("Setting up %s: %s (restarts it)", install.name, "; ".join(changes))
        set_up(install, inst, cfg, run, sleep)
        inst.running = inst.started = False
    if not inst.started:
        if not cfg.launch_emulator:
            raise DeviceError(f"{install.name} is not running. Start it, or turn on 'Start the emulator automatically'.")
        log.info("Starting %s (instance %d)", install.name, inst.index)
        run([str(install.manager), "control", "-v", str(inst.index), "launch"])
    deadline = clock() + cfg.launch_timeout
    last_error: Exception | None = None
    while True:
        try:
            current = next((i for i in instances(install, run) if i.index == inst.index), inst)
            if current.running:
                device = make(current.serial, str(install.adb))
                device.connect()
                if device.shell("getprop", "sys.boot_completed").strip() == "1" and device.foreground_package():
                    log.info("Connected to %s at %s", install.name, current.serial)
                    return device
        except DeviceError as exc:
            last_error = exc
        if clock() >= deadline:
            raise DeviceError(f"{install.name} did not answer over ADB within {cfg.launch_timeout:.0f}s"
                              + (f": {last_error}" if last_error else ""))
        sleep(3)


def quit_instance(cfg: DeviceConfig, run: Runner = _run) -> None:
    """Close the configured MuMu instance (it is started again by ``open_device``)."""
    install = find_install()
    if install is None:
        raise DeviceError("MuMu Player was not found")
    log.warning("Closing %s (instance %d) to start it again", install.name, cfg.mumu_index)
    run([str(install.manager), "control", "-v", str(cfg.mumu_index), "shutdown"])
