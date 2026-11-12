"""Screenshots made safe to share in a bug report: what names a player or a
clan blacked out, on battle screens (attack) and the village (defense).

Blocked by itself:
- the top left, where the game shows a player's name and clan (yours in the
  village, the defender's in a battle, a war's and a result screen's too),
  with the clan's badge. The whole corner goes, whatever screen it is: a
  screen not recognised mustn't keep a name. (The loot read there is in the
  log.)
- clan chat, when it's open (its messages, members' names, the clan's tag);
- on a clan war's map: both clans' names at the top, and every piece of text
  under them (the players' names over their bases, the base's panel), each in
  its own box, so the map can still be made out (blurred whole if Windows'
  OCR isn't there to find the text);
- the Clan Castle, which flies the clan's badge and name, when there's a
  picture of it to find it by: the pictures taught for war and ranked attacks
  (Advanced → Pictures → Clan Castle, one per level), and any kept in
  redact/clan_castle. Without one it can't be found reliably (it changes with
  every level, and its badge with every clan), so the user is told to block
  it by hand.

Anything else can be blocked by hand (``extra``). The original files are
never changed: what's shared is the blocked copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from farmbot import imagefile
from farmbot.geometry import Rect
from farmbot.vision import match_template

BASE_HEIGHT = 920  # the corner below is measured on a 1177 x 920 screen; the HUD scales with the height
# The player's or the defender's name, clan and badge: the village's sits lower
# (beside the Town Hall icon), a battle's higher (beside the clan badge).
NAME_CORNER = (0, 0, 290, 116)
CHAT_CLOSE = "buttons/close_chat"  # the tab on the open chat's right edge
CLAN_CASTLE = "buildings/clan_castle"  # an enemy's, cut for attacks (just the building, one per level)
OWN_CASTLE = "redact/clan_castle"  # the player's own, with its badge and name (kept out of git: personal)
CASTLE_LIKENESS = 0.8
SIZES = (0.8, 0.9, 1.0, 1.1, 1.25)  # pictures are cut on one screen size, error screens may be another
CHAT_LIKENESS = 0.85
WAR_MAP = "war/return_home"  # "Return Home", bottom left of a clan war's map
WAR_LIKENESS = 0.85
WAR_BANNER = (275, 20, 640, 148)  # both clans' names and badges, top middle
WAR_NAMES = (150, 148, 880, 640)  # where the bases and their players' names are (not the buttons at the sides)
WAR_TEXT = (130, 1035)  # left and right of where a war map's text is all blocked (not the buttons at the sides)
TEXT_ROOM = 4  # px around each piece of text blocked
OCR_LINES = Path(__file__).parent / "ocr_lines.ps1"
OCR_TIMEOUT = 30
BLURRED = ("war map",)  # blurred, not blacked out: a war map's names, when its text can't be read
FILL = (32, 32, 32)  # blocked areas: dark grey, so it's plain something was taken out
KEEP_DAYS = 7
NAME = re.compile(r"(\d{8})-(\d{6})\.png$")  # the bot's own error screens: 20261112-031838.png


@dataclass
class Blocked:
    """A screenshot with what's personal blacked out."""

    image: np.ndarray
    areas: list[tuple[str, Rect]] = field(default_factory=list)  # what was blocked, and where
    castle: bool = False  # a Clan Castle was found (and blocked)
    chat: bool = False  # clan chat was open (and blocked)
    war: bool = False  # a clan war's map (its names blocked)

    def summary(self) -> str:
        return describe(self.areas)[0]


# What each box covers, in words: as a box's label in the editor, and in the line saying what's blocked.
LABELS = {"name and clan": "Name and clan", "clan chat": "Clan chat", "war clans": "War clans",
          "war names": "Name", "war map": "War names", "Clan Castle": "Clan Castle", "by hand": "Yours"}
_SAID = {"name and clan": "the name and clan at the top left", "clan chat": "clan chat",
         "war clans": "the war's clan and player names", "war names": "the war's clan and player names",
         "war map": "the war's clan and player names",
         "Clan Castle": "the Clan Castle"}


