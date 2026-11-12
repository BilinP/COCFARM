"""The log, made ready to paste into a GitHub issue: personal details taken
out, and as much of it as GitHub takes (the newest lines kept).

What's taken out: the account's player names (every profile's, and any the
log says it read), player and clan tags, the Windows user name and the PC's
name (in file paths and anywhere else), e-mail addresses, Discord webhooks
and other web addresses' paths, bot tokens, and network addresses other
than the PC's own (the emulators' 127.0.0.1 ports are kept: they help),
and emulator instances' names that aren't the emulators' own ("LDPlayer-1"
stays, "Bob's farm" doesn't).

Names are taken out both when COCFARM knows them (its profiles, its
emulators' instances) and when the log says them ("Account: profile Bob",
"Made profile Bob", ...: a profile deleted since is still a player's name),
wherever else they show. Paths start at COCFARM's own folder.

What's put in, above the log: the emulator (which, its version, the
instance's number and port, and what the device says of itself: the game's
version, its display) and Windows, the settings (which are on, which off,
and the others changed from their defaults), for whoever looks into it to
set up the same; and every error and the commonest warnings of all the days
the log holds (how often, when, which farms), since the log itself may only
fit its newest hours; and a hard crash's trace, from crash.log."""

from __future__ import annotations

import dataclasses
import getpass
import logging
import os
import platform
import re
import socket
import sys
from collections.abc import Iterable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from farmbot import __version__

# GitHub refuses an issue or a comment over 65,536 characters: the pasted
# log, with its header and code fence, stays under that.
GITHUB_LIMIT = 65_536
# (Room for the issue form's own fields, and what the user writes in them.)
BUDGET = GITHUB_LIMIT - 7_500
ERROR_LEVELS = ("ERROR", "CRITICAL")
_LEVEL = re.compile(r"^\S+ \S+ (\w+)\s")  # "2026-11-12 10:00:00,123 ERROR   ..."
# "2026-11-12 10:00:00,123 ERROR   [farm 2] farmbot.bot: Cycle failed ..."
_RECORD = re.compile(r"^(\d{4})-(\d\d-\d\d) (\d\d:\d\d):\S+ (\w+)\s+(?:\[farm (\d+)\] )?[\w.]+: (.*)$")
_DIAGNOSTICS = re.compile(r" \[emulator [^\]]*\]$")  # (a failure's own details: they differ every time)
ERRORS_SHOWN = 20
WARNINGS_SHOWN = 8

PLAYER = "<player>"
USER = "<user>"
INSTANCE = "<instance>"
SECRET = "(set)"

_RULES: list[tuple[re.Pattern, str]] = [
    # Web addresses: the site is kept, what follows it (webhook ids, tokens, ...) isn't.
    # (GitHub's are kept whole: the update check's, nothing personal.)
    (re.compile(r"\b(https?://)([^/\s:@]+@)?(?!(?:[\w-]+\.)*github(?:usercontent)?\.com\b)([^/\s?#'\"<>]+)"
                r"[^\s'\"<>]*", re.I), r"\1\3/<removed>"),
    (re.compile(r"\b[\w.%+-]+@[\w-]+(\.[\w-]+)+\b"), "<email>"),
    (re.compile(r"\b\d{6,12}:[\w-]{30,}\b"), "<token>"),  # a Telegram bot's
    # The Windows user's folders, either slash: C:\Users\name\..., /Users/name/...
    (re.compile(r"(?i)\b([a-z]:[\\/]+(?:users|documents and settings)[\\/]+)[^\\/\s'\"]+"), r"\1" + USER),
    (re.compile(r"(?i)(?<![\w.])(/(?:users|home)/)[^/\s'\"]+"), r"\1" + USER),
    # Player and clan tags (#2PP, #9QJ8LUYV2: the game's letters only).
    # (At least one letter: "attack #200" is a count, not a tag.)
    (re.compile(r"(?<![\w&])#(?=[0289]*[PYLQGRJCUV])[0289PYLQGRJCUV]{3,12}\b"), "#<tag>"),
    # What the log says it read as a player name.
    (re.compile(r"(Player name read as )('[^']*'|\"[^\"]*\"|\S+)"), r"\1" + PLAYER),
    (re.compile(r"(Account name looks a little like )(.+?)( \(\d)"), r"\1" + PLAYER + r"\3"),
    (re.compile(r"(another profile has that name: keeping )(.+)$", re.M), r"\1" + PLAYER),
]
# A network address: with a port, in a web address, or said to be one. (A
# bare a.b.c.d is a version far more often: LDPlayer 14.0.29.2.)
_IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_IP_PORT = re.compile(r":\d")
_IP_SAID = re.compile(r"(?i)(//|@|\b(?:ip|address|host)\s?[:=]?\s?)$")
_KEPT_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
_ACCOUNT = re.compile(r"Account( \d+)?")  # the bot's own names for an account it couldn't read: not personal
# The names emulators give their instances themselves: not personal, and kept.
_OWN_TITLE = re.compile(r"(LDPlayer|Android Device|BlueStacks( App Player)?|MuMu( ?Player)?( \d+)?|COCFARM spare)"
                        r"([ _-]?\d+)?|(Pie|Nougat|Rvc|Tiramisu)(64|32)?(_\d+)?", re.I)
