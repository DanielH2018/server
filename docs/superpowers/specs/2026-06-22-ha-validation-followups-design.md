# HA Validation-Layer Follow-ups — Design

**Date:** 2026-06-22
**Status:** approved (pre-plan)
**Predecessor:** `2026-06-22-ha-testing-interaction-hardening-design.md` (this is a small,
additive follow-on to that work — two further deterministic checks, no new mechanisms.)

## Context

A review of the HA validation surface against the current code found the static layer already
comprehensive (YAML/include/template syntax, entity + service reference resolution, mediator-reason
contract, single-writer HARD enforcement, override tripwire, threshold pairing, macro-test
coverage) and engine-liveness covered by the heartbeat → monitor-bridge → Kuma watchdog. Two
candidate behavioral fixes turned out to be moot or low-ROI on inspection:

- **Adaptive-Lighting self-on at startup is already fixed** — `automation.bedroom_al_startup_suppress`
  (commit `af88617d`). No work needed; a stale memory said otherwise and has been corrected.
- **Stale-override (`sleep_mode`/`manual_off`) startup reconcile is DEFERRED, not built** — see
  Non-Goals. The fan side already has `bedroom_fan_startup_reconcile`.

What remains worth doing are two deterministic, lightweight checks that close real gaps in the
validation/verification layer — one pre-deploy (static), one post-deploy (live).

## Governing principle (unchanged from the predecessor spec)

> Deterministic validation = testing properties (a single correct answer), with clearly defined,
> well-managed state changes. No flaky/fragile testing — if it cannot be made reliable, do not rely
> on it. Fill gaps that can be closed reliably and lightweight; do not chase every bug with a heavy
> test suite. Enforcement should not live in CLAUDE.md alone. No heavy CI added to GitHub — local
> gates where possible.

