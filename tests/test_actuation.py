"""The actuation ledger: a service call returning is not the device holding the value.

These pin the 2026-09-23 incident on a Kumo head. The controller sent the mode and the
setpoint as two calls; the integration still had the mode cached as `off`, dropped the
setpoint with only a log warning, and the unit came up at the heat setpoint it
remembered. The record said one thing and the device did another.
"""
from datetime import datetime, timedelta

from custom_components.fuzzy_thermostat.fuzzy.actuation import Command, Ledger, plan

T0 = datetime(2026, 9, 23, 5, 55)
WINDOW = timedelta(seconds=120)


def _plan(ledger, *, reported_mode, reported, target=68.0, want="heat", now=T0,
          may_set_mode=False, attempts=3):
    return plan(
        ledger,
        target=target,
        want_mode=want,
        reported_mode=reported_mode,
        reported_setpoint=reported,
        step=1.0,
        now=now,
        may_set_mode=may_set_mode,
        confirm_after=WINDOW,
        max_attempts=attempts,
    )


# -- power-on ------------------------------------------------------------------------

def test_power_on_of_a_unit_that_reports_no_setpoint_is_one_call_carrying_mode_and_temperature():
    # Kumo reports temperature=None while off: the setpoint must ride WITH the mode.
    cmd, led = _plan(Ledger(), reported_mode="off", reported=None, may_set_mode=True)
    assert cmd == Command(temperature=68.0, hvac_mode="heat")
    assert led.requested_setpoint == 68.0 and led.requested_mode == "heat"
    assert led.confirmed is False and led.attempts == 1


def test_power_on_of_a_unit_already_holding_the_target_sends_the_mode_alone():
    # Gree keeps reporting its setpoint while off. Carrying the temperature too would be
    # a second push -- a second beep -- for a value the unit already has.
    cmd, _ = _plan(Ledger(), reported_mode="off", reported=68.0, may_set_mode=True)
    assert cmd == Command(temperature=None, hvac_mode="heat")


def test_power_on_when_both_mode_and_setpoint_are_wrong_is_still_one_call():
    cmd, _ = _plan(Ledger(), reported_mode="off", reported=72.0, may_set_mode=True)
    assert cmd == Command(temperature=68.0, hvac_mode="heat")


# -- confirmation --------------------------------------------------------------------

def test_a_request_is_confirmed_only_by_what_the_device_reports():
    _, led = _plan(Ledger(), reported_mode="off", reported=None, may_set_mode=True)
    cmd, led = _plan(led, reported_mode="heat", reported=68.0, now=T0 + timedelta(seconds=60))
    assert cmd is None
    assert led.confirmed is True and led.fault is None and led.attempts == 0


def test_within_the_confirm_window_an_unconfirmed_request_is_left_alone():
    _, led = _plan(Ledger(), reported_mode="off", reported=None, may_set_mode=True)
    cmd, led2 = _plan(led, reported_mode="off", reported=None, now=T0 + timedelta(seconds=30))
    assert cmd is None
    assert led2 == led


def test_a_setpoint_within_half_a_device_step_counts_as_held():
    cmd, led = _plan(Ledger(), reported_mode="heat", reported=68.4)
    assert cmd is None and led.confirmed is True


# -- retry ---------------------------------------------------------------------------

def test_a_lost_power_on_is_retried_in_the_atomic_form_not_as_a_bare_temperature():
    # The retry that matters: a temperature-only retry to a unit still reporting off is
    # dropped by Kumo exactly as the original was.
    _, led = _plan(Ledger(), reported_mode="off", reported=None, may_set_mode=True)
    cmd, led = _plan(led, reported_mode="off", reported=None, now=T0 + timedelta(seconds=150))
    assert cmd == Command(temperature=68.0, hvac_mode="heat")
    assert led.attempts == 2


def test_the_0555_incident_heals_once_the_mode_is_reported():
    # Two-call power-on: the mode landed, the setpoint did not; the unit sits at the 68
    # it remembered while 60 was asked for. Once it REPORTS heat, a temperature-only
    # retry is safe -- the integration's cached mode is the reported one.
    _, led = _plan(Ledger(), target=60.0, reported_mode="off", reported=None, may_set_mode=True)
    cmd, led = _plan(led, target=60.0, reported_mode="heat", reported=68.0,
                     now=T0 + timedelta(seconds=150))
    assert cmd == Command(temperature=60.0, hvac_mode=None)
    cmd, led = _plan(led, target=60.0, reported_mode="heat", reported=60.0,
                     now=T0 + timedelta(seconds=300))
    assert cmd is None and led.confirmed is True


def test_persistent_mismatch_becomes_a_visible_fault_and_stops_commanding():
    led = Ledger()
    sent = []
    for k in range(8):
        cmd, led = _plan(led, reported_mode="off", reported=None, may_set_mode=True,
                         now=T0 + timedelta(seconds=150 * k))
        if cmd is not None:
            sent.append(cmd)
    assert len(sent) == 3            # the original and two retries, then silence
    assert led.fault is not None
    assert "heat" in led.fault and "68" in led.fault


def test_a_fault_clears_when_the_device_later_reports_the_target():
    led = Ledger(requested_setpoint=68.0, requested_mode="heat", requested_at=T0,
                 attempts=3, fault="device reports off")
    cmd, led = _plan(led, reported_mode="heat", reported=68.0, now=T0 + timedelta(hours=1))
    assert cmd is None and led.fault is None and led.confirmed is True


def test_a_new_target_after_a_fault_gets_a_fresh_request():
    led = Ledger(requested_setpoint=68.0, requested_mode=None, requested_at=T0,
                 attempts=3, fault="stuck")
    cmd, led = _plan(led, target=70.0, reported_mode="heat", reported=68.0,
                     now=T0 + timedelta(minutes=10))
    assert cmd == Command(temperature=70.0, hvac_mode=None)
    assert led.attempts == 1 and led.fault is None


# -- manual control is sovereign ------------------------------------------------------

def test_a_confirmed_setpoint_changed_at_the_remote_is_respected_until_the_target_moves():
    confirmed = Ledger(requested_setpoint=68.0, requested_at=T0, confirmed=True)
    cmd, led = _plan(confirmed, reported_mode="heat", reported=72.0,
                     now=T0 + timedelta(hours=1))
    assert cmd is None and led.fault is None
    cmd, _ = _plan(led, target=69.0, reported_mode="heat", reported=72.0,
                   now=T0 + timedelta(hours=2))
    assert cmd == Command(temperature=69.0, hvac_mode=None)


def test_the_ordinary_setpoint_path_never_changes_the_units_mode():
    # Someone switched the unit to cool (or off) at its remote. Only our own power-on
    # edge may set a mode; a setpoint update must not re-power or re-mode the unit.
    for mode in ("off", "cool"):
        cmd, led = _plan(Ledger(), reported_mode=mode, reported=None, may_set_mode=False)
        assert cmd is None and led == Ledger()


def test_a_temperature_request_is_not_retried_after_the_unit_leaves_our_mode():
    _, led = _plan(Ledger(), reported_mode="heat", reported=66.0)       # temp-only request
    cmd, _ = _plan(led, reported_mode="off", reported=None, now=T0 + timedelta(seconds=150))
    assert cmd is None


def test_steady_state_is_silent():
    led = Ledger()
    sent = 0
    for k in range(20):
        cmd, led = _plan(led, reported_mode="heat", reported=68.0,
                         now=T0 + timedelta(minutes=5 * k))
        sent += cmd is not None
    assert sent == 0
