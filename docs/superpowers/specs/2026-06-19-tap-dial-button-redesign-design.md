# Hue Tap Dial — 4-Button Redesign

**Date:** 2026-06-19
**Component:** `home-assistant` → `files/automations.yaml` (`bedroom_tap_dial_control`)
**Status:** Design — awaiting review

## Problem

The Hue Tap Dial (RDM002, Z2M name "Tap Dial") drives `light.bedroom_lights`, but its
button assignments grew organically and overlap. Three different buttons turn the lights
**on** in overlapping ways (button 1 = plain toggle-on, button 2 = bright scene, button 4 =
natural/Adaptive-Lighting state), which is confusing: it isn't obvious which button does what,
and button 4's "natural" behavior is *ungated* while the presence automation that calls the
same script *is* lux-gated — so "I press 4 and lights come on, but walking in doesn't light
them" looks like a bug when it's actually two different code paths.

## Goal

A cohesive mapping where **each button owns one clear theme** and there is **no overlap** in
what each control does. Each of the 3 light buttons uses **press + hold** (two functions); one
button is dedicated to the fan; the dial keeps brightness.

## Guiding principle (the lux-gate philosophy)

The recurring confusion was *where the lux gate lives*. This design resolves it:

> **Manual button taps are ungated — they always obey.** The lux gate (don't auto-light a
> room that's already bright) belongs to the **automatic** path (`bedroom_presence_on`) and to
> the explicit **"reset to auto"** hold. Pressing a button is an override; it should never
> refuse because the room is bright.

`press = the everyday/lighter action, hold = the heavier companion` — so holds are learnable
as "more of" their press (auto→boost, nightlight→full bedtime, etc.).

## Final mapping

| # | Button | Press (tap) | Hold |
|---|--------|-------------|------|
| **1** | **Power** | Toggle: **on → Adaptive Lighting** natural look (ungated); **off → off + set manual-off** | **Reset to auto**: clear manual-off + fan-manual, re-sync lights (**lux-gated**) + fan |
| **2** | **Brightness** | **Relax / Cozy** scene (warm ~30%) | **Bright** scene (full) |
| **3** | **Sleep** | **Nightlight** (warm ~3%) | **Bedtime** routine |
| **4** | **Fan** | Fan → **auto** (clear fan-manual + apply) | Fan **boost** 100% |
| **Dial** | — | rotate = brightness ±12% | — |

### Per-action detail

- **B1 press → ON** (`light.bedroom_lights` currently off): `script.bedroom_apply_natural`
  (the existing, ungated dispatcher — gives Adaptive Lighting by default, the nightlight scene
  at night, the wake ramp in the morning window) + `input_boolean.turn_off bedroom_manual_off`.
- **B1 press → OFF** (lights currently on): `light.turn_off` + `input_boolean.turn_on
  bedroom_manual_off`. *(Unchanged from today.)*
- **B1 hold → Reset to auto**: `input_boolean.turn_off [bedroom_manual_off, bedroom_fan_manual]`
  → `script.bedroom_apply_natural_gated` (lights to natural **only if the gate allows**, else
  off) → `if person.daniel == home: script.bedroom_apply_fan`. Does **not** touch
  `bedroom_sleep_mode` / AL sleep mode (those are owned by bedtime / the morning reset).
- **B2 press → Relax**: `scene.turn_on scene.bedroom_relax` + `turn_off bedroom_manual_off`.
- **B2 hold → Bright**: `scene.turn_on scene.bedroom_bright` + `turn_off bedroom_manual_off`.
- **B3 press → Nightlight**: `scene.turn_on scene.bedroom_nightlight` + `turn_off bedroom_manual_off`.
- **B3 hold → Bedtime**: `script.bedroom_bedtime`. *(Existing script; moved here from today's
  button-1 hold.)*
- **B4 press → Fan auto**: `turn_off bedroom_fan_manual` + `script.bedroom_apply_fan`. *(This is
  today's button-3 press, moved to button 4.)*
- **B4 hold → Fan boost**: `turn_on bedroom_fan_manual` + `fan.turn_on` + `fan.set_percentage
  100`. *(Mirrors the existing `BEDROOM_BOOST_FAN` notification action.)*
- **Dial rotate L/R**: `light.turn_on brightness_step_pct: ±12, transition: 0.2`. *(Unchanged.)*

Every light-**on** action clears `bedroom_manual_off` (so presence won't fight it); the only
thing that **sets** `manual_off` is B1's off branch ("off and stay off").

## New components

1. **`scene.bedroom_relax`** (`files/scenes.yaml`) — the missing "awake but winding down"
   mood, distinct from the 3% navigation nightlight and from automatic AL:
   ```yaml
   - id: bedroom_relax
     name: Bedroom Relax
     entities:
       light.bedroom_lights:
         state: "on"
         color_temp_kelvin: 2200   # warm/cozy — tune to taste
         brightness_pct: 30
   ```

2. **`script.bedroom_apply_natural_gated`** (`files/scripts.yaml`) — "apply the natural lighting
   state, but honor the darkness gate." Used by B1-hold reset (and only there). Turns lights
   **off** when the room is bright and outside the wake window — i.e. it produces the same
   outcome the automatic presence path would:
   ```yaml
   bedroom_apply_natural_gated:
     alias: "Bedroom — apply natural lighting, lux-gated"
     mode: restart
     sequence:
       - if: >-
           {% set ws = states('sensor.bedroom_wake_start') %}
           {% set in_window = ws not in ['unknown', 'unavailable'] and timedelta(0) <= (now() - as_datetime(ws)) < timedelta(minutes=15) %}
           {{ in_window or (states('sensor.aqara_fp300_illuminance') | float(9999) < 50) }}
         then:
           - service: script.bedroom_apply_natural
         else:
           - service: light.turn_off
             target:
               entity_id: light.bedroom_lights
   ```

## What stays unchanged (blast-radius safety)

`script.bedroom_apply_natural` is shared by five callers. Three **must not** change behavior —
they deliberately force lights on regardless of brightness:

- `bedroom_presence_on` — already lux-gated in its own *condition*; left as-is.
- `bedroom_morning_reset` (alarm trigger) — calls it inside the wake window (hits the wake
  exception, never the default branch).
- `bedroom_arrive_home` and the `BEDROOM_AWAY_TURN_ON` ("Turn back on") notification action —
  intentionally ungated; gating them would make "Turn back on" turn lights *off* in daylight.

Therefore the gate is **not** baked into the shared script. The new gated behavior lives only
in the new `bedroom_apply_natural_gated` wrapper, called only by the Tap Dial. The gate
expression is duplicated once (presence_on's condition + the wrapper); extracting a shared
`binary_sensor.bedroom_auto_light_allowed` is a possible future DRY refactor, **deferred** to
keep this change surgical and the critical presence path untouched.

## Files to change

| File | Change |
|------|--------|
| `files/scenes.yaml` | + `bedroom_relax` scene |
| `files/scripts.yaml` | + `bedroom_apply_natural_gated` script |
| `files/automations.yaml` | re-map the `bedroom_tap_dial_control` `choose:` branches per the table |
| `roles/.../home-assistant/CLAUDE.md` | update the Tap Dial mapping description |
| `roles/.../home-assistant/SETUP.md` | update the button map |

All four `files/*` are copied verbatim by Ansible (not Jinja-templated) and feed
`common_config_changed`, so an edit recreates HA (~120s). No compose-template change.

## Decisions / open points for review

- **B1-hold reset is lux-gated** (turns lights off in a bright room) — true to "reset to the
  automatic state." It clears manual-off + fan-manual but **not** sleep mode.
- **Relax scene = 2200 K @ 30 %** — starting values, easy to tune after seeing them.
- **Holds on B2/B3/B4** rely on the RDM002 emitting `button_N_hold` (B1 already uses it today);
  verification step confirms each.

## Out of scope

- Tuning the **50-lux threshold** (kept as-is; the presence path is unchanged).
- Changing bedtime/away/morning triggers or the fan curve.
- The shared `binary_sensor` DRY refactor (noted above, deferred).

## Verification plan

1. `uv run python scripts/validate_compose_templates.py` is N/A (no template change); instead
   confirm the three `files/*.yaml` parse as valid YAML.
2. `uv run ansible-playbook ansible/deploy.yml --tags home-assistant` → `probe.py health
   home-assistant` green.
3. Exercise each action and confirm via the recorder DB (`last_triggered` on
   `automation.bedroom_tap_dial_control` + resulting `light.bedroom_lights` / `fan.tower_fan`
   state): all 4 presses, all 4 holds, dial L/R. Confirm B1-press in a bright room turns lights
   on (ungated) and B1-hold reset turns them off (gated).
