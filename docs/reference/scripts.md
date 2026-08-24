---
generated_from: scripts/gen_reference_scripts.py
generated_at: 2026-08-24 22:28 UTC
generated_sha: bf28f58f
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/gen_reference_scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

47 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).


**12 of 47 have no test file.** That is not automatically wrong — a thin wrapper round another tool may not need one — but it is the list to read before trusting a script you have not run.


## The scripts

| Script | What it does | Tests |
|---|---|---|
| `b2_drain.py` | Delete a stranded Longhorn backup prefix directly through the B2 API. | `test_b2_drain.py` |
| `build_docs.py` | Regenerate the reference pages, then build the MkDocs site. | `test_build_docs.py` |
| `deploy.sh` | Run an interactive Ansible deploy under the same lock the automated deployers take. | — |
| `deploy_detach_notify.py` | Post-deploy notifier for `scripts/deploy.sh --detach`. | `test_deploy_detach_notify.py` |
| `deploy_staleness.py` | Refuse a deploy from a git tree that is behind origin/master. | `test_deploy_staleness.py` |
| `deploy_tags.py` | Validate the --tags a deploy was given, before Ansible silently accepts them. | `test_deploy_tags.py` |
| `dns_witness.py` | Continuously resolve a name against the Pi-hole DNS VIP and record every gap. | `test_dns_witness.py` |
| `docs_provenance.py` | The provenance banner every generated documentation page opens with. | `test_docs_provenance.py` |
| `drain_stranded.sh` | Drain stranded Longhorn backup prefixes from B2. | — |
| `etcd_restore_drill.sh` |  | — |
| `export_grafana_dashboards.py` | Export the *customized* Grafana dashboards from the live DB into code. | `test_export_grafana_dashboards.py` |
| `fetch_grafana_dashboards.py` | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | — |
| `gen_hosts_block.py` | Emit an /etc/hosts block for every homelab `.local` name, with the right IP per service. | `test_gen_hosts_block.py` |
| `gen_infra_map.py` | Render a self-contained HTML map of the homelab infrastructure. | `test_gen_infra_map.py` |
| `gen_reference_crons.py` | Generate docs/reference/crons.md — every scheduled job the tree installs. | `test_gen_reference_crons.py` |
| `gen_reference_hosts.py` | Generate docs/reference/hosts.md — the three hosts and what each one is. | `test_gen_reference_hosts.py` |
| `gen_reference_networking.py` | Generate docs/reference/networking.md — what is routed, and what fronts it. | `test_gen_reference_networking.py` |
| `gen_reference_scripts.py` | Generate docs/reference/scripts.md — every first-party script and what it is for. | `test_gen_reference_scripts.py` |
| `gen_reference_secrets.py` | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | `test_gen_reference_secrets.py` |
| `gitops_tick.sh` |  | — |
| `ha_state_model.py` | Derived state model for the Home Assistant bedroom control plane. | `test_ha_state_model.py` |
| `infra_map_common.py` | Constants shared by the infra-map inventory, live, model and render stages. | — |
| `infra_map_inventory.py` | Declared state: what ``containers_list`` and the role trees say should run. | — |
| `infra_map_live.py` | Live state: what the cluster and the Pi report is actually running. | — |
| `infra_map_model.py` | Reconciliation: overlay live state onto the declared skeleton. | — |
| `infra_map_render.py` | Rendering: turn the reconciled model into one self-contained HTML page. | — |
| `inject_dashboard_annotations.py` | Add the deploy-annotation query to every provisioned Grafana dashboard, from one place. | `test_inject_dashboard_annotations.py` |
| `k8s_autodeploy_counts.py` | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | — |
| `measure_rollout_gap.py` | Measure real downtime across a rollout by polling a service while it restarts. | `test_measure_rollout_gap.py` |
| `postflight.py` | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | `test_postflight.py` |
| `probe.py` | Read-only homelab diagnostics — one allow-listed surface for the queries that | `test_probe.py` |
| `probe_core.py` | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | — |
| `probe_ha.py` | Home Assistant: live state, automations, and the read-only WebSocket trace client. | `test_probe_ha.py` |
| `probe_storage.py` | B2 and Longhorn: the transaction ledger, backup spend, and the object listings. | `test_probe_storage.py` |
| `prune_worktrees.py` | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | `test_prune_worktrees.py` |
| `register_audit.py` | Report which rows of the homelab-review recurring-open register look closed. | `test_register_audit.py` |
| `route_facts.py` | Shared route facts for the reference generators. | `test_route_facts.py` |
| `secret_rotation.py` | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | `test_secret_rotation.py` |
| `service_catalog.py` | Generate a single HTML page answering "what runs in this homelab". | `test_service_catalog.py` |
| `smoke_extract.py` | Extract newly-added container image references from a unified git diff. | `test_smoke_extract.py` |
| `startup_baseline.py` | Container-start to pod-Ready for every workload, read from the live cluster. | `test_startup_baseline.py` |
| `validate_compose_templates.py` | Render every configured container's docker-compose.yml.j2 and assert the | `test_validate_compose_templates.py` |
| `validate_config_templates.py` | Render the high-value NON-compose YAML config templates (monitoring) with stubbed vars and | `test_validate_config_templates.py` |
| `validate_grafana_dashboards.py` | Validate that every provisioned Grafana dashboard's datasource references resolve to a | `test_validate_grafana_dashboards.py` |
| `validate_ha_config.py` | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | `test_validate_ha_config.py` |
| `validate_k8s_manifests.py` | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | `test_validate_k8s_manifests.py` |
| `validate_shell_templates.py` | Render every Jinja-templated shell script under ansible/roles/ with stubbed vars and lint | `test_validate_shell_templates.py` |

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
