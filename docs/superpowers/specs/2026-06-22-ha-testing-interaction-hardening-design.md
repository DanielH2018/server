# HA Testing & Interaction Hardening — Design

- **Date:** 2026-06-22
- **Status:** Approved (brainstorming) — pending spec review
- **Related:** [`2026-06-19-ha-jinja-unit-testing-design.md`](2026-06-19-ha-jinja-unit-testing-design.md),
  [`2026-06-20-ha-config-validation-design.md`](2026-06-20-ha-config-validation-design.md),
  [`2026-06-21-ha-state-model-phase1-representation-design.md`](2026-06-21-ha-state-model-phase1-representation-design.md),
  [`2026-06-21-ha-state-model-phase2-mediator-design.md`](2026-06-21-ha-state-model-phase2-mediator-design.md)

## Context & Problem

Home Assistant testing in this repo is **barbell-shaped**: very fast pure-function tests at one
end (the `custom_templates/*.jinja` macro unit tests), and slow live-deploy verification at the
other (`ha-deploy` → `probe.py ha`), with almost nothing in the middle. Three classes of error
therefore cost a full ~120 s container recreate to discover, or are never tested at all:

1. **Schema / integration-option errors** — the structural `validate-ha-config` hook
   (`scripts/validate_ha_config.py`) deliberately does *not* do HA schema validation
   (unknown keys, bad integration options, bad service refs). Its own docstring and `prek.toml`
   flag `hass --script check_config` as the out-of-scope next rung. Today the live deploy is the
   first thing that catches these.
2. **Automation *decision* logic** — the gating `condition:`/`choose:` blocks that decide what an
   automation does. Only `light_decision` (the Phase-2 mediator gate) has been extracted into a
   pure, truth-table-tested macro; the rest of the compound gates live inline in YAML, untestable
   except by deploying.
3. **Runtime "why did/didn't it fire"** — there is no tooling to pull *why* an automation no-op'd
   (which condition blocked it). `probe.py ha` reads state but not execution traces.

The Phase-1/Phase-2 state-model work created the substrate to close the middle: the codebase now
thinks in terms of **cells → writers → decisions**, and `light_decision` proves the
decision-macro pattern works.

## Goals

- Catch HA **schema** errors before deploy, locally, with zero ongoing maintenance.
- Make automation **decision logic** unit-testable, and *enforce* that new compound gates are
  extracted into tested macros (mirroring the `sanctioned_writers.yml` enforcement style).
- Give `probe.py` / `ha-verify-state` a **canonical, reliable** way to answer "why did/didn't this
  automation fire" using HA's own trace API.

## Non-Goals

- **No heavy CI in GitHub.** The Docker-based schema check runs **locally only**. CI keeps running
  only the existing *fast* (sub-second, no-Docker) suite.
- **No big-bang rewrite** of working automations. The decision-macro pattern is enforced going
  forward + piloted on 2 high-value automations; the long tail is burned down opportunistically.
- **No full HA integration harness** (ephemeral HA driven via API to assert service calls). Out of
  scope; revisit only if Components 1–3 prove insufficient.
- **No new write path to HA.** Git/Ansible remains the write path; the trace tooling is read-only.

---

## Component 1 — `check_config` schema gate (local-only)

### What
A new `scripts/check_ha_config.py` that runs HA's own `hass --script check_config` against the
assembled config in a Docker HA image, and reports any schema/integration-option error before a
deploy recreates the container.

### How
1. **Assemble** the deployed `/config` layout into a tempdir by **reusing
   `validate_ha_config.assemble_config()`** — no second copy of the assembly logic.
2. **Dummy secrets:** scan the assembled config for `!secret <key>` tokens and emit a
   `secrets.yaml` mapping each referenced key to a typed placeholder (string by default; numeric
   for the few keys that require a number). Deriving from what is *actually referenced* means the
   dummy file cannot drift from `secrets.yaml.j2`.
