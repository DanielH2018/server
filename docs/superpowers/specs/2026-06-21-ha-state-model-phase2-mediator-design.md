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
   'off' | 'noop'` (in `custom_templates/lighting.jinja`, numbers/bools in → string out, like the
   existing fan/lighting macros). The one **gated** reason is `presence` (returns `natural` or
   `noop` per the flags); the rest map 1:1 (`natural`, `wake`, `off`).
3. **Delegates to the existing, already-tested primitives** (no rewrite of the computation):
   `natural` → `script.bedroom_apply_natural` (which internally picks its nightlight/wake/default
   exception); `wake` → `script.bedroom_apply_wake`; `off` → `light.turn_off`; `noop` → nothing.

**Scope (decided): the mediator covers the AUTO/programmatic paths only.** The manual **Tap Dial**
is a declared exemption — its writes are intentional and ungated by design, its brightness dial is a
latency-sensitive relative step, and the single-writer invariant is all-or-nothing per automation,
so funnelling it buys little safety at real cost. The Tap Dial is **not modified** in Phase 2
(`apply_natural_gated`, the B1-HOLD lux-or-sun helper, therefore stays).

**Sanctioned light module** = `bedroom_lights_set` + its internal primitives `apply_natural`,
`apply_wake`, `set_natural_brightness`, plus `bedroom_bedtime` (the 15-min fade routine — its own
script because its transition differs).

**Declared light exemptions** = `bedroom_tap_dial_control` (manual surface), `apply_natural_gated`
(Tap-Dial B1-HOLD helper), `bedroom_blip`, `bedroom_alert_pulse`, `bedroom_color_tracking`.

**AL handshake**: no new logic — `set_natural_brightness` already marks AL taken-over (explicit
brightness) and `bedtime` already owns the `set_manual_control` dance; the mediator inherits both
by delegating to them.

### Light gate matrix (derived from current behavior — preserve, do not invent)

The macro encodes exactly today's gating:

| `reason` | called by (after migration) | macro gate | action |
|---|---|---|---|
| `presence` | `bedroom_presence_on` | manual_off off **&** sleep off **&** person home **&** presence on **&** lux-gate on **&** light off | `natural` else `noop` |
| `natural` | `bedroom_arrive_home`, `bedroom_morning_reset` (alarm), `bedroom_wake_ramp` (window-end hand-back) | **ungated** — the caller keeps its own gate, which also guards its non-light side effects | `natural` |
| `wake` | `bedroom_wake_ramp` (in-window) | ungated (caller is already in-window) | `wake` |
| `off` | `bedroom_away`, `bedroom_absence_off`, `bedroom_suppress_al_self_on_at_startup` | unconditional (caller decides when) | `off` |

Only `presence` carries gates in the macro — `presence_on` is the one caller whose *sole* action is
the light, so its 6 conditions move into the macro and it becomes
`trigger → bedroom_lights_set('presence')`. `arrive_home` and `morning_reset` keep their existing
`if` (which also guards their fan/notify side effects) and pass the **ungated** `natural`; this
preserves their exact behavior (which differs from each other and from `presence`, so a shared gated
`auto` reason would not be faithful). `presence_blip` stays a separate sibling automation (calls the
exempt `bedroom_blip`).

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
3. **Auto/wake** — `arrive_home`, `morning_reset`, `wake_ramp` (the Tap Dial is NOT touched).
4. **Off-paths** — `away`, `absence_off`, `suppress_al_self_on_at_startup`.
5. **Fan** — build `bedroom_fan_set`; migrate `fan_temperature`, arrive, morning_reset, the boost
   action, away (the Tap Dial fan branches already call the module scripts `apply_fan`/`fan_nudge`,
   so they need no change).
6. **Lock it** — write `state/sanctioned_writers.yml` (module + the declared exemptions incl. the
   Tap Dial); flip single-writer to hard; regenerate → report empty → green.

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
  `bedroom_fan_set` (`apply_natural_gated` stays — it's the exempt Tap-Dial's helper).
- `ansible/roles/containers/home-assistant/files/automations.yaml` — migrate the AUTO callers to the
  mediator (groups 2–5: presence_on, arrive_home, morning_reset, wake_ramp, away, absence_off,
  suppress_al, and the fan callers). The Tap Dial automation is NOT modified.
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

- *Writer inventory + which are ungated* → the gate matrix above. Only `presence` is gated in the
  macro; `natural`/`wake` are ungated (callers keep their own gate); `off` is unconditional. The
  manual Tap Dial is exempt (intentional/ungated by design).
- *Gate matrix as a `choose:` script vs tested macro* → **tested macro** (`light_decision`) + thin
  dispatcher.
- *How the three scenes are re-expressed* → they stay in the **exempt** Tap Dial (manual surface),
  unchanged. The mediator delegates to `apply_natural` (which uses the nightlight scene internally);
  `bedtime`'s 900s fade stays its own module script.
- *AL handshake* → inherited from the delegated primitives (`set_natural_brightness` /
  `bedtime`); no new handshake in the mediator.
