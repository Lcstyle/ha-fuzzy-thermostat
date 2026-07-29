"""A small, dependency-free Mamdani fuzzy inference engine.

The structure follows the classic seven-step fuzzy-logic-system algorithm
(initialisation of linguistic variables, membership functions and rules, then
fuzzification -> inference -> aggregation -> defuzzification) as presented in
Donald J. Norris, *Beginning Artificial Intelligence with the Raspberry Pi*
(Apress, 2017), chapter 5:

    1-3. Initialisation  -> :class:`Variable`, membership functions, :class:`Rule`
    4.   Fuzzification   -> :meth:`Variable.fuzzify`
    5.   Inference       -> AND = min, OR = max; implication clips ("flattops")
                            the consequent membership function at the firing
                            strength
    6.   Aggregation     -> pointwise max across all clipped consequents
    7.   Defuzzification -> centroid (centre of gravity) by default

Two deliberate departures from the book's skfuzzy-based demos:

* No numpy/scipy/skfuzzy. Everything is plain Python so the engine can ship
  inside a Home Assistant integration.
* The centroid is integrated exactly over the piecewise-linear aggregate
  (per-segment trapezoidal area and first moment) rather than approximated by
  a discrete weighted mean of the samples. This is what reproduces the book's
  published Demo 5-3 outputs to the printed precision — a plain
  ``sum(mu*z)/sum(mu)`` over the samples does not (it gives e.g. 57.52 where
  the book prints 57.78).

An empty aggregate (no rule fired) yields ``value=None`` instead of raising —
the caller decides what "no opinion" means. The book's demo had to nudge its
inputs ("51*" in its test tables) to dodge exactly this division by zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping, Sequence

__all__ = ["Variable", "Rule", "Result", "FuzzyController"]

MembershipFn = Callable[[float], float]

_EPS = 1e-9


@dataclass(frozen=True)
class Variable:
    """A linguistic variable: a universe of discourse plus named terms."""

    name: str
    universe: tuple[float, float]
    terms: Mapping[str, MembershipFn]

    def __post_init__(self) -> None:
        lo, hi = self.universe
        if not lo < hi:
            raise ValueError(f"{self.name}: universe must satisfy lo < hi")

    def clamp(self, x: float) -> float:
        lo, hi = self.universe
        return lo if x < lo else hi if x > hi else x

    def fuzzify(self, x: float) -> dict[str, float]:
        """Step 4: crisp value -> degree of membership per linguistic term."""
        cx = self.clamp(x)
        return {term: mf(cx) for term, mf in self.terms.items()}


@dataclass(frozen=True)
class Rule:
    """IF <antecedent> THEN <output is term>.

    ``antecedent`` is a sequence of ``(variable_name, term_name)`` pairs
    combined with a single connective: ``and`` -> min, ``or`` -> max (the two
    Mamdani connectives used throughout the source chapter). ``weight`` scales
    the firing strength, which is how low-confidence expert rules (for example
    trend damping) are expressed without a second rule base.
    """

    antecedent: tuple[tuple[str, str], ...]
    consequent: str
    weight: float = 1.0
    operator: Literal["and", "or"] = "and"

    def text(self) -> str:
        joiner = f" {self.operator.upper()} "
        cond = joiner.join(f"{v} is {t}" for v, t in self.antecedent)
        w = "" if self.weight == 1.0 else f" [w={self.weight:g}]"
        return f"IF {cond} THEN {self.consequent}{w}"


@dataclass(frozen=True)
class Result:
    """Outcome of one evaluation, kept rich enough to explain itself."""

    value: float | None
    firings: dict[str, float] = field(default_factory=dict)
    memberships: dict[str, dict[str, float]] = field(default_factory=dict)

    def top_rules(self, n: int = 3) -> list[tuple[str, float]]:
        fired = [(t, s) for t, s in self.firings.items() if s > _EPS]
        fired.sort(key=lambda kv: kv[1], reverse=True)
        return fired[:n]


class FuzzyController:
    """A complete FLS: input variables, one output variable, and a rule base."""

    def __init__(
        self,
        inputs: Sequence[Variable],
        output: Variable,
        rules: Sequence[Rule],
        *,
        resolution: int = 201,
        defuzz: Literal["centroid", "weighted_average"] = "centroid",
    ) -> None:
        if resolution < 2:
            raise ValueError("resolution must be >= 2")
        self.inputs = {v.name: v for v in inputs}
        self.output = output
        self.rules = list(rules)
        self.defuzz = defuzz
        lo, hi = output.universe
        step = (hi - lo) / (resolution - 1)
        self._samples = [lo + i * step for i in range(resolution)]
        for rule in self.rules:
            for var, term in rule.antecedent:
                if var not in self.inputs:
                    raise ValueError(f"rule references unknown variable {var!r}")
                if term not in self.inputs[var].terms:
                    raise ValueError(f"{var!r} has no term {term!r}")
            if rule.consequent not in output.terms:
                raise ValueError(f"output has no term {rule.consequent!r}")

    # -- steps 4-7 ---------------------------------------------------------

    def evaluate(self, crisp: Mapping[str, float]) -> Result:
        # Step 4: fuzzification.
        memberships = {
            name: var.fuzzify(crisp[name]) for name, var in self.inputs.items()
        }

        # Step 5: inference — firing strength per rule (AND=min / OR=max),
        # scaled by the rule weight.
        firings: dict[str, float] = {}
        strengths: list[tuple[float, MembershipFn]] = []
        for rule in self.rules:
            degrees = [memberships[v][t] for v, t in rule.antecedent]
            strength = (min(degrees) if rule.operator == "and" else max(degrees))
            strength *= rule.weight
            firings[rule.text()] = strength
            if strength > _EPS:
                strengths.append((strength, self.output.terms[rule.consequent]))

        if not strengths:
            return Result(value=None, firings=firings, memberships=memberships)

        if self.defuzz == "weighted_average":
            value = self._weighted_average(strengths)
        else:
            value = self._centroid(strengths)
        return Result(value=value, firings=firings, memberships=memberships)

    # -- defuzzifiers ------------------------------------------------------

    @staticmethod
    def _weighted_average(strengths: list[tuple[float, MembershipFn]]) -> float:
        """Sigma(mu_i * W_i) / Sigma(mu_i) with W_i = the term's peak.

        The "weighted average" method from the source chapter's defuzzification
        survey. Unlike the centroid of a clipped shoulder set, it can actually
        reach the ends of the output universe, which matters when the output
        must be able to saturate (for example a setpoint that must be allowed
        to hit its configured bounds exactly).
        """
        num = 0.0
        den = 0.0
        for strength, mf in strengths:
            peak = getattr(mf, "peak", None)
            if peak is None:  # pragma: no cover - misuse guard
                raise TypeError("weighted_average needs terms with a .peak")
            num += strength * peak
            den += strength
        return num / den

    def _centroid(self, strengths: list[tuple[float, MembershipFn]]) -> float | None:
        """Centre of gravity of the max-aggregated, min-clipped consequents.

        Step 6 (aggregation, pointwise max) and step 7 (centroid) fused over
        one pass of the sampled universe. Between consecutive samples the
        aggregate is treated as linear, and area/first-moment are integrated
        exactly for each trapezoidal segment:

            area   = (m0 + m1) / 2 * dz
            moment = dz * (m0 * (2*z0 + z1) + m1 * (z0 + 2*z1)) / 6
        """
        agg = []
        for z in self._samples:
            m = 0.0
            for strength, mf in strengths:
                clipped = min(strength, mf(z))  # implication: flattop
                if clipped > m:
                    m = clipped  # aggregation: max
            agg.append(m)

        area = 0.0
        moment = 0.0
        for i in range(len(self._samples) - 1):
            z0, z1 = self._samples[i], self._samples[i + 1]
            m0, m1 = agg[i], agg[i + 1]
            dz = z1 - z0
            area += (m0 + m1) / 2.0 * dz
            moment += dz * (m0 * (2 * z0 + z1) + m1 * (z0 + 2 * z1)) / 6.0
        if area < _EPS:
            return None
        return moment / area
