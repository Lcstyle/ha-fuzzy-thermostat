"""HVAC-specific fuzzy controllers built on the generic engine.

Two small controllers, matching the two jobs a thermostat actually has:

``build_command_controller``
    WHEN to heat/cool. A direct generalisation of the heating-and-cooling
    demonstration in Norris ch. 5 (Demo 5-3): *room temperature* and *target
    temperature* are separate linguistic variables (cold / comfortable / hot)
    and the rule base is the 3x3 command matrix from the book's Table 5-3, of
    which six cells demand action. One improvement over the demo: the output
    universe is a signed demand in [-1, +1] (negative = cool, positive = heat)
    instead of reusing the temperature scale, so "no change" is a principled
    deadband around zero rather than an empirically discovered band of output
    temperatures. Two low-weight trend rules damp the demand when the room is
    already moving in the right direction, which is the classic fuzzy
    anti-overshoot trick.

``build_setpoint_controller``
    WHAT temperature to aim for. Outdoor conditions are mapped to a position
    ``p`` in [0, 1]; the caller converts that to a setpoint by interpolating
    between its configured comfort bounds (p=0 -> most relaxed bound, p=1 ->
    most aggressive). Uses the weighted-average defuzzifier so the position
    can genuinely reach 0 and 1 — a clipped-centroid output never touches the
    ends of its universe, which would quietly shrink the configured comfort
    range. Bounds therefore hold structurally: the weighted average of peaks
    in [0, 1] cannot leave [0, 1].
"""
from __future__ import annotations

from .engine import FuzzyController, Rule, Variable
from .membership import Trapezoidal, Triangular

__all__ = [
    "build_command_controller",
    "build_setpoint_controller",
    "build_load_controller",
    "build_humidity_controller",
]


def build_command_controller(
    t_min: float,
    t_max: float,
    *,
    trend_limit: float = 3.0,
    resolution: int = 201,
) -> FuzzyController:
    """Room/target -> signed heat(+)/cool(-) demand.

    ``t_min``/``t_max`` bound the temperature universe (any unit — the terms
    are built from the bounds, so Celsius and Fahrenheit configurations differ
    only in the numbers supplied). ``trend_limit`` is the |slope| in degrees
    per hour treated as "clearly rising/falling".
    """
    if not t_min < t_max:
        raise ValueError("t_min must be < t_max")
    mid = (t_min + t_max) / 2.0

    def three_terms() -> dict[str, Triangular]:
        return {
            "cold": Triangular(t_min, t_min, mid),
            "comfortable": Triangular(t_min, mid, t_max),
            "hot": Triangular(mid, t_max, t_max),
        }

    room = Variable("room", (t_min, t_max), three_terms())
    target = Variable("target", (t_min, t_max), three_terms())
    trend = Variable(
        "trend",
        (-trend_limit, trend_limit),
        {
            "falling": Triangular(-trend_limit, -trend_limit, 0.0),
            "steady": Triangular(-trend_limit, 0.0, trend_limit),
            "rising": Triangular(0.0, trend_limit, trend_limit),
        },
    )
    demand = Variable(
        "demand",
        (-1.0, 1.0),
        {
            "cool": Triangular(-1.0, -1.0, 0.0),
            "hold": Triangular(-1.0, 0.0, 1.0),
            "heat": Triangular(0.0, 1.0, 1.0),
        },
    )

    rules = [
        # The six action cells of the room x target command matrix (Table 5-3).
        Rule((("room", "cold"), ("target", "comfortable")), "heat"),
        Rule((("room", "cold"), ("target", "hot")), "heat"),
        Rule((("room", "comfortable"), ("target", "cold")), "cool"),
        Rule((("room", "comfortable"), ("target", "hot")), "heat"),
        Rule((("room", "hot"), ("target", "cold")), "cool"),
        Rule((("room", "hot"), ("target", "comfortable")), "cool"),
        # The diagonal: agreement means hold. The demo dropped these rules and
        # lived with whatever the centroid did when nothing fired; keeping them
        # gives the deadband an explicit voice and means the aggregate is never
        # empty in normal operation.
        Rule((("room", "cold"), ("target", "cold")), "hold"),
        Rule((("room", "comfortable"), ("target", "comfortable")), "hold"),
        Rule((("room", "hot"), ("target", "hot")), "hold"),
        # Anti-overshoot damping: if the room is already moving in the right
        # direction, lean toward hold. Low weight so a genuinely wrong
        # temperature still wins.
        Rule((("room", "hot"), ("trend", "falling")), "hold", weight=0.7),
        Rule((("room", "cold"), ("trend", "rising")), "hold", weight=0.7),
    ]
    return FuzzyController([room, target, trend], demand, rules, resolution=resolution)


