# HA `system_log_event` ⇒ `fire_event` Deterministic Check — Design

**Date:** 2026-06-22
**Status:** approved (pre-plan)
**Origin:** During the `ha_runtime_error_alert` live-fire, the automation was found silently DEAD —
`default_config` enables `system_log` *without* `fire_event`, so `system_log_event` never fires and
the trigger never matched. Fixed by adding `system_log: fire_event: true`, but nothing prevents the
class from recurring. This converts that hard-won lesson into a pre-deploy deterministic gate
(operator preference: enforce rules deterministically, do not rely on CLAUDE.md notes).

## Governing principle (unchanged)

> Deterministic validation = testing properties with one correct answer. No flaky/fragile checks —
> structured-data checks only, no Jinja/string parsing. Enforcement belongs in a gate, not a note.
> No new heavy CI (this rides the existing `validate-ha-config` prek hook, local + CI).

## Goal

Fail validation when an automation triggers on `system_log_event` but `configuration.yaml` does not
enable `system_log: fire_event: true` — i.e. the trigger could never match (silently dead).

## Non-Goals

- **Not a general "event ⇒ required config flag" framework.** Only the one concrete pair
  (`system_log_event` ⇒ `system_log.fire_event`) is enforced. The function is structured so a future
  pair is a one-line addition, but no framework is built for a single case (YAGNI).
- **No live/runtime checks, no Docker.** Pure structured-data check over the already-loaded config.
- **Component B (the `| bool`-macro-output AST lint) is out of scope** — deferred by the operator.

## Component — `system_log_fire_event_errors(config) -> list[str]`

**Home:** `scripts/ha_state_model.py`, a new pure function wired into `check_errors` alongside
`single_writer_errors` / `mediator_reason_errors` / `threshold_pairing_errors`. It consumes the same
`config` dict those use (`load_role()` assembles `configuration.yaml` with `automation:` inlined via
`!include`, so both `config['automation']` and `config['system_log']` are present). Runs in the
`validate-ha-config` prek hook (local + CI). No new file, no new mechanism.

**Logic:**
1. Collect automations that trigger on `system_log_event`. For each automation in
   `config.get("automation") or []`, normalize its trigger block (`auto.get("trigger")` may be a
   single dict or a list; a `dict` is wrapped to a one-item list). For each trigger dict, read
   `event_type` (may be a `str` or a `list`); if `system_log_event` is among them, record the
   automation's `id` (fallback `alias`, then `<unknown>`).
2. If none trigger on it → return `[]` (no dependency to enforce).
3. Otherwise require `(config.get("system_log") or {}).get("fire_event") is True`. If it is `True`
   → `[]`. Otherwise → one error string naming the sorted, de-duplicated offending automation id(s):

   > `automation(s) ['ha_runtime_error_alert'] trigger on system_log_event but configuration.yaml
   > does not set 'system_log: fire_event: true' — system_log does not fire that event by default,
   > so the trigger never matches (silently dead). Add it under a top-level 'system_log:' key.`

   (`fire_event: true` parses to the Python bool `True` via `HAConfigLoader`/SafeLoader; the check
   accepts only `True` — the canonical YAML form. `False`/missing/absent all fail.)

**Wiring:** one line in `check_errors` — `errs += system_log_fire_event_errors(config)` — using the
`config` it already loads. No signature change to `check_errors`.

**Current-config safety:** the shipped config has `ha_runtime_error_alert` (a `system_log_event`
trigger) *and* `system_log: fire_event: true`, so the check returns `[]` → `ha_state_model.py check`
stays green. Pure future-tightening; zero current breakage. (Run against the pre-fix state, it would
have returned the error — a red gate before deploy.)

## Testing

`scripts/test_ha_state_model.py` — truth table over `system_log_fire_event_errors(config)` with
minimal hand-built `config` dicts (no live dependency):

| automation trigger | `system_log.fire_event` | expect |
|---|---|---|
| `event_type: system_log_event` | `true` | `[]` |
| `event_type: system_log_event` | missing (`system_log` absent) | 1 error naming the automation |
| `event_type: system_log_event` | `false` | 1 error |
| no `system_log_event` trigger (other/none) | missing | `[]` |
| `event_type: [other, system_log_event]` (list form) | `true` | `[]` (list `event_type` handled) |

## Boundaries

One pure function + its test, plus a one-line wire into `check_errors`. No actuator writes, no new
files, no runtime dependency. The only non-trivial logic (trigger detection + the flag requirement)
is pure and unit-tested.
