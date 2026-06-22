# HA State Model — Phase 2 Implementation Plan (Actuator Mediator)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every AUTO/programmatic write of `light.bedroom_lights` and `fan.tower_fan` through a single guarded mediator script, with the gate decision in a unit-tested Jinja macro, then flip the Phase-1 single-writer report to a hard CI check.

**Architecture:** A thin `bedroom_lights_set(reason)` dispatcher reads live flags, calls a tested `light_decision(reason, flags…)` macro (`presence` gated; `natural`/`wake`/`off` pass-through), and delegates to the existing primitives (`apply_natural`/`apply_wake`/`light.turn_off`). A `bedroom_fan_set(reason)` does the same for the fan's off/boost/auto. The manual **Tap Dial** is a declared exemption (untouched). A hand-maintained `state/sanctioned_writers.yml` (module + exemptions per actuator) drives a hard single-writer check in `scripts/ha_state_model.py`.

**Tech Stack:** Home Assistant YAML (automations/scripts) + `custom_templates/*.jinja` macros, pytest via `uv`, the Phase-1 `ha_state_model.py` validator, prek.

## Global Constraints

- **Behavior-preserving refactor.** Each migrated caller must behave EXACTLY as before. Ungated callers (`arrive_home`, `morning_reset`, `wake_ramp`) keep their own `if`/conditions and pass an **ungated** reason; only `presence_on` (whose sole action is the light) moves its 6 conditions into the macro.
- **The macro is the only new logic; it is unit-tested.** Entity/time reads stay in the YAML caller; the macro takes plain bools/strings (coerce bools with `| bool`, per `auto_light_allowed`). Returns one of: `natural | wake | off | noop`.
- **The manual Tap Dial is exempt and is NOT modified** in this phase (`apply_natural_gated` stays — it's the Tap Dial's B1-HOLD helper).
- **`containers/` is never edited.** Only `ansible/roles/containers/home-assistant/`. HA YAML uses `copy` (not Ansible-templated) — no `{{ ansible }}`.
- **Generated state artifacts stay current**: after any automations/scripts edit, regenerate `derived_state.yml` + `STATE.md` (`ha_state_model.py generate`) and commit them — the freshness gate fails CI otherwise.
- **No live deploy inside the code tasks.** Deploy once at the end (Task 8) via the `ha-deploy` skill; behavioral verification (time/disruption-bound: wake ramp, bedtime) is handed to the operator, as the Phase-1 bedroom-lighting review did.
- Tests: `uv run pytest ansible/roles/containers/home-assistant/tests -q` (macros) and `uv run pytest scripts -q` (validator). Per-edit gate: `uv run python scripts/validate_ha_config.py`.
- **`reason: "off"` MUST be quoted.** Unquoted `off` is a YAML 1.1 boolean (`false`), so
  `reason: off` would pass `False` to the macro and silently fall through to `noop` — the off-paths
  would stop turning the lights/fan off. Keep the quotes everywhere `off` is a value; do not "tidy"
  them away. (`validate-ha-config` parses it as valid YAML either way, so this won't be caught for you.)
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Work on `master`.

---

## File Structure

- `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` — **modify**: add `light_decision`.
- `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py` — **modify**: add `light_decision` tests.
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — **modify**: add `bedroom_lights_set`, `bedroom_fan_set`.
- `ansible/roles/containers/home-assistant/files/automations.yaml` — **modify**: migrate the AUTO callers.
- `ansible/roles/containers/home-assistant/state/sanctioned_writers.yml` — **new, hand-maintained**.
- `ansible/roles/containers/home-assistant/state/derived_state.yml` + `STATE.md` — **regenerated** each step.
- `scripts/ha_state_model.py` + `scripts/test_ha_state_model.py` — **modify**: flip single-writer to a hard check reading `sanctioned_writers.yml`.
- `ansible/roles/containers/home-assistant/CLAUDE.md` — **modify**: note the mediator (Task 8).

---

### Task 1: `light_decision` gate macro (TDD)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`

**Interfaces:**
- Produces: macro `light_decision(reason, manual_off, sleep_mode, person_home, presence, lux_allowed, light_on)` → `'natural' | 'wake' | 'off' | 'noop'`.

- [ ] **Step 1: Write the failing tests**

```python
# add to ansible/roles/containers/home-assistant/tests/test_lighting_macros.py
def _decision(reason, manual_off=False, sleep_mode=False, person_home=True,
              presence=True, lux_allowed=True, light_on=False):
    return render_macro(LIGHT, "light_decision", reason, manual_off, sleep_mode,
                        person_home, presence, lux_allowed, light_on)


def test_light_decision_presence_all_gates_pass():
    assert _decision("presence") == "natural"


def test_light_decision_presence_each_gate_blocks():
    assert _decision("presence", manual_off=True) == "noop"
    assert _decision("presence", sleep_mode=True) == "noop"
    assert _decision("presence", person_home=False) == "noop"
    assert _decision("presence", presence=False) == "noop"
    assert _decision("presence", lux_allowed=False) == "noop"
    assert _decision("presence", light_on=True) == "noop"   # never re-stomp an on light


def test_light_decision_passthrough_reasons_are_ungated():
    # natural/wake/off ignore the flags (the caller already gated).
    assert _decision("natural", manual_off=True, person_home=False) == "natural"
    assert _decision("wake", lux_allowed=False) == "wake"
    assert _decision("off", light_on=True) == "off"


def test_light_decision_unknown_reason_is_noop():
    assert _decision("bogus") == "noop"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -q`
Expected: FAIL — the macro `light_decision` is not defined (`UndefinedError`/template error).

- [ ] **Step 3: Add the macro**

Append to `custom_templates/lighting.jinja`:

```jinja
{# Mediator gate policy. Given the action's `reason` + the live flags, decide what the bedroom
   lights should do. Only `presence` is gated here (it is the one AUTO caller whose SOLE action is
   the light, so its conditions live in one tested place); `natural`/`wake`/`off` are pass-through
   (their caller keeps its own gate, which also guards its non-light side effects). Bools arrive as
   real booleans from is_state() in the caller; `| bool` coerces defensively (and for the test
   harness). Returns: natural | wake | off | noop. #}
{%- macro light_decision(reason, manual_off, sleep_mode, person_home, presence, lux_allowed, light_on) -%}
{%- if reason == 'presence' -%}
{{ 'natural' if (not (manual_off | bool) and not (sleep_mode | bool) and (person_home | bool)
   and (presence | bool) and (lux_allowed | bool) and not (light_on | bool)) else 'noop' }}
{%- elif reason in ['natural', 'wake', 'off'] -%}
{{ reason }}
{%- else -%}
noop
{%- endif -%}
{%- endmacro -%}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -q`
Expected: PASS (existing tests + 4 new).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja \
  ansible/roles/containers/home-assistant/tests/test_lighting_macros.py
git commit -m "feat(ha-mediator): light_decision gate macro (presence gated; natural/wake/off pass-through)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `bedroom_lights_set` mediator script

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml`

**Interfaces:**
- Consumes: `light_decision` (Task 1); existing `script.bedroom_apply_natural`, `script.bedroom_apply_wake`.
- Produces: `script.bedroom_lights_set` with field `reason` (`presence | natural | wake | off`).

- [ ] **Step 1: Add the mediator script**

Append to `files/scripts.yaml` (after `bedroom_set_natural_brightness`, near the other lighting scripts):

```yaml
# Phase-2 mediator: THE single sanctioned writer for the bedroom lights' HELD state. AUTO callers
# pass a `reason`; light_decision (lighting.jinja) applies the gate for that reason and this
# delegates to the existing primitive. The manual Tap Dial is exempt (it writes the lights directly
# by design). Reasons: presence (gated: manual_off/sleep/home/presence/lux/light-off), natural &
# wake (ungated — the caller pre-gated), off.
bedroom_lights_set:
  alias: "Bedroom — lights mediator (single guarded writer)"
  description: >-
    Single sanctioned writer for the bedroom lights' held state. Applies the per-reason gate via the
    light_decision macro, then delegates to apply_natural / apply_wake / light.turn_off.
  mode: restart
  fields:
    reason:
      description: "presence | natural | wake | off"
      required: true
      example: presence
  sequence:
    - variables:
        action: >-
          {% from 'lighting.jinja' import light_decision %}{{ light_decision(reason,
            is_state('input_boolean.bedroom_manual_off', 'on'),
            is_state('input_boolean.bedroom_sleep_mode', 'on'),
            is_state('person.daniel', 'home'),
            is_state('binary_sensor.aqara_fp300_presence', 'on'),
            is_state('binary_sensor.bedroom_auto_light_allowed', 'on'),
            is_state('light.bedroom_lights', 'on')) }}
    - choose:
        - conditions: "{{ action == 'natural' }}"
          sequence:
            - service: script.bedroom_apply_natural
        - conditions: "{{ action == 'wake' }}"
          sequence:
            - service: script.bedroom_apply_wake
        - conditions: "{{ action == 'off' }}"
          sequence:
            - service: light.turn_off
              target:
                entity_id: light.bedroom_lights
      # action == 'noop' (gated out, or unknown reason): do nothing.
```

- [ ] **Step 2: Validate structure**

Run: `uv run python scripts/validate_ha_config.py`
Expected: `Home Assistant config OK` (YAML + inline-Jinja syntax valid).

- [ ] **Step 3: Regenerate the derived model (new script is now a writer)**

Run: `uv run python scripts/ha_state_model.py generate`
Then: `git status --short ansible/roles/containers/home-assistant/state/`
Expected: `derived_state.yml` + `STATE.md` show as modified (bedroom_lights_set now appears as a `light.bedroom_lights` writer). This is expected — the freshness gate requires committing them.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha-mediator): bedroom_lights_set dispatcher over light_decision

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Migrate `presence_on` → the mediator

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (the `bedroom_presence_on` block)

**Interfaces:**
- Consumes: `script.bedroom_lights_set` (Task 2).

- [ ] **Step 1: Replace the conditions + action**

In `files/automations.yaml`, the `bedroom_presence_on` automation currently has a `condition:` block (6 conditions) and `action: - service: script.bedroom_apply_natural`. Replace the entire `condition:` block AND the `action:` block so the automation becomes (keep the existing `trigger:` block and the `id`/`alias`/`description`/`mode` unchanged):

```yaml
  # Gating moved into script.bedroom_lights_set / light_decision (the 'presence' reason applies
  # exactly these checks: manual_off off, sleep off, person home, presence on, lux gate on, light
  # off). presence_on's sole action is the light, so it routes straight through the mediator.
  action:
    - service: script.bedroom_lights_set
      data:
        reason: presence
```

Delete the `condition:` block entirely (its checks now live in the macro). Keep the two triggers (the presence `to: "on"` edge and the `numeric_state below: 75 for: 30s` dusk edge).

- [ ] **Step 2: Validate + regenerate**

Run:
```bash
uv run python scripts/validate_ha_config.py
uv run python scripts/ha_state_model.py generate
uv run python scripts/ha_state_model.py check 2>&1 | grep "single-writer\|state-model OK\|FAILED" || true
git status --short ansible/roles/containers/home-assistant/state/
```
Expected: config OK; `bedroom_presence_on` no longer appears as a direct `light.bedroom_lights` writer in `derived_state.yml` (it calls the mediator now); the single-writer report (still advisory) lists one fewer writer.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "refactor(ha-mediator): presence_on routes through bedroom_lights_set('presence')

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Migrate `arrive_home`, `morning_reset`, `wake_ramp` (ungated reasons)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

**Interfaces:**
- Consumes: `script.bedroom_lights_set`.

These callers keep their existing `if`/conditions (which also guard fan/notify side effects) and only swap the light service call.

- [ ] **Step 1: `arrive_home`** — inside its second `if` (`presence on and manual_off off and light off`), replace `- service: script.bedroom_apply_natural` with:

```yaml
        - service: script.bedroom_lights_set
          data:
            reason: natural
```

- [ ] **Step 2: `morning_reset`** — in the `if: "{{ trigger.id == 'alarm' and is_state('person.daniel', 'home') }}"` branch, replace `- service: script.bedroom_apply_natural` with:

```yaml
        - service: script.bedroom_lights_set
          data:
            reason: natural
```

(Leave the `slept_h` variables + the `bedroom_notify` call that follow unchanged.)

- [ ] **Step 3: `wake_ramp`** — replace BOTH light calls in its `choose:`:
  - in-window branch: `- service: script.bedroom_apply_wake` →

```yaml
            - service: script.bedroom_lights_set
              data:
                reason: wake
```
  - window-end branch (`elapsed >= 30 and elapsed < 31`): `- service: script.bedroom_apply_natural` →

```yaml
            - service: script.bedroom_lights_set
              data:
                reason: natural
```

- [ ] **Step 4: `notification_action` (BEDROOM_AWAY_TURN_ON light)** — in that branch, replace `- service: script.bedroom_apply_natural` with:

```yaml
            - service: script.bedroom_lights_set
              data:
                reason: natural
```
(Leave the `script.bedroom_apply_fan` line for Task 6.)

- [ ] **Step 5: Validate + regenerate + commit**

```bash
uv run python scripts/validate_ha_config.py
uv run python scripts/ha_state_model.py generate
git add ansible/roles/containers/home-assistant/files/automations.yaml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "refactor(ha-mediator): arrive/morning/wake_ramp/away-undo route lights through the mediator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: config OK; those automations drop out of the `light.bedroom_lights` writer list.

---

### Task 5: Migrate the off-paths → `bedroom_lights_set('off')`

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml`

- [ ] **Step 1: `bedroom_away`** — in its `on_items | length > 0` sequence, replace the light turn-off:

```yaml
            - service: light.turn_off
              target:
                entity_id: light.bedroom_lights
```
with:
```yaml
            - service: script.bedroom_lights_set
              data:
                reason: "off"
```
(Leave the `fan.turn_off` for Task 6.)

- [ ] **Step 2: `bedroom_absence_off`** — replace its entire `action:`:

```yaml
  action:
    - service: script.bedroom_lights_set
      data:
        reason: "off"
```

- [ ] **Step 3: `bedroom_al_startup_suppress`** — inside its `if` (presence off and no wake alarm), replace the `light.turn_off` with:

```yaml
        - service: script.bedroom_lights_set
          data:
            reason: "off"
```

- [ ] **Step 4: Validate + regenerate + commit**

```bash
uv run python scripts/validate_ha_config.py
uv run python scripts/ha_state_model.py generate
git add ansible/roles/containers/home-assistant/files/automations.yaml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "refactor(ha-mediator): away/absence/AL-suppress route lights-off through the mediator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: after this, the ONLY `light.bedroom_lights` writers are the sanctioned module + the exemptions (Tap Dial, apply_natural_gated, blip, alert_pulse, color_tracking).

---

### Task 6: `bedroom_fan_set` + migrate the direct fan writers

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml`, `files/automations.yaml`

**Interfaces:**
- Consumes: existing `script.bedroom_apply_fan`. Produces: `script.bedroom_fan_set(reason)` (`auto | boost | off`).

The temperature callers (`fan_temperature`, `arrive_home`, `morning_reset`) and the Tap Dial already call the sanctioned `apply_fan`/`fan_nudge`, so they need **no** change. Only the direct `fan.*` writers (`away`, the boost action) move.

- [ ] **Step 1: Add `bedroom_fan_set`** to `files/scripts.yaml` (after `bedroom_apply_fan`):

```yaml
# Phase-2 fan mediator: front door for the fan's non-temperature writes. `auto` delegates the
# temperature curve + caps (apply_fan); `boost` is the notification "Boost fan" action (max, engages
# the manual override + arms the expected-level accumulator so the cloud echo isn't re-flagged);
# `off` is the away turn-off. The Tap Dial + the temperature automations call apply_fan/fan_nudge
# directly (sanctioned module members), so they are unchanged.
bedroom_fan_set:
  alias: "Bedroom — fan mediator"
  description: "auto -> temperature band (apply_fan); boost -> max + manual override; off -> turn off."
  mode: restart
  fields:
    reason:
      description: "auto | boost | off"
      required: true
      example: auto
  sequence:
    - choose:
        - conditions: "{{ reason == 'auto' }}"
          sequence:
            - service: script.bedroom_apply_fan
        - conditions: "{{ reason == 'boost' }}"
          sequence:
            - service: input_boolean.turn_on
              target:
                entity_id: input_boolean.bedroom_fan_manual
            - service: input_number.set_value
              target:
                entity_id: input_number.bedroom_fan_expected_level
              data:
                value: 9
            - service: fan.turn_on
              target:
                entity_id: fan.tower_fan
            - service: fan.set_percentage
              target:
                entity_id: fan.tower_fan
              data:
                percentage: 100
        - conditions: "{{ reason == 'off' }}"
          sequence:
            - service: fan.turn_off
              target:
                entity_id: fan.tower_fan
```

- [ ] **Step 2: Migrate the boost action** — in `bedroom_notification_action`'s `BEDROOM_BOOST_FAN` branch, replace the three service calls (`input_boolean.turn_on` fan_manual + `fan.turn_on` + `fan.set_percentage` 100) with:

```yaml
            - service: script.bedroom_fan_set
              data:
                reason: boost
```

- [ ] **Step 3: Migrate the away fan-off** — in `bedroom_away`, replace `- service: fan.turn_off ... entity_id: fan.tower_fan` with:

```yaml
            - service: script.bedroom_fan_set
              data:
                reason: "off"
```

- [ ] **Step 4: Migrate the away-undo fan** — in `notification_action`'s `BEDROOM_AWAY_TURN_ON` branch, replace `- service: script.bedroom_apply_fan` with:

```yaml
            - service: script.bedroom_fan_set
              data:
                reason: auto
```

- [ ] **Step 5: Validate + regenerate + commit**

```bash
uv run python scripts/validate_ha_config.py
uv run python scripts/ha_state_model.py generate
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
  ansible/roles/containers/home-assistant/files/automations.yaml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha-mediator): bedroom_fan_set + route away/boost through it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: `fan.tower_fan` writers reduce to `{bedroom_fan_set, apply_fan, fan_nudge}` + exemption `bedroom_fan_startup_reconcile`.

