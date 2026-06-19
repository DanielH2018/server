# Home Assistant — Jinja logic unit testing (logic layer)

**Date:** 2026-06-19
**Status:** Approved design, pending implementation plan

## Problem

The bedroom Home Assistant config is unusually logic-heavy. Almost every bug in recent
sessions has been in the **Jinja computation**, not the YAML structure: the fan curve
(`(t−71)/1.3`), the level round-trip (`pct_to_level`/`level_to_pct`), the morning wake ramp,
the lux gate threshold. None of this is currently tested — it is validated only by deploying to
the live bedroom and watching behavior, which is slow and unreliable as a regression guard.

The repo already has a strong, lightweight test culture (`uv run pytest`, several `pytest`
suites wired into the prek hook and CI). HA logic testing should fit that culture, not bolt on a
heavy HA-runtime dependency.

## Goal & scope

A fast, runtime-free `pytest` suite covering the three bug-prone Jinja hot-spots — **fan curve,
wake ramp, lux gate** — plus the already-pure `fan.jinja` level round-trip. Runs in
`uv run pytest`, the prek `pytest` hook, and CI with no Home Assistant process.

**Layered, logic-first.** Config validation (`hass --script check_config` against the HA image)
is deliberately **deferred** to a separate follow-up spec. Full automation behavior testing
(trigger→condition→action against a live HA instance) is **out of scope** — there is no clean
off-the-shelf harness for *user* automations, only custom components.

## Key technical insight: HA's `round` is not Jinja's `round`

HA's template engine **is** Jinja2 (`ImmutableSandboxedEnvironment`), but HA overrides several
filters. Critically:

- **`round`** → HA's `forgiving_round`, which uses Python's **banker's rounding**
  (round-half-to-**even**) and returns an `int` at precision 0.
- Jinja2's **stock** `round` rounds half **away from zero** and returns a `float`.

They differ exactly at `.5` boundaries — and `fan.jinja`'s level math lands on `.5` midpoints
**by design** (`level_to_pct` sends the midpoint of each level's range so the integration's
`ceil()` lands on the target level). A naive "just use plain Jinja2" harness would silently
disagree with HA on precisely the values that matter most.

**Therefore:** the test harness registers a faithful `round`/`float`/`int` shim mirroring HA's
`forgiving_*` semantics. That 3-filter shim is the **entire** HA-semantics surface the tests
depend on, and it is pinned by its own test so it can never silently drift.

## Architecture

Two layers, mirroring the existing `fan.jinja` precedent (extract pure math into a shared macro
that callers import, so the logic "can never drift").

### 1. Extract math into pure macros

Entity reads (`states()`, `now()`, `as_datetime`, `state_attr`) stay in the YAML callers. Macros
take **plain numbers → return numbers/bools**, making them drift-proof and unit-testable.

**`files/custom_templates/fan.jinja` (extend):**
- Add `fan_target_level(temp_f, cur_level, is_night, sleep)` → `0..9`. Encapsulates the
  `(temp_f − 71)/1.3` ideal curve, the ±0.7-level hysteresis deadband, and the sleep/night caps
  (`2 if sleep else (4 if is_night else 9)`).
- Keep `pct_to_level` / `level_to_pct` unchanged.

**`files/custom_templates/lighting.jinja` (new):**
- `in_wake_window(elapsed_min)` → bool (`0 ≤ elapsed_min < 15`). Replaces the `in_window`
  expression currently duplicated across the nightlight exception, the wake exception, and
  `bedroom_presence_on`.
- `wake_brightness(elapsed_min, sleep_min)` → `1..peak`, where
  `peak = 30 if (0 < sleep_min < 360) else 50`, value `= (1 + (peak−1)·elapsed_min/15) round int`.
- `wake_transition(elapsed_min)` → seconds remaining: `(15 − elapsed_min)·60 round int`.
- `auto_light_allowed(in_window, illuminance)` → bool (`in_window or illuminance < 50`).

