# HA presence "too bright" blip + away notification hold — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an acknowledgement light-blip when the bedroom is entered but too bright for auto-on, and make `script.bedroom_notify` hold non-critical alerts while away (cancelling any that resolve before return, flushing the rest as a digest on arrival).

**Architecture:** Pure Home Assistant YAML edits in the bedroom suite. Feature 1 = a new `script.bedroom_blip` + a sibling `bedroom_presence_blip` automation (the arrival edge with the lux gate inverted). Feature 2 = all hold/cancel logic added inside the single `script.bedroom_notify` choke-point (held alerts parked as `persistent_notification` `hold_<tag>` entities), plus a `recovery: true` flag on recovery call-sites and a `bedroom_flush_held_notifications` automation that digests them on arrival home.

**Tech Stack:** Home Assistant automations/scripts (YAML), deployed by Ansible (`ansible.builtin.copy`) into the `home-assistant` container. Spec: `docs/superpowers/specs/2026-06-19-ha-presence-blip-and-away-notification-hold-design.md`.

## Global Constraints

- **Files are copy'd verbatim, NOT Ansible-templated.** They use HA `{{ }}` Jinja — never wrap in `{% raw %}`, never add Ansible Jinja. (CLAUDE.md: "HA Jinja lives in copy'd files.")
- **`containers/` is read-only** — edit only under `ansible/roles/containers/home-assistant/files/`.
- **Edit on `master` directly** — no feature branch (user convention).
- **No `{# #}` Jinja block comments in these files** — use plain `#` YAML comments (block comments corrupt indentation).
- **YAML validity is the per-task gate:** `uv run python -c "import yaml; yaml.safe_load(open('<path>')); print('YAML OK')"` (the repo's uv env has PyYAML via ansible-core).
- **Deploy command:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA ~120s via `common_config_changed`).
- **Verification gotchas:** query automations by **alias-slug** not id; the HA recorder is stale right after a restart — confirm liveness via `last_triggered` / container `StartedAt`, not recorder timestamps.
- **`away` definition (verbatim):** `{{ states('person.daniel') not in ['home', 'unknown', 'unavailable'] }}` — fails open (over-notify) on tracker glitches.

---

### Task 1: Feature 1 — `bedroom_blip` script + `bedroom_presence_blip` automation

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (add `bedroom_blip` after `bedroom_alert_pulse`)
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (add `bedroom_presence_blip` after `bedroom_presence_on`)
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` (document the blip)

**Interfaces:**
- Consumes: existing `light.bedroom_lights`, `binary_sensor.aqara_fp300_presence`, `binary_sensor.bedroom_auto_light_allowed`, `input_boolean.bedroom_manual_off`, `person.daniel`.
- Produces: `script.bedroom_blip` (no args), `automation.bedroom_presence_blip_too_bright`.

- [ ] **Step 1: Add the `bedroom_blip` script.** In `scripts.yaml`, locate the end of `bedroom_alert_pulse` (the block ending with the `scene.turn_on` of `scene.bedroom_pre_alert`, followed by the `# Fan: set the DREO tower fan...` comment). Insert this block immediately after the `bedroom_alert_pulse` sequence and before that `# Fan:` comment:

```yaml
bedroom_blip:
  alias: "Bedroom — too-bright acknowledgement blip"
  description: >-
    Soft warm flash (off -> 15% -> off) to acknowledge that presence was detected but the room is
    too bright for auto-on (the lux gate is blocking it). Inverse of bedroom_alert_pulse: it only
    runs when the lights are already OFF (the caller enforces that), so no scene snapshot is needed —
    a plain turn_off restores the known prior state.
  mode: single
  sequence:
    - service: light.turn_on
      target:
        entity_id: light.bedroom_lights
      data:
        brightness_pct: 15
        color_temp_kelvin: 2700
        transition: 0.4
    - delay: "00:00:01"
    - service: light.turn_off
      target:
        entity_id: light.bedroom_lights
      data:
        transition: 0.6
```

- [ ] **Step 2: Add the `bedroom_presence_blip` automation.** In `automations.yaml`, locate the end of `bedroom_presence_on` (the line `      service: script.bedroom_apply_natural` under its `action:`, followed by a blank line then the `# Absence OFF:` comment). Insert this block between them (after the blank line that follows `bedroom_presence_on`, before `# Absence OFF:`):

