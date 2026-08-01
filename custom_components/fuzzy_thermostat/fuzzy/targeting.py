"""How the commanded setpoint is composed, as pure arithmetic.

The fuzzy controllers in :mod:`.hvac` decide *what the weather and the room
imply*. This module decides *what number is finally sent to the device* once
the occupant and the hardware have had their say. It is deliberately free of
any Home Assistant import so that the ordering rules below — which is to say,
the part that has actually been wrong twice — can be tested directly.

Two rules govern the composition, and both are about **which bound gets to
veto which signal**:

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
"""

from __future__ import annotations

__all__ = ["outdoor_drive", "sum_biases", "compose_target", "clamp_to_device"]


def sum_biases(values, limit: float = 2.0) -> float:
    """Combine several independent reasons to deviate from the comfort band.

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
    bias_limit: float = 2.0,
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
