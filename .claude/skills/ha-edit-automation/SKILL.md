---
name: ha-edit-automation
description: Author or edit a Home Assistant automation, scene, script, or template sensor the repo's way. Use when adding or changing HA automation/lighting/fan/notification/alert logic in this homelab. Enforces copy-not-template, math-in-a-tested-Jinja-macro, ConfigMap wiring and the state model, then validate → deploy → confirm-loaded. Also use when debugging why an automation didn't fire or when you need ground-truth live HA state — it carries the live-API commands and the alias-slug and recorder-DB traps.
allowed-tools: Read, Edit, Write, Bash, Glob
---

Make a correct, idempotent HA change under `ansible/roles/k8s/home-assistant/`. The repo
is the source of truth — **HA UI edits are overwritten on deploy.** Read the role `CLAUDE.md`
(editing gotchas) and `SETUP.md` (how the bedroom suite fits together) before changing
interdependent logic. Run everything from `/home/ubuntu/server`.

HA itself moved to the k3s cluster at slice-5 B3, but **the edit path did not change**: the
`ansible/roles/k8s/home-assistant/` role copies these same files verbatim into ConfigMaps, so
this role stays the only place to change config. What changed is where you deploy from
(daniel-box) and how you verify (a rollout and the pod, not a container) — see step 5.

## 1. Pick the right file

All under `ansible/roles/k8s/home-assistant/`:

| Change | File (deployed by `copy`, verbatim) |
|---|---|
| Automation | `files/automations/<topic>.yaml` (lighting, wake-and-sleep, fan-and-air, alerts, presence, display, system); a NEW file also goes in `home_assistant_automation_files` in `defaults/main.yml` |
| Scene | `files/scenes.yaml` |
| Script | `files/scripts/<topic>.yaml` (lighting, wake-and-sleep, fan, alerts, test-harness); a NEW file also goes in `home_assistant_script_files` in `defaults/main.yml` |
| Template sensor / binary_sensor | `files/templates.yaml` |
| **Tunable math** (curve/threshold/ramp) | `files/custom_templates/*.jinja` macro **+ a test** |
| A `threshold` sensor | `files/thresholds.yaml` |
| A helper (`input_*`, `timer`) | `files/<domain>.yaml` (`input_boolean.yaml`, `input_number.yaml`, …) |
| HTTP/integrations/`http:`/recorder etc. | `files/configuration.yaml` |
| Dashboard / entity friendly-names | `files/ui-lovelace.yaml` / `files/customize.yaml` |
| A NEW root file behind a fresh `!include` | also goes in `home_assistant_root_files` in `defaults/main.yml` |

**The rule that bites:** HA `{{ }}` Jinja goes in `templates.yaml`, `rest.yaml`, the automations
and the scripts, **never** inline in `configuration.yaml`, `customize.yaml` or `ui-lovelace.yaml`.
Everything ships verbatim (`lookup('file')` in `configmap.yaml.j2`, never `lookup('template')`),
and `validate_ha_config.py` **rejects any `{{`/`{% %}` in those three files**: an Ansible var
there would reach HA unrendered. `template: !include templates.yaml` pulls template sensors in.
`templates/config/secrets.yaml.j2` is the only Ansible-templated config file. The role's
`templates/` root is k8s manifests only — never put HA config there.

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

"Ansible ok" is not done — the live evidence is. Deploying without verifying is not a supported
path here. **Prefer the live REST API over the recorder DB.** This section is also the entry
point when an automation didn't fire, or when you need ground-truth live HA state.

HA runs in the k3s cluster on `daniel-box`, so deploy from daniel-box. The `--tags
home-assistant` deploy ships the config via the k8s role (ConfigMaps + Secret, then a rollout
restart). A restart is ~60-120s.

1. **A new config file must be added to its `home_assistant_*_files` list in the role's
   `defaults/main.yml`** — the ConfigMap loops over those lists and the init container installs
   whatever the ConfigMap holds. A file no list names never reaches `/config`, and
   `validate_ha_config.py` fails on the disagreement.

2. **Commit (step 6), then deploy.** `deploy.sh` deploys `HEAD`, not the working tree.
   ```
   ./scripts/deploy.sh --tags "home-assistant"
   ```
   **Exit 75 means the deploy lock stayed busy and nothing was deployed** — not a playbook
   failure. Use `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" --check`
   for an unlocked dry run if the change is risky. There is no config-only mode for the k8s
   role: the config *is* the ConfigMap, so shipping it is the rollout.

3. **Gate on health:** `kubectl -n homelab get pods -l app=home-assistant` shows 1/1 Running.
   The "could not validate that the sqlite3 database was shutdown cleanly" line on boot is
   **benign** (WAL auto-recovers) — not a deploy failure.

