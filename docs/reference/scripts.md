---
generated_from: scripts/docs/reference/scripts.py
generated_at: 2026-09-05 01:43 UTC
generated_sha: af8d1c87
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

134 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

The sections below split them by **how each one is run**, which is derived from the tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a Claude hook, an Ansible task, or an import edge. The *Reached by* column is the evidence, so a wrong answer is a wrong answer about a real file.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).


**1 of the 38 scripts that run unattended have no test; 14 of all 134 do not.** The first number is the one that matters. An untested script a person runs fails in front of that person; an untested one a cron or a commit gate runs fails unattended, or blocks everybody.

!!! note "Where the Tests column looks"
    First for a `scripts/test_<name>.py`. Failing that, for any test in `scripts/` or `ansible/tests/` that names the script — `gitops_tick.sh` has five, in `test_gitops_manual_trigger.py`, and the naming convention alone called it untested. Those show as *(indirect)*, which means a test exercises it, not that the test is about it.


- `scripts/deploy_tools/land.sh` — Claude hook: nudge-land-sh.py


## Run automatically, on a schedule

18 script(s) — a cron runs it unattended.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/docs/reference/backlog.py` | Generate docs/reference/backlog.md — the open findings Claude filed as GitHub Issues. | build_docs.py (a cron runs it unattended) | `test_gen_reference_backlog.py` *(indirect)* |
| `scripts/docs/build_docs.py` | Regenerate the reference pages, then build the MkDocs site. | cron: Refresh generated docs (via docs-refresh.sh) | `test_build_docs.py` |
| `scripts/watchers/cert_expiry.py` | Watch the TLS leaf cert of every PUBLIC hostname this homelab routes, over Traefik. | cron: TLS cert-expiry watch | `test_cert_expiry.py` |
| `scripts/docs/reference/crons.py` | Generate docs/reference/crons.md — every scheduled job the tree installs. | build_docs.py (a cron runs it unattended) | `test_gen_reference_crons.py` *(indirect)* |
| `scripts/docs/reference/decisions.py` | Generate docs/reference/decisions.md — every `# DECIDED:` marker in the tree. | build_docs.py (a cron runs it unattended) | `test_gen_reference_decisions.py` *(indirect)* |
| `scripts/docs/reference/freshness.py` | Generates docs/reference/freshness.md, ranking hand-written pages by source staleness. | build_docs.py (a cron runs it unattended) | `test_gen_reference_freshness.py` *(indirect)* |
| `scripts/docs/gen_doc_fragments.py` | Generate the fact tables the hand-written docs transclude. | build_docs.py (a cron runs it unattended) | `test_gen_doc_fragments.py` |
| `scripts/infra_map/gen_infra_map.py` | Render a self-contained HTML map of the homelab infrastructure. | cron: Refresh homelab infrastructure map | `test_gen_infra_map.py` |
| `scripts/docs/reference/hosts.py` | Generate docs/reference/hosts.md — the three hosts and what each one is. | build_docs.py (a cron runs it unattended) | `test_gen_reference_hosts.py` *(indirect)* |
| `scripts/dev/k8s_autodeploy_counts.py` | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | gen_doc_fragments.py (a cron runs it unattended) | `test_script_bootstraps_present.py` *(indirect)* |
| `scripts/docs/reference/networking.py` | Generate docs/reference/networking.md — what is routed, and what fronts it. | build_docs.py (a cron runs it unattended) | `test_gen_reference_networking.py` *(indirect)* |
| `scripts/diagnostics/probe.py` | Read-only homelab diagnostics. | cron: B2 deletion accounting | `test_probe.py` |
| `scripts/deploy_tools/publish_pr.py` | Publish a cron's local commit as an auto-merging pull request. | cron: Weekly secret rotation (auto tier) (via secret-rotate.sh) | `test_publish_pr.py` |
| `scripts/docs/reference/scripts.py` | Generate docs/reference/scripts.md — every first-party script and what it is for. | build_docs.py (a cron runs it unattended) | `test_gen_reference_scripts.py` *(indirect)* |
| `scripts/secrets_mgmt/secret_rotation.py` | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | cron: Daily secret rotation audit (via secret-rotation-audit.sh) | `test_secret_rotation.py` |
| `scripts/docs/reference/secrets.py` | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | build_docs.py (a cron runs it unattended) | `test_gen_reference_secrets.py` *(indirect)* |
| `scripts/docs/service_catalog.py` | Generate a single HTML page answering "what runs in this homelab". | build_docs.py (a cron runs it unattended) | `test_service_catalog.py` |
| `scripts/docs/reference/state.py` | Generates docs/reference/state.md, "State of the lab" -- one row per autonomous loop. | build_docs.py (a cron runs it unattended) | `test_gen_reference_state.py` *(indirect)* |

