"""Finding, setting up and connecting to LDPlayer (versions 9 and later).

LDPlayer ships a command-line manager, ``ldconsole.exe``, next to the
player. With it we can list the instances, change an instance's screen
(``modify --resolution w,h,dpi``), CPU cores and memory, and launch it.
ADB is the "Local ADB connection" setting, ``basicSettings.adbDebug`` in the
instance's ``vms/config/leidian<N>.config``, which we switch on while the
instance is stopped. Instance N listens for ADB as ``emulator-<5554 + 2N>``
on LDPlayer's own ``adb.exe``.

    ldconsole list2  ->  index,title,top_hwnd,bind_hwnd,started,pid,vbox_pid,width,height,dpi
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from farmbot.config import DeviceConfig
from farmbot.device import NO_WINDOW, AdbDevice, DeviceError

log = logging.getLogger(__name__)

CONSOLE_EXE = "ldconsole.exe"
ADB_EXE = "adb.exe"
DEFAULT_ROOTS = (Path("C:/LDPlayer"), Path("D:/LDPlayer"), Path("C:/Program Files/LDPlayer"))
MEMORY_SIZES = (2048, 4096, 8192)  # MB: what ldconsole modify --memory accepts, from 2048 up

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False, creationflags=NO_WINDOW)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise DeviceError(f"ldconsole failed: {exc}") from exc
    # ldconsole prints in the system code page (Chinese builds use GBK).
    return result.stdout.decode("utf-8", errors="replace") or result.stdout.decode("mbcs", errors="replace")


@dataclass
class Instance:
    index: int
    title: str
    running: bool
    width: int
    height: int
    dpi: int

    @property
    def serial(self) -> str:
        return f"emulator-{5554 + 2 * self.index}"


@dataclass
class Install:
    path: Path  # the folder with dnplayer.exe and ldconsole.exe, e.g. C:/LDPlayer/LDPlayer9
    version: int  # 9, 14, ... (0 if the folder name doesn't say)

    @property
    def name(self) -> str:
        return f"LDPlayer {self.version}" if self.version else self.path.name

    @property
    def console(self) -> Path:
        return self.path / CONSOLE_EXE

    @property
    def adb(self) -> Path:
        return self.path / ADB_EXE

    def config_file(self, index: int) -> Path:
        return self.path / "vms" / "config" / f"leidian{index}.config"


def _version(path: Path) -> int:
    match = re.search(r"(\d+)\s*$", path.name)
    return int(match.group(1)) if match else 0


def _registry_dirs() -> list[Path]:
    """Install folders from the Windows "Apps" list (dnplayer.exe's folder)."""
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
                if name.lower().startswith("ldplayer") and "dnplayer" in icon.lower():
                    found.append(Path(icon.strip('"').split(",")[0]).parent)
    return found


def find_installs(extra: list[Path] | None = None) -> list[Install]:
    """Every LDPlayer found, newest version first."""
    dirs = list(extra or []) + _registry_dirs()
    for root in DEFAULT_ROOTS:
        if root.is_dir():
            dirs += [d for d in root.iterdir() if d.is_dir()]
    installs: dict[str, Install] = {}
    for d in dirs:
        if (d / CONSOLE_EXE).is_file():
            installs.setdefault(str(d.resolve()).lower(), Install(d, _version(d)))
    return sorted(installs.values(), key=lambda i: -i.version)


def pick_install(cfg: DeviceConfig, installs: list[Install]) -> Install | None:
    """The configured install (a folder, or a name like "LDPlayer 9"), or the newest."""
    wanted = cfg.ldplayer.strip()
    if not wanted:
        return installs[0] if installs else None
    for inst in installs:
        if wanted.lower() in (inst.name.lower(), str(inst.path).lower()):
            return inst
    path = Path(wanted)
    return Install(path, _version(path)) if (path / CONSOLE_EXE).is_file() else None


def parse_list2(text: str) -> list[Instance]:
    instances = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 10 or not parts[0].isdigit():
            continue
        try:
            instances.append(Instance(int(parts[0]), parts[1], parts[4] == "1",
                                      int(parts[7]), int(parts[8]), int(parts[9])))
        except ValueError:
            continue
    return instances


def instances(install: Install, run: Runner = _run) -> list[Instance]:
    return parse_list2(run([str(install.console), "list2"]))


def read_settings(install: Install, index: int) -> dict:
    """CPU cores, memory and the ADB switch from the instance's config file."""
    try:
        data = json.loads(install.config_file(index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {"cpus": data.get("advancedSettings.cpuCount"), "memory": data.get("advancedSettings.memorySize"),
            "adb": bool(data.get("basicSettings.adbDebug"))}


def enable_adb(install: Install, index: int) -> None:
    """Switch on "Local ADB connection" (only while the instance is stopped:
    LDPlayer rewrites the file when it closes)."""
    path = install.config_file(index)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["basicSettings.adbDebug"] = 1
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def _memory(mb: int) -> int:
    """The nearest memory size the settings accept, at least ``mb``."""
    return next((m for m in MEMORY_SIZES if m >= mb), MEMORY_SIZES[-1])


def setup_needed(inst: Instance, settings: dict, cfg: DeviceConfig) -> list[str]:
    """What has to change on the instance, in words (empty: nothing)."""
    changes = []
    if cfg.set_display and (inst.width, inst.height, inst.dpi) != (cfg.display_width, cfg.display_height, cfg.display_dpi):
        changes.append(f"display {inst.width}x{inst.height} {inst.dpi} DPI -> "
                       f"{cfg.display_width}x{cfg.display_height} {cfg.display_dpi} DPI")
    if cfg.set_resources and settings:
        if settings.get("cpus") != cfg.cpus:
            changes.append(f"CPU {settings.get('cpus')} -> {cfg.cpus}")
        if settings.get("memory") != _memory(cfg.memory):
            changes.append(f"memory {settings.get('memory')} MB -> {_memory(cfg.memory)} MB")
    if cfg.enable_adb and settings and not settings.get("adb"):
        changes.append("ADB on")
    return changes


def set_up(install: Install, inst: Instance, cfg: DeviceConfig, run: Runner = _run,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Stop the instance if needed, then apply the display, CPU, memory and
    ADB settings. It is started again by :func:`open_device`."""
    console, index = str(install.console), str(inst.index)
    if inst.running:
        log.info("Closing %s to change its settings", install.name)
        run([console, "quit", "--index", index])
        for _ in range(30):
            sleep(1)
            current = next((i for i in instances(install, run) if i.index == inst.index), None)
            if current is None or not current.running:
                break
    args = [console, "modify", "--index", index]
    if cfg.set_display:
        args += ["--resolution", f"{cfg.display_width},{cfg.display_height},{cfg.display_dpi}"]
    if cfg.set_resources:
        args += ["--cpu", str(min(max(cfg.cpus, 2), 4)), "--memory", str(_memory(cfg.memory))]
    if len(args) > 4:
        run(args)
    if cfg.enable_adb and not read_settings(install, inst.index).get("adb", True):
        enable_adb(install, inst.index)


class WrongEmulator(DeviceError):
    """The address answered, but from another emulator."""


def is_bluestacks(props: str) -> bool:
    """True if ``getprop`` output is BlueStacks' (its own services and
    settings are named bst…)."""
    return bool(re.search(r"^\[(init\.svc\.bst|ro\.bst|bst\.|persist\.bst)", props, re.MULTILINE))


def open_device(cfg: DeviceConfig, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic,
                find: Callable[[], list[Install]] = find_installs,
                make: Callable[[str, str], AdbDevice] = AdbDevice) -> AdbDevice:
    """Find LDPlayer, set the instance up the way the bot needs it (once),
    start it if allowed, and connect over LDPlayer's own ADB."""
    install = pick_install(cfg, find())
    if install is None:
        raise DeviceError("LDPlayer was not found. Install LDPlayer 9 or newer, or pick its folder in Settings.")
    found = instances(install, run)
    inst = next((i for i in found if i.index == cfg.ldplayer_index), None)
    if inst is None:
        raise DeviceError(f"{install.name} has no instance {cfg.ldplayer_index} "
                          f"(have: {', '.join(str(i.index) for i in found) or 'none'}).")
    changes = setup_needed(inst, read_settings(install, inst.index), cfg)
    if changes:
        log.warning("Setting up %s: %s (restarts it)", install.name, "; ".join(changes))
        set_up(install, inst, cfg, run, sleep)
        inst.running = False
    if not inst.running:
        if not cfg.launch_emulator:
            raise DeviceError(f"{install.name} is not running. Start it, or turn on 'Start the emulator automatically'.")
        log.info("Starting %s (instance %d)", install.name, inst.index)
        run([str(install.console), "launch", "--index", str(inst.index)])
    deadline = clock() + cfg.launch_timeout
    last_error: Exception | None = None
    while True:
        try:
            device = make(inst.serial, str(install.adb))
            device.connect()
            # "Boot completed" comes while the screen still says "Tablet is
            # starting…"; the home screen (or an app) having focus means ready.
            if device.shell("getprop", "sys.boot_completed").strip() == "1" and device.foreground_package():
                if is_bluestacks(device.shell("getprop")):
                    # BlueStacks listens on 127.0.0.1:5555, the same port as
                    # LDPlayer's first instance, and wins: playing on would
                    # quietly farm in the wrong emulator.
                    raise WrongEmulator(f"{inst.serial} reached BlueStacks instead of {install.name}: both use "
                                        f"port {5555 + 2 * inst.index}. Close BlueStacks, or pick BlueStacks in Setup.")
                log.info("Connected to %s at %s", install.name, inst.serial)
                return device
        except WrongEmulator:
            raise
        except DeviceError as exc:
            last_error = exc
        if clock() >= deadline:
            raise DeviceError(f"{install.name} did not answer over ADB within {cfg.launch_timeout:.0f}s"
                              + (f": {last_error}" if last_error else ""))
        sleep(3)


def quit_instance(cfg: DeviceConfig, run: Runner = _run) -> None:
    """Close the configured LDPlayer instance (it is started again by
    ``open_device``)."""
    install = pick_install(cfg, find_installs())
    if install is None:
        raise DeviceError("LDPlayer was not found")
    log.warning("Closing %s (instance %d) to start it again", install.name, cfg.ldplayer_index)
    run([str(install.console), "quit", "--index", str(cfg.ldplayer_index)])
