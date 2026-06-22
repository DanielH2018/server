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
The state model already resolves **structural** entity references (`entity_id:` fields, write
targets) against `config refs ∪ external_entities.yml`, gated to `_MANAGED_DOMAINS`
(`ha_state_model.py:297`). Audit and close gaps in the **structural** coverage only (trigger
entities, `target.entity_id`). **Explicitly out of scope (named non-goal):** extracting entity ids
from inside `{{ }}` template bodies (`states('sensor.…')`/`state_attr(...)`) — that is regex-fragile
and risks false-positives on dynamically-built ids, i.e. exactly the flakiness the operator forbids.
Statically un-resolvable templated ids remain skipped, as today. (Note: a typo'd `scene.<id>` is
*already* caught here — `scene` is in `_MANAGED_DOMAINS` and `config_entities` includes the
scene-name map ∪ runtime `created_scenes`; no new work needed for scene-name typos.)

### 1b. Service/action calls resolve (new)
- Extend `refresh` to also `GET /api/services` from live HA and snapshot the registered services as
  a **full flat `{domain.service}` set** into a tracked file (e.g. `state/external_services.yml`).
  The live snapshot is **complete** — it already includes every config-defined script (registered
  under the `script` domain) and every HACS/custom-integration service (`adaptive_lighting.apply`,
  `dreo.*`), so there is no custom-integration blindness.
- **The service resolver must check ALL domains unconditionally — it must NOT inherit the entity
  resolver's `_MANAGED_DOMAINS` gate.** This is the critical difference from 1a: the entity resolver
  skips un-enumerable domains (`notify`, `media_player`, … — see the exclusion comment at
  `ha_state_model.py:295`), so cloning it would let `notify.pixel_watch_3` slip through and defeat
  the headline goal. Because the service snapshot is *complete*, every `domain.service` can be
  checked — so it is. Build a parallel checker, do not reuse `resolution_errors`.
- Resolution universe: **the snapshot is the authority.** The only config-side term is a freshness
  escape-hatch — `{script.<name> for every config-defined script}` — which exists *solely* to avoid
  a false-fail on a brand-new script added but not yet `refresh`ed (the snapshot already contains
  every *previously-refreshed* script).
- Templated service names (a service string containing `{{`) are skipped (cannot resolve
  statically) — a named, documented limitation, consistent with the entity-id handling. The current
  corpus has **zero** templated service names and 36 literal ones, so today's false-positive risk is
  ~nil.
- This catches the `notify.pixel_watch_3` `service_not_found` class — the repo's actual bug shape.

### 1c. Mediator `reason` contract (new)
For every call site of the actuator mediators (`script.bedroom_lights_set`,
`script.bedroom_fan_set`), assert the `reason`:
- **is present** — a call with **no `data:` block at all** (`- service: script.bedroom_lights_set`
  with nothing else) is treated as *reason absent → fail*, not skipped. This is the most likely
  authoring slip and is trivially catchable.
- **is a string in the mediator's valid vocabulary** — `lights: {presence, natural, wake, off}`,
  `fan: {auto, boost, off}`. The vocabulary is a **small declared constant beside the checker**
  (`MEDIATOR_REASONS = {"script.bedroom_lights_set": {...}, "script.bedroom_fan_set": {...}}`), with
  a comment pointing at the macro/`choose:`. **Do NOT derive it by regex** over `light_decision`'s
  Jinja or `bedroom_fan_set`'s `choose:` — that is two brittle extraction paths (the lights vocab
  is a Jinja literal, the fan has no macro at all), exactly the fragile coupling to avoid. A drifted
  constant fails *safe* (a newly-added valid reason false-fails until the constant is updated, which
  surfaces loudly) rather than false-passing.
- The single assertion `isinstance(reason, str) and reason in VOCAB` subsumes the YAML-bool trap:
  the config is loaded through `HAConfigLoader(yaml.SafeLoader)` (YAML 1.1), so an unquoted
  `reason: off`/`on` is already a Python `bool` by the time the checker sees it → `isinstance(...,
  str)` is `False` → fail; `reason: "off"` stays a string and passes.

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
config passes; a typo'd service in an **unmanaged domain** like `notify.pixel_watch_3` fails —
proving the no-domain-gate behavior; a config-defined `script.*` resolves; a templated name is
skipped), and the mediator-reason checker (valid reason passes; out-of-vocabulary fails;
YAML-coerced-bool `off` fails; quoted `"off"` passes; a call with **no `data:` block** fails).

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

**Implementation note:** the macro must `| bool`-coerce `sleep_mode` and `in_window` (macro args
arrive as *strings* from rendered macro output — the same pattern as `auto_light_allowed`'s
`in_window | bool` at `lighting.jinja:40`), or any non-empty string is always truthy and the gate
breaks.

**Tests:** truth-table test for `natural_exception` via `jinja_harness`, covering the boundary —
`hour=4` (in), `hour=5` (out, the strict `< 5`), the early-alarm case (`sleep_mode=False, hour=4,
in_window=True` → must be `wake`, not `nightlight`), and both string and bool forms of the bool
args.

---

## Component 3 — Macro-test guard