```yaml
# Presence BLIP: you arrived but the room is too bright for auto-on (the lux gate is OFF), so the
# lights won't come on. Flash them softly to acknowledge the detection. Sibling of bedroom_presence_on,
# sharing ONLY the arrival edge (not the dusk lux-crossing — that path is when lights DO come on), with
# the lux gate INVERTED. Mutually exclusive with presence_on per arrival: auto_light_allowed is either
# on (presence_on lights up) or off (this blips). Won't loop the lux trigger: a ~1s blip can't satisfy
# presence_on's `below: 50 for: 30s`, and it only fires when illuminance is already >= 50 (bright ambient).
- id: bedroom_presence_blip
  alias: Bedroom presence blip too bright
  description: Soft acknowledgement flash when presence is detected on arrival but the lux gate blocks auto-on.
  mode: single
  trigger:
    - platform: state
      entity_id: binary_sensor.aqara_fp300_presence
      to: "on"
  condition:
    # Didn't deliberately kill the lights.
    - condition: state
      entity_id: input_boolean.bedroom_manual_off
      state: "off"
    # Never blip an empty house on an FP300 radar false-positive.
    - condition: state
      entity_id: person.daniel
      state: "home"
    # Still actually occupied.
    - condition: state
      entity_id: binary_sensor.aqara_fp300_presence
      state: "on"
    # INVERTED lux gate: auto-on is BLOCKED (too bright) — the whole reason to blip instead of light up.
    - condition: state
      entity_id: binary_sensor.bedroom_auto_light_allowed
      state: "off"
    # Only "won't turn on" makes sense when the lights are currently off.
    - condition: state
      entity_id: light.bedroom_lights
      state: "off"
  action:
    - service: script.bedroom_blip
```

- [ ] **Step 3: Verify both files are valid YAML.**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml')); yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 4: Assert both blocks landed.**

Run:
```bash
cd /home/ubuntu/server && grep -c "bedroom_blip:" ansible/roles/containers/home-assistant/files/scripts.yaml && grep -c "id: bedroom_presence_blip" ansible/roles/containers/home-assistant/files/automations.yaml
```
Expected: `1` then `1`

- [ ] **Step 5: Document in CLAUDE.md.** In `ansible/roles/containers/home-assistant/CLAUDE.md`, find the bullet that begins `- **`files/scripts.yaml` — the "natural lighting state" dispatcher` and, after the paragraph describing the lux gate / feedback-loop caveat (the bullet ending `...this is why the fan button stays out of light control entirely.`), add a new bullet:

```markdown
- **Too-bright arrival blip (since 2026-06-19).** `automation.bedroom_presence_blip_too_bright` is a
  sibling of `bedroom_presence_on`: same arrival edge (`binary_sensor.aqara_fp300_presence` -> on),
  but the lux gate is **inverted** (`binary_sensor.bedroom_auto_light_allowed` == off) plus
  `manual_off` off, `person home`, and lights currently off. When you walk in but it's too bright to
  auto-light, it calls `script.bedroom_blip` (off -> 15% warm 2700K ~1s -> off) so you get an
  acknowledgement instead of silence. `bedroom_blip` is the inverse of `bedroom_alert_pulse` — it
  needs NO `scene.create` snapshot because it only runs with the lights already off, so a plain
  `turn_off` restores the known state. No feedback loop: it fires only at illuminance >= 50 (bright
  ambient), and a ~1s blip can't satisfy `presence_on`'s `below: 50 for: 30s`. No cooldown initially;
  add a trigger `for:` debounce if presence flapping makes it chatty.
```

- [ ] **Step 6: Commit.**

