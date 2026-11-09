"""Finding and connecting to BlueStacks 5 on Windows.

BlueStacks records where it is installed in the registry
(``HKLM\\SOFTWARE\\BlueStacks_nxt``), keeps every instance's settings in
``bluestacks.conf`` in its data folder, and ships its own ADB as
``HD-Adb.exe``. With those three we can connect without the user typing
a port or installing platform-tools:

    bst.enable_adb_access="1"
    bst.instance.Pie64.display_name="BlueStacks App Player"
    bst.instance.Pie64.status.adb_port="5555"
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from farmbot import standard
from farmbot.config import EMULATORS, DeviceConfig
from farmbot.device import CONSOLE_TEXT, NO_WINDOW, AdbDevice, Device, DeviceError, GaveUp, NoAnswer

log = logging.getLogger(__name__)

# Registry keys of BlueStacks 5 editions, newest first.
REGISTRY_KEYS = ("SOFTWARE\\BlueStacks_nxt", "SOFTWARE\\BlueStacks_msi5", "SOFTWARE\\BlueStacks_nxt_cn")
DEFAULT_DIRS = (  # used when the registry has nothing
    (Path("C:/Program Files/BlueStacks_nxt"), Path("C:/ProgramData/BlueStacks_nxt")),
    (Path("C:/Program Files/BlueStacks_msi5"), Path("C:/ProgramData/BlueStacks_msi5")),
)
PLAYER_EXE = "HD-Player.exe"
ADB_EXE = "HD-Adb.exe"
_LINE = re.compile(r'^\s*([\w.\-]+)\s*=\s*"(.*)"\s*$')
# The ADB ports the other farms' instances want (another emulator's), given
# this farm's device: set by COCFARM's window once it knows the emulators.
# BlueStacks keeps the port written in its settings, so it's the one moved
# off theirs (farmbot.portmap). None: nothing's checked.
other_ports: Callable[[DeviceConfig], set[int]] | None = None


@dataclass
class Instance:
    name: str  # internal name, e.g. "Pie64" or "Rvc64_1"
    display_name: str
    adb_port: int | None

    @property
    def serial(self) -> str | None:
        return f"127.0.0.1:{self.adb_port}" if self.adb_port else None

    @property
    def label(self) -> str:
        port = f"port {self.adb_port}" if self.adb_port else "no ADB port yet"
        return f"{self.display_name} ({self.name}, {port})"


@dataclass
class Install:
    install_dir: Path
    data_dir: Path
    adb_enabled: bool
    instances: list[Instance] = field(default_factory=list)

    @property
    def adb_exe(self) -> Path:
        return self.install_dir / ADB_EXE

    @property
    def player_exe(self) -> Path:
        return self.install_dir / PLAYER_EXE

    def instance(self, name: str = "") -> Instance | None:
        """The named instance (by internal or display name), else the first one."""
        if name:
            for inst in self.instances:
                if name.lower() in (inst.name.lower(), inst.display_name.lower()):
                    return inst
            return None
        return self.instances[0] if self.instances else None


def parse_conf(text: str) -> tuple[bool, list[Instance]]:
    """``bluestacks.conf`` → (ADB access enabled, instances)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    names: list[str] = []
    for key in values:
        if key.startswith("bst.instance."):
            name = key.split(".")[2]
            if name not in names:
                names.append(name)
    instances = []
    for name in names:
        port_text = values.get(f"bst.instance.{name}.status.adb_port") or values.get(f"bst.instance.{name}.adb_port") or ""
        port = int(port_text) if port_text.isdigit() and int(port_text) > 0 else None
        instances.append(Instance(name, values.get(f"bst.instance.{name}.display_name") or name, port))
    # The master instance first, then clones in name order.
    instances.sort(key=lambda i: ("_" in i.name, i.name))
    return values.get("bst.enable_adb_access", "1") == "1", instances


def _registry_dirs(key: str) -> tuple[Path, Path] | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            def value(name: str) -> str | None:
                try:
                    return str(winreg.QueryValueEx(handle, name)[0])
                except OSError:
                    return None

            install = value("InstallDir")
            data = value("UserDefinedDir") or value("DataDir")
    except OSError:
        return None
    if not install or not data:
        return None
    return Path(install), Path(data)


