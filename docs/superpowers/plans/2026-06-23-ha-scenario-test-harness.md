# HA Scenario Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-tap, in-HA way to exercise the bedtime fade, wake ramp, nightlight, away, arrive, and reset behaviors on demand (compressed or real speed) by driving the real automations/scripts, plus offline regression tests for the away/arrive selection logic.

**Architecture:** A small test subsystem alongside the production bedroom config: two helpers, a dispatcher script (`bedroom_run_scenario`), one new test-only preview script, a DRY extraction (`bedroom_clear_overrides`), and a dashboard card. It *drives* existing sanctioned paths (mediators, `automation.trigger`, the bedtime/wake scripts) — it never reimplements behavior. Phase 2 extracts the away/arrive inline templates into tested Jinja macros.

**Tech Stack:** Home Assistant YAML (automations/scripts/scenes copy-deployed), Jinja2 macros in `custom_templates/`, Ansible deploy, pytest macro tests, the repo's `validate_ha_config.py` + `ha_state_model.py` guardrails.

## Global Constraints

- **Edit only `ansible/roles/containers/home-assistant/`** — `containers/` is generated/read-only.
- **HA Jinja (`{{ }}`/`{% %}`) lives only in copy-deployed files** (`files/*.yaml`, `files/custom_templates/*.jinja`). NEVER put HA Jinja in `templates/configuration.yaml.j2` (Ansible renders it). Helper *definitions* (plain YAML) are fine in `configuration.yaml.j2`.
- **Mediator `reason` vocabulary (quoted strings only):** `script.bedroom_lights_set` ∈ `{presence, natural, wake, off}`; `script.bedroom_fan_set` ∈ `{auto, boost, off}`. Unquoted `off`/`on` becomes a YAML bool → validator fails.
- **Quote `"off"`** everywhere it's a literal value (input_select option, `reason:`) — unquoted `off` = YAML `false`.
- **New direct writer of `light.bedroom_lights`/`fan.tower_fan`** → add to `state/sanctioned_writers.yml` (`exemptions:`). **New writer of an override boolean** (`bedroom_manual_off`/`bedroom_fan_manual`/`bedroom_sleep_mode`) → add to `state/expected_override_writers.yml`. Both checks are **symmetric** — a declared writer that *no longer* writes must be **removed**, or CI fails. `scene.turn_on` of a scene that sets a light **counts as a write** to that light.
- **After any change to the write model, regenerate the state model:** `uv run python scripts/ha_state_model.py generate`, then `git add` `state/derived_state.yml` + `state/STATE.md` (a content-based freshness gate fails the build if they're stale).
- **Validate (must exit 0, prints `HA state-model OK` and no structural errors):** `uv run python scripts/validate_ha_config.py`
- **Macro tests:** `uv run pytest ansible/roles/containers/home-assistant/tests -v`
- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA, ~120s)
- **Post-deploy gate:** `uv run python scripts/probe.py ha verify-automations` (exit 0 = every automation in `files/automations.yaml` loaded + not unavailable)
- **Commit directly to `master`** (no feature branches). Commit explicit paths only.

---

### Task 1: DRY extraction — `script.bedroom_clear_overrides`