def build_setpoint_controller(
    outdoor_mild: float,
    outdoor_torrid: float,
    *,
    resolution: int = 201,
) -> FuzzyController:
    """Outdoor drive -> position p in [0, 1] between the comfort bounds.

    ``outdoor_mild`` is the outdoor temperature at or below which cooling may
    relax fully (p = 0); ``outdoor_torrid`` the temperature at or above which
    it should be at its most aggressive (p = 1). Four overlapping outdoor
    terms map one-to-one onto four evenly spaced output singpoints, and the
    weighted-average defuzzifier interpolates smoothly between them.
    """
    if not outdoor_mild < outdoor_torrid:
        raise ValueError("outdoor_mild must be < outdoor_torrid")
    span = outdoor_torrid - outdoor_mild
    q0 = outdoor_mild
    q1 = outdoor_mild + span / 3.0
    q2 = outdoor_mild + 2.0 * span / 3.0
    q3 = outdoor_torrid
    pad = span * 0.25
    lo, hi = q0 - pad, q3 + pad

    outdoor = Variable(
        "outdoor",
        (lo, hi),
        {
            "mild": Trapezoidal(lo, lo, q0, q1),
            "warm": Triangular(q0, q1, q2),
            "hot": Triangular(q1, q2, q3),
            "torrid": Trapezoidal(q2, q3, hi, hi),
        },
    )
    position = Variable(
        "position",
        (0.0, 1.0),
        {
            "relaxed": Triangular(0.0, 0.0, 1.0 / 3.0),
            "easy": Triangular(0.0, 1.0 / 3.0, 2.0 / 3.0),
            "firm": Triangular(1.0 / 3.0, 2.0 / 3.0, 1.0),
            "aggressive": Triangular(2.0 / 3.0, 1.0, 1.0),
        },
    )
    rules = [
        Rule((("outdoor", "mild"),), "relaxed"),
        Rule((("outdoor", "warm"),), "easy"),
        Rule((("outdoor", "hot"),), "firm"),
        Rule((("outdoor", "torrid"),), "aggressive"),
    ]
    return FuzzyController(
        [outdoor], position, rules, resolution=resolution, defuzz="weighted_average"
    )


