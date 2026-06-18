# Presence + Adaptive Bedroom Lighting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. NOTE: this plan has human-in-the-loop steps (installing a HACS integration, physically walking in/out of the room, pressing the dial). Execute inline, collaboratively.

**Goal:** Make the bedroom lights automatic — FP300 presence on/off, an Adaptive-Lighting sun curve, an illuminance gate, and a Tap-Dial manual-off override with a weekday/weekend morning reset + gentle wake.

**Architecture:** Adaptive Lighting (HACS) owns color/brightness while lights are on; our git-tracked automations + one `input_boolean` override own whether they're on. Config (`adaptive_lighting:`, `input_boolean:`) is templated into `configuration.yaml.j2`; automations live in `files/automations.yaml` (deployed by `copy`). Built in two phases: A = presence/override/morning (no AL), B = Adaptive Lighting.

**Tech Stack:** Home Assistant, Adaptive Lighting (HACS), Zigbee2MQTT + Mosquitto, Aqara FP300, Ansible.

**Spec:** `docs/superpowers/specs/2026-06-18-presence-adaptive-bedroom-lighting-design.md`

## Global Constraints

- Entity IDs (verified): group `light.bedroom_lights`; presence `binary_sensor.aqara_fp300_presence`; illuminance `sensor.aqara_fp300_illuminance`; override `input_boolean.bedroom_manual_off`; AL switch `switch.adaptive_lighting_bedroom`.
- Tap Dial publishes JSON to `zigbee2mqtt/0x001788010f0ccda4`; read `trigger.payload_json.action`.
- Automations are git-tracked static files deployed via `ansible.builtin.copy` (NOT `template` — HA `{{ }}` would collide with Ansible Jinja). `configuration.yaml` is templated; both feed `common_config_changed` (already wired) so an edit recreates HA (~120 s).
- Lux gate ≈ 50 (calibrate). Absence grace `for: "00:01:00"`. Wake = `brightness_pct: 50, transition: 300`. Morning times: 06:00 Mon–Fri, 07:00 Sat–Sun.
- `state:`/`to:` boolean values quoted (`"on"`/`"off"`) for yamllint.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`. Validate first with `prek run --all-files` then `--check`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` | Add `input_boolean:` (Task 1) and `adaptive_lighting:` (Task 3) | **Modify** |
| `ansible/roles/containers/home-assistant/files/automations.yaml` | Modified dial automation + presence on/off (Task 1) + morning reset (Task 2) | **Modify** |
| `ansible/roles/containers/home-assistant/CLAUDE.md` | Note AL is a HACS dep (Task 3) | **Modify** |

---

## Task 1: Override helper + presence on/off + dial override (Phase A core)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2`
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

**Interfaces:**
- Produces: `input_boolean.bedroom_manual_off` (override flag, consumed by Tasks 1–2);
  automations `bedroom_presence_on`, `bedroom_absence_off`; modified `bedroom_tap_dial_control`.

- [ ] **Step 1: Add the override helper to `configuration.yaml.j2`**

Insert after the `automation:`/`scene:` include block:
```yaml
# Helper: manual-off override. When on, presence will NOT auto-turn-on the bedroom lights.
# Set by the Tap Dial when you turn the lights off; cleared on manual-on or the morning reset.
input_boolean:
  bedroom_manual_off:
    name: Bedroom manual off override
    icon: mdi:lightbulb-off
```

- [ ] **Step 2: Replace the dial automation + add presence automations in `files/automations.yaml`**

