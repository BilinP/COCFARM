"""Template matching and glyph-based number reading.

Templates are PNG crops of game UI taken from your own screenshots
(see ``farmbot crop``). A template is addressed by its path relative to
the template root without the extension, e.g. ``buttons/attack``. A
directory name addresses every PNG in it, e.g. ``troops``.
"""

from __future__ import annotations

import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from farmbot import imagefile
from farmbot.geometry import Rect
from farmbot.text import Font, TextStyle


@dataclass(frozen=True)
class Match:
    name: str
    x: int  # centre, screen coordinates
    y: int
    score: float
    w: int
    h: int

    @property
    def point(self) -> tuple[int, int]:
        return (self.x, self.y)


class TemplateLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[Path, tuple[float, np.ndarray]] = {}

    def _load(self, path: Path) -> np.ndarray:
        # Keyed on the file's time stamp so a picture re-captured in the
        # GUI is picked up by a bot that is already running.
        stamp = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached is None or cached[0] != stamp:
            image = imagefile.read(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"cannot read template {path}")
            self._cache[path] = cached = (stamp, image)
        return cached[1]

    def paths(self, name: str) -> list[Path]:
        target = self.root / name
        # ``name.png``, and any other looks of it in a ``name/`` directory
        # (a button drawn a little differently on another emulator or theme).
        file = target.with_suffix(".png")
        found = [file] if file.exists() else []
        if target.is_dir():
            found += sorted(target.glob("*.png"))
        return found

    def has(self, name: str) -> bool:
        return bool(self.paths(name))

    def get(self, name: str) -> list[tuple[str, np.ndarray]]:
        """``(name, image)`` pairs; names are relative paths without suffix."""
        found = self.paths(name)
        if not found:
            raise FileNotFoundError(f"no template named {name!r} under {self.root}")
        return [(p.relative_to(self.root).with_suffix("").as_posix(), self._load(p)) for p in found]


def _crop(screen: np.ndarray, region: Rect | None) -> tuple[np.ndarray, int, int]:
    if region is None:
        return screen, 0, 0
    h, w = screen.shape[:2]
    x0, y0 = max(region.x, 0), max(region.y, 0)
    x1, y1 = min(region.x + region.w, w), min(region.y + region.h, h)
    return screen[y0:y1, x0:x1], x0, y0