def find_install(registry: Callable[[str], tuple[Path, Path] | None] = _registry_dirs,
                 defaults: tuple[tuple[Path, Path], ...] = DEFAULT_DIRS) -> Install | None:
    """Locate BlueStacks 5 and read its instances, or ``None`` if not installed."""
    candidates = [d for d in (registry(k) for k in REGISTRY_KEYS) if d] + list(defaults)
    for install_dir, data_dir in candidates:
        conf = data_dir / "bluestacks.conf"
        if not conf.is_file():
            continue
        adb_enabled, instances = parse_conf(conf.read_text(encoding="utf-8", errors="replace"))
        log.debug("BlueStacks at %s, data %s, instances %s", install_dir, data_dir, [i.name for i in instances])
        return Install(install_dir, data_dir, adb_enabled, instances)
    return None


def players(listing: str | None = None) -> list[tuple[int, str]]:
    """The BlueStacks windows open: (process id, the instance it shows).
    Each is an HD-Player.exe started with ``--instance <name>``; one
    without (an old shortcut) shows the main instance, given as ""."""
    if listing is None:
        if sys.platform != "win32":
            return []
        command = (f"Get-CimInstance Win32_Process -Filter \"Name='{PLAYER_EXE}'\" | "
                   "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        try:
            listing = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                                     text=True, **CONSOLE_TEXT, timeout=30, check=False, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    found = []
    for line in listing.splitlines():
        pid, _, command_line = line.strip().partition("\t")
        if pid.isdigit():
            match = re.search(r"--instance[\s=]+\"?([\w.\-]+)", command_line)
            found.append((int(pid), match.group(1) if match else ""))
    return found


def _shows(shown: str, instance: str, main: str) -> bool:
    return (shown or main).lower() == (instance or main).lower()


def is_running(instance: str = "", main: str = "") -> bool:
    """Is BlueStacks open? With ``instance`` (an internal name; ``main``,
    the main instance's), is that instance's window open: with several
    windows of COCFARM, each plays its own."""
    if sys.platform != "win32":
        return False
    if instance:
        found = players()
        if found:
            return any(_shows(shown, instance, main) for _, shown in found)
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PLAYER_EXE}", "/NH"],
                         capture_output=True, text=True, **CONSOLE_TEXT, check=False, creationflags=NO_WINDOW).stdout
    return PLAYER_EXE.lower() in out.lower() and not instance  # the listing failed: an instance's can't be told


def stop_player(sleep: Callable[[float], None] = time.sleep, running: Callable[[], bool] | None = None,
                instance: str = "", main: str = "") -> None:
    """Close every BlueStacks window, or just ``instance``'s, and wait for it to exit."""
    if instance:
        for pid, shown in players():
            if _shows(shown, instance, main):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False,
                               creationflags=NO_WINDOW)
        running = running or (lambda: is_running(instance, main))
    else:
        subprocess.run(["taskkill", "/IM", PLAYER_EXE, "/F"], capture_output=True, check=False,
                       creationflags=NO_WINDOW)
        running = running or is_running
    for _ in range(30):
        if not running():
            return
        sleep(1)
    raise DeviceError("BlueStacks did not close")


def turn_on_adb(install: Install, sleep: Callable[[float], None] = time.sleep,
                running: Callable[[], bool] = is_running, stop: Callable[..., None] = stop_player) -> None:
    """Switch on Settings > Advanced > Android Debug Bridge by editing
    ``bluestacks.conf``. BlueStacks reads it at start and writes it on exit,
    so it is closed first."""
    log.warning("ADB is off in BlueStacks; turning it on and restarting BlueStacks")
    if running():
        stop(sleep, running)
    conf = install.data_dir / "bluestacks.conf"
    data = conf.read_bytes()
    new, count = re.subn(rb'^bst\.enable_adb_access="[^"]*"', b'bst.enable_adb_access="1"', data, flags=re.M)
    if not count:  # not written yet: add it, keeping the file's line endings
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        new = data.rstrip(b"\r\n") + newline + b'bst.enable_adb_access="1"' + newline
    conf.write_bytes(new)


