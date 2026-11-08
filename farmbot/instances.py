"""Several accounts at once: one farm per emulator instance, all run from
the one COCFARM window (the Farms page switches each on and off).

Farm 1 is COCFARM's own folder, as always. Each farm after it has a folder
of its own, ``instances/<n>/``, with its ``farmbot.toml`` (which emulator
instance it plays), attack history and error screens. The account
profiles (``profiles/``), the button pictures (``templates/``) and the log
(``logs/``, each line marked with its farm) are shared, so an account
keeps its settings whichever farm plays it.

Each farm's bot runs on a thread of its own, marked with :func:`mark` so
its log lines say which farm wrote them.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from farmbot.config import Config, DeviceConfig, ensure_config, load_config, save_config
from farmbot.device import DeviceError

log = logging.getLogger(__name__)

FOLDER = "instances"
CONFIG = "farmbot.toml"
MOST = 12  # farms in all: each runs an emulator with 2 cores and 4 GB

ROOT = Path.cwd()  # COCFARM's own folder (farm 1's)


# ------------------------------------------------------------ which farm a log line is from
_here = threading.local()


def mark(n: int) -> None:
    """Log lines from this thread are farm ``n``'s."""
    _here.farm = n


def current() -> int:
    """The farm this thread works for (0: none in particular, e.g. the window's own)."""
    return getattr(_here, "farm", 0)


_make_record = logging.getLogRecordFactory()


def _record(*args, **kwargs) -> logging.LogRecord:
    record = _make_record(*args, **kwargs)
    record.farm = current()
    record.farm_tag = f"[farm {record.farm}] " if record.farm > 1 else ""
    return record


logging.setLogRecordFactory(_record)


# ------------------------------------------------------------ farms and their folders
def folder(n: int, root: Path | None = None) -> Path:
    root = root or ROOT
    return root if n == 1 else root / FOLDER / str(n)


def config_path(n: int, root: Path | None = None) -> Path:
    return folder(n, root) / CONFIG


def numbers(root: Path | None = None) -> list[int]:
    """The farms there are: 1, then each one added, in order."""
    found = [1]
    base = (root or ROOT) / FOLDER
    if base.is_dir():
        found += sorted(int(p.name) for p in base.iterdir()
                        if p.name.isdigit() and int(p.name) > 1 and (p / CONFIG).is_file())
    return found


PICKED = "picked.json"  # in the instances folder: the farms Start starts


def picked(root: Path | None = None) -> list[int]:
    """The farms picked on the Farms page (each one, until some are unpicked)."""
    there = numbers(root)
    try:
        saved = json.loads(((root or ROOT) / FOLDER / PICKED).read_text(encoding="utf-8"))
        return [n for n in there if n in saved]
    except (OSError, ValueError, TypeError):
        return there


def pick(n: int, on: bool = True, root: Path | None = None) -> None:
    now = set(picked(root))
    now = now | {n} if on else now - {n}
    path = (root or ROOT) / FOLDER / PICKED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(now)), encoding="utf-8")


def device_of(n: int, root: Path | None = None) -> DeviceConfig:
    return load_config(config_path(n, root)).device


def resolve(config: Config, path: str | Path) -> Path:
    """A file the settings name (the attack history, the error screens), in
    the farm's own folder when given without one (``config.home``)."""
    path = Path(path)
    home = getattr(config, "home", None)
    return path if path.is_absolute() or home is None else Path(home) / path


@dataclass
class Choice:
    """An emulator instance a farm can play."""

    key: str  # what it's set by: the instance's number (LDPlayer, MuMu) or name (BlueStacks)
    label: str  # e.g. "1: LDPlayer-1"
    mode: str = ""  # its emulator: "ldplayer", "mumu", "bluestacks"
    install: str = ""  # which LDPlayer ("LDPlayer 9")
    port: int | None = None  # its ADB port, when known

    @property
    def emulator(self) -> str:
        return self.install or EMULATOR_NAMES.get(self.mode, self.mode)

    def device(self, base: DeviceConfig | None = None) -> DeviceConfig:
        """``base``'s settings, playing this instance."""
        cfg = copy.deepcopy(base) if base is not None else DeviceConfig()
        if cfg.mode != self.mode:  # another emulator: its own adb and address
            cfg.adb_path, cfg.serial = DeviceConfig.adb_path, DeviceConfig.serial
        cfg.mode = self.mode
        if self.mode == "ldplayer":
            cfg.ldplayer = self.install
        set_key(cfg, self.key)
        return cfg


