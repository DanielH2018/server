# HA sensor-offline alerts (watched bedroom entities → notify when one goes unavailable)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom + Zigbee2MQTT (`ansible/roles/containers/{home-assistant,zigbee2mqtt}`)

## Problem

The bedroom automations silently depend on four moving parts — the AirGradient ONE
(air-quality alerts + temp→fan control), the Aqara FP300 (presence lighting), the Hue Tap Dial
RDM002 (the controller), and the DREO tower fan (`fan.tower_fan`). When one dies, the dependent
automation just stops with **no signal** — e.g. an FP300 Zigbee dropout makes presence lighting
quietly fail, which undermines everything built this session.

The two device classes fail differently, and that is the crux of the design:

- **AirGradient ONE + DREO fan fail loudly.** Their HA integrations poll the device and mark its
  entities `unavailable` within minutes of a dropout.
- **FP300 + Tap Dial fail silently.** They are battery Zigbee end-devices behind Zigbee2MQTT,
  and **Z2M availability is currently disabled** (verified 2026-06-18: Z2M 2.12.0, no
  `availability:` key in the config, no `zigbee2mqtt/<device>/availability` topic publishes in
  the logs). With availability off, their HA entities never go `unavailable` — they freeze at
  the last value forever. A naive "watch for `unavailable`" automation would therefore *never
  fire* for the two devices we most care about. This is the same "fails silently" trap, one
  layer deeper.

## Goals / decisions

- **Watched entities (4):** AirGradient ONE, FP300, Tap Dial, DREO fan — the bedroom-automation
  dependency layer. The UPS and other infra are explicitly out of scope (covered by the homelab's
  separate monitoring stack).
- **Detection:** enable Z2M availability so the Zigbee devices' entities go `unavailable` on a
  dropout, then one generic automation watches all four entities for `unavailable`.
- **Latency is honest, not uniform.** The AirGradient/fan are detected in minutes; the battery
  Zigbee devices are detected after Z2M's *passive* timeout. Z2M can't actively ping a sleeping
  radio without draining its battery, so battery-device offline detection is inherently coarser —
  a physics fact, not a tuning knob. Logs show the Tap Dial self-reports every ~7–18 min and the
  FP300 is very chatty, so a **60-min passive timeout** catches a real dropout within the hour
  with comfortable margin — far better than Z2M's 25h default.
- **Lifecycle:** alert once when an entity goes offline; a single recovery notice when it returns;
  same notification `tag` per entity so the recovery coalesces with the alert on the phone. No
  nagging.
- **Notify target:** `notify.mobile_app_pixel_9_pro`. No light pulse (offline can happen at any
  hour, including while asleep — never flash).

## Architecture

Two components, deliberately a twin of the existing air-quality alert engine
(`docs/superpowers/specs/2026-06-18-bedroom-air-quality-alerts-design.md`): a primitive that
produces clean `unavailable` edges, feeding one generic attribute-driven notify automation.

### Component 1 — Enable Z2M availability (`zigbee2mqtt/templates/configuration.yaml.j2`)

Add a top-level `availability:` block:

```yaml
availability:
  enabled: true           # off by default in Z2M 2.12 (verified against settings.schema.json)
  active:                 # mains/routed devices (the 3 Hue bulb routers)
    timeout: 10           # minutes
  passive:                # battery end-devices (FP300, Tap Dial)
    timeout: 60           # minutes — STARTING POINT, tune to observed cadence
```

Schema confirmed against the running Z2M 2.12 `settings.schema.json`: `availability.enabled`
defaults to `false` (hence off today), and `active.timeout` / `passive.timeout` are minutes.
Z2M then publishes `online`/`offline` to `zigbee2mqtt/<device>/availability`, and its HA MQTT
discovery configs gain an `availability_topic` so every entity of an offline device flips to
`unavailable` together. Z2M validates config on boot and refuses to start on an unknown key, so
a mistake fails the deploy loudly rather than silently.

The `zigbee2mqtt` role already wires the config template to `common_config_changed`
(`zigbee2mqtt_config is changed`), so this edit recreates the container on deploy.

**Side effect (acceptable):** enabling availability makes Z2M actively ping the 3 Hue bulb
routers every `active.timeout` — minor, normal Zigbee traffic.

### Component 2 — `automation: bedroom_sensor_offline_alert` (`home-assistant/files/automations.yaml`)

Structurally a twin of `bedroom_air_quality_alert`: `mode: queued`, `max: 10`, two state triggers
over one entity list, the message derived generically from the triggering entity.

