# Presence + Adaptive Bedroom Lighting (Home Assistant) — Design

**Date:** 2026-06-18
**Status:** Approved design, pre-implementation
**Scope:** Extend the bedroom lighting: FP300 presence on/off, Adaptive-Lighting sun curve,
illuminance gate, and a manual-off override with a weekday/weekend morning reset + wake.
**Builds on:** `2026-06-18-hue-tap-dial-bedroom-lights-design.md` (the Tap Dial + group + scenes).

## Goal

Make the bedroom lights automatic: turn on when you enter a dim room, off when you leave,
continuously adapt color temperature + brightness to the time of day — with one deliberate
exception: when you turn the lights off with the Tap Dial (bedtime / manual off), nothing turns
them back on until you turn them on yourself or the morning reset fires.

## Verified facts (current state)

- **Group:** `light.bedroom_lights` — a HA Light Group helper over the 3 Hue bulbs (exists).
- **Presence:** `binary_sensor.aqara_fp300_presence` — FP300 mmWave; stays `on` while you're in
  the room even sitting still, clears after the device's absence delay (currently 10 s).
- **Light level:** `sensor.aqara_fp300_illuminance` — live lux (read 261 with lights on).
- **Motion (unused here):** `binary_sensor.aqara_fp300_pir_detection`.
- **Tap Dial:** publishes JSON to `zigbee2mqtt/0x001788010f0ccda4`; automation reads `.action`.
- **Z2M backend healthy**, all 5 devices reporting. ("No devices in the Z2M UI" is a separate
  frontend/websocket display issue — does NOT affect HA entities. Triage separately.)
- HACS is installed; the Dreo custom component is the precedent for a HACS-installed integration.

## Architecture — separation of concerns

The design splits two concerns that are usually tangled:

- **Adaptive Lighting (HACS integration)** owns *how the lights look while on* — the sun-tracking
  color-temp + brightness curve, and backing off when you change them by hand. It never turns
  lights on or off.
- **Our automations + one override flag** own *whether the lights are on* — presence, the
  illuminance gate, and the manual-off override. They never set color.

That boundary keeps the system debuggable: a wrong color is an AL problem; a wrong on/off is an
automation problem.

## Components

| Component | What | Where |
|-----------|------|-------|
| Adaptive Lighting integration | Sun curve over `light.bedroom_lights` | HACS install (manual) + `adaptive_lighting:` templated in `configuration.yaml.j2` |
| `input_boolean.bedroom_manual_off` | Override flag | `input_boolean:` templated in `configuration.yaml.j2` |
| Presence-on / absence-off / morning-reset automations | On/off + wake logic | `files/automations.yaml` (git) |
| Modified Tap Dial automation | Smart button-1 + override set/clear | `files/automations.yaml` (git) |

## Behavior

