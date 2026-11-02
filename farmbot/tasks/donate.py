"""Donating to Clan Castle requests from the donate window.

The window (Donation Resource) shows two rows of troop cards, with the
siege machines at their end, and a row of spells; both scroll sideways,
but the bot only uses the screen the window opens on.
A card in colour can be donated, a grey one can't (it doesn't fit, it's
not unlocked, or there's no resource for it; when a request names units,
those come first in the window and are the only ones in colour). Each tap
gives one unit and costs the elixir or dark elixir on the card.

The bot gives the player's main troops first, in their order, while they're
in colour; then (if asked) fills what's left with the biggest troop that
still fits; then the siege machine and spells picked. With no main troops
picked, it gives the preset army of the requester's Clan Castle level,
read from the room the request's bubble shows (see farmbot/castle.py). It never
donates while the window is set to pay with gems.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from farmbot.castle import Castle, castle_for, preset
from farmbot.config import ClanConfig
from farmbot.game import Game, T
from farmbot.geometry import Rect
from farmbot.text import TextStyle, pieces
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
MAX_TAPS = 60  # taps on one request at most, whatever happens

# The request's bubble in chat, left of its Donate button: "0/55" troops,
# "0/3" spells, "1/2" siege machines, grey on dark grey. Their room is the
# requester's Clan Castle. (The window's own "Donate Troops: 0/25" is how
# much one player may give, not the castle.)
ROOM_X = {"troops": (-321, -241), "spells": (-206, -146), "siege": (-126, -71)}  # from the button's centre
ROOM_Y = (-14, 12)
ROOM_TEXT = TextStyle(max_saturation=50, min_value=100, min_area=4)
ROOM_HEIGHT = 14  # px: taller pieces are the icons, not text
ROOM_GAP = 4  # px: a wider gap ends a word; the number is the last one
ROOM_SCORE = 0.72  # a digit not learned yet scores about 0.65 against the nearest one
ROOM_FONT = "fonts/request"  # its "/" is slash.png
ROOM_SCREENS = Path("screens/donate_room")  # bubbles that couldn't be read, to learn their digits from


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
    ``fill``) the biggest troop in colour (units without a known housing
    last; siege machines only when picked), else none."""
    available = [c for c in cards if c.available]
    for name in main:
        for c in available:
            if c.name == name:
                return c
    troops = [c for c in available if not _siege(c)]
    if fill and troops:
        return max(troops, key=lambda c: c.housing)
    return None


def _siege(card: DonateCard) -> bool:
    return card.name in BY_NAME and BY_NAME[card.name].kind == "siege"


def _title_dy(game: Game) -> int | None:
    match = game.find(T.DONATE_WINDOW)
    return None if match is None else match.y - TITLE_Y


def read_room(screen: np.ndarray, font, button: tuple[int, int], kind: str) -> int | None:
    """The room for one kind ("troops", "spells", "siege") in the bubble of
    the request whose Donate button is at ``button``, or ``None`` if it
    can't be read."""
    (x0, x1), (y0, y1) = ROOM_X[kind], ROOM_Y
    region = Rect(button[0] + x0, button[1] + y0, x1 - x0, y1 - y0)
    word: list[np.ndarray] = []
    end = None
    for x, mask in pieces(screen, region, ROOM_TEXT):
        if mask.shape[0] > ROOM_HEIGHT:
            continue
        if end is not None and x - end > ROOM_GAP:
            word = []
        word.append(mask)
        end = x + mask.shape[1]
    # Only the room, after the "/", has to be read (what's given so far can
    # have any digit; the room is one of a few numbers).
    read = [font.classify(mask) for mask in word]
    slashes = [i for i, (char, score) in enumerate(read) if char == "slash" and score >= ROOM_SCORE]
    if not slashes or any(score < ROOM_SCORE for _, score in read[slashes[-1] + 1:]):
        return None
    room = "".join(char for char, _ in read[slashes[-1] + 1:])
    return int(room) if room.isdigit() else None


