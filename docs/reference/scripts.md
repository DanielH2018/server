---
generated_from: scripts/gen_reference_scripts.py
generated_at: 2026-08-25 14:18 UTC
generated_sha: a3e697ae
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/gen_reference_scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

47 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

The sections below split them by **how each one is run**, which is derived from the tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a Claude hook, an Ansible task, or an import edge. The *Reached by* column is the evidence, so a wrong answer is a wrong answer about a real file.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).


**0 of the 21 scripts that run unattended have no test; 6 of all 47 do not.** The first number is the one that matters. An untested script a person runs fails in front of that person; an untested one a cron or a commit gate runs fails unattended, or blocks everybody.

!!! note "Where the Tests column looks"
    First for a `scripts/test_<name>.py`. Failing that, for any test in `scripts/` or `ansible/tests/` that names the script — `gitops_tick.sh` has five, in `test_gitops_manual_trigger.py`, and the naming convention alone called it untested. Those show as *(indirect)*, which means a test exercises it, not that the test is about it.


## Run automatically, on a schedule

9 script(s) — a cron runs it unattended.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `build_docs.py` | Regenerate the reference pages, then build the MkDocs site. | cron: Refresh generated docs (via docs-refresh.sh) | `test_build_docs.py` |
| `gen_infra_map.py` | Render a self-contained HTML map of the homelab infrastructure. | cron: Refresh homelab infrastructure map | `test_gen_infra_map.py` |
| `gen_reference_crons.py` | Generate docs/reference/crons.md — every scheduled job the tree installs. | build_docs.py (a cron runs it unattended) | `test_gen_reference_crons.py` |
| `gen_reference_hosts.py` | Generate docs/reference/hosts.md — the three hosts and what each one is. | build_docs.py (a cron runs it unattended) | `test_gen_reference_hosts.py` |
| `gen_reference_networking.py` | Generate docs/reference/networking.md — what is routed, and what fronts it. | build_docs.py (a cron runs it unattended) | `test_gen_reference_networking.py` |
| `gen_reference_scripts.py` | Generate docs/reference/scripts.md — every first-party script and what it is for. | build_docs.py (a cron runs it unattended) | `test_gen_reference_scripts.py` |
| `gen_reference_secrets.py` | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | build_docs.py (a cron runs it unattended) | `test_gen_reference_secrets.py` |
| `secret_rotation.py` | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | cron: Daily secret rotation audit (via secret-rotation-audit.sh) | `test_secret_rotation.py` |
| `service_catalog.py` | Generate a single HTML page answering "what runs in this homelab". | build_docs.py (a cron runs it unattended) | `test_service_catalog.py` |

## Run automatically, on a commit, CI run, deploy or session

12 script(s) — every commit, CI run, deploy or Claude session runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `deploy_detach_notify.py` | Post-deploy notifier for `scripts/deploy.sh --detach`. | every deploy (deploy.sh) | `test_deploy_detach_notify.py` |
| `deploy_staleness.py` | Refuse a deploy from a git tree that is behind origin/master. | every deploy (deploy.sh) | `test_deploy_staleness.py` |
| `deploy_tags.py` | Validate the --tags a deploy was given, before Ansible silently accepts them. | every deploy (deploy.sh) | `test_deploy_tags.py` |
| `inject_dashboard_annotations.py` | Add the deploy-annotation query to every provisioned Grafana dashboard, from one place. | deploy: ansible/roles/k8s/claude-otel/tasks/dashboards.yml | `test_inject_dashboard_annotations.py` |
| `probe.py` | Read-only homelab diagnostics — one allow-listed surface for the queries that | Claude hook: session-health.py | `test_probe.py` |
| `smoke_extract.py` | Extract newly-added container image references from a unified git diff. | CI: image-smoke.yml | `test_smoke_extract.py` |
| `validate_compose_templates.py` | Render every configured container's docker-compose.yml.j2 and assert the | prek hook (every commit) | `test_validate_compose_templates.py` |
| `validate_config_templates.py` | Render the high-value NON-compose YAML config templates (monitoring) with stubbed vars and | prek hook (every commit) | `test_validate_config_templates.py` |
| `validate_grafana_dashboards.py` | Validate that every provisioned Grafana dashboard's datasource references resolve to a | prek hook (every commit) | `test_validate_grafana_dashboards.py` |
| `validate_ha_config.py` | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | prek hook (every commit) | `test_validate_ha_config.py` |
| `validate_k8s_manifests.py` | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | prek hook (every commit) | `test_validate_k8s_manifests.py` |
| `validate_shell_templates.py` | Render every Jinja-templated shell script under ansible/roles/ with stubbed vars and lint | prek hook (every commit) | `test_validate_shell_templates.py` |

## Imported, never run on their own

