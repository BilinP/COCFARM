"""Several COCFARM windows at once, one per emulator instance.

Each extra instance is a folder, ``instances/<name>/`` in COCFARM's folder,
with its own settings (``farmbot.toml``: above all, which emulator instance
it plays on), attack history, logs and screenshots. Its window is a COCFARM
process started with ``--instance <name>``, working in that folder. What
belongs to the whole PC stays in COCFARM's own folder and is shared: the
button pictures (``templates/``), the account profiles (``profiles/``: an
account keeps its settings whichever emulator it plays on), and updates.

Two things are locked, so instances don't trip over each other: a window
per instance (the same one can't open twice), and the emulator instance a
window plays on (two windows can't drive one emulator). The locks are held
by open files, so the system frees them when a window closes or crashes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from farmbot.device import DeviceError

log = logging.getLogger(__name__)

FOLDER = "instances"
LOCKS = ".locks"
SETTINGS = "farmbot.toml"
MAIN = ""  # the main window: COCFARM's own folder

APP_DIR = Path.cwd()  # COCFARM's folder; enter() keeps it when moving into an instance's
current = MAIN  # the instance this process is ("" = the main window)
_held: dict[str, object] = {}  # lock name -> its open file


class InstanceError(DeviceError):
    """Something about the instances that the player should hear about (a
    DeviceError, as the emulator being taken is one)."""


def safe_name(name: str) -> str:
    """``name`` as a folder name: letters, digits, spaces, - and _."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w \-]", "", name)).strip()[:40]


def folder(name: str) -> Path:
    return APP_DIR / FOLDER / name if name else APP_DIR


def names() -> list[str]:
    """The instances made so far, by name."""
    root = APP_DIR / FOLDER
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.lower)


def create(name: str) -> str:
    """Make instance ``name``, starting from the main window's settings
    (pick its emulator instance in its own window). Returns its name."""
    name = safe_name(name)
    if not name:
        raise InstanceError("Give the instance a name.")
    if name.lower() in {n.lower() for n in names()}:
        raise InstanceError(f"There's already an instance called {name}.")
    target = folder(name)
    target.mkdir(parents=True)
    main = APP_DIR / SETTINGS
    if main.is_file():
        shutil.copy(main, target / SETTINGS)
    log.info("Made instance %s", name)
    return name


def app_path(path: str | Path) -> Path:
    """In an instance, a path given relative to COCFARM's own folder (shared
    by every instance, such as the pictures' "templates") made absolute;
    otherwise ``path`` as it is."""
    path = Path(path)
    return APP_DIR / path if current and not path.is_absolute() else path


def profiles_folder() -> Path:
    """Where the account profiles are: COCFARM's own folder, for every instance."""
    return APP_DIR / "profiles"


# ------------------------------------------------------------------ locks
def _lock_path(key: str) -> Path:
    return APP_DIR / FOLDER / LOCKS / f"{re.sub(r'[^A-Za-z0-9._-]', '_', key)}.lock"


def _try_lock(path: Path):
    """The open, locked file, or None if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release(key: str) -> None:
    handle = _held.pop(key, None)
    if handle is not None:
        try:
            handle.close()  # closing frees the lock
        except OSError:
            pass


def _owner(key: str) -> str:
    try:
        return _lock_path(key).with_suffix(".owner").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _claim(key: str, owner: str) -> bool:
    """Hold lock ``key`` for this process. False if another process has it."""
    if key in _held:
        return True
    handle = _try_lock(_lock_path(key))
    if handle is None:
        return False
    _held[key] = handle
    try:
        _lock_path(key).with_suffix(".owner").write_text(owner, encoding="utf-8")
    except OSError:
        pass
    return True


def label(name: str) -> str:
    return name or "Main"


def window_key(name: str) -> str:
    return f"window-{name or 'main'}"


def is_open(name: str) -> bool:
    """Whether a window has instance ``name`` open (this process's counts)."""
    key = window_key(name)
    if key in _held:
        return True
    handle = _try_lock(_lock_path(key))
    if handle is None:
        return True
    handle.close()
    return False


def others_open() -> list[str]:
    """The other COCFARM windows open now, by name ("Main" for the main one)."""
    return [label(n) for n in [MAIN, *names()] if n != current and is_open(n)]


def enter(name: str, window: bool = True) -> None:
    """Become instance ``name`` ("" = the main window): for an instance,
    work in its folder; with ``window`` (the window, or a run without it),
    hold its window lock too. Raises InstanceError if a window has it open
    already."""
    global current
    name = safe_name(name)
    if name and not folder(name).is_dir():
        raise InstanceError(f"There's no instance called {name}.")
    if window and not _claim(window_key(name), label(name)):
        raise InstanceError(f"COCFARM's {label(name)} window is open already, or it's farming without one.")
    current = name
    if name:
        os.chdir(folder(name))


def device_key(cfg) -> str:
    """Which emulator instance ``cfg`` (a DeviceConfig) plays on."""
    if cfg.mode == "bluestacks":
        return f"bluestacks-{(cfg.bluestacks_instance or 'main').lower()}"
    if cfg.mode == "ldplayer":
        return f"ldplayer-{cfg.ldplayer_index}"
    if cfg.mode == "mumu":
        return f"mumu-{cfg.mumu_index}"
    return f"device-{cfg.serial.lower()}"


def claim_device(cfg) -> None:
    """Hold the emulator instance ``cfg`` plays on for this window (letting
    go of the one it played on before). Raises InstanceError if another
    window plays on it."""
    key = "device-" + device_key(cfg)
    if not _claim(key, label(current)):
        owner = _owner(key) or "another"
        raise InstanceError(f"COCFARM's {owner} window is already playing on this emulator instance. Pick "
                            "another instance on the Setup page, or close that window.")
    for other in [k for k in _held if k.startswith("device-") and k != key]:
        _release(other)


# ------------------------------------------------------------------ windows
def launch(name: str) -> None:
    """Open instance ``name``'s window, as a COCFARM process of its own."""
    if getattr(sys, "frozen", False):  # COCFARM.exe
        command = [sys.executable, "--instance", name, "gui"]
    else:
        command = [sys.executable, "-m", "farmbot", "--instance", name, "gui"]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, cwd=str(APP_DIR), creationflags=flags, close_fds=True)
    log.info("Opened instance %s", name)
