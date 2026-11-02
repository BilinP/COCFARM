"""COCFARM in Windows' Start menu (so search finds it) and Installed apps.

COCFARM is a folder you unzip, not an installed program, so nothing puts it
in either. The built COCFARM.exe adds a Start menu shortcut and an Installed
apps entry the first time it starts, remembers that in update.json, and only
does it again if the folder is moved or COCFARM is updated (for the version
Installed apps shows). A shortcut you delete stays deleted.

Uninstall in Installed apps runs ``COCFARM.exe uninstall``: after asking,
it removes the shortcut, the entry and the whole COCFARM folder.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from farmbot import __version__, update

log = logging.getLogger(__name__)

NAME = "COCFARM.lnk"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\COCFARM"  # per user: no admin needed

# Paths come in through the environment, so no quoting can go wrong.
SHORTCUT = """
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:COCFARM_LINK)
if ($link.TargetPath -eq $env:COCFARM_EXE -and $link.WorkingDirectory -eq $env:COCFARM_DIR) { exit 0 }
$link.TargetPath = $env:COCFARM_EXE
$link.WorkingDirectory = $env:COCFARM_DIR
$link.IconLocation = "$env:COCFARM_EXE,0"
$link.Description = "COCFARM"
$link.Save()
"""

UNINSTALLER = r"""
param([int]$ParentId, [string]$App, [string]$Exe, [string]$Link, [string]$Key)
# Written by COCFARM.exe uninstall; deletes itself.
Add-Type -AssemblyName System.Windows.Forms
function Ask($text, $buttons, $icon) {
    [System.Windows.Forms.MessageBox]::Show($text, "Uninstall COCFARM", $buttons, $icon,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2, [System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly)
}
$sure = Ask "Remove COCFARM?`n`nThis deletes the COCFARM folder, with your settings, logs and screenshots:`n$App" "YesNo" "Warning"
if ($sure -ne "Yes") { Remove-Item $PSCommandPath -Force; exit }
try { Wait-Process -Id $ParentId -Timeout 60 -ErrorAction Stop } catch {}
Get-Process -Name COCFARM -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Exe } | Stop-Process -Force
Start-Sleep -Milliseconds 500
# Only this copy's shortcut and entry: another COCFARM folder may have taken them over.
$shell = New-Object -ComObject WScript.Shell
if ((Test-Path $Link) -and $shell.CreateShortcut($Link).TargetPath -eq $Exe) { Remove-Item $Link -Force }
$entry = "HKCU:\$Key"
if ((Test-Path $entry) -and (Get-ItemProperty $entry).InstallLocation -eq $App) { Remove-Item $entry -Recurse -Force }
# Windows or an antivirus can hold a file for a moment after COCFARM closes.
for ($i = 1; $i -le 20 -and (Test-Path $App); $i++) {
    Remove-Item $App -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $App) { Start-Sleep -Milliseconds 500 }
}
if (Test-Path $App) {
    Ask "Some of COCFARM's files couldn't be removed. Delete the folder yourself:`n$App" "OK" "Information" | Out-Null
    Start-Process explorer.exe $App
} else {
    Ask "COCFARM was removed." "OK" "Information" | Out-Null
}
Remove-Item $PSCommandPath -Force
"""


def start_menu() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if appdata else None


def built() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def ensure(exe: Path | None = None, folder: Path | None = None, state_path: Path | None = None,
           shortcut: Callable[[Path, Path], bool] | None = None,
           listing: Callable[[Path], bool] | None = None, version: str = __version__) -> bool:
    """Add the Start menu shortcut and Installed apps entry for the built
    COCFARM.exe in the background, once for each folder and version.
    False when there's nothing to do: already done, run from source, or
    not on Windows."""
    if exe is None:
        if not built():
            return False
        exe = Path(sys.executable)
    folder = folder or start_menu()
    if folder is None:
        return False
    exe, state_path = exe.resolve(), state_path or update.STATE
    done = {"exe": str(exe), "version": version}
    if update._load(state_path).get("installed") == done:
        return False

    def add() -> None:
        made = (shortcut or _shortcut)(exe, folder / NAME)
        listed = (listing or _listing)(exe)
        if made and listed:
            state = update._load(state_path)
            state["installed"] = done
            update._save(state_path, state)

    threading.Thread(target=add, daemon=True, name="installed").start()
    return True


def _shortcut(exe: Path, link: Path) -> bool:
    env = {**os.environ, "COCFARM_LINK": str(link), "COCFARM_EXE": str(exe), "COCFARM_DIR": str(exe.parent)}
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-WindowStyle", "Hidden", "-Command", SHORTCUT]
    try:
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Couldn't make the Start menu shortcut: %s", exc)
        return False
    if done.returncode:
        log.debug("Couldn't make the Start menu shortcut: %s", done.stderr.strip())
    return done.returncode == 0


def entry(exe: Path, version: str = __version__) -> dict[str, str | int]:
    """What Installed apps shows for COCFARM, and how it uninstalls."""
    return {
        "DisplayName": "COCFARM",
        "DisplayVersion": version,
        "Publisher": "COCFARM",
        "DisplayIcon": f"{exe},0",
        "InstallLocation": str(exe.parent),
        "UninstallString": f'"{exe}" uninstall',
        "NoModify": 1,
        "NoRepair": 1,
    }


def _listing(exe: Path) -> bool:
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            for name, value in entry(exe).items():
                kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, kind, value)
    except OSError as exc:
        log.debug("Couldn't add COCFARM to Installed apps: %s", exc)
        return False
    return True


def safe_to_remove(app: Path) -> bool:
    """Only a COCFARM folder: never a drive or one of your own folders, if
    COCFARM.exe was put straight in one."""
    app = app.resolve()
    home = Path.home().resolve()
    mine = {home, *(home / n for n in ("Desktop", "Downloads", "Documents", "OneDrive"))}
    return (app / update.EXE).is_file() and app.parent != app and app not in mine and app != home.parent


def uninstall(exe: Path | None = None, launch: Callable[[list[str]], object] | None = None) -> int:
    """``COCFARM.exe uninstall`` (Installed apps' Uninstall): start the
    uninstaller, which asks first, then waits for this to quit."""
    if exe is None and not built():
        print("error: only the built COCFARM.exe uninstalls itself", file=sys.stderr)
        return 1
    exe = (exe or Path(sys.executable)).resolve()
    app = exe.parent
    if not safe_to_remove(app):
        print(f"error: {app} doesn't look like a COCFARM folder; delete it yourself", file=sys.stderr)
        return 1
    # Outside the folder it deletes.
    script = Path(tempfile.gettempdir()) / "cocfarm-uninstall.ps1"
    script.write_text(UNINSTALLER, encoding="utf-8-sig")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(script), "-ParentId", str(os.getpid()), "-App", str(app), "-Exe", str(exe),
               "-Link", str((start_menu() or app) / NAME), "-Key", KEY]
    if launch:
        launch(command)
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(command, cwd=tempfile.gettempdir(), close_fds=True,
                         creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        subprocess.Popen(command, cwd=tempfile.gettempdir(), creationflags=flags, close_fds=True)
    return 0