EMULATOR_NAMES = {"ldplayer": "LDPlayer", "mumu": "MuMu Player", "bluestacks": "BlueStacks"}


@dataclass
class Emulator:
    """An installed emulator."""

    mode: str
    name: str  # "LDPlayer 9", "MuMu Player", "BlueStacks"
    install: str = ""  # which LDPlayer


def emulators() -> list[Emulator]:
    """The emulators installed on this PC."""
    from farmbot import bluestacks, ldplayer, mumu

    found = [Emulator("ldplayer", i.name, i.name) for i in ldplayer.find_installs()]
    if mumu.find_install() is not None:
        found.append(Emulator("mumu", "MuMu Player"))
    if bluestacks.find_install() is not None:
        found.append(Emulator("bluestacks", "BlueStacks"))
    return found


# Where Windows keeps what's installed: each emulator's version (its "DisplayVersion").
UNINSTALL_KEYS = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                  r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
INSTALLED_NAMES = {"mumu": re.compile(r"^mumu", re.I), "bluestacks": re.compile(r"^bluestacks$", re.I)}


def _installed_versions() -> dict[str, str]:
    """Each program installed, by its name, and its version (Windows only)."""
    try:
        import winreg
    except ImportError:
        return {}
    found: dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in UNINSTALL_KEYS:
            try:
                parent = winreg.OpenKey(root, key)
            except OSError:
                continue
            with parent:
                for i in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as entry:
                            name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                            found.setdefault(name, str(winreg.QueryValueEx(entry, "DisplayVersion")[0]))
                    except OSError:
                        continue
    return found


def major_version(mode: str, install: str = "", installed: dict[str, str] | None = None) -> str:
    """The emulator's version number, as players call it: LDPlayer's from its
    name ("LDPlayer 14": "14"), the others' from what Windows has installed
    (BlueStacks 5.22.110: "5"). "" if not known."""
    if mode == "ldplayer":
        number = re.search(r"\d+", install or "")
        return number.group(0) if number else ""
    pattern = INSTALLED_NAMES.get(mode)
    if pattern is None:
        return ""
    installed = _installed_versions() if installed is None else installed
    version = next((v for name, v in sorted(installed.items()) if pattern.search(name)), "")
    number = re.match(r"\d+", version)
    return number.group(0) if number else ""


@functools.lru_cache(maxsize=None)
def emulator_badge(mode: str, install: str = "") -> tuple[Path | None, str]:
    """For the Farms page's pictures: the emulator's own program (its icon)
    and version number. Looked up once."""
    from farmbot import bluestacks, ldplayer, mumu

    program: Path | None = None
    try:
        if mode == "ldplayer":
            installs = ldplayer.find_installs()
            found = next((i for i in installs if i.name == install), installs[0] if installs else None)
            program = found.path / "dnplayer.exe" if found else None
            install = install or (found.name if found else "")
        elif mode == "mumu":
            tools = mumu.find_install()
            if tools is not None:
                program = next((p for p in (tools.tools / "MuMuNxMain.exe", tools.tools / "MuMuPlayer.exe")
                                if p.is_file()), None)
        elif mode == "bluestacks":
            player = bluestacks.find_install()
            program = player.player_exe if player else None
    except (OSError, DeviceError) as exc:
        log.debug("Couldn't find %s's program: %s", mode, exc)
    try:
        version = major_version(mode, install)
    except OSError:
        version = ""
    return (program if program is not None and program.is_file() else None), version