---

### Task 7: `sanctioned_writers.yml` + flip single-writer to a HARD check

**Files:**
- Create: `ansible/roles/containers/home-assistant/state/sanctioned_writers.yml`
- Modify: `scripts/ha_state_model.py`, `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `build_model` (writes), the existing `check_errors`.
- Produces: `load_sanctioned_writers()`, `single_writer_errors(writes, sanctioned) -> list[str]`; `check_errors` calls it (hard); `single_writer_report` removed from the advisory print.

- [ ] **Step 1: Create the hand-maintained registry**

`ansible/roles/containers/home-assistant/state/sanctioned_writers.yml`:

```yaml
# HAND-MAINTAINED — per-actuator sanctioned writers for the Phase-2 single-writer invariant.
# `module` = the mediator + its delegated primitives; `exemptions` = declared special-purpose
# writers (manual surface / transient effects / narrow drift / boot recovery). CI fails if the
# DERIVED writer set of an actuator (from derived_state.yml) is not a subset of module ∪ exemptions.
light.bedroom_lights:
  module:
    - script.bedroom_lights_set
    - script.bedroom_apply_natural
    - script.bedroom_apply_wake
    - script.bedroom_set_natural_brightness
    - script.bedroom_bedtime
  exemptions:
    - automation.bedroom_tap_dial_control
    - script.bedroom_apply_natural_gated
    - script.bedroom_blip
    - script.bedroom_alert_pulse
    - automation.bedroom_color_tracking
