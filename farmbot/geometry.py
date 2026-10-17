"""Small geometry helpers shared by vision and attack code."""

from __future__ import annotations

from dataclasses import dataclass

Point = tuple[int, int]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_list(cls, values: list[int] | tuple[int, ...]) -> Rect:
        x, y, w, h = values
        return cls(int(x), int(y), int(w), int(h))

    @property
    def center(self) -> Point:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def contains(self, point: Point) -> bool:
        px, py = point
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def resolve(self, width: int, height: int) -> Rect:
        """Turn screen-edge-relative values into absolute ones.

        Negative ``x``/``y`` count from the right/bottom edge, and a width
        or height of zero or less stretches to that edge (minus its value),
        so one config fits screens of different aspect ratios.
        """
        x = self.x + width if self.x < 0 else self.x
        y = self.y + height if self.y < 0 else self.y
        w = width - x + self.w if self.w <= 0 else self.w
        h = height - y + self.h if self.h <= 0 else self.h
        return Rect(x, y, w, h)

    def around(self, point: Point) -> Rect:
        """Same-sized rect centred on ``point``."""
        return Rect(point[0] - self.w // 2, point[1] - self.h // 2, self.w, self.h)


def points_along(start: Point, end: Point, count: int) -> list[Point]:
    """``count`` evenly spaced points on the segment start→end (inclusive)."""
    if count <= 0:
        return []
    if count == 1:
        return [((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)]
    step = 1 / (count - 1)
    return [
        (round(start[0] + (end[0] - start[0]) * i * step),
         round(start[1] + (end[1] - start[1]) * i * step))
        for i in range(count)
    ]


def split_evenly(total: int, parts: int) -> list[int]:
    """Split ``total`` into ``parts`` integers that differ by at most one."""
    if parts <= 0:
        return []
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]
