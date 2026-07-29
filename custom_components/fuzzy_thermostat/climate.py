"""Fuzzy-logic thermostat entity.

A thermostat whose decisions come from a Mamdani fuzzy inference engine
(see ``fuzzy/``) instead of a fixed hysteresis band. Two ways to wire it:

Switch mode (``heater:`` or ``cooler:``)
    The direct ``generic_thermostat`` analogue: the entity drives a switch,
    but on/off decisions come from a signed fuzzy *demand* with a principled
    deadband, trend-aware anti-overshoot damping, and a minimum cycle time.

Supervisor mode (``climate_entity:``)
    For equipment that already runs its own compressor logic (mini-splits,
    smart heat pumps). The fuzzy layer never fights the device's internals —
    it decides the *setpoint* (optionally compensated by outdoor conditions
    within hard comfort bounds) and, if ``manage_power`` is on, when the unit
    should run at all. Setpoint changes are rate-limited so the device is not
    chattered.

Design rules carried throughout:

* Bounded authority — the computed target can never leave
  ``[comfort_min, comfort_max]``; the bound is structural, not aspirational.
* Fail safe — an unavailable sensor idles the controller and says so in the
  ``control_reason`` attribute. It never guesses.
* Observable — the demand, the computed target, and the rules that fired are
  entity attributes, so "why did it do that?" has an answer in the UI.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    DOMAIN as CLIMATE_DOMAIN,
    PLATFORM_SCHEMA,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_NAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .const import (
    ATTR_ACTIVATION_MARGIN,
    ATTR_ACTIVE_RULES,
    ATTR_CONTROL_REASON,
    ATTR_FUZZY_DEMAND,
    ATTR_FUZZY_TARGET,
    ATTR_OUTDOOR_DRIVE,
    ATTR_TREND,
    CONF_CLIMATE_ENTITY,
    CONF_COMFORT_MAX,
    CONF_COMFORT_MIN,
    CONF_COOLER,
    CONF_DEMAND_OFF,
    CONF_DEMAND_ON,
    CONF_DIRECTION,
    CONF_FORECAST_HIGH_SENSOR,
    CONF_HEATER,
    CONF_MANAGE_POWER,
    CONF_MARGIN_NARROW,
    CONF_MARGIN_WIDE,
    CONF_MAX_SLEW,
    CONF_MAX_TEMP,
    CONF_MIN_CYCLE_DURATION,
    CONF_MIN_TEMP,
    CONF_OUTDOOR_MILD,
    CONF_OUTDOOR_SENSOR,
    CONF_OUTDOOR_TORRID,
    CONF_SAMPLE_INTERVAL,
    CONF_TARGET_SENSOR,
    CONF_TREND_WINDOW,
    DEFAULT_DEMAND_OFF,
    DEFAULT_DEMAND_ON,
    DEFAULT_MIN_CYCLE_S,
    DEFAULT_SAMPLE_INTERVAL_S,
    DEFAULT_TREND_WINDOW_S,
    DEFAULTS_BY_UNIT,
    DIRECTION_COOL,
    DIRECTION_HEAT,
)
from .fuzzy import build_command_controller, build_setpoint_controller

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_TARGET_SENSOR): cv.entity_id,
        vol.Required(CONF_COMFORT_MIN): vol.Coerce(float),
        vol.Required(CONF_COMFORT_MAX): vol.Coerce(float),
        # Exactly one actuator:
        vol.Exclusive(CONF_HEATER, "actuator"): cv.entity_id,
        vol.Exclusive(CONF_COOLER, "actuator"): cv.entity_id,
        vol.Exclusive(CONF_CLIMATE_ENTITY, "actuator"): cv.entity_id,
        vol.Optional(CONF_DIRECTION, default=DIRECTION_COOL): vol.In(
            [DIRECTION_COOL, DIRECTION_HEAT]
        ),
        vol.Optional(CONF_OUTDOOR_SENSOR): cv.entity_id,
        vol.Optional(CONF_FORECAST_HIGH_SENSOR): cv.entity_id,
        vol.Optional(CONF_OUTDOOR_MILD): vol.Coerce(float),
        vol.Optional(CONF_OUTDOOR_TORRID): vol.Coerce(float),
        vol.Optional(CONF_MARGIN_WIDE): vol.Coerce(float),
        vol.Optional(CONF_MARGIN_NARROW): vol.Coerce(float),
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_SLEW): vol.Coerce(float),
        vol.Optional(
            CONF_SAMPLE_INTERVAL,
            default=timedelta(seconds=DEFAULT_SAMPLE_INTERVAL_S),
        ): cv.positive_time_period,
        vol.Optional(CONF_DEMAND_ON, default=DEFAULT_DEMAND_ON): vol.Coerce(float),
        vol.Optional(CONF_DEMAND_OFF, default=DEFAULT_DEMAND_OFF): vol.Coerce(float),
        vol.Optional(
            CONF_MIN_CYCLE_DURATION,
            default=timedelta(seconds=DEFAULT_MIN_CYCLE_S),
        ): cv.positive_time_period,
        vol.Optional(
            CONF_TREND_WINDOW, default=timedelta(seconds=DEFAULT_TREND_WINDOW_S)
        ): cv.positive_time_period,
        vol.Optional(CONF_MANAGE_POWER, default=True): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the fuzzy thermostat from YAML."""
    if not any(config.get(k) for k in (CONF_HEATER, CONF_COOLER, CONF_CLIMATE_ENTITY)):
        raise vol.Invalid(
            "one of 'heater', 'cooler' or 'climate_entity' is required"
        )
    if config[CONF_COMFORT_MIN] >= config[CONF_COMFORT_MAX]:
        raise vol.Invalid("comfort_min must be below comfort_max")
    async_add_entities([FuzzyThermostat(hass, config)])