def describe(areas: list[tuple[str, Rect]], found: list[tuple[str, Rect]] = ()) -> tuple[str, bool]:
    """What ``areas`` block, in a sentence, and whether something may still
    show that shouldn't (the Clan Castle not covered, or a box COCFARM put
    there (one of ``found``) taken off)."""
    kinds = [what for what, _ in areas]
    said: list[str] = []
    for what in kinds:
        if what in _SAID and _SAID[what] not in said:
            said.append(_SAID[what])
    hand = kinds.count("by hand")
    if hand:
        said.append(f"{hand} area{'' if hand == 1 else 's'} you added")
    text = ("Blocked: " + ", ".join(said[:-1]) + (" and " if len(said) > 1 else "") + said[-1] + "."
            if said else "Nothing is blocked.")
    warn = False
    gone = []
    for what, _ in found:
        if what in _SAID and what not in kinds and _SAID[what] not in gone:
            gone.append(_SAID[what])
    if gone:
        text += f" You took the box off {' and '.join(gone)}: it will show."
        warn = True
    if "Clan Castle" not in kinds and not hand:
        text += " The Clan Castle wasn't found: if it shows, drag a box over it before sharing."
        warn = True
    return text, warn


def _scaled(box: tuple[int, int, int, int], height: int) -> Rect:
    s = height / BASE_HEIGHT
    x, y, w, h = box
    return Rect(round(x * s), round(y * s), round(w * s), round(h * s))


