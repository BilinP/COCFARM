"""Reading game numbers: split white text into characters, then match each
character against digit shapes learned from your own screenshots.

The game draws numbers as white glyphs with a dark outline, so a simple
"bright and unsaturated" mask isolates them. Each glyph is compared after
resizing to a fixed box, which also makes reading tolerant of small
changes in screen scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from farmbot.geometry import Rect

GLYPH_BOX = (12, 18)  # width, height every glyph is compared at


@dataclass(frozen=True)
class TextStyle:
    max_saturation: int = 70
    min_value: int = 190
    min_area: int = 8
    # Pieces shorter than this share of the tallest one are noise (dots,
    # icon highlights), not digits.
    min_height_ratio: float = 0.6


def crop(screen: np.ndarray, region: Rect) -> np.ndarray:
    h, w = screen.shape[:2]
    x0, y0 = max(region.x, 0), max(region.y, 0)
    return screen[y0:min(region.y + region.h, h), x0:min(region.x + region.w, w)]


def segment(screen: np.ndarray, region: Rect, style: TextStyle = TextStyle()) -> list[np.ndarray]:
    """Binary masks of the characters in ``region``, left to right."""
    return [mask for _, mask in pieces(screen, region, style)]


def pieces(screen: np.ndarray, region: Rect, style: TextStyle = TextStyle()) -> list[tuple[int, np.ndarray]]:
    """``(x, mask)`` of each character in ``region``, left to right."""
    area = crop(screen, region)
    if area.size == 0:
        return []
    hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] <= style.max_saturation) & (hsv[:, :, 2] >= style.min_value)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    parts = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= style.min_area]
    if not parts:
        return []
    tallest = max(stats[i, cv2.CC_STAT_HEIGHT] for i in parts)
    parts = [i for i in parts if stats[i, cv2.CC_STAT_HEIGHT] >= style.min_height_ratio * tallest]
    chars = []
    for i in sorted(parts, key=lambda i: stats[i, cv2.CC_STAT_LEFT]):
        x, y, w, h = (int(v) for v in stats[i, :4])
        chars.append((x, ((labels[y:y + h, x:x + w] == i) * 255).astype(np.uint8)))
    return chars


def digit_groups(boxes: list[tuple[int, int]]) -> list[int] | None:
    """Sizes of the space-separated digit groups ("12 345" → [2, 3]) from
    each character's ``(x, width)``, dropping anything after a wide gap
    (an icon, not part of the number). ``None`` if they can't be a number:
    every group after the first has exactly three digits."""
    if not boxes:
        return None
    width = float(np.median([w for x, w in boxes if w > 0.5 * max(w for _, w in boxes)] or [1]))
    groups = [1]
    for (x0, w0), (x1, _) in zip(boxes, boxes[1:]):
        gap = x1 - (x0 + w0)
        if gap > 2 * width:
            break
        if gap > 0.5 * width:
            groups.append(1)
        else:
            groups[-1] += 1
    if len(groups) > 1 and (groups[0] > 3 or any(g != 3 for g in groups[1:])):
        return None
    return groups


def _number_run(found: list[tuple[int, np.ndarray]]) -> list[tuple[int, np.ndarray]]:
    """The pieces of the number: split where there's a wide gap (an icon
    either side of it, such as the builder base's boat behind the resource
    bars, left of the digits), the run with the most pieces (the first of
    equal ones)."""
    if not found:
        return found
    widths = [mask.shape[1] for _, mask in found]
    width = float(np.median([w for w in widths if w > 0.5 * max(widths)] or [1]))
    runs = [[found[0]]]
    for (x0, m0), piece in zip(found, found[1:]):
        if piece[0] - (x0 + m0.shape[1]) > 2 * width:
            runs.append([])
        runs[-1].append(piece)
    return max(runs, key=len)


def _normalize(mask: np.ndarray) -> np.ndarray:
    resized = cv2.resize(mask, GLYPH_BOX, interpolation=cv2.INTER_AREA)
    return (resized >= 128).astype(np.float32)


def holes(mask: np.ndarray) -> int:
    """Enclosed background areas: 0 has one, 8 has two, 5 has none."""
    padded = cv2.copyMakeBorder((mask > 0).astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    _, hierarchy = cv2.findContours(padded, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return 0
    return int(sum(1 for h in hierarchy[0] if h[3] >= 0))


@dataclass
class Glyph:
    char: str
    shape: np.ndarray  # normalized 0/1
    aspect: float  # width / height of the original
    holes: int

    @classmethod
    def from_mask(cls, char: str, mask: np.ndarray) -> Glyph:
        return cls(char, _normalize(mask), mask.shape[1] / mask.shape[0], holes(mask))


class Font:
    def __init__(self, glyphs: list[Glyph]):
        if not glyphs:
            raise ValueError("font has no glyphs")
        self.glyphs = glyphs

    @classmethod
    def load(cls, directory: str | Path) -> Font:
        glyphs = []
        for path in sorted(Path(directory).glob("*.png")):
            char = path.stem.split("_")[0]
            mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if mask is None or not char:
                continue
            glyphs.append(Glyph.from_mask(char, mask))
        return cls(glyphs)

    def classify(self, mask: np.ndarray) -> tuple[str, float]:
        probe = Glyph.from_mask("", mask)
        best, best_score = "?", -1.0
        for g in self.glyphs:
            union = np.maximum(probe.shape, g.shape).sum()
            iou = float(np.minimum(probe.shape, g.shape).sum() / union) if union else 0.0
            # Squashed into the box, a "1" looks like a block, so a clearly
            # different width counts against a match, and so does a
            # different number of holes (9 vs 5, 8 vs 0).
            score = iou - max(0.0, abs(probe.aspect - g.aspect) - 0.15) - 0.3 * abs(probe.holes - g.holes)
            if score > best_score:
                best, best_score = g.char, score
        return best, best_score

    def read(self, screen: np.ndarray, region: Rect, style: TextStyle = TextStyle(), min_score: float = 0.45) -> str:
        out = []
        for mask in segment(screen, region, style):
            char, score = self.classify(mask)
            if score >= min_score:
                out.append(char)
        return "".join(out)

    def read_number(self, screen: np.ndarray, region: Rect, style: TextStyle = TextStyle(),
                    min_score: float = 0.45) -> int | None:
        """The number in ``region``, or ``None`` when it can't be read with
        confidence: a character inside it isn't recognised, or the digit
        grouping is wrong. Unlike :meth:`read`, nothing is silently dropped,
        so a hard-to-see digit can't turn 241 953 into 24 953."""
        found = _number_run(pieces(screen, region, style))
        groups = digit_groups([(x, mask.shape[1]) for x, mask in found])
        if groups is None:
            return None
        digits = []
        for _, mask in found[:sum(groups)]:
            char, score = self.classify(mask)
            if score < min_score or not char.isdigit():
                return None
            digits.append(char)
        return int("".join(digits))


def learn(screen: np.ndarray, region: Rect, text: str, directory: str | Path,
          style: TextStyle = TextStyle(), replace: bool = False) -> list[Path]:
    """Save each character of ``text`` (spaces ignored) found in ``region``
    as a glyph in ``directory``. Existing glyphs are kept unless ``replace``."""
    chars = [c for c in text if not c.isspace()]
    masks = segment(screen, region, style)
    if len(masks) != len(chars):
        raise ValueError(f"found {len(masks)} characters in the region but the text has {len(chars)}")
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for char, mask in zip(chars, masks):
        path = out / f"{char}.png"
        if path.exists() and not replace:
            continue
        cv2.imwrite(str(path), mask)
        saved.append(path)
    return saved
