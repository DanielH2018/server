# HA State Model — Phase 2: Actuator Mediator & Unified Intake — Design (forward spec)

Date: 2026-06-21
Status: **Forward spec.** The detailed design is intentionally deferred until Phase 1
(`2026-06-21-ha-state-model-phase1-representation-design.md`) lands — Phase 1's
single-writer / override-consistency **reports** produce the concrete worklist (the exact set
of paths that write each actuator, and which are deliberately ungated) that this phase
consolidates. This document fixes the **direction, target architecture, and boundaries** so
Phase 1 is built toward them; it will be sharpened into an implementation plan after Phase 1.

## Problem this phase solves

Today, ~15 automation/script paths call `light.bedroom_lights` directly (and several call the
fan directly), each independently responsible for remembering the relevant modes/flags
(`manual_off`, `sleep_mode`, the lux gate, `person home`, AL coordination). Because the logic is
**distributed**, a change to one path can't see the full picture, and gaps appear (e.g. the
2026-06-21 `bedroom_manual_light_detect` fix: three input surfaces could turn the lights off
without engaging the override, so presence re-lit them ~30 s later).

The operator's goal, stated directly: *anything that touches an actuator should run through a
single checked path that respects all modes/flags according to the particulars of the action.*

## Target architecture — one guarded writer per actuator

Split by what HA actually allows (HA has **no service-call middleware** — you cannot intercept a
`light.turn_on`):

1. **Internal actions → an intent-aware mediator (fully funnel-able).**
   `script.bedroom_lights_set(reason, brightness?, color?, transition?, force?)` becomes the **sole
   sanctioned writer** of `light.bedroom_lights`; every internal automation/script calls it instead
   of `light.turn_on`/`scene.turn_on`. It owns the **gate matrix per `reason`**:
   - `presence`/`auto` → fully gated (lux gate, `manual_off`, `person home`, sleep exception);
   - manual (`tap_bright`/`tap_relax`/scene) → **intentionally ungated** (the user asked);
   - `wake` → fixed warm 2200 K ramp frame; `nightlight` → sleep-aware; etc.
   This generalizes today's `script.bedroom_apply_natural` (already an intent-aware `choose:`) to
   **all** light actions. The fan already has a proto-mediator (`script.bedroom_apply_fan` with the
   night/sleep caps); Phase 2 makes it the **sole** writer of `fan.tower_fan` (route the dial-nudge
   and startup-reconcile through it).

2. **External surfaces → detect-and-reconcile at intake (cannot be pre-gated).**
   Google Assistant, the dashboard tile, the physical fan remote, and **AL's own engine** write the
   device directly — HA won't route them through the mediator. They are normalized into
   overrides/intent **after the fact** by the existing detectors (`bedroom_manual_light_detect`,
   `bedroom_fan_manual_detect`), extended as needed. AL is treated as a **co-writer** the mediator
   coordinates with via `take_over_control` / `manual_control`, not one it funnels.

Net: internal actions go through the mediator; external actions become flags at intake; both feed
the same flag-aware state. That is the achievable "unified" model given HA's constraints.

## Enforcement (flip Phase 1's report-mode invariants to HARD)

- **Single-writer (hard):** the only sanctioned writer of `light.bedroom_lights` is
  `script.bedroom_lights_set`; of `fan.tower_fan`, `script.bedroom_apply_fan`. Any other direct
  `light.*`/`fan.*`/`scene.turn_on` write is a CI failure. (Scenes are invoked *through* the
  mediator, or inlined into it.)
- **Override-consistency (hard):** every **manual-surface** entry-point engages the corresponding
  override; every **automatic on-path** reads/gates on it. A small **declared exception list**
  carries the deliberate bypasses (`BEDROOM_AWAY_TURN_ON`, the manual Tap-Dial scene presses) so the
  rule encodes intent rather than a blanket assertion.

Phase 1 built these checks in *report* mode; Phase 2's completion criterion is literally "the
reports are empty, so the invariants can be flipped to hard."

## Migration (safe *because of* Phase 1)

- Use Phase 1's single-writer **report as the worklist**. Consolidate one caller at a time behind
  the mediator; **regenerate `derived_state.yml` after each step** and watch the actuator's writer
  set shrink toward `{ mediator }`; keep tests green throughout.
- **TDD the gate matrix** as a pure, tested macro (flags/numbers in → decision out), per the repo's
  `custom_templates/*.jinja` + `tests/` convention — the mediator script stays thin (entity reads +
  the service call); the policy is unit-tested.
- Flip each invariant to hard only once its report is empty.

## Open questions Phase 1 will answer

- The exact writer inventory per actuator, and which writers are **deliberately ungated** (defines
  the mediator's `reason` set and the override-consistency exception list).
- Whether the gate matrix is one `choose:` script or a tested policy macro + a thin dispatch script.
- How the three scenes (`bright`/`relax`/`nightlight`) are re-expressed behind the mediator.
- The precise AL `take_over_control`/`manual_control` handshake the mediator must own.

## Out of scope

- **Multi-area generalization** — only when a second area actually exists; the mediator is
  bedroom-scoped first.
- **Replacing HA's service-call model** — impossible (no middleware); the external-intake layer is
  the deliberate, permanent seam, not a stopgap.
- Any new *devices/integrations* — this phase only reorganizes control flow over existing actuators.
