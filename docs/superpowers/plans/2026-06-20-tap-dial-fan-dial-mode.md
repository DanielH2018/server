# Tap Dial Fan-Dial Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tap Dial Button-4 HOLD toggle a temporary mode where the rotary dial adjusts the fan level instead of the lights, auto-reverting after 5 minutes of inactivity.

**Architecture:** A single HA `timer` helper (`timer.bedroom_fan_dial`) whose `active` state *is* the mode — hold starts it, hold-again cancels it, expiry reverts. The dial-rotate handlers branch on the timer state. Fan stepping is driven off the existing `input_number.bedroom_fan_expected_level` accumulator (instant, server-side) to survive the DREO cloud-push report lag, with the clamp math in a tested `fan.jinja` macro.

**Tech Stack:** Home Assistant (LSIO container), YAML automations/scripts copied by Ansible, Jinja2 macros in `custom_templates/`, pytest macro tests via `jinja_harness`.

## Global Constraints

- `files/*.yaml` + `custom_templates/*.jinja` are deployed by `ansible.builtin.copy` verbatim — they use HA `{{ }}` Jinja; do NOT add `{% raw %}`. Git is the source of truth; HA UI edits are overwritten.
- HA `round` is **banker's rounding** (round-half-to-even); the harness mirrors this. Fan level math relies on it.
- `% <-> level` conversion + the `FAN_LEVELS = 9` count live ONLY in `custom_templates/fan.jinja` — never duplicate.
- The DREO integration `math.ceil()`s a requested `%` up to the next level; send `level_to_pct(L)` (the level's midpoint %) to land on level L exactly.
- Timer has **no `restore:`** — idle-after-restart is intentional (a deploy must not strand the dial in fan mode).
- Manual dialing **ignores** the night (L4) / sleep (L2) caps — those constrain only the automatic curve in `fan_target_level`.
- Repo convention: **one feature commit at the end** (matches the git log), made on user confirmation — not per-task. TDD still applies (failing test written before the macro).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA ~120 s; all touched files feed `common_config_changed`).

---

## File Structure

- `ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja` — add `fan_nudge_level` macro (clamp math).
- `ansible/roles/containers/home-assistant/tests/test_fan_macros.py` — add `fan_nudge_level` tests.
- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — add `timer.bedroom_fan_dial` helper.
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — add `script.bedroom_fan_nudge`.
- `ansible/roles/containers/home-assistant/files/automations.yaml` — rewire B4 hold (toggle), B4 tap (also cancel), dial-rotate branches.
- `ansible/roles/containers/home-assistant/CLAUDE.md` — update the B4 description (hold is no longer boost).

---

### Task 1: `fan_nudge_level` macro + tests (TDD)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja` (append macro)
- Test: `ansible/roles/containers/home-assistant/tests/test_fan_macros.py` (append tests)

**Interfaces:**
- Produces: `fan_nudge_level(cur_level, delta)` → int level clamped to `0..FAN_LEVELS` (0 = off). Consumed by `script.bedroom_fan_nudge` (Task 3).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_fan_macros.py`:

```python
def _nudge(cur_level, delta):
    return int(render_macro(FAN, "fan_nudge_level", cur_level, delta))


def test_fan_nudge_steps_within_range():
    assert _nudge(3, 1) == 4
    assert _nudge(3, -1) == 2


def test_fan_nudge_clamps_at_zero():
    assert _nudge(0, -1) == 0   # already off, stays off
    assert _nudge(1, -1) == 0   # step down to off


def test_fan_nudge_clamps_at_max():
    assert _nudge(9, 1) == 9    # already max, stays
    assert _nudge(8, 1) == 9


