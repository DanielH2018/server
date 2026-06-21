# Bedroom Lighting Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bedroom wake-up ramp (currently inverted/too-bright) and smooth five day-long rough edges: presence flap, evening bedtime, manual-stick brightness, ambient-aware auto-on, and slow color tracking.

**Architecture:** All changes live in the `home-assistant` Ansible role. Bug-prone math goes into pure, unit-tested Jinja macros (`custom_templates/lighting.jinja`); the YAML automations/scripts/templates read entities and call the macros. A new per-minute automation drives the sunrise ramp; a new 5-minute automation slow-tracks color from Adaptive Lighting's computed sun curve while brightness stays a one-shot ambient value.

**Tech Stack:** Home Assistant (LSIO container), Adaptive Lighting (HACS), Zigbee Hue bulbs via Z2M, Jinja2 macros, Ansible (`copy` deploys), pytest (`jinja_harness`).

**Spec:** `docs/superpowers/specs/2026-06-21-bedroom-lighting-review-design.md`

## Global Constraints

- **`containers/` is read-only** — edit only under `ansible/roles/containers/home-assistant/`.
- **HA Jinja files are `copy`'d verbatim, not Ansible-templated** — `files/automations.yaml`, `files/scripts.yaml`, `files/scenes.yaml`, `files/templates.yaml`, `files/custom_templates/*.jinja`. Never put HA `{{ }}` in `configuration.yaml.j2` *unless* it has no HA-Jinja (the `input_number:` helper does not). Use plain `#` YAML comments — never Jinja `{# #}` — in the `.jinja`/compose-style files.
- **Tunable math lives in a tested `custom_templates/*.jinja` macro** (numbers in → numbers/bool out); import it from the YAML caller; never inline new math in automations.
- **HA's `round` is banker's rounding** (round-half-to-even) — the test harness (`tests/jinja_harness.py`) mirrors this. Pick test points that avoid exact `.5` midpoints unless you intend the banker's result.
- **Verification alias-slug trap:** an automation's entity_id derives from its `alias` (slugified), NOT its `id`. `id: bedroom_color_track` + `alias: Bedroom color tracking` → `automation.bedroom_color_tracking`. Query by the alias-slug.
- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA ~120s). Config + these files feed `common_config_changed`, so an edit recreates the container.
- **Validate before deploy:** `prek run validate-ha-config --all-files`.
- **Stay on `master`** (no feature branches). Commit explicit paths only (a second session may have unrelated unstaged work — `ansible/.../SETUP.md` is currently dirty and must NOT be committed).
- **Every commit message ends with:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `files/custom_templates/lighting.jinja` | Pure wake/lux/auto-on math | Rewrite `wake_brightness`, `in_wake_window` 15→30, remove `wake_transition`, add `natural_brightness` |
| `tests/test_lighting_macros.py` | Macro unit tests | Update wake tests, drop `wake_transition` tests, add `natural_brightness` tests |
| `templates/configuration.yaml.j2` | HA config + helpers | New `input_number.bedroom_light_expected_color_temp` |
| `files/scripts.yaml` | Light/fan "apply" scripts | `bedroom_apply_wake` (new), `apply_natural` (wake→apply_wake, default→ambient-fill), `set_natural_brightness` (flash fix + arm tracker), `bedroom_bedtime` reorder |
| `files/automations.yaml` | Triggers/gating | `bedroom_wake_ramp` (new), `bedroom_color_track` (new), `absence_off` 1m→5m, `presence_on`+`arrive_home` off-guard, `bedtime_prompt` alarm-anchored |
| `files/templates.yaml` | Template sensors | New `sensor.bedroom_winddown_start` |
| `home-assistant/CLAUDE.md` | Role docs | Update wake/bedtime/presence/auto-on prose |

---

## Task 1: Rewrite the wake macros (window 30 min + gentle-then-steep curve)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`

**Interfaces:**
- Produces: `in_wake_window(elapsed_min) -> "True"/"False"` (window now `0 ≤ e < 30`); `wake_brightness(elapsed_min, sleep_min) -> int %` (1 at e=0, 12 at e=15, 40 at e=30; short-night `0<sleep<360` → 7/24).
- Removes: `wake_transition` (no longer used — the per-minute ramp uses a fixed transition).

