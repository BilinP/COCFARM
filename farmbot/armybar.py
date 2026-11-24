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
* ``castle`` – standalone Clan Castle troops: a solid cyan card without a level badge.

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
    kind: str  # troop | spell | single | siege | castle
    saturation: float  # mean colour saturation when first seen

    @property
    def center(self) -> Point:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def rect(self) -> Rect:
        return Rect(self.left, self.top, self.right - self.left, self.bottom - self.top)


GAP_FILL = 20  # px either side of a card filled in between two others
RED_HUE = 8  # a super troop's red frame: hue within this of red
# A squad's green frame (the Barch squad, 19 Nov: hue ~52, saturation ~120,
# bright). Grass behind the bar is yellower (hue ~35), more saturated (~180)
# and darker, so it isn't taken for one.
SQUAD_HUE = (44, 66)
SQUAD_SATURATION = (80, 175)
SQUAD_VALUE = 120
COUNT_FROM = 0.6  # the "xN" count sits in the right part of the header (a siege picture can reach the middle)
WIDTH_PENALTY = 20.0  # score lost per card width of size mismatch
MAX_OVERLAP = 10  # px two cards may share (touching cards share an edge)
CASTLE_FRAME = 0.9  # both solid sides; an empty slot has only a dashed outline


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


def _classify(hsv: np.ndarray, rect: Rect, cfg: BarConfig, *, no_level: bool = False) -> str:
    header_h = max(6, int(rect.h * cfg.header_share))
    corner = hsv[rect.y:rect.y + header_h, rect.x + int(rect.w * COUNT_FROM):rect.x + rect.w - 4]
    white = ((corner[:, :, 1] < 50) & (corner[:, :, 2] > 215)).mean() if corner.size else 0.0
    if white < cfg.count_white_share:
        lo, hi = cfg.siege_hue
        if lo <= _background_hue(hsv, rect) <= hi:
            if no_level and min(_frame_sides(hsv, rect, cfg)) >= CASTLE_FRAME:
                return "castle"  # no level badge, unlike a Siege Machine
            return "siege"
        return "single"
    mid = hsv[rect.y + 2:rect.y + header_h, rect.x + int(rect.w * 0.45):rect.x + int(rect.w * 0.6)]
    coloured = mid[:, :, 1] > 80
    hue = float(np.median(mid[:, :, 0][coloured])) if coloured.any() else 0.0
    lo, hi = cfg.spell_hue
    return "spell" if lo <= hue <= hi else "troop"


def _frame_sides(hsv: np.ndarray, rect: Rect, cfg: BarConfig) -> tuple[float, float]:
    """How much of the box's left and right borders is a card frame (blue to
    purple, red for a super troop, or green for a squad). Cards score well on at least one side; an empty slot at the
    end of the bar, or a box that straddles two cards, shows the map or a
    portrait there instead."""
    rows = slice(rect.y + 15, rect.y + rect.h - 15)
    lo, hi = cfg.frame_hue
    shares = []
    for cols in (slice(rect.x + 2, rect.x + 9), slice(rect.x + rect.w - 9, rect.x + rect.w - 2)):
        side = hsv[rows, cols]
        hue, sat, value = side[:, :, 0], side[:, :, 1], side[:, :, 2]
        # Blue to purple (troops, heroes, spells, siege), or red (super troops).
        framed = (((hue >= lo) & (hue <= hi)) | (hue <= RED_HUE) | (hue >= 180 - RED_HUE)) & (sat > 80)
        framed |= ((hue >= SQUAD_HUE[0]) & (hue <= SQUAD_HUE[1]) & (sat >= SQUAD_SATURATION[0])
                   & (sat <= SQUAD_SATURATION[1]) & (value > SQUAD_VALUE))  # (green: a squad)
        shares.append(float(framed.mean()) if side.size else 0.0)
    return shares[0], shares[1]


SLOT_WHITE = 0.002  # share of white (a level number) below which a card's bottom has none