11 script(s) — imported by another script — not an entry point.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `docs_provenance.py` | The provenance banner every generated documentation page opens with. | imported by gen_infra_map.py, gen_reference_crons.py, gen_reference_hosts.py, gen_reference_networking.py, gen_reference_scripts.py, gen_reference_secrets.py, service_catalog.py | `test_docs_provenance.py` |
| `ha_state_model.py` | Derived state model for the Home Assistant bedroom control plane. | imported by probe_ha.py, validate_ha_config.py | `test_ha_state_model.py` |
| `infra_map_common.py` | Constants shared by the infra-map inventory, live, model and render stages. | imported by gen_infra_map.py, infra_map_inventory.py, infra_map_live.py, infra_map_model.py, infra_map_render.py | — |
| `infra_map_inventory.py` | Declared state: what ``containers_list`` and the role trees say should run. | imported by gen_infra_map.py, infra_map_model.py | — |
| `infra_map_live.py` | Live state: what the cluster and the Pi report is actually running. | imported by gen_infra_map.py | `test_gen_infra_map.py` *(indirect)* |
| `infra_map_model.py` | Reconciliation: overlay live state onto the declared skeleton. | imported by gen_infra_map.py | — |
| `infra_map_render.py` | Rendering: turn the reconciled model into one self-contained HTML page. | imported by gen_infra_map.py | `test_gen_infra_map.py` *(indirect)* |
| `probe_core.py` | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | imported by ha_state_model.py, postflight.py, probe.py, probe_ha.py, probe_storage.py | `test_probe.py` *(indirect)* |
| `probe_ha.py` | Home Assistant: live state, automations, and the read-only WebSocket trace client. | imported by ha_state_model.py, postflight.py, probe.py | `test_probe_ha.py` |
| `probe_storage.py` | B2 and Longhorn: the transaction ledger, backup spend, and the object listings. | imported by probe.py | `test_probe_storage.py` |
| `route_facts.py` | Shared route facts for the reference generators. | imported by gen_reference_networking.py, service_catalog.py | `test_route_facts.py` |

## Run by hand

15 script(s) — a person runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `b2_drain.py` | Delete a stranded Longhorn backup prefix directly through the B2 API. | playbook: ansible/drain_backup_prefix.yml | `test_b2_drain.py` |
| `deploy.sh` | Run an interactive Ansible deploy under the same lock the automated deployers take. | no automated caller in the tree | `test_deploy_annotations.py` *(indirect)* |
| `dns_witness.py` | Continuously resolve a name against the Pi-hole DNS VIP and record every gap. | no automated caller in the tree | `test_dns_witness.py` |
| `drain_stranded.sh` | Drain stranded Longhorn backup prefixes from B2. | no automated caller in the tree | — |
| `etcd_restore_drill.sh` | prove an off-box etcd snapshot actually restores, without an outage. | no automated caller in the tree | — |
| `export_grafana_dashboards.py` | Export the *customized* Grafana dashboards from the live DB into code. | no automated caller in the tree | `test_export_grafana_dashboards.py` |
| `fetch_grafana_dashboards.py` | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | no automated caller in the tree | — |
| `gen_hosts_block.py` | Emit an /etc/hosts block for every homelab `.local` name, with the right IP per service. | no automated caller in the tree | `test_gen_hosts_block.py` |
| `gitops_tick.sh` | trigger a GitOps deploy tick by hand and report what it did. | no automated caller in the tree | `test_gitops_manual_trigger.py` *(indirect)* |
| `k8s_autodeploy_counts.py` | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | no automated caller in the tree | `test_k8s_autodeploy_denylist.py` *(indirect)* |
| `measure_rollout_gap.py` | Measure real downtime across a rollout by polling a service while it restarts. | no automated caller in the tree | `test_measure_rollout_gap.py` |
| `postflight.py` | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | playbook: ansible/bring-up.sh | `test_postflight.py` |
| `prune_worktrees.py` | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | no automated caller in the tree | `test_prune_worktrees.py` |
| `register_audit.py` | Report which rows of the homelab-review recurring-open register look closed. | no automated caller in the tree | `test_register_audit.py` |
| `startup_baseline.py` | Container-start to pod-Ready for every workload, read from the live cluster. | no automated caller in the tree | `test_startup_baseline.py` |

## Usage

7 script(s) document how to invoke themselves. The rest take `--help`.


### `build_docs.py`

```
uv run python scripts/build_docs.py                          # default site dir
uv run python scripts/build_docs.py --site-dir /tmp/site
uv run python scripts/build_docs.py --skip-generators        # rebuild only
```

### `gen_infra_map.py`

```
uv run python scripts/gen_infra_map.py                     # default output path
uv run python scripts/gen_infra_map.py -o /tmp/map.html
uv run python scripts/gen_infra_map.py --no-live           # declared state only
```

### `gen_reference_crons.py`

```
uv run python scripts/gen_reference_crons.py --out docs/reference/crons.md
```

### `gen_reference_hosts.py`

```
uv run python scripts/gen_reference_hosts.py --out docs/reference/hosts.md
```

### `gen_reference_networking.py`

```
uv run python scripts/gen_reference_networking.py --out docs/reference/networking.md
```

### `gen_reference_scripts.py`

```
uv run python scripts/gen_reference_scripts.py --out docs/reference/scripts.md
```

### `gen_reference_secrets.py`

```
uv run python scripts/gen_reference_secrets.py --out docs/reference/secrets.md
```
