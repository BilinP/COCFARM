"""Updating COCFARM from its GitHub releases, inside the app.

1. :func:`check` asks GitHub's API for the latest release. Without an
   account GitHub allows 60 API calls an hour per internet connection, so
   the answer is remembered in ``update.json`` for :data:`CHECK_EVERY`, and
   asked again with the last ETag: a "not modified" answer doesn't count
   against the limit. When GitHub says the limit is used up, no call is
   made until the time it gives.
2. :func:`download` fetches ``COCFARM-windows.zip`` from the release (a plain
   file download, not an API call, so no limit), resuming after a dropped
   connection, and checks its size and SHA-256 against what GitHub lists.
3. :func:`unpack` extracts it into ``_update/new`` and checks it is COCFARM.
4. :func:`install` starts a small PowerShell script and the app quits. The
   script waits for COCFARM to close, swaps the new program files in (the
   old ones are put back if anything fails), and opens COCFARM again.

Your settings, attack log, screenshots and own button pictures are never
touched: the release zip doesn't contain them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from farmbot import __version__

log = logging.getLogger(__name__)

TIMEOUT = 15
CHECK_EVERY = 3600  # seconds between calls to GitHub's API
MIN_GAP = 60  # "Check now" asks GitHub at most once a minute
ASSET = "COCFARM-windows.zip"
# The Android app has releases of its own, beside Windows' (which GitHub
# marks latest): its update check looks through this many recent releases.
RELEASES_LOOKED_AT = 20
STATE = Path("update.json")
WORK = Path("_update")
EXE = "COCFARM.exe"
CHUNK = 1 << 20
# Never moved or replaced by an update, whatever a release contains.
KEEP = {"farmbot.toml", "attacks.csv", "screens", "logs", "farmbot.log", "update.json", "_update", "templates"}


class UpdateError(Exception):
    """An update couldn't be downloaded or installed (message in plain words)."""


@dataclass
class Release:
    version: str
    url: str  # the release page
    download: str = ""  # the zip (empty: this release has no COCFARM-windows.zip)
    size: int = 0
    sha256: str = ""


@dataclass
class Check:
    release: Release | None  # newer than this copy, or None
    note: str  # what to tell the user


Response = tuple[int, dict[str, str], bytes]


