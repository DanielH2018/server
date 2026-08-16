# home-assistant — Home automation platform

LinuxServer.io Home Assistant. See repo-root `CLAUDE.md` for shared conventions, and
[`SETUP.md`](SETUP.md) for a human-readable setup / operation / tuning guide to the bedroom suite
(this file is the editing-gotchas reference).

## At a glance
- **Image:** `lscr.io/linuxserver/homeassistant:<X.Y.Z-lsNN>` — **pinned + Renovate-managed**
  (`watchtower.enable=false`), NOT `:latest`. HA is stateful with monthly, occasionally-breaking
  releases, so it belongs in the critical/stateful tier (like jellyfin/the *arr stack) — bump via
  Renovate PRs (the `/linuxserver/` regex tracks the tag), not watchtower's watch-all `:latest` pool.
  (LSIO is x86-64-maintained; only the 32-bit ARM variant was deprecated — fine for daniel-box.)
- **Host: daniel-box (k8s), since 2026-08-09 — slice 5, B3.** This role is both the workload
  and the **config-authoring home** (validate-ha-config, the macro tests,
  `sanctioned_writers.yml`, the skills all anchor here): its ConfigMap ships
  `templates/config/` + `files/` into the cluster. Edit HA config HERE; deploy with
  `--tags home-assistant` from daniel-box.
- **Port:** 8123 · **Authelia:** no · `home-assistant.<domain>` is served directly by the cluster
  Traefik (companion app unchanged). The `bridge_hostname` forward this used to describe died with
  the suffix retirement (`870723e8`) — the key now appears only in host_vars *comments*, nowhere as
  a live setting · MQTT via the in-cluster `mosquitto` Service · NUT via daniel-server's LAN `3493`
  (DOCKER-USER-locked)
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`


## Where things are documented
This file holds the at-a-glance facts, the copy-not-template convention (the trap that
breaks most edits), testing, and tooling. The behaviour of the automations themselves is
split by topic — `## Notable` had grown to 550 lines, which is loaded into context on
every task in this directory whether or not it is needed.

| Looking for | Read |
|---|---|
| Auth, HACS, `configuration.yaml` templating, entity naming, the PVC, pod networking | [`docs/platform.md`](docs/platform.md) |
| Presence, adaptive lighting, the light/fan mediator, manual override, bedtime + wake | [`docs/lighting-and-presence.md`](docs/lighting-and-presence.md) |
| Threshold alerts, `bedroom_notify` routing, away-hold, UPS/CO2/sensor-offline alerts | [`docs/alerts-and-notifications.md`](docs/alerts-and-notifications.md) |
| Fan control, the YAML dashboard, outdoor AQI + window advisor | [`docs/climate-and-air.md`](docs/climate-and-air.md) |

