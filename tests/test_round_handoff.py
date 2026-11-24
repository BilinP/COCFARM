from datetime import datetime
from types import SimpleNamespace
import pytest

from farmbot import bot as module
from farmbot.config import Config


def make_bot(monkeypatch):
    bot = module.Bot.__new__(module.Bot)
    bot.config = Config()
    bot.config.bot.max_attacks = 2
    bot.game = SimpleNamespace(device=None)
    bot.attacks, bot.gained, bot.errors = 0, None, 0
    bot._now = datetime.now
    bot._maintenance_since, bot.left_until, bot.storage_full = None, None, False
    for name in ['_check_display', '_log_device', 'checkpoint', '_deploy_alert', '_progress_update',
                 'take_break', 'status', '_rest_after_round', '_round_over', '_settle_attack',
                 '_save_error_screen', '_extra_failed']:
        setattr(bot, name, lambda *args: None)
    bot.pause_reason = lambda: None
    bot.playing = lambda: 'home'
    bot.session_fields = lambda: {}
    bot._diagnostics = lambda: ''
    bot.notifier = SimpleNamespace(send=lambda *a, **k: None, alert=lambda *a, **k: None)
    monkeypatch.setattr(module.language, 'warn_once', lambda *a: None)
    return bot


def test_round_callback_runs_after_cycle_not_mid_battle(monkeypatch):
    bot = make_bot(monkeypatch)
    seen = []
    def cycle():
        seen.append('battle')
        bot.attacks += 1
        seen.append('home')
        return True
    bot.cycle = cycle
    bot._on_round = lambda: seen.append('handoff')
    bot.run()
    assert seen == ['battle', 'home', 'handoff', 'battle', 'home']
    assert bot.attacks == 2


def test_offline_wait_uses_callback_or_original_rest():
    bot = module.Bot.__new__(module.Bot)
    seen = []
    bot._rest = lambda seconds: seen.append(('resident', seconds))
    bot._on_idle = lambda seconds: seen.append(('offline', seconds)) or True
    bot._wait_offline(600)
    assert seen == [('offline', 600)]
    bot._on_idle = None
    bot._wait_offline(30)
    assert seen[-1] == ('resident', 30)


@pytest.mark.parametrize('builder', [False, True])
def test_ownership_hold_from_recovery_is_fatal_not_another_round(monkeypatch, builder):
    bot = make_bot(monkeypatch)
    bot._on_round = None
    if builder:
        bot.config.bot.mode = 'auto'
        bot.playing = lambda: 'builder'
    calls = []
    def cycle():
        calls.append('failed cycle')
        raise RuntimeError('simulated failed round')
    def recover():
        raise module.instances.FarmHeld('native ownership changed')
    bot.cycle, bot.recover = cycle, recover
    with pytest.raises(module.instances.FarmHeld):
        bot.run()
    assert calls == ['failed cycle']
