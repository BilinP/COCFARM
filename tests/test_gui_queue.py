import time
import importlib.util
from pathlib import Path

from farmbot import accounts, bluestacks, gui, instances
from farmbot.config import Config, save_config
from farmbot.vision import TemplateLibrary
from test_farm_worker import world  # synthetic devices; never calls native emulator tools


def isolate_gui(monkeypatch, tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.chdir(tmp_path)
    for name in ['_find_bluestacks', 'scan_emulators', 'look_for_open', '_build_pages_idle',
                 '_prebuild', '_after_update', '_whats_new', '_offer_carry']:
        monkeypatch.setattr(gui.FarmbotWindow, name, lambda *a, **k: None)
    monkeypatch.setattr(instances, 'emulator_badge', lambda *a: (None, ''))
    return Path(__file__).resolve().parents[1]


def pump(app, check):
    end = time.monotonic() + 5
    while not check():
        app.processEvents()
        assert time.monotonic() < end
        time.sleep(.005)


def test_gui_control_all_start_direct_wake_and_cancel(world, monkeypatch, tmp_path):
    root = isolate_gui(monkeypatch, tmp_path)
    for n in range(1, 5):
        cfg = Config()
        cfg.device.bluestacks_instance = f'instance-{n}'
        cfg.bot.templates = str(root / 'templates')
        cfg.bot.check_updates = False
        path = instances.config_path(n, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_config(cfg, path)
    app = gui.create_app()
    window = gui.FarmbotWindow(str(tmp_path / 'farmbot.toml'))
    try:
        # Observe every synthetic native launch through the real worker path.
        original_open = world.open_device
        admitted = []
        def open_under_limit(cfg):
            limit = window.farm_queue.limit
            admitted.append((len(world.resident), limit))
            assert not limit or len(world.resident) < limit
            return original_open(cfg)
        monkeypatch.setattr(bluestacks, 'open_device_retrying', open_under_limit)
        page = window.farms_page
        page.rebuild()
        assert page.maximum.text() == '0'
        page.maximum.setText('2')
        assert instances.active_limit(tmp_path) == 2
        for invalid in ['', '-1', '13', 'not a number', '9' * 5000]:
            page.maximum.setText(invalid)
            assert window.farm_queue.limit == 2
            assert instances.active_limit(tmp_path) == 2
        page.maximum.setText('2')
        window.start(show=False)  # direct start
        child = window.farm_window(2)
        child.config.upgrade.wake_up = child.config.upgrade.enabled = True
        child.start_on_wake()  # scheduled wake uses the same worker admission
        pump(app, lambda: len(world.resident) == 2)
        queued_window = window.farm_window(3)
        queued_window.start(show=False)
        pump(app, lambda: window.farm_queue.waiting == [3])
        queued_window.stop_soon()
        pump(app, lambda: not queued_window._running())
        assert 'instance-3' not in world.opened
        page._start_or_stop_all()  # Stop All preserves the existing safe-stop path.
        pump(app, lambda: not any(w._running() for w in window.farm_windows().values()))
        assert not world.resident
        world.opened.clear()
        world.maximum[0] = 0
        # Start All from stopped also routes through the shared cap.
        page._start_or_stop_all()
        pump(app, lambda: len(window.farm_queue.waiting) == 2)
        assert len(world.resident) == 2
        page.maximum.setText('3')
        pump(app, lambda: len(world.resident) == 3)
        page.maximum.setText('1')
        assert len(world.resident) == 3  # current rounds are left alone
        queued = window.farm_queue.waiting[0]
        window.farm_window(queued).stop_soon()
        pump(app, lambda: queued not in window.farm_queue.waiting)
        assert f'instance-{queued}' not in world.opened
        # Queue it again under the smaller limit: admission must wait for all
        # three existing residents to drain, rather than interrupting them.
        pump(app, lambda: not window.farm_window(queued)._running())
        window.farm_window(queued).start(show=False)
        pump(app, lambda: queued in window.farm_queue.waiting)
        # Profile application must not change the hub's setting or copy it into a profile.
        accounts.take(Config(), window.config)
        assert window.farm_queue.limit == 1
        assert not hasattr(window.config, 'maximum_active_instances')
        world.gate.set()
        pump(app, lambda: not any(w._running() for w in window.farm_windows().values()))
        assert not world.resident
        assert world.maximum[0] == 3
        assert any(limit == 1 for _, limit in admitted)
        assert all(not limit or residents < limit for residents, limit in admitted)
        # The upstream self-test's page/template checks, with synthetic device discovery.
        for name in gui.PAGES:
            window.show_page(name)
            app.processEvents()
        assert not gui.catalog.missing(TemplateLibrary(root / 'templates'), required_only=True)
    finally:
        world.gate.set()
        for w in window.farm_windows().values():
            if w.stop_event:
                w.stop_event.set()
        pump(app, lambda: not any(w._running() for w in window.farm_windows().values()))
        window._closing = True
        window.close()
        app.processEvents()


def test_upstream_self_test_entry_with_synthetic_discovery(world, monkeypatch, tmp_path):
    root = isolate_gui(monkeypatch, tmp_path)
    cfg = Config()
    cfg.bot.templates = str(root / 'templates')
    cfg.bot.check_updates = False
    save_config(cfg, tmp_path / 'selftest.toml')
    monkeypatch.setattr('farmbot.vision.TemplateLibrary',
                        lambda path: TemplateLibrary(root / path))
    spec = importlib.util.spec_from_file_location('cocfarm_selftest', root / 'packaging/farmbot_app.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.self_test() == 0
