import pytest

from custom_components.fuzzy_thermostat.fuzzy.engine import (
    FuzzyController,
    Rule,
    Variable,
)
from custom_components.fuzzy_thermostat.fuzzy.membership import Triangular


def three(lo: float, hi: float) -> dict:
    mid = (lo + hi) / 2
    return {
        "low": Triangular(lo, lo, mid),
        "mid": Triangular(lo, mid, hi),
        "high": Triangular(mid, hi, hi),
    }


@pytest.fixture
def simple() -> FuzzyController:
    x = Variable("x", (0, 10), three(0, 10))
    out = Variable("out", (0, 10), three(0, 10))
    rules = [
        Rule((("x", "low"),), "low"),
        Rule((("x", "mid"),), "mid"),
        Rule((("x", "high"),), "high"),
    ]
    return FuzzyController([x], out, rules, resolution=101)


class TestFuzzify:
    def test_degrees(self, simple):
        r = simple.evaluate({"x": 2.5})
        assert r.memberships["x"]["low"] == pytest.approx(0.5)
        assert r.memberships["x"]["mid"] == pytest.approx(0.5)
        assert r.memberships["x"]["high"] == 0.0

    def test_input_clamped_to_universe(self, simple):
        assert simple.evaluate({"x": -50}).value == pytest.approx(
            simple.evaluate({"x": 0}).value
        )


class TestInference:
    def test_and_is_min_or_is_max(self):
        a = Variable("a", (0, 10), three(0, 10))
        b = Variable("b", (0, 10), three(0, 10))
        out = Variable("out", (0, 10), three(0, 10))
        anded = FuzzyController(
            [a, b], out, [Rule((("a", "high"), ("b", "high")), "high")]
        )
        ored = FuzzyController(
            [a, b],
            out,
            [Rule((("a", "high"), ("b", "high")), "high", operator="or")],
        )
        crisp = {"a": 10, "b": 7.5}  # degrees: 1.0 and 0.5
        assert anded.evaluate(crisp).firings[
            "IF a is high AND b is high THEN high"
        ] == pytest.approx(0.5)
        assert ored.evaluate(crisp).firings[
            "IF a is high OR b is high THEN high"
        ] == pytest.approx(1.0)

    def test_weight_scales_firing(self):
        x = Variable("x", (0, 10), three(0, 10))
        out = Variable("out", (0, 10), three(0, 10))
        c = FuzzyController([x], out, [Rule((("x", "high"),), "high", weight=0.5)])
        r = c.evaluate({"x": 10})
        assert r.firings["IF x is high THEN high [w=0.5]"] == pytest.approx(0.5)


class TestDefuzzification:
    def test_full_shoulder_centroid(self, simple):
        """A fully fired right shoulder tri(5,10,10) has its CoG at 10 - 5/3."""
        r = simple.evaluate({"x": 10})
        assert r.value == pytest.approx(10 - 5 / 3, abs=1e-6)

    def test_symmetric_pull_lands_in_middle(self, simple):
        r = simple.evaluate({"x": 5})
        assert r.value == pytest.approx(5.0, abs=1e-6)

    def test_no_rule_fired_returns_none(self):
        x = Variable("x", (0, 10), three(0, 10))
        out = Variable("out", (0, 10), three(0, 10))
        c = FuzzyController([x], out, [Rule((("x", "high"),), "high")])
        assert c.evaluate({"x": 0}).value is None

    def test_weighted_average_reaches_universe_ends(self):
        """Unlike the clipped centroid, weighted-average can output exactly 0/1."""
        x = Variable("x", (0, 10), three(0, 10))
        out = Variable(
            "p",
            (0, 1),
            {"zero": Triangular(0, 0, 1), "one": Triangular(0, 1, 1)},
        )
        c = FuzzyController(
            [x],
            out,
            [Rule((("x", "low"),), "zero"), Rule((("x", "high"),), "one")],
            defuzz="weighted_average",
        )
        assert c.evaluate({"x": 0}).value == pytest.approx(0.0)
        assert c.evaluate({"x": 10}).value == pytest.approx(1.0)


class TestValidation:
    def test_unknown_variable_rejected(self):
        x = Variable("x", (0, 10), three(0, 10))
        out = Variable("out", (0, 10), three(0, 10))
        with pytest.raises(ValueError):
            FuzzyController([x], out, [Rule((("y", "low"),), "low")])

    def test_unknown_term_rejected(self):
        x = Variable("x", (0, 10), three(0, 10))
        out = Variable("out", (0, 10), three(0, 10))
        with pytest.raises(ValueError):
            FuzzyController([x], out, [Rule((("x", "tepid"),), "low")])
        with pytest.raises(ValueError):
            FuzzyController([x], out, [Rule((("x", "low"),), "tepid")])


class TestResult:
    def test_top_rules_sorted_and_filtered(self, simple):
        r = simple.evaluate({"x": 2.5})
        top = r.top_rules()
        assert len(top) == 2  # 'high' never fired
        assert top[0][1] >= top[1][1]