def choices(cfg: DeviceConfig | None = None) -> list[Choice]:
    """The instances made in the emulators' multi-instance managers: of
    ``cfg``'s emulator, or (without) of every emulator installed."""
    from farmbot import bluestacks, ldplayer, mumu

    found: list[Choice] = []
    mode = cfg.mode if cfg is not None else None
    if mode in (None, "ldplayer"):
        installs = ldplayer.find_installs()
        if cfg is not None:
            picked = ldplayer.pick_install(cfg, installs)
            installs = [picked] if picked else []
        for install in installs:
            try:
                found += [Choice(str(i.index), f"{i.index + 1}: {i.title}", "ldplayer", install.name, i.port)
                          for i in ldplayer.instances(install)]
            except DeviceError as exc:
                log.warning("Couldn't list %s's instances: %s", install.name, exc)
    if mode in (None, "mumu"):
        install = mumu.find_install()
        if install is not None:
            try:
                found += [Choice(str(i.index), f"{i.index + 1}: {i.name}", "mumu", "", i.adb_port)
                          for i in mumu.instances(install)]
            except DeviceError as exc:
                log.warning("Couldn't list MuMu Player's instances: %s", exc)
    if mode in (None, "bluestacks"):
        install = bluestacks.find_install()
        if install is not None:
            found += [Choice(i.name, i.display_name, "bluestacks", "", i.adb_port) for i in install.instances]
    return found


def port_clash(device: DeviceConfig, others: dict[int, DeviceConfig], found: list[Choice]) -> int | None:
    """The farm whose emulator instance has the same ADB port as ``device``'s
    (LDPlayer's first instance and BlueStacks' are both on 5555, say): only
    one of the two can be connected at a time."""
    def port(cfg: DeviceConfig) -> int | None:
        return next((c.port for c in found if same_instance(c.device(cfg), cfg)), None)

    mine = port(device)
    if mine is None:
        return None
    return next((n for n, other in others.items() if not same_instance(other, device) and port(other) == mine), None)


# The farms playing now, each with its emulator instance: COCFARM never
# closes an emulator another farm plays (to free a port, say).
active: dict[int, DeviceConfig] = {}
# The farms among them connected to their emulator (past starting it).
connected: set[int] = set()
# The account each farm's game is in (its profile, as read in the game).
accounts_playing: dict[int, str] = {}
_accounts_lock = threading.Lock()


def claim_account(name: str) -> int | None:
    """This thread's farm plays account ``name``: kept, unless another farm
    already plays it. Returns that farm (and claims nothing), or None."""
    me = current() or 1
    with _accounts_lock:
        other = next((n for n, playing in accounts_playing.items() if n != me and playing == name), None)
        if other is None:
            accounts_playing[me] = name
        return other


# Each farm's window's ears while it connects: "status" (a line to show),
# "moved" (its LDPlayer instance changed: was, now), "stopped" (Stop pressed?).
hooks: dict[int, dict[str, Callable]] = {}


def hook(name: str, *args):
    """Call this thread's farm's ``name`` hook, if it has one (None if not)."""
    call = hooks.get(current() or 1, {}).get(name)
    return call(*args) if call is not None else None


def farm_playing(mode: str, holds: Callable[[DeviceConfig], bool] | None = None) -> int | None:
    """Another farm (not this thread's) playing on an emulator of this kind
    (and, given ``holds``, on the instance it accepts)."""
    me = current() or 1
    return next((n for n, cfg in list(active.items())
                 if n != me and cfg.mode == mode and (holds is None or holds(cfg))), None)


def key_of(cfg: DeviceConfig) -> str:
    """Which instance ``cfg`` plays, as in :class:`Choice`."""
    if cfg.mode == "ldplayer":
        return str(cfg.ldplayer_index)
    if cfg.mode == "mumu":
        return str(cfg.mumu_index)
    if cfg.mode == "bluestacks":
        return cfg.bluestacks_instance
    return cfg.serial


