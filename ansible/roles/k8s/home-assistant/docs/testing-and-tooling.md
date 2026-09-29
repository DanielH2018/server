# Testing, tooling and the validation traps

The role's `CLAUDE.md` keeps the rules an edit must not break; this page is the working-out
behind them — what each test harness covers, what the validator does and does not check, the
Claude-side tooling for this role, and the trap that lets a dead entity validate clean. Split
out of the role doc so the inject hook stops truncating it (#2989).

## What is unit-tested, and the convention that keeps it that way

**Bedroom Jinja math is unit-tested** (`tests/`, run via `uv run pytest`, the prek `pytest`
hook and CI — wired in `pyproject.toml` `testpaths`). The bug-prone computed logic lives in
pure `custom_templates/{fan,lighting}.jinja` macros; entity and time reads (`states()`,
`now()`) stay in the YAML callers, and macros take plain numbers. The tested set:
`fan_target_level` (curve, ±0.7-level hysteresis, night/sleep caps — used by
`bedroom_apply_fan`), `in_wake_window` and `wake_brightness` (the morning ramp, used by
`bedroom_apply_natural` and `bedroom_apply_wake`; `wake_transition` was removed, transition is
a fixed 60s per ramp step), and `auto_light_allowed` (the lux gate, used by `templates.yaml`'s
`bedroom_auto_light_allowed`).

**Decision-macro convention:** an automation or script's gating *selection* logic belongs in a
pure `custom_templates/*.jinja` macro — plain values in, no `states()`/`now()`/`is_state()`
inside, an action token out — with a truth-table test, exactly like `light_decision` and
`natural_exception` (the `bedroom_apply_natural` nightlight↔wake selection). The YAML caller
reads entities and `choose:`-es on the returned token. This is guidance; what is enforced is
that the references resolve and that every macro has a test.

**Adding a tunable formula:** put the math in a macro (numbers in, numbers or a bool out),
import it from the YAML caller, and add a test rather than inlining new math in the
automations. A new `.jinja` ships once `home_assistant_template_files` in `defaults/main.yml`
names it, and the validator fails the prek hook until it does — so a macro cannot validate
clean and never reach the pod.

**The harness mirrors HA's filter overrides.** `tests/jinja_harness.py` renders macros in a
bare Jinja2 environment carrying the handful of overrides the macros use, most importantly
HA's `round`, which is **banker's** rounding (`forgiving_round`, round-half-to-even, int at
precision 0) rather than Jinja's stock half-away-from-zero float. The fan level math lands on
`.5` midpoints by design, so this is load-bearing, and `test_ha_round_semantics.py` pins it.
`test_fan_macros.py` carries an old-inline-vs-macro equivalence grid (8.8k points) as a
permanent behaviour-preservation guard against the curve being changed in only one place.

## What the pre-deploy validator checks

The `validate-ha-config` prek hook (`scripts/home_assistant/validate_ha_config.py`) runs
locally and in CI on any change under the role's `templates/` and `files/`. It is pure Python,
no Docker: it assembles the deployed `/config` layout and checks YAML syntax, **duplicate
keys**, broken `!include` targets, and the **syntax** of every inline `{{ }}`/`{% %}` template
and each `custom_templates/*.jinja`.

It does NOT do HA *schema* validation (unknown keys, bad integration options) or
entity-existence checks. That needs `hass --script check_config` in a Docker HA image, which
is out of scope; the deploy still catches schema errors live.

## The scenario test harness

Since 2026-06-23, the bedroom automations can be exercised ON DEMAND instead of waiting for
the real trigger (night, leaving home). The dashboard "🧪 Test scenarios" card plus
`input_select.bedroom_test_scenario` (off/bedtime/wake/nightlight/away/arrive/reset) and
`input_select.bedroom_test_speed` (fast/real) drive `script.bedroom_run_scenario`. Fast is the
default because an `input_select`'s first option is its creation default, which an
`input_boolean` cannot do: it has no `initial:` field, only restores.

It DRIVES the real scripts and automations rather than reimplementing them:
bedtime→`bedroom_bedtime` (which gained an optional `fade`, default 1800; fast passes 30),
wake→`bedroom_preview_wake` (a test-only compressed frame-sweep reusing the tested
`wake_brightness` macro — it does NOT touch the production
`bedroom_wake_ramp`/`bedroom_apply_wake`), nightlight→`scene.bedroom_nightlight`,
away/arrive→`automation.trigger` (skip_condition), reset→`bedroom_clear_overrides` (a DRY
extraction shared with the morning reset).

**Away is response-only:** it tests the lights/fan-off and "Left on" notify; the away
notification-HOLD path needs a real `person.daniel != home`, set in Developer Tools → States,
because no service sets arbitrary entity state. The harness is inert until you press Run, and
the test-only direct light writers (`bedroom_preview_wake`, and `bedroom_run_scenario` via the
nightlight `scene.turn_on`) are declared in `state/sanctioned_writers.yml`. Phase 2 also
extracted the away/arrive selection into tested macros (`away_items_label`,
`arrive_relight_allowed`). It shipped in commit `d4b6b6e8e`.

## Claude tooling for this role

- **`home-assistant-engineer` agent** (`.claude/agents/`) — a read+write HA engineer that
  knows these conventions and traps; delegate HA authoring and debugging to it.
- **Skills** (`.claude/skills/`): `ha-edit-automation` (the authoring workflow —
  copy-not-template, math-in-a-tested-macro, validate→deploy→verify; its step 5 carries live
  state via the API and the recorder and alias-slug traps), and `z2m-device-setting` (persist
  a Zigbee device setting via `mosquitto_pub`).
- **`scripts/diagnostics/probe.py ha`** — read-only live HA state, allow-listed, authed with
  the SOPS `claude_ha_token`. `ha state <entity>`; `ha automation <id-or-alias>`, which
  resolves the alias-slug≠id trap; `ha get <api-path>` (`error_log`, for instance). Prefer it
  over recorder-DB reads. `ha why <id-or-alias>` (alias `ha trace`) pulls the live
  per-condition automation trace over the WS API and answers "it ran, but which condition
  blocked it" — not "it never fired", because traces are in-memory and wiped on restart. `ha
  verify-automations` is the post-deploy gate: exit 0 means every automation in
  `files/automations/*.yaml` loaded and is not unavailable, matching git id against live
  `attributes.id`, and it is file-driven so `.storage`/UI cruft is ignored.
- **Derived state model** (`state/STATE.md` and `state/derived_state.yml`, generated by
  `scripts/home_assistant/ha_state_model.py generate`) — the machine-derived map of cells and
  actuators and who writes them. The `validate-ha-config` hook regenerates and freshness-gates
  it, so never hand-edit it; a stale committed copy fails CI. The one hand-maintained file is
  `state/expected_override_writers.yml`, the three-boolean write tripwire: CI fails if an
  automation or script writes `bedroom_manual_off`, `bedroom_fan_manual` or
  `bedroom_sleep_mode` without being listed. The resolution check (config refs ∪
  `state/external_entities.yml`, snapshotted by `ha_state_model.py refresh`) catches a mistyped
  or renamed entity before it becomes a silent no-op. Live view: `probe.py ha-state` for
  current cell values and anomalies, `--inventory` for the full catalog. The role `CLAUDE.md`
  stays the home of the runtime and physical *why* the model cannot derive. Phases 1 and 2
  shipped in commits `fd343cc31` and `d87841da7`.

## Trap: the entity snapshot is blind to disappearances

`scripts/home_assistant/validate_ha_config.py` resolves every entity reference against config
refs ∪ `state/external_entities.yml`. That file is a snapshot, rewritten only by an explicit
`ha_state_model.py refresh` against live HA. So the guard catches a **typo** — a name that
never existed — and is structurally blind to a **disappearance** — a name that stopped
existing. Both read as "reference resolves".

Found 2026-08-16: `sensor.pixel_9_pro_do_not_disturb_sensor` and
`sensor.pixel_9_pro_sleep_duration` no longer existed on any device, yet stayed listed in the
snapshot. Validation passed clean while three features were inert. `bedroom_notify`'s `quiet`
collapsed, so routine alerts pushed at `default` instead of `low`; the short-night wake knee
became unreachable; the "you slept N h" note never sent.

The failure is silent by construction. `states()` on a missing entity renders `unknown`, which
sat inside `bedroom_notify`'s own `not in ['off','unknown','unavailable']` exclusion list, and
`| float(0)` latched a zero the curve treats as a normal night.

Run `uv run python scripts/diagnostics/probe.py ha verify-entities` (added in PR #218) — it
diffs the snapshot against live HA and exits non-zero on anything that vanished. Treat it as a
post-deploy gate alongside `ha verify-automations`. When a reference does turn out dead, fix
the config first and refresh second: `refresh` alone drops the ids from the snapshot and makes
the validator start failing on the still-present config refs. That failure is the desired
signal, not a regression, but it is not a fix.

A defensive `| float(0)` or an `unknown` in an exclusion list converts a dead entity from a
crash into a permanent wrong answer. When reviewing HA Jinja, treat both idioms as places a
disappearance can hide.
