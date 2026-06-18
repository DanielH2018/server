# Hue Tap Dial → Bedroom Lights (Home Assistant) — Design

**Date:** 2026-06-18
**Status:** Approved design, pre-implementation
**Scope:** First Home Assistant automation — make the Philips Hue Tap Dial control three Hue bulbs.

## Goal

Turn the Hue Tap Dial Switch into a full controller for the three Hue bulbs: rotate to dim,
buttons for on/off and scenes. This is the first automation in the homelab's Home Assistant
instance and establishes the pattern (event-driven controller → light group) plus the
version-control convention for HA automations.

## Verified facts (current state)

- **Bulbs:** 3× Philips Hue White & Color Ambiance, model **LCA017**, on Zigbee2MQTT
  (`0x001788010ff46108`, `0x001788010ff47ed0`, `0x001788010ff4ac53`).
- **Controller:** Philips Hue Tap Dial Switch, model **RDM002**, on Zigbee2MQTT
  (`0x001788010f0ccda4`). Battery device — emits an `action` (events), holds no light state.
- **AirGradient ONE** (model I-9PSL) via the native `airgradient` integration; **Aqara FP300**
  presence and **Dreo** fan also integrated. Out of scope here, candidates for later.
- **Z2M HA discovery is ON** (`homeassistant.enabled: true`) → bulbs auto-appear as `light.*`
  entities, the Tap Dial as a device publishing to `zigbee2mqtt/<name>/action`.
- **No automations exist yet:** no `automations.yaml`, and `configuration.yaml` has no
  `automation:` / `scene:` include.
- Devices still carry default IEEE-address friendly names in Z2M (must be renamed first).

## Control mapping

Dial right/left = brightness up/down (±12% per step, 0.2 s fade) in all cases.

| Control  | Action |
|----------|--------|
| Button 1 | Toggle the bulb group on/off (restores last setting) |
| Button 2 | `scene.bedroom_bright` — cool white 4000 K, 100% |
| Button 3 | `scene.bedroom_relax` — warm white 2700 K, ~40% |
| Button 4 | `scene.bedroom_nightlight` — dim amber, ~3% |

## Architecture decision

**All logic lives in Home Assistant** (user choice), not Zigbee binding. Trade-off accepted:
dial dimming round-trips Zigbee→Z2M→MQTT→HA→back, so very fast spins may feel slightly stepped.
If that becomes annoying, a future upgrade is to bind the dial→group directly in Z2M for the
dimming portion while keeping the buttons in HA (hybrid). Not done now.

## The automation

A single automation triggered by the dial's MQTT `action` topic, branching with `choose:`:

```yaml
alias: Bedroom Tap Dial control
trigger:
  - platform: mqtt
    topic: zigbee2mqtt/Bedroom Tap Dial/action
mode: queued        # process rapid dial ticks in order, don't drop them
max: 10
action:
  - choose:
      - conditions: "{{ trigger.payload == 'button_1_press' }}"
        sequence:
          - service: light.toggle
            target: { entity_id: light.bedroom_lights }
      - conditions: "{{ trigger.payload == 'button_2_press' }}"
        sequence: [{ service: scene.turn_on, target: { entity_id: scene.bedroom_bright } }]
      - conditions: "{{ trigger.payload == 'button_3_press' }}"
        sequence: [{ service: scene.turn_on, target: { entity_id: scene.bedroom_relax } }]
      - conditions: "{{ trigger.payload == 'button_4_press' }}"
        sequence: [{ service: scene.turn_on, target: { entity_id: scene.bedroom_nightlight } }]
      - conditions: "{{ 'dial_rotate_right' in trigger.payload }}"
        sequence:
          - service: light.turn_on
            target: { entity_id: light.bedroom_lights }
            data: { brightness_step_pct: 12, transition: 0.2 }
      - conditions: "{{ 'dial_rotate_left' in trigger.payload }}"
        sequence:
          - service: light.turn_on
            target: { entity_id: light.bedroom_lights }
            data: { brightness_step_pct: -12, transition: 0.2 }
```

**Design rationale:**
- **MQTT-topic trigger, not a `sensor.*_action` state trigger** — MQTT triggers fire on every
  message, so pressing the same button twice in a row still fires (a state trigger would
  suppress the repeat as "no state change").
- **`mode: queued` (max 10)** — a fast dial spin emits a burst of step events; queued processes
  them in order for smooth ramping. `restart` would drop all but the last; `single` would drop
  the burst.
- **Catch `dial_rotate_left`/`right` by substring** so the `_step`/`_slow`/`_fast` variants all
  map to dimming without enumerating each.

## Scenes

```yaml
- id: bedroom_bright
  name: Bedroom Bright
  entities:
    light.bedroom_lights: { state: "on", color_temp_kelvin: 4000, brightness_pct: 100 }
- id: bedroom_relax
  name: Bedroom Relax
  entities:
    light.bedroom_lights: { state: "on", color_temp_kelvin: 2700, brightness_pct: 40 }
- id: bedroom_nightlight
  name: Bedroom Nightlight
  entities:
    light.bedroom_lights: { state: "on", rgb_color: [255, 140, 40], brightness_pct: 3 }
```

