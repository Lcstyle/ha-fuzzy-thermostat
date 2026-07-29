"""Domain-layer tests, including a regression against the source chapter.

The book regression rebuilds Demo 5-3 from Norris, *Beginning Artificial
Intelligence with the Raspberry Pi* (Apress, 2017) ch. 5 exactly as published —
including the asymmetric ``target hot = trimf([50, 90, 90])`` (the room
variable uses ``[70, 90, 90]``), which the book's own test tables were clearly
generated with — and checks this engine against the numeric outputs printed in
the book's Tables 5-4 through 5-8.

Only the thirteen cells that exact piecewise-linear integration of the printed
membership functions can produce are asserted. The remaining cells disagree
with any consistent reading of the printed program (the published listing
cannot run as-is: ``np.fmax`` is called with six positional arrays, and the
pairwise fallback references undefined names), so they are documented rather
than enforced.
"""
import pytest

from custom_components.fuzzy_thermostat.fuzzy.engine import (
    FuzzyController,
    Rule,
    Variable,
)
from custom_components.fuzzy_thermostat.fuzzy.hvac import (
    build_command_controller,
    build_load_controller,
    build_setpoint_controller,
)
from custom_components.fuzzy_thermostat.fuzzy.membership import Triangular


# ---------------------------------------------------------------------------
# Demo 5-3 regression
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo53() -> FuzzyController:
    room = Variable(
        "room",
        (50, 90),
        {
            "cold": Triangular(50, 50, 70),
            "comfortable": Triangular(50, 70, 90),
            "hot": Triangular(70, 90, 90),
        },
    )
    target = Variable(
        "target",
        (50, 90),
        {
            "cold": Triangular(50, 50, 70),
            "comfortable": Triangular(50, 70, 90),
            # As published: [50, 90, 90], NOT the symmetric [70, 90, 90].
            "hot": Triangular(50, 90, 90),
        },
    )
    command = Variable(
        "command",
        (50, 90),
        {
            "cool": Triangular(50, 50, 70),
            "no_change": Triangular(50, 70, 90),
            "heat": Triangular(70, 90, 90),
        },
    )
    rules = [
        Rule((("room", "cold"), ("target", "comfortable")), "heat"),
        Rule((("room", "cold"), ("target", "hot")), "heat"),
        Rule((("room", "comfortable"), ("target", "cold")), "cool"),
        Rule((("room", "comfortable"), ("target", "hot")), "heat"),
        Rule((("room", "hot"), ("target", "cold")), "cool"),
        Rule((("room", "hot"), ("target", "comfortable")), "cool"),
    ]
    # resolution 41 = the book's np.arange(50, 91, 1) sampling; every membership
    # kink for firing strengths in {.25, .5, .75, 1} lands on a sample, so the
    # piecewise-linear centroid is exact.
    return FuzzyController([room, target], command, rules, resolution=41)


BOOK_TABLE = [
    # (room, target, printed command output)
    (60, 50, 57.78),
    (70, 50, 56.67),
    (80, 50, 57.78),
    (90, 50, 56.67),
    (50, 60, 82.22),
    (60, 60, 70.00),
    (70, 60, 66.40),
    (80, 60, 66.40),
    (90, 60, 57.78),
    (50, 70, 83.33),
    (60, 70, 82.22),
    (80, 70, 70.00),
    (90, 70, 56.67),
]


@pytest.mark.parametrize("room,target,expected", BOOK_TABLE)
def test_demo53_reproduces_published_outputs(demo53, room, target, expected):
    result = demo53.evaluate({"room": room, "target": target})
    assert result.value == pytest.approx(expected, abs=0.01)


def test_demo53_degenerate_inputs_yield_none_not_error(demo53):
    """The book had to nudge its extreme test rows ('51*', '89*') because its
    defuzzifier raised on an empty aggregate. This engine reports no-opinion."""
    assert demo53.evaluate({"room": 50, "target": 50}).value is None
    assert demo53.evaluate({"room": 90, "target": 90}).value is None


# ---------------------------------------------------------------------------
# Command controller (production configuration)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def command() -> FuzzyController:
    return build_command_controller(50, 90)


class TestCommandController:
    def test_cold_room_wants_heat(self, command):
        r = command.evaluate({"room": 52, "target": 70, "trend": 0})
        assert r.value is not None and r.value > 0.4

    def test_hot_room_wants_cool(self, command):
        r = command.evaluate({"room": 88, "target": 70, "trend": 0})
        assert r.value is not None and r.value < -0.4

    def test_agreement_is_a_deadband(self, command):
        r = command.evaluate({"room": 70, "target": 70, "trend": 0})
        assert r.value is not None and abs(r.value) < 0.1

    def test_never_empty_in_normal_range(self, command):
        """The explicit hold rules mean the aggregate always has mass."""
        for room in range(50, 91, 5):
            for target in range(50, 91, 10):
                assert (
                    command.evaluate(
                        {"room": room, "target": target, "trend": 0}
                    ).value
                    is not None
                )

    def test_demand_is_monotone_in_room_temperature(self, command):
        prev = None
        for room in range(50, 91, 2):
            v = command.evaluate({"room": room, "target": 70, "trend": 0}).value
            if prev is not None:
                assert v <= prev + 1e-9  # hotter room -> never more heating
            prev = v

    def test_falling_trend_damps_cooling_demand(self, command):
        hot = {"room": 85, "target": 70}
        without = command.evaluate({**hot, "trend": 0}).value
        with_damping = command.evaluate({**hot, "trend": -2.5}).value
        assert abs(with_damping) < abs(without)

    def test_rising_trend_damps_heating_demand(self, command):
        cold = {"room": 55, "target": 70}
        without = command.evaluate({**cold, "trend": 0}).value
        with_damping = command.evaluate({**cold, "trend": 2.5}).value
        assert abs(with_damping) < abs(without)


