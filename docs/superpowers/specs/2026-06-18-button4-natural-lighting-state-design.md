# Button 4 → "natural lighting state" (extensible exceptions over Adaptive Lighting)

**Date:** 2026-06-18
**Status:** Approved — ready for implementation plan
**Area:** Home Assistant bedroom lighting (`ansible/roles/containers/home-assistant`)

## Problem

The Tap Dial's **button 4** currently calls `adaptive_lighting.apply`, which snaps the
`light.bedroom_lights` group to Adaptive Lighting's (AL) raw sun-curve value. That ignores
the **morning wake ramp** (1% → 50% over 15 min) — so pressing button 4 mid-morning jumps to
AL's full daytime brightness, which the operator found too bright.

The operator wants button 4 to mean: **"set the lights to what they would be right now with no
manual intervention"** — accounting for AL *and* any time-based special cases (the morning ramp
today; more in the future). Crucially, **adding future exceptions must be easy** — a new
exception should not require touching AL plumbing or duplicating boilerplate.

## Governor model

Two governors define the "natural" state, with a clear precedence:

1. **Adaptive Lighting** — the *default* governor. Continuous sun-curve **color temp + brightness**.
2. **Time-based exceptions** — *bounded* overrides that take precedence only inside their window.
   The morning wake (06:00 Mon–Fri / 07:00 Sat–Sun, 15-min ramp) is the first one.

Precedence is **ordered exception-first, AL as fallback**: outside every exception window, the
natural state is full AL. An exception only overrides **brightness** (the fade); **color temp
always comes from AL** so the lights stay on the natural warm/cool curve.

The morning wake is bounded to its window: after 06:15 (resp. 07:15) button 4 returns the lights
to AL's value, **not** 50% — the wake does not define brightness for the rest of the day.

## Architecture

Two new Home Assistant **scripts** (in a new templated file `files/scripts.yaml`, wired via
`script: !include scripts.yaml`, copied like `automations.yaml`/`scenes.yaml`):

### `script.bedroom_set_natural_brightness(brightness_pct, transition)` — reusable helper

Centralizes the "apply natural color, then set a caller-chosen brightness" boilerplate so no
exception ever touches AL service syntax. Mode: `restart`.

```yaml
fields:
  brightness_pct: { required: true, description: "Target brightness % (0-100)", example: 24 }
  transition:     { required: true, description: "Fade duration in seconds",    example: 480 }
sequence:
  - service: adaptive_lighting.set_manual_control      # release AL so apply takes effect
    data: { entity_id: switch.bedroom_adaptive_lighting_bedroom, manual_control: false }
  - service: adaptive_lighting.apply                   # natural COLOR temp only
    data:
      entity_id: switch.bedroom_adaptive_lighting_bedroom
      adapt_color: true
      adapt_brightness: false
      turn_on_lights: true
      transition: 1
  - service: light.turn_on                             # caller's brightness over their fade
    target: { entity_id: light.bedroom_lights }
    data: { brightness_pct: "{{ brightness_pct }}", transition: "{{ transition }}" }
```

`light.turn_on` with an explicit brightness re-marks the group as manually controlled (AL
`take_over_control`), so AL pauses brightness adaptation for the duration of the exception —
exactly what we want.

### `script.bedroom_apply_natural` — dispatcher

An ordered `choose:` of exceptions, with full AL (color + brightness) as `default:`. Mode:
`restart`. **This is the single source of truth for "what is natural right now."**

```yaml
sequence:
  - choose:
      # ── Exception: morning wake ramp (06:00 Mon–Fri / 07:00 Sat–Sun, 15 min, 1% → 50%) ──
      - conditions:
          - condition: template
            value_template: >-
              {% set start = today_at('06:00') if now().weekday() < 5 else today_at('07:00') %}
              {{ 0 <= (now() - start).total_seconds() < 900 }}
        sequence:
          - variables:
              wake_start: "{{ today_at('06:00') if now().weekday() < 5 else today_at('07:00') }}"
              wake_elapsed: "{{ (now() - wake_start).total_seconds() }}"
          - service: script.bedroom_set_natural_brightness
            data:
              brightness_pct: "{{ (1 + (50 - 1) * wake_elapsed / 900) | round(0) | int }}"
              transition: "{{ (900 - wake_elapsed) | round(0) | int }}"
    # ── Default: full Adaptive Lighting (natural color + brightness) ──
    default:
      - service: adaptive_lighting.set_manual_control
        data: { entity_id: switch.bedroom_adaptive_lighting_bedroom, manual_control: false }
      - service: adaptive_lighting.apply
        data:
          entity_id: switch.bedroom_adaptive_lighting_bedroom
          turn_on_lights: true
          transition: 1
```

