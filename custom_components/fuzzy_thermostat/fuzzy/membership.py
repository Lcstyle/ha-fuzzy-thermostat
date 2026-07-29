"""Membership functions for fuzzy sets.

A membership function maps a crisp value to a degree of membership in [0, 1].
Only piecewise-linear shapes are provided (triangular and trapezoidal): they
cover every shape used in practice for thermostat control, they make the
centroid defuzzifier exactly integrable, and they keep this package free of
numpy/scipy — a hard requirement for shipping inside a Home Assistant
integration.

``Triangular(a, a, c)`` and ``Triangular(a, c, c)`` are the open-shouldered
edge sets (degenerate vertical edge), matching the common ``trimf`` semantics:
the membership is 1.0 at the shared breakpoint.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Triangular", "Trapezoidal"]


@dataclass(frozen=True)
class Triangular:
    """Triangle with feet at ``a`` and ``c`` and peak at ``b`` (a <= b <= c)."""

    a: float
    b: float
    c: float

    def __post_init__(self) -> None:
        if not self.a <= self.b <= self.c:
            raise ValueError(f"triangular requires a <= b <= c, got {self}")

    @property
    def peak(self) -> float:
        """Representative crisp value of the term (used by weighted-average)."""
        return self.b

    def __call__(self, x: float) -> float:
        if x < self.a or x > self.c:
            return 0.0
        if x <= self.b:  # rising edge (vertical if a == b)
            return 1.0 if self.b == self.a else (x - self.a) / (self.b - self.a)
        # falling edge (vertical if b == c)
        return 1.0 if self.c == self.b else (self.c - x) / (self.c - self.b)


@dataclass(frozen=True)
class Trapezoidal:
    """Trapezoid with feet at ``a``/``d`` and plateau between ``b`` and ``c``."""

    a: float
    b: float
    c: float
    d: float

    def __post_init__(self) -> None:
        if not self.a <= self.b <= self.c <= self.d:
            raise ValueError(f"trapezoidal requires a <= b <= c <= d, got {self}")

    @property
    def peak(self) -> float:
        return (self.b + self.c) / 2.0

    def __call__(self, x: float) -> float:
        if x < self.a or x > self.d:
            return 0.0
        if x < self.b:
            return 1.0 if self.b == self.a else (x - self.a) / (self.b - self.a)
        if x <= self.c:
            return 1.0
        return 1.0 if self.d == self.c else (self.d - x) / (self.d - self.c)