# ---------------------------------------------------------------------------
# Setpoint (outdoor compensation) controller
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def setpoint() -> FuzzyController:
    return build_setpoint_controller(72, 92)


class TestSetpointController:
    def test_saturates_exactly_at_extremes(self, setpoint):
        assert setpoint.evaluate({"outdoor": 60}).value == pytest.approx(0.0)
        assert setpoint.evaluate({"outdoor": 72}).value == pytest.approx(0.0)
        assert setpoint.evaluate({"outdoor": 92}).value == pytest.approx(1.0)
        assert setpoint.evaluate({"outdoor": 100}).value == pytest.approx(1.0)

    def test_known_interpolation_point(self, setpoint):
        """Outdoor 78 with breaks 72/92: p = 0.3, so a 70-73 comfort band
        yields a 72.1 target — the worked example this controller was
        specified against."""
        p = setpoint.evaluate({"outdoor": 78}).value
        assert p == pytest.approx(0.3, abs=0.01)
        target = 73 - p * (73 - 70)
        assert target == pytest.approx(72.1, abs=0.05)

    def test_position_is_monotone_and_bounded(self, setpoint):
        prev = -1.0
        for tenths in range(600, 1051, 5):
            outdoor = tenths / 10.0
            p = setpoint.evaluate({"outdoor": outdoor}).value
            assert p is not None
            assert -1e-9 <= p <= 1 + 1e-9  # NEVER outside [0, 1]
            assert p >= prev - 1e-9  # hotter out -> never more relaxed
            prev = p

    def test_comfort_bounds_are_never_violated(self, setpoint):
        """The property the whole design hangs on: whatever the outdoor input,
        the interpolated target stays inside [comfort_min, comfort_max]."""
        lo, hi = 70.0, 73.0
        for outdoor in range(40, 121):
            p = setpoint.evaluate({"outdoor": outdoor}).value
            target = hi - p * (hi - lo)
            assert lo - 1e-9 <= target <= hi + 1e-9


# ---------------------------------------------------------------------------
# Load (internal gain) controller
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def load() -> FuzzyController:
    # e.g. a CPU package temperature: quiet desktop ~115, flat-out compute ~190
    return build_load_controller(115, 190)


class TestLoadController:
    def test_light_load_contributes_nothing(self, load):
        assert load.evaluate({"load": 100}).value == pytest.approx(0.0)
        assert load.evaluate({"load": 115}).value == pytest.approx(0.0)

    def test_heavy_load_caps_at_two_thirds(self, load):
        """Deliberate: even a flat-out load in a mild week must not command the
        heatwave setpoint floor — the outdoor channel owns the last third."""
        assert load.evaluate({"load": 190}).value == pytest.approx(2 / 3, abs=1e-6)
        assert load.evaluate({"load": 250}).value == pytest.approx(2 / 3, abs=1e-6)

    def test_monotone_and_bounded(self, load):
        prev = -1.0
        for v in range(90, 261, 2):
            p = load.evaluate({"load": v}).value
            assert p is not None and -1e-9 <= p <= 2 / 3 + 1e-9
            assert p >= prev - 1e-9
            prev = p

    def test_max_fusion_semantics(self, load):
        """Heat sources add: the strongest driver wins, and a light load never
        relaxes what a torrid day demands (nor vice versa)."""
        outdoor = build_setpoint_controller(72, 92)
        torrid_idle = max(
            outdoor.evaluate({"outdoor": 95}).value,
            load.evaluate({"load": 100}).value,
        )
        assert torrid_idle == pytest.approx(1.0)  # weather unchallenged
        mild_compute = max(
            outdoor.evaluate({"outdoor": 65}).value,
            load.evaluate({"load": 200}).value,
        )
        assert mild_compute == pytest.approx(2 / 3, abs=1e-6)  # load carries it


class TestHumidityController:
    def test_caps_at_one_third(self):
        from custom_components.fuzzy_thermostat.fuzzy.hvac import (
            build_humidity_controller,
        )
        h = build_humidity_controller()  # defaults 45/75 %RH
        assert h.evaluate({"humidity": 30}).value == pytest.approx(0.0)
        assert h.evaluate({"humidity": 45}).value == pytest.approx(0.0)
        assert h.evaluate({"humidity": 75}).value == pytest.approx(1 / 3, abs=1e-6)
        assert h.evaluate({"humidity": 95}).value == pytest.approx(1 / 3, abs=1e-6)

    def test_monotone_and_bounded(self):
        from custom_components.fuzzy_thermostat.fuzzy.hvac import (
            build_humidity_controller,
        )
        h = build_humidity_controller()
        prev = -1.0
        for rh in range(20, 101):
            p = h.evaluate({"humidity": rh}).value
            assert -1e-9 <= p <= 1 / 3 + 1e-9
            assert p >= prev - 1e-9
            prev = p
