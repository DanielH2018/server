# Fast-Mode Scenario Auto-Revert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fast-mode scenario tests ephemeral — snapshot the room before, run the scenario, then restore the exact prior state (lights, fan, and the sticky flags), so things like `sleep_mode` don't stick.

**Architecture:** Wrap the existing per-scenario `choose:` in `script.bedroom_run_scenario` with a snapshot block (before) and a restore block (after), both gated on `wrap = fast and scenario ∈ {bedtime,wake,nightlight,away,arrive}`. Snapshot = four `states()` reads for the flags + `scene.create` for the visible light/fan state; restore = `scene.turn_on` of that snapshot + setting each flag back. Real mode and `reset`/`off` are untouched.

**Tech Stack:** Home Assistant YAML scripts (copy-deployed), the repo's `validate_ha_config.py` + `ha_state_model.py` state-model guards, Ansible deploy.

## Global Constraints

- **Edit only `ansible/roles/containers/home-assistant/`.** HA Jinja lives in copy-deployed `files/*` (this is `files/scripts.yaml`).
- **`wrap` set = `['bedtime','wake','nightlight','away','arrive']`** — NOT `reset` (the baseline) or `off`/unknown.
- **Observe windows (seconds):** `bedtime=30`, `wake=1`, all others `4`.
- **State-model guard (symmetric — exact match required):** the restore's `input_boolean.turn_on/off` calls make `script.bedroom_run_scenario` a static writer of all three override booleans → it MUST be added to `state/expected_override_writers.yml` for `bedroom_sleep_mode`, `bedroom_manual_off`, `bedroom_fan_manual`. **Do NOT add a `fan.tower_fan` exemption** — `scene.turn_on` of the runtime-created `bedroom_pre_test` scene is NOT a tracked write (it hits `record()`'s `elif svc.startswith("scene."): continue` branch because the scene isn't in the static `scene_map`); adding one would be a stale entry that fails `single_writer_errors`. The existing `light.bedroom_lights` exemption STAYS (needed for the `nightlight` branch's static-scene `scene.turn_on`).
- **After the change, regenerate the state model:** `uv run python scripts/ha_state_model.py generate`, commit `state/derived_state.yml` + `state/STATE.md`.
- **Validate (must exit 0, `HA state-model OK`):** `uv run python scripts/validate_ha_config.py`. If it names a missing/stale writer, reconcile `expected_override_writers.yml` to exactly match and re-run.
- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`. **Post-deploy gate:** `uv run python scripts/probe.py ha verify-automations`.
- **Commit directly to `master`** via explicit pathspec (a concurrent session may be active).

---

### Task 1: Snapshot/restore wrap + override-writer declaration

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (the `bedroom_run_scenario` script: header comment + `variables:`/snapshot before the `choose:`, restore after it)
- Modify: `ansible/roles/containers/home-assistant/state/expected_override_writers.yml` (add `script.bedroom_run_scenario` to all three keys)
- Regenerate: `state/derived_state.yml` + `state/STATE.md`

**Interfaces:**
- Consumes: existing helpers `input_select.bedroom_test_scenario`/`bedroom_test_speed`; existing scripts/scenes; the `scene.create` snapshot pattern.
- Produces: `bedroom_run_scenario` now restores prior state in fast mode for the five wrappable scenarios.

- [ ] **Step 1: Update the `bedroom_run_scenario` header comment.** In `files/scripts.yaml`, replace the comment block above `bedroom_run_scenario:`:

Replace:
```yaml
# Test harness: run the selected scenario (input_select.bedroom_test_scenario) at the selected speed
# (input_select.bedroom_test_speed = fast -> compressed ~30s, real -> production timing). Drives the REAL
# scripts/automations so what you see is what fires for real — never a reimplementation. Narrates each
# run as a persistent_notification (id test_scenario, updates in place). `reset` returns the room to a
# clean daytime baseline. Invoked by the dashboard Run button (ui-lovelace.yaml.j2). Its only DIRECT
# actuator write is the nightlight scene.turn_on (hence the sanctioned_writers exemption); every other
# branch goes through a mediator/module script or automation.trigger.
```
With:
```yaml
# Test harness: run the selected scenario (input_select.bedroom_test_scenario) at the selected speed
# (input_select.bedroom_test_speed = fast -> compressed ~30s + EPHEMERAL, real -> production timing + kept).
# Drives the REAL scripts/automations so what you see is what fires for real — never a reimplementation.
# FAST mode is a PREVIEW: it snapshots the room (sticky flags + light/fan via scene.create), runs the
# scenario, then restores the exact prior state after an observe window — so e.g. bedtime's sleep_mode
# doesn't stick. `reset` (the baseline) and real mode are NOT wrapped. Narrates as a persistent_notification
# (id test_scenario, updates in place). Invoked by the dashboard Run button (ui-lovelace.yaml.j2). DIRECT
# light write = the nightlight branch's scene.turn_on (sanctioned_writers light exemption); the restore
# writes the 3 override booleans (declared in expected_override_writers.yml). The bedroom_pre_test
# scene.turn_on restores the fan at runtime but is NOT a tracked write (runtime scene, not in scene_map).
```

- [ ] **Step 2: Add `wrap`/`observe` vars + the snapshot block** between the `variables:` step and the `choose:`. Replace:
```yaml
    - variables:
        scenario: "{{ states('input_select.bedroom_test_scenario') }}"
        fast: "{{ is_state('input_select.bedroom_test_speed', 'fast') }}"
    - choose:
```
With:
```yaml
    - variables:
        scenario: "{{ states('input_select.bedroom_test_scenario') }}"
        fast: "{{ is_state('input_select.bedroom_test_speed', 'fast') }}"
        # Fast = ephemeral preview: snapshot the room, run the scenario, then restore the exact prior
        # state. `reset` is the baseline (never wrapped); off/none never ran a scenario.
        wrap: "{{ fast and scenario in ['bedtime', 'wake', 'nightlight', 'away', 'arrive'] }}"
        # Observe window (s) before restore: bedtime's fade is async on the bulb (wait 30s);
        # preview_wake already blocks ~30s while it sweeps (1s buffer); the instant ones get a few.
        observe: "{{ 30 if scenario == 'bedtime' else (1 if scenario == 'wake' else 4) }}"
    # Snapshot (fast preview only): capture the four sticky flags as sequence variables (in scope for
    # the whole sequence) + the visible light/fan state via scene.create, so the restore below returns
    # the room EXACTLY to its pre-test state. Same scene.create trick as bedroom_alert_pulse.
    - if: "{{ wrap }}"
      then:
        - variables:
            snap_sleep: "{{ states('input_boolean.bedroom_sleep_mode') }}"
            snap_manual_off: "{{ states('input_boolean.bedroom_manual_off') }}"
            snap_fan_manual: "{{ states('input_boolean.bedroom_fan_manual') }}"
            snap_al_sleep: "{{ states('switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom') }}"
        - service: scene.create
          data:
            scene_id: bedroom_pre_test
            snapshot_entities:
              - light.bedroom_lights
              - fan.tower_fan
    - choose:
```

- [ ] **Step 3: Add the restore block** at the END of the `bedroom_run_scenario` sequence — immediately after the `choose:`'s `default:` block (which ends with the `"Pick a scenario (not 'off')..."` notification). Append at the `sequence:`-item indent (4 spaces):
```yaml
    # Restore (fast preview only): after the observe window, put the room back EXACTLY as it was — the
    # visible light/fan state (from the snapshot scene) + the four sticky flags. This is what makes fast
    # mode ephemeral (e.g. bedtime's sleep_mode no longer sticks). Real mode skips this entirely.
    - if: "{{ wrap }}"
      then:
        - delay: "{{ observe }}"
        - service: scene.turn_on
          target:
            entity_id: scene.bedroom_pre_test
          data:
            transition: 1
        - if: "{{ snap_sleep == 'on' }}"
          then:
            - service: input_boolean.turn_on
              target:
                entity_id: input_boolean.bedroom_sleep_mode
          else:
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_sleep_mode
        - if: "{{ snap_manual_off == 'on' }}"
          then:
            - service: input_boolean.turn_on
              target:
                entity_id: input_boolean.bedroom_manual_off
          else:
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_manual_off
        - if: "{{ snap_fan_manual == 'on' }}"
          then:
            - service: input_boolean.turn_on
              target:
                entity_id: input_boolean.bedroom_fan_manual
          else:
            - service: input_boolean.turn_off
              target:
                entity_id: input_boolean.bedroom_fan_manual
        - if: "{{ snap_al_sleep == 'on' }}"
          then:
            - service: switch.turn_on
              target:
                entity_id: switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
          else:
            - service: switch.turn_off
              target:
                entity_id: switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
        - service: persistent_notification.create
          data:
            notification_id: test_scenario
            title: "↩️ Preview restored"
            message: "Fast preview complete — restored the room to its pre-test state."
```

- [ ] **Step 4: Declare the new override-boolean writer.** Edit `state/expected_override_writers.yml` — add `script.bedroom_run_scenario` to all three keys. The file should read (keep the header comment above it):
```yaml
input_boolean.bedroom_fan_manual:
  - automation.bedroom_fan_manual_override_detect
  - automation.bedroom_fan_startup_reconcile
  - automation.bedroom_tap_dial_control
  - script.bedroom_clear_overrides
  - script.bedroom_fan_nudge
  - script.bedroom_fan_set
  - script.bedroom_run_scenario