def is_empty_slot(screen: np.ndarray, rect: Rect) -> bool:
    """An unused slot of the bar (a dashed outline over the battlefield),
    not a card: troop, spell, hero and Siege cards have a white level number
    on a dark badge along the bottom; standalone Castle troops don't.
    Their solid cyan frame is checked separately by find_cards. (Only for
    cards that aren't greyed out yet: the bar is read before deploying.)"""
    gray = cv2.cvtColor(screen[rect.y + int(rect.h * 0.7):rect.y + rect.h - 2, rect.x + 6:rect.x + rect.w - 6],
                        cv2.COLOR_BGR2GRAY)
    if gray.size == 0:
        return False
    return float((gray > 220).mean()) < SLOT_WHITE


def _no_level_badge(screen: np.ndarray, rect: Rect) -> bool:
    # Levels sit at bottom left. The Castle's emblem occupies the middle:
    # white on a clan's badge must not turn the card into a Siege Machine.
    badge = Rect(rect.x, rect.y, round(rect.w * 0.28), rect.h)
    return is_empty_slot(screen, badge)


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
            empty = is_empty_slot(screen, rect)
            kind = _classify(hsv, rect, cfg, no_level=_no_level_badge(screen, rect))
            if max(sides) < cfg.min_frame_share or (empty and kind != "castle"):
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
            empty = is_empty_slot(screen, rect)
            kind = _classify(hsv, rect, cfg, no_level=_no_level_badge(screen, rect))
            if (not empty or kind == "castle") and max(_frame_sides(hsv, rect, cfg)) >= cfg.min_frame_share / 2:
                picked.append(rect)
    cards = [Card(r.x, r.x + r.w, r.y, r.y + r.h,
                  _classify(hsv, r, cfg, no_level=_no_level_badge(screen, r)), _saturation(hsv, r))
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


CARD_WIDTH_AT_1177 = 91  # px wide a card is on 1177 px wide frames (18 Nov)
PET_ICON = (0.09, 0.42, -0.02, 0.29)  # a placed hero's pet on her card: across and down, in card widths
PET_COLOUR_SAT, PET_COLOUR_VALUE = 60, 120


def pet_colour(screen: np.ndarray, card: Card) -> float:
    """The share of colour in the pet's picture on a placed hero's card (its
    top left corner). Greyed out, the pet is down (18 Nov: the Spirit
    Fox's, 0.60 by 0.00)."""
    w = card.right - card.left
    x0, x1, y0, y1 = (card.left + round(PET_ICON[0] * w), card.left + round(PET_ICON[1] * w),
                      max(card.top + round(PET_ICON[2] * w), 0), card.top + round(PET_ICON[3] * w))
    patch = screen[y0:y1, x0:x1]
    if not patch.size:
        return 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    return float(((hsv[..., 1] > PET_COLOUR_SAT) & (hsv[..., 2] > PET_COLOUR_VALUE)).mean())


EQUIPMENT_ABOVE = (-0.08, 1.08, -1.0, -0.1)  # a placed hero's equipment over her card: across, then up, in card widths
SAME_EQUIPMENT = 0.8


def shows_equipment(screen: np.ndarray, card: Card, picture: np.ndarray) -> bool:
    """True if a placed hero's equipment (the pictures over her card, one per
    piece: 18 Nov, the Royal Champion's Rocket Spear and Electro Boots)
    shows ``picture``."""
    w = card.right - card.left
    scale = w / CARD_WIDTH_AT_1177
    x0, x1 = max(card.left + round(EQUIPMENT_ABOVE[0] * w), 0), card.left + round(EQUIPMENT_ABOVE[1] * w)
    y0, y1 = max(card.top + round(EQUIPMENT_ABOVE[2] * w), 0), max(card.top + round(EQUIPMENT_ABOVE[3] * w), 0)
    area = screen[y0:y1, x0:x1]
    if abs(scale - 1.0) > 0.05:
        picture = cv2.resize(picture, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if area.shape[0] < picture.shape[0] or area.shape[1] < picture.shape[1]:
        return False
    return float(cv2.matchTemplate(area, picture, cv2.TM_CCOEFF_NORMED).max()) >= SAME_EQUIPMENT


def _health_bar_share(screen: np.ndarray, card: Card) -> float:
    """Share of bright green (a unit's health bar) across the top of the
    card and just above it, where it sits whether or not the card is raised."""
    hsv = cv2.cvtColor(screen[max(card.top - 30, 0):card.top + 25, card.left + 8:card.right - 8], cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 45) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)
    return float(green.mean()) if green.size else 0.0


BAR_START = 10  # px in from the card's side a health bar starts within (inside its border)
BAR_SOLID = 0.95  # share of a health bar's run that is green (leaves: 0.9 at most)
BAR_HUE_WARM = 44  # a hurt hero's bar: yellow, orange, red (OpenCV hues up to this; green above)...
BAR_HUE_RED = 8  # ...and red from the other end of the hue circle
BAR_ROWS = 12  # px a health bar is deep at most
BAR_ABOVE = 23  # px over a card's top its health bar's rows are at most (seen: 14-20)
BAR_LENGTH = 0.9  # share of the looked-at width a full bar fills (its outline ends short of the card's side)
BAR_GAP = 3  # px of sparkle a bar's fill may break for
BAR_THICK = 3  # rows a bar's fill is deep at least
BAR_DARK = 120  # mean brightness (HSV value) of a bar's dark outline row, at most
BAR_SHORTEST = 0.1  # share of the width a bar's run is at least (shorter: a fleck of the button behind)...
BAR_SLIVER = 0.04  # ...unless the outline round it glows red: she's nearly down (18 Nov: 8% read as none)


def hero_health(screen: np.ndarray, card: Card) -> float | None:
    """A placed hero's health (0-1) from the length of the bar on its card,
    or ``None`` if no bar shows (not placed, or gone). The bar is green,
    then orange about half way down, then red (18 Nov: a Queen at
    about 45% showed orange, which was read as no bar at all)."""
    read = hero_bar(screen, card)
    return read[0] if read else None


def hero_bar(screen: np.ndarray, card: Card) -> tuple[float, int] | None:
    """A placed hero's health and its bar's colour (OpenCV hue; the bar
    over the hero's head on the field is the same), or ``None``."""
    top = max(card.top - 30, 0)
    left, right = card.left + 8, card.right - 8
    hsv = cv2.cvtColor(screen[top:card.top + 32, left:right], cv2.COLOR_BGR2HSV)
    hue, bright = hsv[:, :, 0], (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)
    # (18 Nov: from green to orange it passes yellow, hue ~38, which was read as no bar)
    green = bright & ((hue <= 85) | (hue >= 180 - BAR_HUE_RED))
    if not green.size:
        return None
    # The bar fills from the card's side without a gap; leaves and grass of the
    # village behind it (it's see-through above the cards), and the card's gold
    # top edge, are patchy. The bar's row: the longest solid run from the side.
    runs = [_solid_run(r) for r in green]
    # ...is a few rows deep, all much the same length, and outlined above and
    # below: dark, or (low) flashing red its whole length. Card art, flecks of
    # scenery and the outline itself aren't (18 Nov: a King and a Dragon
    # Duke not yet out read as hurt).
    value = hsv[:, :, 2]
    # (18 Nov: low, the outline glows a dark red, neither dark nor bright: read as no bar, so she was
    # taken for hidden and nothing went on her)
    reddish = ((hue <= BAR_HUE_RED) | (hue >= 180 - BAR_HUE_RED)) & (hsv[:, :, 1] > 100)
    length = int(BAR_LENGTH * green.shape[1])
    glows = [reddish[r, :length].mean() >= 0.8 for r in range(len(green))]
    outline = [value[r].mean() < BAR_DARK or green[r, :length].mean() >= 0.9 or glows[r]
               for r in range(len(green))]
    # (Sunny lime grass behind a card not yet out is a solid block many rows deep; her bar isn't:
    # 18 Nov, an 18-row block read as a full bar, she was taken for out already and didn't walk.)
    solid = [green[r].mean() >= 0.6 for r in range(len(green))]

    def _deep(i: int) -> bool:
        up = next((k for k in range(i, -1, -1) if not solid[k]), -1)
        down = next((k for k in range(i, len(solid)) if not solid[k]), len(solid))
        return down - up - 1 > BAR_ROWS

    found = []
    for i in sorted(range(len(runs)), key=lambda i: -runs[i]):
        if runs[i] < BAR_SLIVER * green.shape[1]:
            break
        edge = glows if runs[i] < BAR_SHORTEST * green.shape[1] else outline
        up, down = _block(runs, i)
        if (down - up - 1 >= BAR_THICK and not _deep(i) and any(edge[up - k] for k in range(3) if up - k >= 0)
                and any(edge[down + k] for k in range(3) if down + k < len(runs))):
            found.append(i)
    # A strip that carries on past the card's side is the village behind (18 Nov: lime grass
    # over a Champion's card read 100% while her bar, just under it, showed 28%); her bar stops at
    # the card's border (18 Nov: a full bar runs to the far side of the looked-at width, and
    # taking that for grass read flecks of her equipment icons as 22%).
    past = cv2.cvtColor(screen[top:card.top + 32, right:right + 16], cv2.COLOR_BGR2HSV)
    beyond = ((past[:, :, 1] > 150) & (past[:, :, 2] > 150) & ((past[:, :, 0] <= 85) | (past[:, :, 0] >= 180 - BAR_HUE_RED))
              if past.size else np.zeros((len(green), 1), bool))
    def _past(i: int) -> bool:  # (the strip's rows, any: its soft top row is patchier)
        up, down = _block(runs, i)
        return max(beyond[r].mean() for r in range(up + 1, down)) >= 0.8

    scenery = [i for i in found if _run(green[i]) >= green.shape[1] - 1 and _past(i)]
    kept = [i for i in found if i not in scenery]
    # (Her bar sits just over the card, 14-20 px; a strip of grass showing through over it is higher
    # up: 18 Nov, one 25 px over read 100% while she fell, not crossing into the next card.)
    near = [i for i in kept if top + i >= card.top - BAR_ABOVE]
    at = near[0] if near else kept[0] if kept else found[0] if found else None
    if at is None:
        return None
    row = green[at]
    start = int(np.flatnonzero(row)[0])
    # A bar is a thin strip: as wide a run of that colour many rows deep is
    # something behind the cards (the orange Next button over the bar, say).
    # (Low, the bright red outline rows over and under a sliver of fill aren't its depth.)
    deep = sum(1 for r, share in enumerate(green[:, start:start + runs[at]].mean(axis=1))
               if share >= 0.5 and not outline[r])
    if deep > BAR_ROWS:
        return None
    # Low, the bar's outline flashes red its whole length above and below the
    # fill (18 Nov: a Champion at about 30% read as full): the bar's
    # length is the most rows' run from the left, not the longest row's.
    rows = [at]
    for step in (-1, 1):
        r = at + step
        while 0 <= r < len(green) and green[r, :BAR_START + 1].any():
            rows.append(r)
            r += step
    health = min(1.0, float(np.median([_run(green[r]) for r in rows])) / (BAR_LENGTH * row.size))
    fill = hue[rows, :max(_run(row), 1)][green[rows, :max(_run(row), 1)]]
    fill = np.where(fill >= 90, fill.astype(int) - 180, fill)  # (red runs round: 178 is -2)
    return health, int(np.median(fill)) % 180 if fill.size else -1


def _block(runs: list[int], at: int) -> tuple[int, int]:
    """The rows just above and below those on end, ``at``'s among them, that
    run about as long as it."""
    n = runs[at]
    like = [abs(m - n) <= max(3, 0.2 * n) for m in runs]
    up = next((k for k in range(at - 1, -1, -1) if not like[k]), -1)
    down = next((k for k in range(at + 1, len(runs)) if not like[k]), len(runs))
    return up, down


def _solid_run(row: np.ndarray) -> int:
    """How long a row's colour runs, if it could be a bar's: from the card's
    side, all but solid (0 otherwise)."""
    xs = np.flatnonzero(row)
    if xs.size < 3 or xs[0] > BAR_START:
        return 0
    span = _run(row) - int(xs[0])  # (flecks of the village past its end aren't its)
    return span if (xs < xs[0] + span).sum() >= BAR_SOLID * span else 0


def _run(row: np.ndarray) -> int:
    """Where a bar's row ends: its first run of colour from the left (a
    sparkle's gap of a few px doesn't end it)."""
    xs = np.flatnonzero(row)
    if not xs.size:
        return 0
    gaps = np.flatnonzero(np.diff(xs) > BAR_GAP + 1)
    return int(xs[gaps[0]] if gaps.size else xs[-1]) + 1


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
