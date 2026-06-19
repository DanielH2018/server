# Hue Tap Dial 4-Button Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-map the bedroom Hue Tap Dial so each button owns one clear theme (Power / Brightness / Sleep / Fan) with no overlapping controls, using press + hold.

**Architecture:** All four files are static HA config copied verbatim by Ansible (`files/scenes.yaml`, `files/scripts.yaml`, `files/automations.yaml`) and feed `common_config_changed`, so editing them recreates the `home-assistant` container (~120s) on deploy. We add one new scene and one small gated wrapper script, then rewrite the `choose:` branches of the `bedroom_tap_dial_control` automation. The shared `bedroom_apply_natural` script and the entire presence/arrive/away path are left untouched (gating them would break the "Turn back on" action).

**Tech Stack:** Home Assistant (LinuxServer.io image), MQTT (Zigbee2MQTT, device "Tap Dial" = Hue RDM002), Ansible, YAML + HA Jinja2.

## Global Constraints

- **Never edit `containers/` directly** — these source files live under `ansible/roles/containers/home-assistant/files/`; the deploy renders them.
- **`files/*.yaml` are copied verbatim (not Ansible-templated)** — HA `{{ }}` Jinja stays as-is; do NOT add `{% raw %}`.
- **Manual taps stay ungated.** The lux gate (`in wake window OR illuminance < 50`) lives only on the automatic presence path and the new `bedroom_apply_natural_gated` wrapper.
- **Every light-ON action clears `input_boolean.bedroom_manual_off`**; only Button 1's off-branch sets it.
- **Lux gate threshold stays 50** (do not retune in this change).
- **Commit directly to `master`** (repo norm; no feature branch).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags home-assistant`. Health gate: `uv run python scripts/probe.py health home-assistant`.

---

### Task 1: Add the new scene and gated wrapper script

The two new primitives the redesigned automation depends on.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scenes.yaml` (append after `bedroom_nightlight`, currently ends line 18)
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (insert after `bedroom_apply_natural`, before the `# Air-quality alert pulse` comment at line 127)

**Interfaces:**
- Produces: `scene.bedroom_relax` (HA scene), `script.bedroom_apply_natural_gated` (HA script) — both consumed by Task 2.

- [ ] **Step 1: Add `scene.bedroom_relax` to `scenes.yaml`**

Append to the end of the file (after the `bedroom_nightlight` block):

```yaml
- id: bedroom_relax
  name: Bedroom Relax
  entities:
    light.bedroom_lights:
      state: "on"
      color_temp_kelvin: 2200
      brightness_pct: 30
```

- [ ] **Step 2: Add `script.bedroom_apply_natural_gated` to `scripts.yaml`**

Insert this block immediately after the `bedroom_apply_natural:` script (after its `default:` sequence ends, before the `# Air-quality alert pulse:` comment):

```yaml
# Gated dispatcher: apply the natural lighting state ONLY if the darkness gate allows (in the
# morning wake window OR illuminance < 50) — otherwise turn the lights OFF, i.e. produce the same
# outcome the automatic presence path would. Used by the Tap Dial button-1 HOLD ("reset to auto");
# the manual taps call the ungated bedroom_apply_natural directly. The gate expression mirrors
# bedroom_presence_on's condition (the one other place it lives).
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

- [ ] **Step 3: Validate both files parse as YAML**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scenes.yaml')); yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml')); print('OK')"
```
Expected: prints `OK` (no traceback). If it raises, fix the indentation/quoting and re-run.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scenes.yaml ansible/roles/containers/home-assistant/files/scripts.yaml
git commit -m "home-assistant: add bedroom_relax scene + lux-gated natural wrapper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: pre-commit hooks (incl. `check yaml`) all Pass.

---

### Task 2: Re-map the Tap Dial automation

