"""Who gets the onboarding (the tour in ``farmbot/tour.py``): someone who has
just installed COCFARM, in place of the old Setup page.

Who gets it. Only a new install, never someone who downloaded an update:

* When COCFARM's main window opens with no ``farmbot.toml`` and nothing
  else a player keeps (``attacks.csv``, ``update.json``) beside it, it's a
  new install, and ``onboarding.json`` is written there saying the tour
  isn't done. That is decided before anything writes the settings file.
  (An ``update.json`` holding only the Start menu note that
  ``installed.ensure`` writes as the exe starts doesn't count: a new
  install has that too.)
* Nor is it a new install when Windows' Installed apps lists COCFARM in
  another folder: a player who downloaded the new version by hand and
  unzipped it somewhere else.
* The tour shows while ``onboarding.json`` says it isn't done, so quitting
  halfway brings it back next time. Finishing or skipping it marks it done.
* An update keeps ``farmbot.toml`` (see ``update.KEEP``) and never had an
  ``onboarding.json``: nothing marks the tour as due, so it never shows.
  (The opposite, a "done" marker whose absence shows the tour, would show it
  to every player who updates.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE = "onboarding.json"
# Kept by a player who has used COCFARM before (and by every update): any of
# them beside the settings file means this isn't a new install.
SAVED = ("farmbot.toml", "attacks.csv", "update.json")


def _state(config_path: Path) -> Path:
    return Path(config_path).with_name(STATE)


def new_install(config_path: str | Path) -> bool:
    """True when nothing of a previous COCFARM is beside ``config_path``
    (the settings file itself included): someone installing it for the
    first time. Ask before the settings file is written."""
    config_path = Path(config_path)
    folder = config_path.parent
    if config_path.exists() or any(_kept(folder / name) for name in SAVED):
        return False
    return not installed_elsewhere(folder)


def _kept(path: Path) -> bool:
    if not path.exists():
        return False
    if path.name != "update.json":
        return True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # there, but unreadable: rather an update than a tour shown to someone who knows COCFARM
    # installed.ensure notes the Start menu shortcut here as the exe starts, before the window opens.
    return not isinstance(state, dict) or bool(set(state) - {"installed"})


def installed_elsewhere(folder: Path) -> bool:
    """True when Installed apps lists a COCFARM in a folder other than
    ``folder`` (see farmbot/installed.py, which lists it)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        from farmbot.installed import KEY

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
    except (OSError, ImportError):
        return False
    try:
        return bool(location) and Path(location).resolve() != Path(folder).resolve()
    except OSError:
        return False


def begin(config_path: str | Path) -> None:
    """Note that this new install's onboarding isn't done yet."""
    _write(config_path, False)


def finish(config_path: str | Path) -> None:
    """The onboarding was finished or skipped: it doesn't show again."""
    _write(config_path, True)


def due(config_path: str | Path) -> bool:
    """True when this install began an onboarding and hasn't finished it."""
    try:
        return json.loads(_state(Path(config_path)).read_text(encoding="utf-8")).get("done") is False
    except (OSError, ValueError, AttributeError):
        return False  # no file (every update), or one that can't be read: never show it


def _write(config_path: str | Path, done: bool) -> None:
    path = _state(Path(config_path))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"done": done}), encoding="utf-8")
    except OSError:
        pass  # (a read-only folder: the tour just shows this once)


def start_if_new(config_path: str | Path) -> bool:
    """At the main window's start, before ``ensure_config``: mark a new
    install's onboarding as due. True when the onboarding should show."""
    if new_install(config_path):
        begin(config_path)
    return due(config_path)
