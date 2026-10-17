"""Find the red deploy boundary around an enemy base.

The red line is drawn as red/orange dots around the area where troops
cannot be dropped. Instead of matching pictures of it, we pick out its
colour, keep only the outermost red pixels in each direction from the base
centre, and push them outward a few pixels so taps land just outside.

Sides follow the isometric view of a fully zoomed-out base:

    nw = upper-left edge    ne = upper-right edge
    sw = lower-left edge    se = lower-right edge
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import cv2
import numpy as np

from farmbot.config import RedLineConfig
from farmbot.geometry import Point, Rect

log = logging.getLogger(__name__)

# Angle ranges (degrees, image coordinates with y pointing down) per side.
SIDE_ANGLES = {
    "ne": (-90.0, 0.0),
    "se": (0.0, 90.0),
    "sw": (90.0, 180.0),
    "nw": (-180.0, -90.0),
}


@dataclass
class RedLine:
    center: Point
    sides: dict[str, list[Point]]  # ordered along each edge


def red_mask(screen: np.ndarray, cfg: RedLineConfig) -> np.ndarray:
    """Binary mask of red-line coloured pixels inside the battlefield."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    mask = thin_line_mask(hsv, cfg) if cfg.line_hue else np.zeros(screen.shape[:2], np.uint8)
    if cv2.countNonZero(mask) < cfg.min_points:
        # No faint line: look for the bright red one older versions flash.
        for lo, hi in cfg.hue_ranges:
            mask |= cv2.inRange(hsv, (lo, cfg.min_saturation, cfg.min_value), (hi, 255, 255))
    field = Rect.from_list(cfg.battlefield).resolve(screen.shape[1], screen.shape[0])
    keep = np.zeros_like(mask)
    keep[max(field.y, 0):field.y + field.h, max(field.x, 0):field.x + field.w] = 255
    mask &= keep
    for values in cfg.ignore:
        r = Rect.from_list(values).resolve(screen.shape[1], screen.shape[0])
        mask[max(r.y, 0):r.y + r.h, max(r.x, 0):r.x + r.w] = 0
    if cfg.join_px > 1:
        # Merge the dotted line into continuous strokes.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg.join_px, cfg.join_px))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def thin_line_mask(hsv: np.ndarray, cfg: RedLineConfig) -> np.ndarray:
    """The faint orange boundary: pixels of its colour that form thin
    strokes running along the isometric tile grid. Thick patches of the
    same colour (dirt, desert ground, campfires) are dropped."""
    (h_lo, h_hi), (v_lo, v_hi) = cfg.line_hue, cfg.line_value
    mask = cv2.inRange(hsv, (h_lo, cfg.line_min_saturation, v_lo), (h_hi, 255, v_hi))
    thick = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((4, 4), np.uint8))
    mask &= ~cv2.dilate(thick, np.ones((5, 5), np.uint8))
    strokes = np.zeros_like(mask)
    for slope in (cfg.iso_ratio, -cfg.iso_ratio):
        strokes |= cv2.morphologyEx(mask, cv2.MORPH_OPEN, _line_kernel(cfg.line_length, slope))
    # The boundary runs for whole tiles; short strokes are ground texture
    # (the desert scenery is full of them).
    count, labels, stats, _ = cv2.connectedComponentsWithStats(strokes)
    long = [i for i in range(1, count)
            if max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]) >= cfg.line_min_stroke]
    return np.isin(labels, long).astype(np.uint8) * 255


def _line_kernel(length: int, slope: float) -> np.ndarray:
    """A one-pixel line ``length`` wide at ``slope`` (y down), as a kernel."""
    h = round(length * abs(slope)) + 1
    kernel = np.zeros((h, length), np.uint8)
    y0, y1 = (h - 1, 0) if slope > 0 else (0, h - 1)
    cv2.line(kernel, (0, y0), (length - 1, y1), 1, 1)
    return kernel


