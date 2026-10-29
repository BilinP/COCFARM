"""Command line: run the bot, open the GUI, and calibrate from screenshots."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

from farmbot import __version__, bluestacks, catalog, text
from farmbot.accounts import load_active
from farmbot.bot import Bot
from farmbot.config import EMULATORS, Config, ensure_config
from farmbot.device import AdbDevice, DeviceError
from farmbot.game import Game, T, normalize_screen
from farmbot.geometry import Rect
from farmbot.vision import TemplateLibrary, Vision


def _config(args: argparse.Namespace) -> Config:
    # Like the GUI, use farmbot.toml when no file is named, creating it from
    # the starting profile on first run.
    if args.config is None:
        ensure_config("farmbot.toml")
        return load_active("farmbot.toml")
    return load_active(args.config)


def _device(args: argparse.Namespace, cfg: Config) -> AdbDevice:
    if args.serial:  # an explicit address always wins
        cfg.device.mode, cfg.device.serial = "manual", args.serial
    return bluestacks.open_device(cfg.device)


def _screen(args: argparse.Namespace, cfg: Config) -> np.ndarray:
    if getattr(args, "image", None):
        image = cv2.imread(args.image, cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(f"cannot read {args.image}")
    else:
        image = _device(args, cfg).screenshot()
    return normalize_screen(image, _vision(cfg), cfg.device.ui_scale)


def _vision(cfg: Config) -> Vision:
    return Vision(TemplateLibrary(cfg.bot.templates), cfg.bot.threshold)


class StillDevice:
    """A device that always shows the same screenshot (for ``inspect``)."""

    def __init__(self, screen: np.ndarray):
        self._screen = screen

    def screenshot(self) -> np.ndarray:
        return self._screen


def cmd_screenshot(args: argparse.Namespace) -> int:
    cfg = _config(args)
    image = _screen(args, cfg)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), image)
    print(f"saved {out} ({image.shape[1]}x{image.shape[0]}, scaled to the bot's coordinates)")
    return 0


def cmd_crop(args: argparse.Namespace) -> int:
    cfg = _config(args)
    image = _screen(args, cfg)
    r = Rect(args.x, args.y, args.w, args.h)
    crop = image[r.y:r.y + r.h, r.x:r.x + r.w]
    if crop.size == 0:
        print("crop rectangle is outside the image", file=sys.stderr)
        return 1
    out = Path(cfg.bot.templates) / f"{args.name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), crop)
    print(f"saved template {out}")
    return 0


def cmd_locate(args: argparse.Namespace) -> int:
    cfg = _config(args)
    matches = _vision(cfg).find_all(_screen(args, cfg), args.name, args.threshold)
    for m in matches:
        print(f"{m.name}\t({m.x}, {m.y})\tscore {m.score:.3f}")
    if not matches:
        print("no match")
    return 0 if matches else 2


def cmd_learn_digits(args: argparse.Namespace) -> int:
    cfg = _config(args)
    region = Rect(args.x, args.y, args.w, args.h)
    saved = text.learn(_screen(args, cfg), region, args.text, Path(cfg.bot.templates) / T.LOOT_FONT, replace=args.replace)
    print(f"saved {len(saved)} glyph(s): {', '.join(p.name for p in saved) or 'none new'}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show what the bot sees on a scouting screen."""
    from farmbot.inspect import describe

    cfg = _config(args)
    lines, overlay = describe(_screen(args, cfg), cfg, _vision(cfg))
    print("\n".join(lines))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), overlay)
    print(f"overlay: {out}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    install = bluestacks.find_install()
    if install is None:
        print("BlueStacks 5 not found.")
        return 2
    print(f"BlueStacks: {install.install_dir}")
    print(f"ADB access: {'on' if install.adb_enabled else 'OFF (Settings > Advanced > Android Debug Bridge)'}")
    print(f"Running:    {'yes' if bluestacks.is_running() else 'no'}")
    for inst in install.instances:
        print(f"  - {inst.label}")
    return 0


def cmd_pictures(args: argparse.Namespace) -> int:
    library = TemplateLibrary(_config(args).bot.templates)
    feature = None
    for p in catalog.PICTURES:
        if p.feature != feature:
            feature = p.feature
            print(f"\n{feature}{' (required)' if p.required else ''}")
        count = len(library.paths(p.name))
        mark = "ok " if count else "MISSING" if p.required else "ADD" if p.feature == catalog.QUICK_EXIT else "-  "
        extra = f" x{count}" if p.folder and count else ""
        print(f"  {mark:7} {p.name}{extra}: {p.where}")
    missing = catalog.missing(library, required_only=True)
    if missing:
        print(f"\n{len(missing)} required picture(s) missing. Add them under Advanced → Pictures in the window, or with 'farmbot crop'.")
    return 2 if missing else 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if args.max_attacks is not None:
        cfg.bot.max_attacks = args.max_attacks
    missing = [] if cfg.search.maintain_only else catalog.missing(TemplateLibrary(cfg.bot.templates), required_only=True)
    if missing:
        print("error: pictures missing: " + ", ".join(p.name for p in missing) + " (see 'farmbot pictures')", file=sys.stderr)
        return 1
    if not cfg.search.maintain_only and catalog.slow_exit(TemplateLibrary(cfg.bot.templates)):
        print("note: without buttons/return_home and buttons/okay the bot closes the game to leave each battle")
    device = _device(args, cfg)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    reopen = (lambda: bluestacks.restart_emulator(cfg.device)) if cfg.device.mode in EMULATORS else None
    Bot(Game(device, _vision(cfg), stop_event=stop, ui_scale=cfg.device.ui_scale,
             window_scale=cfg.device.window_scale,
             picture_scales=cfg.device.picture_scales), cfg, reopen_device=reopen).run()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from farmbot import splash  # Qt is only needed here

    splash.show()  # while the rest loads
    from farmbot.gui import run_gui

    return run_gui(args.config or "farmbot.toml", args.serial)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="farmbot", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="TOML settings file")
    parser.add_argument("-s", "--serial", default=None, help="ADB serial, overrides the config")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gui", help="open the control window")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("run", help="start farming without the GUI (Ctrl+C to stop)")
    p.add_argument("--max-attacks", type=int, default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("devices", help="list BlueStacks instances and their ADB ports")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("pictures", help="list the button pictures the bot uses and which are missing")
    p.set_defaults(func=cmd_pictures)

    p = sub.add_parser("screenshot", help="save the device screen (scaled to bot coordinates)")
    p.add_argument("output", nargs="?", default="screens/screen.png")
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("inspect", help="show loot, troop cards and deploy points the bot sees")
    p.add_argument("--image", help="screenshot file instead of the live screen")
    p.add_argument("--output", default="screens/inspect.png")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("crop", help="cut a template out of a screenshot")
    p.add_argument("image")
    for name in ("x", "y", "w", "h"):
        p.add_argument(name, type=int)
    p.add_argument("name", help="template name, e.g. buttons/return_home")
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("locate", help="test a template against a screenshot or the live screen")
    p.add_argument("name")
    p.add_argument("--image", help="screenshot file instead of the live screen")
    p.add_argument("--threshold", type=float, default=None)
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("learn-digits", help="learn loot digit shapes from a number on screen")
    p.add_argument("image")
    for name in ("x", "y", "w", "h"):
        p.add_argument(name, type=int)
    p.add_argument("text", help='the number shown, e.g. "1 582 869"')
    p.add_argument("--replace", action="store_true", help="overwrite digits already learned")
    p.set_defaults(func=cmd_learn_digits)
    return parser


LOG_FILE = "logs/farmbot.log"
LOG_DAYS = 14


def log_to_file(path: str) -> None:
    """Keep the activity log on disk too: a new file each midnight, the last
    ``LOG_DAYS`` days kept, so a problem can be looked at after the fact."""
    from logging.handlers import TimedRotatingFileHandler

    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(path, when="midnight", backupCount=LOG_DAYS, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("Not logging to %s: %s", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def gui_main() -> int:
    """``cocfarm``: open the GUI (a windowed program, so no console opens)."""
    return main(["gui", *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows consoles default to a code page without characters like "→".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.func in (cmd_run, cmd_gui):
        log_to_file(LOG_FILE)
        logging.getLogger(__name__).info("COCFARM %s", __version__)  # first thing a bug report's log shows
    try:
        return args.func(args)
    except (DeviceError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
