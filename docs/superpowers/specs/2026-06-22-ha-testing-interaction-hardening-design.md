# HA Testing & Interaction Hardening — Design

- **Date:** 2026-06-22
- **Status:** Approved (brainstorming, revised post-review) — pending spec re-review
- **Related:** [`2026-06-19-ha-jinja-unit-testing-design.md`](2026-06-19-ha-jinja-unit-testing-design.md),
  [`2026-06-20-ha-config-validation-design.md`](2026-06-20-ha-config-validation-design.md),
  [`2026-06-21-ha-state-model-phase1-representation-design.md`](2026-06-21-ha-state-model-phase1-representation-design.md),
  [`2026-06-21-ha-state-model-phase2-mediator-design.md`](2026-06-21-ha-state-model-phase2-mediator-design.md)

## Context & Problem

Home Assistant testing in this repo is **barbell-shaped**: fast pure-function tests at one end
(the `custom_templates/*.jinja` macro unit tests), slow live-deploy verification at the other
(`ha-deploy` → `probe.py ha`). The existing *deterministic* validation covers two property classes:

- **Structure** — `validate_ha_config.py` (YAML syntax, duplicate keys, broken `!include`, inline
  Jinja *syntax*).
- **Write-ownership** — `ha_state_model.py` + `state/*.yml` (single-writer hard check via
  `sanctioned_writers.yml`, the override-writer tripwire, and **entity-reference resolution**
  against `external_entities.yml`).

Three failure classes are **not** validated deterministically today and surface only at runtime or
by observing wrong behavior:

1. **Service/action calls** — a typo'd `service: notify.pixel_watch_3` is unchecked (the documented
   `service_not_found` Repair class — the repo's actual historical bug shape).
2. **Mediator `reason` contract** — an unquoted `reason: off` (→ YAML `false` → silent no-op) or a
   typo'd reason is unchecked (documented CLAUDE.md trap).
3. **The most-edited decision logic** — `bedroom_apply_natural`'s nightlight-vs-wake selection: the
   mutual-exclusion boundary is protected only by a comment (`scripts.yaml:139-143`), not a test.

Plus two ergonomic gaps: nothing enforces that a new macro has a test, and there is no tooling to
answer "the automation ran but no-op'd — which condition blocked it."

The Phase-1/2 state-model work makes closing #1–#3 cheap and reliable: the codebase thinks in
**cells → writers → decisions**, the mediator gives a single state-change chokepoint to validate,
and the `refresh`-snapshot pattern already validates entity references against a live-HA snapshot.

### Design principle (from the operator)

> Deterministic validation = **testing properties** (a single correct answer), with **clearly
> defined, well-managed state changes**. **No flaky/fragile testing** — if it cannot be made
> reliable, do not rely on it. Fill gaps that can be closed **reliably and lightweight**; do not
> chase every bug with a heavy test suite. Enforcement should not live in CLAUDE.md alone.

This principle drove the revision: the work below is purely additive, deterministic, lightweight,
and validated against the **live, running HA** (the authoritative oracle — it has every
integration/entity/service loaded), so it has none of a cold `check_config`'s blindness.

## Goals

- Deterministically validate **reference integrity** (entities + services resolve) and **contract
  integrity** (mediator state-change calls are well-formed) — the two property classes that map to
  the repo's actual bugs.
- Make the most-edited decision logic **testable** (extract one macro with a real boundary bug
  surface) and **enforce** that macros carry tests.
- Give `probe.py` / `ha-verify-state` a reliable way to answer "why did this run no-op."

## Non-Goals

- **No Docker `check_config` gate.** Its harness is fragile (exits 0 on errors in some HA versions;
  output-string parsing against monthly reword) and it is blind to the `.storage`/HACS-configured
  half of HA. It cannot be made cleanly reliable without disproportionate effort, so per the
  operator's principle it is **cut**. HA *schema* errors remain caught by the live deploy. (Earlier
  draft proposed it; removed after review.)
- **No automated "decision complexity" guard.** "Is this condition too complex / should it be a
  macro" is a taste judgment — not a property with one correct answer — so any check for it is
  either flaky (a naive operator-count heuristic flags even the reference-pattern macro callers,
  e.g. `automations.yaml:1385`) or a high-maintenance AST linter. Cut. The decision-macro pattern is
  *documented* as guidance; the things that actually matter (references resolve, logic is tested)
  are enforced deterministically below.
- **No `bedroom_notify` macro pilot.** Its hard part is side-effecting control flow (the away-hold
  vs push orchestration with `persistent_notification.create`/`dismiss`/`stop`), which cannot be a
  pure value→token macro; the extractable part is four trivial ternaries. Low ROI. Cut.
- **No `websockets` async dependency.** The trace puller uses the existing sync/`requests`/stdlib
  style of `probe.py`.
