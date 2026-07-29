"""Constants for the fuzzy thermostat integration."""
from __future__ import annotations

DOMAIN = "fuzzy_thermostat"

CONF_TARGET_SENSOR = "target_sensor"
CONF_HEATER = "heater"
CONF_COOLER = "cooler"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_DIRECTION = "direction"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_FORECAST_HIGH_SENSOR = "forecast_high_sensor"
CONF_COMFORT_MIN = "comfort_min"
CONF_COMFORT_MAX = "comfort_max"
CONF_OUTDOOR_MILD = "outdoor_mild"
CONF_OUTDOOR_TORRID = "outdoor_torrid"
CONF_MARGIN_WIDE = "margin_wide"
CONF_MARGIN_NARROW = "margin_narrow"
CONF_SAMPLE_INTERVAL = "sample_interval"
CONF_MAX_SLEW = "max_slew"
CONF_DEMAND_ON = "demand_on"
CONF_DEMAND_OFF = "demand_off"
CONF_MIN_CYCLE_DURATION = "min_cycle_duration"
CONF_TREND_WINDOW = "trend_window"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_MANAGE_POWER = "manage_power"

DIRECTION_COOL = "cool"
DIRECTION_HEAT = "heat"

ATTR_FUZZY_DEMAND = "fuzzy_demand"
ATTR_FUZZY_TARGET = "fuzzy_target"
ATTR_ACTIVATION_MARGIN = "activation_margin"
ATTR_TREND = "temperature_trend"
ATTR_ACTIVE_RULES = "active_rules"
ATTR_CONTROL_REASON = "control_reason"
ATTR_OUTDOOR_DRIVE = "outdoor_drive"

# Defaults that do not depend on the unit system.
DEFAULT_SAMPLE_INTERVAL_S = 300
DEFAULT_DEMAND_ON = 0.35
DEFAULT_DEMAND_OFF = 0.15
DEFAULT_MIN_CYCLE_S = 600
DEFAULT_TREND_WINDOW_S = 1200

# Unit-dependent defaults, filled in at entity construction from the unit
# system the instance runs in. (F, C)
DEFAULTS_BY_UNIT = {
    "min_temp": (45.0, 7.0),
    "max_temp": (95.0, 35.0),
    "outdoor_mild": (72.0, 22.0),
    "outdoor_torrid": (92.0, 33.0),
    "margin_wide": (2.5, 1.4),
    "margin_narrow": (1.0, 0.6),
    "max_slew": (0.5, 0.3),
}
