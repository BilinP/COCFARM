import threading
import time
from unittest.mock import patch

from farmbot import instances
from farmbot.config import DeviceConfig


def wait_for(check):
    end = time.monotonic() + 2
    while not check():
        assert time.monotonic() < end
        time.sleep(.005)


def test_limit_fifo_rotation_and_cancel():
    farms = instances.FarmQueue(2)
    stopped = threading.Event()
    assert farms.acquire(1, 'one', stopped)
    assert farms.acquire(2, 'two', stopped)
    ready = []
    third = threading.Thread(target=lambda: ready.append(farms.acquire(3, 'three', stopped)))
    third.start()
    wait_for(lambda: farms.waiting == [3])
    assert not ready
    assert farms.should_yield(1)
    farms.release(1)
    third.join(2)
    assert ready == [True]
    assert set(farms.active) == {2, 3}
    fourth = threading.Thread(target=lambda: ready.append(farms.acquire(4, 'four', stopped)))
    fourth.start()
    wait_for(lambda: farms.waiting == [4])
    stopped.set()
    fourth.join(2)
    assert ready == [True, False]
    assert farms.waiting == []


def test_dynamic_limit_keeps_residents_and_holds_failed_cleanup():
    farms = instances.FarmQueue(2)
    stopped = threading.Event()
    assert farms.acquire(1, 'one', stopped)
    assert farms.acquire(2, 'two', stopped)
    ready = []
    third = threading.Thread(target=lambda: ready.append(farms.acquire(3, 'three', stopped)))
    third.start()
    wait_for(lambda: farms.waiting == [3])
    farms.set_limit(3)
    third.join(2)
    assert ready == [True]
    farms.set_limit(1)
    assert set(farms.active) == {1, 2, 3}  # no interruption to battles or paused players
    assert farms.should_yield(2)
    farms.hold(1, 'Exit could not be verified')
    farms.release(2)
    farms.release(3)
    assert farms.active == {1: 'one'}
    assert farms.held == {1: 'Exit could not be verified'}


def test_same_instance_waits_even_with_room_and_uncapped_stays_uncapped():
    farms = instances.FarmQueue(2)
    stop = threading.Event()
    assert farms.acquire(1, 'same', stop)
    ready = []
    alias = threading.Thread(target=lambda: ready.append(farms.acquire(2, 'same', stop)))
    alias.start()
    wait_for(lambda: farms.waiting == [2])
    assert not ready
    farms.release(1)
    alias.join(2)
    assert ready == [True]
    farms = instances.FarmQueue(0)
    for n in range(1, 13):
        assert farms.acquire(n, str(n), stop)
    assert not farms.should_yield(1)


def test_limit_saved_outside_account_config(tmp_path):
    assert instances.active_limit(tmp_path) == 0
    instances.save_active_limit(2, tmp_path)
    assert instances.active_limit(tmp_path) == 2
    assert not (tmp_path / 'farmbot.toml').exists()


def test_paused_queued_farm_resumes_without_taking_a_slot():
    farms = instances.FarmQueue(1)
    stop, pause = threading.Event(), threading.Event()
    assert farms.acquire(1, 'one', stop)
    ready, paused = [], threading.Event()
    thread = threading.Thread(target=lambda: ready.append(farms.acquire(2, 'two', stop, pause, on_pause=paused.set)))
    thread.start()
    wait_for(lambda: farms.waiting == [2])
    pause.set()
    assert paused.wait(2)
    farms.release(1)
    assert not ready and thread.is_alive()
    assert not farms.active
    pause.clear()
    thread.join(2)
    assert ready == [True]


def test_bluestacks_main_display_and_internal_aliases_share_a_key(tmp_path):
    from farmbot.bluestacks import Install, Instance
    install = Install(tmp_path, tmp_path, True, [Instance('Pie64', 'Player One', 5555)])
    with patch('farmbot.bluestacks.find_install', return_value=install):
        keys = {instances.instance_key(DeviceConfig(bluestacks_instance=name))
                for name in ['', 'Pie64', 'pie64', 'Player One']}
    assert len(keys) == 1


def test_post_connect_collision_holds_instead_of_closing_other_player():
    farms = instances.FarmQueue(2)
    stop = threading.Event()
    assert farms.acquire(1, 'one', stop)
    assert farms.acquire(2, 'two', stop)
    import pytest
    with pytest.raises(instances.FarmHeld):
        farms.identify(2, 'one')
    assert 2 in farms.held
