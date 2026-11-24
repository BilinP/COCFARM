import queue
import threading
import time
from types import SimpleNamespace

import pytest

from farmbot import bluestacks, claims, gui, instances
from farmbot.config import Config
from farmbot.device import DeviceError


def eventually(check):
    end = time.monotonic() + 3
    while not check():
        assert time.monotonic() < end
        time.sleep(.005)


@pytest.fixture
def world(monkeypatch):
    instances.active.clear()
    resident, opened, closed, bots = set(), [], [], []
    threads = []
    failure = set()
    gate = threading.Event()
    maximum = [0]
    monkeypatch.setattr(instances, 'instance_key', lambda cfg: cfg.bluestacks_instance)
    monkeypatch.setattr(claims, 'claim', lambda *args: None)
    monkeypatch.setattr(claims, 'release', lambda *args: None)
    monkeypatch.setattr(bluestacks, 'emulator_identity',
                        lambda cfg: (int(cfg.bluestacks_instance.split('-')[-1]),)
                        if cfg.bluestacks_instance in resident else ())

    def open_device(cfg):
        resident.add(cfg.bluestacks_instance)
        opened.append(cfg.bluestacks_instance)
        maximum[0] = max(maximum[0], len(resident))
        return object()

    def close_device(cfg, **kwargs):
        closed.append(cfg.bluestacks_instance)
        if cfg.bluestacks_instance in failure:
            raise DeviceError('native exit unknown')
        resident.remove(cfg.bluestacks_instance)

    monkeypatch.setattr(bluestacks, 'open_device_retrying', open_device)
    monkeypatch.setattr(bluestacks, 'close_emulator', close_device)
    monkeypatch.setattr(bluestacks, 'restart_emulator', lambda cfg: (close_device(cfg), open_device(cfg))[1])
    monkeypatch.setattr('farmbot.vision.TemplateLibrary', lambda *args: None)
    monkeypatch.setattr('farmbot.vision.Vision', lambda *args: None)

    class Game:
        def __init__(self, device, vision, stop_event, **kwargs):
            self.device, self.stop_event, self.screen = device, stop_event, None

    monkeypatch.setattr('farmbot.game.Game', Game)

    class Bot:
        def __init__(self, game, config, **kwargs):
            self.game, self.config, self.callbacks = game, config, kwargs
            self.left_until, self.rounds = None, 0
            bots.append(self)

        def _check_display(self): pass
        def ensure_game_running(self): pass
        def pause_reason(self): return None

        def run(self):
            said = False
            while not gate.is_set() and not self.game.stop_event.is_set():
                if self.callbacks['pause_event'].is_set() and not said:
                    self.callbacks['on_status']('Paused by you: press Resume to carry on')
                    said = True
                self.game.stop_event.wait(.005)
            while not self.game.stop_event.is_set() and self.rounds < 2:
                self.rounds += 1
                if self.rounds == 2:
                    self.game.stop_event.set()
                callback = self.callbacks.get('on_round')
                if callback:
                    callback()

    monkeypatch.setattr('farmbot.bot.Bot', Bot)
    yield SimpleNamespace(**locals())
    gate.set()
    for stop, thread in threads:
        stop.set()
        thread.join(3)
    instances.active.clear()
    instances.connected.clear()
    instances.accounts_playing.clear()
    instances.hooks.clear()


def worker(n, hub, world, target=None, wait_restart=None, mode="bluestacks"):
    config = Config()
    config.device.mode = mode
    config.device.bluestacks_instance = target or f'instance-{n}'
    config.device.launch_emulator = True
    stop = threading.Event()
    window = SimpleNamespace(farm=n, hub=hub, config=config, separate=False,
                             events=queue.Queue(), pause_event=threading.Event(),
                             _account_from_bot=lambda *a: None, village_file=lambda *a: None,
                             _wait_to_restart=wait_restart or (lambda *a: False))
    def run():
        instances.mark(n)
        gui.FarmbotWindow._run_bot(window, stop)
    thread = threading.Thread(target=run)
    world.threads.append((stop, thread))
    thread.start()
    return window, stop, thread