def test_fan_nudge_stays_bounded_over_full_range():
    for cur in range(0, 10):
        for delta in (-1, 1):
            assert 0 <= _nudge(cur, delta) <= 9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -k fan_nudge -v`
Expected: FAIL — the template raises because macro `fan_nudge_level` is not defined in `fan.jinja`.

- [ ] **Step 3: Implement the macro** — append to `files/custom_templates/fan.jinja`:

```jinja
{# Current level + delta -> new level, clamped to 0..FAN_LEVELS (0 = off). Used by
   script.bedroom_fan_nudge for the Tap Dial fan-dial mode (dial = +/-1 fan level). Pure clamp —
   tunable bounds live here. Tested in tests/test_fan_macros.py. #}
{%- macro fan_nudge_level(cur_level, delta) -%}
{{ [[ (cur_level | int(0)) + (delta | int(0)), 0 ] | max, FAN_LEVELS] | min | int }}
{%- endmacro -%}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -k fan_nudge -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full fan-macro suite** (no regression to the existing curve/conversion tests)

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -v`
Expected: PASS (all existing tests + the 4 new).

---

### Task 2: `timer.bedroom_fan_dial` helper

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (add a top-level `timer:` block after the `input_datetime:` block, ~line 58)

**Interfaces:**
- Produces: entity `timer.bedroom_fan_dial`, default duration 5 min. Consumed by Task 3's caller? No — consumed by the automations in Task 4 (start/cancel/active-check).

- [ ] **Step 1: Add the timer helper** — insert after the `input_datetime:` block (before the `# Adaptive Lighting` comment, ~line 59):

```yaml
# Fan-dial mode timer — its `active` state IS "the dial controls the fan" (Tap Dial button-4 hold
# toggles it; bedroom_tap_dial_control's dial_rotate branches read it). 5-min SLIDING window: each
# dial turn restarts it (timer.start), and it auto-reverts to light control on expiry. No `restore:`
# — idle after an HA restart is intended, so a deploy can't strand the dial in fan mode. No separate
# input_boolean: the timer IS the source of truth, which is why restart-safety is free.
timer:
  bedroom_fan_dial:
    name: Bedroom fan-dial mode
    duration: "00:05:00"
    icon: mdi:fan-chevron-up
```

- [ ] **Step 2: Validate config renders + parses**

Run: `uv run python scripts/validate_ha_config.py`
Expected: PASS (no duplicate-key / YAML / include / Jinja errors). The `configuration.yaml.j2` is Ansible-templated — the validator assembles the deployed `/config` layout.

---

### Task 3: `script.bedroom_fan_nudge`

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (add a new script; place it after `bedroom_apply_fan`, before `bedroom_bedtime`)

**Interfaces:**
- Consumes: `fan_nudge_level` (Task 1), `level_to_pct` (existing, `fan.jinja`), `input_number.bedroom_fan_expected_level` (existing accumulator), `input_boolean.bedroom_fan_manual` (existing override).
- Produces: `script.bedroom_fan_nudge` with field `delta` (+1 / -1). Consumed by the dial-rotate handlers (Task 4).

- [ ] **Step 1: Add the script** — insert after the `bedroom_apply_fan:` script block (after its last line, before the `# Bedtime / sleep routine` comment):

```yaml
# Nudge the fan one DREO level (Tap Dial fan-dial mode: dial = +/-1 level). Driven off the
# input_number.bedroom_fan_expected_level ACCUMULATOR (instant + server-side) rather than the fan's
# laggy cloud-pushed percentage, so rapid dial turns accumulate correctly. Engages the manual
# override (a hand-dialed fan is a manual change); writes the new expected level so
# bedroom_fan_manual_detect treats our own cloud echo as expected (not an external change). Ignores
# the night/sleep caps by design — you're explicitly in control. The level PERSISTS after fan-dial
# mode reverts; clear it via Tap Dial button-4 tap or the morning reset. The % <-> level conversion
# and the clamp live in fan.jinja (single source of truth).
bedroom_fan_nudge:
  alias: "Bedroom — nudge fan one level (Tap Dial fan-dial mode)"
  description: >-
    Step fan.tower_fan up/down one DREO level (clamped 0-9, 0 = off) from the
    bedroom_fan_expected_level accumulator. Engages the manual fan override.
  mode: queued
  max: 10
  fields:
    delta:
      description: "+1 to step up a level, -1 to step down."
      example: 1
  sequence:
    - variables:
        new_level: "{% from 'fan.jinja' import fan_nudge_level %}{{ fan_nudge_level(states('input_number.bedroom_fan_expected_level') | int(0), delta) | int }}"
        send_pct: "{% from 'fan.jinja' import level_to_pct %}{{ level_to_pct(new_level) | int }}"
    # Manual override: a hand-dialed fan must not be stomped by the temperature automation.
    - service: input_boolean.turn_on
      target:
        entity_id: input_boolean.bedroom_fan_manual
    # Accumulator = source of truth (instant); also = expected level, so the parent-less cloud echo
    # isn't mis-flagged as an external manual change by bedroom_fan_manual_detect.
    - service: input_number.set_value
      target:
        entity_id: input_number.bedroom_fan_expected_level
      data:
        value: "{{ new_level }}"
    - choose:
        # Level 0 — turn the fan off only if it's currently on.
        - conditions: "{{ new_level | int == 0 }}"
          sequence:
            - if: "{{ is_state('fan.tower_fan', 'on') }}"
              then:
                - service: fan.turn_off
                  target:
                    entity_id: fan.tower_fan
      default:
        - service: fan.turn_on
          target:
            entity_id: fan.tower_fan
        - service: fan.set_percentage
          target:
            entity_id: fan.tower_fan
          data:
            percentage: "{{ send_pct }}"
```

- [ ] **Step 2: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: PASS (the inline `{% from 'fan.jinja' import ... %}` templates are syntax-checked).

---

### Task 4: Rewire `bedroom_tap_dial_control` (B4 hold + tap + dial)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` — the `bedroom_tap_dial_control` automation (B4 press_release ~111-124, B4 hold_release ~125-137, dial_rotate_right ~139-146, dial_rotate_left ~147-154). Line numbers will shift as you edit; match on content.

**Interfaces:**
- Consumes: `timer.bedroom_fan_dial` (Task 2), `script.bedroom_fan_nudge` (Task 3).

- [ ] **Step 1: Replace the Button-4 HOLD branch.** Find the `button_4_hold_release` block (the boost-to-100% sequence) and replace its entire `conditions`/`sequence` with:

```yaml
        # Button 4 HOLD = toggle "fan-dial mode": while timer.bedroom_fan_dial is active the dial
        # controls the FAN level (see the dial_rotate branches); hold again to cancel. It auto-reverts
        # to light control when the 5-min timer expires (SLIDING — each dial turn restarts it). The
        # timer's `active` state IS the mode (no separate boolean) so it's off after any HA restart.
        # Replaces the old hold-to-boost-100% — max fan is still reachable by dialing to L9, and the
        # notification "Boost fan" action is unchanged.
        - conditions: "{{ act == 'button_4_hold_release' }}"
          sequence:
            - if: "{{ is_state('timer.bedroom_fan_dial', 'active') }}"
              then:
                - service: timer.cancel
                  target:
                    entity_id: timer.bedroom_fan_dial
              else:
                - service: timer.start
                  target:
                    entity_id: timer.bedroom_fan_dial
                # Seed the accumulator from the fan's ACTUAL level so the first dial step is relative
                # to reality (a prior remote change may have left expected_level stale).
                - service: input_number.set_value
                  target:
                    entity_id: input_number.bedroom_fan_expected_level
                  data:
                    value: "{% from 'fan.jinja' import pct_to_level %}{{ pct_to_level(state_attr('fan.tower_fan', 'percentage') | float(0) if is_state('fan.tower_fan', 'on') else 0) | int }}"
```

- [ ] **Step 2: Add `timer.cancel` to the Button-4 TAP branch.** Find the `button_4_press_release` block and add this as the FIRST step of its `sequence:` (before the `input_boolean.turn_off` for `bedroom_sleep_mode`):

```yaml
            # Tapping "fan back to auto" also exits fan-dial mode (the dial returns to lights).
            # No-op if the timer is already idle.
            - service: timer.cancel
              target:
                entity_id: timer.bedroom_fan_dial
```

- [ ] **Step 3: Replace the dial-rotate-RIGHT branch** with the fan/light fork:

```yaml
        # Dial rotate = brightness +/- 12% NORMALLY; in fan-dial mode (timer active) = fan +/-1 level.
        - conditions: "{{ 'dial_rotate_right' in act }}"
          sequence:
            - if: "{{ is_state('timer.bedroom_fan_dial', 'active') }}"
              then:
                - service: script.bedroom_fan_nudge
                  data:
                    delta: 1
                # Sliding window: each turn restarts the 5-min revert timer.
                - service: timer.start
                  target:
                    entity_id: timer.bedroom_fan_dial
              else:
                - service: light.turn_on
                  target:
                    entity_id: light.bedroom_lights
                  data:
                    brightness_step_pct: 12
                    transition: 0.2
```

- [ ] **Step 4: Replace the dial-rotate-LEFT branch** (mirror — `delta: -1`, `brightness_step_pct: -12`):

```yaml
        - conditions: "{{ 'dial_rotate_left' in act }}"
          sequence:
            - if: "{{ is_state('timer.bedroom_fan_dial', 'active') }}"
              then:
                - service: script.bedroom_fan_nudge
                  data:
                    delta: -1
                - service: timer.start
                  target:
                    entity_id: timer.bedroom_fan_dial
              else:
                - service: light.turn_on
                  target:
                    entity_id: light.bedroom_lights
                  data:
                    brightness_step_pct: -12
                    transition: 0.2
```

- [ ] **Step 5: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: PASS.

---

### Task 5: Docs, full validation, deploy, live verify, commit

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` (B4 description)

- [ ] **Step 1: Update the role CLAUDE.md.** In the "At a glance"/Notable Tap Dial summary, change the Button-4 description from hold-to-boost to the new toggle. Find:

```
B4 = Fan: press = auto [clear fan-manual + `bedroom_apply_fan`], hold = boost 100%
```

Replace with:

```
B4 = Fan: press = auto [clear fan-manual + `bedroom_apply_fan` + cancel fan-dial timer], hold = toggle fan-dial mode (`timer.bedroom_fan_dial`, 5-min sliding window: dial then steps fan ±1 level via `script.bedroom_fan_nudge`; auto-reverts to light dial on expiry)
```

Also add a one-line note near the fan section that hold-to-boost was replaced (max fan still reachable by dialing to L9; the `BEDROOM_BOOST_FAN` notification action is unchanged).

- [ ] **Step 2: Run the full repo test + lint gate**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests -v && uv run python scripts/validate_ha_config.py`
Expected: all PASS.

- [ ] **Step 3: Run prek on the changed files**

Run: `prek run --files ansible/roles/containers/home-assistant/files/automations.yaml ansible/roles/containers/home-assistant/files/scripts.yaml ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 ansible/roles/containers/home-assistant/tests/test_fan_macros.py ansible/roles/containers/home-assistant/CLAUDE.md`
Expected: PASS (yamllint, ansible-lint, validate-ha-config, pytest, gitleaks).

- [ ] **Step 4: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: `changed` (config recreates HA, ~120 s).

- [ ] **Step 5: Post-deploy health gate**

Run: `uv run python scripts/probe.py health home-assistant`
Expected: exit 0 (container running + healthy).

- [ ] **Step 6: Verify the new entity + clean load.** Confirm `timer.bedroom_fan_dial` registered and is `idle`, and there are no template/automation errors in the HA log since the restart.

Run: `uv run python scripts/probe.py targets` (or inspect HA logs via `docker logs`/probe) — check for `bedroom_fan_dial`, no `TemplateError`/`fan_nudge_level`/`bedroom_fan_nudge` errors.
Expected: timer present + idle; no errors.

- [ ] **Step 7: User does the physical smoke test.** Hold B4 → dial changes the fan (lights unaffected); hold again → dial back to lights; rapid spin accumulates; idle 5 min → reverts; B4 tap returns the fan to auto. (Document this as the acceptance check; the operator confirms on the hardware.)

- [ ] **Step 8: Commit (on user confirmation).** Single feature commit including the spec + plan docs:

```bash
git add ansible/roles/containers/home-assistant/ docs/superpowers/specs/2026-06-20-tap-dial-fan-dial-mode-design.md docs/superpowers/plans/2026-06-20-tap-dial-fan-dial-mode.md
git commit -m "$(cat <<'EOF'
feat(home-assistant): Tap Dial B4 hold = fan-dial mode (dial steps fan, 5-min sliding revert)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- B4 hold toggles fan-dial mode → Task 4 Step 1. ✓
- Hold again toggles off → Task 4 Step 1 (`timer.cancel` when active). ✓
- Dial controls fan in mode / lights otherwise → Task 4 Steps 3-4. ✓
- 5-min sliding auto-revert → timer helper (Task 2) + `timer.start` on each turn (Task 4 Steps 3-4); implicit revert on expiry (no automation needed). ✓
- Silent (no cue) → no notification/blip anywhere. ✓
- Accumulator reuse + seed-on-enter → Task 3 + Task 4 Step 1. ✓
- Manual override + ignore caps + persistence → Task 3. ✓
- B4 tap also cancels → Task 4 Step 2. ✓
- Macro + test → Task 1. ✓
- Restart-safety (no `restore:`) → Task 2. ✓
- Docs → Task 5 Step 1. ✓

**Placeholder scan:** none — all steps contain concrete code/commands.

**Type consistency:** `fan_nudge_level(cur_level, delta)` defined in Task 1, called in Task 3 with `(states(...)|int(0), delta)`. `level_to_pct`/`pct_to_level` are existing macros. `script.bedroom_fan_nudge` field `delta` (int) passed as `delta: 1`/`delta: -1` in Task 4. `timer.bedroom_fan_dial` name consistent across Tasks 2/4. Consistent.