def same_instance(a: DeviceConfig, b: DeviceConfig) -> bool:
    """True if both play the same emulator instance."""
    if a.mode != b.mode:
        return False
    if a.mode == "bluestacks":
        return (a.bluestacks_instance or "").lower() == (b.bluestacks_instance or "").lower()
    if a.mode == "ldplayer" and a.ldplayer and b.ldplayer and a.ldplayer != b.ldplayer:
        return False  # LDPlayer 9's instance 0 and LDPlayer 14's are different ("": the newest found)
    return key_of(a) == key_of(b)


def set_key(cfg: DeviceConfig, key: str) -> None:
    if cfg.mode == "ldplayer":
        cfg.ldplayer_index = int(key)
    elif cfg.mode == "mumu":
        cfg.mumu_index = int(key)
    elif cfg.mode == "bluestacks":
        cfg.bluestacks_instance = key
    else:
        cfg.serial = key


def describe(cfg: DeviceConfig) -> str:
    """The instance in words, e.g. "LDPlayer 9 instance 1"."""
    if cfg.mode == "ldplayer":
        return f"{cfg.ldplayer or 'LDPlayer'} instance {cfg.ldplayer_index + 1}"  # counted from 1
    if cfg.mode == "mumu":
        return f"MuMu instance {cfg.mumu_index + 1}"
    if cfg.mode == "bluestacks":
        return f"BlueStacks {cfg.bluestacks_instance or 'main instance'}"
    return f"Device {cfg.serial}"


# How big the game draws its buttons and windows at the display COCFARM sets
# up: the same on every emulator, so a new farm gets farm 1's.
SIZES = ("ui_scale", "window_scale", "picture_scales")


def _no_sizes(device: DeviceConfig) -> bool:
    return not device.ui_scale and not device.window_scale and not device.picture_scales


def mend_sizes(root: Path | None = None) -> list[int]:
    """Farms added before 0.9.10 from an instance's switch got none of farm
    1's sizes (so the battle menu's and the army screen's buttons went
    unseen): give them farm 1's. Returns the farms mended."""
    first = config_path(1, root)
    if not first.is_file():
        return []
    base = load_config(first).device
    if _no_sizes(base):
        return []
    mended = []
    for n in numbers(root):
        path = config_path(n, root)
        if n == 1 or not path.is_file():
            continue
        try:
            config = load_config(path)
        except (OSError, ValueError) as exc:
            log.warning("Farm %d's settings can't be read: %s", n, exc)
            continue
        if _no_sizes(config.device):
            for name in SIZES:
                setattr(config.device, name, copy.deepcopy(getattr(base, name)))
            save_config(config, path)
            mended.append(n)
    if mended:
        log.info("Gave farm%s %s farm 1's button sizes", "s" * (len(mended) > 1), ", ".join(map(str, mended)))
    return mended


