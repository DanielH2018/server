# HA humidity comfort alerts + unified threshold-alert engine

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Add humidity comfort alerts (too HIGH >~60% → mold; too LOW <~30% → winter-dry) over
`sensor.bedroom_airgradient_one_humidity`. The backlog wants this "folded into the air-quality
alert engine." By now there are **three** near-identical threshold→notify automations
(`bedroom_air_quality_alert`, `bedroom_battery_low_alert`, and the proposed humidity one), so the
chosen approach is **full unification**: replace the air-quality + battery automations with one
generic `bedroom_threshold_alert` engine that also covers humidity.

## Goals / decisions

- **Humidity is two-sided → two one-sided `threshold` sensors** (`Bedroom humidity high` with
  `upper`, `Bedroom humidity low` with `lower`), reusing the air-quality `upper` and battery
  `lower` patterns verbatim — rather than one range-type sensor (which would need bespoke
  `position`-attribute side detection).
- **One unified engine.** All threshold sensors feed `bedroom_threshold_alert`. The only
  per-category differences are the notification **title/emoji** and **whether to pulse the lights**
  — everything else (label/value/unit derivation, message shape, recovery, coalescing tag) is
  generic.
- **Thresholds are starting points** (tunable; humidity joins the existing ~2026-06-25 air-quality
  threshold tuning pass).

## Architecture

### Component 1 — 2 humidity `threshold` binary-sensors (`configuration.yaml.j2`)

Added to the existing `binary_sensor:` list:

| Name | Source | bound | `hysteresis` | Created entity | Alerts / clears |
|---|---|---|---|---|---|
| `Bedroom humidity high` | `sensor.bedroom_airgradient_one_humidity` | `upper: 60` | 3 | `binary_sensor.bedroom_humidity_high` | ≥63% / ≤57% (mold) |
| `Bedroom humidity low` | `sensor.bedroom_airgradient_one_humidity` | `lower: 30` | 3 | `binary_sensor.bedroom_humidity_low` | ≤27% / ≥33% (dry) |

### Component 2 — `automation: bedroom_threshold_alert` (replaces AQ + battery automations)

`mode: queued`, `max: 10`. **Six trigger blocks** = 3 categories × {bad `off→on`, recovery
`on→off`}, with the **category encoded in the trigger `id`** (`airquality_bad`, `airquality_ok`,
`battery_bad`, `battery_ok`, `humidity_bad`, `humidity_ok`). Per-block debounce:

- airquality: `for: "00:00:30"` (as today)
- battery: `for: "00:01:00"` (as today)
- humidity: `for: "00:05:00"` (rides out transient spikes — mold/dry are slow conditions)

All blocks anchor on `off↔on` (not `unknown`) so an HA restart while bad doesn't re-alert and an
unavailable source can't false-alert (the sensor-offline automation owns offline).

**Action:**
- `variables:` derive generically from the triggering threshold sensor (a threshold sensor's
  `entity_id` attribute points at its source):
  - `category = trigger.id.split('_')[0]`, `edge = trigger.id.split('_')[1]`
  - `src = trigger.to_state.attributes.entity_id`, `value = states(src)`,
    `unit = state_attr(src,'unit_of_measurement') or ''`
  - `label = trigger.to_state.attributes.friendly_name | replace(' high','') | replace(' low','')`
    (the single extra `replace(' low','')` is what makes battery + low-humidity read correctly)
  - `tag = 'threshold_alert_' ~ trigger.entity_id`
  - `cfg` = a category→config map:
    ```
    airquality → {bad:'⚠️ Air quality', ok:'✅ Air quality', pulse: true}
    battery    → {bad:'🔋 Battery low',  ok:'🔋 Battery OK',  pulse: false}
    humidity   → {bad:'💧 Humidity',     ok:'💧 Humidity OK', pulse: false}
    ```
- `choose` on `edge`:
  - **bad:** notify — title `{{ cfg.bad }}`, message `{{ label }} is {{ value }}{{ ' '~unit if unit else '' }}`, `data:{tag}`. Then `if cfg.pulse and is_state('light.bedroom_lights','on')` → `script.bedroom_alert_pulse` (unchanged).
  - **recovery (default):** notify — title `{{ cfg.ok }}`, message `{{ label }} back to normal ({{ value }}{{ ' '~unit if unit else '' }})`, same `tag`.

`script.bedroom_alert_pulse` is retained (only the air-quality category uses it).

## Data flow

source sensor → `threshold` binary-sensor (hysteresis) → one of the 6 trigger blocks (category in
`id`) → `bedroom_threshold_alert` → category-titled notify (+ light pulse only for airquality when
lights on).

## Migration / regression notes

- **Deletes** `bedroom_air_quality_alert` and `bedroom_battery_low_alert`; **adds**
  `bedroom_threshold_alert`. The two threshold-sensor sets in `configuration.yaml.j2` are unchanged
  (4 air-quality + 2 battery) plus the 2 new humidity ones.
- **The single behavioral invariant to verify:** air-quality still *pulses* the lights on a bad
  crossing (lights on), battery/humidity do *not*. This is encoded solely by `cfg.pulse`.
- Tag scheme changes from `air_quality_*` / `battery_low_*` to `threshold_alert_*` — purely a
  phone-coalescing key, no functional effect.

## Error handling / edge cases

- **Unavailable source:** threshold → `unknown`; `off↔on` triggers never fire on `*↔unknown`.
- **HA restart while bad:** `unknown→on` ≠ `from:"off"` → no re-alert (same accepted trade-off as
  the original engines: a clear after restart-while-bad can emit a lone recovery — rare).
- **Humidity transient spikes:** the `for: "00:05:00"` debounce drops them.
- **`cfg` dict variable:** HA `variables:` supports a template returning a dict; `cfg.bad` /
  `cfg.pulse` resolve normally.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `bedroom_threshold_alert` is `on`; confirm
  `automation.bedroom_air_quality_alert` and `automation.bedroom_battery_low_alert` are **gone**;
  confirm `binary_sensor.bedroom_humidity_high` / `_low` exist and read `off` (humidity ~53%).
  Functional: temporarily lower the humidity-high threshold (or set the source via Developer Tools
  → States) to force a crossing → confirm the 💧 notification; confirm a CO2 crossing still pulses
  the lights (when on) and a battery/humidity crossing does not.

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — 2 humidity sensors
- `ansible/roles/containers/home-assistant/files/automations.yaml` — remove 2 automations, add the unified one
- `ansible/roles/containers/home-assistant/CLAUDE.md` — replace the AQ + battery bullets with the unified engine
- `ansible/PLANS.md` — move the humidity item to done

`configuration.yaml` + `automations.yaml` feed `common_config_changed`, so a deploy recreates HA
(~120s). Z2M untouched.

## Future / out of scope

- DND-aware routing (separate item) — would wrap the unified notify.
- Two-tier (warn/high) severity per category.
- Surfacing humidity on the Bedroom dashboard.
