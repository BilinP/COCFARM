"""What the bot sees on one screen: loot, troop cards and deploy points.

Used by ``farmbot inspect`` and the GUI's "What the bot sees" check, so a
new setup can be verified on a scouting screen without attacking.
"""

from __future__ import annotations

import cv2
import numpy as np

from farmbot import armybar, deployzone, redline
from farmbot.config import Config
from farmbot.game import Game
from farmbot.tasks.search import is_worth_attacking, own_resources, read_loot
from farmbot.vision import Vision

CARD_COLOURS = {"troop": (255, 200, 0), "spell": (255, 0, 255), "single": (0, 200, 255)}


class _Still:
    def __init__(self, screen: np.ndarray):
        self._screen = screen

    def screenshot(self) -> np.ndarray:
        return self._screen


def describe(screen: np.ndarray, cfg: Config, vision: Vision) -> tuple[list[str], np.ndarray]:
    """Lines describing what was found, and the screen with it drawn on."""
    # The screen is already normalized: keep it exactly as it is.
    game = Game(_Still(screen), vision, reference_height=screen.shape[0], ui_scale=1.0)
    lines = []
    loot = read_loot(game, cfg.regions)
    if loot:
        verdict = "attack" if is_worth_attacking(loot, cfg.search) else "skip"
        lines.append(f"Loot: gold {loot.gold:,}  elixir {loot.elixir:,}  dark {loot.dark:,}  → {verdict}")
    else:
        mine = own_resources(game, cfg.regions)
        lines.append(f"Home village: gold {mine.gold:,}  elixir {mine.elixir:,}  dark {mine.dark:,}" if mine
                     else "Loot: not readable. Show a base being scouted (or the home village).")
        lines.append("Troop cards and deploy points are checked while scouting a base.")
        return lines, screen.copy()
    cards = armybar.find_cards(screen, cfg.bar)
    lines.append(f"Troop bar: {len(cards)} card(s)" + (": " + ", ".join(c.kind for c in cards) if cards else ""))
    line = redline.detect(screen, cfg.redline) if cfg.redline.enabled else None
    zone = deployzone.detect(screen, cfg.zone) if cfg.zone.enabled else None
    for name, found in (("Red line", line), ("Building ring", zone)):
        lines.append(f"{name}: " + (", ".join(f"{s} {len(p)}" for s, p in found.sides.items()) if found else "not found"))
    overlay = redline.draw(screen, line or zone)
    for c in cards:
        cv2.rectangle(overlay, (c.left, c.top), (c.right, c.bottom), CARD_COLOURS[c.kind], 3)
        cv2.putText(overlay, c.kind, (c.left + 4, c.top - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, CARD_COLOURS[c.kind], 2)
    return lines, overlay
