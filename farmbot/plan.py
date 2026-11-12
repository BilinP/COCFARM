"""Where a battle fought for stars goes in.

Farming spreads the army round the base (loot sits all round it). A battle
fought for stars (war, ranked) does better with the army together: it goes
in from one side, or from the two sides meeting at a corner, picked from
the deploy points found. Spells then follow the push: cast part-way in from
that side, where the troops are breaking in, not in the middle of the base.

What the bot knows of the army and the base can steer it further (each only
once the user has taught its pictures, see ``farmbot/buildings.py``): an
air army goes in facing the most Air Defenses, a ground army sends its
heroes and siege first, and a cheap lure troop goes in first by the Clan
Castle to pull its troops out, the push then coming in from that side.
"""

from __future__ import annotations

import logging

import math

from farmbot.geometry import Point

log = logging.getLogger(__name__)

RING = ("nw", "ne", "se", "sw")  # the sides in order round the base: each meets the next at a corner
TRUSTED = ("red line", "building ring")  # where a side's points may come from to be pushed in from
NEAR_TIE = 0.85  # a side with at least this share of the best side's points is as good a pick


def focus_sides(edges: dict[str, list[Point]], origins: dict[str, str], sides: list[str], how: str,
                rng=None, toward: list[Point] | None = None) -> list[str] | None:
    """The side(s) to push in from, or None to spread round the base as
    usual (``how`` "spread", or no side found on the base itself).

    ``how``: "side", one side; "corner", two neighbouring sides. Only sides
    whose points were found on the base (the red line or the building ring,
    per ``origins``) qualify, never a fixed line. The one with the most
    points wins (the widest front, the one least cut off by the screen's
    edges and buttons); near ties are picked between at random, so the
    push doesn't always come from the same side.

    ``toward``: places on the base the push is to face (say, the Air
    Defenses): the pick with the most of them nearest it wins, the widest
    front among equals."""
    if how not in ("side", "corner"):
        return None
    usable = {s: len(edges.get(s, [])) for s in sides if origins.get(s) in TRUSTED and edges.get(s)}
    if toward and usable:
        facing = {s: 0 for s in usable}
        for place in toward:
            facing[nearest_side(edges, list(usable), place)] += 1
        if how == "corner":
            pairs = [(RING[i], RING[(i + 1) % len(RING)]) for i in range(len(RING))]
            pairs = [p for p in pairs if p[0] in usable and p[1] in usable]
            if pairs:
                return list(max(pairs, key=lambda p: (facing[p[0]] + facing[p[1]], usable[p[0]] + usable[p[1]])))
        return [max(usable, key=lambda s: (facing[s], usable[s]))]
    if how == "side":
        options = {(s,): n for s, n in usable.items()}
    else:
        options = {}
        for i, a in enumerate(RING):
            b = RING[(i + 1) % len(RING)]
            if a in usable and b in usable:
                options[(a, b)] = usable[a] + usable[b]
    if not options:
        return None
    best = max(options.values())
    near = sorted(o for o, n in options.items() if n >= NEAR_TIE * best)
    pick = rng.choice(near) if rng is not None and len(near) > 1 else max(near, key=options.get)
    return list(pick)


def push_spot(edges: dict[str, list[Point]], center: Point | None, sides: list[str], depth: float) -> Point:
    """The spot ``depth`` of the way in from the middle of ``sides``' points
    towards the base's ``center`` (0: on the deploy line, 1: the centre)."""
    front = [p for s in sides for p in edges.get(s, [])]
    every = [p for pts in edges.values() for p in pts]
    fx, fy = sum(x for x, _ in front) / len(front), sum(y for _, y in front) / len(front)
    cx, cy = center or (sum(x for x, _ in every) / len(every), sum(y for _, y in every) / len(every))
    depth = min(max(depth, 0.0), 1.0)
    return round(fx + (cx - fx) * depth), round(fy + (cy - fy) * depth)


def middle(points: list[Point]) -> tuple[float, float]:
    return sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)


def nearest_side(edges: dict[str, list[Point]], sides: list[str], place: Point) -> str:
    """Of ``sides`` (each with points), the one whose deploy points come nearest ``place``."""
    return min(sides, key=lambda s: min(math.dist(place, p) for p in edges[s]))


def nearest_point(edges: dict[str, list[Point]], side: str, place: Point) -> Point:
    """The deploy point of ``side`` nearest ``place``."""
    return min(edges[side], key=lambda p: math.dist(place, p))


def army_kind(screen, troops: list, air: list, ground: list, same) -> str | None:
    """"air" or "ground": what most of the ``troops`` cards are, by the
    ``air`` and ``ground`` card pictures (``same(screen, card, picture)``
    tells a card by a picture); None if no card is known or it's even."""
    def count(pictures):
        return sum(1 for card in troops if any(same(screen, card, p) for p in pictures))
    on_air, on_ground = count(air), count(ground)
    if on_air == on_ground:
        return None
    return "air" if on_air > on_ground else "ground"