def match_template(
    screen: np.ndarray,
    template: np.ndarray,
    threshold: float,
    region: Rect | None = None,
    name: str = "",
    min_distance: int | None = None,
) -> list[Match]:
    """All matches of one template above ``threshold``, best first.

    Overlapping hits are collapsed: a hit closer than ``min_distance``
    (default: half the template's smaller side) to a better one is dropped.
    """
    area, ox, oy = _crop(screen, region)
    th, tw = template.shape[:2]
    if area.shape[0] < th or area.shape[1] < tw:
        return []
    hits = _coarse_hits(area, template, threshold)
    if hits is None:
        scores = cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(scores >= threshold)
        hits = [(float(scores[y, x]), int(x), int(y)) for y, x in zip(ys, xs)]
    hits.sort(key=lambda h: -h[0])
    spacing = min_distance if min_distance is not None else max(1, min(tw, th) // 2)
    kept: list[Match] = []
    for score, x, y in hits:
        cx, cy = x + ox + tw // 2, y + oy + th // 2
        if all(abs(cx - m.x) >= spacing or abs(cy - m.y) >= spacing for m in kept):
            kept.append(Match(name, cx, cy, score, tw, th))
    return kept


# Coarse to fine: a picture is looked for on a half-size screen first (about
# a sixth of the work), and only the spots that come near are scored at full
# size, so the scores (and what passes) are the full-size ones. A picture
# that's there scores well above COARSE_DROP below the threshold at half
# size; small pictures (blurred to nothing at half size) are matched in full.
#
# The half-size look is itself in two steps: in grey first (a third of a
# colour match's work), then in colour at the grey spots only. On real
# screens a picture that's there scores the same in grey as in colour to
# within a few hundredths, so grey is given GREY_DROP more room; a picture
# with little light and dark in it (told apart by its colours) skips grey.
# (14: a picture 16 px high is 15 at a window size of 0.94, and a full match costs ten times the coarse one)
COARSE_MIN = 14  # px: a picture's smaller side for the coarse look
COARSE_DROP = 0.2
COARSE_PAD = 3  # px around a coarse spot (full size) searched again in full
COARSE_MAX_SPOTS = 150  # more than this near: match in full (each spot is a tiny match, so many are still quicker)
GREY_DROP = 0.07  # below the colour look's bar
GREY_MIN_CONTRAST = 12.0  # a half-size picture's grey levels (standard deviation) for the grey look
GREY_MAX_SPOTS = 400  # more grey spots than this: the colour look over the whole half-size screen instead
_KERNEL = np.ones((3, 3), np.uint8)

# The half-size screen (colour and grey) of the areas looked at lately: a
# screen check looks at one screenshot for dozens of pictures. Kept by the
# area's memory and shape, with the array itself (so its memory can't be
# reused) and a sprinkling of its pixels (so one drawn on since is seen).
#
# The screenshot is held weakly: once nothing else has it (the bot has moved
# on to the next), it can't be looked at again, so its entries go with it
# rather than keeping up to a dozen old screenshots alive (~50 MB). Its
# memory can't be reused while an entry is kept: the entry goes first.
_HALVES_KEPT = 12
_halves: dict[tuple, tuple[object, bytes, np.ndarray, np.ndarray]] = {}
_halves_lock = threading.RLock()  # RLock: a screenshot freed while it's held drops its entries


def _owner(area: np.ndarray) -> np.ndarray:
    while isinstance(area.base, np.ndarray):
        area = area.base
    return area


def _half(area: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``area`` at half size, in colour and in grey."""
    key = (area.__array_interface__["data"][0], area.shape, area.strides)
    sample = area[::53, ::59].tobytes()
    with _halves_lock:
        kept = _halves.get(key)
        if kept is not None and kept[1] == sample:
            return kept[2], kept[3]
    small = cv2.resize(area, (area.shape[1] // 2, area.shape[0] // 2), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 and small.shape[2] == 3 else small

    def forget(_ref, key=key) -> None:
        with _halves_lock:
            entry = _halves.get(key)
            if entry is not None and entry[0] is _ref:
                del _halves[key]

    try:
        holder: object = weakref.ref(_owner(area), forget)
    except TypeError:  # memory not owned by an array: held as before
        holder = area
    with _halves_lock:
        _halves.pop(key, None)
        while len(_halves) >= _HALVES_KEPT:
            del _halves[next(iter(_halves))]
        _halves[key] = (holder, sample, small, grey)
    return small, grey


def _coarse_spots(small: np.ndarray, grey: np.ndarray, tsmall: np.ndarray, bar: float) -> list[tuple[int, int]] | None:
    """Half-size positions ``(x, y)`` where ``tsmall`` scores ``bar`` or
    more in colour, one per neighbourhood; ``None`` for too many."""
    tgrey = cv2.cvtColor(tsmall, cv2.COLOR_BGR2GRAY) if tsmall.ndim == 3 and tsmall.shape[2] == 3 else None
    spots: list[tuple[int, int]] | None = None
    if tgrey is not None and grey is not small and float(tgrey.std()) >= GREY_MIN_CONTRAST:
        first = cv2.matchTemplate(grey, tgrey, cv2.TM_CCOEFF_NORMED)
        near = first >= bar - GREY_DROP
        if not near.any():
            return []
        ys, xs = np.nonzero(near & (first == cv2.dilate(first, _KERNEL)))
        if len(xs) <= GREY_MAX_SPOTS:
            # Each grey spot in colour, with its neighbours: the best of them, if it passes.
            h, w = tsmall.shape[:2]
            found = set()
            for gx, gy in zip(xs.tolist(), ys.tolist()):
                x0, y0 = max(gx - 1, 0), max(gy - 1, 0)
                scores = cv2.matchTemplate(small[y0:gy + 2 + h, x0:gx + 2 + w], tsmall, cv2.TM_CCOEFF_NORMED)
                _, best, _, (bx, by) = cv2.minMaxLoc(scores)
                if best >= bar:
                    found.add((x0 + bx, y0 + by))
            spots = sorted(found)
    if spots is None:  # the colour look over all of it
        coarse = cv2.matchTemplate(small, tsmall, cv2.TM_CCOEFF_NORMED)
        near = coarse >= bar
        if not near.any():
            return []
        # One spot per neighbourhood: the best of each 3 x 3 block of coarse positions.
        ys, xs = np.nonzero(near & (coarse == cv2.dilate(coarse, _KERNEL)))
        spots = list(zip(xs.tolist(), ys.tolist()))
    return None if len(spots) > COARSE_MAX_SPOTS else spots


def _coarse_hits(area: np.ndarray, template: np.ndarray, threshold: float) -> list[tuple[float, int, int]] | None:
    """``(score, x, y)`` of every full-size position scoring ``threshold``
    or more (top-left corners in ``area``), found coarse to fine; ``None``
    when the picture is too small for it, or too much comes near."""
    th, tw = template.shape[:2]
    if min(th, tw) < COARSE_MIN or min(area.shape[:2]) < 2 * min(th, tw):
        return None
    small, grey = _half(area)
    tsmall = cv2.resize(template, (tw // 2, th // 2), interpolation=cv2.INTER_AREA)
    spots = _coarse_spots(small, grey, tsmall, threshold - COARSE_DROP)
    if not spots:
        return spots
    found: dict[tuple[int, int], float] = {}
    h, w = area.shape[:2]
    for sx, sy in spots:
        x0, y0 = max(2 * sx - COARSE_PAD, 0), max(2 * sy - COARSE_PAD, 0)
        x1, y1 = min(2 * sx + COARSE_PAD + 2 + tw, w), min(2 * sy + COARSE_PAD + 2 + th, h)
        scores = cv2.matchTemplate(area[y0:y1, x0:x1], template, cv2.TM_CCOEFF_NORMED)
        for y, x in zip(*np.nonzero(scores >= threshold)):
            found[(x0 + int(x), y0 + int(y))] = float(scores[y, x])
    return [(score, x, y) for (x, y), score in found.items()]


# Most buttons are drawn at the HUD's scale, but some windows (such as the
# army screen) keep their own size, so on a screen whose UI scale isn't 1
# they come out smaller than the HUD. A template that doesn't match at the
# HUD's scale is tried at the window size too, and whichever matches is
# remembered for it. Only these two sizes are tried, so a window caught
# mid-animation can't lock in a wrong one.
def _resized(image: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return image
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)


ADAPTIVE_NEAR = 0.15  # a picture this close to matching at the window size gets the fine size search
ADAPTIVE_REST = 20  # misses before a fine size search that found nothing is tried again


def _score(screen: np.ndarray, template: np.ndarray, region: Rect | None = None) -> float:
    """Best match score of ``template`` anywhere in ``screen`` (or ``region``)."""
    area, _, _ = _crop(screen, region)
    if template.shape[0] > area.shape[0] or template.shape[1] > area.shape[1] or min(template.shape[:2]) < 4:
        return 0.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED))[1])


def best_scale(screen: np.ndarray, template: np.ndarray, sizes: list[float], region: Rect | None = None) -> tuple[float, float]:
    """``(scale, score)`` of the template size near one of ``sizes`` that
    matches ``screen`` best."""
    area, _, _ = _crop(screen, region)

    def score(scale: float) -> float:
        return _score(area, _resized(template, scale))

    candidates = sorted({round(size + 0.01 * i, 2) for size in sizes for i in range(-3, 4)})
    # matchTemplate lets go of the GIL: the sizes are tried side by side.
    scores = list(_scale_pool().map(score, candidates)) if len(candidates) > 4 else [score(c) for c in candidates]
    i = max(range(len(candidates)), key=lambda k: (scores[k], -k))  # the first of equal bests, as max() gives
    return candidates[i], scores[i]


SCALE_THREADS = max(2, min(8, (os.cpu_count() or 2) // 2))
_scale_executor: ThreadPoolExecutor | None = None
_scale_lock = threading.Lock()


def _scale_pool() -> ThreadPoolExecutor:
    """Threads for ``best_scale``'s sizes (apart from the game's find
    threads, which call it: a pool waiting on itself could stall)."""
    global _scale_executor
    with _scale_lock:
        if _scale_executor is None:
            _scale_executor = ThreadPoolExecutor(SCALE_THREADS, thread_name_prefix="farmbot-scale")
        return _scale_executor


class Vision:
    def __init__(self, library: TemplateLibrary, default_threshold: float = 0.85, adaptive: bool = True):
        self.library = library
        self.default_threshold = default_threshold
        self._fonts: dict[str, Font] = {}
        # Window size relative to the HUD (1 / UI scale; see ``best_scale``),
        # and template name -> the size it matched at.
        self.adaptive = adaptive
        self.window_scale = 1.0
        self.scales: dict[str, float] = {}
        self.fixed_scales: set[str] = set()  # set in the config: never second-guessed
        self._rest: dict[str, int] = {}  # template name -> misses left before its next fine size search
        self._nearly: set[str] = set()  # resting after a near miss (a button popping in, say)
        # Pictures (or directories) shown at both sizes, such as the Okay of a
        # small window and of a big one: on a miss the other size is always tried.
        self.two_sizes: set[str] = set()

    def font(self, name: str) -> Font:
        if name not in self._fonts:
            directory = self.library.root / name
            if not directory.is_dir():
                raise FileNotFoundError(f"no font directory {directory}; see 'farmbot learn-digits'")
            self._fonts[name] = Font.load(directory)
        return self._fonts[name]

    def find_all(
        self,
        screen: np.ndarray,
        name: str,
        threshold: float | None = None,
        region: Rect | None = None,
    ) -> list[Match]:
        threshold = self.default_threshold if threshold is None else threshold
        matches: list[Match] = []
        for tname, image in self.library.get(name):
            cached = self.scales.get(tname, 1.0)
            found = match_template(screen, _resized(image, cached), threshold, region, tname)
            if found and tname not in self.scales:
                # Seen at its own size: later misses mean it isn't there, so
                # they skip the look at the window size below (half the work).
                self.scales[tname] = 1.0
            if not found and cached != 1.0 and tname not in self.fixed_scales:
                # A size learned once (say, mid-animation) can be wrong for
                # good: fall back to the picture's own size.
                found = match_template(screen, image, threshold, region, tname)
                if found:
                    self.scales[tname] = 1.0
            if not found and self.scales.get(tname) == 1.0 and self.window_scale != 1.0 and self._two_sizes(tname):
                found = match_template(screen, _resized(image, self.window_scale), threshold, region, tname)
                if found:
                    self.scales[tname] = self.window_scale
            # Seen at its own size once, it may still really be the window size: a
            # button that only just passes at its own size (the Next button on an
            # 860 x 672 screen, popping in) mustn't lock that size in. Looked at
            # again now and then (each look costs a match).
            relook = self.scales.get(tname) == 1.0 and tname not in self.fixed_scales
            if not found and self.adaptive and (tname not in self.scales or relook) and self.window_scale != 1.0 \
                    and (self._ready(tname) or self._there_now(screen, image, tname, threshold, region)):
                if match_template(screen, _resized(image, self.window_scale), threshold - ADAPTIVE_NEAR, region):
                    # Most misses are buttons that simply aren't on screen: one
                    # look at the window size rules them out before the fine search.
                    scale, score = best_scale(screen, image, [1.0, self.window_scale], region)
                    if score >= threshold:
                        self.scales[tname] = scale
                        found = match_template(screen, _resized(image, scale), threshold, region, tname)
                    else:
                        self._rest[tname] = ADAPTIVE_REST
                        self._nearly.add(tname)
                elif relook:
                    self._rest[tname] = ADAPTIVE_REST
            matches.extend(found)
        return sorted(matches, key=lambda m: -m.score)

    def likeness(self, screen: np.ndarray, image: np.ndarray) -> float:
        """Best score of ``image`` at its own size or the window's."""
        return max(_score(screen, _resized(image, s)) for s in {1.0, self.window_scale})

    def _two_sizes(self, tname: str) -> bool:
        return any(tname == n or tname.startswith(n + "/") for n in self.two_sizes)

    def _there_now(self, screen: np.ndarray, image: np.ndarray, tname: str, threshold: float,
                   region: Rect | None) -> bool:
        """Resting after a near miss, whether the picture now passes at the
        window size: a button caught popping in mustn't go unseen for the
        whole rest (the search's Next is only waited for so long)."""
        return tname in self._nearly and bool(
            match_template(screen, _resized(image, self.window_scale), threshold, region))

    def _ready(self, tname: str) -> bool:
        """Whether a fine size search for ``tname`` may run now (after one
        found nothing, it rests for a number of misses)."""
        left = self._rest.get(tname, 0)
        if left:
            self._rest[tname] = left - 1
        else:
            self._nearly.discard(tname)
        return not left

    def find(
        self,
        screen: np.ndarray,
        name: str,
        threshold: float | None = None,
        region: Rect | None = None,
    ) -> Match | None:
        matches = self.find_all(screen, name, threshold, region)
        return matches[0] if matches else None

    def read_number(
        self,
        screen: np.ndarray,
        region: Rect,
        font: str,
        style: TextStyle = TextStyle(),
    ) -> int | None:
        """Read the number in ``region`` with the digit glyphs in ``font/``
        (made by ``farmbot learn-digits``). ``None`` if it can't be read
        with confidence."""
        return self.font(font).read_number(screen, region, style)