DISPLAY_KEYS = ("fb_width", "fb_height", "dpi")


def read_instance(install: Install, instance: Instance) -> dict[str, str]:
    """The instance's settings from bluestacks.conf, without the
    ``bst.instance.<name>.`` prefix (e.g. "fb_width", "cpus", "ram")."""
    try:
        text = (install.data_dir / "bluestacks.conf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    prefix = f"bst.instance.{instance.name}."
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and match.group(1).startswith(prefix):
            values[match.group(1)[len(prefix):]] = match.group(2)
    return values


def read_display(install: Install, instance: Instance) -> tuple[int, int, int] | None:
    """The instance's display as (width, height, dpi), from bluestacks.conf."""
    values = read_instance(install, instance)
    try:
        return tuple(int(values[k]) for k in DISPLAY_KEYS)  # type: ignore[return-value]
    except (KeyError, ValueError):
        return None


def wanted_settings(cfg: DeviceConfig | None = None) -> dict[str, int | str]:
    """farmbot.standard as bluestacks.conf keys: display (Settings > Display),
    CPU cores / memory (Settings > Performance) and frame rate (60, no high
    frame rate, no vertical sync) and the phone (Settings > Device: a device
    profile code, not a model name)."""
    return dict(fb_width=standard.WIDTH, fb_height=standard.HEIGHT, dpi=standard.DPI,
                cpus=standard.CPUS, ram=standard.MEMORY,
                max_fps=standard.FPS, enable_high_fps=0, enable_vsync=0,
                device_profile_code=standard.BLUESTACKS_PROFILE)


def settings_to_change(install: Install, instance: Instance, cfg: DeviceConfig) -> dict[str, int]:
    current = read_instance(install, instance)
    if not current:
        return {}
    return {k: v for k, v in wanted_settings(cfg).items() if current.get(k) != str(v)}


def set_display(install: Install, instance: Instance, width: int, height: int, dpi: int,
                sleep: Callable[[float], None] = time.sleep, running: Callable[[], bool] | None = None,
                stop: Callable[..., None] | None = None) -> None:
    """Give the instance a custom resolution and DPI (Settings > Display)."""
    apply_settings(install, instance, {"fb_width": width, "fb_height": height, "dpi": dpi}, sleep, running, stop)


def apply_settings(install: Install, instance: Instance, values: dict[str, int],
                   sleep: Callable[[float], None] = time.sleep, running: Callable[[], bool] | None = None,
                   stop: Callable[..., None] | None = None) -> None:
    """Write instance settings into ``bluestacks.conf`` while the instance
    is closed (it reads the file when it starts and writes it when it exits)."""
    main = install.instances[0].name if install.instances else ""
    running = running or (lambda: is_running(instance.name, main))
    stop = stop or (lambda sleep, running: stop_player(sleep, running, instance.name, main))
    log.warning("Setting up BlueStacks: %s (restarts BlueStacks)", ", ".join(f"{k}={v}" for k, v in values.items()))
    if running():
        stop(sleep, running)
    conf = install.data_dir / "bluestacks.conf"
    data = conf.read_bytes()
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    wanted: dict[str, int] = dict(values)
    if "fb_width" in wanted:
        wanted["custom_resolution_selected"] = 1
    for key, value in wanted.items():
        name = f"bst.instance.{instance.name}.{key}".encode()
        line = name + b'="' + str(value).encode() + b'"'
        data, count = re.subn(rb"^" + re.escape(name) + rb'="[^"]*"', line, data, flags=re.M)
        if not count:
            data = data.rstrip(b"\r\n") + newline + line + newline
    conf.write_bytes(data)


def launch(install: Install, instance: Instance) -> None:
    log.info("Starting BlueStacks (%s)", instance.display_name)
    subprocess.Popen([str(install.player_exe), "--instance", instance.name],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def adb_program(cfg: DeviceConfig, install: Install | None) -> str:
    """The configured adb, or BlueStacks' own when left at plain 'adb'."""
    if cfg.adb_path not in ("", "adb") or install is None or not install.adb_exe.is_file():
        return cfg.adb_path or "adb"
    return str(install.adb_exe)


def open_device(cfg: DeviceConfig, sleep: Callable[[float], None] = time.sleep,
                find: Callable[[], Install | None] = find_install,
                running: Callable[[], bool] | None = None,
                start: Callable[[Install, Instance], None] = launch,
                make: Callable[[str, str], AdbDevice] = AdbDevice,
                enable: Callable[[Install], None] = turn_on_adb,
                clock: Callable[[], float] = time.monotonic,
                apply: Callable[..., None] = apply_settings) -> Device:
    """Connect to the device the config describes.

    In "bluestacks" mode this finds BlueStacks, starts it if allowed and
    needed, and waits until its ADB answers (or, with ADB turned off,
    until its window opens). In "manual" mode ``serial``
    is used as given.
    """
    if cfg.mode == "ldplayer":
        from farmbot import ldplayer

        return ldplayer.open_device(cfg, sleep=sleep, clock=clock)
    if cfg.mode == "mumu":
        from farmbot import mumu

        return mumu.open_device(cfg, sleep=sleep, clock=clock)
    install = find() if cfg.mode == "bluestacks" or cfg.adb_path in ("", "adb") else None
    adb = adb_program(cfg, install)
    if cfg.mode == "manual":
        device = make(cfg.serial, adb)
        device.connect()
        return device

    if install is None:
        raise DeviceError("BlueStacks 5 was not found. Install it, or pick another emulator in Setup.")
    instance = install.instance(cfg.bluestacks_instance)
    if instance is None:
        names = ", ".join(i.display_name for i in install.instances) or "none"
        raise DeviceError(f"BlueStacks instance {cfg.bluestacks_instance!r} not found (have: {names}).")

    if running is None:  # this instance's window: other COCFARM windows may play other instances
        main = install.instances[0].name if install.instances else ""
        running = lambda: is_running(instance.name, main)  # noqa: E731

    changes = settings_to_change(install, instance, cfg)
    if other_ports is not None:
        from farmbot import portmap

        port = portmap.bluestacks_port(instance.adb_port, other_ports(cfg),
                                       [i.adb_port for i in install.instances if i is not instance and i.adb_port])
        if port is not None:
            log.info("BlueStacks' port %s is another emulator's: giving %s port %d", instance.adb_port,
                     instance.display_name, port)
            changes.update({"adb_port": port, "status.adb_port": port})
    if changes:
        apply(install, instance, changes)

    if not install.adb_enabled:
        if not cfg.enable_adb:
            if not running():
                start(install, instance)
            return open_window(install, instance, cfg, sleep, clock)
        enable(install)

    if not running():
        if not cfg.launch_emulator and install.adb_enabled:
            raise DeviceError("BlueStacks is not running. Start it, or turn on 'Open the emulator automatically' in Setup.")
        start(install, instance)

    deadline = clock() + cfg.launch_timeout
    last_error: Exception | None = None
    while True:
        # BlueStacks writes the port once the instance has booted, and can
        # change it between runs, so re-read it on every attempt.
        current = find() or install
        inst = current.instance(instance.name) or instance
        if inst.serial:
            try:
                device = make(inst.serial, adb)
                device.connect()
                device.shell("echo", "ok")
                log.info("Connected to %s at %s", inst.display_name, inst.serial)
                return device
            except DeviceError as exc:
                last_error = exc
        if clock() >= deadline:
            raise NoAnswer(f"BlueStacks did not accept an ADB connection within {cfg.launch_timeout:.0f}s"
                              + (f": {last_error}" if last_error else ""))
        sleep(3)


def open_window(install: Install, instance: Instance, cfg: DeviceConfig,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> Device:
    """With ADB off, drive the instance's window instead (see ``farmbot.window``)."""
    from farmbot.window import WindowDevice

    log.info("ADB is off in BlueStacks: using its window (keep it on screen; the bot moves the mouse)")
    device = WindowDevice(instance.display_name, str(install.player_exe), instance.name, cfg.package)
    deadline = clock() + cfg.launch_timeout
    while True:
        try:
            device.connect()
            return device
        except DeviceError:
            if clock() >= deadline:
                raise NoAnswer(f"the BlueStacks window {instance.display_name!r} did not open "
                                  f"within {cfg.launch_timeout:.0f}s") from None
        sleep(3)


EMULATOR_TRIES = 3  # times the emulator is started before the bot gives up on it

# The sound the game makes (music) and the emulator's own (system, notifications).
MUTED_STREAMS = (3, 1, 5)
# Android 9 and newer have "cmd media_session volume"; older ones the "media" tool.
# MuMu's Android 15 ignores media_session's 0 (left at 100, or put at 1 of 100):
# "cmd audio set-volume" (Android 14 and newer) sets 0 there, so it goes last.
_MUTE = ("cmd media_session volume --stream {s} --set 0 >/dev/null 2>&1; "
         "media volume --stream {s} --set 0 >/dev/null 2>&1; "
         "cmd audio set-volume {s} 0 >/dev/null 2>&1")


def mute(device: Device) -> None:
    """Turn the emulator's volume down to nothing (``device.mute``): the
    volume inside Android, so it works the same in every emulator, with
    nothing restarted. Best effort: a device that won't is left as it is."""
    if not isinstance(device, AdbDevice):
        return  # (driving a window: no shell)
    try:
        # "; true": one of the two tools missing (exit 127) is no failure.
        device.shell("; ".join(_MUTE.format(s=s) for s in MUTED_STREAMS) + "; true")
        out = device.shell("cmd media_session volume --stream 3 --get 2>&1; media volume --stream 3 --get 2>&1; true")
        m = re.search(r"volume is (\d+)", out)
        log.info("Muted the emulator (music volume: %s)", m.group(1) if m else "?", extra={"file_only": True})
    except DeviceError as exc:
        log.info("Couldn't mute the emulator: %s", exc)


def close_emulator(cfg: DeviceConfig, sleep: Callable[[float], None] = time.sleep) -> None:
    """Close the configured emulator (it is started again by ``open_device``)."""
    if cfg.mode == "ldplayer":
        from farmbot import ldplayer

        ldplayer.quit_instance(cfg)
    elif cfg.mode == "mumu":
        from farmbot import mumu

        mumu.quit_instance(cfg, sleep=sleep)
    elif cfg.mode == "bluestacks":
        install = find_install()
        instance = install.instance(cfg.bluestacks_instance) if install else None
        log.warning("Closing BlueStacks to start it again")
        if instance is None:
            stop_player(sleep)
        else:  # just this instance: other COCFARM windows may be playing the others
            stop_player(sleep, instance=instance.name, main=install.instances[0].name)
    else:
        raise DeviceError("can't restart a device given by its ADB address")
    sleep(5)


def open_device_retrying(cfg: DeviceConfig, sleep: Callable[[float], None] = time.sleep,
                         opener: Callable[..., Device] = open_device,
                         closer: Callable[..., None] = close_emulator) -> Device:
    """``open_device``; when the emulator doesn't answer, close it and start
    it again, up to :data:`EMULATOR_TRIES` starts in all, then :class:`GaveUp`.
    Only an emulator COCFARM starts by itself is restarted."""
    for attempt in range(1, EMULATOR_TRIES + 1):
        try:
            device = opener(cfg, sleep=sleep)
            if cfg.mute and cfg.mode in EMULATORS:  # (not a phone given by its address)
                mute(device)
            return device
        except NoAnswer as exc:
            if cfg.mode not in EMULATORS or not cfg.launch_emulator:
                raise
            if attempt == EMULATOR_TRIES:
                raise GaveUp(f"{exc} (started {EMULATOR_TRIES} times)") from exc
            log.warning("%s; closing the emulator to start it again (%d of %d)", exc, attempt + 1, EMULATOR_TRIES)
            closer(cfg, sleep)
    raise GaveUp("the emulator did not answer")  # not reached: the last try returns or raises


def restart_emulator(cfg: DeviceConfig, sleep: Callable[[float], None] = time.sleep) -> Device:
    """Close the emulator and open it again (the watchdog's last resort when
    the game or ADB stops answering). Returns the new connection."""
    close_emulator(cfg, sleep)
    return open_device_retrying(cfg, sleep)
