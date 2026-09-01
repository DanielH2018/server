---
generated_from: scripts/docs/gen_reference_scripts.py
generated_at: 2026-09-01 18:17 UTC
generated_sha: 35ae83f3
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/gen_reference_scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

74 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

The sections below split them by **how each one is run**, which is derived from the tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a Claude hook, an Ansible task, or an import edge. The *Reached by* column is the evidence, so a wrong answer is a wrong answer about a real file.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).


**1 of the 31 scripts that run unattended have no test; 12 of all 74 do not.** The first number is the one that matters. An untested script a person runs fails in front of that person; an untested one a cron or a commit gate runs fails unattended, or blocks everybody.

!!! note "Where the Tests column looks"
    First for a `scripts/test_<name>.py`. Failing that, for any test in `scripts/` or `ansible/tests/` that names the script — `gitops_tick.sh` has five, in `test_gitops_manual_trigger.py`, and the naming convention alone called it untested. Those show as *(indirect)*, which means a test exercises it, not that the test is about it.


- `scripts/deploy_tools/land.sh` — Claude hook: nudge-land-sh.py


## Run automatically, on a schedule

9 script(s) — a cron runs it unattended.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/docs/build_docs.py` | Regenerate the reference pages, then build the MkDocs site. | cron: Refresh generated docs (via docs-refresh.sh) | `test_build_docs.py` |
| `scripts/infra_map/gen_infra_map.py` | Render a self-contained HTML map of the homelab infrastructure. | cron: Refresh homelab infrastructure map | `test_gen_infra_map.py` |
| `scripts/docs/gen_reference_crons.py` | Generate docs/reference/crons.md — every scheduled job the tree installs. | build_docs.py (a cron runs it unattended) | `test_gen_reference_crons.py` |
| `scripts/docs/gen_reference_hosts.py` | Generate docs/reference/hosts.md — the three hosts and what each one is. | build_docs.py (a cron runs it unattended) | `test_gen_reference_hosts.py` |
| `scripts/docs/gen_reference_networking.py` | Generate docs/reference/networking.md — what is routed, and what fronts it. | build_docs.py (a cron runs it unattended) | `test_gen_reference_networking.py` |
| `scripts/docs/gen_reference_scripts.py` | Generate docs/reference/scripts.md — every first-party script and what it is for. | build_docs.py (a cron runs it unattended) | `test_gen_reference_scripts.py` |
| `scripts/docs/gen_reference_secrets.py` | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | build_docs.py (a cron runs it unattended) | `test_gen_reference_secrets.py` |
| `scripts/secrets_mgmt/secret_rotation.py` | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | cron: Daily secret rotation audit (via secret-rotation-audit.sh) | `test_secret_rotation.py` |
| `scripts/docs/service_catalog.py` | Generate a single HTML page answering "what runs in this homelab". | build_docs.py (a cron runs it unattended) | `test_service_catalog.py` |

## Run automatically, on a commit, CI run, deploy or session

22 script(s) — every commit, CI run, deploy or Claude session runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/deploy_tools/await_ci.py` | Wait for master CI to reach a verdict on one SHA. | land.sh (every commit, CI run, deploy or Claude session runs it) | `test_await_ci.py` |
| `scripts/deploy.sh` | Run an interactive Ansible deploy under the same lock the automated deployers take. | land.sh (every commit, CI run, deploy or Claude session runs it) | `test_deploy_annotations.py` *(indirect)* |
| `scripts/deploy_tools/deploy_detach_notify.py` | Post-deploy notifier for `scripts/deploy.sh --detach`. | every deploy (deploy.sh) | `test_deploy_detach_notify.py` |
| `scripts/deploy_tools/deploy_staleness.py` | Refuse a deploy from a git tree that is behind origin/master. | every deploy (deploy.sh) | `test_deploy_staleness.py` |
| `scripts/deploy_tools/deploy_tags.py` | Validate the --tags a deploy was given, before Ansible silently accepts them. | every deploy (deploy.sh) | `test_deploy_tags.py` |
| `scripts/deploy_tools/fact_cache_guard.py` | Clear the shared Ansible fact cache when it pins another worktree's interpreter. | every deploy (deploy.sh) | `test_fact_cache_guard.py` |
| `scripts/deploy_tools/gitops_tick.sh` | trigger a GitOps deploy tick by hand and report what it did. | land.sh (every commit, CI run, deploy or Claude session runs it) | `test_gitops_manual_trigger.py` *(indirect)* |
| `scripts/grafana/inject_dashboard_annotations.py` | Add the deploy-annotation query to every provisioned Grafana dashboard, from one place. | deploy: ansible/roles/k8s/claude-otel/tasks/dashboards.yml | `test_inject_dashboard_annotations.py` |
| `scripts/deploy_tools/land.sh` | follow a merged PR through to a verified deploy, in one invocation. | Claude hook: nudge-land-sh.py | — |
| `scripts/deploy_tools/land_tags.py` | Derive deploy tags from a merged PR's own file list. | land.sh (every commit, CI run, deploy or Claude session runs it) | `test_land_tags.py` |
| `scripts/diagnostics/probe.py` | Read-only homelab diagnostics — one allow-listed surface for the queries that | Claude hook: session-health.py | `test_probe.py` |
| `scripts/deploy_tools/prune_releases.py` | Remove old host-script release directories, never the one in use. | deploy: ansible/roles/setup/common/tasks/release_bin.yml | `test_prune_releases.py` |
| `scripts/dev/prune_worktrees.py` | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | Claude hook: session-health.py | `test_prune_worktrees.py` |
| `scripts/dev/smoke_extract.py` | Extract newly-added container image references from a unified git diff. | CI: image-smoke.yml | `test_smoke_extract.py` |
| `scripts/diagnostics/staging_egress_probe.py` | Acceptance gate for the staging guest's egress fence. | deploy: ansible/roles/setup/hypervisor/templates/staging-nwfilter.xml.j2 | `test_staging_egress_fence.py` *(indirect)* |
| `scripts/deploy_tools/staging_gate_remote.sh` | The daniel-server half of the staging gate. Two arguments: the SHA under test, and the | deploy: ansible/roles/setup/hypervisor/tasks/install.yml | `test_staging_gate_paths_agree.py` *(indirect)* |
| `scripts/validate/validate_compose_templates.py` | Render every configured container's docker-compose.yml.j2 and assert the | prek hook (every commit) | `test_validate_compose_templates.py` |
| `scripts/validate/validate_config_templates.py` | Render the high-value NON-compose YAML config templates (monitoring) with stubbed vars and | prek hook (every commit) | `test_validate_config_templates.py` |
| `scripts/validate/validate_grafana_dashboards.py` | Validate that every provisioned Grafana dashboard's datasource references resolve to a | prek hook (every commit) | `test_validate_grafana_dashboards.py` |
| `scripts/home_assistant/validate_ha_config.py` | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | prek hook (every commit) | `test_validate_ha_config.py` |
| `scripts/validate/validate_k8s_manifests.py` | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | prek hook (every commit) | `test_validate_k8s_manifests.py` |
| `scripts/validate/validate_shell_templates.py` | Render every Jinja-templated shell script under ansible/roles/ with stubbed vars and lint | prek hook (every commit) | `test_validate_shell_templates.py` |

