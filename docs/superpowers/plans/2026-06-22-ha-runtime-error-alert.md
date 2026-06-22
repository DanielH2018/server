# HA Runtime Error Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Actively notify (via `bedroom_notify`) when our own automations/scripts/templates throw an `ERROR`/`CRITICAL` at runtime — the runtime complement to the static validation.

**Architecture:** One pure, unit-tested decision macro (`error_in_scope`) encodes the scope; one homelab-wide automation triggers on `system_log_event`, gates on the macro (+ a self-exclusion loop-guard), and routes a routine, per-source-coalescing alert through the existing `script.bedroom_notify`.

**Tech Stack:** Home Assistant (copy-deployed YAML + `custom_templates/*.jinja` macros), the `jinja_harness` macro test harness, `uv run pytest`, the `validate-ha-config` prek hook.

## Global Constraints

- **Scope = our-code ERRORs only:** `level` in `ERROR`/`CRITICAL` AND `logger` starts with one of `homeassistant.components.automation` / `homeassistant.components.script` / `homeassistant.components.template` / `homeassistant.helpers.template`. Excludes WARNINGs and third-party/HACS loggers (e.g. `custom_components.adaptive_lighting.*`).
- **Decision logic lives in a pure macro** (no `states()`/`now()`/`is_state()` inside) with a truth-table test — the decision-macro convention; the macro-test-coverage guard (`tests/test_macro_coverage.py`) enforces the test exists.
- **Routing is routine:** no `watch`, no `pierce`. Per-source coalescing tag `ha_error_<logger-dots→underscores>`.
- **Loop-guard:** never alert on this automation's own logger (`homeassistant.components.automation.ha_runtime_error_alert`).
- **copy-not-template:** `automations.yaml` and `custom_templates/*.jinja` are deployed verbatim by `ansible.builtin.copy`; they contain HA `{{ }}` Jinja that Ansible must NOT render. Edit the role's `files/` sources (never `containers/`).
- **No new dependencies.** No live deploy in this plan — the deploy + live verification is a separate operator action via `ha-deploy` (see the end).

---

### Task 1: The `error_in_scope` decision macro + truth-table test

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/custom_templates/diagnostics.jinja`
- Create: `ansible/roles/containers/home-assistant/tests/test_diagnostics_macros.py`

**Interfaces:**
- Consumes: nothing.
- Produces: macro `error_in_scope(level, logger)` → renders `True`/`False`. Used by Task 2's automation condition.

- [ ] **Step 1: Write the failing truth-table test**

Create `ansible/roles/containers/home-assistant/tests/test_diagnostics_macros.py`:

```python
"""Unit tests for the runtime-error-alert scope macro in custom_templates/diagnostics.jinja."""
from jinja_harness import render_macro

DIAG = "diagnostics.jinja"


def _scope(level, logger):
    return render_macro(DIAG, "error_in_scope", level, logger)


def test_error_in_scope_our_code_errors_alert():
    assert _scope("ERROR", "homeassistant.components.automation.bedroom_presence_on") == "True"
    assert _scope("CRITICAL", "homeassistant.components.script.bedroom_apply_natural") == "True"
    assert _scope("ERROR", "homeassistant.components.template") == "True"
    assert _scope("ERROR", "homeassistant.helpers.template") == "True"


def test_error_in_scope_excludes_warnings_and_info():
    assert _scope("WARNING", "homeassistant.components.automation.x") == "False"
    assert _scope("INFO", "homeassistant.components.automation.x") == "False"


def test_error_in_scope_excludes_third_party_loggers():
    # The benign HACS noise class — an ERROR, but not our code.
    assert _scope("ERROR", "custom_components.adaptive_lighting.switch") == "False"
    assert _scope("ERROR", "homeassistant.components.dreo") == "False"


