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
import math
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
    ATTR_HUMIDITY_POSITION,
    ATTR_INDOOR_HUMIDITY,
    ATTR_FEEDBACK_BIAS,
    ATTR_TRACKING_TRIM,
    ATTR_LOAD_POSITION,
    ATTR_LOAD_SMOOTHED,
    ATTR_HELD_SETPOINT,
    ATTR_OUTDOOR_DRIVE,
    ATTR_OUTDOOR_POSITION,
    ATTR_TREND,
    CONF_CLIMATE_ENTITY,
    CONF_COMFORT_MAX,
    CONF_COMPANION_ENTITIES,
    CONF_CONTROL_STYLE,
    CONF_COMFORT_MIN,
    CONF_COOLER,
    CONF_DEMAND_OFF,
    CONF_DEMAND_ON,
    CONF_DIRECTION,
    CONF_FORECAST_HIGH_SENSOR,
    CONF_FORECAST_WEIGHT,
    CONF_HEATER,
    CONF_HUMIDITY_DRY,
    CONF_HUMIDITY_HUMID,
    CONF_FEEDBACK_ENTITY,
    CONF_TRACKING_GAIN,
    CONF_TRACKING_MAX,
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_HEAVY,
    CONF_LOAD_LIGHT,
    CONF_LOAD_SENSOR,
    CONF_LOAD_SMOOTHING,
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
    DEFAULT_LOAD_SMOOTHING_S,
    DEFAULT_FORECAST_WEIGHT,
    DEFAULT_TREND_WINDOW_S,
    DEFAULTS_BY_UNIT,
    DIRECTION_COOL,
    DIRECTION_HEAT,
    STYLE_CYCLING,
    STYLE_SETPOINT,
)
from .fuzzy import (
    build_command_controller,
    build_humidity_controller,
    build_load_controller,
    build_setpoint_controller,
)
from .fuzzy.targeting import clamp_to_device, compose_target, outdoor_drive

_LOGGER = logging.getLogger(__name__)