# Settings never shown, only whether they're set: they hold addresses or names.
_SECRET_KEYS = re.compile(r"webhook|token|password|chat_id|secret", re.I)
_NAME_KEYS = {"profiles.active", "profiles.default"}


# The log's own lines that say a player's or an instance's name: every name
# they say is taken out wherever it shows (a profile deleted since, an
# instance renamed since, isn't one COCFARM knows of any more).
_SAYS_NAMES = [re.compile(p) for p in (
    r"Account: profile (?P<a>.+)$",
    r"Made profile (?P<a>.+)$",
    r"Copied profile (?P<a>.+?)'s settings to (?P<b>.+)$",
    r"Renamed profile (?P<a>.+?) to (?P<b>.+?) \(the account's name\)$",
    r"Couldn't rename profile (?P<a>.+?) to (?P<b>.+?): ",
    r"Player name read as '(?P<a>[^']*)'$",
    r'Player name read as "(?P<a>[^"]*)"$',
    r"Player name read as (?P<a>.+?), but another profile has that name: keeping (?P<b>.+)$",
    r"Account name looks a little like (?P<a>.+?) \(\d",
    r"is already playing (?:the account )?(?P<a>.+?)(?:: closing|, )",
)]
_SAYS_TITLES = [re.compile(p) for p in (
    r"Copying (?P<a>.+?) to a new instance, (?P<b>.+)$",
    r"'s instance (?P<a>.+?) \(instance \d+\)$",
    r"Deleting MuMu instance \d+ \((?P<a>.+)\)$",
    r"didn't delete (?P<a>.+)$",
    r"Starting BlueStacks \((?P<a>.+)\)$",
    r"Connected to (?P<a>.+?) at \S+$",
)]


def names_in_log(lines: Iterable[str]) -> tuple[set[str], set[str]]:
    """The player names and the instance names ``lines`` say."""
    names: set[str] = set()
    titles: set[str] = set()
    for line in lines:
        m = _RECORD.match(line)
        message = _DIAGNOSTICS.sub("", m.group(6)) if m else line
        for patterns, found in ((_SAYS_NAMES, names), (_SAYS_TITLES, titles)):
            for pattern in patterns:
                said = pattern.search(message)
                if said:
                    found.update(v.strip() for v in said.groupdict().values() if v and v.strip())
    return names, titles


def _ip(match: re.Match) -> str:
    text = match.group(0)
    if text in _KEPT_IPS or match.group(1) == "127" or any(int(p) > 255 for p in match.groups()):
        return text  # this PC (the emulators), or not an address at all (a version, a time)
    whole = match.string
    if not (_IP_PORT.match(whole, match.end()) or _IP_SAID.search(whole, 0, match.start())):
        return text  # a version number (LDPlayer 14.0.29.2), most likely
    return "<ip>"


def private_words() -> list[str]:
    """This PC's user name and computer name: taken out wherever they show."""
    words = set()
    for get in (getpass.getuser, socket.gethostname, lambda: os.environ.get("USERNAME", ""),
                lambda: os.environ.get("COMPUTERNAME", "")):
        try:
            word = (get() or "").strip()
        except Exception:  # (no user name to be had: nothing to take out)
            continue
        if len(word) >= 3:
            words.add(word)
    return sorted(words, key=len, reverse=True)