A behavior-preserving refactor: pull the morning reset's override-clearing into a shared script so the test `reset` scenario (Task 5) and the morning reset use one source of truth. **No functional change** — the morning reset does exactly what it did before, via a delegated script.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (add script after `bedroom_exit_sleep`, ~line 206)
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml:356-366` (morning reset delegates)
- Modify: `ansible/roles/containers/home-assistant/state/expected_override_writers.yml` (swap morning-reset → clear_overrides in all 3 keys)
- Regenerate: `ansible/roles/containers/home-assistant/state/derived_state.yml` + `state/STATE.md`

**Interfaces:**
- Produces: `script.bedroom_clear_overrides` — no fields; turns off the 3 override booleans + AL sleep mode switch. Consumed by Task 5's `reset` scenario and by `automation.bedroom_morning_reset_and_wake`.

- [ ] **Step 1: Add `bedroom_clear_overrides` to `files/scripts.yaml`** (insert immediately after the `bedroom_exit_sleep` script block, before the `bedroom_alert_pulse` comment at line 208):

```yaml
# Clear every overnight override: the three coordination booleans (manual-off, fan-manual, sleep
# mode) + Adaptive Lighting's sleep mode. SINGLE source of truth shared by
# automation.bedroom_morning_reset (daily hygiene) and the test harness's `reset` scenario
# (script.bedroom_run_scenario), so "return the room to a clean baseline" lives in ONE place. Does
# NOT re-apply lights/fan — each caller does that itself (the morning reset relights only on the
# alarm trigger; the test reset re-applies both).
bedroom_clear_overrides:
  alias: "Bedroom — clear overnight overrides"
  description: >-
    Turn off the three override booleans (bedroom_manual_off, bedroom_fan_manual, bedroom_sleep_mode)
    and Adaptive Lighting's sleep mode. Shared by the morning reset and the test-harness reset.
  mode: single
  sequence:
    - service: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.bedroom_manual_off
          - input_boolean.bedroom_fan_manual
          - input_boolean.bedroom_sleep_mode
    - service: switch.turn_off
      target:
        entity_id: switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
```

- [ ] **Step 2: Replace the morning reset's inline clearing with the delegated call.** In `files/automations.yaml`, replace lines 356-366 (the comment + the `input_boolean.turn_off` step + the `switch.turn_off` step):

Replace:
```yaml
    # Daily hygiene (BOTH triggers): clear every overnight override so sleep state / manual-off can't
    # persist into the day. One turn_off over the list covers all three input_booleans.
    - service: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.bedroom_manual_off
          - input_boolean.bedroom_fan_manual
          - input_boolean.bedroom_sleep_mode
    - service: switch.turn_off
      target:
        entity_id: switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
```
With:
```yaml
    # Daily hygiene (BOTH triggers): clear every overnight override (3 booleans + AL sleep) via the
    # shared script so sleep state / manual-off can't persist into the day. Single source of truth
    # with the test-harness reset (script.bedroom_clear_overrides).
    - service: script.bedroom_clear_overrides
```

- [ ] **Step 3: Update `state/expected_override_writers.yml`** — `automation.bedroom_morning_reset_and_wake` no longer writes the booleans directly (it delegates), so remove it from all three keys and add `script.bedroom_clear_overrides`. Replace the whole file body (keep the header comment) with:

```yaml
input_boolean.bedroom_fan_manual:
  - automation.bedroom_fan_manual_override_detect
  - automation.bedroom_fan_startup_reconcile
  - automation.bedroom_tap_dial_control
  - script.bedroom_clear_overrides
  - script.bedroom_fan_nudge
  - script.bedroom_fan_set
input_boolean.bedroom_manual_off:
  - automation.bedroom_manual_light_detect
  - automation.bedroom_tap_dial_control
  - script.bedroom_clear_overrides
input_boolean.bedroom_sleep_mode:
  - automation.bedroom_tap_dial_control
  - script.bedroom_bedtime
  - script.bedroom_clear_overrides
  - script.bedroom_exit_sleep
```

- [ ] **Step 4: Regenerate the state model**

Run: `uv run python scripts/ha_state_model.py generate`
Expected: rewrites `state/derived_state.yml` + `state/STATE.md` (the morning-reset → clear_overrides writer change is now reflected).

- [ ] **Step 5: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, prints `HA state-model OK`. (Confirms the symmetric override-writer check passes with the swap and the regenerated files are fresh.)

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
        ansible/roles/containers/home-assistant/files/automations.yaml \
        ansible/roles/containers/home-assistant/state/expected_override_writers.yml \
        ansible/roles/containers/home-assistant/state/derived_state.yml \
        ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "refactor(ha): extract bedroom_clear_overrides shared by morning reset + test reset

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Parameterize the bedtime fade

Add an optional `fade` field to `script.bedroom_bedtime` so the harness can pass a compressed (30s) fade. **Additive** — every existing caller passes no `fade`, so the default keeps today's 30-min fade.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (the `bedroom_bedtime` script, lines 442-474)

**Interfaces:**
- Produces: `script.bedroom_bedtime(fade=<seconds>)` — optional `fade`, default `1800`. Consumed by Task 5's `bedtime` scenario.

- [ ] **Step 1: Add the `fields:` block.** In `files/scripts.yaml`, after the `bedroom_bedtime` `mode: single` line (line 447), insert:

```yaml
  fields:
    fade:
      description: Nightlight fade duration in seconds (default 1800 = 30 min).
      required: false
      example: 1800