## Storage: version-controlled (git source of truth)

Departs from the role's previous "automations are UI-managed, not templated" convention, by user
choice. Two parts:

1. **Enabling (templated config):** add to `configuration.yaml.j2`:
   ```yaml
   automation: !include automations.yaml
   scene: !include scenes.yaml
   ```
2. **Content (static files, copied):** ship as **static files** in the role and deploy with
   `ansible.builtin.copy` (NOT `template`):
   - `roles/containers/home-assistant/files/automations.yaml`
   - `roles/containers/home-assistant/files/scenes.yaml`

   **Why `copy`/`files/` and not `template`/`templates/`:** HA automation YAML uses `{{ }}`
   Jinja (`{{ trigger.payload == ... }}`). Ansible's templater would try to evaluate those at
   deploy time (where `trigger` is undefined) and fail. `copy` ships the file verbatim, no
   Jinja processing, no `{% raw %}` escaping needed.

   Each copy task is `register:`ed and folded into the existing `common_config_changed`
   expression in `tasks/main.yml`, so editing either file recreates HA on the next deploy —
   mirroring how `configuration.yaml` / `customize.yaml` / `ui-lovelace.yaml` already work.

**Consequence (documented):** git is the source of truth; HA is overwritten on deploy. The HA UI
automation/scene editors still open, but UI edits are NOT persistent — to change an automation or
scene, edit the role file and run the deploy. This matches how `configuration.yaml` already
behaves in this role. The role `CLAUDE.md` will be updated to state automations are now templated.

## Prep work (manual, in the Z2M UI) — must precede content

1. Rename the four devices (default IEEE names → friendly): the 3 bulbs and `Bedroom Tap Dial`.
2. Create a Z2M **group** `Bedroom Lights` containing the 3 bulbs.
   - A group sends one Zigbee groupcast → bulbs dim in unison and expose a single
     `light.bedroom_lights` entity. Three separate entities would drift on fast dial spins.

## Verification checkpoint (before finalizing the static files)

After the Z2M rename + group, confirm the real identifiers in HA (slugs can differ from the
friendly name):
- The group's entity_id (expected `light.bedroom_lights`) — HA Developer Tools → States.
- The dial's action topic (expected `zigbee2mqtt/Bedroom Tap Dial/action`) and the exact action
  payload strings for buttons + dial — Developer Tools → Events (listen to the topic / `mqtt`),
  or the Z2M device "Exposes"/logs while pressing each control.

Adjust the static files to the verified entity_id / topic / payloads before deploying.

## Implementation outline

1. Z2M: rename devices, create `Bedroom Lights` group (manual).
2. Verify entity_id + action topic + payload strings in HA (checkpoint above).
3. Add `files/automations.yaml` + `files/scenes.yaml` to the role (verified IDs).
4. Add `automation:`/`scene:` includes to `configuration.yaml.j2`.
5. Add two `ansible.builtin.copy` tasks to `tasks/main.yml`, register them, extend
   `common_config_changed`.
6. Update role `CLAUDE.md` (automations now templated/overwrite-on-deploy).
7. `--check` then deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`.
8. `prek run --all-files` (compose-template validation runs on the config change); commit.

## Testing / acceptance

- Dial right/left brightens/dims all three bulbs roughly in sync.
- Button 1 toggles the group; buttons 2/3/4 activate Bright / Relax / Nightlight.
- Same-button double-press fires both times.
- HA automation **Trace** shows the correct `choose` branch per event.
- `uv run python scripts/probe.py health home-assistant` passes (container running + healthy).

## Implementation deltas (verified 2026-06-18)

Two things the live verification checkpoint corrected vs. the draft above. The canonical
source is now `roles/containers/home-assistant/files/automations.yaml`:

- **Grouping is a Home Assistant Light Group helper**, not a Z2M group (user built it in the
  HA UI). Same entity_id `light.bedroom_lights`; HA fans out to the 3 bulbs as separate
  commands rather than one Zigbee groupcast. Fine for 3 bulbs; revisit a Z2M group if fast
  dial spins drift.
- **Trigger reads the dial's JSON, not a `/action` subtopic.** Z2M runs default `output: json`,
  so it publishes one blob per device. The dial was not renamed, so the topic is its IEEE
  address: `zigbee2mqtt/0x001788010f0ccda4`, and branches test
  `trigger.payload_json.action` (dial variants matched by substring via `| string` to be
  null-safe on non-action state updates).

## Out of scope (future)

- Air-quality-driven automations (AirGradient CO₂/PM → tint bulbs or notify).
- Presence-based lighting (FP300), time/sun schedules, Dreo integration.
- Proportional dial dimming using the RDM002 `action_step_size` payload.
- Hybrid Zigbee binding for smoother dimming.