- [ ] **Step 1: Update the failing tests** in `tests/test_lighting_macros.py` — replace the `in_wake_window`, `wake_brightness` tests and delete the `_transition` helper + its tests:

```python
def test_in_wake_window_boundaries():
    assert _window(0) == "True"
    assert _window(15) == "True"       # the alarm is now mid-window, not the end
    assert _window(29.99) == "True"
    assert _window(30) == "False"      # window ends 15 min AFTER the alarm
    assert _window(-1) == "False"      # unavailable-sensor sentinel


def test_wake_brightness_curve_endpoints():
    assert _brightness(0, 0) == 1      # 1% at window start (alarm-15)
    assert _brightness(15, 0) == 12    # ~12% at the alarm (gentle pre-alarm)
    assert _brightness(30, 0) == 40    # 40% peak at alarm+15 (the "get up" push)


def test_wake_brightness_is_gentle_then_steep():
    # Post-alarm slope (28% over 15 min) is steeper than pre-alarm (11% over 15 min).
    assert _brightness(22.5, 0) == 26  # 12 + (40-12)*0.5
    assert _brightness(7.5, 0) == 6    # 1 + (12-1)*0.5 = 6.5 -> banker's round -> 6


def test_wake_brightness_short_night_lowers_curve():
    assert _brightness(15, 300) == 7   # 0 < 300 < 360 -> gentler ~7% at the alarm
    assert _brightness(30, 300) == 24  # ...and ~24% peak
    assert _brightness(15, 0) == 12    # unknown/0 sleep -> normal
    assert _brightness(15, 400) == 12  # long night -> normal
```

