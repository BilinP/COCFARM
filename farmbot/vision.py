"""Template matching and glyph-based number reading.

Templates are PNG crops of game UI taken from your own screenshots
(see ``farmbot crop``). A template is addressed by its path relative to
the template root without the extension, e.g. ``buttons/attack``. A
directory name addresses every PNG in it, e.g. ``troops``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

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
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"cannot read template {path}")
            self._cache[path] = cached = (stamp, image)
        return cached[1]

    def paths(self, name: str) -> list[Path]:
        target = self.root / name
        if target.is_dir():
            return sorted(target.glob("*.png"))
        file = target.with_suffix(".png")
        return [file] if file.exists() else []

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
COARSE_MIN = 16  # px: a picture's smaller side for the coarse look
COARSE_DROP = 0.2
COARSE_PAD = 3  # px around a coarse spot (full size) searched again in full
COARSE_MAX_SPOTS = 150  # more than this near: match in full (each spot is a tiny match, so many are still quicker)


def _coarse_hits(area: np.ndarray, template: np.ndarray, threshold: float) -> list[tuple[float, int, int]] | None:
    """``(score, x, y)`` of every full-size position scoring ``threshold``
    or more (top-left corners in ``area``), found coarse to fine; ``None``
    when the picture is too small for it, or too much comes near."""
    th, tw = template.shape[:2]
    if min(th, tw) < COARSE_MIN or min(area.shape[:2]) < 2 * min(th, tw):
        return None
    small = cv2.resize(area, (area.shape[1] // 2, area.shape[0] // 2), interpolation=cv2.INTER_AREA)
    tsmall = cv2.resize(template, (tw // 2, th // 2), interpolation=cv2.INTER_AREA)
    coarse = cv2.matchTemplate(small, tsmall, cv2.TM_CCOEFF_NORMED)
    near = coarse >= threshold - COARSE_DROP
    if not near.any():
        return []
    # One spot per neighbourhood: the best of each 3 x 3 block of coarse positions.
    peaks = near & (coarse == cv2.dilate(coarse, np.ones((3, 3), np.uint8)))
    ys, xs = np.nonzero(peaks)
    if len(xs) > COARSE_MAX_SPOTS:
        return None
    found: dict[tuple[int, int], float] = {}
    h, w = area.shape[:2]
    for sx, sy in zip(xs, ys):
        x0, y0 = max(2 * int(sx) - COARSE_PAD, 0), max(2 * int(sy) - COARSE_PAD, 0)
        x1, y1 = min(2 * int(sx) + COARSE_PAD + 2 + tw, w), min(2 * int(sy) + COARSE_PAD + 2 + th, h)
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
    best = max(candidates, key=score)
    return best, score(best)


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
            if not found and self.adaptive and tname not in self.scales and self.window_scale != 1.0 \
                    and self._ready(tname) \
                    and match_template(screen, _resized(image, self.window_scale), threshold - ADAPTIVE_NEAR, region):
                # Most misses are buttons that simply aren't on screen: one
                # look at the window size rules them out before the fine search.
                scale, score = best_scale(screen, image, [1.0, self.window_scale], region)
                if score >= threshold:
                    self.scales[tname] = scale
                    found = match_template(screen, _resized(image, scale), threshold, region, tname)
                else:
                    self._rest[tname] = ADAPTIVE_REST
            matches.extend(found)
        return sorted(matches, key=lambda m: -m.score)

    def likeness(self, screen: np.ndarray, image: np.ndarray) -> float:
        """Best score of ``image`` at its own size or the window's."""
        return max(_score(screen, _resized(image, s)) for s in {1.0, self.window_scale})

    def _ready(self, tname: str) -> bool:
        """Whether a fine size search for ``tname`` may run now (after one
        found nothing, it rests for a number of misses)."""
        left = self._rest.get(tname, 0)
        if left:
            self._rest[tname] = left - 1
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
