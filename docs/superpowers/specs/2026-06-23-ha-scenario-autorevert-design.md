# HA Scenario Harness — Fast-Mode Auto-Revert — Design

**Date:** 2026-06-23
**Status:** approved (pre-plan)
**Origin:** Follow-on to the scenario test harness (`docs/superpowers/specs/2026-06-23-ha-scenario-test-harness-design.md`).
Running a fast-mode scenario leaves the room in whatever state it produced — most notably `bedtime`
sets `input_boolean.bedroom_sleep_mode` (a coordination flag that silences notifications, caps the
fan, and gates `presence_on` off) and it sticks until the morning reset / a Tap Dial action / `reset`.
The operator wants fast scenarios to be ephemeral: **revert to the exact prior state after the test.**

## Governing principle

> **Fast = ephemeral preview** (snapshot → run → observe → restore the exact prior state).
> **Real = apply and keep** (unchanged). The snapshot/restore wraps the existing per-scenario
> `choose:` without changing it; it reuses HA's `scene.create` snapshot trick (already used by
> `bedroom_alert_pulse`) for the visible state plus a few `states()` reads for the sticky flags.

## Goal

In fast mode, every wrappable scenario returns the room to exactly its pre-test state (lights
brightness+color, fan, and the sticky flags `sleep_mode`/`manual_off`/`fan_manual` + AL sleep) after a
per-scenario observation window. Real mode is unchanged.

## Non-Goals

- **`reset` is never wrapped** — it IS the baseline; snapshot/restoring it would undo the reset.
  `off`/none does nothing (no scenario ran).
- **Real mode is untouched** — it applies and keeps (you want to experience/keep the real effect).
- **Internal accumulators are NOT snapshotted** (`input_number.bedroom_fan_expected_level`,
  `bedroom_light_expected_color_temp`) — they self-correct on the next temp/auto event. Restoring the
  fan via `scene.turn_on` is a parented call, so `bedroom_fan_manual_detect` won't false-trip.
- **No new unit test** — this is HA orchestration (snapshot/restore sequencing), not pure macro math;
  validated structurally (`validate_ha_config.py`) + live, consistent with the rest of the harness.

## Components — all in `bedroom_run_scenario` (`files/scripts.yaml`) + 2 state files

The script gains a `wrap` gate and two conditional blocks around the **unchanged** `choose:`.

### 1. `wrap` decision + observe window (top, in the existing `variables:`)
- `wrap = fast and scenario in ['bedtime','wake','nightlight','away','arrive']` (i.e. fast, and not
  `reset`/`off`/unknown).
- `observe` (seconds, per-scenario, tunable): **bedtime = 30** (the fade is async on the bulb — wait
  for it), **wake = 1** (`script.bedroom_preview_wake` already blocks ~30s while it sweeps),
  **nightlight/away/arrive = 4**.

### 2. Snapshot (before the `choose:`, `if wrap`)
- Capture the four sticky flags as sequence variables (in scope for the whole sequence):
  `snap_sleep`, `snap_manual_off`, `snap_fan_manual` (from the three `input_boolean`s) and
  `snap_al_sleep` (from `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`).
- `scene.create: scene_id: bedroom_pre_test, snapshot_entities: [light.bedroom_lights, fan.tower_fan]`
  — captures the visible state. (Distinct id from `bedroom_alert_pulse`'s `bedroom_pre_alert`.)

### 3. The `choose:` — UNCHANGED (the 6 scenario branches + `default:`).

### 4. Restore (after the `choose:`, `if wrap`)
- `delay: "{{ observe }}"` — the observation window so you see the effect.
- `scene.turn_on: scene.bedroom_pre_test` with `transition: 1` — restores lights + fan.
- Restore each sticky flag to its captured value: `input_boolean.turn_on/off` per `snap_*`, and the AL
  sleep switch `switch.turn_on/off` per `snap_al_sleep`.

### 5. Narration
The fast-path narration reflects auto-revert (e.g. bedtime: "Previewed the bedtime fade; restored to
your prior state."); real-path narration keeps "…run `reset` to clear." The restore block may post a
final "↩️ restored to pre-test state" `persistent_notification` (id `test_scenario`, updates in place).

## Validator-gate impacts (state model)

The restore makes `bedroom_run_scenario` write entities it didn't before — update the guards or CI fails:
- **`state/expected_override_writers.yml`:** add `script.bedroom_run_scenario` to ALL THREE override
  booleans (`bedroom_sleep_mode`/`bedroom_manual_off`/`bedroom_fan_manual`) — the restore writes them.
- **`state/sanctioned_writers.yml`:** add `script.bedroom_run_scenario` to the `fan.tower_fan`
  `exemptions:` (the snapshot-restore `scene.turn_on` writes the fan). It already has the
  `light.bedroom_lights` exemption.
- `scene.bedroom_pre_test` is a `scene.create` runtime scene — the model's `created_scenes` already
  tracks it, so it resolves; `scene.turn_on` of it counts as a light+fan write (covered above).
- Regenerate `state/derived_state.yml` + `state/STATE.md`.

## Error handling / safety

- **Reset still recovers everything** — if a `mode: restart` re-run cancels an in-flight restore (Run
  spammed mid-observe), the room may be left in the scenario state; `reset` clears it. Acceptable for a
  test tool.
- **Snapshot is overwritten each run** (like `bedroom_pre_alert`) — no stale-scene accumulation.
- **Inert until Run**, real mode unaffected, fast runs bounded (observe ≤ 30s + restore).

## Boundaries

One script restructured (`bedroom_run_scenario`: a `wrap` gate + snapshot block + restore block around
the untouched `choose:`) + two state-file declarations + a regen. Reuses the existing `scene.create`
snapshot pattern; no new helpers, no new unit test. Closes the "fast tests leave sleep_mode (and the
rest) stuck" gap.
