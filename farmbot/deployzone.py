"""Where troops can go when no red line is shown.

The base's building area is drawn as bright checkerboard grass; the
darker strip around it is never built on. We find the bright grass,
wrap it in its convex hull, push that outline outward by about a tile,
and keep the points that land on open ground away from the screen's UI.
Any point the game still rejects just costs a tap: the deploy loop checks
whether the troop count went down.

At low Town Halls the bright grass covers nearly the whole map, so its
outline is the map's edge. There the ring goes round the cluster of
buildings instead (see :func:`building_cluster`).
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from farmbot.config import ZoneConfig
from farmbot.geometry import Point, Rect, scaled
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
    # At half size (every second pixel): the patches are big, and it takes
    # a quarter of the time.
    mask, step = mask[::CLUSTER_STEP, ::CLUSTER_STEP], CLUSTER_STEP
    opening, closing = max(1, round(cfg.open_px / step)), 2 * round(cfg.close_px / step / 2) + 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((opening, opening), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (closing, closing)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    keep = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] * step * step >= cfg.min_patch]
    if len(keep) < cfg.min_patches:
        return None
    points = cv2.findNonZero(np.isin(labels, keep).astype(np.uint8))
    return cv2.convexHull(points) * step


def _blank(mask: np.ndarray, rects: list[list[int]]) -> None:
    h, w = mask.shape[:2]
    for values in rects:
        r = Rect.from_list(values).resolve(w, h)
        mask[max(r.y, 0):r.y + r.h, max(r.x, 0):r.x + r.w] = 0


def ground(screen: np.ndarray) -> np.ndarray:
    """Grass or dirt, per pixel."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    grass = (h >= 22) & (h <= 58) & (s >= 90) & (v >= 90)
    dirt = (h >= 10) & (h < 22) & (s >= 80) & (v >= 80)
    return grass | dirt


def open_ground(screen: np.ndarray, cfg: ZoneConfig) -> np.ndarray:
    """Share of non-ground pixels around each pixel (0 = all grass/dirt)."""
    blocked = 1.0 - ground(screen).astype(np.float32)
    k = cfg.ground_window
    return cv2.blur(blocked, (k, k))


CLUSTER_JOIN_PX = 22  # buildings closer than this (px on a screen 860 wide) are one cluster
CLUSTER_PART = 0.003  # of the screen's height squared: a smaller patch is a tree or a rock
CLUSTER_MIN_SHARE = 0.015  # of the screen: a smaller cluster is a stray object, not a base
CLUSTER_GRASS_SHARE = 0.5  # bright grass more than twice the cluster's size is the whole map
CLUSTER_STEP = 2  # the clusters are found on every second pixel
CLUSTER_OFFSET = 1.5  # the ring goes this many times ``offset_px`` out from the buildings


def building_cluster(screen: np.ndarray, cfg: ZoneConfig) -> np.ndarray | None:
    """Convex hull of the clusters of buildings, or ``None``.

    Buildings, walls and decorations are what isn't grass or dirt. Joined
    up, the base is the patches of them bigger than a tree or a rock that
    don't run off the screen (the forest round the map does). A small base
    can be in a few (its buildings, a Builder's Hut set apart)."""
    blocked = (~ground(screen)).astype(np.uint8) * 255
    _blank(blocked, cfg.exclude)
    # At half size: the shapes are big, and it takes a quarter of the time.
    blocked = blocked[::CLUSTER_STEP, ::CLUSTER_STEP]
    h, w = blocked.shape[:2]
    blocked = cv2.morphologyEx(blocked, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    k = 2 * round(scaled(CLUSTER_JOIN_PX, screen.shape[1]) / CLUSTER_STEP / 2) + 1
    blocked = cv2.morphologyEx(blocked, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(blocked)
    inner = [i for i in range(1, count)
             if stats[i, cv2.CC_STAT_LEFT] > 0 and stats[i, cv2.CC_STAT_TOP] > 0
             and stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] < w
             and stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] < h]
    parts = [i for i in inner if stats[i, cv2.CC_STAT_AREA] >= CLUSTER_PART * h * h]
    if not parts:
        return None
    hull = cv2.convexHull(cv2.findNonZero(np.isin(labels, parts).astype(np.uint8))) * CLUSTER_STEP
    return hull if cv2.contourArea(hull) >= CLUSTER_MIN_SHARE * screen.shape[0] * screen.shape[1] else None


def base_outline(screen: np.ndarray, cfg: ZoneConfig) -> tuple[np.ndarray, float] | None:
    """The building area's hull and how far out (px on a screen 860 wide)
    the ring goes: the bright grass, or, where that is (nearly) the whole
    map, the cluster of buildings."""
    hull = building_area(screen, cfg)
    cluster = building_cluster(screen, cfg)
    if cluster is not None and (hull is None or cv2.contourArea(cluster) < CLUSTER_GRASS_SHARE * cv2.contourArea(hull)):
        log.debug("Deploy zone: the bright grass is the whole map; going round the buildings")
        return cluster, cfg.offset_px * CLUSTER_OFFSET
    return (hull, cfg.offset_px) if hull is not None else None


def detect(screen: np.ndarray, cfg: ZoneConfig) -> RedLine | None:
    outline = base_outline(screen, cfg)
    if outline is None:
        log.debug("Deploy zone: building area not found")
        return None
    hull, offset = outline
    h, w = screen.shape[:2]
    filled = np.zeros((h, w), np.uint8)
    cv2.fillPoly(filled, [hull], 255)
    k = 2 * round(scaled(offset, w)) + 1
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
