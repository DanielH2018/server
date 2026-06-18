# Hue Tap Dial → Bedroom Lights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NOTE: this plan has human-in-the-loop steps (Zigbee2MQTT UI, physically pressing the dial) that a subagent cannot perform — execute inline, collaboratively.

**Goal:** Make the Philips Hue Tap Dial (RDM002) control three Hue bulbs (LCA017) in Home Assistant — dial for brightness, buttons for on/off + three scenes — with the automation version-controlled in git.

**Architecture:** A single HA automation triggered by the dial's MQTT `action` topic, branching with `choose:`. The 3 bulbs are dimmed as one Zigbee2MQTT group (`light.bedroom_lights`). Automation + scenes ship as static files in the role's `files/` dir, deployed by `ansible.builtin.copy` (not `template`, to avoid HA's `{{ }}` colliding with Ansible's Jinja). Git is the source of truth; HA UI edits don't persist.

**Tech Stack:** Home Assistant (LSIO image), Zigbee2MQTT 2.x + Mosquitto (MQTT discovery), Ansible, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-06-18-hue-tap-dial-bedroom-lights-design.md`

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ansible/roles/containers/home-assistant/files/automations.yaml` | The Tap Dial automation (static, git-tracked) | **Create** |
| `ansible/roles/containers/home-assistant/files/scenes.yaml` | The 3 scenes (static, git-tracked) | **Create** |
| `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` | Add `automation:`/`scene:` includes | **Modify** |
| `ansible/roles/containers/home-assistant/tasks/main.yml` | Two `copy` tasks + extend `common_config_changed` | **Modify** |
| `ansible/roles/containers/home-assistant/CLAUDE.md` | Document automations are now templated | **Modify** |

---

## Task 1: Prep in Zigbee2MQTT (MANUAL — user, Z2M UI)

**No repo files.** Done by the user in the Z2M frontend (`zigbee.<domain>`).