fan.tower_fan:
  module:
    - script.bedroom_fan_set
    - script.bedroom_apply_fan
    - script.bedroom_fan_nudge
  exemptions:
    - automation.bedroom_fan_startup_reconcile
```

- [ ] **Step 2: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
def test_single_writer_errors_flags_unsanctioned_writer():
    writes = {"light.bedroom_lights": ["script.bedroom_lights_set", "automation.sneaky_new"]}
    sanctioned = {"light.bedroom_lights": {"module": ["script.bedroom_lights_set"], "exemptions": []}}
    errs = hsm.single_writer_errors(writes, sanctioned)
    assert any("sneaky_new" in e for e in errs)


def test_single_writer_errors_clean_when_all_sanctioned():
    writes = {"light.bedroom_lights": ["script.bedroom_lights_set", "script.bedroom_blip"]}
    sanctioned = {"light.bedroom_lights":
                  {"module": ["script.bedroom_lights_set"], "exemptions": ["script.bedroom_blip"]}}
    assert hsm.single_writer_errors(writes, sanctioned) == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py::test_single_writer_errors_flags_unsanctioned_writer -q`
Expected: FAIL — `AttributeError: ... 'single_writer_errors'`.

- [ ] **Step 4: Implement the hard check**

In `scripts/ha_state_model.py`: add `SANCTIONED_YAML = STATE_DIR / "sanctioned_writers.yml"`, a loader, and the check; remove the old `SANCTIONED_WRITERS` dict + `single_writer_report`/`override_consistency_report` advisory prints in favor of the hard check (keep `override_consistency_report` if you want it advisory — leave it printing). Add near the other checks:

```python
SANCTIONED_YAML = STATE_DIR / "sanctioned_writers.yml"


def load_sanctioned_writers() -> dict:
    if not SANCTIONED_YAML.is_file():
        return {}
    return yaml.safe_load(SANCTIONED_YAML.read_text()) or {}


def single_writer_errors(writes: dict, sanctioned: dict) -> list[str]:
    """HARD: every derived writer of a sanctioned actuator must be in module ∪ exemptions."""
    errs = []
    for actuator, spec in sorted(sanctioned.items()):
        allowed = set(spec.get("module", [])) | set(spec.get("exemptions", []))
        for writer in sorted(set(writes.get(actuator, [])) - allowed):
            errs.append(f"{actuator}: unsanctioned writer {writer} — route it through the mediator "
                        f"(script.bedroom_lights_set / bedroom_fan_set) or declare it in "
                        f"state/sanctioned_writers.yml")
    return errs
```

Then in `check_errors`, add `errs += single_writer_errors(model["writes"], load_sanctioned_writers())` and delete the `single_writer_report(...)` line from the advisory-print block (the `override_consistency_report` advisory print may stay).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (including the two new tests). If the old `test_single_writer_report_lists_extra_writers` test now references a removed function, update it to call `single_writer_errors` (keep its intent: a stray writer is flagged).