def add(device: DeviceConfig | str, source: int = 1, root: Path | None = None, account: str = "") -> int:
    """A new farm playing ``device``'s emulator instance (or instance ``key``
    of farm ``source``'s emulator), with farm ``source``'s settings.
    ``account``: the profile it plays with, when known (a copy of another
    farm's instance is signed in to that farm's account); else it's found
    when the game shows the player's name. Returns its number."""
    root = root or ROOT
    taken = numbers(root)
    if len(taken) >= MOST:
        raise ValueError(f"COCFARM runs {MOST} farms at most")
    n = next(i for i in range(2, MOST + 2) if i not in taken)
    if source == 1:
        ensure_config(config_path(1, root))
    config = load_config(config_path(source, root))
    if isinstance(device, str):
        set_key(config.device, device)
    else:  # its emulator instance, with the source's ADB, start-up and sizes
        base, config.device = config.device, copy.deepcopy(device)
        for name in ("enable_adb", "mute", "launch_emulator", "launch_timeout", "package", *SIZES):
            setattr(config.device, name, copy.deepcopy(getattr(base, name)))
    for other in taken:
        if same_instance(device_of(other, root), config.device):
            raise ValueError(f"farm {other} plays {describe(config.device)} already")
    config.profiles.active = account  # "": picked when the game shows the player's name
    path = config_path(n, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_config(config, path)
    log.info("Added farm %d: %s", n, describe(config.device))
    pick(n, True, root)  # a new farm is picked: Start starts it
    return n


def remove(n: int, root: Path | None = None) -> None:
    """Forget farm ``n`` (stop it first). Its folder is kept, renamed
    ``removed-<n>``, so its attack history isn't lost."""
    if n == 1:
        raise ValueError("farm 1 is COCFARM's own")
    path = folder(n, root)
    target = path.with_name(f"removed-{n}")
    i = 1
    while target.exists():
        i += 1
        target = path.with_name(f"removed-{n}-{i}")
    pick(n, False, root)
    path.rename(target)
    log.info("Removed farm %d (its files are in %s)", n, target)


# ------------------------------------------------------------ new emulator instances
class StillOpen(DeviceError):
    """The instance to copy is open (a running one can't be copied)."""


def can_create(cfg: DeviceConfig) -> bool:
    """True if COCFARM can make a new instance of this emulator by itself."""
    return cfg.mode in ("ldplayer", "mumu")


def _copying(cmd: list[str]) -> str:
    """Runs the emulator's copy command: copying a disk of the game takes minutes."""
    import subprocess

    from farmbot.mumu import _clean_env

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=1200, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=_clean_env())
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise DeviceError(f"couldn't copy the instance: {exc}") from exc
    return result.stdout.decode("utf-8", errors="replace")


def _wait_closed(still_open, sleep) -> None:
    for _ in range(60):
        if not still_open():
            return
        sleep(1)
    raise DeviceError("the emulator didn't close: close it, then press Create again")


def source_for(emulator: Emulator, farms: dict[int, DeviceConfig], found: list[Choice] | None = None) -> DeviceConfig:
    """What a new instance of ``emulator`` copies: the last farm's instance
    on it (the game installed and set up), else its first instance (of
    ``found``, the instances listed already: asking MuMu again can take long)."""
    for n in sorted(farms, reverse=True):
        cfg = farms[n]
        if cfg.mode == emulator.mode and (emulator.mode != "ldplayer" or cfg.ldplayer in ("", emulator.install)):
            return copy.deepcopy(cfg)
    cfg = DeviceConfig(mode=emulator.mode, ldplayer=emulator.install)
    if emulator.mode == "mumu":
        listed = [c for c in found if c.mode == "mumu"] if found is not None else choices(cfg)
        if listed:
            cfg.mumu_index = int(listed[0].key)
    return cfg


def bluestacks_manager() -> Path | None:
    """BlueStacks' Multi-Instance Manager, where its instances are made."""
    from farmbot import bluestacks

    install = bluestacks.find_install()
    if install is None:
        return None
    path = install.install_dir / "HD-MultiInstanceManager.exe"
    return path if path.is_file() else None


SPARE_ROOM = 1 << 30  # bytes kept free on top of the copy


def _room_for(folder: Path, drive: Path) -> None:
    """Stop before copying ``folder`` (an instance's disks: several GB) to a
    drive without room for it."""
    import shutil

    if not folder.is_dir():
        return
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    free = shutil.disk_usage(drive).free
    if free < size + SPARE_ROOM:
        raise DeviceError(f"not enough disk space to copy the instance: it needs {(size + SPARE_ROOM) / 2**30:.1f} GB "
                          f"and {drive.anchor or drive} has {free / 2**30:.1f} GB free")


SPARE_TITLE = "COCFARM spare"  # an empty instance holding a number whose port is another LDPlayer's


