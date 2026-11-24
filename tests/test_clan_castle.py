"""Portable, synthetic troop-bar checks: no game artwork or player data."""
import itertools
import random
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import unittest

from farmbot import armybar
from farmbot.config import Config
from farmbot.tasks import attack


def bar(emblem=(255, 255, 255)):
    cfg = Config()
    cfg.bar.band = [0, 10, 0, 110]
    cfg.human.enabled = False
    screen = np.full((150, 580, 3), 25, dtype=np.uint8)
    # A counted troop, a banner, a levelled Siege Machine, a hero, and a
    # dashed empty slot. Colours and rectangles are invented test data.
    for index, hue in enumerate((110, 94, 94, 110, 94)):
        x = 20 + index * 110
        colour = cv2.cvtColor(np.uint8([[[hue, 230, 235]]]), cv2.COLOR_HSV2BGR)[0, 0]
        screen[10:120, x:x + 94] = colour
        if index in (0, 2, 3):
            screen[96:103, x + 10:x + 18] = 255  # bottom-left level badge
        if index == 0:
            screen[12:25, x + 64:x + 82] = 255  # top-right count
        if index == 1:
            screen[40:105, x + 28:x + 66] = emblem
        if index == 4:
            for y in range(10, 120, 20):
                screen[y:y + 7, x:x + 94] = 25
    return cfg, screen


def check_banner_ignores_emblem_and_dashed_slot(emblem):
    cfg, screen = bar(emblem)
    cards = armybar.find_cards(screen, cfg.bar)
    assert [c.kind for c in cards] == ["troop", "castle", "siege", "single"]
    spent = cv2.cvtColor(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert armybar.find_cards(spent, cfg.bar) == []


def check_siege_switch_does_not_disable_banner():
    cfg, screen = bar()
    game = SimpleNamespace(refresh=lambda: screen)
    cfg.attack.use_siege = False
    assert [c.kind for c in attack._bar_cards(game, cfg)] == ["troop", "castle", "single"]
    cfg.attack.use_siege = True
    cards = attack._bar_cards(game, cfg)
    assert [c.kind for c in cards] == ["troop", "castle", "siege", "single"]
    assert [c.kind for c in attack.abilities_to_fire(cards)] == ["single"]


def check_deployment_checks_spent_card(accepts, kind):
    cfg, screen = bar()
    cfg.attack.use_siege = True
    card = next(c for c in armybar.find_cards(screen, cfg.bar) if c.kind == kind)

    class Transport:
        """Simulated transport; an accepted map tap greys the synthetic card."""
        def __init__(self):
            self.rng = random.Random(0)
            self.screen = screen.copy()
            self.map_taps = 0

        def refresh(self):
            return self.screen

        def tap(self, x, y, **kwargs):
            if (x, y) != card.center:
                self.map_taps += 1
                if accepts:
                    r = card.rect
                    roi = self.screen[r.y:r.y + r.h, r.x:r.x + r.w]
                    roi[:] = cv2.cvtColor(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)

        def wait(self, *args, **kwargs):
            pass

        def note(self, *args, **kwargs):
            pass

        def has(self, *args, **kwargs):
            return False

    game = Transport()
    placed = attack.place_singles(game, [card], itertools.repeat((20, 140)), cfg)
    assert placed == ([card] if accepts else [])
    assert game.map_taps == (1 if accepts else attack.SINGLE_TRIES)
    if accepts:
        assert all(c.kind != kind for c in attack._bar_cards(game, cfg))
        assert all(c.kind != kind for c in attack._placed_on_show(game, cfg))


def check_banner_and_siege_use_single_deployment(kind):
    cfg, screen = bar()
    card = next(c for c in armybar.find_cards(screen, cfg.bar) if c.kind == kind)
    game = SimpleNamespace()
    points = iter([(20, 140)])
    with patch.object(attack, "place_single", return_value=True) as place:
        assert attack.deploy_card(game, card, points, cfg) is True
    place.assert_called_once_with(game, card, points, cfg, None)


class ClanCastleTests(unittest.TestCase):
    def test_banner_without_other_cards_or_siege(self):
        cfg, screen = bar()
        screen[:, :129] = 25
        screen[:, 224:] = 25
        cfg.attack.use_siege = False
        game = SimpleNamespace(refresh=lambda: screen)
        assert [c.kind for c in attack._bar_cards(game, cfg)] == ["castle"]

    def test_emblems_and_dashed_slot(self):
        for emblem in [(0, 0, 0), (255, 255, 255), (0, 255, 255)]:
            with self.subTest(emblem=emblem):
                check_banner_ignores_emblem_and_dashed_slot(emblem)

    def test_siege_switch_and_abilities(self):
        check_siege_switch_does_not_disable_banner()

    def test_deployment_success_and_refusal(self):
        for accepts, kind in itertools.product((True, False), ("castle", "siege")):
            with self.subTest(accepts=accepts, kind=kind):
                check_deployment_checks_spent_card(accepts, kind)

    def test_single_deployment_dispatch(self):
        for kind in ("castle", "siege"):
            with self.subTest(kind=kind):
                check_banner_and_siege_use_single_deployment(kind)
