# HA State Model — Phase 2: Actuator Mediator & Unified Intake — Design

Date: 2026-06-21 (detailed design added 2026-06-22)
Status: **Detailed design approved — ready for implementation plan.** Phase 1
(`2026-06-21-ha-state-model-phase1-representation-design.md`) is complete; its single-writer
report gave the exact worklist this phase consolidates and is the acceptance test for it.

## Problem this phase solves

Today ~12 automation/script paths write `light.bedroom_lights` directly and ~5 write
`fan.tower_fan`, each independently responsible for the relevant gates (`manual_off`,
`sleep_mode`, the lux gate, `person home`, AL coordination). Because the gating is **distributed
across automation conditions**, a change to one path can't see the full picture, and a new path
can silently omit a gate (e.g. the 2026-06-21 `bedroom_manual_light_detect` gap, where external
"off" surfaces didn't engage the override). The operator's goal: *anything that sets an actuator's
state runs through one checked path that respects all modes/flags per the particulars of the
action.*

## Consolidation shape (decided)

The 12 light writers are heterogeneous, so a single "only `bedroom_lights_set` may write the
lights" rule doesn't fit all of them. The chosen shape is **a mediator for held-state writes +
a small declared exemption list**:

- **Held-state setters** (natural / wake / nightlight / scenes / off, + the presence/away/absence/
  arrive/morning/Tap-Dial callers) route through the mediator.
- **Transient effects** (`bedroom_blip` off→15%→off, `bedroom_alert_pulse` snapshot→red→restore)
  and the **narrow color drift** (`bedroom_color_tracking`, color-only every 5 min) are **declared
  exemptions** — momentary or narrow, already tightly caller-gated; cramming them into a state-setter
  would distort them.

## Architecture — thin dispatcher + tested decision macro + delegate-not-absorb

`script.bedroom_lights_set(reason)` is the single front door for held-state light writes:

1. Reads the live flags: `manual_off`, `sleep_mode`, `person home`, `presence`,
   `binary_sensor.bedroom_auto_light_allowed` (the lux gate), `light_on`, `sun below horizon`.
2. Calls a new **unit-tested macro** `light_decision(reason, flags…) -> 'natural' | 'wake' |
   'nightlight' | 'scene_bright' | 'scene_relax' | 'off' | 'noop'` (in
   `custom_templates/lighting.jinja`, numbers/bools in → string out, like the existing fan/lighting
   macros). Gated reasons (`presence`/`auto`/`reset`) return their action or `noop`/`off` per the
   flags; ungated reasons (`manual`→`natural`, `nightlight`, `scene_bright`, `scene_relax`) map 1:1.
3. **Delegates to the existing, already-tested primitives** (no rewrite of the computation):
   `natural` → `script.bedroom_apply_natural` (which internally picks its nightlight/wake/default
   exception); `wake` → `script.bedroom_apply_wake`; `nightlight` → `scene.turn_on
   bedroom_nightlight`; `scene_bright`/`scene_relax` → `scene.turn_on bedroom_bright`/`bedroom_relax`;
   `off` → `light.turn_off`; `noop` → nothing.

**Sanctioned light module** = `bedroom_lights_set` + its internal primitives `apply_natural`,
`apply_wake`, `set_natural_brightness`, plus `bedroom_bedtime` (the 15-min fade routine — its own
script because its transition differs). **`apply_natural_gated` is ELIMINATED** — its lux-or-sun
gate becomes the macro's `reset` branch.

**Declared light exemptions** = `bedroom_blip`, `bedroom_alert_pulse`, `bedroom_color_tracking`.

**AL handshake**: no new logic — `set_natural_brightness` already marks AL taken-over (explicit
brightness) and `bedtime` already owns the `set_manual_control` dance; the mediator inherits both
by delegating to them.

### Light gate matrix (derived from current behavior — preserve, do not invent)

The macro encodes exactly today's gating:

| `reason` | called by (after migration) | macro gate | action |
|---|---|---|---|
| `presence` | `bedroom_presence_on` | manual_off off **&** sleep off **&** person home **&** presence on **&** lux-gate on **&** light off | `natural` else `noop` |
| `auto` | `bedroom_arrive_home`, `bedroom_morning_reset` (alarm) | person home (arrive adds: presence on, manual_off off, light off) | `natural` else `noop` |
| `wake` | `bedroom_wake_ramp`, `bedroom_morning_reset` | (caller already in-window) | `wake` |
| `reset` | Tap-Dial B1 HOLD | lux-gate on **OR** sun below horizon | `natural` else `off` |
| `manual` | Tap-Dial B1 press | ungated (caller runs `exit_sleep` first) | `natural` |
| `nightlight` | Tap-Dial B3, overnight presence | ungated | `nightlight` |
| `scene_bright` / `scene_relax` | Tap-Dial B2 press/hold | ungated (manual pick) | `scene.turn_on bright`/`relax` |
| `off` | `bedroom_away`, `bedroom_absence_off`, `bedroom_suppress_al_self_on_at_startup` | unconditional (caller decides when) | `off` |

`presence_on` loses its 6 conditions (they move into the macro) and becomes
`trigger → bedroom_lights_set('presence')`. `presence_blip` stays a separate sibling automation
(it calls the exempt `bedroom_blip`). `exit_sleep` stays in the Tap-Dial caller (a side effect, not
a light write). Scenes `bright`/`relax`/`nightlight` are invoked *by the mediator*, so they need no
separate exemption; the only scene-using exemption is `alert_pulse` (its internal
`scene.create`/`scene.turn_on` of `bedroom_pre_alert`).

## Fan consolidation (the simpler half)

`apply_fan` is already the temperature mediator. Mirror with `script.bedroom_fan_set(reason, level?)`:

| `reason` | called by today | action |
|---|---|---|
| `auto` | `bedroom_fan_temperature`, arrive, morning_reset | delegate `apply_fan` (curve + night/sleep caps) |
| `nudge` | Tap-Dial fan-dial | delegate `fan_nudge` (explicit ±1 level, engages override, ignores caps) |
| `boost` | `bedroom_notification_action` (`BEDROOM_BOOST_FAN`) | max level + engage `fan_manual` |
| `off` | `bedroom_away` | `fan.turn_off` |

**Sanctioned fan module** = `bedroom_fan_set` + `apply_fan` + `fan_nudge`. **Declared exemption** =
`bedroom_fan_startup_reconcile` (boot-only restart-recovery machinery). The override coordination
(`fan_manual` + `bedroom_fan_expected_level` accumulator) is unchanged.

## Enforcement

- **Single-writer → HARD (the structural guarantee).** A new tiny hand-declared
  `state/sanctioned_writers.yml` lists, per actuator, `module:` and `exemptions:`. The Phase-1
  validator already computes the derived writer set; flip `single_writer_report` from advisory to a
  **hard check**: every derived writer of `light.bedroom_lights` / `fan.tower_fan` must be in
  `module ∪ exemptions`, else CI fails. A new automation that writes an actuator directly (instead
  of via the mediator) fails the build. Low-rot, same philosophy as the override tripwire.
- **Override-consistency → stays a REPORT (refinement of the original forward spec).** Once all
  held-state writes go through the mediator, the override (`manual_off`) is read in one place, so
  "every auto path honors the override" is structurally true by construction; a separate hard check
  would be fragile and largely redundant. Keep it advisory (optionally a narrow structural assertion
  that `bedroom_lights_set` reads `manual_off`). Single-writer-hard is the real guarantee.

## External intake (unchanged — the deliberate seam)

HA has no service-call middleware, so external surfaces (Google Assistant, the dashboard tile, the
companion app, the DREO RF remote, AL's own engine) write the device directly and cannot be
pre-gated. They are normalized into overrides **after the fact** by the existing
`bedroom_manual_light_detect` / `bedroom_fan_manual_detect` (the `parent_id is none` detectors).
No change needed; this is the permanent seam, not a stopgap.

## Migration + rollout (live daily-use → incremental, verified)

Each group: edit → regenerate `derived_state.yml` (watch the writer set shrink) → `validate` +
unit tests → **deploy (`ha-deploy`)** → **verify live** (`probe.py ha-state` + the HA logbook).

1. **Macro + mediator, no caller migration.** TDD `light_decision` (exhaustive reason × flag table)
   + build `bedroom_lights_set`; deploy (mediator present but unused).
2. **Presence path** — `presence_on` → `bedroom_lights_set('presence')`.
3. **Auto/wake** — `arrive_home`, `morning_reset`, `wake_ramp`.
4. **Tap-Dial light branches** — `manual`/`reset`/`nightlight`/`scene_*`; remove `apply_natural_gated`.
5. **Off-paths** — `away`, `absence_off`, `suppress_al_self_on_at_startup`.
6. **Fan** — build `bedroom_fan_set`; migrate `fan_temperature`, arrive, morning_reset, the boost
   action, away.
7. **Lock it** — write `state/sanctioned_writers.yml`; flip single-writer to hard; regenerate →
   report empty → green.

## Testing

- **`light_decision` macro**: hermetic, exhaustive (every reason × the relevant flag combinations →
  expected action), in `ansible/roles/containers/home-assistant/tests/` (wired via `pyproject.toml`
  testpaths like the existing macro tests). This is where the gating correctness is pinned.
- **Structural** (automated): the single-writer report shrinking per group; `validate-ha-config`
  green; the generated `derived_state.yml`/`STATE.md` regenerated each step.
- **Behavioral** (partly time/disruption-bound → handed to the operator, as the Phase-1
  bedroom-lighting review did): presence auto-on, the morning wake ramp (needs a morning alarm),
  bedtime (disrupts the room mid-day), away/absence off, the Tap-Dial buttons. Structural checks
  gate the merge; behavioral checks are the operator's live confirmation.

## Files touched

- `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` — add
  `light_decision`.
- `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py` — add `light_decision` tests.
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — add `bedroom_lights_set` +
  `bedroom_fan_set`; remove `apply_natural_gated`.
- `ansible/roles/containers/home-assistant/files/automations.yaml` — migrate each caller to the
  mediator (groups 2–6).
- `ansible/roles/containers/home-assistant/state/sanctioned_writers.yml` — **new, hand-maintained**
  (per-actuator module + exemptions; the second small hand-declared file after the override tripwire).
- `scripts/ha_state_model.py` — flip `single_writer` to a hard check reading `sanctioned_writers.yml`;
  regenerate artifacts.
- `scripts/test_ha_state_model.py` — tests for the hard single-writer check (in-module ok, stray
  writer fails, exemption respected).

## Out of scope

- **Multi-area generalization** — bedroom-scoped first; revisit when a second area exists.
- **Replacing HA's service-call model** — impossible (no middleware); external intake is permanent.
- **New devices/integrations** — this phase only reorganizes control flow over existing actuators.

## Open questions from the forward spec — now resolved

- *Writer inventory + which are ungated* → the gate-matrix tables above (manual/scene/nightlight
  ungated; presence/auto/reset gated; off unconditional).
- *Gate matrix as a `choose:` script vs tested macro* → **tested macro** (`light_decision`) + thin
  dispatcher.
- *How the three scenes are re-expressed* → `nightlight` via the mediator's `nightlight` action;
  `bright`/`relax` via `scene_bright`/`scene_relax` reasons; `bedtime`'s 900s fade stays its own
  module script.
- *AL handshake* → inherited from the delegated primitives (`set_natural_brightness` /
  `bedtime`); no new handshake in the mediator.
