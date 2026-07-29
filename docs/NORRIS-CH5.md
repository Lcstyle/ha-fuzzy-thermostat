# Provenance: Norris, chapter 5, mapped onto this codebase

This project was implemented from a single primary source:

> **Donald J. Norris**, *Beginning Artificial Intelligence with the Raspberry
> Pi*, Apress, Berkeley, CA, 2017 — **Chapter 5, "Fuzzy Logic System"**,
> DOI [10.1007/978-1-4842-2743-5_5](https://doi.org/10.1007/978-1-4842-2743-5_5).

This document is the cross-reference: which idea in the chapter became which
piece of code, where this implementation follows the source exactly, and where
— and why — it deliberately departs. No text from the book is reproduced here;
the mapping is conceptual, and the chapter is worth reading in its own right.

## 1. The seven-step FLS algorithm (Table 5-2) → the engine

The chapter condenses a fuzzy logic system into seven steps. The engine in
[`fuzzy/engine.py`](../custom_components/fuzzy_thermostat/fuzzy/engine.py)
mirrors them one-to-one:

| Book step | Book name | This codebase |
|---|---|---|
| 1 | Define linguistic variables and terms | `Variable(name, universe, terms)` |
| 2 | Construct membership functions | `Triangular` / `Trapezoidal` in [`membership.py`](../custom_components/fuzzy_thermostat/fuzzy/membership.py) |
| 3 | Build rule set | `Rule(antecedent, consequent, weight, operator)` |
| 4 | Fuzzification | `Variable.fuzzify()` |
| 5 | Inference | firing strengths in `FuzzyController.evaluate()` — AND = min, OR = max; implication clips ("flattops", the book's word) the consequent at the firing strength |
| 6 | Aggregation | pointwise **max** across all clipped consequents |
| 7 | Defuzzification | `_centroid()` (the book's chosen method) or `_weighted_average()` |

The four principal components of the chapter's block diagram (Figure 5-1) —
fuzzifier, rules, inference engine, defuzzifier — are the four things a
`FuzzyController` is constructed from.

## 2. Membership function shapes (Demo 5-1 discussion) → `membership.py`

The chapter surveys candidate shapes (Gaussian, trapezoidal, singleton,
piecewise linear, sinusoidal, exponential) and argues for triangles: the
Gaussian may model human judgement better, but its mathematics buys nothing
for control. This implementation follows that argument to its conclusion and
ships **only piecewise-linear shapes** — with the added engineering reason
that a piecewise-linear aggregate admits an *exact* centroid (§5 below).

The book's `trimf([a, a, c])` idiom — a triangle with a vertical edge, used
for every open-shouldered extreme term — is preserved: `Triangular(a, a, c)`
returns full membership at the shared breakpoint.

The OR connective (used in Demo 5-1's tipping rules: *bad food OR poor
service*) is carried by `Rule(..., operator="or")`, evaluated as max, exactly
as the chapter applies it.

## 3. Demo 5-3, the HVAC controller → `fuzzy/hvac.py`

The chapter's second project decomposes room temperature into
*{cold, comfortable, hot}*, treats the occupant's target temperature as a
second linguistic input over the same universe, and drives everything from a
3×3 matrix of command actions (the book's **Table 5-3**), of which six cells
demand action. `build_command_controller()` implements that matrix verbatim —
same variables, same six action rules, AND = min throughout.

Three deliberate departures, each documented at the point of divergence:

1. **The output universe is a signed demand in [−1, +1]**, not a reuse of the
   temperature scale. The book reuses 50–90 for the command variable and then
   has to *discover empirically* that ≈65–75 means "no change", ≈82–83 means
   heat, ≈56–65 means cool. Normalising the output makes the deadband a
   property of the design rather than a finding about it. The book's own
   observed result — a "no change" band of roughly ±4 °F around target —
   emerges here the same way.
2. **The matrix diagonal gets explicit `hold` rules.** The book drops the
   no-change cells and accepts an empty rule base when room and target agree.
   Keeping them means the aggregate always has mass in normal operation.
3. **A trend input with two low-weight damping rules** (*hot and falling →
   hold*, *cold and rising → hold*). This is the classic fuzzy anti-overshoot
   extension; it is not in the chapter, and it is weighted low so a genuinely
   wrong temperature still wins.

## 4. The defuzzification survey → two defuzzifiers

The chapter surveys centroid, bisector, mean/smallest/largest-of-maximum, and
weighted average, and chooses the centroid for both demos. This codebase
implements two of the surveyed methods:

* **Centroid** — used by the command controller, as in the source.
* **Weighted average** (Σμᵢ·Wᵢ / Σμᵢ, with Wᵢ = each term's peak) — used by
  the setpoint controller, for a reason the survey makes visible: the
  centroid of a clipped shoulder set **can never reach the ends of its
  universe**, so a centroid-defuzzified setpoint could never actually hit its
  configured comfort bounds. The weighted average can, and since it is a
  convex combination of peaks inside [0, 1], the bounds hold structurally.

## 5. Reproducing the book's numbers (Tables 5-4 to 5-8) → `tests/test_hvac.py`

The strongest link to the source is numeric. The test suite rebuilds Demo 5-3
with the book's exact membership functions and sampling, and asserts this
engine's outputs against the values printed in the book's test tables —
**thirteen cells reproduce to the printed precision (±0.01)**.

Two findings from that exercise, recorded because they are useful to anyone
else implementing from this chapter:

* **The centroid must be geometric.** The naive discrete weighted mean over
  the samples (`sum(mu*z)/sum(mu)`) yields 57.52 where the book prints 57.78.
  Only exact per-segment trapezoidal integration of area and first moment
  over the piecewise-linear aggregate reproduces the published values. That
  integral is `FuzzyController._centroid()`.
* **The printed listing has errata**, so full-table reproduction is not a
  sound goal: `np.fmax` is called with six positional arrays (a `TypeError`),
  the pairwise fallback references undefined names, and the tables were
  evidently generated with an asymmetric *target hot* membership function
  (`[50, 90, 90]`, where the room variable uses `[70, 90, 90]`). The
  regression therefore asserts only the cells that exact integration of the
  printed membership functions can produce, and uses the as-published
  asymmetric term to match them. The production controller uses the
  symmetric form.

One more behavioural inheritance: the book notes it had to nudge its extreme
test inputs because defuzzification "throws an error when the temperatures
match ... at the extremes" — the empty-aggregate division by zero. This
engine returns `None` ("no opinion") instead, and the caller decides what
that means; there is a test pinning that behaviour to the book's footnote.

## 6. What the chapter does not cover

The outdoor-compensated setpoint controller (`build_setpoint_controller`),
the slew limiting, the minimum-cycle protection, the fail-safe sensor
handling, and the Home Assistant entity itself are engineering added on top
of the chapter's foundations — the chapter supplies the inference machinery
and the HVAC rule pattern, not the supervisory policy. Those additions are
documented in the [README](../README.md).