Rewrite the `action:` block of `bedroom_tap_dial_control` to the Power / Brightness / Sleep / Fan layout.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` — replace the `action:` block of `bedroom_tap_dial_control` (lines 27–95, from `  action:` down to the end of the `dial_rotate_left` branch).

**Interfaces:**
- Consumes: `scene.bedroom_relax`, `script.bedroom_apply_natural_gated` (Task 1); existing `script.bedroom_apply_natural`, `script.bedroom_apply_fan`, `script.bedroom_bedtime`, `scene.bedroom_bright`, `scene.bedroom_nightlight`, `input_boolean.bedroom_manual_off`, `input_boolean.bedroom_fan_manual`, `fan.tower_fan`.

- [ ] **Step 1: Replace the `action:` block**

Keep the lines above `  action:` (the `id`, `alias`, `description`, `mode: queued`, `max: 10`, `trigger`, the MQTT-gate `condition`) exactly as they are. Replace from `  action:` through the end of the `dial_rotate_left` branch with:

```yaml
  action:
    # Read the action once, safely defaulted — so no condition below can hit an undefined attribute.
    - variables:
        act: "{{ trigger.payload_json.action | default('') }}"
    - choose:
        # Button 1 = POWER. PRESS = smart toggle: ON -> off + engage manual-off (presence won't
        # re-on); OFF -> Adaptive Lighting natural look (UNGATED — a manual tap always lights) +
        # clear manual-off. HOLD = reset to auto (below).
        - conditions: "{{ act == 'button_1_press' }}"
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
                - service: script.bedroom_apply_natural
                - service: input_boolean.turn_off
                  target:
                    entity_id: input_boolean.bedroom_manual_off
        # Button 1 HOLD = reset to auto: clear the light + fan overrides, re-sync lights to their
        # natural state HONORING the lux gate (bright room -> off), and re-apply the fan if home.
        - conditions: "{{ act == 'button_1_hold' }}"
          sequence:
            - service: input_boolean.turn_off
              target:
                entity_id:
                  - input_boolean.bedroom_manual_off
                  - input_boolean.bedroom_fan_manual
            - service: script.bedroom_apply_natural_gated
            - if: "{{ is_state('person.daniel', 'home') }}"
              then:
                - service: script.bedroom_apply_fan
        # Button 2 = BRIGHTNESS. PRESS = Relax/cozy scene; HOLD = Bright (full). Both clear manual-off.
        - conditions: "{{ act == 'button_2_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_relax
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ act == 'button_2_hold' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_bright
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        # Button 3 = SLEEP. PRESS = nightlight (warm dim); HOLD = bedtime / sleep routine.
        - conditions: "{{ act == 'button_3_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_nightlight
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - conditions: "{{ act == 'button_3_hold' }}"
          sequence:
            - service: script.bedroom_bedtime
        # Button 4 = FAN. PRESS = reset to automatic (clear fan override + apply temp band);
        # HOLD = boost to 100% (engage fan override so it sticks until reset/morning).
        - conditions: "{{ act == 'button_4_press' }}"
          sequence:
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_fan_manual
            - service: script.bedroom_apply_fan
        - conditions: "{{ act == 'button_4_hold' }}"
          sequence:
            - service: input_boolean.turn_on
              target:
                entity_id: input_boolean.bedroom_fan_manual
            - service: fan.turn_on
              target:
                entity_id: fan.tower_fan
            - service: fan.set_percentage
              target:
                entity_id: fan.tower_fan
              data:
                percentage: 100
        # Dial rotate = brightness +/- 12% (substring match catches the *_step variants).
        - conditions: "{{ 'dial_rotate_right' in act }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: 12
                transition: 0.2
        - conditions: "{{ 'dial_rotate_left' in act }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: -12
                transition: 0.2
```

- [ ] **Step 2: Validate the file parses as YAML**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml')); print('OK')"
```
Expected: prints `OK`. If it raises, the most likely cause is a `choose:`/`sequence:` indentation slip — fix and re-run.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "home-assistant: re-map Tap Dial to Power/Brightness/Sleep/Fan (no overlap)

Each button owns one theme via press+hold. B1 press-on now applies
Adaptive Lighting (ungated); B1 hold = reset-to-auto (lux-gated). B2
press=Relax / hold=Bright. B3 press=nightlight / hold=bedtime. B4
press=fan auto / hold=boost. Dial unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: pre-commit hooks all Pass.

---

### Task 3: Update the docs

Bring the role docs in line with the new mapping so the next reader isn't misled by the old "buttons 2-3 = scenes, button 4 = natural-state reset" description.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` (the Tap Dial sentence in the "Automations…" bullet — currently reads "Hue Tap Dial (RDM002) drives the `light.bedroom_lights` group (dial = brightness, button 1 = smart toggle, buttons 2-3 = scenes, button 4 = natural-state reset → `script.bedroom_apply_natural`, see below)")
- Modify: `ansible/roles/containers/home-assistant/SETUP.md` (the Tap Dial button-map section)

- [ ] **Step 1: Update `CLAUDE.md`**

Replace the parenthetical Tap Dial description with:

```
(dial = brightness ±12%; B1 = Power: press = smart toggle [on → `bedroom_apply_natural`
ungated, off → off + manual-off], hold = reset-to-auto [clear overrides, re-sync lux-gated
via `bedroom_apply_natural_gated` + fan]; B2 = Brightness: press = `scene.bedroom_relax`,
hold = `scene.bedroom_bright`; B3 = Sleep: press = `scene.bedroom_nightlight`, hold =
`script.bedroom_bedtime`; B4 = Fan: press = auto [clear fan-manual + `bedroom_apply_fan`],
hold = boost 100%). Manual taps are ungated by design — the lux gate lives on the presence
path + the reset hold.
```

- [ ] **Step 2: Update `SETUP.md`**

Locate the Tap Dial button-map section (search for `button` / `Tap Dial`) and replace it with this table:

