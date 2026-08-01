"""Tests for setpoint composition.

Every test here is a regression against a bug that reached a running house.
Both had the same shape: a subordinate signal clamped by a bound it exists to
escape.
"""
import pytest

from custom_components.fuzzy_thermostat.fuzzy.targeting import (
    clamp_to_device,
    compose_target,
    outdoor_drive,
)


# -- outdoor_drive ------------------------------------------------------


def test_drive_follows_the_live_reading_by_default():
    """Default weight ignores the forecast entirely."""
    assert outdoor_drive(69.0, 89.0) == 69.0


def test_drive_does_not_anticipate_a_peak_that_has_not_arrived():
    """REGRESSION: max(now, forecast) pinned the target to the band floor.

    On a 69F morning under an 89F forecast the drive used to read 89, which
    put the outdoor position at 0.85 and the base target at the aggressive
    bound -- through the coolest hours of the day, when it is least justified.
    """
    morning = outdoor_drive(69.0, 89.0, weight=0.0)
    assert morning == 69.0
    assert morning != max(69.0, 89.0)


def test_weight_one_reproduces_the_old_max_behaviour():
    """Existing installs can opt back in to full anticipation."""
    assert outdoor_drive(69.0, 89.0, weight=1.0) == max(69.0, 89.0)


@pytest.mark.parametrize(
    "weight,expected",
    [(0.0, 69.0), (0.25, 74.0), (0.5, 79.0), (1.0, 89.0)],
)
def test_weight_folds_in_a_fraction_of_the_climb(weight, expected):
    assert outdoor_drive(69.0, 89.0, weight=weight) == pytest.approx(expected)


def test_a_forecast_below_the_current_reading_never_drags_the_drive_down():
    """The day has already proven the forecast wrong; do not chase it."""
    assert outdoor_drive(85.0, 70.0, weight=1.0) == 85.0


def test_drive_falls_back_to_forecast_when_there_is_no_live_reading():
    assert outdoor_drive(None, 88.0, weight=0.0) == 88.0


def test_drive_is_none_only_when_both_readings_are_missing():
    assert outdoor_drive(None, None) is None


# -- compose_target -----------------------------------------------------


def test_rules_output_is_clamped_to_the_comfort_band():
    """The band IS a structural bound on what the rules may ask for."""
    assert compose_target(95.0, comfort_min=69, comfort_max=71) == 71
    assert compose_target(40.0, comfort_min=69, comfort_max=71) == 69


def test_bias_escapes_the_comfort_band():
    """REGRESSION: the bias used to be clamped back into the band.

    Applied before the clamp it saturated at the edge and did nothing -- with
    a 69-71 band the occupant could never reach 73 however many times they
    said they felt cold. The band may veto the rules; it may not veto the
    person.
    """
    assert compose_target(71.0, comfort_min=69, comfort_max=71, bias=2.0) == 73.0


def test_bias_escapes_downward_too():
    assert compose_target(69.0, comfort_min=69, comfort_max=71, bias=-2.0) == 67.0


def test_bias_applies_on_top_of_the_clamped_value_not_the_raw_rules_output():
    """A wild rules output must not smuggle itself past the band via the bias."""
    assert compose_target(200.0, comfort_min=69, comfort_max=71, bias=1.0) == 72.0


def test_bias_is_capped_at_the_limit():
    """A runaway helper cannot command something absurd."""
    assert compose_target(71.0, comfort_min=69, comfort_max=71, bias=99.0) == 73.0
    assert compose_target(69.0, comfort_min=69, comfort_max=71, bias=-99.0) == 67.0


def test_device_bounds_are_the_only_limit_on_the_biased_target():
    """REGRESSION: the device clamp only ran when tracking was enabled.

    That left the ordinary single-room case with no device bound at all --
    harmless while the band was the effective ceiling, load-bearing once the
    bias could exceed it.
    """
    biased = compose_target(71.0, comfort_min=69, comfort_max=71, bias=2.0)
    assert biased == 73.0
    assert clamp_to_device(biased, device_max=72.0) == 72.0

    biased_down = compose_target(69.0, comfort_min=69, comfort_max=71, bias=-2.0)
    assert biased_down == 67.0
    assert clamp_to_device(biased_down, device_min=68.0) == 68.0


def test_device_bounds_are_optional():
    assert clamp_to_device(72.0) == 72.0
    assert clamp_to_device(72.0, device_min=61.0) == 72.0
    assert clamp_to_device(72.0, device_max=86.0) == 72.0


def test_device_clamp_does_not_move_a_value_already_in_range():
    assert clamp_to_device(73.0, device_min=61.0, device_max=86.0) == 73.0


def test_zero_bias_leaves_the_clamped_target_untouched():
    assert compose_target(70.3, comfort_min=69, comfort_max=71) == pytest.approx(70.3)


def test_the_office_morning_case_end_to_end():
    """The exact scenario that surfaced both bugs, composed.

    69F outside, 89F forecast high, 69-73 band, occupant has said nothing.
    A mild morning must land at the relaxed end of the band on its own.
    """
    drive = outdoor_drive(69.0, 89.0)          # -> 69, mild
    assert drive == 69.0
    p = 0.0                                     # mild => relaxed end
    rules_target = 73 - p * (73 - 69)
    assert compose_target(rules_target, comfort_min=69, comfort_max=73) == 73.0
