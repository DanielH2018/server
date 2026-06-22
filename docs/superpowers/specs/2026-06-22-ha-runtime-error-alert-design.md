# HA Runtime Error Alert — Design

**Date:** 2026-06-22
**Status:** approved (pre-plan)
**Related:** the validation-layer work (`2026-06-22-ha-testing-interaction-hardening`,
`2026-06-22-ha-validation-followups`) — this is the **runtime complement**: static checks catch
authoring errors before deploy; this catches our own automation/script/template logic that throws
*at runtime*, after deploy, on an input the tests didn't cover.

## Context

HA logs already ship to Loki (`probe.py loki-query '{container="home-assistant"}'`), so errors are
captured *passively* — you have to go look. There is no *active* alert when one of our automations,
scripts, or template sensors throws at render/execution time; it is silent until someone reads the
log. The HA automation-engine heartbeat → monitor-bridge → Kuma watchdog already covers the
"HA/engine is wedged or down" failure out-of-band; this design covers the **complementary**
failure: the engine is healthy but a *specific* piece of our logic errored.

## Governing principle (unchanged)

> Deterministic validation = testing properties (a single correct answer). No flaky/fragile
> alerting — if it cannot be made reliable, do not rely on it. Decision/selection logic belongs in
> a pure, unit-tested `custom_templates/*.jinja` macro (the decision-macro convention); the YAML
> caller reads entities and acts on the returned token. No heavy CI.

## Goals

- Actively notify when **our own code** (automations / scripts / template sensors / template
  helpers) logs an `ERROR`/`CRITICAL` at runtime.
- Keep it non-noisy: scope tightly to our code (exclude third-party/HACS log chatter), dedupe
  per source, and route at routine priority.
- Put the scope decision in a **pure, unit-tested macro** (decision-macro convention; enforced by
  the existing macro-test-coverage guard).

## Non-Goals

- **Not WARNING-level.** WARNINGs from our code are frequently benign (an entity briefly
  `unavailable`, a transient lookup) — including them reintroduces the noise this design avoids.
  Scope is `ERROR`/`CRITICAL` only.
- **Not third-party/integration errors.** A dreo/Z2M/cloud-push reconnect ERROR, or the benign
  `custom_components.adaptive_lighting … not tested` HACS line, is out of scope — those are not our
  logic and are the documented noise class. (The broad "all errors" and "out-of-band Loki/Discord"
  approaches were considered and rejected in favor of the in-HA tested-macro path.)
- **No live-fire test.** Staging a real runtime error safely is impractical; coverage rests on the
  unit-tested macro + load verification (`verify-automations`). Acceptable — the macro is the only
  non-trivial logic.

## Components

### 1. Pure decision macro — `custom_templates/diagnostics.jinja` (new file)

```jinja
{# Runtime-error alert scope. Given a log entry's level + logger name, decide whether it is one of
   OUR-code ERRORs worth alerting on (the runtime complement to the static tests). Pure (values in,
   bool out) so it is unit-tested in tests/test_diagnostics_macros.py. `str.startswith(tuple)`
   behaves identically in HA Jinja and the bare test harness. #}
{%- macro error_in_scope(level, logger) -%}
{%- set lv = (level | string | upper) -%}
{%- set lg = (logger | string) -%}
{%- set scoped = ('homeassistant.components.automation', 'homeassistant.components.script',
                  'homeassistant.components.template', 'homeassistant.helpers.template') -%}
{{ lv in ['ERROR', 'CRITICAL'] and lg.startswith(scoped) }}
{%- endmacro -%}
```

- Input: `level` (the event's `level`), `logger` (the event's `name`). Both coerced defensively
  (`| string`) so a `None`/missing value yields `False`, never an error.
- Output: a Jinja-rendered bool string (`True`/`False`) the YAML condition coerces.
- The four in-scope prefixes are the loggers our code emits under: per-automation
  (`…components.automation.<id>`), per-script (`…components.script.<id>`), template sensors
  (`…components.template`), and template-helper render errors (`…helpers.template`, where a
  `custom_templates/*.jinja` macro render error surfaces).
- New `.jinja` ships automatically (the `custom_templates/` deploy is a whole-directory copy).

