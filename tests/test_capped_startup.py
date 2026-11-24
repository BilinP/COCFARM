"""Real capped worker/opener flow with synthetic native and ADB boundaries."""
from pathlib import Path
import threading

from farmbot import bluestacks, instances
from farmbot.device import DeviceError
from types import SimpleNamespace
from test_farm_worker import world, worker


REAL_OPEN = bluestacks.open_device
REAL_RETRY = bluestacks.open_device_retrying


def real_startup(world, monkeypatch, *, never_ready=False, block_poll=False, on_connect=None):
    install = bluestacks.Install(Path('BlueStacks'), Path('data'), True,
        [bluestacks.Instance('instance-1', 'First', 5555),
         bluestacks.Instance('instance-2', 'Second', 5565)])
    elapsed = [0.0]
    polling, release = threading.Event(), threading.Event()
    launched = []
    legacy_closes = []
    monkeypatch.setattr(bluestacks, 'settings_to_change', lambda *a: {})
    monkeypatch.setattr(bluestacks, 'other_ports', None)
    # This models a null-command-line other player being mistaken for main.
    monkeypatch.setattr(bluestacks, 'is_running', lambda *a: bool(world.resident))

    def launch(_, instance):
        launched.append(instance.name)
        world.resident.add(instance.name)
        world.opened.append(instance.name)
        return 1

    monkeypatch.setattr(bluestacks, 'launch', launch)

    class Device:
        def __init__(self, *args): pass

        def connect(self):
            polling.set()
            if block_poll:
                assert release.wait(2)
            if never_ready or 'instance-1' not in world.resident:
                raise DeviceError('synthetic ADB not ready')
            if on_connect is not None:
                on_connect()

        def shell(self, *args):
            return 'ok'

    def open_device(cfg, **kwargs):
        kwargs.pop('sleep', None)
        kwargs.setdefault('start', launch)
        return REAL_OPEN(cfg, find=lambda: install, make=Device,
                         clock=lambda: elapsed[0], sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s),
                         **kwargs)

    monkeypatch.setattr(bluestacks, 'open_device', open_device)
    monkeypatch.setattr(bluestacks, 'open_device_retrying',
                        lambda cfg: REAL_RETRY(cfg, opener=open_device,
                            closer=lambda cfg, *a: legacy_closes.append(cfg.bluestacks_instance)))
    return SimpleNamespace(**locals())


def test_capped_main_starts_when_only_other_player_is_resident(world, monkeypatch):
    setup = real_startup(world, monkeypatch)
    world.resident.add('instance-2')
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(2))
    world.gate.set()
    _, _, thread = worker(1, hub, world)
    thread.join(3)
    assert not thread.is_alive()
    assert setup.launched == ['instance-1']
    assert setup.legacy_closes == []
    assert world.resident == {'instance-2'}
    assert world.closed == ['instance-1']


def test_stop_all_during_real_startup_closes_owned_player_without_retry(world, monkeypatch):
    setup = real_startup(world, monkeypatch, never_ready=True, block_poll=True)
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    window, _, thread = worker(1, hub, world)
    assert setup.polling.wait(2)
    # Stop All first requests pause; it must also cancel startup before a Bot exists.
    window.pause_event.set()
    setup.release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert setup.launched == ['instance-1']
    assert setup.legacy_closes == []
    assert world.closed == ['instance-1']
    assert not world.resident and not world.bots
    assert not hub.farm_queue.active and not hub.farm_queue.held


def test_capped_timeout_uses_outer_retry_boundary_not_legacy_inner_retry(world, monkeypatch):
    setup = real_startup(world, monkeypatch, never_ready=True)
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    _, _, thread = worker(1, hub, world)
    thread.join(3)
    assert not thread.is_alive()
    assert setup.launched == ['instance-1']
    assert setup.legacy_closes == []
    assert world.closed == ['instance-1']
    assert not world.resident and not hub.farm_queue.active


def test_startup_replacement_is_not_adopted_after_adb_connects(world, monkeypatch):
    pid = [1]
    setup = real_startup(world, monkeypatch, on_connect=lambda: pid.__setitem__(0, 99))
    monkeypatch.setattr(bluestacks, 'emulator_identity',
                        lambda cfg: (pid[0],) if cfg.bluestacks_instance in world.resident else ())
    hub = SimpleNamespace(farm_queue=instances.FarmQueue(1))
    world.gate.set()
    _, _, thread = worker(1, hub, world)
    thread.join(3)
    assert not thread.is_alive()
    assert setup.launched == ['instance-1']
    assert not world.closed
    assert 1 in hub.farm_queue.held