def test_error_in_scope_tolerates_missing_logger():
    assert _scope("ERROR", None) == "False"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_diagnostics_macros.py -v`
Expected: FAIL — `diagnostics.jinja` / `error_in_scope` does not exist (`TemplateNotFound`).

- [ ] **Step 3: Create the macro**

Create `ansible/roles/containers/home-assistant/files/custom_templates/diagnostics.jinja`:

```jinja
{# Runtime-error alert scope. Given a log entry's level + logger name, decide whether it is one of
   OUR-code ERRORs worth alerting on (the runtime complement to the static tests). Pure (values in,
   bool out) so it is unit-tested in tests/test_diagnostics_macros.py. Used by
   automation.ha_runtime_error_alert. `str.startswith(tuple)` and tuple literals behave identically
   in HA's sandboxed Jinja and the bare test harness; `| string` coerces a missing/None value so it
   can never raise. #}
{%- macro error_in_scope(level, logger) -%}
{%- set lv = (level | string | upper) -%}
{%- set lg = (logger | string) -%}
{%- set scoped = ('homeassistant.components.automation', 'homeassistant.components.script',
                  'homeassistant.components.template', 'homeassistant.helpers.template') -%}
{{ lv in ['ERROR', 'CRITICAL'] and lg.startswith(scoped) }}
{%- endmacro -%}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_diagnostics_macros.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Confirm the macro-coverage guard is satisfied**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_macro_coverage.py -v`
Expected: PASS — `error_in_scope` is now both defined and invoked via `render_macro(...)`, so the "every macro has a test" guard is green.

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/diagnostics.jinja \
        ansible/roles/containers/home-assistant/tests/test_diagnostics_macros.py
git commit -m "feat(ha): error_in_scope macro — scope runtime-error alerts to our-code ERRORs"
```

---

### Task 2: The `ha_runtime_error_alert` automation

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (append a homelab-wide automation, alongside `ha_heartbeat` / `update_available_digest` / `ups_power_event`)

**Interfaces:**
- Consumes: `error_in_scope` (Task 1); `script.bedroom_notify` (existing; required fields `title`, `message`, `tag`).
- Produces: `automation.ha_runtime_error_alert` (the live automation; raises the `verify-automations` count 29 → 30 after deploy).

- [ ] **Step 1: Confirm the `system_log_event` field names**

The automation reads `trigger.event.data.level`, `.name` (logger), and `.message` (list). The macro is field-name-agnostic (takes plain args), but the automation's wiring is not — a wrong key means it loads but silently never fires. Confirm the keys against HA's documented `system_log_event` schema (fields: `name`, `message`, `level`, `source`, `timestamp`, `exception`). If a live capture is available on daniel-server, cross-check one real event; otherwise the documented schema is authoritative. Do not change the keys away from `name`/`level`/`message` without evidence.

- [ ] **Step 2: Append the automation**

Add to the end of `ansible/roles/containers/home-assistant/files/automations.yaml`:

```yaml
# HA runtime-error alert (homelab-wide, no bedroom_ prefix). Actively notifies when OUR code
# (automations/scripts/template sensors/template helpers) logs an ERROR/CRITICAL at runtime — the
# runtime complement to the static validation (a macro that throws on an edge case, a template that
# errors at render). Scope is the pure, tested error_in_scope(level, logger) macro
# (custom_templates/diagnostics.jinja); third-party/HACS chatter and WARNINGs are excluded there.
# Routine via bedroom_notify (silent while DND/sleep, held-then-digested while away). The per-source
# tag coalesces a repeating error into one updating notification. Self-exclusion (its own logger) is
# the loop-guard: if the notify action itself errors, that ERROR must not re-trigger this automation.
# Spec: docs/superpowers/specs/2026-06-22-ha-runtime-error-alert-design.md.
- id: ha_runtime_error_alert
  alias: HA runtime error alert
  description: Notify when one of our automations/scripts/templates logs an ERROR/CRITICAL at runtime.
  mode: queued
  max: 10
  trigger:
    - platform: event
      event_type: system_log_event
  condition:
    - condition: template
      value_template: >-
        {% from 'diagnostics.jinja' import error_in_scope %}
        {{ error_in_scope(trigger.event.data.level, trigger.event.data.name)
           and trigger.event.data.name != 'homeassistant.components.automation.ha_runtime_error_alert' }}
  action:
    - service: script.bedroom_notify
      data:
        title: "⚠️ HA error: {{ trigger.event.data.name.split('.')[-1] }}"
        message: "{{ (trigger.event.data.message | join(' '))[:200] }}"
        tag: "ha_error_{{ trigger.event.data.name | replace('.', '_') }}"
```

- [ ] **Step 3: Structurally validate the config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exit 0. This confirms YAML/duplicate-key/`!include`/template syntax, that the
`diagnostics.jinja` import + `error_in_scope` reference resolve, that `script.bedroom_notify`
resolves (reference-integrity), and that the macro carries a test (macro-coverage).

- [ ] **Step 4: Run the HA role test suite**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests -q`
Expected: all PASS (the diagnostics macro test from Task 1 + the unchanged macro suite).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(ha): ha_runtime_error_alert automation (system_log_event -> bedroom_notify, macro-gated)"
```

---

## Deploy & verify (operator follow-up — not part of subagent execution)

After both tasks are committed and reviewed, deploy via the `ha-deploy` skill:

1. `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA, ~120s).
2. `uv run python scripts/probe.py health home-assistant` — exit 0.
3. `uv run python scripts/probe.py ha verify-automations` — must now report `all 30 automations loaded` (was 29; confirms `ha_runtime_error_alert` loaded).
4. `uv run python scripts/probe.py ha get error_log` — clean (no template/macro render error from the new automation/macro).

No live-fire test (staging a real runtime error safely is impractical, per the spec's Non-Goals); coverage rests on the unit-tested macro + the load verification.
