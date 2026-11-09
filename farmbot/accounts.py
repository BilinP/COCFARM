"""Settings per Clash of Clans account, picked by the player name.

The village shows the player's name at the top left (the same spot in both
villages). A profile keeps a picture of it, the white letters cut out of the
scenery behind them, and accounts are told apart by that picture. A new
account's profile is named after it: Windows' own OCR reads the name once
(``ocr.ps1``). Each profile is ``profiles/<name>.toml`` (a whole settings
file) plus ``<name>.png`` next to the main settings file. What belongs to
the PC rather than the account (the emulator, the screen regions,
notifications, the window's look) stays in the main file.
"""

from __future__ import annotations

import copy
import logging
import re
import subprocess
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from farmbot import imagefile
from farmbot.config import Config, load_config, save_config

log = logging.getLogger(__name__)

BASE_HEIGHT = 672  # names are compared at this screen height (the HUD scales with it)
NAME_AREA = (92, 38, 220, 26)  # x, y, w, h at BASE_HEIGHT: just right of the level badge
WHITE_SAT, WHITE_VAL = 60, 200  # the letters: near white
LEAST_PIXELS = 25  # fewer letter pixels than this: no name in view (a window over it)
MOST_FILL = 0.7  # letters leave gaps; a solid block is something else
START_SLACK = 12  # the name starts at the left of the area
WORD_GAP = 9  # wider gaps end the name (scenery further right)
SAME = 0.85  # this alike: the same name (one letter apart in a short name is about 0.7-0.9)
DIFFERENT = 0.7  # less alike than this: another name (in between: not sure)

OCR_SCRIPT = Path(__file__).parent / "ocr.ps1"
OCR_ZOOM = 4  # the letters are about 12 px tall: Windows reads them better bigger
PICTURE_ZOOM = 3  # (the name as it shows: read best a little less big)
NAME_ROOM = 12  # px either side of the name in its picture
OCR_TIMEOUT = 30.0

# Kept in the main settings file, shared by every profile.
SHARED_SECTIONS = ("device", "regions", "notify", "profiles")
SHARED_BOT = ("theme", "templates", "threshold", "update_repo", "check_updates")


def name_mask(screen: np.ndarray) -> np.ndarray | None:
    """The player name's letters as a 0/255 mask trimmed to them, or None
    when no name shows there."""
    found = _name(screen)
    return found[0] if found else None


def name_picture(screen: np.ndarray) -> np.ndarray | None:
    """The player name as it shows (grey, a little room around it): what
    the name is read from. The mask loses thick letters' insides (a W or
    an H reads as something else); this doesn't."""
    found = _name(screen)
    return found[1] if found else None


def _name(screen: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    scale = screen.shape[0] / BASE_HEIGHT
    x, y, w, h = (round(v * scale) for v in NAME_AREA)
    area = screen[y:y + h, x:x + w]
    if area.size == 0:
        return None
    if scale != 1:
        area = cv2.resize(area, (NAME_AREA[2], NAME_AREA[3]), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 1] < WHITE_SAT) & (hsv[..., 2] > WHITE_VAL)).astype(np.uint8) * 255
    columns = np.flatnonzero(mask.any(axis=0))
    if not len(columns) or columns[0] > START_SLACK:
        return None
    end = columns[0]
    for col in columns[1:]:
        if col - end > WORD_GAP:
            break
        end = col
    left, right = columns[0], end + 1
    mask = mask[:, left:right]
    rows = np.flatnonzero(mask.any(axis=1))
    if rows[0] == 0 or rows[-1] == mask.shape[0] - 1:
        return None  # running off the area: a window or a white wall, not one line of letters
    mask = mask[rows[0]:rows[-1] + 1]
    if not LEAST_PIXELS <= np.count_nonzero(mask) <= MOST_FILL * mask.size:
        return None
    grey = cv2.cvtColor(area, cv2.COLOR_BGR2GRAY)[:, max(left - NAME_ROOM, 0):right + NAME_ROOM]
    return mask, grey


def likeness(a: np.ndarray, b: np.ndarray) -> float:
    """0-1: how alike two name masks are: the letters' overlap (shared over
    either's), at the best of a pixel's shift either way."""
    if abs(a.shape[1] - b.shape[1]) > max(3, 0.08 * max(a.shape[1], b.shape[1])):
        return 0.0
    if abs(a.shape[0] - b.shape[0]) > 2:
        return 0.0
    # Softened edges: a letter's outline a pixel wider (another screen
    # size's resampling) costs little.
    h, w = max(a.shape[0], b.shape[0]) + 2, max(a.shape[1], b.shape[1]) + 2
    fixed = np.zeros((h, w), np.float32)
    fixed[1:1 + a.shape[0], 1:1 + a.shape[1]] = a > 0
    fixed = cv2.GaussianBlur(fixed, (3, 3), 0)
    best = 0.0
    for dy in range(3):
        for dx in range(3):
            moved = np.zeros((h, w), np.float32)
            moved[dy:dy + b.shape[0], dx:dx + b.shape[1]] = b > 0
            moved = cv2.GaussianBlur(moved, (3, 3), 0)
            best = max(best, float(np.minimum(fixed, moved).sum() / max(1e-6, np.maximum(fixed, moved).sum())))
    return best