def read_castle(game: Game, screen: np.ndarray, button: tuple[int, int]) -> Castle | None:
    """The requester's Clan Castle, from the room its request's bubble shows."""
    try:
        font = game.vision.font(ROOM_FONT)
    except (FileNotFoundError, ValueError):
        return None
    troops, spells, siege = (read_room(screen, font, button, kind) for kind in ("troops", "spells", "siege"))
    castle = castle_for(troops, spells, siege) if troops is not None else None
    if castle is None:
        log.info("Couldn't read the Clan Castle's room (the screen is kept to learn its digits from)")
        from farmbot.tasks.village import _keep_screen

        _keep_screen(ROOM_SCREENS, screen)
        return None
    log.info("Clan Castle level %d: room for %d troops, %d spells, %d siege; units come at level %d at most",
             castle.level, castle.troops, castle.spells, castle.siege, castle.lab_cap)
    return castle


def _give(game: Game, rows, main: list[str], fill: bool, pictures, limit: int,
          counts: dict[str, int | None] | None = None) -> int:
    """Donate from one set of rows (troops or spells) until nothing more
    should or can be given; with ``counts``, at most that many of each main
    unit (``None``: as many as fit). Only the screen the window opens on is used: what a request asks
    for is there, in colour. Returns how many units were given."""
    left = {name: count for name, count in (counts or {}).items() if count is not None}

    def wanted() -> list[str]:
        return [n for n in main if left.get(n, 1) > 0]

    given = 0
    while given < limit:
        dy = _title_dy(game)
        if dy is None:
            break  # the window closed: the request is full
        card = choose(read_cards(game.refresh(), rows, pictures, dy), wanted(), fill)
        if card is None:
            break
        name = card.name
        log.info("Donating %s", name or "an unknown unit")
        # Give it while it stays in colour (and stays the best choice).
        while given < limit:
            game.tap(*card.center)
            given += 1
            if name in left:
                left[name] -= 1
            game.wait(0.4)
            if _title_dy(game) is None:
                return given
            here = read_cards(game.refresh(), rows, pictures, dy)
            card = next((c for c in here if c.name == name and c.available and abs(c.x - card.x) < 20), None)
            if card is None or choose(here, wanted(), fill) != card:
                break  # greyed out, given enough, or something better is on show now
    return given


def donate_open_window(game: Game, cfg: ClanConfig, request: np.ndarray | None = None,
                       button: tuple[int, int] | None = None) -> int:
    """Fill the request whose donate window is open. ``request`` is the
    chat before it opened and ``button`` its Donate button there (to read
    the castle's room from the bubble). Returns units given."""
    dy = _title_dy(game)
    if dy is None:
        return 0
    pictures = _pictures(str(Path(game.vision.library.root) / T.DONATE_ITEM_DIR))
    screen = game.refresh()
    if pays_with_gems(screen, read_cards(screen, TROOP_ROWS + SPELL_ROWS, pictures, dy)):
        log.warning("The donate window is set to pay with gems: not donating (switch it back to elixir)")
        return 0
    limit = MAX_TAPS
    # No main troops picked: the preset army of the requester's castle
    # level. A request that names its units shows only those in colour, so
    # the preset's others are passed over and the filling gives those.
    castle = None if cfg.donate_main or request is None or button is None else read_castle(game, request, button)
    plan = preset(cfg.donate_presets.get(str(castle.level), [])) if castle else {}

    def of(kind: str, picked: list[str]) -> list[str]:
        return [n for n in picked or list(plan) if BY_NAME.get(n) and BY_NAME[n].kind == kind]

    given = _give(game, TROOP_ROWS, of("troop", cfg.donate_main), cfg.donate_fill, pictures, limit, plan)
    siege = of("siege", cfg.donate_siege)
    if siege and _title_dy(game) is not None and given < limit:
        # Siege machines sit at the end of the troop rows: only the ones picked.
        given += _give(game, TROOP_ROWS, siege, False, pictures, limit - given, None if cfg.donate_siege else plan)
    spells = of("spell", cfg.donate_spells)
    if spells and _title_dy(game) is not None and given < limit:
        # Spells: only the ones picked (filling is for troops).
        given += _give(game, SPELL_ROWS, spells, False, pictures, limit - given, None if cfg.donate_spells else plan)
    return given
