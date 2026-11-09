"""Finding, setting up and connecting to LDPlayer (versions 9 and later).

LDPlayer ships a command-line manager, ``ldconsole.exe``, next to the
player. With it we can list the instances, change an instance's screen
(``modify --resolution w,h,dpi``), CPU cores and memory, and launch it.
ADB is the "Local ADB connection" setting, ``basicSettings.adbDebug`` in the
instance's ``vms/config/leidian<N>.config``, which we switch on while the
instance is stopped. Instance N listens for ADB on ``127.0.0.1:<5555 + 2N>``,
reached with LDPlayer's own ``adb.exe``.

    ldconsole list2  ->  index,title,top_hwnd,bind_hwnd,started,pid,vbox_pid,width,height,dpi
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from farmbot import ports
from farmbot import standard
from farmbot.config import DeviceConfig
from farmbot.device import NO_WINDOW, AdbDevice, DeviceError, NoAnswer, switch_off_ad_apps

log = logging.getLogger(__name__)

AD_PACKAGES = ("com.android.ld.appstore",)  # LDPlayer's store, whose "Top-Up via LDPlayer" ad opens over the game
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
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")


@dataclass
class Instance:
    index: int
    title: str
    running: bool
    width: int
    height: int
    dpi: int
    pid: int = -1  # the player's process (-1: none)
    vm_pid: int = -1  # its virtual machine's

    @property
    def serial(self) -> str:
        # By address, not as emulator-5554: LDPlayer starts an adb server of
        # its own on 5037, which takes the emulator-NNNN device for itself,
        # and COCFARM's server then can't see it ("device 'emulator-5554' not
        # found"). A connection by address works beside it.
        return f"127.0.0.1:{self.port}"

    @property
    def port(self) -> int:
        """The ADB port, fixed by LDPlayer: whichever emulator takes it first keeps it."""
        return 5555 + 2 * self.index


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
                                      int(parts[7]), int(parts[8]), int(parts[9]), int(parts[5]), int(parts[6])))
        except ValueError:
            continue
    return instances


def instances(install: Install, run: Runner = _run) -> list[Instance]:
    return parse_list2(run([str(install.console), "list2"]))


def read_settings(install: Install, index: int) -> dict:
    """The instance's config file (empty if it can't be read)."""
    try:
        return json.loads(install.config_file(index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _memory(mb: int) -> int:
    """The nearest memory size the settings accept, at least ``mb``."""
    return next((m for m in MEMORY_SIZES if m >= mb), MEMORY_SIZES[-1])


# farmbot.standard as ``ldconsole modify`` flags, with the config-file key
# each one is read back from.
MODIFY = {"--cpu": ("advancedSettings.cpuCount", standard.CPUS),
          "--memory": ("advancedSettings.memorySize", _memory(standard.MEMORY)),
          "--manufacturer": ("propertySettings.phoneManufacturer", standard.MANUFACTURER),
          "--model": ("propertySettings.phoneModel", standard.MODEL)}
# What ldconsole can't set: written into the config file while the instance
# is stopped (LDPlayer rewrites the file when it closes). Frame rate: 60, no
# vertical sync, no "high frame rate" mode.
CONFIG = {"basicSettings.fps": standard.FPS, "basicSettings.verticalSync": False,
          "basicSettings.heightFrameRate": False}
ADB_KEY = "basicSettings.adbDebug"


def _same(have, want) -> bool:
    if isinstance(want, str):
        return str(have or "").strip().lower() == want.lower()
    if isinstance(want, bool):
        return bool(have) == want
    return have == want


def setup_needed(inst: Instance, settings: dict, cfg: DeviceConfig) -> dict[str, object]:
    """What has to change for farmbot.standard, as ``modify`` flags and
    config-file keys -> wanted value (empty: nothing). A config file that
    can't be read leaves only the display to check."""
    changes: dict[str, object] = {}
    if (inst.width, inst.height, inst.dpi) != (standard.WIDTH, standard.HEIGHT, standard.DPI):
        changes["--resolution"] = f"{standard.WIDTH},{standard.HEIGHT},{standard.DPI}"
    if not settings:
        return changes
    for flag, (key, want) in MODIFY.items():
        if not _same(settings.get(key), want):
            changes[flag] = want
    for key, want in CONFIG.items():
        if not _same(settings.get(key), want):
            changes[key] = want
    if cfg.enable_adb and not settings.get(ADB_KEY):
        changes[ADB_KEY] = 1
    return changes