def namer_for(picture: np.ndarray | None) -> Callable[[np.ndarray], str]:
    """Reads a name: from ``picture`` (:func:`name_picture`), else (or if
    that reads nothing) from the mask it's given."""
    def read(mask: np.ndarray) -> str:
        return (read_name(picture) if picture is not None else "") or read_name(mask)
    return read


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")


def read_name(mask: np.ndarray) -> str:
    """The player name in ``mask`` (or, better, its :func:`name_picture`),
    read by Windows' OCR ("" if it can't: unusual letters, or not Windows
    10 or later)."""
    if len(np.unique(mask)) > 2:  # the name as it shows
        picture = cv2.resize(mask, None, fx=PICTURE_ZOOM, fy=PICTURE_ZOOM, interpolation=cv2.INTER_CUBIC)
        picture = cv2.copyMakeBorder(picture, 30, 30, 30, 30, cv2.BORDER_REPLICATE)
    else:
        picture = 255 - cv2.resize(mask, None, fx=OCR_ZOOM, fy=OCR_ZOOM, interpolation=cv2.INTER_CUBIC)
        picture = cv2.copyMakeBorder(picture, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)  # dark letters on white
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "name.png"
        imagefile.write(str(path), picture)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                 str(OCR_SCRIPT), str(path)],
                capture_output=True, timeout=OCR_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("Couldn't read the player name: %s", exc)
            return ""
    name = " ".join(out.stdout.decode("utf-8", "replace").split())
    log.info("Player name read as %r", name)
    return name if out.returncode == 0 else ""


class Profiles:
    """The profiles kept in ``folder``."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    @classmethod
    def beside(cls, config_path: str | Path) -> Profiles:
        folder = Path(config_path).resolve().parent
        if folder.parent.name == "instances":  # another window's (farmbot/instances.py): they're shared
            folder = folder.parent.parent
        return cls(folder / "profiles")

    def names(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted((p.stem for p in self.folder.glob("*.toml")), key=str.lower)

    def path(self, name: str) -> Path:
        return self.folder / f"{name}.toml"

    def picture_path(self, name: str) -> Path:
        return self.folder / f"{name}.png"

    def village_path(self, name: str) -> Path:
        """The account's village export (``farmbot/townhall.py``)."""
        return self.folder / f"{name}.village.json"

    def mask(self, name: str) -> np.ndarray | None:
        path = self.picture_path(name)
        if not path.is_file():
            return None
        picture = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_GRAYSCALE)
        return picture if picture is not None and picture.size else None

    def match(self, mask: np.ndarray) -> tuple[str | None, float]:
        """The profile whose name picture is most like ``mask``, and how alike."""
        best, score = None, 0.0
        for name in self.names():
            known = self.mask(name)
            if known is None:
                continue
            s = likeness(mask, known)
            if s > score:
                best, score = name, s
        return best, score

    def new_name(self, wanted: str = "") -> str:
        """``wanted`` (or Account 1, 2…), numbered if a profile has it already."""
        taken = {n.lower() for n in self.names()}
        wanted = _safe(wanted)
        if wanted and wanted.lower() not in taken:
            return wanted
        base = wanted or "Account"
        n = 2 if wanted else 1
        while f"{base} {n}".lower() in taken:
            n += 1
        return f"{base} {n}"

    def create(self, name: str, config: Config, mask: np.ndarray | None = None) -> str:
        name = _safe(name) or self.new_name()
        self.folder.mkdir(parents=True, exist_ok=True)
        save_config(config, self.path(name))
        if mask is not None:
            self.set_picture(name, mask)
        log.info("Made profile %s", name)
        return name

    def set_picture(self, name: str, mask: np.ndarray) -> None:
        ok, data = cv2.imencode(".png", mask)
        if ok:
            self.picture_path(name).write_bytes(data.tobytes())

    def load(self, name: str, shared: Config) -> Config:
        """Profile ``name``'s settings, with the PC's own from ``shared``."""
        config = load_config(self.path(name))
        share(shared, config)
        return config

    def save(self, name: str, config: Config) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        save_config(config, self.path(name))

    def copy(self, source: str, target: str, shared: Config) -> None:
        """Give profile ``target`` the settings of ``source``. What's only
        true of ``target``'s account (its storage sizes) stays."""
        settings = self.load(source, shared)
        config = self.load(target, shared)
        storage = config.storage
        take(settings, config)
        config.storage = storage
        self.save(target, config)
        log.info("Copied profile %s's settings to %s", source, target)

    def rename(self, old: str, new: str) -> str:
        new = _safe(new)
        if not new or new.lower() in {n.lower() for n in self.names() if n != old}:
            raise ValueError(f"there's already a profile called {new}" if new else "give it a name")
        for path, renamed in ((self.path(old), self.path(new)), (self.picture_path(old), self.picture_path(new)),
                              (self.village_path(old), self.village_path(new))):
            if path.exists():
                path.rename(renamed)
        return new

    def delete(self, name: str) -> None:
        for path in (self.path(name), self.picture_path(name), self.village_path(name)):
            path.unlink(missing_ok=True)