- **No ephemeral-HA integration harness.** Too heavy; deferred indefinitely.
- **No new write path to HA.** Git/Ansible stays the write path; the trace tool is read-only.

---

## Component 1 — Reference & Contract Integrity gate (headline)

A single pure-Python gate, extending `scripts/ha_state_model.py` and its `refresh` snapshot, that
validates three deterministic properties against a **live-HA snapshot** and fails CI on violation.
Runs inside the existing fast `validate-ha-config` prek hook + CI (no Docker, <1 s).

### 1a. Entity references resolve (strengthen existing)
The state model already resolves entity references against `config refs ∪ external_entities.yml`.
Audit and close any coverage gaps (trigger entities, `target.entity_id`, `state_attr`/`states('…')`
reads). Statically un-resolvable templated entity ids remain skipped (documented limitation, as
today).

### 1b. Service/action calls resolve (new)
- Extend `refresh` to also `GET /api/services` from live HA and snapshot the registered service
  list into a tracked file (e.g. `state/external_services.yml`), exactly like `external_entities.yml`
  (services from HACS/custom integrations — `adaptive_lighting.apply`, `dreo.*` — are present in the
  live snapshot, so there is no custom-integration blindness).
- Validate every `service:`/`action:` call in `automations.yaml`/`scripts.yaml`/`scenes.yaml`
  against `config-defined services (every script.<name>) ∪ snapshot`.
- Templated service names (`service: "{{ … }}"`) are skipped (cannot resolve statically) — a named,
  documented limitation, consistent with the entity-id handling.
- This catches the `notify.pixel_watch_3` `service_not_found` class.

### 1c. Mediator `reason` contract (new)
For every call site of the actuator mediators (`script.bedroom_lights_set`,
`script.bedroom_fan_set`), assert the `reason`:
- **is present**,
- **is in the mediator's valid vocabulary** — `lights: {presence, natural, wake, off}`,
  `fan: {auto, boost, off}`. The vocabulary is derived from the mediator's own `choose:` branches
  where feasible (single source of truth), else a small declared constant kept next to the mediator,
- **is a string, not a YAML-coerced bool** — i.e. the loaded value must not be Python `True`/`False`
  (catches the unquoted `reason: off`/`on` → silent no-op trap).

This is the deterministic expression of "well-managed state changes": single-writer already
guarantees actuator writes flow *through* the mediator; 1c guarantees the calls *into* it are
well-formed.

### Snapshot freshness
The service snapshot (1b) is committed and refreshed via `ha_state_model.py refresh`, identical to
the existing entity snapshot — the gate is only as current as the last `refresh`; adding an
integration means re-running `refresh` and committing. A new committed snapshot that differs from a
re-derivation is freshness-gated like `derived_state.yml` today.

### Tests
Unit tests (pure-Python, fast, in CI) for: the service-call extractor + resolver (a known-good
config passes; a deliberately typo'd service fails; a config-defined `script.*` resolves; a
templated name is skipped), and the mediator-reason checker (valid reason passes; out-of-vocabulary
fails; YAML-coerced-bool `off` fails; quoted `"off"` passes).

---

## Component 2 — `natural_exception` macro + truth-table test

Extract the **selection** logic from `bedroom_apply_natural`'s `choose:` ladder (`scripts.yaml:135-178`)
— nightlight vs wake vs default, given `sleep_mode` / `hour` / wake-window state — into a pure
`natural_exception(sleep_mode, hour, in_window) -> 'nightlight'|'wake'|'default'` macro in
`custom_templates/lighting.jinja`. The YAML caller computes `in_window` (already an
`in_wake_window` macro call), `hour`, and `sleep_mode`, passes plain values in, and `choose:`-es on
the returned token. The brightness math already lives in macros; this extracts only the
which-exception-wins logic.

**Value:** the nightlight↔wake mutual exclusion at the boundary — including the documented
early-alarm trap (`(sleep_mode or hour < 5) and not in_window`, `scripts.yaml:139-143`) — becomes a
**truth-table test** instead of a load-bearing comment.

The decision-macro contract (pure values in → action token out; no `states()`/`now()` inside; a
truth-table test) is documented in the HA-role `CLAUDE.md` "Testing" section, with `light_decision`
and `natural_exception` as the reference examples. This is *guidance*; enforcement of what matters
comes from Components 1 and 3, not from the doc.

**Tests:** truth-table test for `natural_exception` via `jinja_harness`, covering the boundary
(early alarm with `hour < 5` inside the window must select `wake`, not `nightlight`).

---

## Component 3 — Macro-test guard

