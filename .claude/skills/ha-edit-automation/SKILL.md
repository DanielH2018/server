---
name: ha-edit-automation
description: Author or edit a Home Assistant automation, scene, script, or template sensor the repo's way. Use when adding or changing HA automation/lighting/fan/notification/alert logic in this homelab. Enforces copy-not-template, math-in-a-tested-Jinja-macro, ConfigMap wiring and the state model, then validate → deploy → confirm-loaded.
allowed-tools: Read, Edit, Write, Bash, Glob
---

Make a correct, idempotent HA change under `ansible/roles/k8s/home-assistant/`. The repo
is the source of truth — **HA UI edits are overwritten on deploy.** Read the role `CLAUDE.md`
(editing gotchas) and `SETUP.md` (how the bedroom suite fits together) before changing
interdependent logic. Run everything from `/home/ubuntu/server`.

HA itself moved to the k3s cluster at slice-5 B3, but **the edit path did not change**: the
`ansible/roles/k8s/home-assistant/` role copies these same files verbatim into ConfigMaps, so
this role stays the only place to change config. What changed is where you deploy from
(daniel-box) and how you verify (a rollout and the pod, not a container) — see `ha-deploy`.

## 1. Pick the right file

All under `ansible/roles/k8s/home-assistant/`:

| Change | File (deployed by `copy`, verbatim) |
|---|---|
| Automation | `files/automations/<topic>.yaml` (lighting, wake-and-sleep, fan-and-air, alerts, presence, display, system); a NEW file also goes in `home_assistant_automation_files` in `defaults/main.yml` |
| Scene | `files/scenes.yaml` |
| Script | `files/scripts/<topic>.yaml` (lighting, wake-and-sleep, fan, alerts, test-harness); a NEW file also goes in `home_assistant_script_files` in `defaults/main.yml` |
| Template sensor / binary_sensor | `files/templates.yaml` |
| **Tunable math** (curve/threshold/ramp) | `files/custom_templates/*.jinja` macro **+ a test** |
| HTTP/integrations/`threshold:`/`http:` etc. | `templates/config/configuration.yaml.j2` |
| Dashboard / entity friendly-names | `templates/config/ui-lovelace.yaml.j2` / `templates/config/customize.yaml.j2` |

**The rule that bites:** HA `{{ }}` Jinja goes in `files/`, **never** inline in
`configuration.yaml.j2`. Both trees ship verbatim (`lookup('file')` in `configmap.yaml.j2`,
never `lookup('template')`), and `validate_ha_config.py` **rejects any `{{`/`{% %}` in
`templates/config/`** — the `.j2` suffix there is vestigial. `template: !include templates.yaml`
pulls template sensors in. `secrets.yaml.j2` is the only Ansible-templated config file.
The role's `templates/` root is k8s manifests only — never put HA config there.

## 2. If it's math, put it in a tested macro — don't inline

Tunable formulas (fan curve, lux gate, wake ramp, hysteresis, caps) live in
`files/custom_templates/*.jinja` as macros: **plain numbers/bools in → number/bool out**. Entity
and time reads (`states()`, `now()`) stay in the YAML caller and are passed in as arguments.

1. Add/extend the macro in `custom_templates/fan.jinja` / `lighting.jinja` (or a new `*.jinja` —
   a new file also goes in `home_assistant_template_files` in `defaults/main.yml`; the validator fails until it does).
2. Add a test in `tests/` (e.g. `test_fan_macros.py`, `test_lighting_macros.py`) via the
   `jinja_harness.py` env. **HA's `round` is banker's rounding** (`forgiving_round`, half-to-even)
   — the harness mirrors it and the fan curve hits `.5` midpoints by design, so test the midpoints.
3. Import the macro from the YAML caller; don't duplicate the formula anywhere.

Keep cross-cutting logic single-sourced: the lux gate lives **only** in
`binary_sensor.bedroom_auto_light_allowed`; alerts route **only** through `script.bedroom_notify`.

## 3. Carry a NEW config file into the ConfigMap

Files that already exist redeploy automatically — the ConfigMap embeds them, so an edit changes
the rendered manifest and `k8s/manifests` rolls the pod. But a **new** `files/*.yaml` or
`custom_templates/*.jinja` needs its own `lookup('file', …)` line in
`templates/configmap.yaml.j2` (and its install line in `deployment.yaml.j2`, plus an `!include`
if HA must load it). Miss that and the deploy is green while the pod never sees the file.
This replaced the Docker-era `common_config_changed` wiring — there are no bind mounts now.

## 3b. Regenerate the state model if you changed who writes what

Adding/moving a service call that writes an entity makes `state/derived_state.yml` + `STATE.md`
stale, and `validate_ha_config.py` fails on it:

```
uv run python scripts/home_assistant/ha_state_model.py generate
```

Review the diff. If the single-writer invariant (`state/sanctioned_writers.yml`) or the
override-boolean tripwire (`state/expected_override_writers.yml`) fires, declare the new writer
there deliberately — those two are hand-maintained on purpose; don't widen them on reflex.

## 4. Validate

```
uv run python scripts/home_assistant/validate_ha_config.py          # YAML, dup keys, !include, template syntax, state-model guardrails
uv run pytest ansible/roles/k8s/home-assistant/tests   # if you touched a macro
```
(The `validate-ha-config` + `pytest` prek hooks run these on commit too.) Fix before deploying —
validation catches Jinja-syntax and structural errors, but NOT HA schema or entity-existence
(the deploy surfaces those live).

## 5. Deploy + confirm it loaded

Invoke **`ha-deploy`** (deploy via Ansible on daniel-box → gate on the rollout → confirm the
automation/entity actually loaded). Then invoke **`ha-verify-state`** to prove behavior: `probe.py ha automation
<id-or-alias>` exists and `last_triggered` advances when triggered. "Ansible ok" is not done —
the live evidence is.

## 6. Commit

Commit the changed file(s) under the role. Don't deploy from the commit — `ha-deploy` owns that.
Note any non-templated side-effects (e.g. a Z2M device setting via `z2m-device-setting`) in the
role `CLAUDE.md` so they survive a re-pair.

## Watch-outs
- A renamed automation gets a **new** `entity_id` from its new alias — update any reference and
  re-verify by the new slug.
- Don't reintroduce duplicated ramp/curve math — extend the macro and its test instead.
- New/Z2M entities read `unknown` until first report; don't treat that as a broken deploy.