3. **Version-matched image:** read the LSIO tag from
   `ansible/roles/containers/home-assistant/templates/docker-compose.yml.j2`
   (`lscr.io/linuxserver/homeassistant:<X.Y.Z>-lsNN`), strip the `-lsNN` suffix, and run:
   ```
   docker run --rm -v <tmp>:/config ghcr.io/home-assistant/home-assistant:<X.Y.Z> \
     python -m homeassistant --script check_config -c /config
   ```
   Renovate bumps the prod LSIO pin; the check image version tracks it automatically (single
   source of version truth).
4. **Allow-list filter:** parse the output and discard **only** the known
   `Integration 'adaptive_lighting' not found` / `dreo` not-found lines (these are HACS-installed,
   not in git). Any **other** error → non-zero exit. The allow-list is a small, named constant with
   a comment explaining why each entry is tolerated.

### Placement (local-only)
- **Hard pre-deploy gate in the `ha-deploy` skill:** run `check_ha_config.py` before
  `ansible-playbook`; block the deploy on a real error. A `--skip-check` escape exists for
  emergencies. Docker is already present on daniel-server, so the image is available locally; the
  first run pulls the official image once.
- **Standalone:** `uv run python scripts/check_ha_config.py` for ad-hoc runs.
- **NOT** a GitHub CI job and **NOT** a per-commit prek hook (Docker pull/run is too slow next to
  the <1 s structural hook).

### Tests (fast, in existing CI)
- Unit-test the **output parser** (allow-list filtering) against fixture `check_config` outputs
  (a clean run, an AL/dreo-only run that must pass, a run with a real error that must fail).
- Unit-test the **dummy-secrets generator** (correct keys extracted, typed placeholders).
- The Docker invocation itself is local integration only — not unit-tested.

---

## Component 2 — decision-macro pattern + guard + pilot

### The contract (documented convention)
A *decision macro* is a pure function in `custom_templates/*.jinja`:
- **Inputs:** entity states / time as plain values (numbers, bools, strings). No `states()` /
  `now()` / `is_state()` inside — those stay in the YAML caller, which passes plain values in.
- **Output:** an **action token** — e.g. `natural|wake|off|noop`, a level int, an advice string.
- **Tested:** a truth-table test in `tests/test_*_macros.py` via the existing `jinja_harness`.

`light_decision` is the reference implementation; `fan_target_level`, `auto_light_allowed`,
`ventilation_advice`, and the wake macros already conform. The automation YAML becomes a thin
**trigger → compute decision via macro → dispatch on the token → service call** shell (ideally the
service call routes through the Phase-2 mediator script).

This convention is documented in the HA-role `CLAUDE.md` "Testing" section with a worked example.

### The guard (mirrors `sanctioned_writers.yml`)
A lint added to `scripts/ha_state_model.py` (it already parses the automation/script YAML):
- **Flags** any `condition:` / `choose:`-condition template whose logic **exceeds a complexity
  threshold** (≥3 boolean/comparison operators: `and`/`or`/`not`/`==`/`!=`/`<`/`>`/`<=`/`>=`)
  **and** is not a single macro call. This targets the "compound gate inline in YAML" smell, not
  simple one-liners like `{{ is_state('x','on') }}`.
- **Escape hatch:** a hand-maintained `state/inline_decision_exemptions.yml` (automation/script id
  → reason) lists accepted inline gates. New compound inline gating **fails** until refactored into
  a macro **or** exempted-with-reason.
- Runs inside the existing fast `validate-ha-config` prek hook + CI (pure-Python, no Docker).
- **Rollout:** the existing automations that trip the threshold and are *not* being piloted are
  seeded into `inline_decision_exemptions.yml` with a `# burn down opportunistically` note, so the
  guard goes green immediately and only *new* inline complexity is blocked.

### Pilot conversions (2 now)
1. **`bedroom_apply_natural` exception selector** — extract the `choose:` ladder's *selection*
   (nightlight vs wake vs default, given `sleep_mode` / `hour` / wake-window state) into
   `natural_exception(...) -> token`. The single most-edited, most-gotcha-laden piece. (The
   brightness math is already in macros; this extracts the *which-exception-wins* logic.)