```bash
cd /home/ubuntu/server && git add ansible/roles/containers/home-assistant/files/scripts.yaml ansible/roles/containers/home-assistant/files/automations.yaml ansible/roles/containers/home-assistant/CLAUDE.md && git commit -m "$(cat <<'EOF'
home-assistant: blip the lights when presence is detected but too bright to auto-on

New script.bedroom_blip (off -> 15% warm -> off) + bedroom_presence_blip automation
(arrival edge with the lux gate inverted, guarded by manual_off/person-home/lights-off).
Acknowledges presence when bedroom_auto_light_allowed blocks the normal turn-on.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Feature 2a — hold/cancel routing inside `script.bedroom_notify`

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (`bedroom_notify`: add `recovery` field, `away` variable, early hold/dismiss branch)

**Interfaces:**
- Consumes: `person.daniel`, existing `tag`/`pierce` fields.
- Produces: `script.bedroom_notify` now accepts optional `recovery: bool`; when away and non-`pierce`, parks alerts as `persistent_notification` id `hold_<tag>` (or dismisses on recovery) and stops before the push path. Recovery call-sites (Task 3) and the flush automation (Task 4) depend on the `hold_` prefix and the `recovery` field name.

- [ ] **Step 1: Add the `recovery` field.** In `scripts.yaml`, in `bedroom_notify`'s `fields:` block, after the `pierce:` field (the one ending with `example: true`) and before the `actions:` field, insert:

```yaml
    recovery:
      description: This is an "all clear" — while away it cancels the held alert with the same tag instead of being delivered.
      required: false
      example: true
```

- [ ] **Step 2: Add the `away` variable.** In the same script's `sequence:`, in the first `- variables:` block (which defines `quiet`, `importance`, `channel`), add an `away` key. After the `channel:` line, add:

```yaml
        away: "{{ states('person.daniel') not in ['home', 'unknown', 'unavailable'] }}"
```

- [ ] **Step 3: Add the early hold/dismiss branch.** Immediately AFTER that `- variables:` block and BEFORE the `- service: notify.mobile_app_pixel_9_pro` step, insert this step:

```yaml
    # Away-aware hold: while you're outside the home geofence, non-critical (non-pierce) alerts are
    # parked as a persistent_notification (id hold_<tag>) instead of pushed — then either cancelled by
    # their recovery (same tag) or flushed as a digest on arrival (bedroom_flush_held_notifications).
    # pierce alerts and the at-home path fall straight through to the normal push below. `away` fails
    # open (unknown/unavailable -> deliver) so a tracker glitch over-notifies rather than swallowing.
    - if: "{{ away and not (pierce | default(false)) }}"
      then:
        - if: "{{ recovery | default(false) }}"
          then:
            # Resolved while away -> drop the held alert; you'll never see it.
            - service: persistent_notification.dismiss
              data:
                notification_id: "hold_{{ tag }}"
          else:
            # Park it (same tag updates in place) — held until you get home.
            - service: persistent_notification.create
              data:
                notification_id: "hold_{{ tag }}"
                title: "{{ title }}"
                message: "{{ message }}"
        - stop: "held while away (non-critical)"
```

- [ ] **Step 4: Verify YAML.**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 5: Assert the routing landed.**

Run:
```bash
cd /home/ubuntu/server && grep -c "hold_{{ tag }}" ansible/roles/containers/home-assistant/files/scripts.yaml && grep -c "held while away" ansible/roles/containers/home-assistant/files/scripts.yaml
```
Expected: `2` (one dismiss + one create) then `1`

- [ ] **Step 6: Commit.**

```bash
cd /home/ubuntu/server && git add ansible/roles/containers/home-assistant/files/scripts.yaml && git commit -m "$(cat <<'EOF'
home-assistant: bedroom_notify holds non-critical alerts while away

While person.daniel is outside the home geofence (fail-open on unknown), non-pierce
alerts are parked as persistent_notification hold_<tag> instead of pushed; a recovery
(recovery: true, same tag) dismisses the held one. pierce + at-home paths unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Feature 2b — mark recovery call-sites with `recovery: true`

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (four recovery `bedroom_notify` calls)

**Interfaces:**
- Consumes: the `recovery` field added in Task 2.
- Produces: nothing new — wires existing recoveries into the hold-cancel logic.

> Note: the spec named three call-sites; `zigbee_bridge_offline`'s recovery is the same alert/recovery class and is included here for consistency (four total).

- [ ] **Step 1: Threshold recovery.** In `automations.yaml`, in `bedroom_threshold_alert`'s `default:` branch, find the recovery `bedroom_notify` call:

```yaml
            - service: script.bedroom_notify
              data:
                title: "{{ cfg.ok }}"
                message: "{{ label }} back to normal ({{ value }}{{ unit_sp }})"
                tag: "{{ tag }}"
```
Change it to add the flag (append `recovery: true` as the last `data:` key):
```yaml
            - service: script.bedroom_notify
              data:
                title: "{{ cfg.ok }}"
                message: "{{ label }} back to normal ({{ value }}{{ unit_sp }})"
                tag: "{{ tag }}"
                recovery: true
```

- [ ] **Step 2: Sensor-offline recovery.** Find the "back online" call in `bedroom_sensor_offline_alert`:
```yaml
            - service: script.bedroom_notify
              data:
                title: "✅ Sensor back online"
                message: "{{ name }} is reporting again"
                tag: "{{ tag }}"
```
Change to:
```yaml
            - service: script.bedroom_notify
              data:
                title: "✅ Sensor back online"
                message: "{{ name }} is reporting again"
                tag: "{{ tag }}"
                recovery: true
```

- [ ] **Step 3: UPS power-restored recovery.** Find the "Power restored" call in `ups_power_event`:
```yaml
            - service: script.bedroom_notify
              data:
                title: "✅ Power restored"
                message: "Mains power back; UPS online ({{ charge }}%)."
                tag: ups_power
```
Change to:
```yaml
            - service: script.bedroom_notify
              data:
                title: "✅ Power restored"
                message: "Mains power back; UPS online ({{ charge }}%)."
                tag: ups_power
                recovery: true
```

- [ ] **Step 4: Zigbee-bridge recovery.** Find the "Zigbee bridge online" call in `zigbee_bridge_offline`'s `default:`:
```yaml
        - service: script.bedroom_notify
          data:
            title: "✅ Zigbee bridge online"
            message: "Zigbee2MQTT is back; devices are reconnecting."
            tag: zigbee_bridge
```
Change to:
```yaml
        - service: script.bedroom_notify
          data:
            title: "✅ Zigbee bridge online"
            message: "Zigbee2MQTT is back; devices are reconnecting."
            tag: zigbee_bridge
            recovery: true
```

- [ ] **Step 5: Verify YAML + count the flags.**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml')); print('YAML OK')" && grep -c "recovery: true" ansible/roles/containers/home-assistant/files/automations.yaml
```
Expected: `YAML OK` then `4`

- [ ] **Step 6: Commit.**

```bash
cd /home/ubuntu/server && git add ansible/roles/containers/home-assistant/files/automations.yaml && git commit -m "$(cat <<'EOF'
home-assistant: mark recovery notifies so away-hold cancels them

Add recovery: true to the four "all clear" bedroom_notify calls (threshold-ok,
sensor-online, UPS-restored, zigbee-bridge-online) so a resolution arriving while
away dismisses the held alert instead of being delivered.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Feature 2c — `bedroom_flush_held_notifications` digest on arrival

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (add automation after `bedroom_arrive_home`)
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` (document the away-hold system)

**Interfaces:**
- Consumes: `person.daniel`, the `hold_<tag>` persistent notifications created in Task 2.
- Produces: `automation.bedroom_flush_held_notifications`.

- [ ] **Step 1: Add the flush automation.** In `automations.yaml`, locate the end of the `bedroom_arrive_home` automation (its `action:` ends with the `script.bedroom_apply_natural` / lights re-check; it is immediately followed by the `bedroom_bedtime` automation). Insert this block between `bedroom_arrive_home` and `bedroom_bedtime`:

```yaml
# Flush held notifications on arrival: when you re-enter the home geofence, any alerts that were
# parked while you were away (persistent_notification ids starting with hold_) and did NOT resolve
# meanwhile are delivered as ONE "While you were out" digest, then cleared. Anything that resolved
# while away was already dismissed by its recovery (bedroom_notify), so it never appears here. The
# condition gates the whole thing so arriving with nothing held is silent. `match` is start-anchored,
# so 'hold_' won't catch the pierce path's bare-tag persistent notifications.
- id: bedroom_flush_held_notifications
  alias: Bedroom flush held notifications
  description: On arrival home, deliver any still-unresolved away-held alerts as one digest, then clear them.
  mode: single
  trigger:
    - platform: state
      entity_id: person.daniel
      to: "home"
  condition:
    - condition: template
      value_template: "{{ states.persistent_notification | selectattr('object_id', 'match', 'hold_') | list | count > 0 }}"
  action:
    - variables:
        held: "{{ states.persistent_notification | selectattr('object_id', 'match', 'hold_') | list }}"
        count: "{{ held | count }}"
        body: "{{ held | map(attribute='attributes.message') | map('regex_replace', '^', '• ') | join('\n') }}"
    - service: notify.mobile_app_pixel_9_pro
      data:
        title: "🏠 While you were out ({{ count }})"
        message: "{{ body }}"
        data:
          tag: held_digest
    - repeat:
        for_each: "{{ held | map(attribute='object_id') | list }}"
        sequence:
          - service: persistent_notification.dismiss
            data:
              notification_id: "{{ repeat.item }}"
