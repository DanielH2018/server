---
name: ha-edit-automation
description: Author or edit a Home Assistant automation, scene, script, or template sensor the repo's way. Use when adding or changing HA automation/lighting/fan/notification/alert logic in this homelab. Enforces copy-not-template, math-in-a-tested-Jinja-macro, config-change wiring, then validate → deploy → confirm-loaded.
allowed-tools: Read, Edit, Write, Bash, Glob
---

Make a correct, idempotent HA change under `ansible/roles/containers/home-assistant/`. The repo
is the source of truth — **HA UI edits are overwritten on deploy.** Read the role `CLAUDE.md`
(editing gotchas) and `SETUP.md` (how the bedroom suite fits together) before changing
interdependent logic. Run everything from `/home/ubuntu/server`.

## 1. Pick the right file

All under `ansible/roles/containers/home-assistant/`:

| Change | File (deployed by `copy`, verbatim) |
|---|---|
| Automation | `files/automations.yaml` |
| Scene | `files/scenes.yaml` |
| Script | `files/scripts.yaml` |
| Template sensor / binary_sensor | `files/templates.yaml` |
| **Tunable math** (curve/threshold/ramp) | `files/custom_templates/*.jinja` macro **+ a test** |
| HTTP/integrations/`threshold:`/`http:` etc. | `templates/configuration.yaml.j2` (Ansible-rendered) |
| Dashboard / entity friendly-names | `templates/ui-lovelace.yaml.j2` / `customize.yaml.j2` |

**The rule that bites:** HA `{{ }}` Jinja goes in `files/` (copied byte-for-byte), **never**
inline in `configuration.yaml.j2` (which Ansible templates — it would try to render HA's `{{ }}`
and fail). `template: !include templates.yaml` pulls template sensors in. Never edit `containers/`.

## 2. If it's math, put it in a tested macro — don't inline

Tunable formulas (fan curve, lux gate, wake ramp, hysteresis, caps) live in
`files/custom_templates/*.jinja` as macros: **plain numbers/bools in → number/bool out**. Entity
and time reads (`states()`, `now()`) stay in the YAML caller and are passed in as arguments.

1. Add/extend the macro in `custom_templates/fan.jinja` / `lighting.jinja` (or a new `*.jinja` —
   the whole `custom_templates/` dir is copied, so a new file ships automatically).
2. Add a test in `tests/` (e.g. `test_fan_macros.py`, `test_lighting_macros.py`) via the
   `jinja_harness.py` env. **HA's `round` is banker's rounding** (`forgiving_round`, half-to-even)
   — the harness mirrors it and the fan curve hits `.5` midpoints by design, so test the midpoints.
3. Import the macro from the YAML caller; don't duplicate the formula anywhere.

Keep cross-cutting logic single-sourced: the lux gate lives **only** in
`binary_sensor.bedroom_auto_light_allowed`; alerts route **only** through `script.bedroom_notify`.

## 3. Wire config-change recreation (only for a NEW bind-mounted file)

The existing automations/scenes/scripts/templates/configuration tasks already feed
`common_config_changed`. If you add a *new* bind-mounted config file, `register:` its config task
(`<role>_`-prefixed) and OR it into `common_config_changed` on the deploy include, or an edit
won't recreate the container. See the role `tasks/main.yml` + role `CLAUDE.md`.

## 4. Validate

```
uv run python scripts/validate_ha_config.py          # YAML, dup keys, !include, template syntax
uv run pytest ansible/roles/containers/home-assistant/tests   # if you touched a macro
```
(The `validate-ha-config` + `pytest` prek hooks run these on commit too.) Fix before deploying —
validation catches Jinja-syntax and structural errors, but NOT HA schema or entity-existence
(the deploy surfaces those live).

## 5. Deploy + confirm it loaded

Invoke **`ha-deploy`** (deploy via Ansible → `probe.py health` → confirm the automation/entity
actually loaded). Then invoke **`ha-verify-state`** to prove behavior: `probe.py ha automation
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
