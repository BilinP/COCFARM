"""Reading the troop bar at the bottom of the battle screen.

No per-troop pictures are needed. Each card is found from its outline
(strong vertical edges running the full card height at the right spacing),
then classified:

* ``troop`` – has an "xN" count in its top-right corner and a blue header,
* ``spell`` – has a count and a purple header,
* ``single`` – no count: a hero (deploys one unit),
* ``siege`` – no count, on the light cyan background every siege machine
  card has (heroes' cards are blue to purple). Its Clan Castle troops come
  out when it's destroyed.

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


GAP_FILL = 20  # px either side of a card filled in between two others
RED_HUE = 8  # a super troop's red frame: hue within this of red
COUNT_FROM = 0.6  # the "xN" count sits in the right part of the header (a siege picture can reach the middle)
WIDTH_PENALTY = 20.0  # score lost per card width of size mismatch
MAX_OVERLAP = 10  # px two cards may share (touching cards share an edge)


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


def _background_hue(hsv: np.ndarray, rect: Rect) -> float:
    """The card's background colour: just inside its left and right frame,
    over the middle of its height, where the picture rarely reaches."""
    y0, y1 = rect.y + int(rect.h * 0.35), rect.y + int(rect.h * 0.65)
    sides = [hsv[y0:y1, rect.x + 5:rect.x + 12], hsv[y0:y1, rect.x + rect.w - 12:rect.x + rect.w - 5]]
    pixels = np.concatenate([s.reshape(-1, 3) for s in sides if s.size])
    coloured = pixels[pixels[:, 1] > 150]  # siege backgrounds are strongly coloured
    return float(np.median(coloured[:, 0])) if len(coloured) > len(pixels) // 3 else -1.0


def _classify(hsv: np.ndarray, rect: Rect, cfg: BarConfig) -> str:
    header_h = max(6, int(rect.h * cfg.header_share))
    corner = hsv[rect.y:rect.y + header_h, rect.x + int(rect.w * COUNT_FROM):rect.x + rect.w - 4]
    white = ((corner[:, :, 1] < 50) & (corner[:, :, 2] > 215)).mean() if corner.size else 0.0
    if white < cfg.count_white_share:
        lo, hi = cfg.siege_hue
        return "siege" if lo <= _background_hue(hsv, rect) <= hi else "single"
    mid = hsv[rect.y + 2:rect.y + header_h, rect.x + int(rect.w * 0.45):rect.x + int(rect.w * 0.6)]
    coloured = mid[:, :, 1] > 80
    hue = float(np.median(mid[:, :, 0][coloured])) if coloured.any() else 0.0
    lo, hi = cfg.spell_hue
    return "spell" if lo <= hue <= hi else "troop"


def _frame_sides(hsv: np.ndarray, rect: Rect, cfg: BarConfig) -> tuple[float, float]:
    """How much of the box's left and right borders is a card frame (blue to
    purple, or red for a super troop). Cards score well on at least one side; an empty slot at the
    end of the bar, or a box that straddles two cards, shows the map or a
    portrait there instead."""
    rows = slice(rect.y + 15, rect.y + rect.h - 15)
    lo, hi = cfg.frame_hue
    shares = []
    for cols in (slice(rect.x + 2, rect.x + 9), slice(rect.x + rect.w - 9, rect.x + rect.w - 2)):
        side = hsv[rows, cols]
        hue, sat = side[:, :, 0], side[:, :, 1]
        # Blue to purple (troops, heroes, spells, siege), or red (super troops).
        framed = (((hue >= lo) & (hue <= hi)) | (hue <= RED_HUE) | (hue >= 180 - RED_HUE)) & (sat > 80)
        shares.append(float(framed.mean()) if side.size else 0.0)
    return shares[0], shares[1]


SLOT_WHITE = 0.002  # share of white (a level number) below which a card's bottom has none


def is_empty_slot(screen: np.ndarray, rect: Rect) -> bool:
    """An unused slot of the bar (a dashed outline over the battlefield),
    not a card: every card has a white level number on a dark badge along
    its bottom; an empty slot shows only the battlefield there. (Only for
    cards that aren't greyed out yet: the bar is read before deploying.)"""
    gray = cv2.cvtColor(screen[rect.y + int(rect.h * 0.7):rect.y + rect.h - 2, rect.x + 6:rect.x + rect.w - 6],
                        cv2.COLOR_BGR2GRAY)
    if gray.size == 0:
        return False
    return float((gray > 220).mean()) < SLOT_WHITE


def find_cards(screen: np.ndarray, cfg: BarConfig) -> list[Card]:
    h, w = screen.shape[:2]
    band = Rect.from_list(cfg.band).resolve(w, h)
    area = screen[band.y:band.y + band.h, band.x:band.x + band.w]
    if area.size == 0:
        return []
    edges = _edge_columns(cv2.cvtColor(area, cv2.COLOR_BGR2GRAY), cfg)
    lo, hi = cfg.card_width * (1 - cfg.width_tolerance), cfg.card_width * (1 + cfg.width_tolerance)
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    # Every pair of edges a card's width apart that frames a real card is a
    # candidate; where candidates overlap (one straddling a gap and part of
    # the next card), the one closest to a card's width, then the best
    # framed on both sides, wins.
    candidates: list[tuple[float, Rect]] = []
    for left in edges:
        for right in edges:
            if not lo <= right - left <= hi:
                continue
            rect = Rect(band.x + left, band.y, right - left, band.h)
            sides = _frame_sides(hsv, rect, cfg)
            if max(sides) < cfg.min_frame_share or is_empty_slot(screen, rect):
                continue
            candidates.append((sum(sides) / 2 - WIDTH_PENALTY * abs(rect.w - cfg.card_width) / cfg.card_width, rect))
    picked: list[Rect] = []
    for _, rect in sorted(candidates, key=lambda c: -c[0]):
        if all(min(rect.x + rect.w, p.x + p.w) - max(rect.x, p.x) <= MAX_OVERLAP for p in picked):
            picked.append(rect)
    # A card-sized space between two cards holds a card whose edges didn't
    # show (a red super troop's, or a picture that covers its frame).
    picked.sort(key=lambda r: r.x)
    for a, b in zip(picked, picked[1:]):
        space = b.x - (a.x + a.w)
        if cfg.card_width * 0.85 <= space <= cfg.card_width + 2 * GAP_FILL:
            rect = Rect(a.x + a.w + (space - cfg.card_width) // 2, a.y, cfg.card_width, a.h)
            if not is_empty_slot(screen, rect) and max(_frame_sides(hsv, rect, cfg)) >= cfg.min_frame_share / 2:
                picked.append(rect)
    cards = [Card(r.x, r.x + r.w, r.y, r.y + r.h, _classify(hsv, r, cfg), _saturation(hsv, r))
             for r in sorted(picked, key=lambda r: r.x)]
    return cards


ART = (0.28, 0.80, 0.14, 0.88)  # a card's picture: top, bottom, left, right shares (no count, no level)
SAME_CARD = 0.85  # picture likeness to call two cards the same unit
ART_SLACK = 6  # px around the picture, for cards found a little off


def card_art(screen: np.ndarray, card: Card, pad: int = 0) -> np.ndarray:
    h, w = card.bottom - card.top, card.right - card.left
    return screen[max(card.top + int(h * ART[0]) - pad, 0):card.top + int(h * ART[1]) + pad,
                  max(card.left + int(w * ART[2]) - pad, 0):card.left + int(w * ART[3]) + pad]


def card_is(screen: np.ndarray, card: Card, picture: np.ndarray, threshold: float = SAME_CARD) -> bool:
    """True if ``card`` shows ``picture`` (a card's picture cut with
    :func:`card_art`), e.g. the Bat Spell."""
    area = card_art(screen, card, ART_SLACK)
    if area.shape[0] < picture.shape[0] or area.shape[1] < picture.shape[1]:
        return False
    return float(cv2.matchTemplate(area, picture, cv2.TM_CCOEFF_NORMED).max()) >= threshold


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


def hero_health(screen: np.ndarray, card: Card) -> float | None:
    """A placed hero's health (0-1) from the length of the green bar on its
    card, or ``None`` if no bar shows (not placed, or gone)."""
    top = max(card.top - 30, 0)
    left, right = card.left + 8, card.right - 8
    hsv = cv2.cvtColor(screen[top:card.top + 25, left:right], cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 45) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)
    if not green.size:
        return None
    row = green[int(green.sum(axis=1).argmax())]
    xs = np.flatnonzero(row)
    if xs.size < 3:
        return None
    return min(1.0, (int(xs.max()) + 1) / row.size)


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
