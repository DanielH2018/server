# HA low-battery alerts (battery threshold sensors → generic notify)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

The two battery Zigbee devices in the bedroom — the Aqara FP300 (presence lighting) and the Hue
Tap Dial RDM002 (the controller) — will eventually run their batteries down. A dead battery is the
same silent failure the sensor-offline alert addresses, but with *warning* available: the battery
level is reported continuously, so we can notify *before* the device dies, not just after.

## Goals / decisions

- **Watched batteries (2):** `sensor.aqara_fp300_battery`, `sensor.0x001788010f0ccda4_battery`
  (Tap Dial — IEEE-named, never renamed in Z2M).
- **Alert point:** ~15% (a starting point — tunable like the air-quality thresholds).
- **Lifecycle:** alert once when a battery crosses low; single recovery notice when a fresh
  battery clears it; same notification `tag` per device so the recovery coalesces.
- **Delivery:** phone notification only (`notify.mobile_app_pixel_9_pro`). No light pulse — a low
  battery is not an air-quality urgency.

## Architecture

A deliberate mirror of the air-quality alert engine
(`docs/superpowers/specs/2026-06-18-bedroom-air-quality-alerts-design.md`): built-in `threshold`
binary-sensors provide the hysteresis lifecycle, feeding one generic attribute-driven notify
automation. Batteries are the **lower-bound** case of the same primitive.

### Component 1 — two `threshold` binary-sensors (`configuration.yaml.j2`)

Added to the existing top-level `binary_sensor:` list (alongside the four air-quality thresholds):

| Name | Source | `lower` | `hysteresis` | Created entity | Alerts ≤ / clears ≥ |
|---|---|---|---|---|---|
| `Bedroom FP300 battery low` | `sensor.aqara_fp300_battery` | 20 | 5 | `binary_sensor.bedroom_fp300_battery_low` | 15% / 25% |
| `Bedroom Tap Dial battery low` | `sensor.0x001788010f0ccda4_battery` | 20 | 5 | `binary_sensor.bedroom_tap_dial_battery_low` | 15% / 25% |

A `lower` threshold inverts the hysteresis relative to the air-quality `upper` ones: the sensor is
`on` (low) when value ≤ `lower − hysteresis` (15%) and `off` (recovered) when value ≥
`lower + hysteresis` (25%). Battery drain is monotonic (only jumps up on replacement), so flapping
is a non-issue. Thresholds are starting points — tune per device if 15% proves too late/early.

### Component 2 — `automation: bedroom_battery_low_alert` (`files/automations.yaml`)

Structurally identical to `bedroom_air_quality_alert`: `mode: queued`, `max: 10`, two state
triggers over both threshold sensors (`off→on` id `low`, `on→off` id `recovery`, each
`for: "00:01:00"`), message derived generically from the triggering sensor.

- A `variables:` block derives everything from the triggering threshold sensor (the `threshold`
  platform exposes its source in the `entity_id` attribute):
  `src = trigger.to_state.attributes.entity_id`, `value = states(src)`,
  `label = trigger.to_state.attributes.friendly_name | replace(' battery low','')`,
  `tag = 'battery_low_' ~ trigger.entity_id`.
- `choose` on `trigger.id`:
  - **low:** notify — title `🔋 Battery low`, message `{{ label }} battery at {{ value }}%`,
    `data: {tag: "{{ tag }}"}`.
  - **recovery (default):** notify — title `🔋 Battery OK`, message
    `{{ label }} battery back to {{ value }}%`, same `tag`.
- Anchored on `off↔on` (not `unknown`), so an HA restart while a battery is already low does not
  re-alert, and a battery source going `unavailable` (device offline) produces no false battery
  alert — the sensor-offline automation owns that case.

## Data flow

Battery sensor → `threshold` binary-sensor (lower-bound hysteresis) → generic automation →
notify (low) / notify (recovery, same tag).

## Error handling / edge cases

- **Unavailable source:** threshold goes `unknown`; `off↔on`-anchored triggers never fire on
  `*→unknown`, so a dead/offline device produces no false *battery* alert (offline is the
  sensor-offline automation's job).
- **HA restart while low:** `unknown→on` ≠ `from:"off"`, so no re-alert (same trade-off as
  air-quality: a clear after a restart-while-low can emit a lone recovery — rare, acceptable).
- **No light pulse / not time-gated:** battery alerts are low-urgency and infrequent; a plain
  notification at any hour is fine.

## Testing (manual — repo has no HA unit harness)

- Before deploy: Z2M unaffected; HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `binary_sensor.bedroom_fp300_battery_low` /
  `binary_sensor.bedroom_tap_dial_battery_low` exist and read `off` (both batteries are healthy).
  To force: temporarily raise a threshold `lower` above the live battery value (or set the source
  via Developer Tools → States) to cross it; confirm the low notification, then a recovery.

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — 2 threshold sensors
- `ansible/roles/containers/home-assistant/files/automations.yaml` — `bedroom_battery_low_alert`
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the battery-alert subsystem
- `ansible/PLANS.md` — move the item to done

Both HA edits feed `common_config_changed`, so a deploy recreates HA (~120s). Z2M is untouched.

## Future / out of scope

- **Unify the alert engines.** `bedroom_air_quality_alert`, `bedroom_battery_low_alert`, and the
  planned humidity alert (which the backlog wants folded into "the air-quality alert engine") are
  the same skeleton over different `threshold` sensors. Once humidity lands, generalize all three
  into one `bedroom_threshold_alert` engine (category → title/icon map; the light-pulse branch
  gated to the air-quality category). Not pre-built now (YAGNI) — flagged as the refactor point.
- Auto-discovering every `device_class: battery` entity instead of an explicit list (more magic;
  the explicit-list pattern matches air-quality and gives clean per-device tags).