def describe_changes(inst: Instance, settings: dict, changes: dict[str, object]) -> str:
    """The changes in words, e.g. "--cpu 3 -> 2"."""
    def was(name: str):
        if name == "--resolution":
            return f"{inst.width},{inst.height},{inst.dpi}"
        return settings.get(MODIFY[name][0] if name in MODIFY else name)
    return ", ".join(f"{name.lstrip('-')} {was(name)} -> {want}" for name, want in changes.items())


def set_up(install: Install, inst: Instance, changes: dict[str, object], run: Runner = _run,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Stop the instance if needed, then apply ``changes`` (from
    :func:`setup_needed`). It is started again by :func:`open_device`."""
    console, index = str(install.console), str(inst.index)
    if inst.running:
        log.info("Closing %s to change its settings", install.name)
        _quit(install, inst, run, sleep)
    flags = [arg for name, want in changes.items() if name.startswith("--") for arg in (name, str(want))]
    if flags:
        run([console, "modify", "--index", index, *flags])
    keys = {name: want for name, want in changes.items() if not name.startswith("--")}
    if keys:  # after modify, which rewrites the same file
        path = install.config_file(inst.index)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(keys)
        path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def _quit(install: Install, inst: Instance, run: Runner, sleep: Callable[[float], None]) -> None:
    """Close an instance and wait (up to 30 s) until it has."""
    run([str(install.console), "quit", "--index", str(inst.index)])
    for _ in range(30):
        sleep(1)
        current = next((i for i in instances(install, run) if i.index == inst.index), None)
        if current is None or not current.running:
            return


def _in_box(path: Path, installs: list[Install]) -> bool:
    """Whether ``path`` is in a virtual machine folder (ldplayer9box), not an LDPlayer's own."""
    text = str(path).lower().replace("/", "\\")
    return bool(re.search(r"ldplayer\d+box", text)) and not any(
        text.startswith(str(i.path).lower().replace("/", "\\").rstrip("\\") + "\\") for i in installs)


def _running(install: Install, index: int, run: Runner) -> bool:
    try:
        return any(i.index == index and i.running for i in instances(install, run))
    except Exception:  # noqa: BLE001 - a console that can't list: not known to be running
        return False


def emulator_of(path: Path, installs: list[Install]) -> tuple[str, Install | None]:
    """Which emulator a program belongs to: ("ldplayer", its install),
    ("bluestacks", None), ("mumu", None) or ("other", None)."""
    text = str(path).lower().replace("/", "\\")
    for install in installs:
        if text.startswith(str(install.path).lower().replace("/", "\\").rstrip("\\") + "\\"):
            return "ldplayer", install
    # LDPlayer 9 runs its virtual machine from C:\Program Files\ldplayer9box.
    if m := re.search(r"ldplayer(\d+)box", text):
        return "ldplayer", next((i for i in installs if i.version == int(m.group(1))), None)
    if "bluestacks" in text:
        return "bluestacks", None
    if "mumu" in text:
        return "mumu", None
    return "other", None


# A start that didn't take: neither the player nor its virtual machine
# runs this long after "launch" (it would answer in ~25 s): started again
# then, not after the whole launch timeout.
LAUNCH_GONE = 45  # seconds
PORT_FREE_TIMEOUT = 30  # seconds for the emulator that was closed to let go of the port


def clear_port(install: Install, inst: Instance, installs: list[Install], run: Runner = _run,
               sleep: Callable[[float], None] = time.sleep,
               owner_of: Callable[[int], Path | None] | None = None) -> bool:
    """Close whichever other emulator holds the instance's ADB port (an
    LDPlayer of another version, BlueStacks or MuMu): with it open, this
    instance has no connection. True if one was closed."""
    owner_of = owner_of or ports.owner
    path = owner_of(inst.port)
    if path is None:
        return False
    kind, other = emulator_of(path, installs)
    if kind == "ldplayer" and _in_box(path, installs):
        # The virtual machine folder (ldplayer9box) is shared by LDPlayer 9
        # and 14: which one holds the port is which one has an instance running.
        others = [o for o in installs if o.path != install.path and _running(o, inst.index, run)]
        if not others and _running(install, inst.index, run):
            return False  # this LDPlayer's own
        other = others[0] if others else other  # none listed as running: the folder's own LDPlayer
    if kind == "ldplayer" and (other is None or other.path == install.path or other.version == install.version):
        return False  # this LDPlayer's own
    if kind in ("ldplayer", "bluestacks", "mumu"):
        from farmbot import instances as farms

        def holds(cfg: DeviceConfig) -> bool:
            """The farm on the LDPlayer instance holding the port (same index, the other install)."""
            if kind != "ldplayer" or other is None:
                return True
            mine = pick_install(cfg, installs)
            return cfg.ldplayer_index == inst.index and mine is not None and mine.path == other.path

        farm = farms.farm_playing(kind, holds)
        if farm is None and kind != "ldplayer":  # nor another COCFARM program's (it shuts all of them)
            from farmbot import claims

            farm = claims.played_elsewhere(kind)
        if farm is not None:  # never close another farm's emulator
            holder = other.name if other else farms.EMULATOR_NAMES[kind]
            message = (f"farm {farm}'s {holder} is using port {inst.port}, which {install.name} instance "
                       f"{inst.index + 1} needs. Only one of the two can play: give one of them another "
                       "instance on the Farms page.")
            # Another emulator (MuMu's first instance also listens on 5555): this farm can move.
            raise DeviceError(message) if kind == "ldplayer" else PortHeldByFarm(message, farm, f"farm {farm}'s {holder}")
    if kind == "ldplayer":
        name = other.name
        log.warning("Closing %s: it is using port %d, which %s needs", name, inst.port, install.name)
        run([str(other.console), "quit", "--index", str(inst.index)])
    elif kind == "bluestacks":
        from farmbot import bluestacks

        name = "BlueStacks"
        log.warning("Closing BlueStacks: it is using port %d, which %s needs", inst.port, install.name)
        bluestacks.stop_player(sleep)
    elif kind == "mumu":
        from farmbot import mumu

        name = "MuMu Player"
        manager = mumu.find_install()
        if manager is None:
            raise DeviceError(f"MuMu Player is using port {inst.port}, which {install.name} needs. Close MuMu Player.")
        log.warning("Closing MuMu Player: it is using port %d, which %s needs", inst.port, install.name)
        run([str(manager.manager), "control", "-v", "all", "shutdown"])
    else:
        # Not an emulator COCFARM knows how to close: never kill an unknown program.
        raise DeviceError(f"{path.name} ({path.parent}) is using port {inst.port}, which {install.name} needs. "
                          f"Close it.")
    for _ in range(PORT_FREE_TIMEOUT):
        if owner_of(inst.port) is None:
            return True
        sleep(1)
    raise DeviceError(f"{name} did not close (it still holds port {inst.port}, which {install.name} needs)")


MOVE_WAIT = 1800  # seconds a farm waits for another to copy its instance away (copying takes minutes)


class PortHeldByFarm(DeviceError):
    """Another farm's emulator (not an LDPlayer) holds this instance's port."""

    def __init__(self, message: str, farm: int, holder: str):
        super().__init__(message)
        self.farm, self.holder = farm, holder


def sharing_farm(install: Install, inst: Instance, installs: list[Install]) -> int | None:
    """Another farm playing another LDPlayer's instance with this one's
    number, so its port: only one of the two could connect."""
    from farmbot import instances as farms

    def shares(cfg: DeviceConfig) -> bool:
        if cfg.ldplayer_index != inst.index:
            return False
        theirs = pick_install(cfg, installs)
        return theirs is not None and theirs.path != install.path

    return farms.farm_playing("ldplayer", shares)


def _used_elsewhere(install: Install, installs: list[Install], run: Runner) -> set[int]:
    """The instance numbers the other LDPlayers have (their ports are taken)."""
    used: set[int] = set()
    for other in installs:
        if other.path != install.path:
            try:
                used |= {i.index for i in instances(other, run)}
            except DeviceError as exc:
                log.warning("Couldn't list %s's instances: %s", other.name, exc)
    return used


def move_off_shared_port(cfg: DeviceConfig, install: Install, inst: Instance, installs: list[Install],
                         other: int, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
                         owner_of: Callable[[int], Path | None] | None = None, holder: str = "") -> Instance:
    """Copy this farm's instance (the game and its login with it) to a new
    one whose port no other LDPlayer (nor, with ``owner_of``, any other
    program) uses, and play that from now on (``cfg.ldplayer_index``
    changes). The old instance is kept. ``holder``: what holds the port,
    if not another LDPlayer's instance."""
    from farmbot import instances as farms

    log.warning("%s has the same port (%d) as %s instance %d: copying this instance to one with a port of its "
                "own (takes a few minutes)", holder or f"Farm {other}'s LDPlayer instance {inst.index + 1}",
                inst.port, install.name, inst.index + 1)
    from farmbot import portmap

    avoid = _used_elsewhere(install, installs, run)
    me = farms.current() or 1  # nor a number whose port a farm playing now wants (MuMu's first: 5555)
    avoid |= portmap.ldplayer_avoid(install.name, [d for n, d in list(farms.active.items()) if n != me])
    if owner_of is not None:  # ports held by other programs (MuMu: 5555): never copied to
        listed = {i.index for i in instances(install, run)}
        avoid |= {k for k in range(len(listed) + len(avoid) + 2) if k not in listed and owner_of(5555 + 2 * k)}
    farms.hook("status", f"Copying {install.name} instance {inst.index + 1} to one with a port of its own "
                         "(a few minutes)…")
    key = farms.create_instance(cfg, run=None if run is _run else run, close=True, sleep=sleep,
                                avoid=avoid, installs=installs)
    moved = next((i for i in instances(install, run) if i.index == int(key)), None)
    if moved is None:
        raise DeviceError(f"{install.name} has no instance {int(key) + 1} after copying")
    log.warning("This farm now plays %s instance %d (port %d). Instance %d is kept: delete it on the Farms page "
                "if you don't need it", install.name, moved.index + 1, moved.port, inst.index + 1)
    cfg.ldplayer_index = moved.index
    farms.hook("moved", inst.index, moved.index)  # kept in its settings straight away
    return moved


def wait_for_move(inst: Instance, other: int, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait while farm ``other`` copies its instance away from this one's
    port. True once it has (or stopped); False if it connected there instead."""
    from farmbot import instances as farms

    from farmbot.game import Stopped

    log.info("Farm %d shares this instance's port (%d): waiting while it moves to an instance of its own",
             other, inst.port)
    farms.hook("status", f"Waiting for farm {other} to move to an emulator instance of its own…")
    deadline = clock() + MOVE_WAIT
    while True:
        if farms.hook("stopped"):
            raise Stopped("stopped while waiting for another farm to move")
        theirs = farms.active.get(other)
        if theirs is None or theirs.ldplayer_index != inst.index:
            return True
        if other in farms.connected:
            return False
        if clock() >= deadline:
            raise DeviceError(f"farm {other} didn't move off port {inst.port} within {MOVE_WAIT // 60} minutes")
        sleep(5)


class WrongEmulator(DeviceError):
    """The address answered, but from another emulator."""


def is_bluestacks(props: str) -> bool:
    """True if ``getprop`` output is BlueStacks' (its own services and
    settings are named bst…)."""
    return bool(re.search(r"^\[(init\.svc\.bst|ro\.bst|bst\.|persist\.bst)", props, re.MULTILINE))


def open_device(cfg: DeviceConfig, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic,
                find: Callable[[], list[Install]] = find_installs,
                make: Callable[[str, str], AdbDevice] = AdbDevice,
                owner_of: Callable[[int], Path | None] | None = None) -> AdbDevice:
    """Find LDPlayer, set the instance up the way the bot needs it (once),
    start it if allowed, and connect over LDPlayer's own ADB."""
    installs = find()
    install = pick_install(cfg, installs)
    if install is None:
        raise DeviceError("LDPlayer was not found. Install LDPlayer 9 or newer, or pick another emulator in Setup.")
    found = instances(install, run)
    inst = next((i for i in found if i.index == cfg.ldplayer_index), None)
    if inst is None:
        raise DeviceError(f"{install.name} has no instance {cfg.ldplayer_index + 1} "
                          f"(have: {', '.join(str(i.index + 1) for i in found) or 'none'}).")
    other = sharing_farm(install, inst, installs)
    if other is not None:
        from farmbot import instances as farms

        # One of the two moves to an instance with a port of its own: the
        # later farm, or this one if the other is already playing.
        me = farms.current() or 1
        if other in farms.connected or other < me or not wait_for_move(inst, other, sleep, clock):
            inst = move_off_shared_port(cfg, install, inst, installs, other, run, sleep)
        elif inst.running and (owner_of or ports.owner)(inst.port) is None:
            # It started while the other held the port: no connection until started again.
            log.info("Restarting %s to get its connection", install.name)
            _quit(install, inst, run, sleep)
            inst.running = False
    settings = read_settings(install, inst.index)
    changes = setup_needed(inst, settings, cfg)
    if changes:
        log.warning("Setting up %s: %s (restarts it)", install.name, describe_changes(inst, settings, changes))
        set_up(install, inst, changes, run, sleep)
        inst.running = False
    try:
        cleared = clear_port(install, inst, installs, run, sleep, owner_of)
    except PortHeldByFarm as exc:
        # Another farm's MuMu or BlueStacks holds the port: this farm moves.
        inst = move_off_shared_port(cfg, install, inst, installs, exc.farm, run, sleep, owner_of or ports.owner,
                                    exc.holder)
        cleared = clear_port(install, inst, installs, run, sleep, owner_of)
    if cleared and inst.running:
        # It started after the other one, so without a connection: start it again.
        log.info("Restarting %s to get its connection", install.name)
        _quit(install, inst, run, sleep)
        inst.running = False
    if not inst.running:
        if not cfg.launch_emulator:
            raise DeviceError(f"{install.name} is not running. Start it, or turn on 'Open the emulator automatically' in Setup.")
        log.info("Starting %s (instance %d)", install.name, inst.index + 1)
        run([str(install.console), "launch", "--index", str(inst.index)])
    launched = clock()
    deadline = launched + cfg.launch_timeout
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
                switch_off_ad_apps(device, AD_PACKAGES, install.name)
                log.info("Connected to %s at %s", install.name, inst.serial)
                return device
        except WrongEmulator:
            raise
        except DeviceError as exc:
            last_error = exc
        if clock() >= deadline:
            raise NoAnswer(f"{install.name} did not answer over ADB within {cfg.launch_timeout:.0f}s"
                              + (f": {last_error}" if last_error else ""))
        if clock() - launched >= LAUNCH_GONE and _gone(install, inst.index, run):
            raise NoAnswer(f"{install.name} isn't running {clock() - launched:.0f}s after it was started "
                           "(neither the player nor its virtual machine)")
        sleep(3)


def _gone(install: Install, index: int, run: Runner) -> bool:
    """The instance has neither a player nor a virtual machine process."""
    try:
        current = next((i for i in instances(install, run) if i.index == index), None)
    except Exception:  # noqa: BLE001 - a console that can't list: not known to be gone
        return False
    return current is not None and current.pid <= 0 and current.vm_pid <= 0


def quit_instance(cfg: DeviceConfig, run: Runner = _run) -> None:
    """Close the configured LDPlayer instance (it is started again by
    ``open_device``)."""
    install = pick_install(cfg, find_installs())
    if install is None:
        raise DeviceError("LDPlayer was not found")
    log.warning("Closing %s (instance %d) to start it again", install.name, cfg.ldplayer_index + 1)
    run([str(install.console), "quit", "--index", str(cfg.ldplayer_index)])