## Imported, never run on their own

19 script(s) — imported by another script — not an entry point.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/availability_bots/common.py` | Shared helpers for the availability-watcher bots in this folder. | imported by glenstone-bot.py, osteria-francescana-bot.py | `test_availability_bots.py` *(indirect)* |
| `scripts/lib/docs_provenance.py` | The provenance banner every generated documentation page opens with. | imported by gen_infra_map.py, gen_reference_crons.py, gen_reference_hosts.py, gen_reference_networking.py, gen_reference_scripts.py, gen_reference_secrets.py, service_catalog.py | `test_docs_provenance.py` |
| `scripts/home_assistant/ha_state_model.py` | Derived state model for the Home Assistant bedroom control plane. | imported by validate_ha_config.py | `test_ha_state_model.py` |
| `scripts/infra_map/infra_map_common.py` | Constants shared by the infra-map inventory, live, model and render stages. | imported by gen_infra_map.py, infra_map_inventory.py, infra_map_live.py, infra_map_model.py, infra_map_render.py | — |
| `scripts/infra_map/infra_map_inventory.py` | Declared state: what ``containers_list`` and the role trees say should run. | imported by gen_infra_map.py, infra_map_model.py | — |
| `scripts/infra_map/infra_map_live.py` | Live state: what the cluster and the Pi report is actually running. | imported by gen_infra_map.py | `test_gen_infra_map.py` *(indirect)* |
| `scripts/infra_map/infra_map_model.py` | Reconciliation: overlay live state onto the declared skeleton. | imported by gen_infra_map.py | — |
| `scripts/infra_map/infra_map_render.py` | Rendering: turn the reconciled model into one self-contained HTML page. | imported by gen_infra_map.py | `test_gen_infra_map.py` *(indirect)* |
| `scripts/diagnostics/probe_alerts.py` | `probe.py alerts` -- DOWN history reconstructed from Loki, since Kuma keeps only current state. | imported by probe.py | `test_probe_alerts.py` |
| `scripts/diagnostics/probe_arr.py` | `probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr. | imported by postflight.py, probe.py | `test_probe.py` *(indirect)* |
| `scripts/diagnostics/probe_core.py` | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | imported by postflight.py, probe.py, probe_alerts.py, probe_arr.py, probe_ha.py, probe_health.py, probe_metrics.py, probe_monitors.py, probe_releases.py, probe_storage.py, ui_login.py | `test_probe.py` *(indirect)* |
| `scripts/diagnostics/probe_ha.py` | Home Assistant: live state, automations, and the read-only WebSocket trace client. | imported by postflight.py, probe.py | `test_probe_ha.py` |
| `scripts/diagnostics/probe_health.py` | `probe.py health <svc>` — the post-deploy gate, plus the argv builders it shares. | imported by postflight.py, probe.py, probe_arr.py, probe_monitors.py | `test_probe_health.py` |
| `scripts/diagnostics/probe_metrics.py` | `probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries. | imported by probe.py | `test_probe.py` *(indirect)* |
| `scripts/diagnostics/probe_monitors.py` | `probe.py monitors` and `probe.py kuma-drift` -- what is down, and what is missing. | imported by probe.py | `test_probe.py` *(indirect)* |
| `scripts/diagnostics/probe_releases.py` | `probe.py releases` -- which commit produced the manifests each k8s service is running. | imported by probe.py | `test_probe_releases.py` |
| `scripts/diagnostics/probe_storage.py` | B2 and Longhorn: the transaction ledger, backup spend, and the object listings. | imported by probe.py | `test_probe_storage.py` |
| `scripts/docs/route_facts.py` | Shared route facts for the reference generators. | imported by gen_reference_networking.py, service_catalog.py | `test_route_facts.py` |
| `scripts/deploy_tools/staging_gate.py` | Ask the staging cluster whether it accepts a commit, from daniel-box. | imported by backfill_staging_gate.py | `test_staging_gate.py` |

## Run by hand

24 script(s) — a person runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/backup/b2_drain.py` | Delete a stranded Longhorn backup prefix directly through the B2 API. | playbook: ansible/drain_backup_prefix.yml | `test_b2_drain.py` |
| `scripts/deploy_tools/backfill_staging_gate.py` | Drive the staging gate over a run of real master commits and report whether it is | no automated caller in the tree | `test_backfill_staging_gate.py` |
| `scripts/backup/drain_stranded.sh` | Drain stranded Longhorn backup prefixes from B2. | no automated caller in the tree | — |
| `scripts/backup/etcd_restore_drill.sh` | prove an off-box etcd snapshot actually restores, without an outage. | no automated caller in the tree | `test_etcd_restore_drill_cron.py` *(indirect)* |
| `scripts/grafana/export_grafana_dashboards.py` | Export the *customized* Grafana dashboards from the live DB into code. | no automated caller in the tree | `test_export_grafana_dashboards.py` |
| `scripts/grafana/fetch_grafana_dashboards.py` | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | no automated caller in the tree | — |
| `scripts/dev/gen_hosts_block.py` | Emit an /etc/hosts block for every homelab `.local` name, with the right IP per service. | no automated caller in the tree | `test_gen_hosts_block.py` |
| `scripts/availability_bots/glenstone-bot.py` | Watch Glenstone's timed-entry calendar and alert when a target date opens up. | no automated caller in the tree | `test_availability_bots.py` *(indirect)* |
| `scripts/diagnostics/grafana_panel_report.py` | Classify what a Grafana dashboard page actually rendered. | no automated caller in the tree | `test_grafana_panel_report.py` |
| `scripts/dev/k8s_autodeploy_counts.py` | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | no automated caller in the tree | `test_script_bootstraps_present.py` *(indirect)* |
| `scripts/dev/measure_rollout_gap.py` | Measure real downtime across a rollout by polling a service while it restarts. | no automated caller in the tree | `test_measure_rollout_gap.py` |
| `scripts/dev/memory_survey.py` | Survey the project's Claude memory store and report what it costs and what nothing reads. | no automated caller in the tree | `test_memory_survey.py` |
| `scripts/availability_bots/osteria-francescana-bot.py` | Watch Osteria Francescana (via CoverManager) for a table on the target dates. | no automated caller in the tree | `test_availability_bots.py` *(indirect)* |
| `scripts/diagnostics/postflight.py` | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | playbook: ansible/bring-up.sh | `test_postflight.py` |
| `scripts/validate/refresh_crd_schemas.py` | Re-download the vendored CRD JSON schemas that validate_k8s_manifests.py checks against. | no automated caller in the tree | — |
| `scripts/dev/register_audit.py` | Report which rows of the homelab-review recurring-open register look closed. | no automated caller in the tree | `test_register_audit.py` |
| `scripts/lib/release_bin_groups.py` | Resolve which source files a `release_bin.yml` group deploys. | no automated caller in the tree | — |
| `scripts/dev/run_as_cron.sh` | run a command in the environment cron actually gives it. | no automated caller in the tree | `test_run_as_cron.py` |
| `scripts/secrets_mgmt/secret_bearing_host_paths.py` | Deployed host paths whose content embeds a credential, derived from the tree. | no automated caller in the tree | — |
| `scripts/deploy_tools/staging_expect_remote.sh` | The daniel-server half of the staging expectation check. Piped over ssh by | no automated caller in the tree | — |
| `scripts/deploy_tools/staging_expectations.py` | Check that staging's services ANSWER the way they are supposed to, not just that they start. | no automated caller in the tree | `test_staging_expectations.py` |
| `scripts/diagnostics/ui_login.py` | Mint a Playwright storage-state file holding a logged-in Authelia session. | ui_mcp.sh (a person runs it) | `test_ui_login.py` |
| `scripts/diagnostics/ui_mcp.sh` | Launch @playwright/mcp against this homelab's LAN routes. | no automated caller in the tree | — |
| `scripts/deploy_tools/verify_staging_gate_key.sh` | Prove the staging gate's restricted ssh key is confined to its dispatcher. Run on daniel-box. | no automated caller in the tree | — |