### 2. Automation — `ha_runtime_error_alert` in `files/automations.yaml`

Homelab-wide (no `bedroom_` prefix), a sibling of `ha_heartbeat` / `update_available_digest` /
`ups_power_event`. `mode: queued` (each distinct error event is processed; rapid events serialize
through the notify path rather than being dropped).

- **Trigger:** `platform: event`, `event_type: system_log_event` (no `event_data` filter — the
  macro is the single source of the scope decision).
- **Condition (template):** alert only when in scope AND not our own logger (loop-guard):
  ```jinja
  {% from 'diagnostics.jinja' import error_in_scope %}
  {{ error_in_scope(trigger.event.data.level, trigger.event.data.name)
     and trigger.event.data.name != 'homeassistant.components.automation.ha_runtime_error_alert' }}
  ```
  The self-exclusion prevents recursion: if this automation's own action errors (e.g. a transient
  notify failure), that ERROR logs under its own logger and must not re-trigger it.
- **Action:** call `script.bedroom_notify` with the routing below.

`system_log_event` data fields used: `level`, `name` (logger), `message` (list of strings).
**Reconcile against live during implementation** (the macro takes plain args so it is
field-name-agnostic, but the automation's `trigger.event.data.<field>` wiring is not — a wrong
field name = the automation silently never fires): confirm the exact keys on a real
`system_log_event` before finalizing the condition (e.g. read one off the live event bus / HA docs).
This is the analog of the live-trace-shape reconciliation in the prior WS-trace work.

### 3. Routing — through `script.bedroom_notify` (the single notification layer)

Routine priority (`watch: false`, `pierce: false` — both default false, so omitted):

- `title`: `⚠️ HA error: <last dotted segment of trigger.event.data.name>`
  (e.g. `⚠️ HA error: bedroom_presence_on`).
- `message`: `trigger.event.data.message | join(' ')`, truncated to ~200 chars (phone-friendly).
- `tag`: `ha_error_{{ trigger.event.data.name | replace('.', '_') }}` — a **per-source**
  coalescing tag. Repeated errors from the same logger update one notification in place (no spam);
  distinct sources still produce distinct alerts. Tag derivation is trivial string munging done
  inline in the YAML (not decision logic — no macro needed).

Routing routine means errors are silent while DND/sleep and held-then-digested while away (correct
for non-critical dev feedback). `bedroom_notify`'s existing away-hold / DND logic is unchanged.

## Testing

`tests/test_diagnostics_macros.py` — truth table over `error_in_scope` via the existing
`jinja_harness.render_macro`:

| level | logger | expect |
|---|---|---|
| `ERROR` | `homeassistant.components.automation.bedroom_presence_on` | True |
| `CRITICAL` | `homeassistant.components.script.bedroom_apply_natural` | True |
| `ERROR` | `homeassistant.components.template` | True |
| `ERROR` | `homeassistant.helpers.template` | True |
| `WARNING` | `homeassistant.components.automation.x` | False |
| `ERROR` | `custom_components.adaptive_lighting.switch` | False (the benign HACS noise) |
| `INFO` | `homeassistant.components.automation.x` | False |
| `ERROR` | `None` (missing logger) | False |

The macro-test-coverage guard (`tests/test_macro_coverage.py`) independently requires
`error_in_scope` to be invoked via `render_macro(...)` in a test — so the macro cannot ship
untested.

## Deploy & verify

- `ha-deploy`: validate (`validate_ha_config.py` — the reference-integrity check confirms
  `script.bedroom_notify` and `diagnostics.jinja` resolve) + `uv run pytest
  ansible/roles/containers/home-assistant/tests` (the new macro test).
- Post-deploy: `probe.py ha verify-automations` confirms `ha_runtime_error_alert` loaded (count
  rises 29 → 30); `ha get error_log` clean.
- No live-fire (see Non-Goals); the unit-tested macro + load verification are the coverage.

## Boundaries

One macro (scope decision, tested), one automation (trigger + condition + notify, no actuator
writes — no `sanctioned_writers` concern), reusing the existing `bedroom_notify` layer. Each unit
is independently understandable and the only non-trivial logic (the scope predicate) is pure and
unit-tested.