input_boolean.bedroom_manual_off:
  - automation.bedroom_manual_light_detect
  - automation.bedroom_tap_dial_control
  - script.bedroom_clear_overrides
  - script.bedroom_run_scenario
input_boolean.bedroom_sleep_mode:
  - automation.bedroom_tap_dial_control
  - script.bedroom_bedtime
  - script.bedroom_clear_overrides
  - script.bedroom_exit_sleep
  - script.bedroom_run_scenario
```

- [ ] **Step 5: Regenerate the state model**

Run: `uv run python scripts/ha_state_model.py generate`
Expected: rewrites `state/derived_state.yml` + `state/STATE.md`. `script.bedroom_run_scenario` now appears as a writer of the three `input_boolean`s and `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`. It does NOT appear under `fan.tower_fan` (the `bedroom_pre_test` scene.turn_on is a non-tracked runtime-scene write).

- [ ] **Step 6: Validate**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exit 0, prints `HA state-model OK`. If it reports `input_boolean.bedroom_* : unsanctioned writer script.bedroom_run_scenario`, you missed a key in Step 4 — add it. If it reports a stale `fan.tower_fan` sanctioned writer, you wrongly added a fan exemption — remove it (this plan does not touch `sanctioned_writers.yml`).

- [ ] **Step 7: Commit** (explicit pathspec — a concurrent session may be active)

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
        ansible/roles/containers/home-assistant/state/expected_override_writers.yml \
        ansible/roles/containers/home-assistant/state/derived_state.yml \
        ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha): fast-mode scenarios auto-revert to pre-test state

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Deploy + live acceptance

**Files:** none (deploy + verify)

- [ ] **Step 1: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: play completes `failed=0`; HA container recreated.

- [ ] **Step 2: Wait for HA healthy, then gate on automations**

Run (background poll until healthy): `for i in $(seq 1 48); do uv run python scripts/probe.py health home-assistant 2>/dev/null | grep -q "health=healthy" && { echo healthy; exit 0; }; sleep 5; done; exit 1`
Then run: `uv run python scripts/probe.py ha verify-automations`
Expected: poll exits 0 (healthy); verify prints `all 30 automations loaded`, exit 0.

- [ ] **Step 3: Live acceptance (operator).** On the Bedroom dashboard, ensure **Speed = fast**, then:
  1. Note the current state (e.g. lights on at some level, `Sleep mode` off).
  2. Run **bedtime** → watch the ~30s fade to amber 3% + `Sleep mode` flip on → after the observe window, the lights return to their prior level and `Sleep mode` flips back off, with an "↩️ Preview restored" notification.
  3. Confirm `input_boolean.bedroom_sleep_mode` is **off** afterward (Developer Tools or the Bedroom Controls card) — i.e. it did NOT stick.
  4. Optionally run **away** (fast) → lights/fan go off + "Left on" → then restored on.
  5. Set **Speed = real**, run **bedtime** → confirm it does NOT auto-revert (sleep_mode stays on; the room stays in night mode) — proving real mode is unchanged.

- [ ] **Step 4: No commit** (verification only). If the revert misbehaves, fix `bedroom_run_scenario`, re-run Task 1 Steps 5–7, redeploy, re-verify.

---

## Self-Review

**Spec coverage:**
- Fast = ephemeral preview, real unchanged, reset/off never wrapped → Task 1 Steps 2–3 (`wrap` gate) ✓
- Snapshot (4 flags + scene.create light/fan) → Step 2 ✓
- Restore (scene.turn_on + 4 flags) after observe window → Step 3 ✓
- Observe windows bedtime 30 / wake 1 / others 4 → Step 2 `observe` ✓
- Validator gates: add run_scenario to expected_override_writers (3 keys); NO fan exemption; regen → Steps 4–6 ✓
- Narration reflects revert (↩️ Preview restored overwrite) → Step 3 ✓
- Deploy + live acceptance proving revert + real-mode-unchanged → Task 2 ✓

**Placeholder scan:** none — every step shows complete YAML/commands + expected output.

**Type/name consistency:** `wrap`/`observe`/`snap_sleep`/`snap_manual_off`/`snap_fan_manual`/`snap_al_sleep` defined in Step 2, consumed in Step 3; `scene.bedroom_pre_test` created in Step 2, turned on in Step 3; the three override-boolean entity_ids match between Step 3 (writes) and Step 4 (declaration). `fan.tower_fan` deliberately NOT in any sanctioned-writer edit (Global Constraints + Step 6 guard).