2. **`script.bedroom_notify` routing** — extract the channel/importance/hold-vs-push decision
   (`pierce` + quiet state + away → route + channel + importance) into `notify_routing(...)`.
   High-traffic, pure logic, currently inline and untested.

### Tests (fast, in existing CI)
Truth-table tests for `natural_exception` and `notify_routing` via the `jinja_harness`, plus tests
for the guard's complexity detector (a known-simple condition passes, a known-compound one is
flagged, an exempted one passes).

---

## Component 3 — WS trace diagnosis in `probe.py`

### What
A new **read-only** subcommand `probe.py ha trace <automation>` (alias `ha why <automation>`) that
answers "why did / didn't this automation fire" at per-condition fidelity, using HA's own
WebSocket trace API — the exact mechanism the HA UI trace timeline uses.

### How
1. Resolve the automation id via the existing `match_automation` alias-slug logic (handles the
   `alias`-slug ≠ `id` trap).
2. Open a WebSocket to `/api/websocket`, complete the `auth` handshake with `ha_token()`.
3. Call `trace/list` for `{domain: automation, item_id: <id>}` → recent run ids; then `trace/get`
   for the latest run → the full trace dict.
4. Format a human timeline: trigger → each condition with **pass/fail** → the chosen `choose:`
   branch → the actions called → any error. Mirrors the HA UI trace.

### Dependency & safety
- Adds a WS client (`websockets`) to the uv dev deps — consistent with the "managed, pinned in
  `uv.lock`" preference. The subcommand only ever sends `trace/*` reads, so it remains read-only
  and stays **allow-listed** (the auto-approve hook keys on the `probe.py` command line, not the
  network).

### Wiring
Update the `ha-verify-state` skill so the "why didn't it fire" diagnosis routes through
`ha trace` / `ha why`.

### Tests (fast, in existing CI)
Unit-test the trace **parser** against a fixture trace JSON (a run blocked at a condition; a run
that completed; a run with an error). The live WS round-trip is local integration only — exercised
against live HA via the subcommand, not unit-tested.

---

## Testing strategy (summary)

| Layer | What it covers | Where it runs |
|---|---|---|
| Macro truth-table tests (incl. new `natural_exception`, `notify_routing`) | Decision logic | CI + prek (fast) |
| Guard complexity-detector tests | The lint itself | CI + prek (fast) |
| `check_config` parser + dummy-secrets tests | Component 1 logic | CI + prek (fast) |
| Trace parser tests | Component 3 logic | CI + prek (fast) |
| `check_config` Docker run | Real HA schema validation | **Local only** (ha-deploy gate + standalone) |
| WS trace round-trip | Real trace fetch | **Local only** (against live HA) |
| `ha-deploy` → `probe.py` | End-to-end live verification | Local (unchanged) |

## Rollout / phasing

One spec, three **independently shippable** components. Suggested plan phasing:

1. **Component 1** — `check_config` script + tests + `ha-deploy` gate.
2. **Component 3** — WS trace subcommand + parser tests + `ha-verify-state` wiring.
3. **Component 2** — decision-macro convention + guard + exemptions seed + 2 pilot conversions
   (largest; touches working automations last).

Each phase is independently mergeable and testable. The HA-role `CLAUDE.md` "Testing" section is
updated per phase.

## Risks & open questions

- **`check_config` exit-code behavior:** some HA versions print errors but exit 0 — the parser must
  decide pass/fail by scanning output markers, not solely the exit code. Verify against the pinned
  version during implementation.
- **Dummy-secrets typing:** a `!secret` key consumed where a number/list is required needs a typed
  placeholder, not a bare string, or `check_config` will false-fail. Enumerate the referenced keys
  during implementation and type the few that need it.
- **Guard threshold tuning:** ≥3 operators is a starting point; tune against the real automation
  corpus so the initial exemption seed is small and the signal stays high.
- **`websockets` version:** pin in `uv.lock`; HA's WS auth/`trace` API is stable but confirm the
  message shapes against the pinned HA version.