def _own_folder(root: Path | None) -> tuple[re.Pattern, str] | None:
    """COCFARM's folder written out in full (either slash, any case), and its name."""
    if root is None:
        from farmbot import instances

        root = instances.ROOT
    parts = [p.rstrip("\\/") for p in Path(root).parts]
    if len(parts) < 2:  # (a drive's root: nothing in front of it to take out)
        return None
    pattern = r"(?i)(?<![\w])" + r"[\\/]+".join(re.escape(p) for p in parts) + r"(?![\w])"
    return re.compile(pattern), Path(root).name


def scrub(text: str, names: Iterable[str] = (), words: Iterable[str] | None = None,
          titles: Iterable[str] = (), root: Path | None = None) -> str:
    """``text`` without personal details. ``names``: the accounts' player
    names (each profile's); ``words``: this PC's user and computer names
    (looked up when not given); ``titles``: the emulator instances' names
    (see :func:`instance_titles`); ``root``: COCFARM's folder (its own
    when not given), its paths shown from it on: "COCFARM\\screens\\x.png",
    not where it is (the Desktop, a user's folder)."""
    own = _own_folder(root)
    if own is not None:
        text = own[0].sub(lambda _: own[1], text)
    for pattern, put in _RULES:
        text = pattern.sub(put, text)
    text = _IP.sub(_ip, text)
    for word in (private_words() if words is None else words):
        text = re.sub(rf"(?i)(?<![\w]){re.escape(word)}(?![\w])", USER, text)
    # Player names: exactly as written (a name like "WHAT" mustn't take out
    # every "what"), longest first (so "Bob 2" goes before "Bob").
    for name in sorted({n.strip() for n in names}, key=len, reverse=True):
        if len(name) >= 2 and not _ACCOUNT.fullmatch(name):
            text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", PLAYER, text)
    for title in sorted({t.strip() for t in titles}, key=len, reverse=True):
        if len(title) >= 2 and not _OWN_TITLE.fullmatch(title):
            text = re.sub(rf"(?<![\w]){re.escape(title)}(?![\w])", INSTANCE, text)
    return text


def instance_titles() -> list[str]:
    """Every emulator instance's name on this PC (their managers are asked)."""
    from farmbot import instances

    try:
        return [instances.instance_title(c.label) for c in instances.choices()]
    except Exception as exc:  # (an emulator that can't be asked: its names can't be taken out either)
        logging.getLogger(__name__).info("Couldn't list the emulator instances: %s", exc)
        return []


EMULATOR_KINDS = ("ldplayer", "mumu", "bluestacks")


def emulator_section(device, farms: int = 1) -> list[str]:
    """What the bot plays on: the emulator and its version, the instance
    (its number, not its name), the connection, and Windows."""
    from farmbot import instances, standard

    try:
        installed = instances._installed_versions()
    except OSError:
        installed = {}
    found = sorted(f"{name} {version}" for name, version in installed.items()
                   if any(k in name.lower().replace(" ", "") for k in EMULATOR_KINDS)
                   and "services" not in name.lower())
    plays = instances.describe(device)
    if device.mode in EMULATOR_KINDS:
        if device.mode == "ldplayer":
            version = next((v for n, v in installed.items() if n.lower() == (device.ldplayer or "").lower()), "")
        else:
            pattern = instances.INSTALLED_NAMES[device.mode]
            version = next((v for n, v in sorted(installed.items()) if pattern.search(n)), "")
        plays += f" ({version})" if version else ""
    else:
        plays += f" (connected by hand: {device.mode})"
    exe = "COCFARM.exe" if getattr(sys, "frozen", False) else "from source"
    from farmbot import diag

    says = diag.facts.get(device.serial)
    return [
        "### Emulator",
        f"- Plays on: {plays}",
        f"- Connection: {device.serial}" + ("" if device.adb_path in ("", "adb") else f", adb {device.adb_path}"),
        f"- Display COCFARM sets: {standard.WIDTH} x {standard.HEIGHT}, {standard.DPI} DPI",
        "- The device says: " + (diag.describe_facts(says) if says else "(not asked yet: it's asked when the bot "
                                                                          "starts)"),
        f"- Emulators installed: {', '.join(found) or 'none found'}",
        f"- Farms: {farms}",
        f"- Windows: {windows_name()} ({platform.version()}), COCFARM {__version__} ({exe})",
    ]