4. **Prove it loaded**, using the live-API commands below:
   - **Assert ALL automations loaded** (not just one): `uv run python scripts/diagnostics/probe.py ha
     verify-automations` — exit 0 = every automation in `files/automations/*.yaml` is present in
     the live instance and not `unavailable`. A non-zero exit lists the dropped/errored ids
     (a schema error HA silently skipped at load).
   - **Assert every referenced entity still EXISTS**: `uv run python scripts/diagnostics/probe.py ha
     verify-entities` — it diffs `state/external_entities.yml` against live HA and exits non-zero
     on anything that vanished. Run it every deploy, not only when you touched entities. Nothing
     else in this repo can see a disappearance: `validate_ha_config.py` resolves references against
     that snapshot, so a name that *stopped* existing reads exactly like one that resolves. On
     2026-08-16 two Pixel sensors disappeared and three bedroom features sat inert behind a clean
     validation — `states()` on a missing entity renders `unknown`, which the automation's own
     exclusion list swallowed. When something does turn up dead, **fix the config first and
     refresh the snapshot second**: `refresh` alone drops the ids and makes the validator start
     failing on the still-present config refs, which is the desired signal, not a fix.
   - Edited an automation → `probe.py ha automation <id-or-alias>` must find it, and
     `last_triggered` must advance once you trigger it.
   - Edited an entity/template → `probe.py ha state <entity_id>` — value present and
     `last_updated` newer than the pod's start time.

5. **Report** the deploy result, the health line, and the live load/fire evidence. If health
   fails or the automation didn't load, pull logs
   (`kubectl -n homelab logs deploy/home-assistant --tail=50` from daniel-box) before
   declaring success.

### The live API

`scripts/diagnostics/probe.py ha …` is read-only and allow-listed (no prompt); it queries HA's
REST API with the `claude_ha_token`:

- **Entity state:** `probe.py ha state <entity_id>` → current `state` + attributes +
  `last_changed`/`last_updated`. `--json` for raw.
- **Did an automation load / fire?** `probe.py ha automation <id-or-alias>` → on/off +
  `last_triggered`. It accepts the automation's `id`, its alias-slug or the full
  `automation.<slug>`. A non-zero exit + "not found" means it did NOT load.
- **Why did it run but no-op?** `probe.py ha why <id-or-alias>` pulls the live per-condition
  trace. Traces are in-memory and wiped on every HA restart, and an automation whose trigger
  NEVER matched leaves no trace — for that case use `ha get logbook/<entity>` + `last_triggered`.
- **Live error log:** `probe.py ha get error_log` — catches a template that parsed structurally
  but throws at render time, or an integration that failed to set up.

To confirm an automation *fired*: note `last_triggered`, cause the trigger, re-query, and check
it advanced.

`probe.py ha` needs a host age key to decrypt the token, so run it on daniel-server or
daniel-box. If HA is down, check first with `probe.py health home-assistant`, then drill down
with `kubectl -n homelab get pods -l app=home-assistant` and the pod logs.

### The verification traps

1. **alias-slug ≠ id.** An automation's `entity_id` is derived from its **alias** (slugified) at
   first creation, NOT its `id`. So `id: bedroom_fan_temperature` lives at
   `automation.bedroom_fan_temperature_control`. `probe.py ha automation` handles this; if you
   ever read `/api/states` or the recorder directly, match by the alias-slug or `attributes.id`.
2. **The recorder DB goes stale after a restart.** Rows can predate the restart. Discriminate
   live vs stale by comparing a row's `last_updated_ts` against the pod's start time
   (`kubectl -n homelab get pod -l app=home-assistant -o jsonpath='{.items[0].status.startTime}'`).
   The DB lives on the Longhorn PVC, so reaching it means going through the pod.
3. **The recorder needs its WAL.** If you must read the SQLite recorder, copy `*.db` **plus**
   `*.db-wal` and `*.db-shm` together — the `.db` alone (or `immutable=1`) gives a stale
   snapshot. A `null` context column does NOT mean "external" — custom integrations stamp their
   own context.

## 6. Commit

Commit the changed file(s) under the role. Commit before deploying: `deploy.sh` deploys `HEAD`,
not the working tree.
Note any non-templated side-effects (e.g. a Z2M device setting via `z2m-device-setting`) in the
role `CLAUDE.md` so they survive a re-pair.

## Watch-outs
- A renamed automation gets a **new** `entity_id` from its new alias — update any reference and
  re-verify by the new slug.
- Don't reintroduce duplicated ramp/curve math — extend the macro and its test instead.
- New/Z2M entities read `unknown` until first report; don't treat that as a broken deploy.
