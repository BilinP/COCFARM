from datetime import datetime
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from farmbot import screens, text, townhall
from farmbot.bot import Bot
from farmbot.config import BuilderConfig, Config
from farmbot.tasks.search import Loot
from farmbot.tasks import builder
def synthetic_cards(counts=(3, 3), selected=None):
    """Generated textures and OpenCV lettering; no game image or digit assets."""
    masks = {}
    for char in "23x":
        canvas = np.zeros((32, 32), np.uint8)
        cv2.putText(canvas, char, (2, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 255, 2,
                    lineType=cv2.LINE_8)
        canvas = np.where(canvas >= 128, 255, 0).astype(np.uint8)
        x, y, w, h = cv2.boundingRect(cv2.findNonZero(canvas))
        masks[char] = canvas[y:y + h, x:x + w]
    font = text.Font([text.Glyph.from_mask(char, mask) for char, mask in masks.items()])
    vision = SimpleNamespace(font=lambda name: font)
    screen = np.full((920, 1177, 3), 30, np.uint8)
    for i, (x, count) in enumerate(zip((81, 185), counts)):
        offset = -8 if i == selected else 0
        screen[820:860, x-30:x+30] = np.random.default_rng(i).integers(20, 180, (40, 60, 3), dtype=np.uint8)
        header = np.full((28, 85, 3), (111, 191, 255), np.uint8)
        screen[788+offset:816+offset, x-40:x+45] = cv2.cvtColor(header, cv2.COLOR_HSV2BGR)
        left = x + 8
        digit = masks[str(count)]
        for mask in (digit, masks["x"]):
            top = 792 + offset + digit.shape[0] - mask.shape[0]
            patch = screen[top:top+mask.shape[0], left:left+mask.shape[1]]
            patch[mask > 0] = 255
            left += mask.shape[1] + 3
    return screen, vision


class OrdinaryCards(unittest.TestCase):
    def test_builder_cycle_loads_cached_hero_metadata_from_either_village(self):
        class AtAttack(Exception):
            pass
        for start in (screens.Screen.BUILDER, screens.Screen.HOME):
            for heroes in (0,1):
                with self.subTest(start=start, heroes=heroes), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'village.json'
                    exported = {'buildings':[{'data':townhall.TOWN_HALL,'lvl':8}],
                                'buildings2':[{'data':1000034,'lvl':3}],
                                'heroes2':[{'data':4000012,'lvl':1}] * heroes}
                    townhall.save(path, json.dumps(exported), datetime.now().date().isoformat())
                    cfg = Config()
                    cfg.bot.mode = 'builder'
                    cfg.human.enabled = False
                    cfg.builder.collect_resources = cfg.builder.collect_cart = cfg.builder.clock_tower = False
                    cfg.builder.upgrade = cfg.builder.walls = False
                    game = SimpleNamespace(rng=random.Random(0), ledger=Mock(), uses=lambda name:False,
                                           clock=lambda:0, zoom_out=Mock(), refresh=Mock())
                    bot = Bot(game, cfg, village_path=lambda name:path, notifier=Mock())
                    bot.read_storage_max = Mock()
                    bot.read_resources = Mock(return_value=None)
                    bot._centred = Mock()
                    self.assertIsNone(bot.village)
                    with patch('farmbot.bot.screens.settle', return_value=start), \
                         patch('farmbot.bot.screens.identify', return_value=start), \
                         patch.object(builder, 'go_to_builder_base'), \
                         patch.object(builder, 'back_to_first_base'), \
                         patch('farmbot.bot.village.clear_popups'), \
                         patch('farmbot.bot.export.copy_village') as copy_export, \
                         patch.object(builder, 'attack', side_effect=AtAttack) as attack:
                        with self.assertRaises(AtAttack):
                            bot.builder_cycle()
                    self.assertIsNotNone(bot.village)
                    self.assertEqual(bot.village.builder_heroes, heroes)
                    used = attack.call_args.args[1]
                    self.assertEqual(used.use_hero, bool(heroes))
                    self.assertEqual(used.first_troop_x, 185 if heroes else 81)
                    self.assertEqual(cfg.builder.first_troop_x, 185)
                    copy_export.assert_not_called()

    def test_deploys_every_unit_in_counted_cards(self):
        self.check_deploy(builder.without_hero(BuilderConfig()), [3, 3], False)

    def test_hero_present_deployment_is_preserved(self):
        self.check_deploy(BuilderConfig(switch_hero=False), [1, 1], True)

    def test_first_single_troop_is_kept_for_abilities(self):
        self.check_deploy(builder.without_hero(BuilderConfig()), [1, 1], False)

    def test_unreadable_after_first_tap_waits_for_result_without_replay(self):
        for consume in (False, True):
            with self.subTest(first_tap_consumed=consume):
                self.check_deploy(builder.without_hero(BuilderConfig()), [3,3], False,
                                  uncertain=True, consume=consume)

    def test_initial_unknown_count_sends_no_card_or_map_input(self):
        self.check_deploy(builder.without_hero(BuilderConfig()), [3,3], False,
                          uncertain=True, initial_unknown=True)

    def test_transient_initial_unknown_count_stops_before_later_read(self):
        self.check_deploy(builder.without_hero(BuilderConfig()), [3,3], False,
                          uncertain=True, initial_unknown=True, transient_unknown=True)

    def test_normal_runner_never_recovers_or_readmits_uncertain_battle(self):
        for consume in (False, True):
            with self.subTest(first_tap_consumed=consume):
                self.check_deploy(builder.without_hero(BuilderConfig()), [3,3], False,
                                  uncertain=True, consume=consume, run_bot=True)

    def check_deploy(self, cfg, counts, hero, uncertain=False, consume=True, initial_unknown=False, run_bot=False, transient_unknown=False):
        class Game:
            def __init__(self):
                self.left = counts.copy()
                self.selected = None
                self.hero_landed = False
                self.drops = 0
                self.map_inputs = 0
                self.events = []
                self.device = object()
                self.rng = random.Random(0)
                self.vision = object()
                self.time = 0
                self.refresh()

            def refresh(self):
                self.screen = np.zeros((920, 1177, 3), np.uint8)
                for i, n in enumerate(self.left):
                    x = builder.slot_x(i + 1, cfg)
                    self.screen[820:860, x-30:x+30] = np.random.default_rng(i).integers(0, 256, (40,60,3), dtype=np.uint8)
                    hsv = (145,190,245) if n else (50,190,245)
                    self.screen[796:812,x-30:x+30] = cv2.cvtColor(np.full((16,60,3),hsv,np.uint8),cv2.COLOR_HSV2BGR)
                return self.screen

            def tap(self, x, y, **kw):
                if y == cfg.card_y:
                    if hero and x == cfg.hero_x:
                        self.selected = 'hero'
                    else:
                        self.selected = min(range(len(self.left)), key=lambda i: abs(builder.slot_x(i+1,cfg)-x))
                elif self.selected == 'hero':
                    self.hero_landed = True
                elif self.selected is not None:
                    self.map_inputs += 1
                    if self.left[self.selected] and consume:
                        self.left[self.selected] -= 1
                        self.drops += 1
                    if not self.left[self.selected] and self.selected + 1 < len(self.left):
                        self.selected += 1

            def tap_many(self, points, **kw):
                for x,y in points: self.tap(x,y)
            def wait(self, seconds, **kw): self.time += seconds
            def clock(self): return self.time
            def uses(self, name): return False
            def note(self, *args): pass
            def zoom_out(self, **kw): pass
            def look_around(self): pass
            def stop_fast_taps(self): pass
            def wait_for(self, *args, **kw):
                self.events.append('natural_result')
                return object()

        game = Game()
        reads = 0
        def read_count(screen, slot, cfg, vision):
            nonlocal reads
            reads += 1
            if initial_unknown and (not transient_unknown or reads == 1):
                return None
            if uncertain and not initial_unknown and game.map_inputs:
                return None
            return game.left[slot.index-1]
        with patch.object(builder, 'pick_corner', return_value=('left',(200,468))), \
             patch.object(builder, 'base_centre', return_value=(590,450)), \
             patch.object(builder, 'hero_out', side_effect=lambda *args: game.hero_landed), \
             patch.object(builder, 'troop_count', side_effect=read_count):
            if transient_unknown:
                with self.assertRaisesRegex(builder.DeploymentCountError, 'count unreadable'):
                    builder.deploy_stage(game, cfg)
                self.assertIsNone(game.selected)
                self.assertEqual(game.map_inputs, 0)
                self.assertEqual(reads, 1)
                return
            if uncertain:
                with patch.object(builder, 'start_battle'), \
                     patch.object(builder, '_finish', side_effect=lambda *args: game.events.append('returned_home')), \
                     patch.object(builder, '_fire_abilities', side_effect=AssertionError('ability after uncertainty')):
                    call = lambda: builder.attack(game, cfg, on_deployment_error=lambda: game.events.append('original_frame'))
                    if run_bot:
                        bot = Bot.__new__(Bot)
                        bot.config = Config()
                        bot.config.bot.max_attacks = 1
                        bot.config.bot.mode = 'builder'
                        bot.game, bot.attacks, bot.errors = game, 0, 0
                        bot.gained = Loot(0,0,0)
                        bot._now = datetime.now
                        bot.notifier = Mock()
                        for name in ('_check_display','_log_device','checkpoint','_save_error_screen',
                                     'take_break','status','_rest_after_round','_round_over'):
                            setattr(bot, name, Mock())
                        bot.pause_reason = lambda: None
                        bot.playing = lambda: 'builder'
                        bot._diagnostics = lambda: 'synthetic test'
                        bot.session_fields = lambda: []
                        bot.recover = Mock()
                        bot.cycle = Mock(side_effect=call)
                        caught = None
                        with patch('farmbot.bot.language.warn_once'), self.assertLogs(level='ERROR'):
                            try: bot.run()
                            except Exception as exc: caught = exc
                        self.assertEqual(bot.cycle.call_count, 1)
                        bot.recover.assert_not_called()
                        self.assertIsInstance(caught, builder.DeploymentCountError)
                        self.assertEqual(bot.attacks, 0)
                    else:
                        with self.assertLogs(builder.log, level='ERROR'), self.assertRaisesRegex(builder.BuilderError, 'count unreadable'):
                            call()
                self.assertEqual(game.map_inputs, 0 if initial_unknown else 1)
                if initial_unknown:
                    self.assertIsNone(game.selected)
                self.assertEqual(game.events, ['original_frame','natural_result','returned_home'])
                return
            _, sent = builder.deploy_stage(game, cfg)
        self.assertEqual(game.left, [0,0])
        self.assertEqual(game.drops, sum(counts))
        self.assertEqual(game.hero_landed, hero)
        self.assertEqual([s.index for s in sent], [1, 2])

    def test_blue_card_is_ready(self):
        screen, _ = synthetic_cards()
        self.assertEqual(builder.card_state(screen, 185, BuilderConfig()), "ready")

    def test_selected_card_count_is_not_clipped(self):
        screen, vision = synthetic_cards((2, 3), selected=0)
        cfg = builder.without_hero(BuilderConfig())
        slots = builder.ready_troops(screen, cfg)
        self.assertEqual([s.x for s in slots], [81, 185])
        self.assertEqual([builder.troop_count(screen, s, cfg, vision) for s in slots], [2, 3])

    def test_blue_card_without_readable_count_is_not_guessed(self):
        _, vision = synthetic_cards()
        screen = np.zeros((920, 1177, 3), np.uint8)
        screen[796:812,155:215] = cv2.cvtColor(np.full((16,60,3),(111,191,255),np.uint8),cv2.COLOR_HSV2BGR)
        self.assertIsNone(builder.troop_count(screen, builder.Slot(1,185), BuilderConfig(), vision))

    def test_no_hero_counted_cards(self):
        screen, vision = synthetic_cards()
        cfg = builder.without_hero(BuilderConfig())
        slots = builder.ready_troops(screen, cfg)
        self.assertEqual([s.x for s in slots], [81, 185])
        self.assertEqual([builder.troop_count(screen, s, cfg, vision) for s in slots], [3, 3])

    def test_locked_hero_changes_only_battle_layout(self):
        cfg = BuilderConfig()
        fixed = builder.without_hero(cfg)
        self.assertFalse(fixed.use_hero)
        self.assertFalse(fixed.hero_ability)
        self.assertEqual(fixed.first_troop_x, 81)
        self.assertTrue(cfg.use_hero)
        self.assertEqual(cfg.first_troop_x, 185)

    def test_existing_card_states(self):
        cfg = BuilderConfig()
        rng = np.random.default_rng(1)
        screen = rng.integers(0, 256, (920, 1177, 3), dtype=np.uint8)
        for hsv, state in [((145, 190, 245), 'ready'), ((112, 190, 120), 'ready'),
                           ((50, 190, 245), 'out'), ((0, 0, 180), 'dead')]:
            band = np.full((16, 60, 3), hsv, np.uint8)
            screen[796:812, 155:215] = cv2.cvtColor(band, cv2.COLOR_HSV2BGR)
            self.assertEqual(builder.card_state(screen, 185, cfg), state)
        screen[:] = 30
        self.assertEqual(builder.ready_troops(screen, cfg), [])


if __name__ == '__main__':
    unittest.main()