- **Triggers:**
  - `offline` — watched entities `to: "unavailable"`, `for: "00:05:00"`.
  - `recovery` — watched entities `from: "unavailable"`.
- **Watched entity list (both triggers):**
  - `sensor.bedroom_airgradient_one_carbon_dioxide` (AirGradient — fast-polling representative)
  - `binary_sensor.aqara_fp300_presence` (FP300 — the entity presence lighting depends on)
  - `sensor.0x001788010f0ccda4_battery` (Tap Dial — confirmed against the HA entity registry;
    friendly name is the IEEE address since the dial was never renamed in Z2M)
  - `fan.tower_fan` (DREO)
  - With Z2M availability on, *all* of a device's entities flip together, so one representative
    entity per device suffices.
- **Action:** a `variables:` block derives the human name from the **available side** of the
  transition, then `choose` on `trigger.id`:
  - **offline:** `notify.mobile_app_pixel_9_pro` — title `⚠️ Sensor offline`, message
    `{{ name }} is offline (no data)`, `data: {tag: "sensor_offline_{{ trigger.entity_id }}"}`.
  - **recovery:** `notify.mobile_app_pixel_9_pro` — title `✅ Sensor back online`, message
    `{{ name }} is reporting again`, same `tag`.

**Name-derivation gotcha (load-bearing):** an entity's `friendly_name` attribute is empty *while
it is `unavailable`*, so the name can't be read from the unavailable state. Read it from the
available side of the transition — `trigger.from_state` for the offline edge, `trigger.to_state`
for the recovery edge — with `default(trigger.entity_id)` as the fallback. A single coalescing
expression covers both directions:

```yaml
variables:
  name: >-
    {{ (trigger.to_state.attributes.friendly_name
        if (trigger.to_state is not none and trigger.to_state.attributes.friendly_name is defined)
        else trigger.from_state.attributes.friendly_name)
       | default(trigger.entity_id, true) }}
```

## Data flow

Device dropout → (Zigbee: Z2M passive/active availability → `unavailable`; AirGradient/fan: their
own integration → `unavailable`) → `for: 5min` state trigger → one generic automation → notify
(offline). Device returns → `from: unavailable` trigger → notify (recovery, same tag).

## Error handling / edge cases

- **HA restart / HA+Z2M deploy recreate (~120s):** entities go briefly `unavailable`; the
  `for: "00:05:00"` debounce rides it out — no false offline alert. (Same reasoning the
  air-quality automation used to anchor on stable edges.)
- **`unknown` vs `unavailable`:** we anchor on `unavailable` (the real offline signal). `unknown`
  is a transient startup state that the 5-min `for:` would ride out anyway.
- **Attribute-on-unavailable:** handled by the coalescing name expression above.
- **Recovery without a prior alert:** if HA restarts while a device is already offline, a later
  recovery can emit a lone "back online" with no preceding alert — rare, acceptable (mirrors the
  air-quality spec's restart-while-bad trade-off).
- **DND / critical routing:** not built (separate backlog item). Ships as a plain notify with the
  hook left for that future cross-cutting layer.

## Testing (manual — repo has no HA unit harness)

- **Before deploy:** Z2M validates its config on boot; HA Developer Tools → YAML → Check
  Configuration. The repo's `validate-compose` hook re-renders the Z2M template on edit.
- **After deploy:**
  - Confirm Z2M is publishing availability: `docker logs zigbee2mqtt | grep "/availability'"`
    should now show `online` publishes per device.
  - Force an offline: pull the FP300 battery (or unplug the AirGradient); wait past the `for:`
    window; confirm the offline notification; restore; confirm the recovery notification replaces
    it. For a faster smoke test, temporarily set `for:` to `00:00:30` and set an entity to
    `unavailable` via Developer Tools → States.

## Files touched

- `ansible/roles/containers/zigbee2mqtt/templates/configuration.yaml.j2` — `availability:` block
- `ansible/roles/containers/zigbee2mqtt/CLAUDE.md` — document availability + the timeout rationale
- `ansible/roles/containers/home-assistant/files/automations.yaml` — `bedroom_sensor_offline_alert`
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the offline-alert subsystem
- `ansible/PLANS.md` — move the item from Backlog to a done note

The Z2M and HA config edits each feed `common_config_changed`, so a deploy recreates the
respective container.

## Future / out of scope

- DND-aware / critical-channel routing (separate backlog item) — would wrap this notify.
- Surfacing per-device availability on the Bedroom dashboard.
- Per-device passive-timeout tuning once each battery device's natural cadence is observed.
