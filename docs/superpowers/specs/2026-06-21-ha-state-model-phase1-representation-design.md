# HA State Model — Phase 1: Derived Representation & Guardrails — Design

Date: 2026-06-21
Status: Approved direction; ready for implementation plan.
Phase 2 (the actuator mediator refactor) is a separate spec:
`2026-06-21-ha-state-model-phase2-mediator-design.md`. Phase 1 is its prerequisite.

## Problem

The bedroom automation has become a distributed reactive system: 29 automations + 11
scripts coordinate through a handful of shared **override cells** (`bedroom_manual_off`,
`bedroom_fan_manual`, `bedroom_sleep_mode`, the expected-value accumulators, the
`bedroom_fan_dial` timer) plus Adaptive Lighting's internal state and the device actuators.
The same actuator (`light.bedroom_lights`, `fan.tower_fan`) is driven by automations **and**
manual surfaces (Tap Dial, dashboard, Google Assistant, the DREO remote, AL's own engine).

The knowledge of how these pieces interact — who writes which cell, which automations
respect it, the restart-survival rules, the feedback-loop traps — exists **only as prose**
(the role `CLAUDE.md`) and operator memory. That representation **drifts**: confirmed live
on 2026-06-21, `CLAUDE.md:70` names a non-existent AL sleep-mode switch entity_id
(`switch.bedroom_adaptive_lighting_sleep_mode_bedroom` — "Entity not found") while the code
correctly uses `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`. The
hand-written doc was wrong while the code was right.

## Goals

1. A **clear, extensible, drift-resistant representation** of the control-plane state —
   every cell/actuator, who writes it, who reads it — usable both to edit safely and to debug.
2. **Drift made structurally impossible**, not merely discouraged: the representation is
   *generated from the real config* and a CI freshness gate rejects any change that doesn't
   regenerate it.
3. **CI guardrails** that catch the overlap-bugs this system actually has (a manual surface
   that doesn't engage its override; a bad/renamed entity reference; a half-added engine
   category).
4. A **live debug view** of current cell values + anomalies.
5. **Measure the Phase 2 gap** (how many paths write each actuator directly) without yet
   refactoring.

## Design principles

- **Derive as much as possible.** Hand-declare only facts that exist nowhere else in the repo
  (so there is nothing for them to drift *from*) — the same discipline `secret_rotation.yml`
  uses (it hand-tracks dates/tiers, which live nowhere else, but `sync`s names against reality).
- **Drift is a build failure**, via a regenerate-and-diff freshness gate.
- **One validator, one hook.** Extend the existing `validate_ha_config.py` / `validate-ha-config`
  prek+CI slot; do not add a parallel tool/hook.
- **Keep the irreducible "why" in `CLAUDE.md`.** The runtime/physical traps (lux feedback
  loop, DREO parent-less cloud echo, AL self-on at startup, stale-restore) are *not* derivable
  from config and *not* statically checkable — they stay as prose; the generated doc links them.

## Out of scope (this phase)

- The **actuator-mediator refactor** (single guarded writer per actuator, external-surface
  intake normalization, flipping the invariants to hard-fail) — that is Phase 2.
- Modeling passive entities (every pollutant/battery/phone sensor) as first-class nodes — they
  appear only in the live `--inventory` dump.
- **Multi-area namespacing** — built for the bedroom; kept trivially namespaceable for a future
  second area (YAGNI — no second area exists).
- HA *schema*/entity-existence validation against a live HA image — out of scope (the live
  deploy still catches schema errors; this phase resolves references against derivable +
  snapshotted entity sets).

## What is modeled (the control plane)

**Cells (coordination state):**

| Cell | Type | Role |
|---|---|---|
| `bedroom_manual_off` | input_boolean | lights override |
| `bedroom_fan_manual` | input_boolean | fan override |
| `bedroom_sleep_mode` | input_boolean | quiet-night mode / lighting gate |
| `bedroom_fan_expected_level` | input_number | DREO cloud-echo accumulator |
| `bedroom_light_expected_color_temp` | input_number | color-tracker baseline |
| `ha_heartbeat` | input_datetime | cross-system liveness (monitor-bridge reads it) |
| `bedroom_fan_dial` | timer | fan-dial mode (`active` IS the mode) |
| AL master | `switch.bedroom_adaptive_lighting_bedroom` | AL on/off (a co-writer of the lights) |
| AL sleep | `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom` | AL sleep mode |
| AL per-light `manual_control` | (via `adaptive_lighting.set_manual_control`) | AL hands-off flag |

**Actuators:** `light.bedroom_lights`, `fan.tower_fan`.

**Sensors / read-only deps** (referenced, resolution-checked, not owned): `person.daniel`,
`binary_sensor.aqara_fp300_presence`, `binary_sensor.bedroom_auto_light_allowed`, `sun.sun`,
the AirGradient + UPS + outdoor sensors, the watch/phone sensors.

## Components

### 1. Extractor — the parsing core

Reuses `validate_ha_config.py`'s `HAConfigLoader` + `assemble_config()` to load the real
`!include` tree (automations/scripts/scenes/templates/configuration). For each automation and
script it walks the action tree **recursively** — `choose: → conditions/sequence`, `if/then/else`,
`repeat: → sequence/for_each`, `parallel`, nested `sequence` — so writes buried in branches are
not missed (verified necessary: `bedroom_fan_expected_level` is written at `automations.yaml:166`
inside a button-4-hold `else:` branch; the whole Tap Dial automation is one big `choose:`).

**Writes recognized** (the load-bearing set — keys off the service domain, not just `input_*`):
- `input_boolean.turn_on|turn_off|toggle`, `input_number.set_value`, `timer.start|cancel|pause|finish`
- `switch.turn_on|turn_off|toggle` (catches the AL master + sleep switches)
- `adaptive_lighting.set_manual_control` (+ `apply`) → writes AL `manual_control` / the lights
- `scene.turn_on` → resolved to the entities the scene sets (scenes embed only
  `light.bedroom_lights` today) so a `scene.turn_on: bedroom_nightlight` counts as a **light write**
- `scene.create` (the alert-pulse snapshot) → a transient-scene write
- `light.turn_on|off|toggle`, `fan.turn_on|off|set_percentage|set_preset_mode`
- `homeassistant.turn_on|off|toggle` (generic; none today, but a documented HA pattern)

**Entity-id forms handled:** `target.entity_id`, legacy top-level `entity_id`, `data.entity_id`;
scalar **and** list values (flattened — e.g. `morning_reset` clears three booleans in one call at
`automations.yaml:387`). Templated entity_ids (`{{ repeat.item }}`) are flagged `dynamic` and
reported, not silently dropped.

**Reads (advisory only):** best-effort scan of `states()/is_state()/state_attr()` in templates +
`trigger`/`condition` entity refs. Per the honesty caveat, reads are **documentation, not a hard
gate** (they hide in Jinja and can't be enumerated reliably).

**Attribution:** a write inside `script.X` is owned by `script.X`. The automation→script call
graph is followed to annotate the doc with the triggering automation (advisory).

### 2. Generator — `scripts/ha_state_model.py generate`

Emits two **committed, generated** artifacts (never hand-edited) under
`ansible/roles/containers/home-assistant/state/`:

- **`derived_state.yml`** — machine-readable. Per cell/actuator: `{ writers: [...], readers: [...],
  references: [...] }`, plus `dynamic_writes` for templated targets. Deterministically sorted for
  clean diffs.
- **`STATE.md`** — human-readable: the cells table (with each cell's `purpose` **extracted from the
  existing explanatory comment** above its definition in `configuration.yaml.j2` — derive, don't
  re-author), per-actuator writer/reader lists, the **override → input-surfaces map**, and a compact
  Mermaid graph of the *override subsystem only* (the 3 booleans + their writers/readers — small and
  readable; the 20-writer actuator is shown as a grouped list, never a Mermaid hairball). Links to
  `CLAUDE.md` for the "why".

### 3. Refresh — `scripts/ha_state_model.py refresh`

Snapshots the **integration-provided** entity ids (device entities like `fan.tower_fan`,
`sensor.aqara_fp300_*` that exist in no repo file) from live HA via the existing probe auth path,
writing **`state/external_entities.yml`** (committed, generated — only changes when hardware
changes). The validator's reference check uses it; a referenced entity missing from it means
"typo, or run `refresh`."

### 4. Validator — checks (extends `validate_ha_config.py`, same prek/CI hook)

Hard fails:
- **Freshness:** re-run `generate` in CI; fail if the committed `derived_state.yml`/`STATE.md`
  differ from the regenerated output (the drift gate). You cannot merge a config change without the
  representation matching it.
- **Entity-reference resolution:** every referenced `light./fan./switch./scene./binary_sensor./
  input_*/timer.` entity resolves against the **repo-derivable** set (declared helpers, scene ids,
  threshold sensors, template sensors, REST sensors, AL-generated switches) ∪ `external_entities.yml`.
  Unresolved → fail. Templated refs skipped + reported. (Would have caught the AL-switch typo had it
  been in code.)
- **Override-writer tripwire** — the **only deliberately hand-declared fact in the state model**
  (the role `CLAUDE.md`'s "why" prose is the separate, irreducible exception — it is knowledge that
  lives in no config file): a tiny `expected_override_writers` list for the three booleans only. CI
  fails if the *derived* writer set for a boolean diverges from the declared list. This forces a
  conscious "I am touching shared coordination state" acknowledgement at edit time (the value is the
  friction, not the data).
- **Structural completeness** of the generic engines: every threshold `_bad` trigger has its `_ok`;
  every category in a trigger `id` has a `cfg` map entry; the threshold automation's trigger entity
  list matches the declared `binary_sensor: threshold` set.
- **Alias-slug sanity:** each automation's `automation.<slug>` (slugified `alias`) is what the state
  machine uses; flag id/alias mismatches that would break verification.
- **recorder `exclude` references** resolve to real entities.

Report mode (warn, do not fail — these establish the Phase 2 baseline):
- **Override-consistency:** for each actuator+override, diff the *manual-surface writers* against the
  *override writers*; warn on asymmetry (a manual entry-point that changes the actuator without
  engaging the override — the `bedroom_manual_light_detect`-class gap).
- **Single-writer:** list every writer of `light.bedroom_lights` / `fan.tower_fan` beyond the
  sanctioned funnel script. Today ~15 for the lights — the Phase 2 worklist.

### 5. Live view — `scripts/probe.py ha-state`

New subcommand in the existing `ha` group (read-only GET, allow-listed, no prompt). Reads
`derived_state.yml` for the cell list, queries live HA, renders:
- each **cell**: current value + `last_changed` age;
- each **automation**: enabled? + `last_triggered` age;
- an **anomaly summary** at the top (e.g. `sleep_mode=on` outside a sane window; `manual_off=on`
  but the lights are following the auto color/brightness; `fan_dial` timer `active` > 5 min; an
  override `on` with no live cause);
- `--inventory`: the full live entity catalog grouped by device (the "full inventory" tier).

### 6. CLAUDE.md + freebie

- Keep the role `CLAUDE.md` as the home of the non-derivable "why"; add a pointer to `STATE.md`
  for the derived/structural facts.
- **Fix `CLAUDE.md:70`**: `switch.bedroom_adaptive_lighting_sleep_mode_bedroom` →
  `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`.

## Files touched

- `scripts/ha_state_model.py` — **new** (extractor + `generate`/`refresh` + the new checks).
- `scripts/test_ha_state_model.py` — **new** (hermetic, fixture-driven).
- `scripts/probe.py` — extend with the `ha-state` subcommand.
- `scripts/validate_ha_config.py` — call into `ha_state_model`'s checks (or vice-versa) so one hook
  runs both; no second hook.
- `ansible/roles/containers/home-assistant/state/derived_state.yml` — **new, generated, committed**.
- `ansible/roles/containers/home-assistant/state/STATE.md` — **new, generated, committed**.
- `ansible/roles/containers/home-assistant/state/external_entities.yml` — **new, generated, committed**.
- `ansible/roles/containers/home-assistant/state/expected_override_writers.yml` — **new, tiny,
  hand-maintained** (the 3-boolean tripwire; the only hand-declared file in the state model —
  `CLAUDE.md` prose aside).
- `prek.toml` — wire freshness+checks into the existing `validate-ha-config` hook (or a sibling that
  shares the slot).
- `ansible/roles/containers/home-assistant/CLAUDE.md` — pointer to `STATE.md` + the `:70` fix.
- `pyproject.toml` — `testpaths` already includes `scripts/`; confirm the new test is collected.

## Testing (hermetic — no live HA)

- **Extractor:** fixtures of automation/script YAML → expected writer/reader sets, exercising nested
  `choose/if/else/repeat`, list-valued targets, `target` vs `entity_id` vs `data.entity_id`,
  `scene.turn_on` → light resolution, AL `switch.*`/`set_manual_control` writes, templated-target
  flagging.
- **Each check** positive + negative: unresolved entity, override-writer divergence, missing
  threshold `_ok`, category↔`cfg` mismatch, alias-slug mismatch, stale generated artifact (freshness).
- **Report-mode invariants** produce the expected warning lists on a fixture with a known gap.
- Wired into `pyproject.toml` `testpaths` + the prek `pytest` hook + CI.

## Rollout

1. Build extractor + `generate` + tests (TDD); generate the initial `derived_state.yml`/`STATE.md`;
   commit.
2. Add `refresh` + `external_entities.yml`; add the reference-resolution + structural + alias-slug +
   recorder checks (hard) and the override-consistency + single-writer reports.
3. Add the `expected_override_writers.yml` tripwire (seed from the current derived writers).
4. Add `probe.py ha-state`.
5. Run the full validator; fix the `CLAUDE.md:70` typo; capture the single-writer/override-consistency
   reports as the documented **Phase 2 baseline**.

## Defaults chosen (easy to change)

- Generated artifacts live in `…/home-assistant/state/`.
- **Hard** checks: freshness, entity-resolution, override-writer tripwire, structural completeness,
  alias-slug, recorder refs. **Report** checks: override-consistency, single-writer (Phase 2 flips
  these to hard).
- `STATE.md` shows the override subsystem as a small Mermaid graph + everything else as grouped
  lists (no actuator hairball). Drop the Mermaid block entirely if it proves noisy — it's generated,
  so the choice costs nothing.
- The override tripwire covers exactly the 3 booleans (`manual_off`, `fan_manual`, `sleep_mode`),
  not the accumulators, timer, or actuators.