def _spares(install, listed: list, avoid, run) -> list:
    """LDPlayer gives a new instance the lowest free number: while that
    number's port is another LDPlayer's (in ``avoid``), an empty spare
    instance takes it, so the copy lands past it. Returns the instances now."""
    from farmbot import ldplayer

    def lowest(found) -> int:
        taken = {i.index for i in found}
        return next(k for k in range(len(taken) + 1) if k not in taken)

    while (k := lowest(listed)) in avoid:
        log.info("Adding an empty instance as number %d: its port is another LDPlayer's", k + 1)
        run([str(install.console), "add", "--name", f"{SPARE_TITLE} {k + 1}"])
        listed = ldplayer.instances(install, run)
        if all(i.index != k for i in listed):
            raise DeviceError(f"{install.name} didn't add an instance")
    return listed


def _remove_spares(install, listed: list, run) -> None:
    """The copy has its number: the empty spares that kept it off another
    LDPlayer's port go again (numbers aren't handed down, so the copy keeps
    its own), and no instances pile up in the emulator."""
    for spare in listed:
        if spare.title.startswith(SPARE_TITLE) and not spare.running and spare.index != 0:
            log.info("Removing the empty instance %s", spare.title)
            try:
                run([str(install.console), "remove", "--index", str(spare.index)])
            except DeviceError as exc:  # (it stays: harmless, and removed after the next copy)
                log.info("Couldn't remove %s: %s", spare.title, exc)


def create_instance(cfg: DeviceConfig, run=None, close: bool = False, sleep=time.sleep,
                    avoid: set[int] | frozenset[int] = frozenset(), installs=None) -> str:
    """Make a new instance of ``cfg``'s emulator, a copy of the one ``cfg``
    plays (with the game installed). Returns the new instance's key. A
    running one can't be copied: with ``close`` it's closed first, else
    :class:`StillOpen` is raised. LDPlayer: the copy never gets a number in
    ``avoid`` (whose ports another LDPlayer's instances use)."""
    from farmbot import ldplayer, mumu

    copying = _copying if run is None else None  # tests pass their own run for everything
    if cfg.mode == "ldplayer":
        run = run or ldplayer._run
        install = ldplayer.pick_install(cfg, installs if installs is not None else ldplayer.find_installs())
        if install is None:
            raise DeviceError("LDPlayer was not found")
        before = ldplayer.instances(install, run)
        source = next((i for i in before if i.index == cfg.ldplayer_index), None)
        if source is None:
            raise DeviceError(f"{install.name} has no instance {cfg.ldplayer_index + 1}")
        _room_for(install.path / "vms" / f"leidian{source.index}", install.path)
        if source.running:
            if not close:
                raise StillOpen(source.title)
            log.info("Closing %s to copy it", source.title)
            run([str(install.console), "quit", "--index", str(source.index)])
            _wait_closed(lambda: any(i.running for i in ldplayer.instances(install, run) if i.index == source.index),
                         sleep)
        before = _spares(install, before, avoid, run)
        titles = {i.title for i in before}
        k = 1
        while f"LDPlayer-{k}" in titles:
            k += 1
        name = f"LDPlayer-{k}"
        log.info("Copying %s to a new instance, %s", source.title, name)
        (copying if copying else run)([str(install.console), "copy", "--name", name, "--from", str(source.index)])
        made = [i for i in ldplayer.instances(install, run) if i.index not in {b.index for b in before}]
        if not made:
            raise DeviceError(f"{install.name} didn't make the copy")
        if made[0].index in avoid:
            raise DeviceError(f"{install.name} made the copy as instance {made[0].index + 1}, whose port another "
                              "LDPlayer's instance uses")
        _remove_spares(install, before, run)
        return str(made[0].index)
    if cfg.mode == "mumu":
        run = run or mumu._run
        install = mumu.find_install()
        if install is None:
            raise DeviceError("MuMu Player was not found")
        before = mumu.instances(install, run)
        source = next((i for i in before if i.index == cfg.mumu_index), None)
        if source is None:
            raise DeviceError(f"MuMu Player has no instance {cfg.mumu_index + 1}")
        if source.started:
            if not close:
                raise StillOpen(source.name or f"MuMu instance {source.index + 1}")
            log.info("Closing MuMu instance %d to copy it", source.index + 1)
            run([str(install.manager), "control", "-v", str(source.index), "shutdown"])
            _wait_closed(lambda: any(i.started for i in mumu.instances(install, run) if i.index == source.index),
                         sleep)
        log.info("Copying MuMu instance %d to a new one", source.index + 1)
        (copying if copying else run)([str(install.manager), "clone", "-v", str(source.index)])
        made = [i for i in mumu.instances(install, run) if i.index not in {b.index for b in before}]
        if not made:
            raise DeviceError("MuMu Player didn't make the copy")
        return str(made[0].index)
    raise DeviceError("COCFARM can make new instances of LDPlayer and MuMu. For BlueStacks, use its "
                      "Multi-Instance Manager, then add the farm here.")


