"""What the log needs to tell what went wrong, after the fact.

- :func:`where`: the lines of COCFARM's own code a failure came through,
  short enough for its log line ("attack.py:412 deploy < bot.py:1460 run").
- :func:`traceback_to_file`: a failure's whole traceback, in farmbot.log only
  (not the window), once an hour for the same failure: a round failing three
  times in a row on four farms doesn't fill the log with the same one.
- :func:`install`: errors nothing caught (in the window, or a thread) go
  to the log, rather than to a console a windowed program doesn't have,
  and a hard crash (Python itself, or Qt) leaves its trace in crash.log.
- :func:`device_facts`: the game's version and the device's own display,
  logged each time the bot starts and shown in the bug report's copy.
"""

from __future__ import annotations

import faulthandler
import logging
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

FILE_ONLY = {"file_only": True}  # (see gui._QueueLogHandler: kept out of the window)
TRACEBACK_EVERY = 3600.0  # seconds: the same failure's traceback, at most once in this long
CRASH_FILE = "crash.log"
CRASH_KEEP = 200_000  # bytes: an older crash.log starts afresh past this

_said: dict[tuple, float] = {}
_said_lock = threading.Lock()
_crash_file = None  # (kept open: faulthandler writes to it when Python itself fails)

# The device's own answers, by its serial (for the bug report's copy).
facts: dict[str, dict[str, str]] = {}


def where(exc: BaseException, most: int = 3) -> str:
    """The innermost ``most`` frames of COCFARM's own code ``exc`` came
    through, innermost first: "attack.py:412 deploy < bot.py:1460 run"."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if "farmbot" in Path(f.filename).parts]
    return " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-most:]))


def traceback_to_file(logger: logging.Logger, exc: BaseException, what: str = "") -> bool:
    """Log ``exc``'s traceback to farmbot.log only, unless the same failure
    (its type, and where it was raised) had its own in the last hour.
    Returns whether it was logged."""
    key = (type(exc).__name__, where(exc, 1))
    now = time.monotonic()
    with _said_lock:
        if now - _said.get(key, -TRACEBACK_EVERY) < TRACEBACK_EVERY:
            return False
        _said[key] = now
    logger.info("Traceback%s:", f" ({what})" if what else "", exc_info=(type(exc), exc, exc.__traceback__),
                extra=FILE_ONLY)
    return True


def failure(exc: BaseException) -> str:
    """``exc`` for a log line: its type, what it says, and where it came from."""
    at = where(exc)
    return f"{type(exc).__name__}: {exc}" + (f" (at {at})" if at else "")


def install(folder: Path | None = None) -> None:
    """Send errors nothing caught to the log, and hard crashes to
    ``folder``/crash.log (none: no crash file)."""
    previous = sys.excepthook

    def uncaught(kind, exc, tb):
        if issubclass(kind, KeyboardInterrupt):
            previous(kind, exc, tb)
            return
        logging.getLogger("farmbot").critical("Uncaught error: %s: %s", kind.__name__, exc,
                                              exc_info=(kind, exc, tb))

    def uncaught_in_thread(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logging.getLogger("farmbot").critical("Uncaught error in thread %s: %s: %s", name, args.exc_type.__name__,
                                              args.exc_value, exc_info=(args.exc_type, args.exc_value,
                                                                        args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = uncaught_in_thread
    if folder is not None:
        _crash_trace(Path(folder) / CRASH_FILE)


def _crash_trace(path: Path) -> None:
    global _crash_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > CRASH_KEEP:
            path.unlink()
        if _crash_file is not None:
            faulthandler.disable()
            _crash_file.close()
        _crash_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - open for as long as COCFARM runs
        from farmbot import __version__

        _crash_file.write(f"--- COCFARM {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _crash_file.flush()
        faulthandler.enable(_crash_file, all_threads=True)
    except OSError as exc:
        log.info("No crash file: %s", exc)


def crashes(path: Path, since: float = 0.0) -> list[str]:
    """crash.log's crashes (not its "started" lines alone), from the
    starts after ``since`` (a time.time())."""
    try:
        if not path.is_file() or path.stat().st_mtime < since:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    block: list[str] = []
    for line in [*lines, "--- end"]:
        if line.startswith("--- "):
            if len(block) > 1:  # a start with something after it: a crash
                out += block
            block = []
        block.append(line)
    return out


def startup() -> str:
    """COCFARM's version, Windows' and Python's, for the log's first line."""
    from farmbot import __version__
    from farmbot.logshare import windows_name

    how = "COCFARM.exe" if getattr(sys, "frozen", False) else "from source"
    return (f"COCFARM {__version__} ({how}) on {windows_name()} ({platform.version()}), "
            f"Python {platform.python_version()}")


def device_facts(device, package: str) -> dict[str, str]:
    """The game's version and the device's Android, display and density, as
    the device answers (an answer it can't give is left out)."""
    shell = getattr(device, "shell", None)
    if shell is None:
        return {}
    found: dict[str, str] = {}

    def ask(name: str, *args: str, pick=lambda out: out.strip()) -> None:
        try:
            value = pick(str(shell(*args)))
        except Exception:  # noqa: BLE001 - a fact it can't tell: left out
            return
        if value:
            found[name] = value[:60]

    def version(out: str) -> str:
        return next((line.split("=", 1)[1].strip() for line in out.splitlines()
                     if line.strip().startswith("versionName=")), "")

    def last(out: str) -> str:  # "Physical size: 860x672\nOverride size: ...": the one in use
        sizes = [line.split(":", 1)[1].strip() for line in out.splitlines() if ":" in line]
        if len(sizes) < 2:
            return "".join(sizes)
        return f"{sizes[-1]} (its own {sizes[0]})"

    ask("game", "dumpsys", "package", package, pick=version)
    ask("android", "getprop", "ro.build.version.release")
    ask("display", "wm", "size", pick=last)
    ask("density", "wm", "density", pick=last)
    serial = getattr(device, "serial", "")
    if found and serial:
        facts[serial] = found
    return found


def describe_facts(found: dict[str, str]) -> str:
    names = {"game": "game", "android": "Android", "display": "display", "density": "density"}
    return ", ".join(f"{names[k]} {v}" for k, v in found.items() if k in names)
