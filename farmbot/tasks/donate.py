"""Donating to Clan Castle requests from the donate window.

The window (Donation Resource) shows two rows of troop cards, with the
siege machines at their end, and a row of spells; both scroll sideways.
A card in colour can be donated, a grey one can't (it doesn't fit, it's
not unlocked, or there's no resource for it). Each tap gives one unit and
costs the elixir or dark elixir on the card.

The bot gives the player's main units first, in their order, while they're
in colour; then (if asked) fills what's left with the biggest troop that
still fits. It never donates while the window is set to pay with gems.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from farmbot.config import ClanConfig
from farmbot.game import Game, T
from farmbot.units import BY_NAME, BY_SLUG

log = logging.getLogger(__name__)

# The donate window, in bot coordinates when its title is at TITLE_Y.
TITLE_Y = 185
TROOP_ROWS = ((268, 382), (386, 500))
SPELL_ROWS = ((553, 667),)
PANEL_X = (455, 1125)
GAP_BGR = np.array([205, 210, 210])  # the light grey between cards
CARD_W = (78, 92)
ART = (0.05, 0.5, 0.06, 0.94)  # the card's picture: top, bottom, left, right shares (no level, no price)
SAME_UNIT = 0.7  # picture likeness to name a card
IN_COLOUR = 45  # mean saturation of a card's picture: in colour (donatable), below: greyed out
PAGES = 12  # sideways screens of a row at most
SWIPE = (950, 650, 700)  # forward: from x, to x, ms
MAX_TAPS = 60  # taps on one request at most, whatever happens


@dataclass(frozen=True)
class DonateCard:
    name: str | None  # None: a unit without a picture yet
    x: int
    y: int
    w: int
    h: int
    available: bool

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def housing(self) -> int:
        unit = BY_NAME.get(self.name or "")
        return unit.housing if unit else 0


def _art(card: np.ndarray) -> np.ndarray:
    h, w = card.shape[:2]
    top, bottom, left, right = ART
    return card[int(h * top):int(h * bottom), int(w * left):int(w * right)]


@lru_cache(maxsize=4)
def _pictures(folder: str) -> tuple[tuple[str, np.ndarray], ...]:
    """Every unit picture under ``folder``, grey (a greyed-out card must
    still be recognised)."""
    out = []
    for path in sorted(Path(folder).glob("*.png")):
        unit = BY_SLUG.get(path.stem.split("@")[0])  # super_miner@2.png: another picture of it
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_GRAYSCALE)
        if unit and image is not None:
            out.append((unit.name, image))
    return tuple(out)


def name_card(card: np.ndarray, pictures) -> str | None:
    art = cv2.cvtColor(_art(card), cv2.COLOR_BGR2GRAY)
    best, best_score = None, SAME_UNIT
    for name, picture in pictures:
        sized = cv2.resize(art, (picture.shape[1], picture.shape[0]))
        score = float(cv2.matchTemplate(sized, picture, cv2.TM_CCOEFF_NORMED)[0, 0])
        if score > best_score:
            best, best_score = name, score
    return best


def _columns(screen: np.ndarray, y0: int, y1: int) -> list[tuple[int, int]]:
    """Left and right edge of each whole card in a row, from the gaps between them."""
    x0, x1 = PANEL_X
    band = screen[y0 + 7:y1 - 7, x0:x1].astype(np.int16)
    gap = (np.abs(band - GAP_BGR).max(axis=2) < 14).mean(axis=0) > 0.5
    out, start = [], None
    for i, is_gap in enumerate(list(gap) + [True]):
        if not is_gap and start is None:
            start = i
        elif is_gap and start is not None:
            if CARD_W[0] <= i - start <= CARD_W[1]:
                out.append((x0 + start, x0 + i))
            start = None
    return out


def read_cards(screen: np.ndarray, rows, pictures, dy: int = 0) -> list[DonateCard]:
    cards = []
    for y0, y1 in rows:
        y0, y1 = y0 + dy, y1 + dy
        for left, right in _columns(screen, y0, y1):
            card = screen[y0:y1, left:right]
            saturation = float(cv2.cvtColor(_art(card), cv2.COLOR_BGR2HSV)[:, :, 1].mean())
            cards.append(DonateCard(name_card(card, pictures), left, y0, right - left, y1 - y0,
                                    saturation >= IN_COLOUR))
    return cards


def pays_with_gems(screen: np.ndarray, cards: list[DonateCard]) -> bool:
    """True if the cards' prices are in gems (the green gem icon) rather
    than elixir or dark elixir."""
    for c in cards:
        icon = screen[c.y + int(c.h * 0.82):c.y + int(c.h * 0.96), c.x + int(c.w * 0.78):c.x + c.w - 3]
        hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
        green = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 120)
        if icon.size and green.mean() > 0.25:
            return True
    return False


def choose(cards: list[DonateCard], main: list[str], fill: bool) -> DonateCard | None:
    """The next card to tap: the first main unit in colour, else (with
    ``fill``) the biggest unit in colour (units without a known housing
    last), else none."""
    available = [c for c in cards if c.available]
    for name in main:
        for c in available:
            if c.name == name:
                return c
    if fill and available:
        return max(available, key=lambda c: c.housing)
    return None


def _title_dy(game: Game) -> int | None:
    match = game.find(T.DONATE_WINDOW)
    return None if match is None else match.y - TITLE_Y


def _scroll(game: Game, rows, dy: int, forward: bool) -> bool:
    """Swipe a row sideways; True if it moved."""
    y = (rows[0][0] + rows[-1][1]) // 2 + dy
    before = game.refresh()[rows[0][0] + dy:rows[-1][1] + dy, PANEL_X[0]:PANEL_X[1]].astype(np.int16)
    # A short, slow swipe moves about four cards, so screens overlap and no
    # card is only ever half on show at an edge. Back: a long fling.
    if forward:
        game.swipe(SWIPE[0], y, SWIPE[1], y, SWIPE[2])
    else:
        game.swipe(560, y, 1050, y, 500)
    game.wait(0.9)
    after = game.refresh()[rows[0][0] + dy:rows[-1][1] + dy, PANEL_X[0]:PANEL_X[1]].astype(np.int16)
    return float(np.abs(after - before).mean()) > 2


def _give(game: Game, rows, main: list[str], fill: bool, pictures, limit: int) -> int:
    """Donate from one set of rows (troops or spells) until nothing more
    should or can be given. Returns how many units were given."""
    given = 0
    while given < limit:
        dy = _title_dy(game)
        if dy is None:
            break  # the window closed: the request is full
        # Look through the row's screens for the best card to give.
        while _scroll(game, rows, dy, forward=False):
            pass
        pages = []
        for _ in range(PAGES):
            pages.append(read_cards(game.refresh(), rows, pictures, dy))
            if not _scroll(game, rows, dy, forward=True):
                break
        target = choose([c for page in pages for c in page], main, fill)
        if target is None:
            break
        # Back to the screen it's on, then give it while it stays in colour
        # (and stays the best choice).
        while _scroll(game, rows, dy, forward=False):
            pass
        for _ in range(PAGES):
            here = read_cards(game.refresh(), rows, pictures, dy)
            card = next((c for c in here if c.name == target.name and c.available
                         and (target.name or c.housing == target.housing)), None)
            if card or not _scroll(game, rows, dy, forward=True):
                break
        if card is None:
            break
        log.info("Donating %s", card.name or "an unknown unit")
        while given < limit:
            game.tap(*card.center)
            given += 1
            game.wait(0.4)
            if _title_dy(game) is None:
                return given
            here = read_cards(game.refresh(), rows, pictures, dy)
            card = next((c for c in here if c.name == target.name and c.available
                         and abs(c.x - card.x) < 20), None)
            if card is None or choose(here, main, fill) != card:
                break  # greyed out, or something better is on show now
    return given


def donate_open_window(game: Game, cfg: ClanConfig) -> int:
    """Fill the request whose donate window is open. Returns units given."""
    dy = _title_dy(game)
    if dy is None:
        return 0
    pictures = _pictures(str(Path(game.vision.library.root) / T.DONATE_ITEM_DIR))
    screen = game.refresh()
    if pays_with_gems(screen, read_cards(screen, TROOP_ROWS + SPELL_ROWS, pictures, dy)):
        log.warning("The donate window is set to pay with gems: not donating (switch it back to elixir)")
        return 0
    limit = min(cfg.max_donations_per_request, MAX_TAPS)
    troops = [n for n in cfg.donate_main if BY_NAME.get(n) and BY_NAME[n].kind in ("troop", "siege")]
    given = _give(game, TROOP_ROWS, troops, cfg.donate_fill, pictures, limit)
    if _title_dy(game) is not None and given < limit:
        # Spells: only the ones picked (filling is for troops).
        given += _give(game, SPELL_ROWS, cfg.donate_spells, False, pictures, limit - given)
    return given
