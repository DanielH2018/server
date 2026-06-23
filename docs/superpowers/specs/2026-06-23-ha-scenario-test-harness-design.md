# HA Scenario Test Harness — Design

**Date:** 2026-06-23
**Status:** approved (pre-plan)
**Origin:** Operator question — "is there a way to better test HA together? e.g. test the nighttime
cycle more than just when it fires at night, or test the away automations without leaving home?"
The bedroom automations are gated on real-world signals you can't easily fake — the real clock
(`00:00–05:00`, `22:00–06:00`, the dynamic wake window `sensor.bedroom_wake_start` =
`sensor.pixel_watch_3_next_alarm − 15 min`) and GPS-derived presence (`person.daniel` going
`from: "home"` `for: "00:10:00"`). The repo already has pure-math unit tests, structural validation
(`validate_ha_config.py`), and live introspection (`probe.py ha why` / `verify-automations`). The
missing layer is **behavioral/scenario testing**: "put the room in state X on demand, then watch the
real automation do Y" — without waiting for night or leaving the house.

## Governing principle

> **The harness exercises the real production paths; it does not fork them.** Every scenario drives an
> existing script or `automation.trigger`s an existing automation. Where a real path can't be driven
> on demand (the wake ramp's per-minute clock dependency), the harness *reuses the already-tested
> macro* rather than duplicating logic. The harness is inert until explicitly run, time-bounded in
> fast mode, and reversible via a `reset` scenario.

Decisions locked during brainstorming:
- **Live-first, both phases.** Phase 1 = live on-demand harness; Phase 2 = backfill offline regression
  tests for the away/arrive selection logic (the night-cycle math is *already* unit-tested).
- **Selectable speed, fast by default** (`~30s` fast / production durations real).
- **Away depth = response-only (option A).** No test seam in production notification code; the
  alert-hold path is verified manually (Developer Tools → States) and documented in the runbook.

## Goal

A one-tap, in-HA way to run the bedtime fade, wake ramp, nightlight, away-shutoff, arrive-home, and a
reset-to-normal — at compressed or real speed — narrating intended-vs-resulting state, all by driving
the real automations/scripts. Plus offline regression tests for the away/arrive selection logic.

## Non-Goals

- **No new HA integration, no `python_script`, no host-mode change.** Pure YAML + existing macros.
- **Not a clock/GPS simulator.** HA has no service to set arbitrary entity state from a script (there
  is no real `homeassistant.set_state` — common misconception), and `person.daniel` is GPS-recomputed.
  So the away **notification-hold** path (`script.bedroom_notify` parking alerts while
  `person.daniel` is away) is **out of scope for automated driving** — `automation.trigger` faithfully
  tests the away *response* (lights/fan off + "Left on" notify) but not the hold-and-flush. The
  runbook documents the manual check (set `person.daniel` in Developer Tools → States).
- **Not a replacement for the macro unit tests or structural validation** — a complementary layer.
- **Production behavior is unchanged.** All production-script edits are additive with defaults equal
  to today's behavior (e.g. `bedroom_bedtime`'s `fade` defaults to `1800`).

## Components

### 1. Helpers — `templates/configuration.yaml.j2`
- `input_select.bedroom_test_scenario`: options `off · bedtime · wake · nightlight · away · arrive ·
  reset` (default `off`, icon `mdi:test-tube`).
- `input_boolean.bedroom_test_fast`: speed toggle, default **on** (`mdi:fast-forward`).

These are inert — nothing reads them except the run button. Exclude from the recorder if noisy
(optional; they change rarely).

### 2. Dispatcher — `script.bedroom_run_scenario` (`files/scripts.yaml`)
A `choose:` on `input_select.bedroom_test_scenario`, reading `input_boolean.bedroom_test_fast`. Each
branch narrates via `persistent_notification.create` (fixed id `test_scenario`, so re-runs update in
place) describing intended + resulting state.

| Scenario | Fast (~30s) | Real | Mechanism (real path driven) |
|---|---|---|---|
| **bedtime** | `script.bedroom_bedtime(fade=30)` | `bedroom_bedtime` (`fade=1800`) | parameterized fade on the existing script |
| **wake** | `script.bedroom_preview_wake` frame-sweep | single computed frame + note to set a real alarm | **reuses tested `wake_brightness` macro** |
| **nightlight** | instant | instant | `scene.turn_on: scene.bedroom_nightlight` |
| **away** | `automation.trigger: bedroom_away` (`skip_condition: true`) | same | exercises the real away action verbatim |
| **arrive** | `automation.trigger: bedroom_arrive_home` | same | inline `if` conditions still run |
| **reset** | `script.bedroom_clear_overrides` + re-apply natural/fan | same | shared with the morning reset |

For **away** to exercise the notify path, the branch first ensures something is "on" (turn the lights
on via the mediator if both are off) so `on_items` is non-empty — otherwise the away action is
correctly silent and the test shows nothing.

### 3. Additive production-script changes (defaults = today's behavior)
- **`script.bedroom_bedtime`** gains optional field `fade` (default `1800`). The per-call
  `scene.turn_on` transition (already a per-call value, not baked into the scene) reads `fade`. Fast
  mode passes `30`. Verified additive: existing callers (`automation.bedroom_bedtime`, Tap Dial B3
  hold, the bedtime prompt action) pass no `fade` → unchanged 30-min fade.
