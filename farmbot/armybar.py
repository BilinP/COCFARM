"""Reading the troop bar at the bottom of the battle screen.

No per-troop pictures are needed. Each card is found from its outline
(strong vertical edges running the full card height at the right spacing),
then classified:

* ``troop`` – has an "xN" count in its top-right corner and a blue header,
* ``spell`` – has a count and a purple header,
* ``single`` – no count: a hero or a siege machine (deploys one unit).

A card is *spent* once it greys out.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from farmbot.config import BarConfig
from farmbot.geometry import Point, Rect


@dataclass(frozen=True)
class Card:
    left: int
    right: int
    top: int
    bottom: int
    kind: str  # troop | spell | single
    saturation: float  # mean colour saturation when first seen

    @property
    def center(self) -> Point:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def rect(self) -> Rect:
        return Rect(self.left, self.top, self.right - self.left, self.bottom - self.top)


MAX_GAP = 20  # px between neighbouring cards


def _edge_columns(gray: np.ndarray, cfg: BarConfig) -> list[int]:
    """Columns where most rows show a sharp left/right brightness step."""
    steps = np.abs(np.diff(gray.astype(np.int16), axis=1)) > cfg.edge_contrast
    share = steps.mean(axis=0)
    cols = [x for x in range(len(share)) if share[x] >= cfg.edge_share]
    merged: list[int] = []
    for x in cols:
        if merged and x - merged[-1] <= 3:
            continue  # same edge, a pixel or two wide
        merged.append(x)
    return merged


def _saturation(hsv: np.ndarray, card: Rect) -> float:
    inner = hsv[card.y + 3:card.y + card.h - 3, card.x + 3:card.x + card.w - 3, 1]
    return float(inner.mean()) if inner.size else 0.0


def _classify(hsv: np.ndarray, rect: Rect, cfg: BarConfig) -> str:
    header_h = max(6, int(rect.h * cfg.header_share))
    corner = hsv[rect.y:rect.y + header_h, rect.x + rect.w // 2:rect.x + rect.w - 4]
    white = ((corner[:, :, 1] < 50) & (corner[:, :, 2] > 215)).mean() if corner.size else 0.0
    if white < cfg.count_white_share:
        return "single"
    mid = hsv[rect.y + 2:rect.y + header_h, rect.x + int(rect.w * 0.45):rect.x + int(rect.w * 0.6)]
    coloured = mid[:, :, 1] > 80
    hue = float(np.median(mid[:, :, 0][coloured])) if coloured.any() else 0.0
    lo, hi = cfg.spell_hue
    return "spell" if lo <= hue <= hi else "troop"


def _frame_share(hsv: np.ndarray, rect: Rect, cfg: BarConfig) -> float:
    """How much of the box's left or right border is a card frame (blue to
    purple). Cards score well on at least one side; an empty slot at the
    end of the bar, or a box that straddles two cards, shows the map or a
    portrait there instead."""
    rows = slice(rect.y + 15, rect.y + rect.h - 15)
    lo, hi = cfg.frame_hue
    best = 0.0
    for cols in (slice(rect.x + 2, rect.x + 9), slice(rect.x + rect.w - 9, rect.x + rect.w - 2)):
        side = hsv[rows, cols]
        if side.size:
            framed = (side[:, :, 0] >= lo) & (side[:, :, 0] <= hi) & (side[:, :, 1] > 80)
            best = max(best, float(framed.mean()))
    return best


def find_cards(screen: np.ndarray, cfg: BarConfig) -> list[Card]:
    h, w = screen.shape[:2]
    band = Rect.from_list(cfg.band).resolve(w, h)
    area = screen[band.y:band.y + band.h, band.x:band.x + band.w]
    if area.size == 0:
        return []
    edges = _edge_columns(cv2.cvtColor(area, cv2.COLOR_BGR2GRAY), cfg)
    lo, hi = cfg.card_width * (1 - cfg.width_tolerance), cfg.card_width * (1 + cfg.width_tolerance)
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    cards: list[Card] = []

    def right_edge(left: int) -> int | None:
        return next((e for e in edges if lo <= e - left <= hi), None)

    i = 0
    while i < len(edges):
        left = edges[i]
        right = right_edge(left)
        if right is None:
            i += 1
            continue
        rect = Rect(band.x + left, band.y, right - left, band.h)
        if _frame_share(hsv, rect, cfg) < cfg.min_frame_share:
            i += 1  # not a card: the real one may start at the next edge
            continue
        cards.append(Card(rect.x, rect.x + rect.w, rect.y, rect.y + rect.h, _classify(hsv, rect, cfg), _saturation(hsv, rect)))
        # The next card starts just after a small gap, or, when cards
        # touch, at this card's own right edge.
        j = edges.index(right)
        after_gap = j + 1 < len(edges) and edges[j + 1] - right <= MAX_GAP and right_edge(edges[j + 1]) is not None
        i = j + 1 if after_gap else j
    return cards


def is_spent(screen: np.ndarray, card: Card, cfg: BarConfig) -> bool:
    """True once the card has greyed out (no units left)."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    return _saturation(hsv, card.rect) < cfg.spent_ratio * card.saturation


def _health_bar_share(screen: np.ndarray, card: Card) -> float:
    """Share of bright green (a unit's health bar) across the top of the
    card and just above it, where it sits whether or not the card is raised."""
    hsv = cv2.cvtColor(screen[max(card.top - 30, 0):card.top + 25, card.left + 8:card.right - 8], cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 45) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)
    return float(green.mean()) if green.size else 0.0


def unit_on_field(before: np.ndarray, after: np.ndarray, card: Card, min_gain: float = 0.06) -> bool:
    """True once a hero or siege machine from ``card`` is on the field: its
    card shows the unit's green health bar, which it didn't ``before``.
    (Just selecting a card also changes how it looks, so a plain
    before/after difference can't tell.)"""
    return _health_bar_share(after, card) - _health_bar_share(before, card) >= min_gain


def card_changed(before: np.ndarray, after: np.ndarray, card: Card, min_diff: float = 4.0) -> bool:
    r = card.rect
    a = before[r.y:r.y + r.h, r.x:r.x + r.w].astype(np.int16)
    b = after[r.y:r.y + r.h, r.x:r.x + r.w].astype(np.int16)
    return a.shape == b.shape and float(np.abs(a - b).mean()) >= min_diff