- [ ] **Step 6: Regenerate + run the real check (must be clean)**

Run:
```bash
uv run python scripts/ha_state_model.py generate
uv run python scripts/ha_state_model.py check 2>&1 | grep -v "^\[state-model report\]" | tail -2
```
Expected: `HA state-model OK`. If `single_writer_errors` reports anything, a caller was missed in Tasks 3–6 OR a writer needs to be added to `sanctioned_writers.yml` — fix the real cause (that is the invariant working).

- [ ] **Step 7: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/state/sanctioned_writers.yml \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha-state): flip single-writer to a HARD check via sanctioned_writers.yml

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Final green + CLAUDE.md note + deploy/verify handoff

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

- [ ] **Step 1: Full structural gate**

Run:
```bash
uv run pytest ansible/roles/containers/home-assistant/tests -q
uv run pytest scripts -q
uv run python scripts/ha_state_model.py check
prek run --all-files
```
Expected: all pass; `HA state-model OK`; `prek run` green (incl. `validate-ha-config` running the now-hard single-writer check).

- [ ] **Step 2: Add the mediator note to CLAUDE.md**

In `ansible/roles/containers/home-assistant/CLAUDE.md`, under the bedroom-lights notes, add a short paragraph:

```markdown
- **Light/fan mediator (Phase 2).** AUTO/programmatic writes of `light.bedroom_lights` go through
  `script.bedroom_lights_set(reason)` (`presence` gated by the `light_decision` macro in
  `lighting.jinja`; `natural`/`wake`/`off` pass-through — the caller pre-gates). Fan non-temperature
  writes go through `script.bedroom_fan_set(reason)` (`auto`/`boost`/`off`). The manual **Tap Dial**
  is a declared exemption (writes directly, by design), as are `apply_natural_gated`, `blip`,
  `alert_pulse`, `color_tracking`, and `fan_startup_reconcile`. The set of allowed writers is
  enforced HARD by the `validate-ha-config` hook via `state/sanctioned_writers.yml` — a new automation
  that writes an actuator directly fails CI. Add a writer = route it through the mediator, or declare
  it in `sanctioned_writers.yml`.
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "docs(ha-mediator): document the light/fan mediator + hard single-writer invariant

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: Deploy + behavioral verification (controller/operator — NOT a subagent step)**

Deploy via the `ha-deploy` skill (`uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`), gate on container health, then confirm the mediator loaded:
```bash
uv run python scripts/probe.py ha state script.bedroom_lights_set
uv run python scripts/probe.py ha-state
```
**Behavioral checks handed to the operator** (time/disruption-bound): presence auto-on in a dim room; the morning wake ramp (needs a morning alarm); `away` turning lights+fan off after leaving; the Tap Dial buttons (unchanged, sanity); the "Boost fan" notification action. Each preserves prior behavior by construction (the mediator delegates to the same primitives with the same gates) — these confirm it live.

---

## Self-Review (completed during planning)

**Spec coverage:**
- Mediator + tested decision macro + delegate-not-absorb → Tasks 1–2. ✅
- Gate matrix (presence gated; natural/wake/off) preserving current behavior → Task 1 macro + Tasks 3–5. ✅
- Tap Dial exempt / untouched; `apply_natural_gated` stays → not modified; declared in `sanctioned_writers.yml` (Task 7). ✅
- Fan consolidation (`bedroom_fan_set`; away/boost migrated; temperature callers already on `apply_fan`) → Task 6. ✅
- Single-writer flipped to HARD via `sanctioned_writers.yml` → Task 7. ✅
- Override-consistency stays a report → unchanged (the advisory print is left in `check_errors`). ✅
- Incremental, validate+regenerate per group; one deploy + behavioral handoff at the end → Tasks 3–6, 8. ✅
- CLAUDE.md note → Task 8. ✅

**Placeholder scan:** none — every step has concrete code/edits/commands.

**Type consistency:** `bedroom_lights_set(reason)` and `bedroom_fan_set(reason)` field name `reason` used consistently in Tasks 2/6 and all migration calls (3–6). `light_decision(reason, manual_off, sleep_mode, person_home, presence, lux_allowed, light_on)` arg order matches the mediator's call site (Task 2) and the test helper (Task 1). `single_writer_errors(writes, sanctioned)` signature matches Task 7's test + `check_errors` call. `sanctioned_writers.yml` keys (`module`/`exemptions`) match `single_writer_errors`'s `spec.get(...)`.

**Behavior-preservation notes (the live risk):** `presence_on`'s 6 macro gates are a 1:1 copy of its removed conditions (verified against `automations.yaml`). Ungated callers keep their own `if` and pass `natural`/`wake`, so their gating is untouched. `away`/`absence`/`suppress_al` `off` is unconditional exactly as before. The `boost` mediator additionally arms `bedroom_fan_expected_level=9` (a deliberate improvement over the current boost, which omits it — prevents the manual-detect from re-flagging the cloud echo); call this out in review as the one intentional behavior delta.

> **Live-path note:** Task 8's deploy + behavioral verification run on daniel-server. Tasks 1–7 (macro, mediator, migrations, the hard check) are structural and run anywhere; the freshness gate keeps the committed `derived_state.yml`/`STATE.md` in lockstep at every step.