def _best(screen: np.ndarray, images: list[np.ndarray], threshold: float) -> list[tuple[float, Rect]]:
    """Where any of ``images`` shows on ``screen``, at any of :data:`SIZES`
    (and those for a smaller screen than the 920-high one most are cut on)."""
    found: list[tuple[float, Rect]] = []
    shrink = screen.shape[0] / BASE_HEIGHT
    sizes = sorted(set(SIZES) | ({round(s * shrink, 3) for s in SIZES} if abs(shrink - 1) > 0.02 else set()))
    for image in images:
        for size in sizes:
            picture = image if size == 1.0 else cv2.resize(image, None, fx=size, fy=size,
                                                            interpolation=cv2.INTER_AREA)
            if picture.shape[0] > screen.shape[0] or picture.shape[1] > screen.shape[1]:
                continue
            for m in match_template(screen, picture, threshold):
                found.append((m.score, Rect(m.x - m.w // 2, m.y - m.h // 2, m.w, m.h)))
    return sorted(found, key=lambda f: -f[0])


def _pictures(library, name: str) -> list[np.ndarray]:
    if library is None:
        return []
    try:
        return [image for _, image in library.get(name)] if library.has(name) else []
    except (OSError, KeyError, ValueError):
        return []


def castles(screen: np.ndarray, library) -> list[Rect]:
    """The Clan Castles on ``screen`` found by the user's pictures of them,
    each box taking in the clan's badge and name over it."""
    boxes: list[Rect] = []
    for name, grow in ((OWN_CASTLE, False), (CLAN_CASTLE, True)):
        for _, r in _best(screen, _pictures(library, name), CASTLE_LIKENESS):
            if any(r.x < b.x + b.w and b.x < r.x + r.w and r.y < b.y + b.h and b.y < r.y + r.h for b in boxes):
                continue  # one already found (at another size, or by another picture)
            if grow:  # just the building: its clan's name shows above it in the village, below it on a war map
                r = Rect(r.x - r.w // 2, r.y - r.h, r.w * 2, r.h * 3)
            else:  # marked with its badge and name: a margin, for a longer name a later day
                r = Rect(r.x - r.w // 5, r.y - r.h // 8, r.w * 7 // 5, r.h * 5 // 4)
            boxes.append(r)
    return boxes


def chat_edge(screen: np.ndarray, library) -> int | None:
    """The open clan chat's right edge (x), or None when it isn't open."""
    found = _best(screen, _pictures(library, CHAT_CLOSE), CHAT_LIKENESS)
    if not found:
        return None
    _, r = found[0]
    return r.x + r.w if r.x < screen.shape[1] * 0.6 else None  # (the chat opens from the left)


def read_lines(image: np.ndarray, zoom: float) -> list[Rect] | None:
    """Where Windows' OCR reads a line of text on ``image`` (made ``zoom``
    times bigger first: it reads small text better), or None if it can't."""
    import subprocess
    import tempfile

    if not OCR_LINES.exists():
        return None
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "screen.png"
        if not imagefile.write(str(path), cv2.resize(image, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)):
            return None
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                 str(OCR_LINES), str(path)],
                capture_output=True, timeout=OCR_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return None
    if out.returncode != 0:
        return None
    lines = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        try:
            x, y, w, h = (round(int(v) / zoom) for v in parts[:4])
        except ValueError:
            continue
        lines.append(Rect(x, y, w, h))
    return lines


def _light_text(image: np.ndarray) -> np.ndarray:
    """Just the white and pale letters, black on white: how the OCR reads
    the names on a busy map best."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    light = (hsv[..., 2] > 175) & (hsv[..., 1] < 120)
    return cv2.cvtColor(np.where(light, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def _lines_of(mask: np.ndarray, s: float, join: tuple[int, int]) -> list[Rect]:
    """The pieces of ``mask`` shaped like a line of text, once letters
    ``join`` px apart are joined."""
    text = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, join))
    _, _, stats, _ = cv2.connectedComponentsWithStats(text)
    return [Rect(int(x), int(y), int(w), int(h)) for x, y, w, h, a in stats[1:]
            if 7 * s <= h <= 28 * s and 18 * s <= w <= 300 * s and w >= 1.4 * h and a >= 0.2 * w * h]


def text_marks(image: np.ndarray) -> list[Rect]:
    """Letters with a dark outline, lighter than the map around them: the
    labels the OCR can't read (any language, any symbol). White ones (on
    light sand, where they're no lighter than it) and coloured ones (the
    picked base's red name, the panel's yellow one) too."""
    s = image.shape[0] / BASE_HEIGHT
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    v, sat = hsv[..., 2].astype(np.int16), hsv[..., 1]
    around = cv2.blur(hsv[..., 2], (41, 41)).astype(np.int16)  # the map's own brightness, there
    outline = cv2.dilate((v < around - 50).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    lighter = (v > around + 25) & (v > 150) & outline
    white = (v > 205) & (sat < 45) & outline
    # Joined side to side only, too: a name sits right on its base, and joined up and down it's one with it.
    return _lines_of(lighter, s, (11, 5)) + _lines_of(lighter, s, (9, 1)) + _lines_of(white, s, (9, 1))


def _merged(boxes: list[Rect]) -> list[Rect]:
    """``boxes`` with those that touch on one line of text made one (not a
    name and the base under it: just the name stays blocked)."""
    boxes = list(boxes)
    joined = True
    while joined:
        joined = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                same_line = min(a.y + a.h, b.y + b.h) - max(a.y, b.y) >= 0.6 * min(a.h, b.h)
                if same_line and a.x <= b.x + b.w and b.x <= a.x + a.w:
                    x, y = min(a.x, b.x), min(a.y, b.y)
                    boxes[i] = Rect(x, y, max(a.x + a.w, b.x + b.w) - x, max(a.y + a.h, b.y + b.h) - y)
                    del boxes[j]
                    joined = True
                    break
            if joined:
                break
    return sorted(boxes, key=lambda r: (r.y, r.x))


def war_text(screen: np.ndarray) -> list[Rect] | None:
    """A box over every piece of text on a war map: the players' names by
    their bases, and in the base's panel at the bottom (its stars and loot
    too: blocking a little more is better than a name showing). Four
    finders together: the OCR as the screen is, on the pale letters alone,
    and with the contrast raised (the names faint behind the panel's
    see-through edge), and the outlined labels. None if Windows' OCR isn't
    there."""
    from concurrent.futures import ThreadPoolExecutor

    height, width = screen.shape[:2]
    s = height / BASE_HEIGHT
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    raised = cv2.cvtColor(cv2.createCLAHE(clipLimit=6, tileGridSize=(16, 16)).apply(gray), cv2.COLOR_GRAY2BGR)
    with ThreadPoolExecutor(3) as pool:  # (each takes most of a second: side by side)
        reads = [pool.submit(read_lines, image, zoom)
                 for image, zoom in ((screen, 2), (_light_text(screen), 3), (raised, 2))]
        reads = [r.result() for r in reads]
    if all(r is None for r in reads):
        return None
    left, right = round(WAR_TEXT[0] * s), round(WAR_TEXT[1] * s)
    banner = _scaled(WAR_BANNER, height)
    room = max(2, round(TEXT_ROOM * s))
    boxes = []
    for r in [line for lines in reads for line in lines or []] + text_marks(screen):
        middle_x = r.x + r.w / 2
        in_banner = (banner.x <= r.x and r.x + r.w <= banner.x + banner.w
                     and banner.y <= r.y and r.y + r.h <= banner.y + banner.h)
        if not left <= middle_x <= right or in_banner or r.h > 45 * s or r.w > 500 * s:
            continue  # the buttons at the sides, the banner (blocked whole), or not a line of text
        x, y = max(r.x - room, 0), max(r.y - room, 0)
        boxes.append(Rect(x, y, min(r.x + r.w + room, width) - x, min(r.y + r.h + room, height) - y))
    return _merged(boxes)


def block(screen: np.ndarray, library=None, extra: list[Rect] = ()) -> Blocked:
    """``screen`` with what's personal blacked out (see the module's notes).
    ``library``: the bot's pictures (a TemplateLibrary), to find the chat's
    edge and the Clan Castle by; ``extra``: areas the user marked."""
    image = screen.copy()
    height = image.shape[0]
    done = Blocked(image)
    done.areas.append(("name and clan", _scaled(NAME_CORNER, height)))
    edge = chat_edge(screen, library)
    if edge:
        done.chat = True
        done.areas.append(("clan chat", Rect(0, 0, edge, height)))
    if _best(screen, _pictures(library, WAR_MAP), WAR_LIKENESS):
        done.war = True
        done.areas.append(("war clans", _scaled(WAR_BANNER, height)))
        found = war_text(screen)
        if found is None:  # (Windows' OCR not there): blur where the names are, as before
            done.areas.append(("war map", _scaled(WAR_NAMES, height)))
        else:
            done.areas += [("war names", r) for r in found]
    for box in castles(screen, library):
        done.castle = True
        done.areas.append(("Clan Castle", box))
    done.areas += [("by hand", r) for r in extra]
    cover(image, done.areas)
    return done


def cover(image: np.ndarray, areas: list[tuple[str, Rect]]) -> np.ndarray:
    """Black out (or blur, for :data:`BLURRED`) each area of ``image``, in place."""
    height, width = image.shape[:2]
    for what, r in sorted(areas, key=lambda a: a[0] not in BLURRED):  # blur first, so a block stays black
        x0, y0 = max(r.x, 0), max(r.y, 0)
        x1, y1 = min(r.x + r.w, width), min(r.y + r.h, height)
        if x1 <= x0 or y1 <= y0:
            continue
        if what in BLURRED:
            small = cv2.resize(image[y0:y1, x0:x1], None, fx=1 / 12, fy=1 / 12, interpolation=cv2.INTER_AREA)
            image[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
        else:
            image[y0:y1, x0:x1] = FILL
    return image


def taken_at(path: Path) -> datetime | None:
    """When the bot saved this error screen (from its name), or None if it
    isn't one of the bot's."""
    m = NAME.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def recent(folder: Path, now: datetime | None = None, days: int = KEEP_DAYS) -> list[Path]:
    """The bot's error screens in ``folder`` from the last ``days`` days,
    newest first. (Older ones stay on disk; they're just not shown.)"""
    if not folder.is_dir():
        return []
    since = (now or datetime.now()) - timedelta(days=days)
    dated = [(t, p) for p in folder.glob("*.png") if (t := taken_at(p)) is not None and t >= since]
    return [p for _, p in sorted(dated, reverse=True)]


def load(path: Path) -> np.ndarray | None:
    return imagefile.read(str(path))