def _get(url: str, headers: dict[str, str]) -> Response:
    """GET ``url``: (status, headers, body). HTTP errors are answers too."""
    request = urllib.request.Request(url, headers={"User-Agent": "COCFARM", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b""


def parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3); "v1.2.3-14" (a build of main) -> (1, 2, 3)."""
    numbers = re.findall(r"\d+", text)
    return tuple(int(n) for n in numbers[:3])


def can_install() -> bool:
    """Only the built COCFARM.exe can replace itself; from source, use git."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError as exc:
        log.debug("Couldn't save %s: %s", path, exc)


def _release(data: dict, repo: str, asset: str = ASSET) -> Release:
    tag = str(data.get("tag_name") or "")
    numbers = parse_version(tag)  # "v0.5.0-10" (a build of main) is COCFARM 0.5.0
    release = Release(".".join(map(str, numbers)) if len(numbers) == 3 else tag.lstrip("v"), str(data.get("html_url") or f"https://github.com/{repo}/releases"))
    for item in data.get("assets") or []:
        if item.get("name") == asset:
            release.download = str(item.get("browser_download_url") or "")
            release.size = int(item.get("size") or 0)
            digest = str(item.get("digest") or "")
            release.sha256 = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
    return release


def _newest_with(data: list, repo: str, asset: str) -> Release | None:
    """The newest published release in ``data`` (GitHub's list, newest
    first) that has ``asset``; ``None`` if none does."""
    for item in data:
        if item.get("draft") or item.get("prerelease"):
            continue
        release = _release(item, repo, asset)
        if release.download:
            return release
    return None


def _header(headers: dict[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def _wait_until(headers: dict[str, str], now: float) -> float:
    """When GitHub says to try again, after a 403/429."""
    retry = _header(headers, "Retry-After")
    if retry.isdigit():
        return now + int(retry)
    if _header(headers, "X-RateLimit-Remaining") == "0" and _header(headers, "X-RateLimit-Reset").isdigit():
        return float(_header(headers, "X-RateLimit-Reset")) + 5
    return now + 3600  # a secondary limit with no time given: GitHub asks for at least a minute; wait an hour


def _at(when: float) -> str:
    return time.strftime("%H:%M", time.localtime(when))


def check(repo: str, current: str = __version__, *, force: bool = False, state_path: Path | None = None,
          now: Callable[[], float] = time.time, get: Callable[[str, dict], Response] | None = None,
          asset: str = ASSET) -> Check:
    """Is a newer release of ``repo`` ("owner/name") out than ``current``?
    (``asset``: the file it downloads, the Windows zip or the Android app.)

    Asks GitHub at most every :data:`CHECK_EVERY` seconds (``force``: at most
    every :data:`MIN_GAP`), never while it says the limit is used up, and
    never fails: offline or any other trouble just leaves the last answer.
    """
    if not repo:
        return Check(None, "Update checks are off.")
    state_path = state_path or STATE
    state = _load(state_path)
    if state.get("repo") != repo or state.get("asset", ASSET) != asset:
        state = {"repo": repo, "asset": asset}
    t = now()
    gap = MIN_GAP if force else CHECK_EVERY
    if t < state.get("wait_until", 0):
        note = f"GitHub asked COCFARM to wait: it checks again after {_at(state['wait_until'])}."
    elif t - state.get("checked", 0) < gap and "checked" in state:
        note = ""
    else:
        # Windows' zip is on the latest release; the Android app is on its own releases.
        url = (f"https://api.github.com/repos/{repo}/releases/latest" if asset == ASSET
               else f"https://api.github.com/repos/{repo}/releases?per_page={RELEASES_LOOKED_AT}")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if state.get("etag") and state.get("latest"):
            headers["If-None-Match"] = state["etag"]
        note = ""
        try:
            status, answer, body = (get or _get)(url, headers)
        except Exception as exc:  # offline, DNS, timeout...
            log.debug("Update check failed: %s", exc)
            return _answer(state, current, "Couldn't reach GitHub. Check your internet connection.")
        if status == 200:
            try:
                data = json.loads(body)
                if isinstance(data, list):
                    found = _newest_with(data, repo, asset)
                    if found is None:
                        state.pop("latest", None)
                    else:
                        state["latest"] = asdict(found)
                else:
                    state["latest"] = asdict(_release(data, repo, asset))
            except (ValueError, TypeError, AttributeError) as exc:
                log.debug("Odd answer from GitHub: %s", exc)
            state["etag"] = _header(answer, "ETag")
            state["checked"] = t
        elif status == 304:  # nothing new, and free
            state["checked"] = t
        elif status in (403, 429):
            state["wait_until"] = _wait_until(answer, t)
            note = f"GitHub asked COCFARM to wait: it checks again after {_at(state['wait_until'])}."
            log.info("GitHub rate limit: next update check after %s", _at(state["wait_until"]))
        elif status == 404:  # no releases yet (or the repository is private)
            state.pop("latest", None)
            state["checked"] = t
        else:
            log.debug("Update check: GitHub answered %s", status)
            note = "GitHub didn't answer properly. Try again later."
        _save(state_path, state)
    return _answer(state, current, note)


def _answer(state: dict, current: str, note: str) -> Check:
    latest = state.get("latest")
    release = Release(**latest) if isinstance(latest, dict) else None
    if release and parse_version(release.version) > parse_version(current):
        return Check(release, note or f"COCFARM {release.version} is out.")
    return Check(None, note or f"You have the newest COCFARM ({current}).")


def download(release: Release, folder: Path | None = None, progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None, opener: Callable[[str, dict], object] | None = None,
             suffix: str = ".zip") -> Path:
    """Download the release's zip (or, with ``suffix`` ".apk", the Android
    app) into ``folder`` and check it. A part download left from last time
    is resumed rather than started over."""
    if not release.download:
        raise UpdateError("This release has no download for this version of COCFARM.")
    folder = folder or WORK
    folder.mkdir(exist_ok=True)
    zip_path = folder / f"COCFARM-{release.version}{suffix}"
    if zip_path.exists() and _matches(zip_path, release):
        return zip_path
    part = zip_path.with_suffix(".part")
    if release.size and shutil.disk_usage(folder).free < release.size * 4:
        raise UpdateError(f"Not enough free disk space: the update needs about {release.size * 4 // 2**20} MB.")
    for attempt in range(4):
        try:
            _fetch(release, part, progress, cancel, opener or _open)
            break
        except (OSError, urllib.error.URLError) as exc:  # a dropped connection: carry on from where it stopped
            if attempt == 3 or (cancel and cancel.is_set()):
                raise UpdateError(f"The download kept failing ({exc}). Try again later.") from exc
            log.info("Update download interrupted (%s): resuming", exc)
            time.sleep(2 * (attempt + 1))
    if cancel and cancel.is_set():
        raise UpdateError("Download cancelled.")
    if not _matches(part, release):
        part.unlink(missing_ok=True)
        raise UpdateError("The download was damaged (its checksum doesn't match). Try again.")
    part.replace(zip_path)
    return zip_path


def _open(url: str, headers: dict[str, str]):
    request = urllib.request.Request(url, headers={"User-Agent": "COCFARM", "Accept": "application/octet-stream",
                                                   **headers})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def _fetch(release: Release, part: Path, progress, cancel, open_) -> None:
    have = part.stat().st_size if part.exists() else 0
    if release.size and have >= release.size:
        have = 0
        part.unlink()
    headers = {"Range": f"bytes={have}-"} if have else {}
    with open_(release.download, headers) as response:
        if have and getattr(response, "status", 200) != 206:  # the server sent the whole file again
            have = 0
        total = release.size or have + int(_header(dict(response.headers), "Content-Length") or 0)
        with part.open("ab" if have else "wb") as out:
            done = have
            while not (cancel and cancel.is_set()):
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)


def _matches(path: Path, release: Release) -> bool:
    if release.size and path.stat().st_size != release.size:
        return False
    if release.sha256:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest() == release.sha256
    return True


def unpack(zip_path: Path, folder: Path | None = None) -> Path:
    """Extract the zip into ``folder/new`` and return its COCFARM folder."""
    target = (folder or WORK) / "new"
    shutil.rmtree(target, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            for name in names:
                parts = Path(name).parts
                if not parts or parts[0] != "COCFARM" or ".." in parts or Path(name).is_absolute() or ":" in name:
                    raise UpdateError(f"The download has an unexpected file in it ({name}).")
            if "COCFARM/COCFARM.exe" not in names:
                raise UpdateError("The download doesn't contain COCFARM.exe.")
            archive.extractall(target)
    except zipfile.BadZipFile as exc:
        zip_path.unlink(missing_ok=True)
        raise UpdateError("The download isn't a valid zip. Try again.") from exc
    return target / "COCFARM"


INSTALLER = r"""
param([int]$ParentId, [string]$App, [string]$New, [int]$Tries = 20)
# Written by COCFARM to install an update; deleted by the next update.
$ErrorActionPreference = "Stop"
$work = Join-Path $App "_update"
$old = Join-Path $work "old"
$log = Join-Path $work "install.log"
$keep = @(@KEEP@)
function Say($text) { Add-Content -Path $log -Value "$(Get-Date -Format s)  $text" }
function Retry([scriptblock]$step) {
    # Windows or an antivirus can hold a file for a moment after COCFARM closes.
    for ($i = 1; ; $i++) { try { & $step; return } catch { if ($i -ge $Tries) { throw }; Start-Sleep -Milliseconds 500 } }
}
Say "Installing the update from $New"
try { Wait-Process -Id $ParentId -Timeout 120 -ErrorAction Stop } catch {}
Start-Sleep -Milliseconds 500
if (Test-Path $old) { Remove-Item $old -Recurse -Force }
New-Item -ItemType Directory $old | Out-Null
$moved = @(); $placed = @()
try {
    foreach ($item in Get-ChildItem $New -Force) {
        if ($keep -contains $item.Name) { continue }
        $target = Join-Path $App $item.Name
        if (Test-Path $target) { Retry { Move-Item $target $old }; $moved += $item.Name }
        Retry { Move-Item $item.FullName $target }; $placed += $item.Name
    }
    # Shipped pictures are refreshed; pictures you added stay.
    $pictures = Join-Path $New "templates"
    if (Test-Path $pictures) { Copy-Item (Join-Path $pictures "*") (Join-Path $App "templates") -Recurse -Force }
    Say "Updated"
} catch {
    Say "Failed: $_. Putting the old version back."
    foreach ($name in $placed) { Remove-Item (Join-Path $App $name) -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($name in $moved) { Move-Item (Join-Path $old $name) (Join-Path $App $name) -Force }
}
Start-Process (Join-Path $App "@EXE@") -WorkingDirectory $App
"""


def install(staged: Path, app: Path | None = None, version: str = "", state_path: Path | None = None,
            launch: Callable[[list[str]], object] | None = None) -> None:
    """Start the installer for the unpacked ``staged`` folder. The caller
    must quit COCFARM straight after, so its files can be replaced."""
    app = (app or Path(sys.executable).parent).resolve()
    script = app / "_update" / "install.ps1"  # next to the program, wherever COCFARM was started from
    script.parent.mkdir(exist_ok=True)
    keep = ", ".join(f'"{name}"' for name in sorted(KEEP))
    script.write_text(INSTALLER.replace("@KEEP@", keep).replace("@EXE@", EXE), encoding="utf-8-sig")
    state_path = state_path or STATE
    state = _load(state_path)
    state["installing"] = {"from": __version__, "to": version}
    _save(state_path, state)
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(script), "-ParentId", str(os.getpid()), "-App", str(app), "-New", str(staged.resolve())]
    if launch:
        launch(command)
        return
    # A hidden console (PowerShell quits at once without one), in its own group so it outlives COCFARM;
    # out of COCFARM's job too when Windows allows, or closing COCFARM could end it.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(command, cwd=str(app), close_fds=True,
                         creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        subprocess.Popen(command, cwd=str(app), creationflags=flags, close_fds=True)


def finished_install(state_path: Path | None = None, folder: Path | None = None,
                     current: str = __version__) -> str | None:
    """After an update: what to tell the user (once), and tidy up."""
    state_path, folder = state_path or STATE, folder or WORK
    state = _load(state_path)
    installing = state.pop("installing", None)
    if not installing:
        return None
    _save(state_path, state)
    for leftover in ("new", "old"):
        shutil.rmtree(folder / leftover, ignore_errors=True)
    for zip_path in folder.glob("COCFARM-*.zip"):
        zip_path.unlink(missing_ok=True)
    if installing.get("to") and parse_version(current) >= parse_version(installing["to"]):
        return f"Updated to COCFARM {current}."
    return f"The update didn't install, so you still have COCFARM {current}. Details: {folder / 'install.log'}"