**Rewire callers** (pure refactor — behavior preserved):
- `files/scripts.yaml` → `bedroom_apply_fan` (fan curve), `bedroom_apply_natural` (wake ramp +
  the nightlight exception's `in_window`).
- `files/templates.yaml` → `bedroom_auto_light_allowed` (lux gate), and `bedroom_wake_start`
  window consumers reuse `in_wake_window`.
- `files/automations.yaml` → `bedroom_presence_on`'s window condition reuses `in_wake_window`.

This also **DRYs the triplicated `in_window` formula** into one macro — a code improvement, not
just test scaffolding.

**Migration safety net (implementation step, not a permanent test):** before deleting each
inline formula, render *old inline vs new macro* across an input grid in the harness and assert
equality, so the extraction is provably behavior-preserving at the unit level.

### 2. Test harness + suites

Location: `ansible/roles/containers/home-assistant/tests/` (mirrors the monitor-bridge /
terraria-stats placement; NOT under `ansible/filter_plugins/`, which the Ansible plugin loader
would import at deploy time).

- **`jinja_harness.py`** — `render_macro(file, macro, *args)` over
  `jinja2.Environment(FileSystemLoader(files/custom_templates))`, registering faithful HA
  `round`/`float`/`int` overrides. ~15 auditable lines; the only HA-semantics replicated.
- **`test_fan_macros.py`** —
  - Round-trip property: `pct_to_level(level_to_pct(L)) == L` for `L ∈ 0..9` (the comment's
    promise, made executable).
  - Curve endpoints: `<72°F → 0`, `72 → 1`, `~82 → 9`.
  - Hysteresis: no level step when the temp wants a level within ±0.7 of `cur_level`; turning on
    from 0 jumps to ideal.
  - Caps: `sleep → ≤ 2`, `night → ≤ 4`, uncapped → up to 9.
- **`test_lighting_macros.py`** —
  - Wake ramp: `elapsed 0 → 1%`, `elapsed 15 → peak`, short night (`0 < sleep_min < 360`) peak
    30 vs normal 50, unknown/0 sleep falls back to 50.
  - Window boundaries: `0` in, `15` out, negative out.
  - Lux gate truth table: `in_window` true ⇒ allowed regardless of lux; else allowed iff
    `illuminance < 50`.
- **`test_ha_round_semantics.py`** — pins banker's rounding (`round(2.5) == 2`,
  `round(0.5) == 0`, `round(1.5) == 2`) so the harness can never silently drift from HA.

### 3. Wiring

- Add `"ansible/roles/containers/home-assistant/tests"` to `testpaths` in `pyproject.toml`
  (single source of truth consumed by both `uv run pytest` and the prek `pytest` hook → also CI).
- Switch the `custom_templates` deploy task in `tasks/main.yml` from a hardcoded per-file copy
  (`src: custom_templates/fan.jinja`) to a **directory copy** so new `.jinja` files ship to
  `/config/custom_templates/` automatically. Keep it registered into `common_config_changed`.

### 4. Deploy & verify live

After tests are green, deploy home-assistant
(`uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`, ~120s recreate) and verify
the fan and lights still behave — the real proof that the extraction preserved behavior, since
unit tests cannot observe HA's runtime template rendering. Use
`scripts/probe.py health home-assistant` as the post-deploy gate and spot-check the rendered
`bedroom_apply_fan` / wake values in HA Developer Tools → Template.

## Out of scope / deferred

- **Config validation** (`hass --script check_config`) — separate follow-up spec; needs the HA
  Docker image and resolved includes/secrets.
- **Automation behavior** (end-to-end trigger→action) — no clean user-automation harness.
- **Further macro extraction** (notify importance/channel, threshold label-strip + `cfg` map) —
  candidates for a later pass once the pattern is established.

## Testing strategy

TDD per the repo norm: for each macro, write the characterization/property test first (pinning
the *current* inline formula's outputs), then extract the macro to satisfy it, then rewire the
caller. The harness's `round`/`float`/`int` shim is itself tested (`test_ha_round_semantics.py`)
before any macro test relies on it.

## Risks & mitigations

- **Refactoring delicate, recently-stabilized automation logic** → pure-refactor discipline +
  the old-vs-new render-equality safety net + live deploy verification.
- **Harness drifting from HA's filter semantics** → minimal shim surface (3 filters), each
  pinned by an explicit test; macros use only arithmetic + `float`/`int`/`round`/`min`.
- **New macro file not deployed to HA** → directory copy in `tasks/main.yml` so any
  `custom_templates/*.jinja` ships automatically.
