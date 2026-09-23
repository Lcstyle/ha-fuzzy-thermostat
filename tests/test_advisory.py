"""Advisory mode: the setpoint is decided exactly as the write path decides it, then published
instead of sent. Needs Home Assistant importable (runs in the HA container, skips elsewhere)."""
import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.fuzzy_thermostat import climate as C  # noqa: E402
from custom_components.fuzzy_thermostat.fuzzy.actuation import Ledger  # noqa: E402


class _State:
    def __init__(self, state, attrs):
        self.state, self.attributes = state, attrs


class _Hass:
    def __init__(self, wstate):
        self.calls = []
        self.states = type("States", (), {"get": lambda _self, _e: wstate})()
        outer = self

        class _Services:
            async def async_call(self, *args, **kwargs):
                outer.calls.append(args)

        self.services = _Services()


def _entity(advisory, *, held=60, target=64.2, gain=0.0, room=None, manage_power=False):
    e = object.__new__(C.FuzzyThermostat)
    e.hass = _Hass(_State("heat", {"temperature": held, "target_temp_step": 1, "min_temp": 40, "max_temp": 90}))
    e.entity_id = "climate.test"
    e._wrapped = "climate.oil"
    e._effective_target = target
    e._extra = {}
    e._tracking_gain = gain
    e._tracking_max = 3.0
    e._samples = [(0, room)] if room is not None else []
    e._advisory = advisory
    e._manage_power = manage_power
    e._ledger = Ledger()
    e._confirm_after = timedelta(seconds=120)
    e._max_send_attempts = 3
    e._direction = C.DIRECTION_HEAT
    e._actuator_on = False
    e._last_switch = None
    e._min_cycle = timedelta(0)
    return e


def test_advisory_publishes_and_sends_nothing():
    e = _entity(True)
    assert asyncio.run(e._async_send_setpoint()) is False
    assert e._extra[C.ATTR_ADVISED_SETPOINT] == 64
    assert e.hass.calls == []


def test_non_advisory_still_sends():
    e = _entity(False)
    assert asyncio.run(e._async_send_setpoint()) is True
    assert len(e.hass.calls) == 1
    assert C.ATTR_ADVISED_SETPOINT not in e._extra


def test_advisory_applies_the_tracking_trim():
    e = _entity(True, target=64, gain=1.0, room=66)
    asyncio.run(e._async_send_setpoint())
    assert e._extra[C.ATTR_ADVISED_SETPOINT] == 62


def test_advisory_keeps_the_schmitt_hold():
    e = _entity(True, held=64, target=64.4)
    asyncio.run(e._async_send_setpoint())
    assert e._extra[C.ATTR_ADVISED_SETPOINT] == 64


def test_advisory_never_switches_power():
    e = _entity(True, manage_power=True)
    asyncio.run(e._async_actuate(True, reason="t"))
    assert e.hass.calls == []
    assert e._actuator_on is True