def build_load_controller(
    load_light: float,
    load_heavy: float,
    *,
    resolution: int = 201,
) -> FuzzyController:
    """Internal-load proxy -> position p in [0, 1], like the outdoor controller.

    Rooms whose dominant heat source is INSIDE — equipment closets, home
    offices full of computers, server corners — break the outdoor-compensation
    assumption: "mild outside" does not mean "low cooling need" when several
    hundred watts dissipate into the space regardless of the weather. This is
    the load-compensation half of that story: any numeric proxy for internal
    dissipation (a CPU package temperature, a smart-plug wattage, a rack
    sensor) maps to a position that pulls the setpoint toward its aggressive
    bound as the load grows.

    ``load_light`` is the sensor value at or below which the load contributes
    nothing; ``load_heavy`` the value at or above which it demands the most.
    Units are whatever the sensor reports — the breakpoints carry them.

    The caller fuses this with the outdoor position as ``max(p_outdoor,
    p_load)``: heat sources add, so the setpoint is as aggressive as the
    strongest driver demands — but a light load must never *relax* what a hot
    day already requires, which is why the fusion is max and not a mean.

    Peaks are 0 / 1/3 / 2/3 rather than reaching 1.0: even a flat-out load in
    a mild week should not command the same setpoint floor as a heatwave —
    the outdoor channel keeps sole ownership of the last third of the range.
    """
    if not load_light < load_heavy:
        raise ValueError("load_light must be < load_heavy")
    span = load_heavy - load_light
    mid = load_light + span / 2.0
    pad = span * 0.25
    lo, hi = load_light - pad, load_heavy + pad

    load = Variable(
        "load",
        (lo, hi),
        {
            "light": Trapezoidal(lo, lo, load_light, mid),
            "moderate": Triangular(load_light, mid, load_heavy),
            "heavy": Trapezoidal(mid, load_heavy, hi, hi),
        },
    )
    position = Variable(
        "position",
        (0.0, 1.0),
        {
            "relaxed": Triangular(0.0, 0.0, 1.0 / 3.0),
            "easy": Triangular(0.0, 1.0 / 3.0, 2.0 / 3.0),
            "firm": Triangular(1.0 / 3.0, 2.0 / 3.0, 1.0),
        },
    )
    rules = [
        Rule((("load", "light"),), "relaxed"),
        Rule((("load", "moderate"),), "easy"),
        Rule((("load", "heavy"),), "firm"),
    ]
    return FuzzyController(
        [load], position, rules, resolution=resolution, defuzz="weighted_average"
    )


def build_humidity_controller(
    humidity_dry: float = 45.0,
    humidity_humid: float = 75.0,
    *,
    resolution: int = 201,
) -> FuzzyController:
    """Indoor relative humidity -> position p in [0, 1/3].

    Thermal comfort is a feels-like judgment, not a dry-bulb number: the same
    room temperature reads comfortable at 45% RH and clammy at 75%. This
    channel biases the setpoint downward as the air gets muggier, which both
    compensates the perception and puts the compressor to work as a
    dehumidifier — cooling lower on humid days does double duty.

    Deliberately capped at ONE THIRD of the range: humidity shifts how a
    temperature feels by roughly a degree on a typical comfort band, it is not
    a heat source. The weather channel keeps sole ownership of the top of the
    range and the load channel of the middle; all three fuse as max() in the
    entity. Indoor RH is the right input — it already integrates outdoor
    humidity, infiltration and the equipment's own drying.
    """
    if not humidity_dry < humidity_humid:
        raise ValueError("humidity_dry must be < humidity_humid")
    span = humidity_humid - humidity_dry
    mid = humidity_dry + span / 2.0
    pad = span * 0.25
    lo, hi = humidity_dry - pad, humidity_humid + pad

    humidity = Variable(
        "humidity",
        (lo, hi),
        {
            "dry": Trapezoidal(lo, lo, humidity_dry, mid),
            "pleasant": Triangular(humidity_dry, mid, humidity_humid),
            "muggy": Trapezoidal(mid, humidity_humid, hi, hi),
        },
    )
    position = Variable(
        "position",
        (0.0, 1.0),
        {
            "none": Triangular(0.0, 0.0, 1.0 / 6.0),
            "slight": Triangular(0.0, 1.0 / 6.0, 1.0 / 3.0),
            "firm": Triangular(1.0 / 6.0, 1.0 / 3.0, 1.0 / 3.0),
        },
    )
    rules = [
        Rule((("humidity", "dry"),), "none"),
        Rule((("humidity", "pleasant"),), "slight"),
        Rule((("humidity", "muggy"),), "firm"),
    ]
    return FuzzyController(
        [humidity], position, rules, resolution=resolution, defuzz="weighted_average"
    )