```markdown
| Control | Press / Rotate | Hold |
|---------|----------------|------|
| **Button 1 — Power** | toggle: on → natural (Adaptive Lighting, ungated); off → off + stay-off | reset to auto (clear overrides, re-sync lights lux-gated + fan) |
| **Button 2 — Brightness** | Relax / cozy scene (warm ~30%) | Bright scene (full) |
| **Button 3 — Sleep** | Nightlight (warm ~3%) | Bedtime routine |
| **Button 4 — Fan** | fan → auto | boost 100% |
| **Dial** | brightness ±12% | — |
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md ansible/roles/containers/home-assistant/SETUP.md
git commit -m "docs(home-assistant): update Tap Dial button map to the new 4-button layout

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Deploy and verify all eight button actions

**Files:** none (deploy + behavioral verification).

- [ ] **Step 1: Deploy the role**

```bash
cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags home-assistant
```
Expected: play recaps `changed` for the config copies + a container recreate, `failed=0`.

- [ ] **Step 2: Health gate**

```bash
uv run python scripts/probe.py health home-assistant
```
Expected: exits 0 (running + healthy). Wait ~120s after deploy if it reports unhealthy mid-restart, then re-run.

- [ ] **Step 3: Confirm the automation reloaded with no config error**

```bash
docker exec -i home-assistant python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/config/home-assistant_v2.db")
q = """SELECT s.state, datetime(s.last_updated_ts,'unixepoch','localtime')
FROM states s JOIN states_meta sm ON s.metadata_id=sm.metadata_id
WHERE sm.entity_id='automation.bedroom_tap_dial_control'
ORDER BY s.last_updated_ts DESC LIMIT 1"""
print(db.execute(q).fetchone())
PY
```
Expected: `('on', '<recent timestamp>')`. If `unavailable`/missing, check `docker logs home-assistant` for an automations.yaml parse error and fix.

- [ ] **Step 4: Simulate each action over MQTT and confirm the outcome**

Each action can be fired without the physical remote by publishing the action payload to the trigger topic (the automation only reads `payload_json.action`). Run from inside the mosquitto container (adjust creds to the broker's, as used for the FP300 tuning):

```bash
docker exec -i mosquitto mosquitto_pub -t 'zigbee2mqtt/Tap Dial' -m '{"action":"button_2_hold"}'
```

Fire each of these and confirm the expected effect (watch `light.bedroom_lights` / `fan.tower_fan` state, e.g. with the Step-3 query pattern or the HA logbook):

| Payload `action` | Expected effect |
|------------------|-----------------|
| `button_1_press` (lights off) | lights ON at Adaptive-Lighting natural; `bedroom_manual_off` → off |
| `button_1_press` (lights on)  | lights OFF; `bedroom_manual_off` → on |
| `button_1_hold` | `bedroom_manual_off` + `bedroom_fan_manual` → off; in a **bright** room lights OFF (gated), in a dim room lights ON natural; fan re-applied if home |
| `button_2_press` | `scene.bedroom_relax` (warm ~30%) |
| `button_2_hold`  | `scene.bedroom_bright` (full) |
| `button_3_press` | `scene.bedroom_nightlight` (warm ~3%) |
| `button_3_hold`  | bedtime: `bedroom_sleep_mode` → on, AL sleep on, nightlight |
| `button_4_press` | `bedroom_fan_manual` → off, fan set to temp band |
| `button_4_hold`  | `bedroom_fan_manual` → on, fan at 100% |

- [ ] **Step 5: Confirm the holds actually arrive from the physical remote**

The simulation proves the automation logic; this proves the RDM002 emits `button_{2,3,4}_hold` (button 1's hold is already in use today, so it's known-good). Physically press-and-hold buttons 2, 3, 4 once each and confirm the same effects. If any hold does nothing, check what Z2M published:

```bash
docker exec -i mosquitto mosquitto_sub -t 'zigbee2mqtt/Tap Dial' -C 1
```
Then press-hold that button and read the `action` value; adjust the matching string if the firmware names it differently.

- [ ] **Step 6: Final state sanity**

Leave the room in a sane state (e.g. fire `button_1_hold` to reset to auto, or `button_4_press` to return the fan to automatic) so the test payloads don't leave a stray override engaged.

---

## Notes for the implementer

- **No pytest exists for HA YAML** — validation is YAML-parse (`yaml.safe_load`) + the deploy + the behavioral checks in Task 4. That is the intended test cycle here.
- **Holding a button does not also fire its press** on the RDM002 (press fires on a short press-release, hold after the hold threshold) — that's why B1 can carry both a press and a hold today.
- If a deploy reports the container unhealthy, it's usually mid-recreate; the known-benign "could not validate that the sqlite3 database was shutdown cleanly" log line on boot is expected (see role CLAUDE.md).