def _blob_points(mask: np.ndarray, min_area: int) -> np.ndarray:
    """Pixels of every red blob big enough to be a dot of the line."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    big = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= min_area]
    if not big:
        return np.empty((0, 2), int)
    ys, xs = np.nonzero(np.isin(labels, big))
    return np.column_stack([xs, ys])


def _outermost(angles: np.ndarray, radii: np.ndarray, bins: int, band: tuple[float, float]) -> dict[str, list[int]]:
    """Per side, the index of the farthest point (within ``band``) in each angular bin."""
    lo_r, hi_r = band
    usable = (radii >= lo_r) & (radii <= hi_r)
    result: dict[str, list[int]] = {}
    for side, (start, end) in SIDE_ANGLES.items():
        width = (end - start) / bins
        result[side] = []
        for b in range(bins):
            lo = start + b * width
            in_bin = np.nonzero(usable & (angles >= lo) & (angles < lo + width))[0]
            if len(in_bin):
                result[side].append(int(in_bin[np.argmax(radii[in_bin])]))
    return result


def detect(screen: np.ndarray, cfg: RedLineConfig) -> RedLine | None:
    """Locate the red line; ``None`` if too little of it is visible."""
    points = _blob_points(red_mask(screen, cfg), cfg.min_blob)
    if len(points) < cfg.min_points:
        log.debug("Red line: only %d red pixels found", len(points))
        return None
    if cfg.center:
        cx, cy = cfg.center
    else:
        # Middle of the red pixels' extent, ignoring the extreme 1% so a
        # stray red speck cannot drag it.
        (x0, y0), (x1, y1) = np.percentile(points, [1, 99], axis=0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    dx = points[:, 0] - cx
    dy = points[:, 1] - cy
    angles = np.degrees(np.arctan2(dy, dx))
    radii = np.hypot(dx, dy)
    # Undo the isometric squash so the line is roughly equidistant from
    # the centre all the way round.
    flat = np.hypot(dx, dy / cfg.iso_ratio)

    # First pass: rough outline, used only to learn the typical line distance.
    rough = _outermost(angles, flat, cfg.bins_per_side, (0.0, np.inf))
    every = [i for idx in rough.values() for i in idx]
    typical = float(np.median(flat[every]))
    band = (cfg.min_radius_ratio * typical, cfg.max_radius_ratio * typical)
    # Second pass: farthest red pixel per bin that is plausibly the line.
    outline = _outermost(angles, flat, cfg.bins_per_side, band)

    field = Rect.from_list(cfg.battlefield).resolve(screen.shape[1], screen.shape[0])
    sides: dict[str, list[Point]] = {}
    for side, idx in outline.items():
        envelope: list[Point] = []
        for i in idx:
            r = radii[i]
            if r == 0:
                continue
            scale = (r + cfg.offset_px) / r  # step outward from the centre
            envelope.append((round(cx + dx[i] * scale), round(cy + dy[i] * scale)))
        points = _drop_inner(_inside(envelope, field), (cx, cy), cfg)
        sides[side] = points if _smooth(points, (cx, cy), cfg) else []
    kept = [p for pts in sides.values() for p in pts]
    if not kept or max(x for x, _ in kept) - min(x for x, _ in kept) < cfg.min_extent * field.w:
        log.debug("Red line: red pixels found but they do not form a line around the base")
        return None
    log.debug("Red line: centre (%d, %d), points %s", cx, cy, {s: len(p) for s, p in sides.items()})
    return RedLine((round(cx), round(cy)), sides)


def _smooth(points: list[Point], center: tuple[float, float], cfg: RedLineConfig) -> bool:
    """Neighbouring points of a real line sit at similar distances."""
    if len(points) < 2:
        return False
    cx, cy = center
    radii = np.array([np.hypot(x - cx, (y - cy) / cfg.iso_ratio) for x, y in points])
    jumps = np.abs(np.diff(radii)) / np.median(radii)
    return float(np.percentile(jumps, 90)) <= cfg.max_jump


INNER_SHARE = 0.85  # points closer in than this share of their side's typical distance are dropped


def _drop_inner(points: list[Point], center: tuple[float, float], cfg: RedLineConfig) -> list[Point]:
    """Drop points well inside the rest of their side: red or orange bits of
    the base itself. A point too far out only costs a longer walk, but one
    inside the line is refused by the game."""
    if len(points) < 3:
        return points
    cx, cy = center
    radii = [np.hypot(x - cx, (y - cy) / cfg.iso_ratio) for x, y in points]
    typical = float(np.median(radii))
    return [p for p, r in zip(points, radii) if r >= INNER_SHARE * typical]


def _inside(points: list[Point], field: Rect) -> list[Point]:
    return [p for p in points if field.contains(p)]


def spread(points: list[Point], count: int) -> list[Point]:
    """``count`` points spread evenly along an ordered edge (repeats if short)."""
    if count <= 0 or not points:
        return []
    if count == 1:
        return [points[len(points) // 2]]
    last = len(points) - 1
    return [points[math.floor(i * last / (count - 1) + 0.5)] for i in range(count)]


def draw(screen: np.ndarray, redline: RedLine | None) -> np.ndarray:
    """Debug picture: detected deploy points per side over the screenshot."""
    out = screen.copy()
    colours = {"nw": (255, 255, 0), "ne": (0, 255, 0), "sw": (255, 0, 255), "se": (0, 255, 255)}
    if redline:
        cv2.drawMarker(out, redline.center, (255, 255, 255), cv2.MARKER_CROSS, 20, 2)
        for side, pts in redline.sides.items():
            for p in pts:
                cv2.circle(out, p, 4, colours[side], -1)
            if pts:
                cv2.putText(out, side, pts[len(pts) // 2], cv2.FONT_HERSHEY_SIMPLEX, 0.8, colours[side], 2)
    return out