def share(source: Config, target: Config) -> None:
    """Copy the PC's own settings (not the account's) from ``source``."""
    for section in SHARED_SECTIONS:
        setattr(target, section, getattr(source, section))
    for key in SHARED_BOT:
        setattr(target.bot, key, getattr(source.bot, key))


def take(source: Config, target: Config) -> None:
    """Put the account's settings of ``source`` into ``target`` in place
    (the bot and the window hold on to ``target``)."""
    kept = {key: getattr(target.bot, key) for key in SHARED_BOT}
    for f in fields(Config):
        if f.name not in SHARED_SECTIONS:
            setattr(target, f.name, copy.deepcopy(getattr(source, f.name)))
    for key, value in kept.items():
        setattr(target.bot, key, value)


def for_account(profiles: Profiles, config: Config, mask: np.ndarray,
                namer: Callable[[np.ndarray], str] = read_name) -> tuple[str | None, bool]:
    """The profile for the account whose name is ``mask``: a known one, or a
    new one named after the account, made from the starred profile's
    settings (``profiles.default``), else from ``config``. The first
    profile keeps its storage sizes (the settings were this account's);
    later ones read their own. (None, False) when it's too close to call.
    Returns (name, made)."""
    name, score = profiles.match(mask)
    if score >= SAME:
        return name, False
    if score >= DIFFERENT:
        log.info("Account name looks a little like %s (%.2f): not switching", name, score)
        return None, False
    starred = config.profiles.default
    if starred and profiles.path(starred).is_file():
        fresh = profiles.load(starred, config)
    else:
        fresh = copy.deepcopy(config)
    fresh.profiles = copy.deepcopy(config.profiles)
    fresh.profiles.active = ""
    new = profiles.new_name(namer(mask))
    if not profiles.names():
        return profiles.create(new, fresh, mask), True
    fresh.storage.gold = fresh.storage.elixir = fresh.storage.dark = 0
    fresh.storage.builder_gold = fresh.storage.builder_elixir = 0
    fresh.storage.read_on = fresh.storage.builder_read_on = ""
    fresh.storage.read = fresh.storage.builder_read = True
    return profiles.create(new, fresh, mask), True


FALLBACK_NAME = re.compile(r"Account \d+")  # a profile named while the account's name couldn't be read


def read_later(profiles: Profiles, name: str, mask: np.ndarray,
               namer: Callable[[np.ndarray], str] = read_name) -> str | None:
    """Profile ``name`` is still "Account 1" (or 2...): the account's name
    couldn't be read when it was made. Read it again now; the name read,
    if it's one no other profile has, else None."""
    if not FALLBACK_NAME.fullmatch(name):
        return None
    read = _safe(namer(mask))
    if not read or FALLBACK_NAME.fullmatch(read):
        return None
    if read.lower() in {n.lower() for n in profiles.names() if n != name}:
        log.info("Player name read as %s, but another profile has that name: keeping %s", read, name)
        return None
    return read


def load_active(path: str | Path) -> Config:
    """The settings in use: the main file's, or its active profile's."""
    config = load_config(path)
    name = config.profiles.active
    profiles = Profiles.beside(path)
    if name and profiles.path(name).is_file():
        return profiles.load(name, config)
    config.profiles.active = ""
    return config


def save_active(config: Config, path: str | Path) -> None:
    """Save ``config``: the PC's settings to the main file, and the rest to
    the active profile (or, with none, all of it to the main file)."""
    name = config.profiles.active
    if not name:
        save_config(config, path)
        return
    Profiles.beside(path).save(name, config)
    main = load_config(path)
    share(config, main)
    save_config(main, path)
