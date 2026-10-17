"""Where troops can go when no red line is shown.

The base's building area is drawn as bright checkerboard grass; the
darker strip around it is never built on. We find the bright grass,
wrap it in its convex hull, push that outline outward by about a tile,
and keep the points that land on open ground away from the screen's UI.
Any point the game still rejects just costs a tap: the deploy loop checks
whether the troop count went down.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from farmbot.config import ZoneConfig
from farmbot.geometry import Point, Rect
from farmbot.redline import SIDE_ANGLES, RedLine

log = logging.getLogger(__name__)


def building_area(screen: np.ndarray, cfg: ZoneConfig) -> np.ndarray | None:
    """Convex hull of the bright building-area grass, or ``None``.

    The grass is a little darker on some screens (the scouting view is
    dimmed slightly on some emulators), so a darker threshold is tried when
    the configured one finds nothing.
    """
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    for darker in (0, 10, 20):
        hull = _grass_hull(hsv, cfg, cfg.grass_min_value - darker)
        if hull is not None:
            return hull
    return None


def _grass_hull(hsv: np.ndarray, cfg: ZoneConfig, v_min: int) -> np.ndarray | None:
    (h_lo, h_hi), s_min = cfg.grass_hue, cfg.grass_min_saturation
    mask = cv2.inRange(hsv, (h_lo, s_min, v_min), (h_hi, 255, 255))
    _blank(mask, cfg.exclude)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((cfg.open_px, cfg.open_px), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg.close_px, cfg.close_px)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    keep = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= cfg.min_patch]
    if len(keep) < cfg.min_patches:
        return None
    points = cv2.findNonZero(np.isin(labels, keep).astype(np.uint8))
    return cv2.convexHull(points)


def _blank(mask: np.ndarray, rects: list[list[int]]) -> None:
    h, w = mask.shape[:2]
    for values in rects:
        r = Rect.from_list(values).resolve(w, h)
        mask[max(r.y, 0):r.y + r.h, max(r.x, 0):r.x + r.w] = 0


def open_ground(screen: np.ndarray, cfg: ZoneConfig) -> np.ndarray:
    """Share of non-ground pixels around each pixel (0 = all grass/dirt)."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    grass = (h >= 22) & (h <= 58) & (s >= 90) & (v >= 90)
    dirt = (h >= 10) & (h < 22) & (s >= 80) & (v >= 80)
    blocked = 1.0 - (grass | dirt).astype(np.float32)
    k = cfg.ground_window
    return cv2.blur(blocked, (k, k))


def detect(screen: np.ndarray, cfg: ZoneConfig) -> RedLine | None:
    hull = building_area(screen, cfg)
    if hull is None:
        log.debug("Deploy zone: building area not found")
        return None
    h, w = screen.shape[:2]
    filled = np.zeros((h, w), np.uint8)
    cv2.fillPoly(filled, [hull], 255)
    k = 2 * cfg.offset_px + 1
    grown = cv2.dilate(filled, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    contours, _ = cv2.findContours(grown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    outline = max(contours, key=cv2.contourArea).reshape(-1, 2)

    allowed = np.full((h, w), 255, np.uint8)
    _blank(allowed, cfg.exclude)
    blocked = open_ground(screen, cfg)
    moments = cv2.moments(hull)
    cx, cy = moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]

    sides: dict[str, list[tuple[float, Point]]] = {s: [] for s in SIDE_ANGLES}
    for x, y in outline[::cfg.step_px]:
        if not (0 <= x < w and 0 <= y < h) or not allowed[y, x] or blocked[y, x] > cfg.max_blocked:
            continue
        angle = float(np.degrees(np.arctan2(y - cy, x - cx)))
        for side, (lo, hi) in SIDE_ANGLES.items():
            if lo <= angle < hi or (side == "sw" and angle == 180.0):
                sides[side].append((angle, (int(x), int(y))))
                break
    ordered = {s: [p for _, p in sorted(pts)] for s, pts in sides.items()}
    log.debug("Deploy zone: centre (%d, %d), points %s", cx, cy, {s: len(p) for s, p in ordered.items()})
    return RedLine((round(cx), round(cy)), ordered)
