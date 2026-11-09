"""Which ADB ports each emulator instance takes, across every emulator on
the PC, so farms on different emulators never want the same one.

What each emulator listens on (measured on one PC with all four installed):

* LDPlayer (9 and 14 alike): instance N on 5555 + 2N, nothing else. Both
  versions number their instances the same way and run them in one shared
  virtual machine service, so LDPlayer 9's instance 2 and LDPlayer 14's
  instance 2 want the same port: they're one numbering.
* MuMu Player: instance N on its own port (16384 + 32N), and its first
  instance on 5555 and 7555 too, with no setting to turn those off.
* BlueStacks: the port written in ``bluestacks.conf`` (5555 for its first
  instance), which it keeps: so COCFARM moves BlueStacks off the others'
  ports instead of moving the others (see :func:`bluestacks_port`).

A clash between two farms is found before either starts, and the instance
that can move without a copy does (BlueStacks); an LDPlayer instance whose
port is taken is never picked for a new instance in the first place.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

from farmbot.config import DeviceConfig

if TYPE_CHECKING:
    from farmbot.instances import Choice

LDPLAYER_BASE, LDPLAYER_STEP = 5555, 2  # LDPlayer instance N: 5555 + 2N
MUMU_BASE, MUMU_STEP = 16384, 32  # MuMu instance N: 16384 + 32N (when MuMu doesn't say)
MUMU_FIRST = frozenset({5555, 7555})  # MuMu's first instance listens on these too
LDPLAYER_MOST = 64  # instance numbers looked at for LDPlayer (far past COCFARM's farms)
# Where BlueStacks is moved to: past every port the others use (LDPlayer's
# 5555 + 2N for N under LDPLAYER_MOST ends at 5681; MuMu's start at 16384).
BLUESTACKS_PORTS = range(5705, 5905, 10)


def ldplayer_port(index: int) -> int:
    return LDPLAYER_BASE + LDPLAYER_STEP * index


def _listed(cfg: DeviceConfig, found: Iterable["Choice"]) -> "Choice | None":
    from farmbot.instances import same_instance

    return next((c for c in found if c.mode == cfg.mode and same_instance(c.device(cfg), cfg)), None)


def ports_of(cfg: DeviceConfig, found: Iterable["Choice"] = ()) -> frozenset[int]:
    """Every ADB port the instance ``cfg`` plays listens on (``found``: the
    instances listed, for the ports MuMu and BlueStacks report)."""
    if cfg.mode == "ldplayer":
        return frozenset({ldplayer_port(cfg.ldplayer_index)})
    if cfg.mode == "mumu":
        listed = _listed(cfg, found)
        own = listed.port if listed is not None and listed.port else MUMU_BASE + MUMU_STEP * cfg.mumu_index
        return frozenset({own}) | (MUMU_FIRST if cfg.mumu_index == 0 else frozenset())
    if cfg.mode == "bluestacks":
        listed = _listed(cfg, found)
        return frozenset({listed.port}) if listed is not None and listed.port else frozenset()
    match = re.search(r":(\d+)$", cfg.serial or "")
    return frozenset({int(match.group(1))}) if match else frozenset()


def clash(device: DeviceConfig, others: dict[int, DeviceConfig], found: Iterable["Choice"] = ()) -> int | None:
    """The farm (of ``others``) whose instance wants one of the ports
    ``device``'s does: the two can't be connected at the same time."""
    from farmbot.instances import same_instance

    found = list(found)
    mine = ports_of(device, found)
    if not mine:
        return None
    return next((n for n, other in others.items()
                 if not same_instance(other, device) and mine & ports_of(other, found)), None)


def movable(device: DeviceConfig, other: DeviceConfig) -> bool:
    """True if COCFARM settles a clash between these two without a copy:
    BlueStacks is given another port (:func:`bluestacks_port`)."""
    return "bluestacks" in (device.mode, other.mode)


def ldplayer_avoid(install: str, others: Iterable[DeviceConfig], found: Iterable["Choice"] = ()) -> set[int]:
    """The LDPlayer instance numbers a new instance of ``install`` mustn't
    get: the ones whose port another farm's instance wants (BlueStacks
    aside: it moves), and the numbers another LDPlayer has (one numbering
    for every version)."""
    found = list(found)
    taken: set[int] = set()
    for cfg in others:
        if cfg.mode != "bluestacks":
            taken |= ports_of(cfg, found)
    avoid = {k for k in range(LDPLAYER_MOST) if ldplayer_port(k) in taken}
    avoid |= {int(c.key) for c in found if c.mode == "ldplayer" and c.install and c.install != install}
    return avoid


def bluestacks_port(current: int | None, others: Iterable[int], used: Iterable[int] = ()) -> int | None:
    """A port for a BlueStacks instance to have instead of ``current``, or
    None to keep it: kept unless it's one of ``others`` (the other
    emulators' instances' ports) or in LDPlayer's range (asked only while
    another emulator is installed). ``used``: ports BlueStacks' other
    instances have."""
    others = set(others)
    if current is not None and current not in others and not _ldplayer_range(current):
        return None
    if current is None and not others:
        return None
    taken = others | set(used)
    return next((p for p in BLUESTACKS_PORTS if p not in taken), None)


def _ldplayer_range(port: int) -> bool:
    return LDPLAYER_BASE <= port < ldplayer_port(LDPLAYER_MOST) and (port - LDPLAYER_BASE) % LDPLAYER_STEP == 0
