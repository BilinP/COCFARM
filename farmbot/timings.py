"""How long each part of a round takes, and what each human-like habit
adds to it (see ``farmbot/human.py``), for the "Like a person" page.

The page shows typical times, measured on a live account (``DEFAULTS``):
each part's time without the habits, what the ones picked add, and the
time with them (:class:`Timings`). Nothing is kept in a file.

While a round runs, the game keeps a :class:`Ledger`: the bot names the
part being played (village care, upgrades, the search, the battle, the
rest after it), the ledger times it, and every human-like step notes what
it added and what it adds on average, whether its habit is on or off
(how ``DEFAULTS`` was measured; a round's ledger is dropped once it's over).
"""

from __future__ import annotations

from collections.abc import Callable


PHASES = {
    "home": ("village", "upgrades", "search", "battle", "rest"),
    "builder": ("village", "upgrades", "battle", "rest"),
}
PHASE_NAMES = {"village": "Village care", "upgrades": "Upgrades and walls", "search": "Searching",
               "battle": "Battle", "rest": "Rest between rounds"}

# Typical rounds, from a live run with every habit on: per part, its
# seconds, what the habits added to them (taken off for "without"), and
# what each habit adds on average. Upgrades: estimated (none were bought).
DEFAULTS: dict[str, dict[str, dict]] = {
    "home": {
        "village": {"seconds": 19.0, "added": 1.3, "expected": {"tap_hold": 0.7, "waits": 0.8, "fatigue": 0.6,
                                                                "browse": 0.64}},
        "upgrades": {"seconds": 30.0, "added": 2.0, "expected": {"tap_hold": 1.0, "waits": 1.5, "swipes": 0.3,
                                                                 "curved_swipes": 0.4}},
        "search": {"seconds": 14.0, "added": 1.9, "expected": {"tap_hold": 0.23, "waits": 0.5, "look_at_bases": 1.7,
                                                               "fatigue": 0.4}},
        "battle": {"seconds": 108.0, "added": 16.0, "expected": {"tap_hold": 0.7, "waits": 0.46, "swipes": 0.4,
                                                                 "curved_swipes": 0.72, "battle_rhythm": 3.4,
                                                                 "result_pause": 2.23, "end_spread": 2.25,
                                                                 "scout_look": 2.69}},
        "rest": {"seconds": 3.3, "added": 0.3, "expected": {"waits": 0.3, "distracted": 1.4}},
    },
    "builder": {
        "village": {"seconds": 36.0, "added": 2.9, "expected": {"tap_hold": 1.0, "waits": 1.8, "swipes": 0.21,
                                                                "curved_swipes": 0.24}},
        "upgrades": {"seconds": 25.0, "added": 2.0, "expected": {"tap_hold": 0.8, "waits": 1.2, "swipes": 0.2,
                                                                 "curved_swipes": 0.3}},
        "battle": {"seconds": 152.0, "added": 12.0, "expected": {"tap_hold": 1.64, "waits": 2.2, "swipes": 0.09,
                                                                 "curved_swipes": 0.16, "battle_rhythm": 1.0,
                                                                 "result_pause": 2.23, "scout_look": 2.69}},
        "rest": {"seconds": 3.3, "added": 0.3, "expected": {"waits": 0.3, "distracted": 1.4}},
    },
}


def _part() -> dict:
    return {"seconds": 0.0, "added": 0.0, "expected": {}}


class Ledger:
    """One round's parts: their time, and what the habits added."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.village = "home"
        self.phase: str | None = None
        self._started = 0.0
        self.parts: dict[tuple[str, str], dict] = {}

    def begin(self, village: str, phase: str) -> None:
        """From now on, the time goes to ``phase`` of ``village``."""
        self.end()
        self.village, self.phase, self._started = village, phase, self.clock()

    def end(self) -> None:
        if self.phase is not None:
            self._part()["seconds"] += self.clock() - self._started
            self.phase = None

    def _part(self) -> dict:
        return self.parts.setdefault((self.village, self.phase), _part())

    def note(self, habit: str, expected: float, added: float = 0.0) -> None:
        """A step of ``habit`` was taken (or would have been, were it on):
        it adds ``expected`` seconds on average, and added ``added``."""
        if self.phase is None or (expected <= 0 and not added):
            return
        part = self._part()
        part["expected"][habit] = part["expected"].get(habit, 0.0) + expected
        part["added"] += added

    def take(self) -> dict[tuple[str, str], dict]:
        """The round's parts, and a fresh start for the next one."""
        self.end()
        parts, self.parts = self.parts, {}
        return parts


class Timings:
    """What a round's parts take and what the habits add: the typical
    numbers (``DEFAULTS``). Nothing is measured into a file."""

    def summary(self, village: str, habits: set[str]) -> list[tuple[str, float, float, float]]:
        """Rows of (part, seconds without the habits, seconds the ``habits``
        add, seconds with them), the total last."""
        parts = DEFAULTS[village]
        rows = []
        for phase in PHASES[village]:
            part = parts.get(phase, {})
            plain = max(part.get("seconds", 0.0) - part.get("added", 0.0), 0.0)
            added = sum(v for h, v in part.get("expected", {}).items() if h in habits)
            rows.append((PHASE_NAMES[phase], plain, added, plain + added))
        rows.append(("A whole round", sum(r[1] for r in rows), sum(r[2] for r in rows), sum(r[3] for r in rows)))
        return rows

    def habit_cost(self, village: str, habit: str) -> float:
        """Seconds ``habit`` adds to a round of ``village``, on average."""
        return sum(p.get("expected", {}).get(habit, 0.0) for p in DEFAULTS[village].values())


def duration(seconds: float) -> str:
    """A short duration: 0.4 s, 12 s, 2 min 5 s."""
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def cost(seconds: float) -> str:
    """What a habit adds: "no time", "+80 ms", "+1.2 s"."""
    if seconds < 0.005:
        return "no time"
    if seconds < 1:
        return f"+{seconds * 1000:.0f} ms"
    return f"+{duration(seconds)}"