```

- [ ] **Step 2: Make the fade transition read `fade`.** In the same script, change the nightlight `scene.turn_on` transition (line 465):

Replace:
```yaml
      data:
        transition: 1800
```
With:
```yaml
      data:
        transition: "{{ fade | default(1800) | int }}"
```

- [ ] **Step 3: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`. (No writer change — confirms the templated transition parses.)

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml
git commit -m "feat(ha): optional fade param on bedroom_bedtime (default 1800)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `script.bedroom_preview_wake` — compressed wake-ramp preview

A test-only script that sweeps the wake ramp's brightness frames in ~30s, reusing the already-tested `wake_brightness` macro. Writes the light directly (like `bedroom_apply_wake`), so it needs a sanctioned-writer exemption.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (add script after `bedroom_apply_wake`, ~line 116)
- Modify: `ansible/roles/containers/home-assistant/state/sanctioned_writers.yml` (light exemption)
- Regenerate: `state/derived_state.yml` + `state/STATE.md`

**Interfaces:**
- Consumes: `wake_brightness(elapsed_min, sleep_min)` macro from `lighting.jinja` (existing — `elapsed_min` 0→30, returns brightness %).
- Produces: `script.bedroom_preview_wake` — no fields. Consumed by Task 5's `wake` scenario (fast).

- [ ] **Step 1: Add `bedroom_preview_wake` to `files/scripts.yaml`** (insert immediately after the `bedroom_apply_wake` script block, before the `bedroom_apply_natural` comment at line 117):

```yaml
# Test-only: preview the morning wake ramp on demand, COMPRESSED. Sweeps the 30-min window's frames
# (elapsed 0 -> 15 -> 30 = window start -> alarm -> alarm+15) using the SAME tested wake_brightness
# macro the production ramp uses, applying each with a short fade so you watch 1% -> ~12% -> 40% in
# ~30s — without waiting for a real alarm or touching automation.bedroom_wake_ramp. Warm 2200K, like
# bedroom_apply_wake. Driven only by script.bedroom_run_scenario (the test harness).
bedroom_preview_wake:
  alias: "Bedroom — preview wake ramp (test, compressed)"
  description: >-
    Sweep the wake ramp's brightness frames in ~30s for testing, reusing the wake_brightness macro.
    Not part of the production wake path; invoked by the scenario test harness only.
  mode: restart
  sequence:
    - repeat:
        for_each: [0, 7.5, 15, 22.5, 30]
        sequence:
          - variables:
              target: "{% from 'lighting.jinja' import wake_brightness %}{{ wake_brightness(repeat.item, states('sensor.pixel_9_pro_sleep_duration') | float(0)) | int }}"
          - service: light.turn_on
            target:
              entity_id: light.bedroom_lights
            data:
              brightness_pct: "{{ target }}"
              color_temp_kelvin: 2200
              transition: 5
          - delay: "00:00:06"
```

- [ ] **Step 2: Add the sanctioned-writer exemption.** In `state/sanctioned_writers.yml`, add `script.bedroom_preview_wake` to the `light.bedroom_lights:` `exemptions:` list (append after `automation.bedroom_color_tracking`):

```yaml
    - automation.bedroom_color_tracking
    - script.bedroom_preview_wake
```

- [ ] **Step 3: Regenerate the state model**

Run: `uv run python scripts/ha_state_model.py generate`
Expected: `state/derived_state.yml` + `state/STATE.md` now list `script.bedroom_preview_wake` as a writer of `light.bedroom_lights`.

