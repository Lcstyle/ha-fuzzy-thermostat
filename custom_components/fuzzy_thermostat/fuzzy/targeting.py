"""How the commanded setpoint is composed, as pure arithmetic.

The fuzzy controllers in :mod:`.hvac` decide *what the weather and the room
imply*. This module decides *what number is finally sent to the device* once
the occupant and the hardware have had their say. It is deliberately free of
any Home Assistant import so that the ordering rules below — which is to say,
the part that has actually been wrong twice — can be tested directly.

Three rules govern the composition. The first two are about **which bound gets
to veto which signal**; the third is about **which bound is even in force**:

``outdoor_drive``
    The weather that the setpoint should answer to. This was once
    ``max(now, forecast_high)``, described as anticipating the afternoon, but
    a max against the day's peak never relaxes: from midnight onward the
    controller reasons as though it were already the hottest moment of the
    day, holding the target at its aggressive bound straight through a cool
    morning. That is not anticipation, it is a permanently pessimistic
    constant. The live reading leads; ``weight`` folds back a *fraction* of
    the expected climb for callers who genuinely want to pre-cool, and
    ``weight=1.0`` reproduces the old behaviour exactly.

``compose_target``
    The comfort band is a **structural** bound on what the fuzzy rules may
    ask for, so it clamps the rules' output. The occupant's bias is applied
    *after* that clamp, because the entire premise of the bias is that the
    band is wrong for this person right now — clamping it back into the band
    made it saturate at the edge and do nothing. What bounds the bias instead
    is the device's own range, since that is the only limit that is a fact
    rather than a preference. The same reasoning already governs the
    remote-room tracking trim.

``seasonal_band``
    Which band the other two are talking about. The occupant bias says *this
    band is wrong for me right now*; the seasonal dial says *this band is wrong
    for this time of year*, and so it moves the bounds themselves, before
    anything has been clamped to them. Everything downstream — the position
    mapping, the clamp, the reported attribute — reads the shifted pair.
"""

from __future__ import annotations

__all__ = [
    "outdoor_drive",
    "sum_biases",
    "target_from_position",
    "seasonal_band",
    "compose_target",
    "clamp_to_device",
]


def sum_biases(values, limit: float) -> float:
    """Sum several independent, individually-capped requests into one bounded total.

    Used for two DIFFERENT authorities -- the occupant biases that deviate from the
    comfort band, and the seasonal dial that moves it -- which is why ``limit`` is
    required rather than defaulted. It carried a default of 2.0, inherited from the
    days when the bias cap was a module constant; a shared helper silently applying
    one authority's cap to another's is exactly the kind of quiet wrong answer this
    project keeps finding.

    Each contribution is capped at ``+-limit`` and so is the total, so adding
    helpers can never widen the authority the band has already delegated. They
    SUM rather than override because each is a real request from a different
    quarter — the occupant saying they feel cold, an interlock easing this zone
    off while a larger system runs — and one must not silently mask another.

    ``None`` entries (an unavailable helper) contribute nothing.
    """
    total = 0.0
    for value in values:
        if value is None:
            continue
        total += max(-limit, min(limit, value))
    return max(-limit, min(limit, total))


def outdoor_drive(
    now: float | None,
    forecast_high: float | None,
    weight: float = 0.0,
) -> float | None:
    """Return the outdoor temperature the setpoint should answer to.

    ``weight`` in [0, 1] is how much of the climb from ``now`` up to
    ``forecast_high`` to fold in: 0 follows the weather that exists, 1
    anticipates the full peak. A forecast *below* the current reading never
    drags the drive down — the day has already proven the forecast wrong.

    Returns ``None`` only when neither reading is available.
    """
    if now is None:
        # No live reading at all: a forecast is better than nothing.
        return forecast_high
    if forecast_high is None or weight <= 0.0:
        return now
    climb = max(0.0, forecast_high - now)
    return now + weight * climb


def compose_target(
    rules_target: float,
    *,
    comfort_min: float,
    comfort_max: float,
    bias: float = 0.0,
    bias_limit: float,
) -> float:
    """Compose the target from the rules' output and the occupant's bias.

    Order is load-bearing: clamp the rules to the comfort band, *then* apply
    the bias. Applying the bias first lets the band veto the occupant, which
    is precisely backwards — with a 69-71 band the target could never reach
    73 no matter how many times someone said they felt cold.

    ``bias`` is clamped to +-``bias_limit`` so a runaway helper cannot command
    something absurd. What stops it commanding something *impossible* is
    :func:`clamp_to_device`, applied at the point of sending.
    """
    target = min(comfort_max, max(comfort_min, rules_target))
    if bias:
        target += max(-bias_limit, min(bias_limit, bias))
    return target


def clamp_to_device(
    value: float,
    device_min: float | None = None,
    device_max: float | None = None,
) -> float:
    """Bound a setpoint by the wrapped device's own supported range.

    This is the *only* limit that is a fact rather than a preference, and so
    the only one allowed to override the occupant bias and the remote-room
    tracking trim — both of which exist precisely to push a setpoint past the
    comfort band. Applied unconditionally at send time: it once ran only when
    tracking was enabled, which left the ordinary single-room case with no
    device bound at all.
    """
    if device_min is not None:
        value = max(device_min, value)
    if device_max is not None:
        value = min(device_max, value)
    return value


def target_from_position(
    position: float,
    comfort_min: float,
    comfort_max: float,
    direction: str = "cool",
) -> float:
    """Map an aggressiveness position in [0, 1] onto a commanded target.

    ``position`` is deliberately direction-AGNOSTIC: every channel answers the
    same question, "how hard should this thing be working", where 0 is fully
    relaxed and 1 is flat out. Which *temperature* that corresponds to is the
    direction's business, and the two are mirror images about the band:

    ====== ================= =================
    p      cool              heat
    ====== ================= =================
    0      comfort_max       comfort_min
    1      comfort_min       comfort_max
    ====== ================= =================

    This function exists because getting it wrong is not a small error. Every
    channel was gated to cooling until 2026-09-08, so a heating instance had no
    weather compensation at all. The obvious repair -- delete the gate -- would
    have been WORSE than the gap it closed: the old expression
    ``comfort_max - p * span`` is cooling's mapping, and on the coldest night of
    the year a heating instance running it would compute p near 1 and aim at
    ``comfort_min``. Maximum demand, minimum setpoint.
    """
    span = comfort_max - comfort_min
    if direction == "heat":
        return comfort_min + position * span
    return comfort_max - position * span


def seasonal_band(
    comfort_min: float,
    comfort_max: float,
    shift: float | None,
    limit: float,
) -> tuple[float, float]:
    """Slide the whole comfort band by a seasonal offset, preserving its width.

    This is NOT the occupant bias, and the difference is the reason it is a
    separate concept rather than another feedback helper. The bias says *the
    band is wrong for me right now* and is therefore applied AFTER the band has
    clamped the rules, so it can push past the edge. The seasonal dial says
    *the band itself is wrong for this time of year* -- the ASHRAE adaptive
    observation that people acclimatised to July are comfortable several degrees
    warmer than the same people in January. So it moves the bounds, before
    anything is clamped to them, and every channel downstream inherits it.

    Chosen over an automatic running-mean term ON PURPOSE: a term that silently
    walks the comfort band through the year is another rule doing the wrong
    thing invisibly, and at the moment it is wrong there is nothing to grab.
    A dial is a thing a person can see and turn.

    ``None`` (an unavailable helper) means no shift, never a guess.
    """
    if not shift:
        return comfort_min, comfort_max
    shift = max(-limit, min(limit, shift))
    return comfort_min + shift, comfort_max + shift