Also delete the `_transition` helper function and `test_*transition*` test(s) if present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -v`
Expected: FAIL (old macro still returns 50 at e=15, window upper bound still 15).

- [ ] **Step 3: Rewrite the macros** in `custom_templates/lighting.jinja`. Replace `in_wake_window`, `wake_brightness`, and remove `wake_transition`:

```jinja
{# Wake window: the ramp runs for WAKE_WINDOW_MIN minutes total, CENTERED on the alarm —
   it starts at sensor.bedroom_wake_start (alarm - WAKE_PRE_MIN) and ends WAKE_PRE_MIN after the
   alarm. WAKE_WINDOW_MIN (30) MUST equal 2 * the offset in templates.yaml's bedroom_wake_start
   (15). The caller passes minutes since wake_start (or a negative sentinel when unavailable). #}
{%- macro in_wake_window(elapsed_min) -%}
{%- set e = elapsed_min | float(-1) -%}
{{ 0 <= e < 30 }}
{%- endmacro -%}

{# Wake-ramp brightness %: a gentle-then-steep sunrise. 1% at window start (e=0) rises to a mid
   point at the alarm (e=15), then climbs steeper to the peak at e=30 (alarm+15) to actually get you
   up. Sleep-aware: a short night (0 < sleep_min < 360) scales the mid/peak down. Re-evaluated once a
   minute by bedroom_wake_ramp, so a single call returns the level for the current elapsed. #}
{%- macro wake_brightness(elapsed_min, sleep_min) -%}
{%- set e = elapsed_min | float(0) -%}
{%- set s = sleep_min | float(0) -%}
{%- set short = (0 < s < 360) -%}
{%- set start = 1 -%}
{%- set mid = 7 if short else 12 -%}
{%- set peak = 24 if short else 40 -%}
{%- if e <= 15 -%}
{{ (start + (mid - start) * e / 15) | round(0) | int }}
{%- else -%}
{{ (mid + (peak - mid) * (e - 15) / 15) | round(0) | int }}
{%- endif -%}
{%- endmacro -%}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja ansible/roles/containers/home-assistant/tests/test_lighting_macros.py
git commit -m "feat(home-assistant): gentle-then-steep 30-min wake curve (macro)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Add the `natural_brightness` macro (ambient-fill auto-on)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`

**Interfaces:**
- Produces: `natural_brightness(hour, illuminance) -> int %`. Time-of-day base (morning 55 / day 45 / evening 35) scaled by an ambient factor falling `1.0 → 0.2` across `0 → 75` lux, floored at 5%.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_lighting_macros.py`:

```python
def _natural(hour, illuminance):
    return int(render_macro(LIGHT, "natural_brightness", hour, illuminance))


def test_natural_brightness_time_bands_dark_room():
    assert _natural(7, 0) == 55     # morning base, dark room -> factor 1.0
    assert _natural(12, 0) == 45    # daytime base
    assert _natural(20, 0) == 35    # evening base


def test_natural_brightness_dims_with_ambient():
    assert _natural(12, 75) == 9    # at the gate ceiling: 45 * 0.2
    assert _natural(12, 750) == 9   # above the gate: factor clamps at 0.2
    assert _natural(20, 0) > _natural(20, 70)   # brighter room -> dimmer output


def test_natural_brightness_deep_night_falls_back_low():
    assert _natural(3, 0) == 35     # 00:00-05:00 is the nightlight path; fallback base
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -k natural -v`
Expected: FAIL with "natural_brightness" undefined.

- [ ] **Step 3: Add the macro** to `custom_templates/lighting.jinja` (after `auto_light_allowed`):

```jinja
{# Ambient-fill auto-on brightness: the level to turn the lights on at, from the time of day AND the
   current ambient illuminance. Time-of-day base (the level if the room were pitch dark): morning
   (05-09) 55, daytime (09-17) 45, evening (else) 35. Then dimmed by ambient: factor falls linearly
   1.0 -> 0.2 across 0 -> 75 lux (the auto_light_allowed gate ceiling), so a brighter room gets less
   added light. Output floored at 5% so it's always visibly on. Read illuminance while the lights are
   OFF (true ambient — the FP300 is dominated by the bulbs when on). Color comes from Adaptive
   Lighting in the caller; this is brightness only. TUNE the bases / factor here. #}
{%- macro natural_brightness(hour, illuminance) -%}
{%- set h = hour | int(12) -%}
{%- set lux = illuminance | float(0) -%}
{%- set base = 55 if (5 <= h < 9) else (45 if (9 <= h < 17) else 35) -%}
{%- set factor = [[1.0 - 0.8 * lux / 75, 1.0] | min, 0.2] | max -%}
{{ [(base * factor) | round(0) | int, 5] | max }}
{%- endmacro -%}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -v`
Expected: PASS (all lighting macro tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja ansible/roles/containers/home-assistant/tests/test_lighting_macros.py
git commit -m "feat(home-assistant): ambient-fill natural_brightness macro

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Add the color-tracker helper

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (the `input_number:` block, ~line 38, beside `bedroom_fan_expected_level`)

**Interfaces:**
- Produces: `input_number.bedroom_light_expected_color_temp` (Kelvin) — the color the auto path last set; `bedroom_color_track` compares against it.

- [ ] **Step 1: Add the helper** under `input_number:` in `configuration.yaml.j2`:

```yaml
  # The color temperature the auto-lighting path (bedroom_set_natural_brightness) last set, so
  # bedroom_color_track can tell its own auto color from a manual scene/slider pick (expected-value
  # pattern, like bedroom_fan_expected_level).
  bedroom_light_expected_color_temp:
    name: Bedroom light expected color temp
    min: 2000
    max: 6600
    step: 1
    unit_of_measurement: K
    mode: box
```

- [ ] **Step 2: Validate the rendered config**

Run: `prek run validate-ha-config --all-files`
Expected: PASS (YAML syntax + includes OK).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2
git commit -m "feat(home-assistant): expected-color helper for color tracking

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Rework the lighting scripts (wake frame, ambient-fill default, flash fix)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml`

**Interfaces:**
- Consumes: `wake_brightness`, `natural_brightness` (Tasks 1-2); `input_number.bedroom_light_expected_color_temp` (Task 3).
- Produces: `script.bedroom_apply_wake` (sets the current sunrise frame); `script.bedroom_set_natural_brightness(brightness_pct, transition)` (one `light.turn_on` at AL's color + given brightness, arms the color tracker); `script.bedroom_apply_natural` default now uses ambient-fill.

- [ ] **Step 1: Replace `bedroom_set_natural_brightness`** with the flash-free, tracker-arming version:

```yaml
bedroom_set_natural_brightness:
  alias: "Bedroom — set brightness with natural color"
  description: >-
    Turn the bedroom lights on at a caller-supplied brightness and Adaptive Lighting's CURRENT
    natural color temperature, in a single light.turn_on (no AL turn-on flash). Arms the color
    tracker (input_number.bedroom_light_expected_color_temp) so bedroom_color_track treats this
    color as "auto". The explicit brightness marks the group manually controlled (AL take_over).
  mode: restart
  fields:
    brightness_pct:
      description: Target brightness percent (0-100).
      required: true
      example: 24
    transition:
      description: Fade duration in seconds.
      required: true
      example: 2
  sequence:
    # AL keeps COMPUTING its sun-curve color even while taken over, so its color_temp_kelvin
    # attribute is the right "time of day" color to apply. Fallback 2700K if unavailable.
    - variables:
        al_ct: "{{ state_attr('switch.bedroom_adaptive_lighting_bedroom', 'color_temp_kelvin') | int(2700) }}"
    - service: light.turn_on
      target:
        entity_id: light.bedroom_lights
      data:
        brightness_pct: "{{ brightness_pct | int }}"
        color_temp_kelvin: "{{ al_ct }}"
        transition: "{{ transition | int }}"
    # Arm the color tracker: record the color we just set so bedroom_color_track recognizes it as auto.
    - service: input_number.set_value
      target:
        entity_id: input_number.bedroom_light_expected_color_temp
      data:
        value: "{{ al_ct }}"
```

- [ ] **Step 2: Add `bedroom_apply_wake`** (new script, place after `bedroom_set_natural_brightness`):

```yaml
# Wake frame: set the lights to the current sunrise-ramp brightness for NOW (warm 2200K), computed
# from sensor.bedroom_wake_start via the wake_brightness macro. bedroom_wake_ramp calls this every
# minute through the 30-min window; bedroom_apply_natural's wake exception also calls it so a mid-ramp
# resume (presence / Tap-Dial) recomputes the right frame. No-op when no morning alarm is set.
bedroom_apply_wake:
  alias: "Bedroom — apply wake ramp frame"
  mode: restart
  sequence:
    - variables:
        ws: "{{ states('sensor.bedroom_wake_start') }}"
    - condition: template
      value_template: "{{ ws not in ['unknown', 'unavailable'] }}"
    - variables:
        wake_elapsed_min: "{{ (now() - as_datetime(ws)).total_seconds() / 60 }}"
        sleep_min: "{{ states('sensor.pixel_9_pro_sleep_duration') | float(0) }}"
        target: "{% from 'lighting.jinja' import wake_brightness %}{{ wake_brightness(wake_elapsed_min, sleep_min) | int }}"
    - service: light.turn_on
      target:
        entity_id: light.bedroom_lights
      data:
        brightness_pct: "{{ target }}"
        color_temp_kelvin: 2200
        transition: 60
```

- [ ] **Step 3: Update `bedroom_apply_natural`** — the wake exception calls `apply_wake`, and the `default:` uses ambient-fill. Replace the wake-exception `sequence:` and the `default:`:

Wake exception `sequence:` (the block under the `in_wake_window` condition) becomes:

```yaml
          sequence:
            - service: script.bedroom_apply_wake
```

`default:` becomes:

```yaml
      # Default: ambient-fill brightness (time of day + current ambient lux) on AL's natural color.
      # Read illuminance while the lights are off -> true ambient. set_natural_brightness applies the
      # color and arms the color tracker; bedroom_color_track then drifts color with the sun.
      default:
        - service: script.bedroom_set_natural_brightness
          data:
            brightness_pct: "{% from 'lighting.jinja' import natural_brightness %}{{ natural_brightness(now().hour, states('sensor.aqara_fp300_illuminance')) | int }}"
            transition: 2
```

- [ ] **Step 4: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml
git commit -m "feat(home-assistant): wake frame + ambient-fill auto-on + flash fix

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Smooth the bedtime fade (reorder so AL can't pre-dim)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (`bedroom_bedtime`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `script.bedroom_bedtime` with a genuinely gradual 15-min fade.

- [ ] **Step 1: Reorder `bedroom_bedtime`'s sequence** — mark AL hands-off BEFORE enabling AL sleep mode, so the only brightness change is the scene fade. Replace the `sequence:`:

```yaml
  sequence:
    - service: input_boolean.turn_on
      target:
        entity_id: input_boolean.bedroom_sleep_mode
    # Mark AL hands-off FIRST so enabling AL sleep mode below can't force a ~45s pre-dim to
    # sleep_brightness before the fade — the fade must be the only brightness change.
    - service: adaptive_lighting.set_manual_control
      data:
        entity_id: switch.bedroom_adaptive_lighting_bedroom
        manual_control: true
    # The ONLY brightness change: a smooth 15-min fade from the current level to amber 3%. The bulb
    # ramps internally (single Zigbee command), so an HA/Z2M restart mid-fade doesn't abort it.
    - service: scene.turn_on
      target:
        entity_id: scene.bedroom_nightlight
      data:
        transition: 900
    # Set AL's sleep target (warm/dim) for after the morning reset releases control. Harmless now —
    # AL is already hands-off, so this can't move the lights.
    - service: switch.turn_on
      target:
        entity_id: switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
    # Re-apply the fan so the Low sleep cap takes effect now (gated on the manual-fan override).
    - if: "{{ is_state('input_boolean.bedroom_fan_manual', 'off') }}"
      then:
        - service: script.bedroom_apply_fan
```

- [ ] **Step 2: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml
git commit -m "fix(home-assistant): smooth bedtime fade (AL hands-off before sleep mode)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Per-minute wake ramp automation

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

**Interfaces:**
- Consumes: `script.bedroom_apply_wake` (Task 4), `in_wake_window` (Task 1).
- Produces: `automation.bedroom_wake_ramp` (alias-slug matches the id).

- [ ] **Step 1: Add the automation** (place after `bedroom_morning_reset`):

```yaml
# Per-minute sunrise ramp. While inside the 30-min wake window, set the current ramp frame
# (script.bedroom_apply_wake) each minute — stateless ticks resume cleanly after an HA restart. On
# the tick where the window just ended (elapsed 30..31), hand the lights back to Adaptive Lighting
# for the day (apply_natural default = ambient-fill). Gated on home + not manual-off; the wake math
# (window + curve) lives in lighting.jinja.
- id: bedroom_wake_ramp
  alias: Bedroom wake ramp
  description: Drive the sunrise ramp once a minute through the wake window; hand back to AL at window end.
  mode: single
  trigger:
    - platform: time_pattern
      minutes: "/1"
  condition:
    - condition: state
      entity_id: person.daniel
      state: "home"
    - condition: state
      entity_id: input_boolean.bedroom_manual_off
      state: "off"
  action:
    - variables:
        ws: "{{ states('sensor.bedroom_wake_start') }}"
        elapsed: "{{ (now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1 }}"
        in_window: "{% from 'lighting.jinja' import in_wake_window %}{{ in_wake_window(elapsed) | bool }}"
    - choose:
        - conditions: "{{ in_window }}"
          sequence:
            - service: script.bedroom_apply_wake
        # Window just ended this minute -> release to Adaptive Lighting (ambient-fill) for the day.
        - conditions: "{{ elapsed >= 30 and elapsed < 31 }}"
          sequence:
            - service: script.bedroom_apply_natural
```

- [ ] **Step 2: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(home-assistant): per-minute wake ramp automation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Slow color-tracking automation (item 5b)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

**Interfaces:**
- Consumes: `input_number.bedroom_light_expected_color_temp` (Task 3), armed by `set_natural_brightness` (Task 4); `in_wake_window` (Task 1).
- Produces: `automation.bedroom_color_tracking` (NOTE the alias-slug: id `bedroom_color_track`, entity `automation.bedroom_color_tracking`).

- [ ] **Step 1: Add the automation** (place after `bedroom_wake_ramp`):

```yaml
# Slow color tracking: while the lights are in AUTO mode, drift their color every 5 min toward
# Adaptive Lighting's CURRENT sun-curve color (read off the AL switch attribute — AL computes it even
# while taken over). Color only (no brightness arg), so the one-shot ambient brightness is untouched
# and there's no lux feedback loop. "Auto" = the live color is still within ~150K of what the auto
# path last set (input_number.bedroom_light_expected_color_temp); a manual scene/slider/RGB pick fails
# that and pauses tracking until a reset/fresh turn-on re-arms it. Gated off during the wake ramp
# (fixed warm), sleep/00:00-05:00 (night warmth), away, and lights-off.
- id: bedroom_color_track
  alias: Bedroom color tracking
  description: While in auto, slowly follow Adaptive Lighting's sun-curve color without touching brightness.
  mode: single
  trigger:
    - platform: time_pattern
      minutes: "/5"
  condition:
    - condition: state
      entity_id: light.bedroom_lights
      state: "on"
    - condition: state
      entity_id: person.daniel
      state: "home"
    - condition: state
      entity_id: input_boolean.bedroom_manual_off
      state: "off"
    - condition: state
      entity_id: input_boolean.bedroom_sleep_mode
      state: "off"
    # Not the deep-night warmth window (00:00-05:00).
    - condition: template
      value_template: "{{ now().hour >= 5 }}"
    # Not during the wake ramp (it owns a fixed warm color).
    - condition: template
      value_template: >-
        {% set ws = states('sensor.bedroom_wake_start') %}
        {% from 'lighting.jinja' import in_wake_window %}
        {{ not (in_wake_window((now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1) | bool) }}
    # Only when the bulb is in color-temp mode (a manual RGB pick switches color_mode to xy -> skip).
    - condition: template
      value_template: "{{ state_attr('light.bedroom_lights', 'color_mode') == 'color_temp' }}"
    # Still "auto": live color within ~150K of what the auto path last set (else a manual color is active).
    - condition: template
      value_template: >-
        {{ (state_attr('light.bedroom_lights', 'color_temp_kelvin') | int(0)
            - states('input_number.bedroom_light_expected_color_temp') | int(0)) | abs <= 150 }}
  action:
    - variables:
        al_ct: "{{ state_attr('switch.bedroom_adaptive_lighting_bedroom', 'color_temp_kelvin') | int(0) }}"
    # Skip if AL's color is unreadable (0) — don't slam the lights to a bogus value.
    - condition: template
      value_template: "{{ al_ct > 0 }}"
    - service: light.turn_on
      target:
        entity_id: light.bedroom_lights
      data:
        color_temp_kelvin: "{{ al_ct }}"
        transition: 290
    - service: input_number.set_value
      target:
        entity_id: input_number.bedroom_light_expected_color_temp
      data:
        value: "{{ al_ct }}"
```

- [ ] **Step 2: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(home-assistant): slow color tracking while in auto (item 5b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Presence-flap fix + manual-sticks guards

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (`bedroom_absence_off`, `bedroom_presence_on`, `bedroom_arrive_home`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `presence_on` and `arrive_home` only turn lights from off→on (never re-stomp an on light); `absence_off` requires 5 min empty.

- [ ] **Step 1: Lengthen `bedroom_absence_off`** — change its trigger `for:`:

```yaml
  trigger:
    - platform: state
      entity_id: binary_sensor.aqara_fp300_presence
      to: "off"
      for: "00:05:00"
```

- [ ] **Step 2: Guard `bedroom_presence_on`** — add a "lights currently off" condition so it never overrides a manually-set on light (also hardens the flap fix). Add to its `condition:` list:

```yaml
    # Only ever turn lights from off -> on. Never re-stomp an already-on light: that's what lets a
    # manual dim stick (the dusk-lux trigger used to fire apply_natural and slam back to AL ~100%),
    # and it preserves the "light a dark room as dusk falls" purpose (lights are off in that case).
    - condition: state
      entity_id: light.bedroom_lights
      state: "off"
```

- [ ] **Step 3: Guard `bedroom_arrive_home`** — add the same off-check to its light re-check `if:`. Replace that `if:` condition:

```yaml
    - if: >-
        {{ is_state('binary_sensor.aqara_fp300_presence', 'on')
           and is_state('input_boolean.bedroom_manual_off', 'off')
           and is_state('light.bedroom_lights', 'off') }}
      then:
        - service: script.bedroom_apply_natural
```

- [ ] **Step 4: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "fix(home-assistant): de-flap absence-off + manual brightness sticks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Alarm-anchored bedtime prompt

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/templates.yaml` (new sensor)
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (`bedroom_bedtime_prompt`)

**Interfaces:**
- Produces: `sensor.bedroom_winddown_start` (= next morning alarm − 8h); `bedroom_bedtime_prompt` fires there, with a 22:30 fallback only on no-alarm nights.

- [ ] **Step 1: Add the wind-down sensor** to `files/templates.yaml` (after the `bedroom_wake_start` sensor block):

```yaml
# Wind-down anchor: the bedtime PROMPT fires 8 h before the next MORNING alarm (target sleep ~8h),
# so it tracks the real schedule instead of a fixed clock. Same morning-alarm availability guard as
# bedroom_wake_start (local hour 03:00-11:00), so a nap/evening alarm never arms it. For a 6:00am
# alarm this resolves to 10:00pm. No alarm -> unavailable, and the prompt's 22:30 fallback covers it.
- sensor:
    - name: "Bedroom winddown start"
      unique_id: bedroom_winddown_start
      device_class: timestamp
      availability: >-
        {% set na = states('sensor.pixel_watch_3_next_alarm') %}
        {{ na not in ['unknown', 'unavailable', 'none', ''] and 3 <= (as_datetime(na) | as_local).hour < 11 }}
      state: >-
        {{ (as_datetime(states('sensor.pixel_watch_3_next_alarm')) - timedelta(hours=8)).isoformat() }}
```

- [ ] **Step 2: Re-anchor `bedroom_bedtime_prompt`** — replace its `trigger:` and `condition:`:

```yaml
  trigger:
    - platform: time
      at: sensor.bedroom_winddown_start
      id: dynamic
    - platform: time
      at: "22:30:00"
      id: fallback
  condition:
    - condition: state
      entity_id: binary_sensor.aqara_fp300_presence
      state: "on"
    - condition: state
      entity_id: input_boolean.bedroom_sleep_mode
      state: "off"
    - condition: state
      entity_id: person.daniel
      state: "home"
    # The fixed 22:30 fallback only fires on no-alarm nights (sensor unavailable) -> no double prompt
    # on a night that already has an alarm-anchored dynamic trigger.
    - condition: template
      value_template: "{{ trigger.id == 'dynamic' or states('sensor.bedroom_winddown_start') in ['unknown', 'unavailable'] }}"
```

(The `action:` — the `Start now` actionable notify — is unchanged.)

- [ ] **Step 3: Validate**

Run: `prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/templates.yaml ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(home-assistant): alarm-anchored bedtime prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Update role docs

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

**Interfaces:** none.

- [ ] **Step 1: Update the prose** to match the new behavior. Edit these documented claims:
  - Wake ramp: "1%→50% over 15 min ending at the alarm" → **"gentle-then-steep 1%→~12% (alarm)→40%, 30-min window centered on the alarm (alarm−15 → alarm+15), driven per-minute by `automation.bedroom_wake_ramp` (warm 2200K), short-night ~0.6×"**. Note `wake_transition` is gone and the ramp no longer flashes (no `adaptive_lighting.apply turn_on_lights`).
  - `bedroom_apply_natural` default: "full Adaptive Lighting (color + brightness)" → **"AL color + ambient-fill brightness (`natural_brightness(hour, illuminance)`); AL is now a color source at turn-on, with `automation.bedroom_color_tracking` slow-drifting color thereafter (item 5b)"**.
  - `bedroom_presence_on` / `bedroom_arrive_home`: note the new **`light == off` guard** (manual brightness sticks; presence only turns lights off→on).
  - `bedroom_absence_off`: **1 min → 5 min** (FP300 false-absence de-flap).
  - Bedtime: note the **reorder** (AL `set_manual_control: true` before sleep mode) for a genuinely gradual fade.
  - Bedtime prompt: now **alarm-anchored** via `sensor.bedroom_winddown_start` (alarm−8h) + 22:30 no-alarm fallback.
  - Add `input_number.bedroom_light_expected_color_temp` to the helper notes.

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "docs(home-assistant): update lighting prose for the review changes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Deploy and verify live

**Files:** none (deploy + verification only).

- [ ] **Step 1: Run the full unit suite + config validation**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests -v && prek run validate-ha-config --all-files`
Expected: PASS.

- [ ] **Step 2: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: play completes; HA recreates.

- [ ] **Step 3: Gate on container health**

Run: `uv run python scripts/probe.py health home-assistant`
Expected: exit 0 (running + healthy).

- [ ] **Step 4: Confirm new automations + sensor + helper loaded** (mind the alias-slugs)

Run:
```bash
uv run python scripts/probe.py ha automation bedroom_wake_ramp
uv run python scripts/probe.py ha automation bedroom_color_tracking
uv run python scripts/probe.py ha state sensor.bedroom_winddown_start
uv run python scripts/probe.py ha state input_number.bedroom_light_expected_color_temp
```
Expected: each resolves (automations present/last_triggered visible; `bedroom_winddown_start` is a timestamp or `unavailable` if no morning alarm is set; helper is a number).

- [ ] **Step 5: Verify item 4 root cause + that manual now sticks.** Manually dim `light.bedroom_lights` (dashboard slider or Tap-Dial), wait 2+ min, then read the logbook:

```bash
uv run python scripts/probe.py ha get "logbook/$(date -u -d '5 min ago' +%Y-%m-%dT%H:%M:%S)+00:00?entity=light.bedroom_lights"
```
Expected: the manual level holds — no `Bedroom presence on` / `call_service` snap-back. **If a snap-back still appears AND its context is `al:` (Adaptive Lighting), the culprit is AL re-applying, not `presence_on`** — implement the spec's fallback: add `input_boolean.bedroom_light_manual` (set by a `bedroom_light_manual_detect` automation on a user change; gate `presence_on` on it; clear it on B1-hold / morning reset / absence-off), mirroring `bedroom_fan_manual`. Otherwise the `presence_on` off-guard (Task 8) is sufficient.

- [ ] **Step 6: Verify the bedtime fade is gradual.** Trigger `script.bedroom_bedtime` (Tap-Dial B3 hold, or Developer Tools → call the service), watch the bulbs: expect a smooth ~15-min descent to amber, **not** a ~45s crash to near-off. (If a fast pre-dim still occurs, drop the `switch.turn_on ... sleep_mode` line from `bedroom_bedtime` — AL's sleep-mode-on apply is overriding manual_control — and redeploy.)

- [ ] **Step 7: Verify the wake ramp live.** Set a watch alarm ~16 min out (so `wake_start` = now+1 min, a morning hour). Within a minute confirm the lights come on dim (~1%) and **rise** over the next minutes (read `light.0x001788010ff4ac53` brightness history); confirm they do NOT pop bright at the start. Then clear the test alarm.

```bash
uv run python scripts/probe.py ha state sensor.bedroom_wake_start
uv run python scripts/probe.py ha get "history/period/$(date -u -d '5 min ago' +%Y-%m-%dT%H:%M:%S)+00:00?filter_entity_id=light.0x001788010ff4ac53"
```
Expected: brightness increasing over successive samples.

- [ ] **Step 8: Verify presence flap is gone.** Over the next active period, re-run the 18h logbook tally and confirm the `absence off → presence on` cycling no longer appears while home:

```bash
uv run python scripts/probe.py ha get "logbook/$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%S)+00:00?entity=light.bedroom_lights"
```
Expected: no rapid absence→presence relight cycles.

- [ ] **Step 9: Final commit** (only if any verification-driven fixes were made in Steps 5–6; otherwise nothing to commit). Use an appropriate message with the `Co-Authored-By` trailer.

---

## Self-review notes

- **Spec coverage:** wake (T1,4,6), presence flap (T8), bedtime abruptness (T5), bedtime timing (T9), manual-sticks (T8, +T11 contingency), ambient-fill auto-on (T2,3,4), slow color tracking (T3,4,7), docs (T10), test/deploy/verify (T11). All spec sections map to a task.
- **Brightness vs color decoupling:** brightness = one-shot ambient (`natural_brightness`, set once per turn-on); color = continuous via `bedroom_color_track`. The expected-color helper (T3) is written by `set_natural_brightness` (T4) and read by `color_track` (T7) — names consistent.
- **Alias-slug:** `bedroom_color_track` → `automation.bedroom_color_tracking` (flagged in T7 + T11).