```

- [ ] **Step 2: Verify YAML + assert the automation landed.**

Run:
```bash
cd /home/ubuntu/server && uv run python -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml')); print('YAML OK')" && grep -c "id: bedroom_flush_held_notifications" ansible/roles/containers/home-assistant/files/automations.yaml
```
Expected: `YAML OK` then `1`

- [ ] **Step 3: Document the away-hold system in CLAUDE.md.** In `ansible/roles/containers/home-assistant/CLAUDE.md`, find the existing notification bullet that begins `- **Notification routing — `script.bedroom_notify``. Immediately after that bullet (before the next `- **` bullet), add:

```markdown
- **Away-aware notification hold (since 2026-06-19).** `script.bedroom_notify` parks non-critical
  alerts while you're outside the home geofence. `away = person.daniel not in [home, unknown,
  unavailable]` (fails OPEN — a tracker glitch over-notifies, the opposite safe default to the
  unexpected-occupancy tripwire). While away + NOT `pierce`: instead of pushing, it
  `persistent_notification.create`s id `hold_<tag>` (so a re-fire with the same tag updates in place),
  then `stop`s before the push path. A recovery (`recovery: true`, same tag) `dismiss`es `hold_<tag>`
  and sends nothing — so a condition that self-resolves before you return is never seen. `pierce`
  alerts and the at-home path are unchanged. On arrival (`automation.bedroom_flush_held_notifications`,
  `person.daniel -> home`), all still-held `hold_*` notifications are delivered as ONE "While you were
  out (N)" digest (bulleted messages; phone-only, so per-alert action buttons like Boost fan are lost —
  tap into HA to act) and then dismissed; arriving with nothing held is silent. Recovery call-sites
  carrying `recovery: true`: threshold-ok, sensor-online, UPS-restored, zigbee-bridge-online.
  **Known limitation:** persistent notifications are in-memory, so an HA restart (e.g. a deploy) while
  away loses the held queue — accepted, since held items are non-critical and the overlap is rare.
  `match` filtering is start-anchored, so `hold_` never catches the pierce path's bare-`tag`
  persistent notifications.
```

- [ ] **Step 4: Commit.**