## Usage

7 script(s) document how to invoke themselves. The rest take `--help`.


### `scripts/docs/build_docs.py`

```
uv run python scripts/docs/build_docs.py                          # default site dir
uv run python scripts/docs/build_docs.py --site-dir /tmp/site
uv run python scripts/docs/build_docs.py --skip-generators        # rebuild only
```

### `scripts/infra_map/gen_infra_map.py`

```
uv run python scripts/infra_map/gen_infra_map.py                     # default output path
uv run python scripts/infra_map/gen_infra_map.py -o /tmp/map.html
uv run python scripts/infra_map/gen_infra_map.py --no-live           # declared state only
```

### `scripts/docs/gen_reference_crons.py`

```
uv run python scripts/docs/gen_reference_crons.py --out docs/reference/crons.md
```

### `scripts/docs/gen_reference_hosts.py`

```
uv run python scripts/docs/gen_reference_hosts.py --out docs/reference/hosts.md
```

### `scripts/docs/gen_reference_networking.py`

```
uv run python scripts/docs/gen_reference_networking.py --out docs/reference/networking.md
```

### `scripts/docs/gen_reference_scripts.py`

```
uv run python scripts/docs/gen_reference_scripts.py --out docs/reference/scripts.md
```

### `scripts/docs/gen_reference_secrets.py`

```
uv run python scripts/docs/gen_reference_secrets.py --out docs/reference/secrets.md
```