## The one convention that breaks edits
- **Automations + scenes + scripts + template sensors + shared Jinja macros ARE copy'd (since 2026-06-18).**
  `files/automations.yaml`, `files/scenes.yaml`, `files/scripts.yaml`, `files/templates.yaml`,
  `files/rest.yaml`, and `files/custom_templates/*.jinja` (whole-dir copy —
  fan/lighting/ventilation/diagnostics)
  are shipped verbatim — `configmap.yaml.j2` carries each one with `lookup('file')`, NOT
  `lookup('template')`, because they use HA `{{ }}` Jinja that Ansible's templater would try to
  render and fail (no `{% raw %}` needed). **This is why HA Jinja lives in these files, never
  inline in `configuration.yaml.j2`** — `template: !include templates.yaml` pulls the template
  sensors in. `templates/config/*.j2` ship verbatim too (the suffix is vestigial;
  `validate_ha_config.py` rejects Ansible markers in them); `secrets.yaml.j2` is the only
  genuinely templated config file. Git is the source of truth; HA UI edits are overwritten on
  deploy. A config edit changes the rendered ConfigMap, so `k8s/manifests` rolls the Deployment
  (~120s) — the k8s replacement for the old `common_config_changed` wiring. A *new* file ships
  nothing until `configmap.yaml.j2` and `deployment.yaml.j2`'s init container both name it. First automation: Hue Tap Dial (RDM002) drives the
  `light.bedroom_lights` group (dial = brightness ±12%; B1 = Power: press = smart toggle [on → `bedroom_apply_natural`
  ungated, off → off + manual-off], hold = reset-to-auto [clear overrides, re-sync lux-gated
  via `bedroom_apply_natural_gated` + fan]; B2 = Sleep (moved from B3 2026-07-18): press = sleep TOGGLE
  [in sleep mode + lights on → lights off (stay in sleep mode, fan quiet); else →
  `scene.bedroom_nightlight` + clear manual-off], hold = `script.bedroom_bedtime` (30-min fade);
  B3 = Fan character: press = toggle fan oscillation (sweep↔fixed via `fan.oscillate`), hold = stop the
  fan [engage fan-manual + `bedroom_fan_set` off + cancel fan-dial mode]; B4 = Fan: press = auto [clear fan-manual + `bedroom_apply_fan`
  + cancel fan-dial mode], hold = toggle fan-dial mode [`timer.bedroom_fan_dial`, 5-min sliding window:
  the dial then steps the fan ±1 level via `script.bedroom_fan_nudge`; auto-reverts to light dial on
  expiry — replaces the old hold-to-boost-100%, max fan still reachable by dialing to L9]). Manual taps
  are ungated by design — the lux gate lives on the presence
  path + the reset hold. **Tap Dial gotchas (RDM002, verified live):** match button actions on the
  `*_press_release`/`*_hold_release` events — a tap fires `button_N_press`→`button_N_press_release`,
  but a HOLD fires `button_N_press` (!) then repeats `button_N_hold` then `button_N_hold_release`, so
  matching `*_press`/`*_hold` double-fires the tap before every hold AND runs holds ~3×; the release
  events are mutually exclusive (exactly one per gesture). The LIGHT button (B1) calls
  `script.bedroom_exit_sleep` FIRST (clears `sleep_mode` + AL sleep mode) — using the normal lights
  releases the night state (the daytime sleep-exit the morning reset otherwise owns; closes the "very
  red" trap where a stuck `sleep_mode` made B1's `apply_natural` serve the amber nightlight). B2 is now
  the Sleep button and deliberately does NOT exit sleep (that's the point of it). The FAN
  button (B4) is **fan-only** — it clears the `sleep_mode` flag (un-caps `apply_fan` from its L2 sleep cap)
  but does NOT touch AL sleep or the lights. Two reasons it stays off the lights: clearing AL sleep makes
  AL **self-on the lights asynchronously** (a flash that beat a prompt `light.turn_off`), and the FP300
  illuminance is dominated by the bedroom lights THEMSELVES (~640 lux on / ~48 off), so anything that
  turns the lights off makes the in-room sensor read "dark" and `presence_on` re-lights them (a feedback
  loop the fan button must not get tangled in — see the lux-gate note below).
  **Fan-dial mode (since 2026-06-20):** B4 HOLD toggles `timer.bedroom_fan_dial` (5-min sliding
  window) — while it's `active` the dial steps the fan ±1 level (`script.bedroom_fan_nudge`) instead
  of the lights; it auto-reverts to the light dial on expiry, and a B4 tap cancels it. The timer's
  `active` state IS the mode (no `input_boolean`, so it's off after any HA restart — deliberately
  sidesteps the stale-override-restore trap below). The nudge drives off the
  `input_number.bedroom_fan_expected_level` accumulator (not the laggy DREO cloud %) so rapid turns
  accumulate, engages `bedroom_fan_manual`, and ignores the night/sleep caps (you're in control until
  a B4 tap / the morning reset clears the override). `fan_nudge_level` clamp math is a tested macro.
  The
  stuck state itself recurs because the LSIO HA's unclean shutdown restores a STALE `input_boolean` snapshot
  on restart — every deploy can resurrect an overnight override until the 09:00/alarm morning reset clears it. The
  dial emits `dial_rotate_<dir>_<slow|fast|step>` (caught by the substring match) alongside harmless
  `brightness_step_*` no-ops. Presence
  (FP300) + an `input_boolean` manual-off override + an alarm-driven morning reset live in the
  same file; `bedroom_presence_on` and the morning reset BOTH call `script.bedroom_apply_natural`.
  The lux gate is window-aware (`in morning window OR illuminance < 75` — wake regardless of ambient
  light during the 15-min window, gate on darkness afterwards) and lives in ONE place:
  `binary_sensor.bedroom_auto_light_allowed` (templates.yaml). `bedroom_presence_on` (its darkness
  condition) and `bedroom_apply_natural_gated` reference that sensor — tune the 75-lux threshold / window
  there, once. The window reads `sensor.bedroom_wake_start` (the shared dynamic-wake source — see below),
  the SAME sensor the dispatcher's morning exception uses, so the two are inherently in sync (no duplicated
  formula). **Feedback-loop caveat (tuning):** `sensor.aqara_fp300_illuminance` is dominated by the
  bedroom lights themselves (~640 lux with them on, ~48 off), so the gate is partly circular — turning the
  lights off makes the room read "dark," which can have `presence_on` re-light it ~30 s later. The 75-lux
  threshold sits ABOVE this room's lights-off ambient (~48), so with the lights off the room always reads
  "dark" at night and the gate is effectively always-allow then; pick a value clearly
  below the lights-off daytime ambient if you want to stop daytime auto-lighting (this is why the fan
  button stays out of light control entirely). The illuminance also **LAGS** (sleepy battery sensor on
  `light_sampling: low`, ~100 s to reflect a lights-off drop) — see the sun-aware button-1 HOLD note below.

## Testing
- **Bedroom Jinja math is unit-tested** (`tests/`, run via `uv run pytest` / the prek `pytest`
  hook / CI — wired in `pyproject.toml` `testpaths`). The bug-prone computed logic now lives in
  pure `custom_templates/{fan,lighting}.jinja` macros (entity/time reads — `states()`/`now()` —
  stay in the YAML callers; macros take plain numbers): `fan_target_level` (curve + ±0.7-level
  hysteresis + night/sleep caps, used by `bedroom_apply_fan`), `in_wake_window` /
  `wake_brightness` (morning ramp, used by `bedroom_apply_natural` + `bedroom_apply_wake`;
  `wake_transition` was removed — transition is now a fixed 60 s per ramp step), and
  `auto_light_allowed` (lux gate, used by `templates.yaml`'s `bedroom_auto_light_allowed`).
- **Decision-macro convention:** an automation/script's gating *selection* logic belongs in a pure
  `custom_templates/*.jinja` macro — plain values in (no `states()`/`now()`/`is_state()` inside),
  an action token out — with a truth-table test, exactly like `light_decision` and
  `natural_exception` (the `bedroom_apply_natural` nightlight↔wake selection). The YAML caller reads
  entities and `choose:`-es on the returned token. This is *guidance*; what's *enforced* is that
  the references resolve (service/entity checks) and that every macro has a test (Component 3).
- The harness `tests/jinja_harness.py` renders macros in a bare Jinja2 env that mirrors the handful
  of HA filter overrides the macros use — most importantly HA's `round` is **banker's** rounding
  (`forgiving_round`, round-half-to-even, int at precision 0), NOT Jinja's stock half-away-from-zero
  float; the fan level math lands on `.5` midpoints by design, so this is load-bearing.
  `test_ha_round_semantics.py` pins it. `test_fan_macros.py` carries an old-inline-vs-macro
  equivalence grid (8.8k points) as a permanent behavior-preservation guard against the curve being
  changed in only one place.
- **Adding a tunable formula:** put the math in a `custom_templates/*.jinja` macro (numbers in →
  numbers/bool out), import it from the YAML caller, and add a test — don't inline new math in the
  automations. The `custom_templates/` deploy is a whole-directory copy, so a new `.jinja` ships
  automatically.
- **Config is structurally validated pre-deploy** by the `validate-ha-config` prek hook
  (`scripts/validate_ha_config.py`, runs locally + in CI on any change under the role's
  `templates/`+`files/`). Pure Python (no Docker): it assembles the deployed `/config` layout and
  checks YAML syntax, **duplicate keys**, broken `!include` targets, and the **syntax** of every
  inline `{{ }}`/`{% %}` template + each `custom_templates/*.jinja`. It does NOT do HA *schema*
  validation (unknown keys, bad integration options) or entity-existence checks — that needs
  `hass --script check_config` in a Docker HA image (out of scope); the deploy still catches schema
  errors live.
- **Scenario test harness (since 2026-06-23).** Exercise the bedroom automations ON DEMAND instead of
  waiting for the real trigger (night / leaving home). The dashboard "🧪 Test scenarios" card +
  `input_select.bedroom_test_scenario` (off/bedtime/wake/nightlight/away/arrive/reset) +
  `input_select.bedroom_test_speed` (fast/real — **fast is the default because an `input_select`'s
  first option is its creation default, which an `input_boolean` can't do: it has no `initial:`
  field, only restores**) drive `script.bedroom_run_scenario`. It DRIVES the real scripts/automations,
  never reimplements them: bedtime→`bedroom_bedtime` (gained an optional `fade`, default 1800; fast
  passes 30), wake→`bedroom_preview_wake` (test-only **compressed frame-sweep reusing the tested
  `wake_brightness` macro** — does NOT touch the production `bedroom_wake_ramp`/`bedroom_apply_wake`),
  nightlight→`scene.bedroom_nightlight`, away/arrive→`automation.trigger` (skip_condition),
  reset→`bedroom_clear_overrides` (a DRY extraction shared with the morning reset). **Away is
  response-only:** it tests the lights/fan-off + "Left on" notify; the away notification-HOLD path
  needs a real `person.daniel != home` (set it in Developer Tools → States — no service sets arbitrary
  entity state). Inert until you press Run; the test-only direct light writers (`bedroom_preview_wake`,
  `bedroom_run_scenario` via the nightlight `scene.turn_on`) are declared in
  `state/sanctioned_writers.yml`. Phase 2 also extracted the away/arrive selection into tested macros
  (`away_items_label`/`arrive_relight_allowed`). Spec:
  `docs/superpowers/specs/2026-06-23-ha-scenario-test-harness-design.md`.

## Claude tooling for this role
- **`home-assistant-engineer` agent** (`.claude/agents/`) — read+write HA engineer that knows
  these conventions + traps; delegate HA authoring/debugging to it.
- **Skills** (`.claude/skills/`): `ha-edit-automation` (the authoring workflow — copy-not-template,
  math-in-a-tested-macro, validate→deploy→verify), `ha-deploy` (deploy + confirm-loaded),
  `ha-verify-state` (live state via the API; the recorder + alias-slug traps), `z2m-device-setting`
  (persist a Zigbee device setting via `mosquitto_pub`).
- **`scripts/probe.py ha`** — read-only live HA state (allow-listed, no prompt), authed with the
  SOPS `claude_ha_token`: `probe.py ha state <entity>` · `ha automation <id-or-alias>` (resolves
  the alias-slug≠id trap) · `ha get <api-path>` (e.g. `error_log`). Prefer it over recorder-DB reads.
 · `ha why <id-or-alias>` (alias `ha trace`) pulls the live per-condition automation trace over the
 WS API — answers "it ran but which condition blocked it" (not "it never fired"; traces are
 in-memory, wiped on restart).
 · `ha verify-automations` (post-deploy gate: exit 0 = every automation in files/automations.yaml
 loaded + not unavailable; matches git id ↔ live attributes.id; file-driven so .storage/UI cruft
 is ignored).
- **Derived state model** (`state/STATE.md` + `state/derived_state.yml`, generated by
  `scripts/ha_state_model.py generate`): the machine-derived map of cells/actuators and who
  writes them. Regenerated + freshness-gated by the `validate-ha-config` hook — never hand-edit
  (a stale committed copy fails CI). The single hand-maintained file is
  `state/expected_override_writers.yml` (the 3-boolean write tripwire: CI fails if an
  automation/script writes `bedroom_manual_off`/`bedroom_fan_manual`/`bedroom_sleep_mode` without
  being listed). The resolution check (config refs ∪ `state/external_entities.yml`, snapshotted by
  `ha_state_model.py refresh`) catches a mistyped/renamed entity before it becomes a silent no-op.
  Live view: `scripts/probe.py ha-state` (current cell values + anomalies; `--inventory` for the
  full catalog). This file (CLAUDE.md) remains the home of the runtime/physical *why* the model
  can't derive. Design + Phase-2 plan: `docs/superpowers/specs/2026-06-21-ha-state-model-phase*`.

## Editing
- HA cfg: `templates/config/configuration.yaml.j2` + `files/` (shipped into the cluster by
  `roles/k8s/home-assistant`)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
  — or the `/ha-deploy` skill, which adds the health + loaded-config gates