Both components below are purely additive, deterministic (exit-code gated), and validated against
ground truth (the committed config for the static check; the live running HA — "the authoritative
oracle" — for the live check).

## Goals

- **#3 — Make the single-writer invariant symmetric.** Catch a *stale* `sanctioned_writers.yml`
  entry (a `module`/`exemptions` writer that no longer writes the actuator), which today silently
  widens the allowed-writer set.
- **#2 — Assert post-deploy that every git-defined automation actually loaded** and is not in an
  errored (`unavailable`) state — the failure mode that, unlike a stale override, does *not*
  self-heal and is easy to miss.

## Non-Goals

- **No `sleep_mode`/`manual_off` startup reconcile.** DEFERRED, by the operator's call. Rationale:
  the problem self-heals daily (the 09:00/alarm `bedroom_morning_reset` clears all three override
  booleans), it is non-deterministic (only bites when the ~15-min RestoreState snapshot caught a
  stale value before the documented unclean shutdown), it has never been observed in practice
  (precautionary only), and a fix would have to *write* guarded coordination state with a real
  "wipe a legitimate night sleep" failure mode — i.e. it could create the bug class it aims to
  prevent. If it is ever observed, `bedroom_al_startup_suppress` is the proven precedent (decide at
  startup using presence + wake-window availability) and a concrete observed case is a better design
  input than a hypothesis. Tracked in memory `ha-stale-override-restore-on-deploy`.
- **No `.storage` cruft removal in this spec.** The live instance currently has 3 `unavailable`
  automations that are NOT in git (`bedroom_air_quality_alert` and `bedroom_battery_low_alert` —
  superseded by the unified `bedroom_threshold_alert` engine — and `test_critical_alert_dnd_setup_remove_after`,
  a leftover test). They are `.storage`/UI entries a deploy cannot remove; deleting them is a
  separate, write-requiring one-off (HA UI or WS config API). #2 is deliberately **file-driven** so
  this cruft cannot make its gate perpetually red — once cleaned, nothing about #2 changes.
- **No Docker `check_config` gate.** Cut in the predecessor spec (exits 0 on errors in some HA
  versions; output parsing is reword-fragile; blind to the `.storage`/HACS half). #2 is the
  principle-aligned realization of the same intent: check load success against the live oracle, not
  a cold validator. HA schema errors remain caught by the live deploy.

## Component A — #3: Symmetric sanctioned-writers check (pre-deploy, static)

**Where:** `scripts/ha_state_model.py`, `single_writer_errors` (currently `:555`). Mirror the
existing `override_writer_errors` (`:463`), which already checks both directions.

**Today** the check is one-directional — for each sanctioned actuator it flags only
`derived_writers − (module ∪ exemptions)` (an unsanctioned writer). A stale entry in the *other*
direction (a listed writer that no longer writes the actuator, e.g. a renamed/deleted script, or a
writer removed from the config but left in the YAML) is not flagged, so it silently widens the
allowed set: a later automation that happens to match a dead exemption name would slip the gate.

**Change:** add the reverse check.

```python
def single_writer_errors(writes: dict, sanctioned: dict) -> list[str]:
    """HARD + symmetric: the derived writer set of each sanctioned actuator must equal
    module ∪ exemptions. An unsanctioned writer fails; a sanctioned entry that no longer
    writes the actuator fails (stale entry widens the allowed set — remove it)."""
    errs = []
    for actuator, spec in sorted(sanctioned.items()):
        allowed = set(spec.get("module", [])) | set(spec.get("exemptions", []))
        got = set(writes.get(actuator, []))
        for writer in sorted(got - allowed):
            errs.append(f"{actuator}: unsanctioned writer {writer} — route it through the mediator "
                        f"(script.bedroom_lights_set / bedroom_fan_set) or declare it in "
                        f"state/sanctioned_writers.yml")
        for stale in sorted(allowed - got):
            errs.append(f"{actuator}: sanctioned writer {stale} no longer writes it — remove it "
                        f"from state/sanctioned_writers.yml")
    return errs
```

**Wiring:** none new — `check_errors` already calls `single_writer_errors`, which already runs in
the `validate-ha-config` prek hook (local + CI).

**Current-config safety:** verified that every `module`/`exemptions` entry in the current
`sanctioned_writers.yml` is a live writer in `derived_state.yml` (both actuators), so this is a pure
future-tightening with **zero current breakage** — the real role's `check` stays green.

**Tests** (`scripts/test_ha_state_model.py`): keep the existing unsanctioned-writer cases; add a
stale-entry case (a sanctioned actuator whose `allowed` set contains a name absent from `writes`
→ exactly one "no longer writes it" error) and an all-clean case (derived == allowed → no errors).

## Component B — #2: Post-deploy automation-load assertion (post-deploy, live)

**Where:** `scripts/probe.py` — a new read-only `ha verify-automations` subcommand (allow-listed,
exit 0 = all good / 1 = errors), plus a pure helper for the comparison logic.

**Robust matching key (confirmed against the live instance):** every live automation entity carries
`attributes.id` equal to its configured `id:`. Matching git `id:` ↔ live `attributes.id` sidesteps
the alias-slug≠id trap entirely (no slug derivation). The live count (32) does not equal the file
count (29) because of `.storage` cruft, so a count-equality check is wrong — the check is per-id and
**file-driven**.

**Pure core (unit-testable):**

```python
def automation_load_errors(expected_ids: set[str], live_automations: list[dict]) -> list[str]:
    """expected_ids = the `id:` of every automation in files/automations.yaml.
    live_automations = the automation.* entries from /api/states.
    A defined id with no live automation carrying that attributes.id = not loaded (dropped).
    A defined id whose live automation is `unavailable` = loaded but errored. A disabled
    automation (`state == 'off'`) is NOT an error. Cruft (live ids not in the file) is ignored."""
    by_id = {a.get("attributes", {}).get("id"): a for a in live_automations}
    errs = []
    for aid in sorted(expected_ids):
        live = by_id.get(aid)
        if live is None:
            errs.append(f"automation {aid} is defined in automations.yaml but did not load")
        elif live.get("state") == "unavailable":
            errs.append(f"automation {aid} loaded but is unavailable (config error at load)")
    return errs
```

**Expected-id extraction:** regex `^- id:\s*(\S+)` over
`ansible/roles/containers/home-assistant/files/automations.yaml` (resolved relative to the repo
root from `probe.py`'s location). Dependency-light and robust to the HA Jinja inside the YAML
(no full parse needed); the top-level automation ids are simple slugs.

**Live fetch:** the existing authenticated `GET /api/states` path probe.py already uses for `ha`
commands; filter to `entity_id` starting `automation.`.

**Deploy integration:** add one line to `ha-deploy` SKILL.md step 5 — run
`uv run python scripts/probe.py ha verify-automations` as a post-deploy gate, alongside the existing
health + `error_log` checks. Exit non-zero stops the "deploy verified" claim.

**Tests** (`scripts/test_probe.py`): the pure function — defined-but-missing → error;
defined-but-`unavailable` → error; defined-and-`on`/`off` → no error; cruft (live id not expected)
→ ignored — plus an id-extraction test over a small fixture (mirrors the `format_trace` test style).

## Cohesion & boundaries

- **A (static, pre-deploy)** keeps the *writer model* honest before the container is recreated.
- **B (live, post-deploy)** confirms the *automations actually loaded* after recreate.
- Each is a self-contained unit with a pure, tested core and a thin I/O shell (the prek hook for A;
  the probe subcommand + ha-deploy step for B). Neither changes runtime HA behavior.

## Testing strategy

All logic is pure functions with truth-table/case tests (no live dependency in the test suite). A
runs in the existing `validate-ha-config` prek hook (local + CI); B runs locally as a post-deploy
gate (never in GitHub CI — it needs the live HA + the SOPS token, consistent with the no-heavy-CI
constraint). Both gate on exit code — deterministic, no flakiness.