def windows_name() -> str:
    """"Windows 11" and so on. (Python calls Windows 11 "10": its build, 22000 and up, tells.)"""
    release = platform.release()
    build = platform.version().rsplit(".", 1)[-1]
    if platform.system() == "Windows" and release == "10" and build.isdigit() and int(build) >= 22000:
        release = "11"
    return f"{platform.system()} {release}"


def _leaves(obj, prefix: str = ""):
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        path = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(value):
            yield from _leaves(value, path + ".")
        else:
            yield path, value


def settings_section(config) -> list[str]:
    """The settings: each on/off one under On or Off, by section, and every
    other one that isn't at its default. Ones holding web addresses or
    tokens only say they're set; the profiles' names are player names."""
    defaults = dict(_leaves(type(config)()))
    on: dict[str, list[str]] = {}
    off: dict[str, list[str]] = {}
    changed: list[str] = []
    for path, value in _leaves(config):
        section, _, key = path.partition(".")
        if isinstance(value, bool):
            (on if value else off).setdefault(section, []).append(key)
            continue
        if value == defaults.get(path):
            continue
        if _SECRET_KEYS.search(key):
            shown = SECRET if value else '""'
        elif path in _NAME_KEYS:
            shown = PLAYER if value else '""'
        else:
            shown = repr(value) if isinstance(value, str) else str(value)
        changed.append(f"- {path} = {shown}")
    lines = ["### Settings", "**On:**"]
    lines += [f"- {section}: {', '.join(keys)}" for section, keys in on.items()]
    lines += ["", "**Off:**"]
    lines += [f"- {section}: {', '.join(keys)}" for section, keys in off.items()]
    lines += ["", "**Changed from the defaults:**", *(changed or ["- (none)"])]
    return lines