- **`script.bedroom_preview_wake`** (NEW, test-only). Sweeps `elapsed_min` across the 30-min window
  (`0 → 15 → 30`, i.e. window-start → alarm → alarm+15), computing each brightness by calling the
  existing `wake_brightness(elapsed_min, sleep_min)` macro from `lighting.jinja` — `elapsed_min` is
  already a plain number the macro takes, so no time injection is needed; pass a representative
  `sleep_min` (or read `sensor.pixel_9_pro_sleep_duration`). Applies each frame with a short
  transition + `delay`, showing `1% → ~12% → 40%` in ~30s **without touching**
  `automation.bedroom_wake_ramp` or `script.bedroom_apply_wake`. Writes `light.bedroom_lights`
  directly (like `apply_wake`) → **needs a `state/sanctioned_writers.yml` exemption entry.**
- **`script.bedroom_clear_overrides`** (NEW, DRY extraction). The override-clearing currently inline
  in `automation.bedroom_morning_reset` (sleep_mode off, AL sleep off, manual_off off, fan_manual off)
  moves into this script; the morning reset calls it. The `reset` scenario calls it too, then
  re-applies natural lighting + fan. **This is a net DRY win independent of the harness** — one source
  of truth for "return the room to normal."

### 4. Dashboard card — `templates/ui-lovelace.yaml.j2`
A "🧪 Test scenarios" `entities` card (collapsible / at the bottom, out of the way): the
`input_select`, the `bedroom_test_fast` toggle, and a button row that calls
`script.bedroom_run_scenario`. One tap to run the selected scenario.

## Data flow

```
dashboard card
  → input_select.bedroom_test_scenario  +  input_boolean.bedroom_test_fast
  → [Run] button  →  script.bedroom_run_scenario
       → reads fast flag
       → dispatch: real script call  OR  automation.trigger(skip_condition)
       → persistent_notification.create(id=test_scenario)  narrates intended vs resulting state
```

## Phase 2 — offline regression tests (CI)

The **night-cycle math is already covered** (`wake_brightness`, `natural_exception`, `auto_light_allowed`
are unit-tested) — the harness was the missing piece *there*, not new tests. The remaining gap is the
**away/arrive selection logic**, currently inline templates in `automations.yaml`:
- Extract `bedroom_away`'s `on_items` selection into a pure macro (e.g. `away_shutoff_items(light_on,
  fan_on)` → list) in `lighting.jinja`, and `bedroom_arrive_home`'s relight gate into
  `arrive_relight_allowed(presence, manual_off, light_on)` → bool.
- Add truth-table tests to `tests/` joining the existing suite (`uv run pytest` / prek `pytest` hook /
  CI). Asserts "given lights+fan on → away shuts off both", "arrive relights only when present + not
  manual-off + currently off", etc.
- The YAML callers read entities and pass plain values to the macros (the repo's decision-macro
  convention). This makes the away/arrive *logic* regression-proof; the harness makes it *observable*.

## Guardrails accounted for (repo-specific)

- **`validate-ha-config` sanctioned-writers** (`state/sanctioned_writers.yml`): `bedroom_preview_wake`
  writes `light.bedroom_lights` directly → add an exemption entry. `bedroom_clear_overrides` writes
  the three override booleans → covered by `state/expected_override_writers.yml` (add it there, like
  the morning reset). `bedroom_run_scenario` drives via existing sanctioned paths
  (`bedroom_lights_set`/`bedroom_fan_set` mediator, `scene.turn_on`, `automation.trigger`).
- **Mediator `reason` contract**: any `bedroom_lights_set`/`bedroom_fan_set` calls from the harness
  use the declared `MEDIATOR_REASONS` vocabulary (quoted `reason`).
- **Copy-not-template**: all new HA Jinja lives in `files/scripts.yaml` / `lighting.jinja` (copied
  verbatim), never inline in the Ansible-templated `configuration.yaml.j2`. The helper *definitions*
  (`input_select`/`input_boolean`) are plain YAML and fine in `configuration.yaml.j2`.
- **Config-change wiring**: edits to `files/*` and `configuration.yaml.j2` already feed
  `common_config_changed`, so a deploy recreates HA (~120s).
- **Validate → deploy → confirm-loaded** via the `ha-edit-automation` / `ha-deploy` workflow;
  `probe.py ha verify-automations` confirms nothing regressed.

## Error handling / safety

- **Inert until run** — helpers do nothing on their own; no triggers fire the harness.
- **Reversible** — the `reset` scenario restores normal state (shared `bedroom_clear_overrides` +
  re-apply); narration makes a half-completed run obvious.
- **Time-bounded** — fast mode completes in ~30s, so a wedged run self-clears; no long-lived state.
- **Production-safe** — additive params default to current behavior; the harness can only *invoke*
  paths you could already trigger by hand, just packaged and narrated.

## Boundaries

Two helpers + one dispatcher script + two new small scripts (one of which is a DRY extraction that
stands alone) + one dashboard card + a Phase-2 macro extraction with tests. The only non-trivial new
logic — the wake frame-sweep — *reuses* the existing tested `wake_brightness` macro rather than
re-deriving the curve. Everything else is glue that drives real automations. Closes the "test the
night cycle / away on demand" gap; the away notification-hold path remains a documented manual check.
