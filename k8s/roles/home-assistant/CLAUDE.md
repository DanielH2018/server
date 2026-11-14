# home-assistant — Home automation platform

LinuxServer.io Home Assistant. See repo-root `CLAUDE.md` for shared conventions, and
[`SETUP.md`](SETUP.md) for a human-readable setup / operation / tuning guide to the bedroom suite
(this file is the editing-gotchas reference).

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=home-assistant` gates the rollout
- **Images:** `lscr.io/linuxserver/homeassistant` (`home_assistant_k8s_image`), `alpine`
  (`home_assistant_k8s_init_image`)
- **Route:** `home-assistant.<domain>` · `home-assistant.local.<domain>`, no Authelia
- **Claim:** `home-assistant-config` (daily -> R2)
<!-- /generated_from -->

- **Pinned + Renovate-managed**, NOT `:latest`. HA is stateful with monthly,
  occasionally-breaking releases, so it belongs in the critical/stateful tier (like jellyfin
  and the *arr stack) — bump via Renovate PRs, which track the tag through the
  `/linuxserver/` regex.
- **This role is both the workload and the config-authoring home** — validate-ha-config, the
  macro tests, `sanctioned_writers.yml` and the skills all anchor here, and its ConfigMap
  ships `files/` into the cluster. Edit HA config HERE, then merge; `promote` applies it.
- **Flux applies it.** Its `containers_list` entry
  is in `cluster/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. The
  Deployment is `Recreate`, so a
  change that rolls it takes HA down for the length of the pod restart, about 2 minutes. The
  `home-assistant-prepull` Kustomization pulls the 720 MB image onto the node before that
  restart, so the old pod serves through the pull (`flux_prepull_timeout` in
  `defaults/main.yml`, #5855). `docs/platform.md` has the outages a cold pull caused (#4188).
- **The `home-assistant-snapshot` Job snapshots `home-assistant-config` before each revision
  applies**. The home-assistant Kustomization depends on `home-assistant-snapshot`, so
  a revision applies only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the
  Job's checks. A failed revision that changes home-assistant writes the stateful hold, and
  `promote` does not reset `prod`, because the failed image may already have migrated the
  config volume. Nothing reverts the claim on its own: the operator follows the revert
  runbook in `docs/volume-snapshot-drills.md`. A revert invalidates a companion-app token minted
  after the snapshot, which forces that device to re-authenticate. `k8s_autodeploy: true` still
  lets Renovate automerge the image bump.
- **The Google project id in `secrets.yaml` and the Google service-account Secret substitute from
  `homelab-cluster-vars`.** Their values are `${<key>}` placeholders in the published
  render, and Flux substitutes the values from the `homelab-cluster-vars` Secret. A rotation
  reaches the cluster only through `cluster_vars.py write`.
- **Its snapshot and rollout.** The `home-assistant-snapshot` Job takes the pre-apply
  snapshot, and the Kustomization's `wait` holds the rollout.
- **Port 8123, no Authelia**, so the companion app works unchanged. MQTT goes via the
  in-cluster `mosquitto` Service, NUT via the in-cluster `nut` Service (admitted by
  `k8s/roles/netpol-baseline/templates/networkpolicy-nut.yaml.j2`).
- **Pinned to `k8s_primary_node`** with the home-critical chain, beside mosquitto and the
  VIPs (#3452):
  `k8s/roles/home-assistant/templates/deployment.yaml.j2:DECIDED: every Deployment of a home-critical`.

## Where things are documented
This file holds the at-a-glance facts, the copy-not-template convention (the trap that
breaks most edits), and the rules for testing and tooling. Everything else is split by topic,
because a role doc is loaded on every task in this directory whether it is needed or not.

| Looking for | Read |
|---|---|
| Auth, Marketplace, `configuration.yaml` templating, entity naming, the PVC, pod networking | [`docs/platform.md`](docs/platform.md) |
| Presence, adaptive lighting, the light/fan mediator, the Tap Dial, the lux gate, manual override, bedtime + wake | [`docs/lighting-and-presence.md`](docs/lighting-and-presence.md) |
| Threshold alerts, `bedroom_notify` routing, away-hold, UPS/CO2/sensor-offline alerts | [`docs/alerts-and-notifications.md`](docs/alerts-and-notifications.md) |
| Fan control, the YAML dashboard, outdoor AQI + window advisor | [`docs/climate-and-air.md`](docs/climate-and-air.md) |
| The tested macros, the scenario harness, `probe.py ha`, the state model, the entity-snapshot trap | [`docs/testing-and-tooling.md`](docs/testing-and-tooling.md) |

## The one convention that breaks edits
- **Automations, scenes, scripts, template sensors and shared Jinja macros are COPIED, not
  templated** (since 2026-06-18). `configmap.yaml.j2` carries each with `lookup('file')`,
  because they hold HA `{{ }}` Jinja that the renderer's Jinja would try to render and fail on
  — so no `{% raw %}` is needed, and **HA Jinja lives in these files, never inline in
  `configuration.yaml`**. `templates/config/secrets.yaml.j2` is the only genuinely templated
  config file, and `validate_ha_config.py` rejects a `{{ }}` marker in the three verbatim root
  files, where a role var would reach HA unrendered and render to nothing.
- **A NEW file ships nothing until its list in `defaults/main.yml` names it.** Four lists,
  one per shape: `home_assistant_automation_files` (merged by `!include_dir_merge_list`),
  `home_assistant_script_files` (`!include_dir_merge_named`), `home_assistant_template_files`
  and `home_assistant_root_files`, which also carries the `.py` scripts a `shell_command`
  runs. The validator checks the directory and the list agree.
- **Git is the source of truth; HA UI edits are overwritten on deploy.** A config edit changes
  the rendered ConfigMap, so the renderer's restart annotation rolls the Deployment (~120s).


## Testing
- **Bedroom Jinja math is unit-tested** (`tests/`). The computed logic lives in pure
  `custom_templates/{fan,lighting}.jinja` macros — numbers in, numbers or a token out — and
  the YAML callers do the entity and time reads.
- **New math goes in a macro with a test, never inline in an automation.** A macro ships only
  once `home_assistant_template_files` names it, and the validator fails the prek hook until
  it does.
- **The test harness mirrors HA's `round`, which is banker's rounding.** The fan curve lands
  on `.5` midpoints by design, so stock Jinja rounding would silently disagree.
- **Config is structurally validated pre-deploy** by the `validate-ha-config` prek hook
  (`scripts/home_assistant/validate_ha_config.py`): YAML syntax, duplicate keys, broken
  `!include` targets and template *syntax* — but no HA schema or entity-existence checks.
- **The bedroom automations can be driven on demand** by the scenario harness rather than
  waiting for night or for leaving home — the dashboard's "🧪 Test scenarios" card.

## Claude tooling for this role
- **The `home-assistant-engineer` agent** and the `ha-edit-automation` and
  `z2m-device-setting` skills carry the authoring workflow.
- **`scripts/diagnostics/probe.py ha`** is read-only live HA state; `probe.py ha --help` lists
  its subcommands. `ha verify-automations` is the post-deploy gate. Prefer it over recorder-DB
  reads.
- **The derived state model** (`state/STATE.md`, `state/derived_state.yml`) is generated and
  freshness-gated by the `validate-ha-config` hook — never hand-edit it. The one
  hand-maintained file is `state/expected_override_writers.yml`, the write tripwire for
  `bedroom_manual_off`, `bedroom_fan_manual` and `bedroom_sleep_mode`.

## Editing
- HA cfg: `files/` (shipped into the cluster by `k8s/roles/home-assistant/`; only
  `templates/config/secrets.yaml.j2` is rendered)
- The Google service account JSON has a Secret of its own,
  `templates/google-sa-secret.yaml.j2`, which `configuration.yaml` reads through `!include`.
  `docs/platform.md` has the wiring.
- Deploy: merge the PR, and `promote` applies it. Nothing applies it by hand. After the apply, run the health + loaded-config gates of
  `/ha-edit-automation` (`probe.py ha verify-automations`).

## Traps
- **The entity-reference check is blind to a DISAPPEARANCE.** It resolves against a snapshot
  (`state/external_entities.yml`) that only `ha_state_model.py refresh` rewrites, so a name
  that stopped existing reads like one that resolves. Three features sat inert behind clean
  validation for weeks in 2026-08. Run `probe.py ha verify-entities` as a post-deploy gate,
  and fix the config before refreshing the snapshot.
- **`| float(0)` and `unknown` in an exclusion list hide a dead entity**, converting a crash
  into a permanent wrong answer. Treat both idioms as suspects when reviewing HA Jinja.
- **HA lists Repairs only over the WebSocket API** (`repairs/list_issues`); no REST endpoint
  lists them. `scripts/diagnostics/probe_lib/ha.py` has a stdlib WebSocket client to reuse. A
  `service_not_found` repair survives both the fix and a restart. Clear it with
  `POST /api/repairs/issues/fix` (domain and issue id), then
  `POST /api/repairs/issues/fix/<flow_id>`.
- **After an unclean shutdown, a deploy's restart can restore stale overrides.** HA restores
  `bedroom_sleep_mode` and `bedroom_manual_off` from before its last clean save, so the bedroom
  can come back in a mode it already left. The operator chose not to add a reset on
  `homeassistant` start: the Tap Dial's exit-sleep action and the morning reset clear it. Do not
  propose the on-start reset again without new evidence. `bedroom_fan_manual` is not affected,
  because `bedroom_fan_startup_reconcile` in `files/automations/fan-and-air.yaml` rebuilds it.
