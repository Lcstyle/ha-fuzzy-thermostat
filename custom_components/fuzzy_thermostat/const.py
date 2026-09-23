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
CONF_FORECAST_WEIGHT = "forecast_weight"
CONF_COMFORT_MIN = "comfort_min"
CONF_COMFORT_MAX = "comfort_max"
CONF_OUTDOOR_MILD = "outdoor_mild"
CONF_OUTDOOR_TORRID = "outdoor_torrid"
# Heating's aggressive anchor, the mirror of outdoor_torrid. Named for what it
# describes rather than reusing "torrid" with an inverted meaning.
CONF_OUTDOOR_FRIGID = "outdoor_frigid"
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
CONF_CONTROL_STYLE = "control_style"
CONF_COMPANION_ENTITIES = "companion_entities"
STYLE_SETPOINT = "setpoint"
STYLE_CYCLING = "cycling"
CONF_LOAD_SENSOR = "load_sensor"
CONF_LOAD_LIGHT = "load_light"
CONF_LOAD_HEAVY = "load_heavy"
CONF_LOAD_SMOOTHING = "load_smoothing"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_HUMIDITY_DRY = "humidity_dry"
CONF_HUMIDITY_HUMID = "humidity_humid"
CONF_FEEDBACK_ENTITY = "feedback_entity"
CONF_FEEDBACK_BIAS_LIMIT = "feedback_bias_limit"
# The seasonal dial: helpers that slide the whole comfort band, and the cap on
# how far they may slide it. Distinct from feedback_entity -- see
# fuzzy/targeting.seasonal_band for why the band and the bias are not the same
# authority.
CONF_COMFORT_SHIFT_ENTITY = "comfort_shift_entity"
CONF_COMFORT_SHIFT_LIMIT = "comfort_shift_limit"
CONF_HUMIDITY_CAP = "humidity_cap"
CONF_TRACKING_GAIN = "tracking_gain"
CONF_TRACKING_MAX = "tracking_max"

DIRECTION_COOL = "cool"
DIRECTION_HEAT = "heat"

ATTR_FUZZY_DEMAND = "fuzzy_demand"
ATTR_FUZZY_TARGET = "fuzzy_target"
ATTR_ACTIVATION_MARGIN = "activation_margin"
ATTR_TREND = "temperature_trend"
ATTR_ACTIVE_RULES = "active_rules"
ATTR_CONTROL_REASON = "control_reason"
ATTR_OUTDOOR_DRIVE = "outdoor_drive"
ATTR_OUTDOOR_POSITION = "outdoor_position"
ATTR_LOAD_POSITION = "load_position"
ATTR_LOAD_SMOOTHED = "load_smoothed"
ATTR_HELD_SETPOINT = "held_setpoint"
# The read-back ledger (fuzzy/actuation.py). held_setpoint is only ever a value the
# wrapped device has REPORTED back; requested_setpoint is what was asked for, and
# control_fault says so when the device never showed it back.
ATTR_REQUESTED_SETPOINT = "requested_setpoint"
ATTR_CONTROL_FAULT = "control_fault"
ATTR_HUMIDITY_POSITION = "humidity_position"
ATTR_INDOOR_HUMIDITY = "indoor_humidity"
ATTR_FEEDBACK_BIAS = "feedback_bias"
ATTR_COMFORT_SHIFT = "comfort_shift"
ATTR_COMFORT_BAND = "comfort_band"
ATTR_TRACKING_TRIM = "tracking_trim"

# Defaults that do not depend on the unit system.
DEFAULT_SAMPLE_INTERVAL_S = 300
DEFAULT_LOAD_SMOOTHING_S = 900
# How long a wrapped device gets to report a command back before it is retried, and how
# many sends (the original included) before a mismatch is surfaced as a fault. A Kumo
# head took ~60 s to report a new setpoint in the 2026-09-23 test.
DEFAULT_CONFIRM_AFTER_S = 120
DEFAULT_MAX_SEND_ATTEMPTS = 3
# How much of the forecast high to fold into the outdoor drive. 0 = follow the
# weather that actually exists; 1 = the old max(now, forecast) behaviour, which
# reasons as though it were already the hottest moment of the day from midnight
# onward. Defaults to 0 because the controller must be temporally responsive:
# anticipation that never relaxes is not anticipation, it is a permanently
# pessimistic constant.
DEFAULT_FORECAST_WEIGHT = 0.0
# The margin (target + margin) is the intended binding start gate; demand only
# contributes direction and trend damping. At 0.35 on a wide temperature
# universe the demand gate accidentally out-ranked the margin and would hold
# off until ~10 degrees past target. Found in the first real deployment.
DEFAULT_DEMAND_ON = 0.10
DEFAULT_DEMAND_OFF = 0.03
DEFAULT_MIN_CYCLE_S = 600
DEFAULT_TREND_WINDOW_S = 1200
DEFAULT_HUMIDITY_CAP = 1.0 / 3.0

# Unit-dependent defaults, filled in at entity construction from the unit
# system the instance runs in. (F, C)
DEFAULTS_BY_UNIT = {
    "min_temp": (45.0, 7.0),
    "max_temp": (95.0, 35.0),
    "outdoor_mild": (72.0, 22.0),
    "outdoor_torrid": (92.0, 33.0),
    # Heating's own curve ends. NOT derived from the cooling pair: a default of
    # `outdoor_mild - (torrid - mild)` puts the aggressive anchor at 52F, which
    # saturates p=1 across most of any real heating season and makes the
    # "weather-compensated" curve a constant. 62F is where heating stops being
    # wanted; 10F is a design-day cold snap.
    "heat_mild": (62.0, 17.0),
    "outdoor_frigid": (10.0, -12.0),
    # Degree-dimensioned like every other row here, and for the same reason: a
    # flat 5.0 handed to a metric install is 9F of travel on a band that may be
    # 3C wide.
    "feedback_bias_limit": (2.0, 1.1),
    "comfort_shift_limit": (5.0, 2.8),
    "margin_wide": (2.5, 1.4),
    "margin_narrow": (1.0, 0.6),
    "max_slew": (0.5, 0.3),
}