A lightweight CI check (rides the existing `validate-ha-config` hook, or a small standalone checked
by pytest) asserting **every macro defined in `custom_templates/*.jinja` is referenced by at least
one test** under `tests/`. Grep/parse-based — no AST, no complexity threshold, no exemption file.

This is the deterministic replacement for the cut complexity guard: it enforces the *testability*
property (a single correct answer — "is this macro referenced by a test: yes/no") rather than a
taste judgment, so you cannot add an untested decision macro. It honors "enforcement not in
CLAUDE.md alone."

**Tests:** the guard's own check (a macro with a test passes; an unreferenced macro fails),
fixture-based.

---

## Component 4 — WS trace diagnosis in `probe.py`

A read-only `probe.py ha trace <automation>` (alias `ha why <automation>`) that pulls HA's own
execution trace and prints a per-condition timeline (trigger → each condition pass/fail → chosen
`choose:` branch → actions → error). Resolves the automation id via the existing `match_automation`
alias-slug logic.

### Mechanism & dependency
HA traces are a **WebSocket** API (`trace/list` + `trace/get`). Implement a **minimal synchronous**
WS handshake (`auth` → command) using the stdlib / existing `requests`-era style of `probe.py` —
**no async `websockets` dependency** (a sync diagnostic subprocess script must not drag in an event
loop). The subcommand only ever sends `trace/*` reads, so it stays read-only and allow-listed (the
auto-approve hook keys on the `probe.py` command line).

### Honest scope (this is diagnosis, not validation)
- Traces are **in-memory**, capped (`stored_traces` default 5), and **wiped on every HA restart /
  deploy** — so immediately after a deploy the buffer is empty.
- An automation that **never triggered** produces **no trace** (`trace/get` only captures runs). A
  run blocked by a *condition* IS traced (the useful case); a trigger that never matched is not.
- Therefore `ha why` answers "it ran but no-op'd — which condition blocked it," **not** "nothing
  happened." For the no-run case, pair with `probe.py ha get logbook/<entity>` + `last_triggered`.
  The subcommand's help text states this boundary explicitly.

### Wiring & tests
Wire `ha-verify-state` to route "why didn't it fire" through `ha trace`/`ha why`. Unit-test the
trace **parser** against fixture trace JSON (a condition-blocked run; a completed run; an errored
run); the live WS round-trip is local integration only.

---

## Testing strategy (summary)

| Layer | Covers | Where it runs |
|---|---|---|
| Reference & contract integrity tests (extractor/resolver, mediator-reason) | Component 1 logic | CI + prek (fast, no Docker) |
| `natural_exception` truth-table test | Component 2 boundary logic | CI + prek (fast) |
| Macro-test-guard self-test | Component 3 | CI + prek (fast) |
| Trace parser test | Component 4 logic | CI + prek (fast) |
| `refresh` live snapshot | Keeps the gate's oracle current | Local (operator-run, committed) |
| WS trace round-trip | Real trace fetch | Local only (against live HA) |
| `ha-deploy` → `probe.py` | End-to-end live verification | Local (unchanged) |

## Rollout / phasing

One spec, four **independently shippable** components, one focused commit each (minimizes the
prek-auto-stash concurrent-session hazard). Suggested order by value/independence:

1. **Component 1** — reference & contract integrity gate (+ `refresh` service snapshot). Headline.
2. **Component 2** — `natural_exception` macro + test + convention doc.
3. **Component 3** — macro-test guard.
4. **Component 4** — WS trace subcommand + `ha-verify-state` wiring.

The HA-role `CLAUDE.md` "Testing" section is updated per phase.

## What is explicitly NOT changing

The five existing layers stay as-is: macro unit tests, the structural validator, single-writer +
override-writer enforcement, the derived state model, and the deploy/verify skills. The plan is
purely additive to the validation surface (Components 1–3) plus one diagnostic tool (Component 4).

## Risks & open questions

- **Service-name extraction robustness:** `service:`/`action:` appear in several shapes (string,
  `{{ templated }}`, inside `choose:`/`if`/`repeat`, `parallel`). The extractor must walk the action
  tree the way `ha_state_model.py` already walks for writers; templated names are skipped. Verify
  coverage against the real corpus during implementation.
- **Mediator vocabulary source of truth:** prefer deriving the valid `reason` set from the
  mediator's `choose:` branches so it cannot drift; fall back to a declared constant beside the
  mediator only if derivation is brittle.
- **Snapshot drift:** the service snapshot shares the entity snapshot's failure mode — a stale
  snapshot can mask a real typo (false pass) or flag a just-added service (false fail until
  `refresh`). Accepted, identical to the existing entity check; the freshness gate surfaces drift.
- **WS message shapes:** HA's `trace/list`/`trace/get` payloads are stable but version-sensitive;
  pin the parser against the running `2026.6.x` shapes and keep the parser tolerant of unknown keys.