```bash
cd /home/ubuntu/server && git add ansible/roles/containers/home-assistant/files/automations.yaml ansible/roles/containers/home-assistant/CLAUDE.md && git commit -m "$(cat <<'EOF'
home-assistant: flush away-held notifications as a digest on arrival home

New bedroom_flush_held_notifications automation: on person.daniel -> home, deliver any
still-unresolved hold_<tag> persistent notifications as one "While you were out" digest,
then dismiss them. Silent when nothing is held. Documents the away-hold system in CLAUDE.md.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Deploy and live verification

**Files:** none (deploy + observe). Make any small doc/code fixes uncovered here, then commit if needed.

**Interfaces:** Consumes everything from Tasks 1-4.

- [ ] **Step 1: Deploy.**

Run:
```bash
cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"
```
Expected: play completes; the `home-assistant` container is recreated (config changed).

- [ ] **Step 2: Confirm HA is up and healthy.**

Run:
```bash
cd /home/ubuntu/server && uv run python scripts/probe.py health home-assistant
```
Expected: exit 0 (running + healthy). If it needs a moment, re-run.

- [ ] **Step 3: Confirm all new automations loaded (query by alias-slug, not id).** In HA → Developer Tools → States (or Settings → Automations), verify these exist and are `on`:
  - `automation.bedroom_presence_blip_too_bright`
  - `automation.bedroom_flush_held_notifications`

  And in Developer Tools → Actions, verify `script.bedroom_blip` is callable.

- [ ] **Step 4: Verify the blip (Feature 1).** With the room bright enough that `binary_sensor.bedroom_auto_light_allowed` is `off` and `light.bedroom_lights` off and `input_boolean.bedroom_manual_off` off and `person.daniel` home: trigger FP300 presence (walk in, or in Developer Tools → States set `binary_sensor.aqara_fp300_presence` to `on` to simulate). Expected: one soft warm flash (~15% ~1s) then off — NOT a sustained turn-on. Then confirm the negative cases: with `auto_light_allowed` on, arrival should run the NORMAL `bedroom_presence_on` turn-on (no blip); with `manual_off` on, no blip.

- [ ] **Step 5: Verify hold + cancel (Feature 2).** Set `person.daniel` to an away state (Developer Tools → States set `person.daniel` to `not_home`, or leave the geofence). Fire a non-critical alert — easiest: call `script.bedroom_notify` from Developer Tools → Actions with:
  ```yaml
  title: Test offline
  message: Test sensor is offline
  tag: test_hold
  ```
  Expected: NO phone push; a persistent notification `hold_test_hold` appears (Developer Tools → States → `persistent_notification.hold_test_hold`). Now fire its recovery — call `script.bedroom_notify` again with `tag: test_hold` and `recovery: true`. Expected: the `hold_test_hold` persistent notification disappears; no push.

- [ ] **Step 6: Verify the flush digest.** Still away, fire a held alert (as in Step 5, `tag: test_hold2`, no recovery) and confirm `hold_test_hold2` exists. Then set `person.daniel` back to `home`. Expected: one push titled "🏠 While you were out (1)" with the bulleted message, and `hold_test_hold2` is dismissed afterwards. Arriving again with nothing held → no push.

- [ ] **Step 7: Verify pierce bypass.** Set `person.daniel` away, call `script.bedroom_notify` with `tag: test_pierce`, `pierce: true`, `message: Test severe`. Expected: immediate phone push (not held); a bare `persistent_notification.test_pierce` (no `hold_` prefix) is created by the existing pierce path — and it is NOT picked up by a later flush (flush only matches `hold_*`).

- [ ] **Step 8: Clean up test artifacts.** Dismiss any leftover test persistent notifications (`persistent_notification.dismiss` for `test_pierce`, etc.) and restore `person.daniel`'s real state. No commit unless Steps 4-7 surfaced a fix.

## Self-Review

**Spec coverage:**
- Feature 1 blip (script + inverted-gate automation, guards, no-loop rationale) → Task 1. ✓
- Feature 2 critical=`pierce`, away fail-open, hold-as-persistent-notification, recovery cancels → Task 2. ✓
- Recovery call-sites marked → Task 3 (4 sites; spec sampled 3, zigbee added for consistency). ✓
- Summary digest on arrival, silent when empty, phone-only loses buttons → Task 4. ✓
- Edge cases (restart loses queue; alert-before-leaving; pierce no collision; empty arrival) → documented in Task 4 CLAUDE.md + verified Task 5 Steps 6-7. ✓
- Spec's testing/verification section → Task 5. ✓

**Placeholder scan:** no TBD/TODO; every code step shows full YAML; every command has expected output. ✓

**Type/name consistency:** `hold_<tag>` prefix, `recovery` field name, `away` expression, `script.bedroom_blip`, `automation.bedroom_presence_blip_too_bright` (alias "Bedroom presence blip too bright"), `automation.bedroom_flush_held_notifications` — all consistent across Tasks 2-5. The flush `selectattr('object_id', 'match', 'hold_')` matches the create's `notification_id: "hold_{{ tag }}"`. ✓