def can_delete(choice: Choice) -> bool:
    """COCFARM deletes LDPlayer and MuMu instances, never an emulator's first
    one (the emulator's own); BlueStacks deletes its own, in its manager."""
    return choice.mode in ("ldplayer", "mumu") and choice.key != "0"


def delete_instance(cfg: DeviceConfig, run=None, close: bool = False, sleep=time.sleep) -> None:
    """Delete the emulator instance ``cfg`` plays, and everything in it. A
    running one is closed first with ``close``, else :class:`StillOpen` is raised."""
    from farmbot import ldplayer, mumu

    if cfg.mode == "ldplayer":
        run = run or ldplayer._run
        install = ldplayer.pick_install(cfg, ldplayer.find_installs())
        if install is None:
            raise DeviceError("LDPlayer was not found")
        inst = next((i for i in ldplayer.instances(install, run) if i.index == cfg.ldplayer_index), None)
        if inst is None:
            raise DeviceError(f"{install.name} has no instance {cfg.ldplayer_index + 1}")
        if inst.index == 0:
            raise DeviceError(f"{install.name}'s first instance is the emulator's own: it can't be deleted")
        if inst.running:
            if not close:
                raise StillOpen(inst.title)
            run([str(install.console), "quit", "--index", str(inst.index)])
            _wait_closed(lambda: any(i.running for i in ldplayer.instances(install, run) if i.index == inst.index),
                         sleep)
        log.warning("Deleting %s's instance %s (instance %d)", install.name, inst.title, inst.index + 1)
        run([str(install.console), "remove", "--index", str(inst.index)])
        if any(i.index == inst.index for i in ldplayer.instances(install, run)):
            raise DeviceError(f"{install.name} didn't delete {inst.title}")
        return
    if cfg.mode == "mumu":
        run = run or mumu._run
        install = mumu.find_install()
        if install is None:
            raise DeviceError("MuMu Player was not found")
        inst = next((i for i in mumu.instances(install, run) if i.index == cfg.mumu_index), None)
        if inst is None:
            raise DeviceError(f"MuMu Player has no instance {cfg.mumu_index + 1}")
        if inst.index == 0:
            raise DeviceError("MuMu Player's first instance is the emulator's own: it can't be deleted")
        if inst.started:
            if not close:
                raise StillOpen(inst.name or f"MuMu instance {inst.index + 1}")
            run([str(install.manager), "control", "-v", str(inst.index), "shutdown"])
            _wait_closed(lambda: any(i.started for i in mumu.instances(install, run) if i.index == inst.index),
                         sleep)
        log.warning("Deleting MuMu instance %d (%s)", inst.index + 1, inst.name)
        run([str(install.manager), "delete", "-v", str(inst.index)])
        if any(i.index == inst.index for i in mumu.instances(install, run)):
            raise DeviceError(f"MuMu Player didn't delete instance {inst.index + 1}")
        return
    raise DeviceError("BlueStacks deletes its instances in its Multi-Instance Manager")


def instance_title(text: str) -> str:
    """A label's instance name without its number ("1: LDPlayer-1" -> "LDPlayer-1")."""
    return re.sub(r"^\d+:\s*", "", text)