- [ ] **Step 1: Rename the four devices** (Z2M → each device → ⚙ → rename / "Friendly name"):
  - The 3 bulbs (`0x001788010ff46108`, `…ff47ed0`, `…ff4ac53`) → any friendly names you like.
  - The dial (`0x001788010f0ccda4`) → exactly **`Bedroom Tap Dial`** (the automation's MQTT topic depends on this name → `zigbee2mqtt/Bedroom Tap Dial/action`).

- [ ] **Step 2: Create a group** (Z2M → Groups → "Create group"):
  - Name it exactly **`Bedroom Lights`** and add all 3 bulbs.
  - This yields one `light.bedroom_lights` entity in HA and dims all 3 bulbs in a single Zigbee groupcast (in sync).

- [ ] **Step 3: Tell the assistant when done** so it can run the verification checkpoint.

---

## Task 2: Verification checkpoint (assistant — read-only)

**No repo files modified.** Confirm the real identifiers before writing the static files, because HA slugs can differ from friendly names.

- [ ] **Step 1: Confirm the group entity_id**

Run:
```bash
grep -aoE '"entity_id": *"light\.[a-z0-9_]*bedroom[a-z0-9_]*"' \
  /home/ubuntu/server/containers/home-assistant/config/.storage/core.entity_registry
```
Expected: a line containing `"entity_id": "light.bedroom_lights"`. If the slug differs (e.g. `light.bedroom_lights_2`), note the actual value and substitute it everywhere `light.bedroom_lights` appears in Tasks 3–4.

- [ ] **Step 2: Confirm the dial's MQTT action topic**

Run:
```bash
grep -aoE "'?0x001788010f0ccda4'?:|Bedroom Tap Dial" \
  /home/ubuntu/server/containers/zigbee2mqtt/data/devices.yaml
```
Expected: `friendly_name: Bedroom Tap Dial`. The action topic is therefore `zigbee2mqtt/Bedroom Tap Dial/action`. If the name differs, substitute it in the trigger topic in Task 4.

- [ ] **Step 3: (Optional) Confirm live action payloads**

The RDM002 payload strings used (`button_1_press`…`button_4_press`, `dial_rotate_left_*`, `dial_rotate_right_*`) are the Z2M standard, and the automation matches the dial by substring so the `_step`/`_slow`/`_fast` variants all work. To watch them live while pressing each control:
```bash
docker exec mosquitto mosquitto_sub -t 'zigbee2mqtt/Bedroom Tap Dial/action' -C 6 -v
```
Expected: lines like `zigbee2mqtt/Bedroom Tap Dial/action button_1_press`. (If `mosquitto_sub` needs auth, add `-u "$MQTT_USER" -P "$MQTT_PASS"`; skip this step if it's fiddly — the strings are well-known.)

---

## Task 3: Create the scenes file

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/scenes.yaml`

- [ ] **Step 1: Write the file** (substitute the verified entity_id from Task 2 if different)

```yaml
---
# Managed by Ansible (roles/containers/home-assistant/files/scenes.yaml).
# Source of truth — copied to ./config and overwritten on deploy. HA UI scene edits are
# NOT persistent. To change a scene, edit this file and redeploy home-assistant.
- id: bedroom_bright
  name: Bedroom Bright
  entities:
    light.bedroom_lights:
      state: "on"
      color_temp_kelvin: 4000
      brightness_pct: 100
- id: bedroom_relax
  name: Bedroom Relax
  entities:
    light.bedroom_lights:
      state: "on"
      color_temp_kelvin: 2700
      brightness_pct: 40
- id: bedroom_nightlight
  name: Bedroom Nightlight
  entities:
    light.bedroom_lights:
      state: "on"
      rgb_color: [255, 140, 40]
      brightness_pct: 3
```

- [ ] **Step 2: Sanity-check YAML parses**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scenes.yaml'))" && echo OK`
Expected: `OK`

---

## Task 4: Create the automation file

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/automations.yaml`

- [ ] **Step 1: Write the file** (substitute verified entity_id / topic from Task 2 if different)

```yaml
---
# Managed by Ansible (roles/containers/home-assistant/files/automations.yaml).
# Source of truth — copied to ./config and overwritten on deploy. HA UI automation edits are
# NOT persistent. To change, edit this file and run:
#   uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"
- id: bedroom_tap_dial_control
  alias: Bedroom Tap Dial control
  description: Hue Tap Dial (RDM002) controls the Bedroom Lights group.
  mode: queued
  max: 10
  trigger:
    - platform: mqtt
      topic: zigbee2mqtt/Bedroom Tap Dial/action
  action:
    - choose:
        - conditions: "{{ trigger.payload == 'button_1_press' }}"
          sequence:
            - service: light.toggle
              target:
                entity_id: light.bedroom_lights
        - conditions: "{{ trigger.payload == 'button_2_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_bright
        - conditions: "{{ trigger.payload == 'button_3_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_relax
        - conditions: "{{ trigger.payload == 'button_4_press' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_nightlight
        - conditions: "{{ 'dial_rotate_right' in trigger.payload }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: 12
                transition: 0.2
        - conditions: "{{ 'dial_rotate_left' in trigger.payload }}"
          sequence:
            - service: light.turn_on
              target:
                entity_id: light.bedroom_lights
              data:
                brightness_step_pct: -12
                transition: 0.2
```

- [ ] **Step 2: Sanity-check YAML parses** (the `{{ }}` are inside quoted strings → valid YAML)

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml'))" && echo OK`
Expected: `OK`

---

## Task 5: Wire the includes into configuration.yaml.j2

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2`

- [ ] **Step 1: Add the includes after the `homeassistant:` block**

Find:
```jinja
homeassistant:
  customize: !include customize.yaml
```
Replace with:
```jinja
homeassistant:
  customize: !include customize.yaml

# Automations + scenes are Ansible-managed static files (role files/automations.yaml,
# files/scenes.yaml), copied to ./config and version-controlled. The HA UI editors still
# open, but edits are NOT persistent — git is the source of truth; redeploy to change.
automation: !include automations.yaml
scene: !include scenes.yaml
```

- [ ] **Step 2: Verify the template still renders to valid YAML**

Run: `uv run python scripts/validate_compose_templates.py 2>/dev/null; echo "rc=$?"`
Expected: the validator targets compose templates, so it should pass/no-op here (`rc=0`). The real YAML check happens in Task 8's `--check` + HA load.

---

## Task 6: Add the copy tasks + extend the recreate trigger

**Files:**
- Modify: `ansible/roles/containers/home-assistant/tasks/main.yml`

- [ ] **Step 1: Insert two `copy` tasks after the "Remove the superseded per-dashboard file" task**

After this existing task block:
```yaml
- name: Remove the superseded per-dashboard file (migrated to root ui-lovelace.yaml)
  tags: [config]
  ansible.builtin.file:
    path: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/dashboards"
    state: absent
```
Add:
```yaml
- name: Deploy automations from static file
  tags: [config]
  ansible.builtin.copy:
    src: automations.yaml
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/automations.yaml"
    mode: "0664"
  register: home_assistant_automations

- name: Deploy scenes from static file
  tags: [config]
  ansible.builtin.copy:
    src: scenes.yaml
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/scenes.yaml"
    mode: "0664"
  register: home_assistant_scenes
```

- [ ] **Step 2: Extend `common_config_changed`** so an edit to either file recreates HA

Find:
```yaml
    common_config_changed: "{{ (home_assistant_config is changed) or (home_assistant_customize is changed) or (home_assistant_dashboard is changed) }}"
```
Replace with:
```yaml
    common_config_changed: "{{ (home_assistant_config is changed) or (home_assistant_customize is changed) or (home_assistant_dashboard is changed) or (home_assistant_automations is changed) or (home_assistant_scenes is changed) }}"
```

- [ ] **Step 3: Lint the role**

Run: `cd /home/ubuntu/server && uv run ansible-lint ansible/roles/containers/home-assistant/tasks/main.yml`
Expected: no errors (warnings about `latest` image tags etc. are pre-existing and fine).

---

## Task 7: Update the role CLAUDE.md

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

- [ ] **Step 1: Amend the "NOT templated" line** (automations.yaml is now templated)

Find: `stores separately (`.storage/`, automations.yaml…), which are NOT templated.`
Replace `automations.yaml…` with `the recorder DB…` so the sentence reads `stores separately (`.storage/`, the recorder DB…), which are NOT templated.`

- [ ] **Step 2: Add a Notable bullet documenting the new behavior**

Add under the `## Notable` list (e.g. after the `configuration.yaml is templated` bullet):
```markdown
- **Automations + scenes ARE templated (since 2026-06-18).** `files/automations.yaml` and
  `files/scenes.yaml` are static files deployed by `ansible.builtin.copy` (NOT `template` —
  HA automation YAML uses `{{ }}` Jinja that Ansible would try to render and fail; `copy`
  ships them verbatim). Git is the source of truth; HA UI automation/scene edits are
  overwritten on deploy. Both feed `common_config_changed`, so an edit recreates HA (~120s).
  First automation: the Hue Tap Dial → Bedroom Lights group.
```

---

## Task 8: Validate and deploy

- [ ] **Step 1: Run the full pre-commit suite**

Run: `cd /home/ubuntu/server && prek run --all-files`
Expected: all hooks Pass (yamllint, ansible-lint, gitleaks, compose validation, pytest). Fix any yamllint findings on the new files inline (e.g. quote/indent), re-run.

- [ ] **Step 2: Dry-run the deploy**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" --check`
Expected: the two new `copy` tasks + `configuration.yaml` template show as **changed**; play completes with `failed=0`.

- [ ] **Step 3: Deploy for real**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: `changed` on the config tasks; HA container recreated (`common_config_changed` true); `failed=0`.

- [ ] **Step 4: Confirm HA came back healthy**

Run: `cd /home/ubuntu/server && uv run python scripts/probe.py health home-assistant`
Expected: exit 0, container running + healthy. If unhealthy, check `docker logs homeassistant` for a config error (a bad `!include` or YAML typo stops HA from starting).

- [ ] **Step 5: Confirm the automation + scenes loaded**

Run: `cd /home/ubuntu/server && uv run python scripts/probe.py loki-query '{container="homeassistant"} |= "Invalid config"' 2>/dev/null | head; echo "---"; grep -c 'bedroom' /home/ubuntu/server/containers/home-assistant/config/automations.yaml`
Expected: no "Invalid config" lines; the grep confirms the file deployed. (Also visible in HA UI: Settings → Automations shows "Bedroom Tap Dial control"; Settings → Scenes shows the 3 scenes.)

---

## Task 9: Commit

- [ ] **Step 1: Stage only the explicit paths** (concurrent-session safety — don't `git add -A`)

```bash
cd /home/ubuntu/server
git add ansible/roles/containers/home-assistant/files/automations.yaml \
        ansible/roles/containers/home-assistant/files/scenes.yaml \
        ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 \
        ansible/roles/containers/home-assistant/tasks/main.yml \
        ansible/roles/containers/home-assistant/CLAUDE.md \
        docs/superpowers/plans/2026-06-18-hue-tap-dial-bedroom-lights.md
```

- [ ] **Step 2: Commit**

```bash
git commit -m "$(cat <<'EOF'
home-assistant: add Hue Tap Dial → Bedroom Lights automation

First HA automation. RDM002 Tap Dial controls the Z2M "Bedroom Lights"
group: dial = brightness (±12%/step), button 1 = toggle, buttons 2-4 =
Bright/Relax/Nightlight scenes. MQTT-topic trigger + choose, mode: queued.

Automations now version-controlled: files/automations.yaml + files/scenes.yaml
shipped via ansible.builtin.copy (not template, to avoid HA's {{ }} colliding
with Ansible Jinja). Wired into common_config_changed; CLAUDE.md updated.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Hardware acceptance test (MANUAL — user)

- [ ] **Step 1:** Rotate the dial right → all 3 bulbs brighten in sync; left → dim.
- [ ] **Step 2:** Press button 1 → group toggles off, press again → on.
- [ ] **Step 3:** Press buttons 2 / 3 / 4 → Bright / Relax / Nightlight looks.
- [ ] **Step 4:** Double-press one button → fires both times (confirms the MQTT-topic trigger).
- [ ] **Step 5: If something misbehaves,** open HA → Settings → Automations → "Bedroom Tap Dial control" → **Traces** to see which `choose` branch ran (or didn't). Most likely fix is a wrong `light.bedroom_lights` entity_id or topic name → correct in `files/` and redeploy.

---

## Self-Review Notes

- **Spec coverage:** mapping table → Task 4; scenes → Task 3; includes → Task 5; copy tasks + recreate trigger → Task 6; CLAUDE.md convention change → Task 7; Z2M prep + group → Task 1; verification checkpoint → Task 2; deploy/test → Tasks 8–10. All spec sections covered.
- **Entity-id risk:** the single biggest failure mode is the group entity_id / dial topic not matching the hard-coded strings. Task 2 verifies both before the files are written, and Task 10 Step 5 gives the trace-based recovery path.
- **No placeholders:** every file's full content is inline; every command has expected output.
