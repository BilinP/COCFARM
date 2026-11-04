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
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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
                found += [Choice(str(i.index), f"{i.index}: {i.title}", "ldplayer", install.name, i.port)
                          for i in ldplayer.instances(install)]
            except DeviceError as exc:
                log.warning("Couldn't list %s's instances: %s", install.name, exc)
    if mode in (None, "mumu"):
        install = mumu.find_install()
        if install is not None:
            try:
                found += [Choice(str(i.index), f"{i.index}: {i.name}", "mumu", "", i.adb_port)
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


def farm_playing(mode: str) -> int | None:
    """Another farm (not this thread's) playing on an emulator of this kind."""
    me = current() or 1
    return next((n for n, cfg in list(active.items()) if n != me and cfg.mode == mode), None)


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


def add(device: DeviceConfig | str, source: int = 1, root: Path | None = None) -> int:
    """A new farm playing ``device``'s emulator instance (or instance ``key``
    of farm ``source``'s emulator), with farm ``source``'s settings.
    Returns its number."""
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
    else:  # its emulator instance, with the source's display, cores and memory
        base, config.device = config.device, copy.deepcopy(device)
        for name in ("display_width", "display_height", "display_dpi", "cpus", "memory", "set_display",
                     "set_resources", "enable_adb", "launch_emulator", "launch_timeout", "package", *SIZES):
            setattr(config.device, name, copy.deepcopy(getattr(base, name)))
    for other in taken:
        if same_instance(device_of(other, root), config.device):
            raise ValueError(f"farm {other} plays {describe(config.device)} already")
    config.profiles.active = ""  # its account is picked when the game shows it
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


def create_instance(cfg: DeviceConfig, run=None, close: bool = False, sleep=time.sleep) -> str:
    """Make a new instance of ``cfg``'s emulator, a copy of the one ``cfg``
    plays (with the game installed). Returns the new instance's key. A
    running one can't be copied: with ``close`` it's closed first, else
    :class:`StillOpen` is raised."""
    from farmbot import ldplayer, mumu

    copying = _copying if run is None else None  # tests pass their own run for everything
    if cfg.mode == "ldplayer":
        run = run or ldplayer._run
        install = ldplayer.pick_install(cfg, ldplayer.find_installs())
        if install is None:
            raise DeviceError("LDPlayer was not found")
        before = ldplayer.instances(install, run)
        source = next((i for i in before if i.index == cfg.ldplayer_index), None)
        if source is None:
            raise DeviceError(f"{install.name} has no instance {cfg.ldplayer_index + 1}")
        if source.running:
            if not close:
                raise StillOpen(source.title)
            log.info("Closing %s to copy it", source.title)
            run([str(install.console), "quit", "--index", str(source.index)])
            _wait_closed(lambda: any(i.running for i in ldplayer.instances(install, run) if i.index == source.index),
                         sleep)
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
