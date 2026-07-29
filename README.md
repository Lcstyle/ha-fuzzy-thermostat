# Fuzzy Thermostat for Home Assistant

A thermostat whose decisions come from a **Mamdani fuzzy inference engine**
instead of a fixed on/off band.

This project is a working answer to the long-standing feature request
[*Generic thermostat — make use of modern algorithms (TPI, load and weather
compensation, fuzzy logic)*](https://community.home-assistant.io/t/generic-thermostat-make-use-of-modern-algorithms-tpi-load-and-weather-compensation-fuzzy-logic/332400):
it implements the fuzzy-logic piece, as a drop-in custom component with **zero
dependencies** — no numpy, no scipy, no skfuzzy.

## Why fuzzy

A conventional thermostat reasons in hard thresholds: below 68 turn on, above
70 turn off. A fuzzy controller reasons in overlapping degrees — a room can be
*0.7 comfortable and 0.3 hot* — evaluates a set of human-readable rules
simultaneously, and blends their conclusions into one crisp action. In
practice that buys less overshoot, fewer uncomfortable swings, and setpoints
that respond to conditions (a mild day should not be conditioned like a
heatwave) — without pretending to beat the building's thermal inertia.

## How it works

The engine follows the classic seven-step fuzzy-logic-system algorithm as
presented in Donald J. Norris, *Beginning Artificial Intelligence with the
Raspberry Pi* (Apress, 2017), chapter 5:

```
1-3. Initialisation    linguistic variables, membership functions, rule base
4.   Fuzzification     crisp inputs -> degrees of membership
5.   Inference         AND = min, OR = max; consequents clipped at firing strength
6.   Aggregation       pointwise max across all clipped consequents
7.   Defuzzification   exact piecewise-linear centroid (or weighted average)
```

The engine's test suite **reproduces the numeric outputs published in that
chapter's HVAC demonstration (Demo 5-3, Tables 5-4 to 5-8) to the printed
precision** — the discrete-sum centroid most tutorials implement does not; you
need the true geometric centroid of the clipped piecewise-linear aggregate.

A full cross-reference — which idea in the chapter became which piece of this
code, where the implementation is faithful, where it deliberately departs, and
the errata found while reproducing the published tables — is in
[docs/NORRIS-CH5.md](docs/NORRIS-CH5.md).

Two controllers are built on the engine:

**When to run** — room and target temperature are separate linguistic
variables (*cold / comfortable / hot*) evaluated against the classic 3×3
command matrix, plus two low-weight trend rules that damp demand when the room
is already moving in the right direction (anti-overshoot):

| room \ target | cold | comfortable | hot |
|---|---|---|---|
| **cold** | hold | heat | heat |
| **comfortable** | cool | hold | heat |
| **hot** | cool | cool | hold |

The output is a signed demand in [-1, +1] with a principled deadband around 0,
rather than a pair of magic thresholds.

**What to aim for** — optional outdoor compensation. Outdoor conditions
(current temperature, and today's forecast high so afternoons are anticipated
rather than chased) map to a position between your comfort bounds: mild
outside → the relaxed end, torrid outside → the aggressive end, smoothly
interpolated in between. The computed target **can never leave
`[comfort_min, comfort_max]`** — the bound is structural (a weighted average
of positions in [0, 1] cannot leave [0, 1]), not a clamp bolted on afterwards.

## Two wiring modes

**Switch mode** — the `generic_thermostat` analogue: drives a `heater:` or
`cooler:` switch, with a minimum cycle time.

**Load compensation** — optional, for rooms whose dominant heat source is
*inside*: equipment corners, home offices full of computers, server closets.
Any numeric proxy for internal dissipation (a CPU package temperature, a
smart-plug wattage, a rack sensor) maps to its own position, and the two
drivers fuse as **max(outdoor, load)** — heat sources add, so the setpoint is
as aggressive as the strongest driver demands, and a light load can never
relax what a hot day already requires. The load channel deliberately tops out
at two-thirds of the range: a flat-out computer in a mild week should not
command the same setpoint floor as a heatwave.

**Humidity compensation** — optional. Thermal comfort is a feels-like
judgment: the same temperature reads comfortable at 45% RH and clammy at 75%.
An indoor humidity sensor biases the setpoint downward as the air gets muggier
— compensating perception and putting the compressor to work as a
dehumidifier. Capped at one third of the range: humidity shifts how a
temperature feels by about a degree, it is not a heat source. Indoor RH is the
right input; it already integrates outdoor humidity and infiltration.

The three drivers — weather (full range), internal load (two thirds),
humidity (one third) — fuse as **max()**: the setpoint is as aggressive as the
strongest driver demands, and no mild channel ever dilutes a strong one.

**Supervisor mode** — for equipment that already runs its own controller
(mini-splits, smart heat pumps). Default style is **setpoint governance**: the
device stays on while this entity is on, and the only ongoing output is its
setpoint — inverter units modulate their compressor to meet it, and even
fixed-speed units run their own hysteresis at whatever setpoint they are
handed. Power-cycling a modulating unit defeats its design; the fuzzy layer
moves the target, not the power. Setpoint changes are slew-limited so the
device is never chattered. `control_style: cycling` restores demand-gated
on/off for devices that genuinely should be duty-cycled.

## Install

HACS → custom repository → this repo (category: integration), or copy
`custom_components/fuzzy_thermostat/` into your config's `custom_components/`.

## Configuration

```yaml
climate:
  # Supervisor mode: govern a mini-split
  - platform: fuzzy_thermostat
    name: Study
    climate_entity: climate.study_minisplit
    direction: cool
    target_sensor: sensor.study_temperature
    outdoor_sensor: sensor.outdoor_temperature
    forecast_high_sensor: sensor.forecast_high      # optional
    comfort_min: 70          # hard bounds for the computed target
    comfort_max: 73

  # Switch mode: the generic_thermostat replacement
  - platform: fuzzy_thermostat
    name: Workshop
    heater: switch.workshop_heater
    target_sensor: sensor.workshop_temperature
    comfort_min: 17
    comfort_max: 21
```

| option | default | description |
|---|---|---|
| `target_sensor` | required | room temperature sensor |
| `comfort_min` / `comfort_max` | required | hard bounds on the computed target |
| `heater` / `cooler` / `climate_entity` | one required | the actuator |
| `direction` | `cool` | `heat`/`cool`, for `climate_entity` wiring |
| `outdoor_sensor` | — | enables outdoor compensation (cooling) |
| `forecast_high_sensor` | — | anticipates the day's peak |
| `outdoor_mild` / `outdoor_torrid` | 72/92 °F, 22/33 °C | compensation curve ends |
| `margin_wide` / `margin_narrow` | 2.5/1.0 °F, 1.4/0.6 °C | start-conditioning margin at mild/torrid |
| `sample_interval` | 5 min | evaluation cadence |
| `max_slew` | 0.5 °F, 0.3 °C | max target movement per sample |
| `demand_on` / `demand_off` | 0.10 / 0.03 | demand hysteresis (margin is the binding start gate) |
| `min_cycle_duration` | 10 min | compressor/switch protection |
| `trend_window` | 20 min | slope window for the trend input |
| `manage_power` | `true` | supervisor may switch the device on/off |
| `control_style` | `setpoint` | supervisor style: govern the setpoint, or `cycling` |
| `load_sensor` | — | internal-load proxy (enables load compensation) |
| `load_light` / `load_heavy` | — | sensor values meaning "idle" / "flat out" |
| `humidity_sensor` | — | indoor RH sensor (enables humidity compensation) |
| `humidity_dry` / `humidity_humid` | 45 / 75 %RH | breakpoints for the humidity channel |
| `min_temp` / `max_temp` | 45/95 °F, 7/35 °C | universe bounds for the linguistic terms |

## Observability

Every evaluation is explained on the entity:

```yaml
fuzzy_demand: -0.42
fuzzy_target: 71.5
activation_margin: 1.6
temperature_trend: 0.8        # degrees/hour
outdoor_drive: 88.0
active_rules:
  - "IF room is hot AND target is comfortable THEN cool (0.55)"
  - "IF room is hot AND trend is falling THEN hold [w=0.7] (0.21)"
control_reason: "started: demand -0.42"
```

If a sensor goes unavailable the controller idles and says so — an
unreadable room is never treated as a comfortable one.

## Design principles

* **Bounded authority.** The fuzzy layer proposes; the comfort bounds
  dispose. It cannot set a temperature outside them.
* **Don't fight the equipment.** Minimum cycle times are respected;
  supervisor mode leaves compressor staging to the device that owns it.
* **Fail safe, fail visible.** Missing inputs idle the controller and are
  reported, never guessed around.
* **Explainable.** The rules that fired are readable sentences in the
  attributes.

## Tests

```
python3 -m pytest tests/    # 51 tests, pure python, no HA install needed
```

The suite covers membership geometry, the inference pipeline, the published
book regression, monotonicity of both controllers, and the
bounds-are-never-violated property.

## Roadmap

* TPI and weather-compensated *heating* curves (the other half of the feature
  request)
* Config flow / UI configuration
* Extraction of `fuzzy/` to PyPI, as required for a core Home Assistant PR

## License

MIT.