- [ ] **Step 4: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`. (Confirms the new writer is sanctioned and the macro reference resolves.)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
        ansible/roles/containers/home-assistant/state/sanctioned_writers.yml \
        ansible/roles/containers/home-assistant/state/derived_state.yml \
        ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha): bedroom_preview_wake — compressed wake-ramp preview for testing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Scenario helpers (`input_select` + `input_boolean`)

Add the two helpers the harness reads. Plain YAML, so they live in the Ansible-templated `configuration.yaml.j2` (no HA Jinja).

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (the `input_boolean:` block ~line 51-64)

**Interfaces:**
- Produces: `input_select.bedroom_test_scenario` (options: off/bedtime/wake/nightlight/away/arrive/reset) and `input_boolean.bedroom_test_fast`. Consumed by Task 5 (`bedroom_run_scenario`) and Task 6 (dashboard).

- [ ] **Step 1: Add `bedroom_test_fast` to the `input_boolean:` block.** In `configuration.yaml.j2`, after the `bedroom_sleep_mode` entry (lines 62-64):

Replace:
```yaml
  bedroom_sleep_mode:
    name: Bedroom sleep mode
    icon: mdi:power-sleep
```
With:
```yaml
  bedroom_sleep_mode:
    name: Bedroom sleep mode
    icon: mdi:power-sleep
  # Test-harness speed toggle (script.bedroom_run_scenario): on = compressed (~30s), off = real timing.
  bedroom_test_fast:
    name: Bedroom test fast mode
    icon: mdi:fast-forward
```

- [ ] **Step 2: Add the `input_select:` block.** Insert a new top-level block immediately after the `input_boolean:` block (i.e. right before the `input_number:` comment that begins at line 66 — `# Helper: the fan LEVEL script...`):

```yaml
# Test-harness scenario picker (script.bedroom_run_scenario): pick a scenario, then press the
# dashboard Run button. `reset` returns the room to a clean daytime baseline. `off` does nothing.
input_select:
  bedroom_test_scenario:
    name: Bedroom test scenario
    icon: mdi:test-tube
    options:
      - "off"
      - bedtime
      - wake
      - nightlight
      - away
      - arrive
      - reset
```