Replace the single `bedroom_tap_dial_control` entry (keep the file's header comment) so the file's automation list is exactly:
```yaml
- id: bedroom_tap_dial_control
  alias: Bedroom Tap Dial control
  description: Hue Tap Dial (RDM002, 0x001788010f0ccda4) controls the Bedroom Lights group.
  mode: queued
  max: 10
  trigger:
    - platform: mqtt
      topic: zigbee2mqtt/0x001788010f0ccda4
  action:
    - choose:
        - conditions: "{{ trigger.payload_json.action == 'button_1_press' }}"
          sequence:
            - if: "{{ is_state('light.bedroom_lights', 'on') }}"
              then:
                - service: light.turn_off
                  target:
                    entity_id: light.bedroom_lights
                - service: input_boolean.turn_on
                  target:
                    entity_id: input_boolean.bedroom_manual_off
              else:
                - service: light.turn_on
                  target:
                    entity_id: light.bedroom_lights
                - service: input_boolean.turn_off
                  target:
                    entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ trigger.payload_json.action == 'button_2_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_bright
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ trigger.payload_json.action == 'button_3_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_relax
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ trigger.payload_json.action == 'button_4_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_nightlight
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ 'dial_rotate_right' in (trigger.payload_json.action | string) }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: 12
                transition: 0.2
        - conditions: "{{ 'dial_rotate_left' in (trigger.payload_json.action | string) }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: -12
                transition: 0.2

- id: bedroom_presence_on
  alias: Bedroom presence on
  description: Turn on the bedroom lights when the room becomes occupied and is dim, unless overridden.
  mode: single
  trigger:
    - platform: state
      entity_id: binary_sensor.aqara_fp300_presence
      to: "on"
  condition:
    - condition: state
      entity_id: input_boolean.bedroom_manual_off
      state: "off"
    - condition: numeric_state
      entity_id: sensor.aqara_fp300_illuminance
      below: 50
  action:
    - service: light.turn_on
      target:
        entity_id: light.bedroom_lights

- id: bedroom_absence_off
  alias: Bedroom absence off
  description: Turn off the bedroom lights after the room has been empty for a minute.
  mode: single
  trigger:
    - platform: state
      entity_id: binary_sensor.aqara_fp300_presence
      to: "off"
      for: "00:01:00"
  action:
    - service: light.turn_off
      target:
        entity_id: light.bedroom_lights
```

- [ ] **Step 3: Validate YAML + templates**

Run: `cd /home/ubuntu/server && python3 -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml'))" && prek run --all-files 2>&1 | tail -12`
Expected: YAML parses; all prek hooks Pass.

- [ ] **Step 4: Dry-run + deploy**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" --check 2>&1 | tail -5`
Expected: config tasks changed, `failed=0`.
Then: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" 2>&1 | tail -5`
Expected: HA recreated, `failed=0`.

- [ ] **Step 5: Confirm health + entities**

Run: `cd /home/ubuntu/server && uv run python scripts/probe.py health home-assistant`
Expected (after ~90 s): running + healthy. Then in HA → Developer Tools → States, confirm
`input_boolean.bedroom_manual_off` exists and `automation.bedroom_presence_on` /
`automation.bedroom_absence_off` are loaded (no "Invalid config" in `docker logs home-assistant`).

- [ ] **Step 6: Hardware test (user)**

1. Lights off, room dark (lux < 50): walk in → lights come on.
2. Leave the room → after ~1 min, lights off.
3. With lights on, press dial **button 1** → off; confirm `input_boolean.bedroom_manual_off`
   flips **on** (Developer Tools → States). Walk around → lights stay off (override holds).
4. Press dial **button 1** again → on; override flips **off**.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/server
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 \
        ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "$(cat <<'EOF'
home-assistant: bedroom presence on/off + Tap Dial manual-off override

FP300 presence turns the Bedroom Lights group on (when dim + not
overridden) and off after 1 min empty. Tap Dial button 1 is now a smart
toggle that sets/clears input_boolean.bedroom_manual_off; scene buttons
clear it. Override suppresses presence auto-on.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Morning reset + gentle wake

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

**Interfaces:**
- Consumes: `input_boolean.bedroom_manual_off`, `binary_sensor.aqara_fp300_presence`,
  `light.bedroom_lights` (from Task 1).
- Produces: automation `bedroom_morning_reset`.

- [ ] **Step 1: Append the morning-reset automation to `files/automations.yaml`** (after `bedroom_absence_off`)

```yaml
- id: bedroom_morning_reset
  alias: Bedroom morning reset and wake
  description: 06:00 Mon-Fri / 07:00 Sat-Sun — clear the manual-off override; gently wake if present.
  mode: single
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
      target:
        entity_id: input_boolean.bedroom_manual_off
    - if: "{{ is_state('binary_sensor.aqara_fp300_presence', 'on') }}"
      then:
        - service: light.turn_on
          target:
            entity_id: light.bedroom_lights
          data:
            brightness_pct: 50
            transition: 300
```

- [ ] **Step 2: Validate + deploy**

Run: `cd /home/ubuntu/server && python3 -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml'))" && prek run --all-files 2>&1 | tail -6`
Expected: parses; hooks Pass.
Then deploy + health as in Task 1 Steps 4–5. Confirm `automation.bedroom_morning_reset` loaded.

- [ ] **Step 3: Functional test (no waiting until 6 AM)**

In HA → Settings → Automations → "Bedroom morning reset and wake" → ⋮ → **Run actions** (this
runs the `action:` block, bypassing the time/condition). With the override **on** and you
**present**: verify the override clears AND the lights fade up over ~5 min. With the room empty:
verify it only clears the override.

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/server
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "$(cat <<'EOF'
home-assistant: bedroom morning reset + gentle wake

06:00 Mon-Fri / 07:00 Sat-Sun: always clear the manual-off override;
if present, fade the lights up over 5 min. Fixes a stuck override and
gives a weekday/weekend wake light.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Adaptive Lighting (Phase B)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2`
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

**Interfaces:**
- Consumes: `light.bedroom_lights`.
- Produces: `switch.adaptive_lighting_bedroom`.

- [ ] **Step 1: Install Adaptive Lighting via HACS (user, one-time)**

In HA → HACS → search "Adaptive Lighting" (by basnijholt) → Download → **Restart Home Assistant**
(Settings → System → Restart). This drops `custom_components/adaptive_lighting/` into config
(Kopia-backed, not templated — same pattern as Dreo). **Must precede Step 2**, or HA logs
"Integration 'adaptive_lighting' not found" and skips the config.

- [ ] **Step 2: Add the `adaptive_lighting:` block to `configuration.yaml.j2`** (after the `input_boolean:` block)

```yaml
# Adaptive Lighting (HACS integration) — sun-tracking color temp + brightness for the bedroom
# group while it is on. Install via HACS first (custom_components/, like dreo) or HA logs
# "integration not found". Creates switch.adaptive_lighting_bedroom.
adaptive_lighting:
  - name: "Bedroom"
    lights:
      - light.bedroom_lights
    min_brightness: 1
    max_brightness: 100
    min_color_temp: 2200
    max_color_temp: 4500
    sleep_brightness: 1
    sleep_color_temp: 2200
    take_over_control: true
    detect_non_ha_changes: false
    transition: 45
```

- [ ] **Step 3: Note the dependency in the role `CLAUDE.md`** (add a bullet under `## Notable`)

```markdown
- **Adaptive Lighting is a HACS dependency (since 2026-06-18).** `configuration.yaml` declares
  `adaptive_lighting:` for the bedroom group; the integration code installs via HACS into
  `custom_components/adaptive_lighting/` (Kopia-backed, not templated — like `dreo`). Install
  it via HACS BEFORE deploying, or HA logs "integration not found" and skips the block.
```

- [ ] **Step 4: Validate + deploy + health** (as Task 1 Steps 3–5)

Confirm `switch.adaptive_lighting_bedroom` exists (Developer Tools → States) and there is no
"adaptive_lighting" error in `docker logs home-assistant`.

- [ ] **Step 5: Verify one adaptation cycle**

Turn the group on (dial or presence). Within ~1–2 min, AL should set its color temp + brightness
for the current time (watch `light.bedroom_lights` attributes change in Developer Tools). Change
brightness with the dial → AL should stop adjusting (take-over-control). **Fallback:** if AL
behaves oddly against the group, change `lights:` to the three bulb entities and redeploy (note
their entity IDs from Developer Tools first).

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/server
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 \
        ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "$(cat <<'EOF'
home-assistant: adaptive lighting for the bedroom group

Declares adaptive_lighting: (HACS integration) over light.bedroom_lights
— sun-tracking color temp + brightness while on, take-over-control so the
dial wins. Integration installed via HACS (custom_components/, like dreo).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Calibrate + accept

**No file changes unless tuning is needed.**

- [ ] **Step 1: Calibrate the lux gate**

Read `sensor.aqara_fp300_illuminance` (Developer Tools → States) at three times: bright daylight,
dusk, and fully dark with lights off. Pick a gate just above the "fully dark" reading and below
dusk. If ≠ 50, edit the `below:` value in `bedroom_presence_on` (`files/automations.yaml`),
redeploy, and commit.

- [ ] **Step 2: Tune absence flicker (only if observed)**

If lights drop while you sit still, raise `number.aqara_fp300_absence_delay_timer` (Developer
Tools or the FP300 device page) from 10 s to ~30–60 s. This is a device setting, not a repo change.

- [ ] **Step 3: Full acceptance pass** (spec's testing section)

Dark walk-in → on; daylight walk-in → stays off; leave → off after 1 min; dial-off → stays off
despite presence; dial-on → on + auto; manual-off→leave→return → stays off until dial-on; run the
morning automation → clears + wakes if present; AL tracks the sun and respects the dial.
Final: `uv run python scripts/probe.py health home-assistant` passes.

---

## Self-Review

- **Spec coverage:** presence on + gate → Task 1 (`bedroom_presence_on`); absence off → Task 1
  (`bedroom_absence_off`); override set/clear + smart button-1 + scene buttons → Task 1 (dial
  automation); morning reset + wake (weekday/weekend) → Task 2; Adaptive Lighting curve + config
  + HACS dep → Task 3; lux/absence calibration → Task 4. All spec sections covered.
- **Sequencing:** AL install (Task 3 Step 1) precedes the `adaptive_lighting:` deploy (Step 2) —
  the one hard ordering constraint, called out. Phase A (Tasks 1–2) is independent of AL and
  ships first.
- **Type/ID consistency:** `input_boolean.bedroom_manual_off`, `light.bedroom_lights`,
  `binary_sensor.aqara_fp300_presence`, `sensor.aqara_fp300_illuminance`,
  `switch.adaptive_lighting_bedroom`, dial topic `zigbee2mqtt/0x001788010f0ccda4` — used
  identically across all tasks and match the spec's verified facts.
- **No placeholders:** every automation/config block is complete inline; commands have expected
  output.