**Unification insight:** the ramp brightness is `1 + (50-1)·elapsed/900` with `transition =
900-elapsed`. At `elapsed = 0` this is **1% over 900 s** — identical to the morning wake's start.
So the morning automation and button 4 call the *same* dispatcher; there is no second copy of the
ramp math. At `elapsed = 420 s` (button 4 at 06:07) it is **~24% over the remaining ~480 s** —
"resume the ramp."

## Caller changes (`files/automations.yaml`)

- **Button 4** (`bedroom_tap_dial_control`, `button_4_press` branch): replace the two AL service
  calls with a single call to `script.bedroom_apply_natural`, keeping the existing
  `input_boolean.turn_off bedroom_manual_off` (clear the override).
- **Morning reset** (`bedroom_morning_reset`): replace the "snap to 1% → delay → fade to 50%"
  block (added earlier today) with a call to `script.bedroom_apply_natural`. At 06:00/07:00 the
  dispatcher yields the identical 1% → 50% / 900 s ramp, so behavior is unchanged but the ramp
  math now lives in exactly one place. Triggers, weekday/weekend condition, and the
  override-clear stay as they are.

## Extending with future exceptions

Drop one block above `default:` in `bedroom_apply_natural`. A **flat** exception hard-codes its
two values; a **ramping** one computes them from `now()` in a `variables:` step first.

```yaml
# ── Exception: evening wind-down (21:00–21:30, hold at 15%) ──
- conditions:
    - condition: template
      value_template: "{{ now() >= today_at('21:00') and now() < today_at('21:30') }}"
  sequence:
    - service: script.bedroom_set_natural_brightness
      data: { brightness_pct: 15, transition: 5 }
```

The contract for every exception: a `(condition, brightness_pct, transition)` triple. The
condition answers "is now in my window?"; the helper handles all AL plumbing.

## Ansible / deploy

- New `files/scripts.yaml` deployed by `ansible.builtin.copy` (NOT `template` — HA `{{ }}` Jinja),
  registered (e.g. `home_assistant_scripts`) and folded into `common_config_changed` so editing it
  recreates the container, like `automations.yaml`/`scenes.yaml`.
- `configuration.yaml.j2` gains `script: !include scripts.yaml`.
- Update `home-assistant/CLAUDE.md` to document the scripts file + the exception pattern.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`, then gate on
  `uv run python scripts/probe.py health home-assistant`.

## Out of scope

- `bedroom_presence_on` keeps its current behavior (turn on, AL tones) — it governs *whether* to
  turn on, not *what value*. Routing walk-in through the dispatcher is a possible later change.
- No change to scenes, the lux gate, or the override state machine.

## Verification

- Reload/restart HA; confirm `script.bedroom_apply_natural` + `script.bedroom_set_natural_brightness`
  exist and HA logs no config errors.
- Button 4 outside any window → lights go to AL sun-curve color + brightness.
- Button 4 during the morning window (manually move the time or test next morning) → lights resume
  the ramp toward 50% over the remaining time, on the natural color.
- Morning reset still fades 1% → 50% over 15 min when present.

## Entities (reference)

- Group: `light.bedroom_lights` · AL master switch: `switch.bedroom_adaptive_lighting_bedroom`
- Tap Dial: MQTT `zigbee2mqtt/0x001788010f0ccda4`, action `button_4_press`
- Presence: `binary_sensor.aqara_fp300_presence` · Override: `input_boolean.bedroom_manual_off`