def log_file() -> Path | None:
    """The file the log is being written to (``cli.log_to_file``), if any."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            return Path(handler.baseFilename)
    from farmbot.cli import LOG_FILE

    path = Path(LOG_FILE).resolve()
    return path if path.is_file() else None


def log_files(path: Path | None = None) -> list[Path]:
    """The log and the days before it (``farmbot.log.2026-11-11``, ...), newest first."""
    path = path or log_file()
    if path is None:
        return []
    older = sorted(path.parent.glob(path.name + ".*"), key=lambda p: p.name, reverse=True)
    return [p for p in [path, *older] if p.is_file()]


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def problems_section(lines: Iterable[str]) -> list[str]:
    """The errors (the newest first) and the commonest warnings in
    ``lines`` (oldest first): each kind once, with how often, when first and
    last, and on which farms. (Numbers in them don't make a kind of its own:
    "3 in a row".)"""
    kinds: dict[tuple[str, str], dict] = {}
    days: set[str] = set()
    for line in lines:
        m = _RECORD.match(line)
        if not m:
            continue
        _, day, clock, level, farm, message = m.groups()
        days.add(day)
        if level not in (*ERROR_LEVELS, "WARNING"):
            continue
        message = _DIAGNOSTICS.sub("", message)
        key = ("ERROR" if level in ERROR_LEVELS else "WARNING", re.sub(r"\d+", "#", message))
        kind = kinds.setdefault(key, {"count": 0, "first": f"{day} {clock}", "farms": set()})
        kind["count"] += 1
        kind["last"] = f"{day} {clock}"
        kind["newest"] = line[:23]  # (to the millisecond: "2026-11-12 10:00:00,123")
        kind["said"] = message  # (the newest wording)
        kind["farms"].add(int(farm or 1))

    def listed(level: str, most: int) -> list[str]:
        order = (lambda kv: kv[1]["newest"]) if level == "ERROR" else (lambda kv: kv[1]["count"])
        found = sorted(((k, v) for k, v in kinds.items() if k[0] == level), key=order, reverse=True)
        out = []
        for _, kind in found[:most]:
            when = kind["first"] if kind["first"] == kind["last"] else f"{kind['first']} to {kind['last']}"
            farms = sorted(kind["farms"])
            on = "" if farms == [1] else f", farm{'s' if len(farms) > 1 else ''} {', '.join(map(str, farms))}"
            said = kind["said"] if len(kind["said"]) <= 300 else kind["said"][:297] + "..."
            out.append(f"- {kind['count']}x {said} ({when}{on})")
        if len(found) > most:
            out.append(f"- and {len(found) - most} other kind{'s' if len(found) - most > 1 else ''}")
        return out or ["- (none)"]

    span = f"{min(days)} to {max(days)}" if len(days) > 1 else (next(iter(days)) if days else "none")
    return [f"### Problems (the log's days: {span})", "**Errors (the newest first):**", *listed("ERROR", ERRORS_SHOWN), "",
            "**Commonest warnings:**", *listed("WARNING", WARNINGS_SHOWN)]


def crash_section(path: Path | None = None, most: int = 60) -> list[str]:
    """The newest hard crashes' traces (Python or Qt failing outright), from
    crash.log next to the log: its last ``most`` lines. Nothing if none."""
    from farmbot import diag

    if path is None:
        log = log_file()
        if log is None:
            return []
        path = log.parent / diag.CRASH_FILE
    import time

    lines = diag.crashes(path, since=time.time() - 7 * 86400)
    if not lines:
        return []
    return ["### Hard crashes (crash.log)", "```text", *lines[-most:], "```"]


def count_errors(lines: Iterable[str]) -> int:
    return sum(1 for line in lines if (m := _LEVEL.match(line)) and m.group(1) in ERROR_LEVELS)


def report(names: Iterable[str] = (), files: list[Path] | None = None, lines: list[str] | None = None,
           budget: int = BUDGET, words: Iterable[str] | None = None, titles: Iterable[str] = (),
           sections: Iterable[str] = (), summary: bool = True) -> tuple[str, int, int]:
    """The log, scrubbed, as a Markdown code block that fits in a GitHub
    issue: the newest lines, going back over the days before while there's
    room. ``lines``: use these instead of the files (the window's own log,
    when nothing is written to disk). Returns the text, the lines in it and
    the lines there were (in the days looked at). ``sections``: lines put
    above the log (the emulator's, the settings'), scrubbed too.
    ``summary``: the errors and warnings of every day, above the log too."""
    names, titles = list(names), list(titles)
    words = private_words() if words is None else list(words)
    files = [] if lines is not None else (log_files() if files is None else files)
    chunks = [lines] if lines is not None else [_read(f) for f in files]
    # (And the names the log itself says: a profile deleted since is still a player's name.)
    said_names, said_titles = names_in_log(line for chunk in chunks for line in chunk)
    names, titles = [*names, *said_names], [*titles, *said_titles]
    sections = list(sections)
    if summary:
        oldest_first = [line for chunk in reversed(chunks) for line in chunk]
        sections += ["", *problems_section(oldest_first)]
        from farmbot import diag

        crashed = crash_section(files[0].parent / diag.CRASH_FILE) if files else []
        sections += ["", *crashed] if crashed else []
    above = [scrub(line, names, words, titles) for line in sections]
    total = 0
    head = f"COCFARM {__version__} report (personal details removed by COCFARM)"
    fence = "```"
    above = [*above, "", "### Log"] if above else above
    room = budget - len(head) - sum(len(line) + 1 for line in above) - 2 * len(fence) - 200  # (and the note)
    kept: list[str] = []
    for chunk in chunks:  # newest day first; within it, from the end
        total += len(chunk)
        for line in reversed(chunk):
            clean = scrub(line, names, words, titles)
            if len(clean) + 1 > room:
                room = 0
                break
            room -= len(clean) + 1
            kept.append(clean)
        if room <= 0:
            break
    kept.reverse()
    errors = count_errors(kept)
    note = f"{len(kept):,} lines, {errors} error{'' if errors == 1 else 's'}"
    if len(kept) < total:
        note += f" (the newest {len(kept):,}: GitHub takes {GITHUB_LIMIT:,} characters)"
    text = "\n".join([head, "", *above, note, fence + "text", *kept, fence])
    return text, len(kept), total