- [ ] **Step 3: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`. (Confirms the helpers parse and `input_boolean.bedroom_test_fast` resolves as a known entity.)

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2
git commit -m "feat(ha): scenario test-harness helpers (input_select + fast toggle)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `script.bedroom_run_scenario` — the dispatcher

The heart of the harness. Reads the picker + fast flag, drives the real scripts/automations per scenario, narrates via `persistent_notification`. Its only *direct* light write is the nightlight `scene.turn_on`, so it needs a sanctioned-writer exemption.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (add script at end of file, after `bedroom_notify`)
- Modify: `ansible/roles/containers/home-assistant/state/sanctioned_writers.yml` (light exemption)
- Regenerate: `state/derived_state.yml` + `state/STATE.md`

**Interfaces:**
- Consumes: `input_select.bedroom_test_scenario`, `input_boolean.bedroom_test_fast` (Task 4); `script.bedroom_bedtime(fade)` (Task 2); `script.bedroom_preview_wake` (Task 3); `script.bedroom_clear_overrides` (Task 1); `script.bedroom_lights_set(reason)` / `script.bedroom_fan_set(reason)` / `script.bedroom_apply_wake` (existing); `automation.bedroom_away` / `automation.bedroom_arrive_home` (existing); `scene.bedroom_nightlight` (existing).
- Produces: `script.bedroom_run_scenario` — no fields. Consumed by Task 6's dashboard button.

- [ ] **Step 1: Add `bedroom_run_scenario` to the end of `files/scripts.yaml`** (after the `bedroom_notify` block, which ends at line 576):

```yaml
# Test harness: run the selected scenario (input_select.bedroom_test_scenario) at the selected speed
# (input_boolean.bedroom_test_fast on = compressed ~30s). Drives the REAL production
# scripts/automations so what you see is what fires for real — never a reimplementation. Narrates each
# run as a persistent_notification (id test_scenario, updates in place). `reset` returns the room to a
# clean daytime baseline. Invoked by the dashboard Run button (ui-lovelace.yaml.j2). Its only DIRECT
# actuator write is the nightlight scene.turn_on (hence the sanctioned_writers exemption); every other
# branch goes through a mediator/module script or automation.trigger.
bedroom_run_scenario:
  alias: "Bedroom — run test scenario"
  description: >-
    Exercise a bedroom scenario on demand (bedtime/wake/nightlight/away/arrive/reset) at compressed or
    real speed, driving the real scripts/automations — for testing without waiting for night or
    leaving home.
  mode: restart
  sequence:
    - variables:
        scenario: "{{ states('input_select.bedroom_test_scenario') }}"
        fast: "{{ is_state('input_boolean.bedroom_test_fast', 'on') }}"
    - choose:
        # Bedtime fade — fast passes a 30s fade; real uses the production 30-min fade.
        - conditions: "{{ scenario == 'bedtime' }}"
          sequence:
            - service: script.bedroom_bedtime
              data:
                fade: "{{ 30 if fast else 1800 }}"
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: bedtime ({{ 'fast' if fast else 'real' }})"
                message: >-
                  Engaged sleep mode + AL sleep; fading to amber 3% over
                  {{ '30s' if fast else '30 min' }}; fan capped to Low. Run `reset` to clear.
        # Wake ramp — fast sweeps the frames in ~30s; real applies the current frame only (the
        # production per-minute ramp is alarm-driven — set a morning watch alarm to see it live).
        - conditions: "{{ scenario == 'wake' }}"
          sequence:
            - if: "{{ fast }}"
              then:
                - service: script.bedroom_preview_wake
              else:
                - service: script.bedroom_apply_wake
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: wake ({{ 'fast' if fast else 'real' }})"
                message: >-
                  {{ 'Swept the wake ramp 1% to 12% to 40% in ~30s (warm 2200K).' if fast
                     else 'Applied the current wake frame. The real ramp is alarm-driven — set a
                     morning watch alarm to see it live.' }}
        # Nightlight — the dim amber "got up overnight" scene, instantly.
        - conditions: "{{ scenario == 'nightlight' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_nightlight
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: nightlight"
                message: "Applied scene.bedroom_nightlight (amber 3%). Run `reset` to return to normal."
        # Away — ensure the lights are on (so there's something to turn off), then trigger the REAL
        # away automation (skip_condition: its 10-min person trigger can't be faked).
        - conditions: "{{ scenario == 'away' }}"
          sequence:
            - if: "{{ is_state('light.bedroom_lights', 'off') }}"
              then:
                - service: script.bedroom_lights_set
                  data:
                    reason: "natural"
                - delay: "00:00:02"
            - service: automation.trigger
              target:
                entity_id: automation.bedroom_away
              data:
                skip_condition: true
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: away"
                message: >-
                  Triggered the away response: bedroom lights + fan off, "Left on" notification sent.
                  (The away notification-HOLD path needs a real away state — set person.daniel to
                  not_home in Developer Tools then Set State to test that.)
        # Arrive home — trigger the real arrive automation (its inline conditions still run).
        - conditions: "{{ scenario == 'arrive' }}"
          sequence:
            - service: automation.trigger
              target:
                entity_id: automation.bedroom_arrive_home
              data:
                skip_condition: true
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: arrive"
                message: >-
                  Triggered arrive-home: fan resumes per temperature; lights re-check only if FP300
                  present, not manual-off, and currently off.
        # Reset — clear every override and return to a clean daytime baseline.
        - conditions: "{{ scenario == 'reset' }}"
          sequence:
            - service: script.bedroom_clear_overrides
            - service: script.bedroom_lights_set
              data:
                reason: "natural"
            - service: script.bedroom_fan_set
              data:
                reason: "auto"
            - service: persistent_notification.create
              data:
                notification_id: test_scenario
                title: "🧪 Scenario: reset"
                message: "Cleared overrides + AL sleep; re-applied natural lighting + temperature fan."
      default:
        - service: persistent_notification.create
          data:
            notification_id: test_scenario
            title: "🧪 Scenario: none"
            message: "Pick a scenario (not 'off') in the picker, then press Run."
```

- [ ] **Step 2: Add the sanctioned-writer exemption.** In `state/sanctioned_writers.yml`, append `script.bedroom_run_scenario` to the `light.bedroom_lights:` `exemptions:` list (after the `script.bedroom_preview_wake` line added in Task 3):

```yaml
    - script.bedroom_preview_wake
    - script.bedroom_run_scenario
```

- [ ] **Step 3: Regenerate the state model**

Run: `uv run python scripts/ha_state_model.py generate`
Expected: `state/derived_state.yml` + `state/STATE.md` now list `script.bedroom_run_scenario` as a writer of `light.bedroom_lights` (the nightlight scene.turn_on).

- [ ] **Step 4: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`. (Confirms: every mediator `reason` is in-vocabulary; the new light writer is sanctioned; all referenced scripts/scenes resolve; the inline templates parse.)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
        ansible/roles/containers/home-assistant/state/sanctioned_writers.yml \
        ansible/roles/containers/home-assistant/state/derived_state.yml \
        ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha): bedroom_run_scenario test-harness dispatcher

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Dashboard card

Add a "🧪 Test scenarios" card to the bedroom dashboard with the picker, the fast toggle, and a Run button.

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/ui-lovelace.yaml.j2` (append a card after the FP300 glance, line 167)

**Interfaces:**
- Consumes: `input_select.bedroom_test_scenario`, `input_boolean.bedroom_test_fast` (Task 4); `script.bedroom_run_scenario` (Task 5).

- [ ] **Step 1: Append the card.** In `ui-lovelace.yaml.j2`, after the final FP300 `glance` card (the `- entity: sensor.aqara_fp300_battery` / `name: Battery` block ending at line 166), add a new card at the same `cards:` indent level (6 spaces):

```yaml
      # ── 🧪 Test scenarios (dev harness — script.bedroom_run_scenario) ─────
      - type: entities
        title: 🧪 Test scenarios
        icon: mdi:test-tube
        entities:
          - entity: input_select.bedroom_test_scenario
            name: Scenario
          - entity: input_boolean.bedroom_test_fast
            name: Fast (compressed)
          - type: button
            name: Run scenario
            icon: mdi:play
            action_name: Run
            tap_action:
              action: perform-action
              perform_action: script.bedroom_run_scenario
```

- [ ] **Step 2: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`. (Confirms the dashboard YAML parses.)

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/ui-lovelace.yaml.j2
git commit -m "feat(ha): dashboard card for the scenario test harness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Deploy + live acceptance (Phase 1)

Deploy the harness and verify it loaded, then walk the scenarios live (manual acceptance — the agent deploys + runs the automated gate; the operator drives the dashboard).

**Files:** none (deploy + verify)

- [ ] **Step 1: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: play completes; HA container recreated (~120s).

- [ ] **Step 2: Gate on automations loading**

Run: `uv run python scripts/probe.py ha verify-automations`
Expected: exit 0 (every automation in `files/automations.yaml` loaded + not unavailable — confirms the morning-reset edit didn't break it).

- [ ] **Step 3: Confirm the new scripts loaded**

Run: `uv run python scripts/probe.py ha state script.bedroom_run_scenario`
Expected: state `off` (idle) — the script entity exists. Repeat for `script.bedroom_preview_wake` and `script.bedroom_clear_overrides`.

- [ ] **Step 4: Manual acceptance (operator).** On the Bedroom dashboard, in "🧪 Test scenarios", with **Fast** on, run each scenario and confirm:
  - **bedtime** → lights fade to amber ~3% over ~30s; sleep-mode toggle turns on; `test_scenario` notification appears.
  - **wake** → lights sweep 1% → ~12% → 40% (warm) over ~30s.
  - **nightlight** → instant dim amber.
  - **away** → (lights come on first if off) then lights + fan turn off; a "🏠 Left on" notification fires.
  - **arrive** → fan resumes per temperature; lights only relight if you're in the room.
  - **reset** → overrides clear, lights return to natural, fan to its temperature band.
  Then toggle **Fast** off and run **bedtime** to confirm the real 30-min fade begins.

- [ ] **Step 5: No commit** (verification only). If a scenario misbehaves, fix the relevant task's file, re-run its `validate_ha_config.py`, redeploy, and re-verify.

---

### Task 8: Phase 2 — away/arrive regression macros (TDD)

Extract the away/arrive selection logic into pure, tested macros (the night-cycle math is already covered by `wake_brightness`/`natural_exception`). Classic TDD: failing test → macro → passing test → rewire callers.

**Files:**
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py` (add tests)
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` (add 2 macros)
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (`bedroom_away` ~918-937; `bedroom_arrive_home` ~961-964)

**Interfaces:**
- Consumes: `render_macro` from `jinja_harness` (existing test helper).
- Produces: `away_items_label(light_on, fan_on)` → joined string (`"lights + fan"` / `"lights"` / `"fan"` / `""`); `arrive_relight_allowed(presence, manual_off, light_on)` → `"True"`/`"False"`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_lighting_macros.py`:

```python
def _away_label(light_on, fan_on):
    return render_macro(LIGHT, "away_items_label", light_on, fan_on)


def test_away_items_label_truth_table():
    assert _away_label(True, True) == "lights + fan"
    assert _away_label(True, False) == "lights"
    assert _away_label(False, True) == "fan"
    assert _away_label(False, False) == ""   # nothing on -> gate stays silent


def _arrive(presence, manual_off, light_on):
    return render_macro(LIGHT, "arrive_relight_allowed", presence, manual_off, light_on)


def test_arrive_relight_allowed_truth_table():
    assert _arrive(True, False, False) == "True"    # present, not blocked, lights off -> relight
    assert _arrive(False, False, False) == "False"  # not in the room
    assert _arrive(True, True, False) == "False"    # manual-off engaged
    assert _arrive(True, False, True) == "False"    # already on -> never re-stomp
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -k "away_items_label or arrive_relight_allowed" -v`
Expected: FAIL — `UndefinedError` / template error (macros `away_items_label`, `arrive_relight_allowed` not defined in `lighting.jinja`).

- [ ] **Step 3: Add the macros.** Append to `files/custom_templates/lighting.jinja` (after the `light_decision` macro, end of file):

```jinja
{# Away shut-off label: the human-readable list of what's on — used BOTH as the bedroom_away gate
   (empty string = nothing on = stay silent) and as the "Left on" message body. Pure (bools in,
   joined string out) so the selection is truth-tabled, not just inline. #}
{%- macro away_items_label(light_on, fan_on) -%}
{%- set items = (['lights'] if (light_on | bool) else []) + (['fan'] if (fan_on | bool) else []) -%}
{{ items | join(' + ') }}
{%- endmacro -%}

{# Arrive-home relight gate: relight only if you're physically in the room (FP300 present), have NOT
   engaged manual-off, and the lights are currently off (never re-stomp an on light). Pure bool. #}
{%- macro arrive_relight_allowed(presence, manual_off, light_on) -%}
{{ (presence | bool) and not (manual_off | bool) and not (light_on | bool) }}
{%- endmacro -%}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -k "away_items_label or arrive_relight_allowed" -v`
Expected: PASS (both tests green).

- [ ] **Step 5: Rewire `bedroom_away` to use the macro.** In `files/automations.yaml`, in the `bedroom_away` action: replace the `on_items` variable + the gate condition + the message.

Replace (lines ~918-921):
```yaml
    - variables:
        on_items: >-
          {{ (['lights'] if is_state('light.bedroom_lights', 'on') else [])
           + (['fan'] if is_state('fan.tower_fan', 'on') else []) }}
```
With:
```yaml
    - variables:
        on_label: >-
          {% from 'lighting.jinja' import away_items_label %}{{ away_items_label(is_state('light.bedroom_lights', 'on'), is_state('fan.tower_fan', 'on')) }}
```
Replace the gate condition (line ~925):
```yaml
        - conditions: "{{ on_items | length > 0 }}"
```
With:
```yaml
        - conditions: "{{ on_label != '' }}"
```
Replace the message (line ~937):
```yaml
                message: "Turned off bedroom {{ on_items | join(' + ') }} (you're away)"
```
With:
```yaml
                message: "Turned off bedroom {{ on_label }} (you're away)"
```

- [ ] **Step 6: Rewire `bedroom_arrive_home` to use the macro.** In `files/automations.yaml`, replace the inline relight gate (lines ~961-964):

Replace:
```yaml
    - if: >-
        {{ is_state('binary_sensor.aqara_fp300_presence', 'on')
           and is_state('input_boolean.bedroom_manual_off', 'off')
           and is_state('light.bedroom_lights', 'off') }}
```
With:
```yaml
    - if: >-
        {% from 'lighting.jinja' import arrive_relight_allowed %}{{ arrive_relight_allowed(
           is_state('binary_sensor.aqara_fp300_presence', 'on'),
           is_state('input_boolean.bedroom_manual_off', 'on'),
           is_state('light.bedroom_lights', 'on')) | bool }}
```
(Note: the macro takes `manual_off` as "is it engaged" — pass `is_state(..., 'on')` — and the `| bool` coercion is required because the macro renders a string, where `"False"` is truthy.)

- [ ] **Step 7: Regenerate + validate + full test run.** Writes are unchanged (same mediators), but regen to satisfy the freshness gate if the model shifted, then validate and run the whole macro suite.

Run: `uv run python scripts/ha_state_model.py generate`
Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, `HA state-model OK`.
Run: `uv run pytest ansible/roles/containers/home-assistant/tests -v`
Expected: all tests pass (existing + the 2 new).

- [ ] **Step 8: Deploy + verify**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Run: `uv run python scripts/probe.py ha verify-automations`
Expected: deploy completes; verify-automations exits 0 (away/arrive still loaded after the rewire).

- [ ] **Step 9: Commit**

```bash
git add ansible/roles/containers/home-assistant/tests/test_lighting_macros.py \
        ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja \
        ansible/roles/containers/home-assistant/files/automations.yaml \
        ansible/roles/containers/home-assistant/state/derived_state.yml \
        ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "test(ha): tested away/arrive selection macros + rewire callers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Helpers (input_select + fast toggle) → Task 4 ✓
- Dispatcher `bedroom_run_scenario` with all 6 scenarios → Task 5 ✓
- Bedtime `fade` param → Task 2 ✓; `bedroom_preview_wake` frame-sweep reusing `wake_brightness` → Task 3 ✓; away/arrive via `automation.trigger` → Task 5 ✓; `bedroom_clear_overrides` DRY extraction → Task 1 ✓; reset scenario → Task 5 ✓
- Dashboard card → Task 6 ✓
- Narration via `persistent_notification` (id `test_scenario`) → Task 5 ✓
- Away response-only (option A); hold-path documented as a manual check → Task 5 away-scenario notification text ✓
- Guardrails: sanctioned_writers exemptions (preview_wake T3, run_scenario T5), expected_override_writers swap (T1), mediator reasons quoted+in-vocab (T5), copy-not-template (helpers in .j2, Jinja in files), regen+freshness (T1/T3/T5/T8) ✓
- Phase 2 away/arrive macros + tests → Task 8 ✓ (night-cycle math already tested — noted, no task needed)
- Live acceptance → Task 7 ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete content; commands have expected output. ✓

**Type/name consistency:** `bedroom_clear_overrides` (T1) consumed in T5 reset + the morning reset; `fade` field (T2) consumed in T5 bedtime; `bedroom_preview_wake` (T3) consumed in T5 wake; `input_select.bedroom_test_scenario`/`input_boolean.bedroom_test_fast` (T4) consumed in T5 + T6; `away_items_label`/`arrive_relight_allowed` (T8) defined before use; mediator reasons (`"natural"`, `"auto"`) verified against `MEDIATOR_REASONS`. ✓