## Run automatically, on a commit, CI run, deploy or session

20 script(s) — every commit, CI run, deploy or Claude session runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/validate/compose_templates.py` | Render every configured container's docker-compose.yml.j2 and assert it parses as YAML. | prek hook (every commit) | `test_validate_compose_templates.py` *(indirect)* |
| `scripts/validate/config_templates.py` | Render the high-value NON-compose YAML config templates (monitoring) and assert they parse. | prek hook (every commit) | `test_validate_config_templates.py` *(indirect)* |
| `scripts/deploy.sh` | Run an interactive Ansible deploy under the same lock the automated deployers take. | staging_gate_remote.sh (every commit, CI run, deploy or Claude session runs it) | `test_deploy_exit_codes.py` *(indirect)* |
| `scripts/deploy_tools/deploy_detach_notify.py` | Post-deploy notifier for `scripts/deploy.sh --detach`. | every deploy (deploy.sh) | `test_deploy_detach_notify.py` |
| `scripts/deploy_tools/deploy_staleness.py` | Refuse a deploy from a git tree that is behind origin/master. | every deploy (deploy.sh) | `test_deploy_staleness.py` |
| `scripts/deploy_tools/deploy_tags.py` | Validate the --tags a deploy was given, before Ansible silently accepts them. | every deploy (deploy.sh) | `test_deploy_tags.py` |
| `scripts/deploy_tools/fact_cache_guard.py` | Clear the shared Ansible fact cache when it pins another worktree's interpreter. | every deploy (deploy.sh) | `test_fact_cache_guard.py` |
| `scripts/validate/grafana_dashboards.py` | Validate that every provisioned Grafana dashboard's datasource uid resolves to a real one. | prek hook (every commit) | `test_validate_grafana_dashboards.py` *(indirect)* |
| `scripts/grafana/inject_dashboard_annotations.py` | Add the deploy-annotation query to every provisioned Grafana dashboard, from one place. | deploy: ansible/roles/k8s/claude-otel/tasks/dashboards.yml | `test_inject_dashboard_annotations.py` |
| `scripts/validate/k8s_manifests.py` | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | prek hook (every commit) | `test_validate_k8s_manifests.py` *(indirect)* |
| `scripts/deploy_tools/land.py` | Follow a merged PR through to a verified deploy, in one invocation. | land.sh (every commit, CI run, deploy or Claude session runs it) | `test_land_pipeline.py` *(indirect)* |
| `scripts/deploy_tools/land.sh` | the entry point every doc, skill and hook names; it execs land.py beside it. | Claude hook: nudge-land-sh.py | — |
| `scripts/deploy_tools/prune_releases.py` | Remove old host-script release directories, never the one in use. | deploy: ansible/roles/setup/common/tasks/release_bin.yml | `test_prune_releases.py` |
| `scripts/dev/prune_worktrees.py` | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | Claude hook: session-health.py | `test_prune_worktrees.py` |
| `scripts/validate/shell_templates.py` | Render every Jinja-templated shell script under ansible/roles/ and lint the output. | prek hook (every commit) | `test_backup_health_shim.py` *(indirect)* |
| `scripts/dev/smoke_extract.py` | Extract newly-added container image references from a unified git diff. | CI: image-smoke.yml | `test_smoke_extract.py` |
| `scripts/diagnostics/staging_egress_probe.py` | Acceptance gate for the staging guest's egress fence. | deploy: ansible/roles/setup/hypervisor/templates/staging-nwfilter.xml.j2 | `test_staging_egress_fence.py` *(indirect)* |
| `scripts/deploy_tools/staging_gate_remote.sh` | The daniel-server half of the staging gate. Two arguments: the SHA under test, and the | deploy: ansible/roles/setup/hypervisor/tasks/install.yml | `test_staging_gate_paths_agree.py` *(indirect)* |
| `scripts/validate/unit_templates.py` | Render every systemd unit template under ansible/roles/ and verify the output. | prek hook (every commit) | `test_validate_unit_templates.py` *(indirect)* |
| `scripts/home_assistant/validate_ha_config.py` | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | prek hook (every commit) | `test_validate_ha_config.py` |

## Imported, never run on their own

71 script(s) — imported by another script — not an entry point.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/diagnostics/probe_lib/alerts.py` | `probe.py alerts` -- DOWN history reconstructed from Loki, since Kuma keeps only current state. | imported by probe.py | `test_probe_alerts.py` *(indirect)* |
| `scripts/lib/ansible_jinja_compat.py` | Ansible's `search` test and `bool` filter, reimplemented for a vanilla Jinja2 environment. | imported by shell_lint.py | `test_validate_shell_templates.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/arr.py` | `probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr. | imported by postflight.py, probe.py | `test_probe.py` *(indirect)* |
| `scripts/deploy_tools/await_ci.py` | Wait for master CI to reach a verdict on one SHA. | imported by tools.py | `test_await_ci.py` |
| `scripts/diagnostics/probe_lib/b2_ledger.py` | The B2 spend ledger: what maintenance tools spent, since B2 publishes no usage API. | imported by longhorn.py, probe.py | `test_probe_b2_ledger.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/ci.py` | Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose. | imported by deploy.py, pipeline.py | `test_land_ci.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/classify.py` | Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait. | imported by pipeline.py | `test_land_classify.py` *(indirect)* |
| `scripts/availability_bots/common.py` | Thin re-export of the shared watcher helpers for the availability-watcher bots. | imported by glenstone-bot.py, osteria-francescana-bot.py | `test_availability_bots.py` *(indirect)* |
| `scripts/infra_map/constants.py` | Constants shared by the infra-map inventory, live, model and render stages. | imported by gen_infra_map.py, inventory.py, live.py, model.py, render.py | — |
| `scripts/diagnostics/probe_lib/core.py` | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | imported by alerts.py, arr.py, b2_ledger.py, cert_expiry.py, ha.py, ha_state_model.py, health.py, longhorn.py, metrics.py, monitors.py, pi_plane.py, postflight.py, probe.py, readonly_rbac.py, ui_login.py, vip_placement.py | `test_probe.py` *(indirect)* |
| `scripts/lib/cron_checks.py` | The two cron-environment rules a rendered shell template must satisfy. | imported by shell_templates.py | `test_shell_template_cron_rules.py` *(indirect)* |
| `scripts/lib/cron_targets.py` | Resolve which shell templates under `ansible/roles/` are scheduled as cron `job:` targets. | imported by cron_checks.py, shell_templates.py | `test_shell_template_cron_rules.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/deploy.py` | Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree. | imported by pipeline.py | `test_land_deploy.py` *(indirect)* |
| `scripts/infra_map/diagram.py` | The architecture figure: how a request reaches a workload, and on what it runs. | imported by render.py | `test_infra_map_render.py` *(indirect)* |
| `scripts/lib/doc_freshness.py` | How old a hand-written doc is, and whether the files it names have moved under it. | imported by _mkdocs_freshness.py, freshness.py | `test_doc_freshness.py` |
| `scripts/lib/docs_provenance.py` | The provenance banner every generated documentation page opens with. | imported by backlog.py, crons.py, decisions.py, freshness.py, gen_doc_fragments.py, gen_infra_map.py, hosts.py, networking.py, scripts.py, secrets.py, service_catalog.py, state.py | `test_docs_provenance.py` |
| `scripts/deploy_tools/exit_codes.py` | Every exit-code contract the deploy tools share, named once. | imported by ci.py, deploy.py, deploy_tags.py, merge.py, publish_pr.py, staging_gate.py, tick.py, tools.py | `test_exit_codes.py` |
| `scripts/dev/findings_gh.py` | The gh reads and writes `findings.py` makes, and the one place a plan is executed. | imported by backlog.py, findings.py | — |
| `scripts/dev/findings_model.py` | The finding vocabulary and the pure reads over a gh issue: no gh, no shell, no argv. | imported by _findings_fakes.py, backlog.py, findings.py, findings_gh.py, findings_plans.py, findings_verify.py | — |
| `scripts/dev/findings_plans.py` | What `findings.py` decides to do, as gh argv nobody has run yet. | imported by findings.py, findings_gh.py | — |
| `scripts/dev/findings_tools.py` | Every process boundary `findings.py` crosses, as one injectable object. | imported by _findings_fakes.py, findings.py, findings_gh.py, findings_verify.py | — |
| `scripts/dev/findings_verify.py` | The verify-by gate: what a stored command must clear before it runs, and what it means. | imported by findings.py | `test_findings_verify.py` |
| `scripts/docs/fragment_readers.py` | The readers behind the doc fragments: the tree, parsed, never imported. | imported by gen_doc_fragments.py | `test_gen_doc_fragments.py` *(indirect)* |
| `scripts/docs/fragment_renderers.py` | The renderers behind the doc fragments: pure functions from plain values to markdown. | imported by gen_doc_fragments.py | `test_gen_doc_fragments.py` *(indirect)* |
| `scripts/lib/gh.py` | One way to run the GitHub CLI from a script, with no prompt and no notifier. | imported by findings_tools.py, publish_pr.py, tools.py | `test_gh.py` |
| `scripts/lib/git.py` | One way to run git from a script, with the repository chosen by ``cwd`` alone. | imported by await_ci.py, decisions.py, deploy_staleness.py, doc_freshness.py, docs_provenance.py, prune_worktrees.py, publish_pr.py, rotation_tools.py, state.py, tools.py | `test_git.py` |
| `scripts/infra_map/groups.py` | The functional grouping behind the workload strip under the diagram. | imported by render.py | `test_infra_map_render.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/ha.py` | Home Assistant: live state, automations, and the read-only WebSocket trace client. | imported by ha_state_model.py, postflight.py, probe.py | `test_probe_ha.py` *(indirect)* |
| `scripts/home_assistant/ha_state_checks.py` | Guardrail checks over the HA state model built by `ha_state_model.py`. | imported by ha_state_model.py, validate_ha_config.py | `test_ha_state_checks.py` |
| `scripts/home_assistant/ha_state_model.py` | Derived state model for the Home Assistant bedroom control plane. | imported by ha.py, ha_state_checks.py | `test_ha_state_model.py` |
| `scripts/diagnostics/probe_lib/health.py` | `probe.py health <svc>` — the post-deploy gate, plus the argv builders it shares. | imported by arr.py, monitors.py, postflight.py, probe.py | `test_deploy_detach_notify.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/health_verdict.py` | Step 6: the health verdict, and the two halves a healthy deploy can still leave open. | imported by pipeline.py | `test_land_health_verdict.py` *(indirect)* |
| `scripts/infra_map/html_views.py` | The HTML host panels: one row per service, one panel per host. | imported by render.py | `test_infra_map_render.py` *(indirect)* |
| `scripts/infra_map/inventory.py` | Declared state: what ``containers_list`` and the role trees say should run. | imported by gen_infra_map.py, model.py | — |
| `scripts/lib/invocation_sites.py` | Where a `scripts/...` path can be executed from, read once for two different questions. | imported by scripts.py | `test_invocation_sites.py` |
| `scripts/lib/k8s_context.py` | Ansible's variable semantics, reproduced for the k8s manifest render guard. | imported by k8s_manifests.py | `test_k8s_context.py` |
| `scripts/lib/k8s_net_rules.py` | The two semantic rules on rendered manifests that no schema can make. | imported by k8s_manifests.py | `test_k8s_net_rules.py` |
| `scripts/lib/k8s_pvc.py` | PersistentVolumeClaim names a rendered manifest declares, and the ones it references. | imported by k8s_manifests.py | `test_k8s_pvc.py` |
| `scripts/lib/k8s_roles.py` | Which roles under ``ansible/roles/k8s/`` the manifest validator renders, and which it skips. | imported by k8s_manifests.py | `test_skip_roles_classes_hold.py` *(indirect)* |
| `scripts/lib/k8s_schema.py` | Schema validation for a rendered k8s object: the core OpenAPI check and the vendored CRDs. | imported by k8s_manifests.py | `test_k8s_schema.py` |
| `scripts/lib/k8s_yaml.py` | YAML parsing for rendered k8s manifests: the strict loaders and the ``lookup()`` stub. | imported by k8s_manifests.py, k8s_pvc.py | `test_k8s_yaml.py` |
| `scripts/deploy_tools/land_tags.py` | Derive deploy tags from a merged PR's own file list. | imported by _land_fakes.py, classify.py, tools.py | `test_land_tags.py` |
| `scripts/deploy_tools/land_lib/landing.py` | One PR's landing: the state every phase reads and writes, and the ways it ends. | imported by _land_fakes.py, ci.py, classify.py, deploy.py, health_verdict.py, land.py, merge.py, pipeline.py, tick.py | — |
| `scripts/deploy_tools/land_lib/ledger.py` | What the Landings board reads: one logfmt line per landing, from a Ledger of stamps. | imported by land.py, landing.py | `test_probe_b2_ledger.py` *(indirect)* |
| `scripts/infra_map/live.py` | Live state: what the cluster and the Pi report is actually running. | imported by gen_infra_map.py | `test_infra_map_live.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/longhorn.py` | Longhorn's B2 backup objects: what the estate holds, what it costs, and what block size it's on. | imported by b2_ledger.py, probe.py | `test_probe_longhorn.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/merge.py` | The merge phase: arm `gh pr merge --auto` here, and wait for the merge to arrive. | imported by pipeline.py | `test_land_merge.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/metrics.py` | `probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries. | imported by probe.py | `test_probe.py` *(indirect)* |
| `scripts/infra_map/model.py` | Reconciliation: overlay live state onto the declared skeleton. | imported by gen_infra_map.py | — |
| `scripts/diagnostics/probe_lib/monitors.py` | `probe.py monitors` and `probe.py kuma-drift` -- what is down, and what is missing. | imported by probe.py | `test_probe.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/options.py` | Everything the command line sets, plus the budgets a test shortens. | imported by _land_fakes.py, land.py, landing.py | `test_land_options.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/outcome.py` | The words a landing ends with: the verdict set, the cause set, the Outcome, and say(). | imported by ci.py, classify.py, deploy.py, health_verdict.py, landing.py, ledger.py, merge.py, pipeline.py, tick.py | `test_land_outcome.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/pi_plane.py` | `probe.py targets --pi` and `probe.py pi containers` — first-command triage for daniel-pi. | imported by probe.py | `test_probe_pi_plane.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/pipeline.py` | The phase order, the step headers, and nothing else. | imported by land.py | `test_land_pipeline.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/readonly_rbac.py` | `probe.py readonly-rbac` — is the read-only ServiceAccount still read-only? | imported by probe.py | `test_probe_readonly_rbac.py` *(indirect)* |
| `scripts/lib/registry.py` | A small named-entry registry shared by this repo's CLI dispatchers. | imported by probe.py, run_all.py | `test_registry.py` |
| `scripts/lib/release_bin_groups.py` | Resolve which source files a `release_bin.yml` group deploys. | imported by cron_targets.py | `test_kuma_env_renders_for_every_cron_tag.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/releases.py` | `probe.py releases` -- which commit produced the manifests each k8s service is running. | imported by health.py, probe.py | `test_probe_health.py` *(indirect)* |
| `scripts/infra_map/render.py` | Rendering: turn the reconciled model into one self-contained HTML page. | imported by gen_infra_map.py | `test_infra_map_render.py` *(indirect)* |
| `scripts/lib/render_guard.py` | Shared helpers for the render-guard scripts and other Ansible-inventory readers. | imported by cert_expiry.py, compose_templates.py, config_templates.py, deploy_tags.py, hosts.py, k8s_context.py, k8s_manifests.py, k8s_roles.py, k8s_yaml.py, networking.py, service_catalog.py, shell_lint.py, shell_templates.py, unit_templates.py | `test_render_guard.py` |
| `scripts/lib/repo_paths.py` | The repo path anchors a script under ``scripts/`` reads the Ansible tree through. | imported by await_ci.py, build_docs.py, cert_expiry.py, constants.py, core.py, cron_checks.py, cron_targets.py, crons.py, decisions.py, deploy_detach_notify.py, deploy_tags.py, docs_provenance.py, fact_cache_guard.py, findings_tools.py, findings_verify.py, fragment_readers.py, freshness.py, gen_doc_fragments.py, gen_hosts_block.py, grafana_dashboards.py, ha.py, health.py, hosts.py, invocation_sites.py, k8s_autodeploy_counts.py, k8s_context.py, k8s_pvc.py, k8s_roles.py, k8s_schema.py, land_tags.py, memory_survey.py, monitors.py, networking.py, pi_plane.py, releases.py, render_guard.py, route_facts.py, scripts.py, secret_bearing_host_paths.py, secrets.py, shell_templates.py, staging_egress_probe.py, staging_gate.py, state.py, validate_ha_config.py | `test_render_guard.py` *(indirect)* |
| `scripts/secrets_mgmt/rotation_tools.py` | Every process boundary `secret_rotation.py` crosses, as one injectable object. | imported by _rotation_fakes.py, secret_rotation.py | `test_rotation_tools.py` |
| `scripts/docs/route_facts.py` | Shared route facts for the reference generators. | imported by cert_expiry.py, networking.py, service_catalog.py | `test_route_facts.py` |
| `scripts/lib/shell_lint.py` | Render a Jinja-templated shell script, then lint the output with `bash -n` and shellcheck. | imported by shell_templates.py | `test_backup_health_shim.py` *(indirect)* |
| `scripts/deploy_tools/staging_gate.py` | Ask the staging cluster whether it accepts a commit, from daniel-box. | imported by backfill_staging_gate.py | `test_staging_gate.py` |
| `scripts/infra_map/style.py` | The page's stylesheet, its status vocabulary, and the escape every view calls. | imported by diagram.py, html_views.py, render.py | `test_infra_map_render.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/tick.py` | Step 4, the GitOps tick, retried while the unit's own flock gives up. | imported by deploy.py, pipeline.py | `test_land_tick.py` *(indirect)* |
| `scripts/deploy_tools/land_lib/tools.py` | Every process boundary a landing crosses, as one injectable object. | imported by _land_fakes.py, land.py, landing.py | `test_land_landing.py` *(indirect)* |
| `scripts/diagnostics/probe_lib/vip_placement.py` | `probe.py vip-placement` — does every ETP=Local VIP have a Ready endpoint on an announcing node? | imported by probe.py | `test_probe_vip_placement.py` *(indirect)* |
| `scripts/lib/watcher.py` | Generic scaffold for an external-watcher: fetch -> check -> notify -> healthcheck ping. | imported by cert_expiry.py, common.py | `test_watcher.py` |
| `scripts/lib/yaml_fast.py` | `yaml.safe_load` backed by libyaml, which parses the same schema an order faster. | imported by backfill_staging_gate.py, compose_templates.py, config_templates.py, cron_targets.py, crons.py, deploy_tags.py, fragment_readers.py, gen_hosts_block.py, ha_state_checks.py, ha_state_model.py, inventory.py, invocation_sites.py, k8s_autodeploy_counts.py, k8s_pvc.py, land_tags.py, release_bin_groups.py, render_guard.py, rotation_tools.py, route_facts.py, secret_bearing_host_paths.py, secret_rotation.py, staging_egress_probe.py, staging_expectations.py, state.py, validate_ha_config.py | `test_yaml_fast.py` |

## Run by hand

25 script(s) — a person runs it.

| Script | What it does | Reached by | Tests |
|---|---|---|---|
| `scripts/backup/b2_drain.py` | Delete a stranded Longhorn backup prefix directly through the B2 API. | playbook: ansible/drain_backup_prefix.yml | `test_b2_drain.py` |
| `scripts/deploy_tools/backfill_staging_gate.py` | Drive the staging gate over real master commits and report whether it is trustworthy. | no automated caller in the tree | `test_backfill_staging_gate.py` |
| `scripts/backup/etcd_restore_drill.sh` | prove an off-box etcd snapshot actually restores, without an outage. | no automated caller in the tree | `test_etcd_restore_drill_cron.py` *(indirect)* |
| `scripts/grafana/export_grafana_dashboards.py` | Export the *customized* Grafana dashboards from the live DB into code. | no automated caller in the tree | `test_export_grafana_dashboards.py` |
| `scripts/grafana/fetch_grafana_dashboards.py` | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | no automated caller in the tree | — |
| `scripts/dev/findings.py` | File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues. | no automated caller in the tree | `test_findings.py` |
| `scripts/dev/gen_hosts_block.py` | Emit an /etc/hosts block for every homelab `.local` name, with the right IP per service. | no automated caller in the tree | `test_gen_hosts_block.py` |
| `scripts/deploy_tools/gitops_tick.sh` | trigger a GitOps deploy tick by hand and report what it did. | no automated caller in the tree | `test_gitops_manual_trigger.py` *(indirect)* |
| `scripts/availability_bots/glenstone-bot.py` | Watch Glenstone's timed-entry calendar and alert when a target date opens up. | no automated caller in the tree | `test_availability_bots.py` *(indirect)* |
| `scripts/diagnostics/grafana_panel_report.py` | Classify what a Grafana dashboard page actually rendered. | no automated caller in the tree | `test_grafana_panel_report.py` |
| `scripts/dev/measure_rollout_gap.py` | Measure real downtime across a rollout by polling a service while it restarts. | no automated caller in the tree | `test_measure_rollout_gap.py` |
| `scripts/dev/memory_survey.py` | Survey the project's Claude memory store and report what it costs and what nothing reads. | no automated caller in the tree | `test_memory_survey.py` |
| `scripts/availability_bots/osteria-francescana-bot.py` | Watch Osteria Francescana (via CoverManager) for a table on the target dates. | no automated caller in the tree | `test_availability_bots.py` *(indirect)* |
| `scripts/diagnostics/postflight.py` | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | playbook: ansible/bring-up.sh | `test_postflight.py` |
| `scripts/validate/refresh_crd_schemas.py` | Re-download the vendored CRD JSON schemas that validate/k8s_manifests.py checks against. | no automated caller in the tree | `test_validate_k8s_manifests.py` *(indirect)* |
| `scripts/dev/review_metrics.py` | Print the /homelab-review outcome trend: false-positive and fix-refusal rates. | no automated caller in the tree | `test_review_metrics.py` |
| `scripts/validate/run_all.py` | Run every registered template/manifest validator in one process. | no automated caller in the tree | `test_validate_run_all.py` *(indirect)* |
| `scripts/dev/run_as_cron.sh` | run a command in the environment cron actually gives it. | no automated caller in the tree | `test_run_as_cron.py` |
| `scripts/secrets_mgmt/secret_bearing_host_paths.py` | Deployed host paths whose content embeds a credential, derived from the tree. | no automated caller in the tree | — |
| `scripts/dev/split_module.py` | Split a large Python module along its seams: show the references, then move names by spec. | no automated caller in the tree | `test_split_module.py` |
| `scripts/deploy_tools/staging_expect_remote.sh` | The daniel-server half of the staging expectation check. Piped over ssh by | no automated caller in the tree | — |
| `scripts/deploy_tools/staging_expectations.py` | Check that staging's services ANSWER the way they are supposed to, not just that they start. | no automated caller in the tree | `test_staging_expectations.py` |
| `scripts/diagnostics/ui_login.py` | Mint a Playwright storage-state file holding a logged-in Authelia session. | ui_mcp.sh (a person runs it) | `test_ui_login.py` |
| `scripts/diagnostics/ui_mcp.sh` | Launch @playwright/mcp against this homelab's LAN routes. | no automated caller in the tree | — |
| `scripts/deploy_tools/verify_staging_gate_key.sh` | Prove the staging gate's restricted ssh key is confined to its dispatcher. Run on daniel-box. | no automated caller in the tree | — |

## Usage

15 script(s) document how to invoke themselves. The rest take `--help`.


### `scripts/docs/reference/backlog.py`

```
uv run python scripts/docs/reference/backlog.py --out docs/reference/backlog.md
```

### `scripts/docs/build_docs.py`

```
uv run python scripts/docs/build_docs.py                          # default site dir
uv run python scripts/docs/build_docs.py --site-dir /tmp/site
uv run python scripts/docs/build_docs.py --skip-generators        # rebuild only
```

### `scripts/docs/reference/crons.py`

```
uv run python scripts/docs/reference/crons.py --out docs/reference/crons.md
```

### `scripts/docs/reference/decisions.py`

```
uv run python scripts/docs/reference/decisions.py --out docs/reference/decisions.md
```

### `scripts/dev/findings.py`

```
uv run python scripts/dev/findings.py sync-labels
uv run python scripts/dev/findings.py open --title "..." --body-file f.md \
--severity high --kind gap [--domain network] [--file path/to/file.py:12] \
[--source review-2026-09-02] [--no-vetted-remediation] \
[--verify-by 'uv run python scripts/diagnostics/probe.py health <svc>'] [--dry-run]
uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]
uv run python scripts/dev/findings.py verify --all [--close] [--timeout 120]
uv run python scripts/dev/findings.py verify 688 701 [--close]
```

### `scripts/docs/reference/freshness.py`

```
uv run python scripts/docs/reference/freshness.py --out docs/reference/freshness.md
```

### `scripts/docs/gen_doc_fragments.py`

```
uv run python scripts/docs/gen_doc_fragments.py --out-dir docs/assets/generated/fragments
```

### `scripts/infra_map/gen_infra_map.py`

```
uv run python scripts/infra_map/gen_infra_map.py                     # default output path
uv run python scripts/infra_map/gen_infra_map.py -o /tmp/map.html
uv run python scripts/infra_map/gen_infra_map.py --no-live           # declared state only
```

### `scripts/docs/reference/hosts.py`

```
uv run python scripts/docs/reference/hosts.py --out docs/reference/hosts.md
```

### `scripts/deploy_tools/land.py`

```
land.sh --pr 574 --since <pre-merge-sha>
land.sh --pr 574 --since <sha> --await-merge   # arm `gh pr merge --auto` first, then this
land.sh --pr 574 --arm-merge --await-merge --since <sha>   # arm the merge INSIDE this script
land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand
```

### `scripts/docs/reference/networking.py`

```
uv run python scripts/docs/reference/networking.py --out docs/reference/networking.md
```

### `scripts/docs/reference/scripts.py`

```
uv run python scripts/docs/reference/scripts.py --out docs/reference/scripts.md
```

### `scripts/docs/reference/secrets.py`

```
uv run python scripts/docs/reference/secrets.py --out docs/reference/secrets.md
```

### `scripts/dev/split_module.py`

```
uv run python scripts/dev/split_module.py graph SRC
uv run python scripts/dev/split_module.py split SRC SPEC.json
```

### `scripts/docs/reference/state.py`

```
uv run python scripts/docs/reference/state.py --out docs/reference/state.md
```