| Situation | Result |
|---|---|
| Enter **empty, dim** room (lux < gate, override off) | Lights on; AL sets the look for the hour |
| Enter in **daylight** (lux ≥ gate) | Stay off (illuminance gate) |
| Room **empty for ~1 min** | Lights off |
| **Dial rotate** | Brightness; AL detects the manual change and backs off |
| **Dial button 1 → off** (bedtime/manual) | Off **+ override ON** (presence won't re-on) |
| **Dial button 1 → on**, or any **scene button (2–4)** | On **+ override OFF** (auto resumes) |
| **06:00 Mon–Fri / 07:00 Sat–Sun** | Override **always cleared**; **if present** → gentle ~5-min fade-up, then AL takes over |

## The override (state machine)

`input_boolean.bedroom_manual_off`:

- **SET on** by the Tap Dial button-1 branch when it turns the group **off** (smart toggle:
  group on → `light.turn_off` + override on; group off → `light.turn_on` + override off).
- **CLEARED off** by: (a) a manual-on — button-1 turning the group on, or any scene button
  (2–4); **or** (b) the morning reset (unconditionally, see below).
- **Leaving the room does NOT clear it** (deliberate — a quick step-out shouldn't blast the
  lights back on when you return).

Consumers: only the **presence-on** automation checks it (won't turn on while override is on).
The **absence-off** automation ignores it (leaving always allows off).

## Morning reset + gentle wake

One automation, two time triggers, day-type matched:

- **06:00 on Mon–Fri**, **07:00 on Sat–Sun**.
- **Always:** clear `input_boolean.bedroom_manual_off` (fixes any stuck override, incl. the
  manual-off-then-left case).
- **If `binary_sensor.aqara_fp300_presence` is on:** gentle wake — `light.turn_on` to ~50% with a
  ~5-minute `transition`, *not* lux-gated (a wake-up is deliberate); AL resumes the curve after.
- **If the room is empty:** clear-only; normal presence handles the next entry.

```yaml
# sketch — full YAML in the plan
trigger:
  - platform: time
    at: "06:00:00"
    id: weekday
  - platform: time
    at: "07:00:00"
    id: weekend
condition:
  - condition: or
    conditions:
      - "{{ trigger.id == 'weekday' and now().weekday() < 5 }}"
      - "{{ trigger.id == 'weekend' and now().weekday() >= 5 }}"
action:
  - service: input_boolean.turn_off
    target: { entity_id: input_boolean.bedroom_manual_off }
  - if: "{{ is_state('binary_sensor.aqara_fp300_presence', 'on') }}"
    then:
      - service: light.turn_on
        target: { entity_id: light.bedroom_lights }
        data: { brightness_pct: 50, transition: 300 }
```

## Illuminance = gate only

Used **only** on the turn-*on* path (`sensor.aqara_fp300_illuminance` below the gate). Never used
to turn lights off — controlling lights from a same-room light sensor closes a feedback loop
(brighten → sensor reads bright → dim → oscillate). Off is driven purely by absence. The wake-on
is also not lux-gated.

## Key automation sketches

```yaml
# Presence on (full YAML in plan)
- id: bedroom_presence_on
  trigger: [{ platform: state, entity_id: binary_sensor.aqara_fp300_presence, to: "on" }]
  condition:
    - { condition: state, entity_id: input_boolean.bedroom_manual_off, state: "off" }
    - { condition: numeric_state, entity_id: sensor.aqara_fp300_illuminance, below: 50 }
  action: [{ service: light.turn_on, target: { entity_id: light.bedroom_lights } }]

# Absence off
- id: bedroom_absence_off
  trigger:
    - { platform: state, entity_id: binary_sensor.aqara_fp300_presence, to: "off", for: "00:01:00" }
  action: [{ service: light.turn_off, target: { entity_id: light.bedroom_lights } }]

# Tap Dial button 1 becomes a smart toggle (inside the existing dial automation's choose)
- conditions: "{{ trigger.payload_json.action == 'button_1_press' }}"
  sequence:
    - if: "{{ is_state('light.bedroom_lights', 'on') }}"
      then:
        - { service: light.turn_off, target: { entity_id: light.bedroom_lights } }
        - { service: input_boolean.turn_on, target: { entity_id: input_boolean.bedroom_manual_off } }
      else:
        - { service: light.turn_on, target: { entity_id: light.bedroom_lights } }
        - { service: input_boolean.turn_off, target: { entity_id: input_boolean.bedroom_manual_off } }
# Scene buttons 2–4 additionally: input_boolean.turn_off (clear override)
```

## Adaptive Lighting config (templated)

```yaml
adaptive_lighting:
  - name: "Bedroom"
    lights: [light.bedroom_lights]
    min_brightness: 1
    max_brightness: 100
    min_color_temp: 2200
    max_color_temp: 4500
    sleep_brightness: 1
    sleep_color_temp: 2200
    take_over_control: true        # stop adapting a light you changed by hand (the dial)
    detect_non_ha_changes: false   # avoid fighting Z2M-reported state
    transition: 45
```

Creates `switch.adaptive_lighting_bedroom`. **Risk/fallback:** AL works most precisely on
individual lights; if it behaves oddly against the HA Light Group, switch `lights:` to the three
bulb entities. Decide during implementation by observing one adaptation cycle.

## Tunables (start here, retune live)

- **Lux gate:** ~50 — calibrate by reading `sensor.aqara_fp300_illuminance` in daylight vs dark.
- **Absence grace:** presence off `for: 1 min` → off; may also raise the FP300
  `number.aqara_fp300_absence_delay_timer` from 10 s if mmWave drops you while still.
- **Wake:** ~5-min fade to ~50%, warm.
- **AL curve:** ~2200 K/1 % late night → ~4500 K/100 % midday, sun-based.

## IaC / storage

- **Templated** into `configuration.yaml.j2`: the `adaptive_lighting:` block + the
  `input_boolean:` helper. Both feed `common_config_changed` (already wired) → editing recreates HA.
- **Automations** added to / modified in `files/automations.yaml` (git, copied — established
  pattern). See `[[ha-automations-templated]]`.
- **Manual prep (sequencing matters):** install "Adaptive Lighting" via HACS and restart HA
  **before** deploying the templated `adaptive_lighting:` config — otherwise HA logs
  "Integration adaptive_lighting not found" and skips it. The `input_boolean` + automations are
  independent of AL and can deploy first.

## Open implementation detail

The 5-minute wake fade may visibly fight AL (AL re-adapts on its interval). Resolve in the plan:
either briefly mark the light `adaptive_lighting.set_manual_control` during the ramp, or rely on
AL's own `transition` and a higher wake target. Verify against one real 6 AM run (or simulate by
firing the automation).

## Testing / acceptance

- Walk into a dark room → lights on (AL-toned for the hour). In daylight → stay off.
- Leave → off after ~1 min.
- Dial-off (bedtime) → stays off despite continued presence; dial-on → on + auto resumes.
- Manual-off then leave then return → stays off until you dial on (per the chosen rule).
- Fire the morning-reset automation manually (Developer Tools → run actions / trigger) →
  override clears; if present, lights fade up.
- AL: over a couple of hours, color temp/brightness track the sun; a dial change is respected.
- `uv run python scripts/probe.py health home-assistant` passes; no "Invalid config" in logs.

## Out of scope (future)

- Air-quality (AirGradient) automations.
- Per-bulb adaptive (vs the group), weekday-specific curves, vacation mode.
- Fixing the Z2M frontend "no devices" websocket display issue (tracked separately).