def test_worker_admission_rotates_without_replacing_bot(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(2))
    workers = [worker(n, hub, world) for n in range(1, 5)]
    eventually(lambda: len(hub.farm_queue.waiting) == 2)
    assert len(world.resident) == 2
    initial = set(world.resident)
    queued = {f'instance-{n}' for n in hub.farm_queue.waiting}
    world.gate.set()
    for _, _, thread in workers:
        thread.join(3)
        assert not thread.is_alive()
    assert world.maximum[0] == 2
    assert not world.resident
    assert len(world.bots) == 4
    assert all(bot.rounds == 2 for bot in world.bots)
    assert set(world.opened[:2]) == initial
    assert set(world.opened[2:4]) == queued


def test_unknown_cleanup_keeps_slot_and_queued_cancel_never_opens(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    world.failure.add('instance-1')
    first = worker(1, hub, world)
    eventually(lambda: world.resident == {'instance-1'})
    second = worker(2, hub, world)
    eventually(lambda: hub.farm_queue.waiting == [2])
    world.gate.set()
    first[2].join(3)
    assert 1 in hub.farm_queue.held
    assert world.opened == ['instance-1']
    second[1].set()
    second[2].join(3)
    assert not second[2].is_alive()
    assert world.opened == ['instance-1']


def test_external_player_is_not_adopted_or_closed(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    world.resident.add('instance-1')
    _, _, thread = worker(1, hub, world)
    thread.join(3)
    assert not thread.is_alive()
    assert not world.opened and not world.closed
    assert 1 in hub.farm_queue.held


def test_uncapped_preserves_existing_emulator_after_stop(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(0))
    _, _, thread = worker(1, hub, world)
    world.gate.set()
    thread.join(3)
    assert not thread.is_alive()
    assert world.opened == ['instance-1']
    assert not world.closed


def test_retry_after_failed_close_checks_original_target(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    world.failure.add('instance-1')
    _, _, thread = worker(1, hub, world)
    world.gate.set()
    thread.join(3)
    assert 1 in hub.farm_queue.held
    # A changed farm selection cannot retire the old, still-open player.
    original = instances.active[1]
    original_name = original.bluestacks_instance
    assert original_name == 'instance-1'
    window, stop, thread = worker(1, hub, world, target='instance-9')
    thread.join(3)
    assert 1 in hub.farm_queue.held and not thread.is_alive()
    assert world.opened == ['instance-1']


def test_replaced_native_player_is_not_closed(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    current = [1]
    monkeypatch.setattr(bluestacks, 'emulator_identity',
                        lambda cfg: (current[0],) if cfg.bluestacks_instance in world.resident else ())
    _, _, thread = worker(1, hub, world)
    eventually(lambda: bool(world.bots))
    current[0] = 99
    world.gate.set()
    thread.join(3)
    assert 1 in hub.farm_queue.held
    assert not world.closed


def test_retry_releases_closed_emulator_and_readmits_through_cap(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    attempts = []
    original = world.Bot.run
    def run(bot):
        attempts.append(bot.config.device.bluestacks_instance)
        if len(attempts) == 1:
            raise DeviceError('retryable simulated error')
        original(bot)
    monkeypatch.setattr(world.Bot, 'run', run)
    monkeypatch.setattr(gui, 'restart_wait', lambda *a: 0)
    retrying, resume = threading.Event(), threading.Event()
    def restart_wait(*args):
        retrying.set()
        return resume.wait(2)
    _, _, first = worker(1, hub, world, wait_restart=restart_wait)
    assert retrying.wait(2)
    _, _, second = worker(2, hub, world)
    eventually(lambda: world.resident == {'instance-2'})
    resume.set()
    eventually(lambda: hub.farm_queue.waiting == [1])
    world.gate.set()
    for thread in [first, second]:
        thread.join(3)
        assert not thread.is_alive()
    assert attempts.count('instance-1') == 2
    assert world.maximum == [1]
    assert not world.resident
    assert hub.farm_queue.active == {}


def test_idle_wait_releases_and_reconnects_same_bot(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    original = world.Bot.run
    def run(bot):
        assert bot.callbacks['on_idle'](0)
        original(bot)
    monkeypatch.setattr(world.Bot, 'run', run)
    _, _, thread = worker(1, hub, world)
    world.gate.set()
    thread.join(3)
    assert not thread.is_alive()
    assert len(world.bots) == 1 and world.bots[0].rounds == 2
    assert world.opened == ['instance-1', 'instance-1']
    assert not world.resident


def test_native_move_collision_never_closes_other_farms_player(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(2))
    first = worker(1, hub, world)
    eventually(lambda: world.resident == {'instance-1'})
    original = world.open_device
    def moved(cfg):
        if cfg.bluestacks_instance == 'instance-2':
            cfg.bluestacks_instance = 'instance-1'
            return object()
        return original(cfg)
    monkeypatch.setattr(bluestacks, 'open_device_retrying', moved)
    _, _, second = worker(2, hub, world)
    second.join(3)
    assert not second.is_alive() and 2 in hub.farm_queue.held
    assert not world.closed and world.resident == {'instance-1'}
    world.gate.set()
    first[2].join(3)


def test_watchdog_reopens_known_crashed_player_under_same_permit(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    original = world.Bot.run
    def run(bot):
        world.resident.remove(bot.config.device.bluestacks_instance)
        bot.callbacks['reopen_device']()
        original(bot)
    monkeypatch.setattr(world.Bot, 'run', run)
    _, _, thread = worker(1, hub, world)
    world.gate.set()
    thread.join(3)
    assert not thread.is_alive() and not hub.farm_queue.held
    assert world.opened == ['instance-1', 'instance-1']
    assert world.maximum == [1]


def test_watchdog_unknown_inventory_holds_without_restart(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    # A separate identity callable lets its behavior change after connection.
    unknown = [False]
    def identity(cfg):
        if unknown[0]:
            raise DeviceError('unreadable native inventory')
        return (1,) if cfg.bluestacks_instance in world.resident else ()
    monkeypatch.setattr(bluestacks, 'emulator_identity', identity)
    def run(bot):
        unknown[0] = True
        bot.callbacks['reopen_device']()
    monkeypatch.setattr(world.Bot, 'run', run)
    _, _, thread = worker(1, hub, world)
    thread.join(3)
    assert not thread.is_alive() and 1 in hub.farm_queue.held
    assert world.opened == ['instance-1'] and not world.closed


def test_other_emulators_keep_uncapped_behavior(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    _, _, thread = worker(1, hub, world, mode='ldplayer')
    eventually(lambda: bool(world.bots))
    assert hub.farm_queue.active == {}
    world.gate.set()
    thread.join(3)
    assert not thread.is_alive()
    assert not world.closed


def test_unset_limit_avoids_new_native_inventory_checks(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(0))
    calls = []
    monkeypatch.setattr(bluestacks, 'emulator_identity', lambda cfg: calls.append(cfg) or ())
    _, _, thread = worker(1, hub, world)
    world.gate.set()
    thread.join(3)
    assert not thread.is_alive()
    assert calls == []
    assert not world.closed


def test_failed_launch_releases_only_after_verified_absence(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    original = world.open_device
    failed = threading.Event()
    def open_device(cfg):
        if cfg.bluestacks_instance == 'instance-1':
            failed.set()
            raise DeviceError('simulated launch failed without a native player')
        return original(cfg)
    monkeypatch.setattr(bluestacks, 'open_device_retrying', open_device)
    _, _, first = worker(1, hub, world)
    assert failed.wait(2)
    first.join(3)
    assert not first.is_alive()
    assert not hub.farm_queue.active and not hub.farm_queue.held
    _, _, second = worker(2, hub, world)
    eventually(lambda: world.resident == {'instance-2'})
    world.gate.set()
    second.join(3)
    assert not second.is_alive()
    assert world.maximum == [1]


def test_failed_launch_with_unowned_player_holds_slot(world, monkeypatch):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    def open_device(cfg):
        world.resident.add(cfg.bluestacks_instance)
        raise DeviceError('launch failed after an unidentified player appeared')
    monkeypatch.setattr(bluestacks, 'open_device_retrying', open_device)
    _, _, first = worker(1, hub, world)
    first.join(3)
    assert not first.is_alive() and 1 in hub.farm_queue.held
    assert not world.closed
    _, stop, second = worker(2, hub, world)
    eventually(lambda: hub.farm_queue.waiting == [2])
    assert world.resident == {'instance-1'}
    stop.set()
    second.join(3)
    assert not second.is_alive()


def test_paused_resident_keeps_slot_until_owned_exit(world):
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    first, stop, thread = worker(1, hub, world)
    eventually(lambda: bool(world.bots))
    first.pause_event.set()
    _, _, second = worker(2, hub, world)
    eventually(lambda: hub.farm_queue.waiting == [2])
    assert world.resident == {'instance-1'}
    assert hub.farm_queue.active == {1: 'instance-1'}
    stop.set()
    thread.join(3)
    eventually(lambda: world.resident == {'instance-2'})
    world.gate.set()
    second.join(3)
    assert not second.is_alive() and world.maximum == [1]