A lightweight CI check (rides the existing `validate-ha-config` hook, or a small standalone checked
by pytest) asserting **every macro defined in `custom_templates/*.jinja` is referenced by at least
one test** under `tests/`. Grep/parse-based — no AST, no complexity threshold, no exemption file.

This is the deterministic replacement for the cut complexity guard: it enforces the *testability*
property (a single correct answer — "is this macro referenced by a test: yes/no") rather than a
taste judgment, so you cannot add an untested decision macro. It honors "enforcement not in
CLAUDE.md alone."

**"Referenced by a test" is defined precisely** as the macro name appearing as the macro-name
argument to a `render_macro(<FILE>, "<name>", …)` call (the one true invocation path —
`jinja_harness.py:82`), **not** a bare substring match (which a comment, docstring, or an unrelated
assertion message could spoof). This keeps the guard's "single correct answer" property intact.

**Tests:** the guard's own check (a macro invoked via `render_macro(...)` passes; an unreferenced
macro fails; a macro whose name appears only in a comment fails), fixture-based.

---

## Component 4 — WS trace diagnosis in `probe.py`

A read-only `probe.py ha trace <automation>` (alias `ha why <automation>`) that pulls HA's own
execution trace and prints a per-condition timeline (trigger → each condition pass/fail → chosen
`choose:` branch → actions → error). Resolves the automation id via the existing `match_automation`
alias-slug logic.

### Mechanism & dependency
HA traces are **WebSocket-only** — verified on live 2026.6.3 that both
`GET /api/config/automation/trace/<id>` and `/api/trace/automation` return 404. So WS is mandatory;
there is no REST shortcut.

**This is genuinely new plumbing, not an extension of existing code — the spec must budget for it.**
`probe.py`'s entire HA surface is `curl` *subprocess* calls (`ha_curl_argv:93`, `ha_get:358`) — there
is no Python HTTP-client object to extend, and `requests` is not even used (it's a transitive dep of
`community.docker`). `requests` cannot do a WS upgrade, and stdlib `http.client`/`urllib` do not
implement WS framing. So "no async `websockets` dep" means a **hand-rolled, synchronous stdlib WS
client** (~40–60 lines: `socket` + the `Sec-WebSocket-Key` handshake + a single RFC-6455 masked text
frame + read-until-response + close, via `base64`/`hashlib`). This is doable and genuinely sync, but
it is NOT "minimal" and NOT the existing curl style — the plan allocates a dedicated WS-client
function **with its own fixture-backed unit test of the frame encoder/decoder**, kept separate from
the trace parser.

Auth flow: `connect → recv auth_required → send {type:auth, access_token} → recv auth_ok →
send {id, type:trace/list|trace/get}`. The `claude_ha_token` is the same bearer.

The subcommand must **only ever send `trace/list`/`trace/get`** (never a free-form WS command from an
argument), so it stays provably read-only. It remains allow-listed because the auto-approve hook
keys on the `probe.py` command line — note the hook cannot see the raw socket traffic, which is why
the only-trace-reads constraint is load-bearing, not cosmetic.

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
   **Intra-component ordering constraint:** the first commit must land the `refresh` extension AND
   the committed initial `state/external_services.yml` together with the 1b check, or CI fails with
   no snapshot to resolve against.
2. **Component 2** — `natural_exception` macro + test + convention doc.
3. **Component 3** — macro-test guard.
4. **Component 4** — WS trace subcommand + `ha-verify-state` wiring.

Components 2, 3, 4 are fully independent; Component 1 has the internal ordering noted above. The
HA-role `CLAUDE.md` "Testing" section is updated per phase.

## What is explicitly NOT changing

The five existing layers stay as-is: macro unit tests, the structural validator, single-writer +
override-writer enforcement, the derived state model, and the deploy/verify skills. The plan is
purely additive to the validation surface (Components 1–3) plus one diagnostic tool (Component 4).

## Risks & open questions

- **Service-name extraction robustness:** `service:`/`action:` appear in several shapes (string,
  `{{ templated }}`, inside `choose:`/`if`/`repeat`/`parallel`). Good news, verified: the existing
  `iter_service_calls`/`call_service` (`ha_state_model.py:56`,`:32`) already does this universal
  recursive action-tree walk and returns `None` for non-`domain.service` strings — so the extractor
  is reused, not rebuilt. Templated names (string contains `{{`) are skipped. Re-verify coverage if
  the corpus gains a templated service name (none today).
- **Mediator vocabulary source of truth:** a **declared constant** beside the checker is the
  primary (not fallback) mechanism — deriving by regex from `light_decision`'s Jinja and
  `bedroom_fan_set`'s `choose:` is two brittle paths and is rejected. A drifted constant fails safe
  (false-fail, surfaces loudly), never false-passes.
- **Snapshot drift:** the service snapshot shares the entity snapshot's failure mode — a stale
  snapshot can mask a real typo (false pass) or flag a just-added service (false fail until
  `refresh`). Accepted, identical to the existing entity check; the freshness gate surfaces drift.
- **WS message shapes:** HA's `trace/list`/`trace/get` payloads are stable but version-sensitive;
  pin the parser against the running `2026.6.x` shapes and keep the parser tolerant of unknown keys.