# How far the occupant's feedback helper may move the target, in degrees.
# Referenced twice - once to widen the entity's advertised limits, once to
# clamp the helper itself - so the two can never disagree.
FEEDBACK_BIAS_LIMIT = 2.0

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
        vol.Optional(
            CONF_FORECAST_WEIGHT, default=DEFAULT_FORECAST_WEIGHT
        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
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
        # Supervisor control style. `setpoint` (default): the wrapped device
        # stays ON while this entity is on, and the ONLY control output is its
        # setpoint — inverter units modulate their own compressor, and even
        # fixed-speed units run their own hysteresis at whatever setpoint they
        # are handed. Power-cycling a modulating unit defeats it. `cycling`
        # restores margin/demand-gated on/off for devices that should be
        # duty-cycled. Switch mode always cycles — a relay has no setpoint.
        vol.Optional(CONF_CONTROL_STYLE, default=STYLE_SETPOINT): vol.In(
            [STYLE_SETPOINT, STYLE_CYCLING]
        ),
        # Companion actuators (circulation fans, dampers) that follow the
        # conditioning state: turned on when it becomes active, off when it
        # stops. STATEFUL entities only — an RF toggle has no readable state
        # and will drift out of phase; wrap it or leave it out. For richer
        # policies (presence gating, off-delays) trigger an automation on this
        # entity's hvac_action instead — that attribute is the loose hook.
        vol.Optional(CONF_COMPANION_ENTITIES): cv.entity_ids,
        # Load compensation: all three or none. The sensor is any numeric proxy
        # for internal dissipation (CPU package temp, plug wattage, rack temp);
        # the breakpoints carry its units.
        vol.Inclusive(CONF_LOAD_SENSOR, "load"): cv.entity_id,
        vol.Inclusive(CONF_LOAD_LIGHT, "load"): vol.Coerce(float),
        vol.Optional(
            CONF_LOAD_SMOOTHING,
            default=timedelta(seconds=DEFAULT_LOAD_SMOOTHING_S),
        ): cv.positive_time_period,
        vol.Inclusive(CONF_LOAD_HEAVY, "load"): vol.Coerce(float),
        # Humidity compensation: RH% is universal, so the breakpoints have
        # safe defaults and only the sensor is needed to enable it.
        vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
        vol.Optional(CONF_FEEDBACK_ENTITY): cv.entity_id,
        vol.Optional(CONF_TRACKING_GAIN, default=0.0): vol.Coerce(float),
        vol.Optional(CONF_TRACKING_MAX, default=3.0): vol.Coerce(float),
        vol.Optional(CONF_HUMIDITY_DRY, default=45.0): vol.Coerce(float),
        vol.Optional(CONF_HUMIDITY_HUMID, default=75.0): vol.Coerce(float),
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
        self._forecast_weight = config[CONF_FORECAST_WEIGHT]
        self._manage_power = config[CONF_MANAGE_POWER]
        self._style = config[CONF_CONTROL_STYLE]
        self._companions: list[str] = config.get(CONF_COMPANION_ENTITIES) or []
        self._companion_owned: list[str] = []

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
        # The comfort band bounds what the fuzzy RULES may ask for. An
        # occupant feedback helper is applied on top of that band (see below),
        # so the entity's own limits must leave room for it - otherwise
        # target_temperature would report a value outside its own min/max the
        # moment someone says they feel cold.
        bias_room = FEEDBACK_BIAS_LIMIT if config.get(CONF_FEEDBACK_ENTITY) else 0.0
        self._attr_min_temp = self._comfort_min - bias_room
        self._attr_max_temp = self._comfort_max + bias_room

        # Trend universe: |3 F/h| (|1.7 C/h|) counts as clearly moving.
        self._trend_limit = 3.0 if us else 1.7
        self._command = build_command_controller(
            t_min, t_max, trend_limit=self._trend_limit
        )
        self._setpoint = build_setpoint_controller(
            config.get(CONF_OUTDOOR_MILD, unit_default("outdoor_mild")),
            config.get(CONF_OUTDOOR_TORRID, unit_default("outdoor_torrid")),
        )
        self._feedback = config.get(CONF_FEEDBACK_ENTITY)
        self._tracking_gain = config[CONF_TRACKING_GAIN]
        self._tracking_max = config[CONF_TRACKING_MAX]
        self._load_sensor = config.get(CONF_LOAD_SENSOR)
        self._load_tau = config[CONF_LOAD_SMOOTHING].total_seconds()
        self._load_ema: float | None = None
        self._load_ema_ts: datetime | None = None
        self._load = (
            build_load_controller(config[CONF_LOAD_LIGHT], config[CONF_LOAD_HEAVY])
            if self._load_sensor
            else None
        )
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._humidity = (
            build_humidity_controller(
                config[CONF_HUMIDITY_DRY], config[CONF_HUMIDITY_HUMID]
            )
            if self._humidity_sensor
            else None
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
        if self._feedback:
            # Occupant feedback is the one input with a person waiting on the
            # other end of it, so it re-evaluates immediately instead of
            # waiting for the next sample. Everything else here is
            # environmental and can wait: a degree of weather is not urgent,
            # but someone who just said they feel cold should not sit through
            # a whole sample_interval (5 min by default) before anything
            # moves - and on a thermally stable room nothing else would wake
            # the loop in the meantime.
            self._unsub.append(
                async_track_state_change_event(
                    self.hass, [self._feedback], self._feedback_changed
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
        elif self._wrapped and self._style == STYLE_SETPOINT:
            # Setpoint style: enabling this entity turns the device on ONCE;
            # from here the only ongoing output is the setpoint. If someone
            # later turns the unit off at its own remote we do not fight them —
            # occupancy re-enables it via this entity, not a 5-minute nag loop.
            await self._async_actuate(True, reason="enabled", force=True)
        await self._async_control()

    # -- sampling ----------------------------------------------------------

    @callback
    def _sensor_changed(self, _event: Any) -> None:
        self.async_write_ha_state()

    async def _feedback_changed(self, _event: Any) -> None:
        await self._async_control()

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
        # The drive is the weather that ACTUALLY EXISTS, so the setpoint tracks
        # the day as it happens. This used to be max(outdoor_now, forecast_high)
        # to "anticipate the afternoon", but a max against the day's peak never
        # relaxes: from midnight onward the controller reasoned as though it
        # were already the hottest moment of the day, parking the target at the
        # aggressive bound straight through a cool morning. That is not
        # anticipation, it is a permanently pessimistic constant - and it is
        # felt most exactly when it is least justified, at dawn, in a room that
        # is already at the mild end of its band.
        #
        # forecast_weight (default 0) folds a FRACTION of the day's expected
        # climb back in for anyone who does want to pre-cool; 1.0 restores the
        # old behaviour exactly. Temporal responsiveness belongs here; letting a
        # transient preference fade belongs in the feedback helper's own decay.
        drive: float | None = None
        if self._outdoor and self._direction == DIRECTION_COOL:
            drive = outdoor_drive(
                self._read_float(self._outdoor),
                self._read_float(self._forecast_high),
                self._forecast_weight,
            )

        # Two independent drivers can push the setpoint toward its aggressive
        # bound: the weather, and the room's own internal load. They FUSE AS
        # MAX — heat sources add, so the setpoint is as aggressive as the
        # strongest driver demands, but a light load must never relax what a
        # hot day already requires (and vice versa). A mean would do exactly
        # that dilution.
        p_outdoor: float | None = None
        p_load: float | None = None
        p_humidity: float | None = None
        indoor_humidity: float | None = None
        if drive is not None:
            p_outdoor = self._setpoint.evaluate({"outdoor": drive}).value
        if self._load is not None and self._direction == DIRECTION_COOL:
            load_value = self._read_float(self._load_sensor)
            if load_value is not None:
                load_value = self._smooth_load(load_value, now)
                p_load = self._load.evaluate({"load": load_value}).value
        if self._humidity is not None and self._direction == DIRECTION_COOL:
            indoor_humidity = self._read_float(self._humidity_sensor)
            if indoor_humidity is not None:
                p_humidity = self._humidity.evaluate(
                    {"humidity": indoor_humidity}
                ).value
        positions = [p for p in (p_outdoor, p_load, p_humidity) if p is not None]
        if positions:
            p = max(positions)
            fuzzy_target = self._comfort_max - p * (
                self._comfort_max - self._comfort_min
            )
            margin = self._margin_wide - p * (self._margin_wide - self._margin_narrow)
        else:
            fuzzy_target = self._clamp_comfort(self._attr_target_temperature)
            margin = self._margin_wide

        # HUMAN FEEDBACK: comfort is ultimately subjective, and no sensor
        # measures the occupant. feedback_entity (any input_number, in degrees)
        # biases the fused target directly - "I'm feeling warm" nudges it down
        # a degree. compose_target owns the ordering (band clamps the rules,
        # THEN the occupant is heard); see fuzzy/targeting.py for why that
        # order is load-bearing. _async_send_setpoint bounds the result by the
        # device's own range.
        bias = 0.0
        if self._feedback:
            raw_bias = self._read_float(self._feedback)
            if raw_bias is not None:
                bias = max(-FEEDBACK_BIAS_LIMIT, min(FEEDBACK_BIAS_LIMIT, raw_bias))
        fuzzy_target = compose_target(
            fuzzy_target,
            comfort_min=self._comfort_min,
            comfort_max=self._comfort_max,
            bias=bias,
            bias_limit=FEEDBACK_BIAS_LIMIT,
        )

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
            ATTR_OUTDOOR_DRIVE: drive,
            ATTR_OUTDOOR_POSITION: round(p_outdoor, 3) if p_outdoor is not None else None,
            ATTR_LOAD_POSITION: round(p_load, 3) if p_load is not None else None,
            ATTR_LOAD_SMOOTHED: round(self._load_ema, 1)
            if self._load_ema is not None
            else None,
            ATTR_HELD_SETPOINT: self._last_sent_setpoint,
            ATTR_HUMIDITY_POSITION: round(p_humidity, 3)
            if p_humidity is not None
            else None,
            ATTR_INDOOR_HUMIDITY: indoor_humidity,
            ATTR_FEEDBACK_BIAS: bias,
            ATTR_ACTIVE_RULES: [
                f"{text} ({strength:.2f})" for text, strength in result.top_rules()
            ],
            ATTR_CONTROL_REASON: "evaluated",
        }

        if self._attr_hvac_mode == HVACMode.OFF:
            self._extra[ATTR_CONTROL_REASON] = "idle: thermostat is off"
            self.async_write_ha_state()
            return

        if self._wrapped and self._style == STYLE_SETPOINT:
            # Setpoint governance: no power gating at all. The device stays on
            # and its own controller — inverter ramp or fixed-speed hysteresis
            # — meets the (fuzzy-computed, channel-fused) setpoint we maintain.
            if self._actuator_on:
                await self._async_send_setpoint()
                self._extra[ATTR_HELD_SETPOINT] = self._last_sent_setpoint
                self._extra[ATTR_CONTROL_REASON] = (
                    f"governing setpoint {self._last_sent_setpoint:g}"
                    if self._last_sent_setpoint is not None
                    else "governing (device setpoint unknown)"
                )
            else:
                self._extra[ATTR_CONTROL_REASON] = (
                    "device is off (switched off externally; re-enable via this entity)"
                )
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
            mode = (
                (HVACMode.HEAT if self._direction == DIRECTION_HEAT else HVACMode.COOL)
                if on
                else HVACMode.OFF
            )
            # STATE-DRIVEN OUTPUT: if the device is already in the mode we
            # want, adopt it silently — do not re-assert. Every redundant
            # command is a beep from the unit confirming nothing.
            wstate = self.hass.states.get(self._wrapped)
            if wstate is not None and wstate.state == mode:
                self._actuator_on = on
                self._last_switch = now
                return
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
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
        # Companions ride the same edge, through the same choke point, so
        # every style (setpoint enable/disable, cycling gates, switch mode)
        # carries them for free. MANUAL CONTROL IS SOVEREIGN: claim only
        # companions that are currently off — one already running was someone
        # else's decision — and on stop, release only what was claimed.
        if self._companions:
            if on:
                claim = []
                for eid in self._companions:
                    st = self.hass.states.get(eid)
                    if st is not None and st.state == "off":
                        claim.append(eid)
                self._companion_owned = claim
                if claim:
                    await self.hass.services.async_call(
                        "homeassistant",
                        SERVICE_TURN_ON,
                        {ATTR_ENTITY_ID: claim},
                        blocking=False,
                    )
            else:
                if self._companion_owned:
                    await self.hass.services.async_call(
                        "homeassistant",
                        SERVICE_TURN_OFF,
                        {ATTR_ENTITY_ID: self._companion_owned},
                        blocking=False,
                    )
                self._companion_owned = []

    def _smooth_load(self, value: float, now: datetime) -> float:
        """The room integrates heat over tens of minutes; load PROXIES (package
        temps, power meters) move in seconds. Feed the channel the thermal
        average, not the flicker — sampling the flicker at control cadence
        aliases it straight into the setpoint. Set load_smoothing: 0 to
        disable for a proxy that is already slow."""
        if self._load_tau <= 0:
            return value
        if self._load_ema is None or self._load_ema_ts is None:
            self._load_ema = value
        else:
            dt = max((now - self._load_ema_ts).total_seconds(), 0.0)
            self._load_ema += (1.0 - math.exp(-dt / self._load_tau)) * (
                value - self._load_ema
            )
        self._load_ema_ts = now
        return self._load_ema

    async def _async_send_setpoint(self) -> None:
        """Supervisor mode: govern the wrapped device's setpoint, gently.

        STATE-DRIVEN OUTPUT, not periodic re-assertion: the target is rounded
        to the DEVICE's own setpoint step (a unit that takes whole degrees
        never sees halves — quantizing to a finer grid than the device's makes
        a noisy input channel flap the command across a rounding boundary),
        and nothing is sent if the device already holds that value. The unit
        beeps per command; a correct controller is silent in steady state.
        """
        wstate = self.hass.states.get(self._wrapped)
        if wstate is None or wstate.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        step = wstate.attributes.get("target_temp_step") or 0.5
        raw = self._effective_target
        # REMOTE-ROOM TRACKING: when the wrapped device senses a DIFFERENT room
        # than target_sensor (a hallway thermostat governed for a bedroom), the
        # fuzzy target alone lands the wrong temperature at the sensor that
        # matters. Trim the sent setpoint by the tracked room's error - room
        # running warm pushes the wrapped setpoint below target, cold above,
        # identically in both directions. tracking_max caps how far one room
        # may impose on a shared zone. Gain 0 (default) disables: a device
        # sensing its own room needs no trim.
        trim = 0.0
        if self._tracking_gain > 0 and self._samples:
            room_now = self._samples[-1][1]
            trim = self._tracking_gain * (room_now - raw)
            trim = max(-self._tracking_max, min(self._tracking_max, trim))
            raw = raw - trim
        self._extra[ATTR_TRACKING_TRIM] = round(trim, 2)

        # Bound by the DEVICE's supported range, not the room's comfort band -
        # see fuzzy/targeting.clamp_to_device. Runs unconditionally: it used to
        # live inside the tracking branch above, which left an instance with
        # tracking disabled (the common single-room case) with no device bound.
        dev_min = wstate.attributes.get("min_temp")
        dev_max = wstate.attributes.get("max_temp")
        raw = clamp_to_device(
            raw,
            float(dev_min) if dev_min is not None else None,
            float(dev_max) if dev_max is not None else None,
        )
        held = wstate.attributes.get(ATTR_TEMPERATURE)
        if held is not None:
            held = float(held)
            # Schmitt gate: the setpoint the device already holds is the
            # anchor. Do not move off it until the target is DECISIVELY
            # elsewhere (3/4 of a device step) — residual channel wobble
            # around a grid boundary must never flap the command. A real
            # change (a degree of weather, sustained load) clears this
            # easily; noise never does.
            if abs(raw - held) < 0.75 * step:
                self._last_sent_setpoint = held
                return
        target = round(raw / step) * step
        if held is not None and abs(held - target) < step / 2:
            self._last_sent_setpoint = target  # device already there
            return
        if (
            self._last_sent_setpoint is not None
            and abs(target - self._last_sent_setpoint) < step / 2
        ):
            return
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self._wrapped, ATTR_TEMPERATURE: target},
            blocking=True,
        )
        self._last_sent_setpoint = target
