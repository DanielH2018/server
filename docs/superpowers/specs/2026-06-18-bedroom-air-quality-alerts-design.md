# Bedroom air-quality alerts (AirGradient ONE → notify + context-aware light pulse)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

The bedroom AirGradient ONE is integrated into HA and reporting CO₂, PM, VOC, and NOx, but
nothing acts on it — bad air (especially CO₂ climbing overnight in a closed room) goes
unnoticed. The operator wants to be alerted when any of four pollutants crosses an unhealthy
threshold, without notification spam and without fighting the existing bedroom lighting model
(Adaptive Lighting + the `input_boolean.bedroom_manual_off` override + the natural-state
dispatcher).

## Goals / decisions

- **Pollutants:** CO₂, PM2.5, VOC index, NOx index (all four).
- **Delivery:** phone notification **always**; **plus** a brief light pulse in the bedroom, but
  **only when the lights are already on** (operator is up/present). Never flash when the lights
  are off — never wake the operator, never fight Adaptive Lighting at night.
- **Lifecycle:** alert **once** when a pollutant crosses into "bad"; stay silent while it stays
  bad; send a single **recovery** notice when it drops back below (with a hysteresis deadband so
  it can't bounce). No periodic nagging.
- **Notify target:** the HA companion app on the operator's phone.

## Architecture

Built on HA's built-in **`threshold` binary-sensor** platform, which provides native hysteresis
("on" above `upper + hysteresis`, "off" below `upper − hysteresis`). That primitive *is* the
"once + recovery, no bounce" lifecycle — no `input_number` bookkeeping or `last_triggered`
checks. One threshold sensor per pollutant feeds **one generic alert automation**; the message
is derived from the triggering sensor's own attributes, so a future pollutant is "add one
threshold sensor + one entity to the trigger list."

Considered and rejected: (B) one automation per pollutant — four copies, hand-rolled hysteresis
×4; (C) a single Jinja config-map automation — hand-rolled hysteresis/state-memory, harder to
read. (A) was chosen because it offloads the tricky part to a built-in integration and stays DRY.

### Component 1 — four `threshold` binary-sensors (in `configuration.yaml.j2`)

Added as a top-level `binary_sensor:` list alongside the existing `input_boolean` /
`adaptive_lighting` blocks. ASCII names (clean entity-id slugs + clean message labels):

| Name | Source sensor | `upper` | `hysteresis` | Created entity | Alerts ≥ / clears ≤ |
|---|---|---|---|---|---|
| `Bedroom CO2 high` | `sensor.bedroom_airgradient_one_carbon_dioxide` | 1200 | 100 | `binary_sensor.bedroom_co2_high` | 1300 / 1100 ppm |
| `Bedroom PM2.5 high` | `sensor.bedroom_airgradient_one_pm2_5` | 35 | 5 | `binary_sensor.bedroom_pm2_5_high` | 40 / 30 µg/m³ |
| `Bedroom VOC high` | `sensor.bedroom_airgradient_one_voc_index` | 250 | 25 | `binary_sensor.bedroom_voc_high` | 275 / 225 (index) |
| `Bedroom NOx high` | `sensor.bedroom_airgradient_one_nox_index` | 50 | 10 | `binary_sensor.bedroom_nox_high` | 60 / 40 (index) |

**Thresholds are starting points — tune to the observed baseline in week one**, especially the
two **index** sensors (Sensirion VOC index baselines ~100; NOx index typically sits near 1 and
spikes), which vary per environment.

### Component 2 — `script.bedroom_alert_pulse` (in `files/scripts.yaml`)

Snapshot-flash-restore, so the lights return to *exactly* what was showing (manual scene, morning
ramp, or AL) rather than being forced back to AL:

1. `scene.create` snapshotting `light.bedroom_lights` into `scene.bedroom_pre_alert`.
2. `light.turn_on` → `rgb_color: [255, 0, 0]`, `brightness_pct: 60`, `transition: 0.3`.
3. `delay: 00:00:02`.
4. `scene.turn_on` → `scene.bedroom_pre_alert`, `transition: 0.5`.

`mode: single`. The caller (Component 3) gates this on the lights being on and calls it as a
blocking service, so invocations serialize — the snapshot can never capture the red pulse itself.

### Component 3 — `automation: bedroom_air_quality_alert` (in `files/automations.yaml`)

- **`mode: queued`, `max: 10`** so simultaneous crossings serialize.
- **Triggers** (two state triggers over all four threshold sensors, each `for: "00:00:30"`):
  - `from: "off" → to: "on"`, `id: bad`
  - `from: "on" → to: "off"`, `id: recovery`
  - Anchoring on `off`↔`on` (not `unknown`) means an HA restart while air is already bad does
    **not** re-alert, and a source going `unavailable` produces no false alert.
- **Action:**
  - A `variables:` block derives everything generically from the triggering sensor:
    `src = trigger.to_state.attributes.entity_id` (threshold exposes its source as `entity_id`),
    `value = states(src)`, `unit = state_attr(src,'unit_of_measurement') or ''`,
    `label = trigger.to_state.attributes.friendly_name | replace(' high','')`,
    `tag = 'air_quality_' ~ trigger.entity_id`.
  - `choose` on `trigger.id`:
    - **bad:** `notify.mobile_app_pixel_9_pro` — title `⚠️ Air quality`, message
      `{{ label }} is {{ value }}{{ ' ' ~ unit if unit else '' }}`, `data: {tag: "{{ tag }}"}`.
      Then **`if is_state('light.bedroom_lights','on')`** → `script.bedroom_alert_pulse`.
    - **recovery:** `notify.mobile_app_pixel_9_pro` — title `✅ Air quality`, message
      `{{ label }} back to normal ({{ value }}{{ ' ' ~ unit if unit else '' }})`, same `tag`
      (so it replaces the bad notification on the phone). No light action.

## Data flow

AirGradient (local polling) → source sensors → `threshold` binary-sensors (hysteresis) →
single automation → notify (always) + conditional snapshot-flash-restore light pulse.

## Error handling / edge cases

- **Unavailable/unknown source:** threshold goes `unknown`; triggers anchored on `off`↔`on`
  never fire on `*→unknown` or `unknown→*`, so no false alert/recovery.
- **HA restart while bad:** `unknown→on` ≠ `from:"off"`, so no re-alert. (Trade-off: a clear
  that happens after a restart-while-bad can emit a lone recovery with no preceding bad — rare,
  acceptable.)
- **Momentary spikes:** the `for: "00:00:30"` debounce drops blips.
- **Manual-off respected implicitly:** the pulse only runs when the lights are on, so an engaged
  `bedroom_manual_off` (lights off) yields notification-only — no light action, by construction.
- **Notifications are not time-gated** (CO₂ at night is exactly when you'd want to know). Quiet
  hours are a documented future tunable, not built now (YAGNI).

## Testing (manual — repo has no HA unit harness)

- Before deploy: `Developer Tools → YAML → Check Configuration`.
- After deploy: temporarily lower a threshold (or set a source value via `Developer Tools →
  States`) to force a crossing; confirm the bad notification, the light pulse (lights on) /
  no pulse (lights off), then a recovery notification that replaces it.

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — `binary_sensor:` block (4 threshold sensors)
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — `bedroom_alert_pulse`
- `ansible/roles/containers/home-assistant/files/automations.yaml` — `bedroom_air_quality_alert`
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the air-quality alert subsystem

All feed `common_config_changed`, so a deploy recreates HA (~120s).

## Future / out of scope

- Surfacing CO₂/PM2.5 on the Bedroom dashboard (separate, optional).
- Quiet-hours gating of notifications.
- Two-tier (warn/high) severity per pollutant.