class FuzzyThermostat(ClimateEntity, RestoreEntity):
    """Climate entity governed by the fuzzy controllers."""

    _attr_should_poll = False
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        self.hass = hass
        us = hass.config.units is US_CUSTOMARY_SYSTEM

        def unit_default(key: str) -> float:
            f_val, c_val = DEFAULTS_BY_UNIT[key]
            return f_val if us else c_val

        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = f"fuzzy_thermostat_{config[CONF_NAME]}"
        self._attr_temperature_unit = hass.config.units.temperature_unit

        self._sensor = config[CONF_TARGET_SENSOR]
        self._heater = config.get(CONF_HEATER)
        self._cooler = config.get(CONF_COOLER)
        self._wrapped = config.get(CONF_CLIMATE_ENTITY)
        self._outdoor = config.get(CONF_OUTDOOR_SENSOR)
        self._forecast_high = config.get(CONF_FORECAST_HIGH_SENSOR)
        self._manage_power = config[CONF_MANAGE_POWER]

        # Direction: a heater switch always heats, a cooler always cools; the
        # wrapped-climate form takes it from `direction`.
        if self._heater:
            self._direction = DIRECTION_HEAT
        elif self._cooler:
            self._direction = DIRECTION_COOL
        else:
            self._direction = config[CONF_DIRECTION]

        self._comfort_min = config[CONF_COMFORT_MIN]
        self._comfort_max = config[CONF_COMFORT_MAX]
        self._margin_wide = config.get(CONF_MARGIN_WIDE, unit_default("margin_wide"))
        self._margin_narrow = config.get(
            CONF_MARGIN_NARROW, unit_default("margin_narrow")
        )
        self._max_slew = config.get(CONF_MAX_SLEW, unit_default("max_slew"))
        self._demand_on = config[CONF_DEMAND_ON]
        self._demand_off = config[CONF_DEMAND_OFF]
        self._sample_interval: timedelta = config[CONF_SAMPLE_INTERVAL]
        self._min_cycle: timedelta = config[CONF_MIN_CYCLE_DURATION]
        self._trend_window: timedelta = config[CONF_TREND_WINDOW]

        t_min = config.get(CONF_MIN_TEMP, unit_default("min_temp"))
        t_max = config.get(CONF_MAX_TEMP, unit_default("max_temp"))
        self._attr_min_temp = self._comfort_min
        self._attr_max_temp = self._comfort_max

        # Trend universe: |3 F/h| (|1.7 C/h|) counts as clearly moving.
        self._trend_limit = 3.0 if us else 1.7
        self._command = build_command_controller(
            t_min, t_max, trend_limit=self._trend_limit
        )
        self._setpoint = build_setpoint_controller(
            config.get(CONF_OUTDOOR_MILD, unit_default("outdoor_mild")),
            config.get(CONF_OUTDOOR_TORRID, unit_default("outdoor_torrid")),
        )

        mode = HVACMode.HEAT if self._direction == DIRECTION_HEAT else HVACMode.COOL
        self._attr_hvac_modes = [HVACMode.OFF, mode]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = (self._comfort_min + self._comfort_max) / 2

        self._samples: deque[tuple[datetime, float]] = deque(maxlen=64)
        self._actuator_on = False
        self._last_switch: datetime | None = None
        self._last_sent_setpoint: float | None = None
        self._effective_target: float | None = None
        self._extra: dict[str, Any] = {ATTR_CONTROL_REASON: "not yet evaluated"}
        self._unsub: list[Any] = []

    # -- lifecycle ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state in (m.value for m in self._attr_hvac_modes):
                self._attr_hvac_mode = HVACMode(last.state)
            if (anchor := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._attr_target_temperature = self._clamp_comfort(float(anchor))

        self._unsub.append(
            async_track_state_change_event(
                self.hass, [self._sensor], self._sensor_changed
            )
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_sample, self._sample_interval
            )
        )
        # First evaluation without waiting a whole interval.
        await self._async_control()

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # -- HA-facing state ---------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        return self._read_float(self._sensor)

    @property
    def hvac_action(self) -> HVACAction:
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._actuator_on:
            return (
                HVACAction.HEATING
                if self._direction == DIRECTION_HEAT
                else HVACAction.COOLING
            )
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._extra)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = self._clamp_comfort(float(temp))
            await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            # force: an explicit OFF must not be swallowed by the minimum-cycle
            # guard — nothing would ever retry it, and the equipment would keep
            # running while this entity reported off.
            await self._async_actuate(False, reason="turned off", force=True)
        await self._async_control()

    # -- sampling ----------------------------------------------------------

    @callback
    def _sensor_changed(self, _event: Any) -> None:
        self.async_write_ha_state()

    async def _async_sample(self, _now: datetime) -> None:
        await self._async_control()

    def _read_float(self, entity_id: str | None) -> float | None:
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _clamp_comfort(self, value: float) -> float:
        return min(self._comfort_max, max(self._comfort_min, value))

    def _trend(self, now: datetime) -> float:
        """Temperature slope over the configured window, in degrees/hour."""
        cutoff = now - self._trend_window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0
        (t0, v0), (t1, v1) = self._samples[0], self._samples[-1]
        hours = (t1 - t0).total_seconds() / 3600.0
        if hours < 0.05:  # need a few minutes of history to trust a slope
            return 0.0
        return (v1 - v0) / hours

    # -- the control loop --------------------------------------------------

    async def _async_control(self) -> None:
        now = dt_util.utcnow()

        # Supervisor mode: adopt the wrapped device's real state every cycle.
        # Restarts, manual remote presses and other automations all change the
        # device behind our back; believing a stale _actuator_on means an off
        # command no-ops ("already off") while the compressor keeps running.
        if self._wrapped:
            wstate = self.hass.states.get(self._wrapped)
            if wstate is not None and wstate.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._actuator_on = wstate.state != HVACMode.OFF

        room = self._read_float(self._sensor)
        if room is None:
            # Fail safe: no reading, no action. An unavailable sensor must
            # never be mistaken for a comfortable room.
            self._extra[ATTR_CONTROL_REASON] = (
                f"idle: {self._sensor} is unavailable"
            )
            self.async_write_ha_state()
            return
        self._samples.append((now, room))
        trend = self._trend(now)

        # -- WHAT to aim for: outdoor-compensated target within hard bounds --
        outdoor_drive: float | None = None
        if self._outdoor and self._direction == DIRECTION_COOL:
            readings = [
                r
                for r in (
                    self._read_float(self._outdoor),
                    self._read_float(self._forecast_high),
                )
                if r is not None
            ]
            if readings:
                # Anticipate the afternoon instead of chasing it.
                outdoor_drive = max(readings)

        if outdoor_drive is not None:
            p = self._setpoint.evaluate({"outdoor": outdoor_drive}).value or 0.0
            fuzzy_target = self._comfort_max - p * (
                self._comfort_max - self._comfort_min
            )
            margin = self._margin_wide - p * (self._margin_wide - self._margin_narrow)
        else:
            fuzzy_target = self._clamp_comfort(self._attr_target_temperature)
            margin = self._margin_wide

        fuzzy_target = self._clamp_comfort(fuzzy_target)  # structural, twice over

        # Rate-limit how fast the effective target may move (no chattering).
        if self._effective_target is None:
            self._effective_target = fuzzy_target
        else:
            step = max(-self._max_slew, min(self._max_slew, fuzzy_target - self._effective_target))
            self._effective_target += step

        # -- WHEN to run: signed demand from the room/target/trend rules ----
        result = self._command.evaluate(
            {"room": room, "target": self._effective_target, "trend": trend}
        )
        demand = result.value if result.value is not None else 0.0

        self._extra = {
            ATTR_FUZZY_DEMAND: round(demand, 3),
            ATTR_FUZZY_TARGET: round(self._effective_target, 2),
            ATTR_ACTIVATION_MARGIN: round(margin, 2),
            ATTR_TREND: round(trend, 2),
            ATTR_OUTDOOR_DRIVE: outdoor_drive,
            ATTR_ACTIVE_RULES: [
                f"{text} ({strength:.2f})" for text, strength in result.top_rules()
            ],
            ATTR_CONTROL_REASON: "evaluated",
        }

        if self._attr_hvac_mode == HVACMode.OFF:
            self._extra[ATTR_CONTROL_REASON] = "idle: thermostat is off"
            self.async_write_ha_state()
            return

        # START gate: the margin IS the policy — "no more than `margin` past
        # target" — plus a trend guard so we do not start into a room already
        # falling fast (post-cooling coast). Fuzzy demand is deliberately NOT a
        # start gate: with explicit hold rules the aggregate is hold-dominated
        # near target, so demand sits at ~0 even well past the margin. The
        # first live deployment idled at room 73 / target 71 because demand
        # (-0.006) was allowed to out-vote a margin the room had already
        # crossed by a full degree.
        #
        # STOP gate: that near-target flatness is exactly what makes demand the
        # right OFF signal — advocacy gone means the room is back at target.
        settle = self._trend_limit / 3.0  # "clearly moving the right way"
        past_margin = (
            room - self._effective_target >= margin
            if self._direction == DIRECTION_COOL
            else self._effective_target - room >= margin
        )
        if self._direction == DIRECTION_HEAT:
            want_on = past_margin and trend < settle
            want_off = demand <= self._demand_off
        else:
            want_on = past_margin and trend > -settle
            want_off = demand >= -self._demand_off

        if not self._actuator_on and want_on:
            await self._async_actuate(True, reason=f"demand {demand:+.2f}")
        elif self._actuator_on and want_off:
            await self._async_actuate(False, reason=f"demand {demand:+.2f}")

        if self._wrapped and self._actuator_on:
            await self._async_send_setpoint()
        self.async_write_ha_state()

    # -- actuation ---------------------------------------------------------

    def _cycle_blocked(self, now: datetime) -> bool:
        return (
            self._last_switch is not None
            and now - self._last_switch < self._min_cycle
        )

    async def _async_actuate(self, on: bool, *, reason: str, force: bool = False) -> None:
        now = dt_util.utcnow()
        if on == self._actuator_on:
            return
        if not force and self._cycle_blocked(now):
            self._extra[ATTR_CONTROL_REASON] = (
                f"held: minimum cycle time ({reason})"
            )
            return

        if self._wrapped:
            if not self._manage_power:
                self._actuator_on = on
                self._last_switch = now
                return
            service = SERVICE_SET_HVAC_MODE
            mode = (
                (HVACMode.HEAT if self._direction == DIRECTION_HEAT else HVACMode.COOL)
                if on
                else HVACMode.OFF
            )
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                service,
                {ATTR_ENTITY_ID: self._wrapped, "hvac_mode": mode},
                blocking=True,
            )
            if on:
                self._last_sent_setpoint = None  # re-send after power-on
        else:
            switch = self._heater or self._cooler
            await self.hass.services.async_call(
                "homeassistant",
                SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: switch},
                blocking=True,
            )
        self._actuator_on = on
        self._last_switch = now
        self._extra[ATTR_CONTROL_REASON] = (
            f"{'started' if on else 'stopped'}: {reason}"
        )

    async def _async_send_setpoint(self) -> None:
        """Supervisor mode: govern the wrapped device's setpoint, gently."""
        target = round(self._effective_target * 2) / 2  # most units take halves
        if (
            self._last_sent_setpoint is not None
            and abs(target - self._last_sent_setpoint) < 0.3
        ):
            return
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self._wrapped, ATTR_TEMPERATURE: target},
            blocking=True,
        )
        self._last_sent_setpoint = target
