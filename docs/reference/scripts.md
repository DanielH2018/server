---
generated_from: scripts/docs/reference/scripts.py
generated_at: 2026-10-11 00:36 UTC
generated_sha: 4e4415485
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

285 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

The sections below split them by **how each one is run**, which is derived from the tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a Claude hook, an Ansible task, or an import edge. The *Reached by* column is the evidence, so a wrong answer is a wrong answer about a real file. The filter bar above the first table narrows all four at once: by section, by directory, or by any text in a row. A header click sorts by that column.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).

!!! note "Where the Tests column looks"
    Only for a `test_<name>.py` in the script's `tests/` sibling or beside it. A script with an empty cell may still be exercised elsewhere: `gitops_tick.sh` has five tests in `test_gitops_manual_trigger.py`, and a module split out of a facade is run by the facade's suite. The column says where a script's own suite lives, not whether anything reaches it.


## Run automatically, on a schedule

20 script(s) — a cron runs it unattended.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/docs/reference/backlog.py` | docs | Generate docs/reference/backlog.md — the findings Claude filed as GitHub Issues. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/build_docs.py` | docs | Regenerate the reference pages, then build the MkDocs site. | cron: Refresh generated docs (via docs-refresh.sh) | `test_build_docs.py` | — |
| `scripts/docs/reference/crons.py` | docs | Generate docs/reference/crons.md — every scheduled job the tree installs. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/reference/decisions.py` | docs | Generate docs/reference/decisions.md — every `# DECIDED:` marker in the tree. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/dev/findings.py` | dev | File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues. | prune_worktrees.py (a cron runs it unattended) | `test_findings.py` | — |
| `scripts/docs/reference/freshness.py` | docs | Generates docs/reference/freshness.md, ranking hand-written pages by source staleness. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/gen_doc_fragments.py` | docs | Generate the fact tables the hand-written docs transclude. | build_docs.py (a cron runs it unattended) | `test_gen_doc_fragments.py` | — |
| `scripts/infra_map/gen_infra_map.py` | infra_map | Render a self-contained HTML map of the homelab infrastructure. | cron: Refresh homelab infrastructure map | `test_gen_infra_map.py` | — |
| `scripts/docs/reference/hosts.py` | docs | Generate docs/reference/hosts.md — the three hosts and what each one is. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/dev/k8s_autodeploy_counts.py` | dev | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | gen_doc_fragments.py (a cron runs it unattended) | — | — |
| `scripts/docs/reference/networking.py` | docs | Generate docs/reference/networking.md — what is routed, and what fronts it. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/diagnostics/probe.py` | diagnostics | Read-only homelab diagnostics. | cron: B2 deletion accounting | `test_probe.py` | — |
| `scripts/dev/prune_worktrees.py` | dev | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | cron: Weekly git object-store repair | `test_prune_worktrees.py` | — |
| `scripts/deploy_tools/publish_pr.py` | deploy_tools | Publish a cron's local commit as a pull request, and start the landing that merges it. | cron: Weekly secret rotation (auto tier) (via secret-rotate.sh) | `test_publish_pr.py` | [0, 1, 2, 3](#scriptsdeploytoolspublishprpy) |
| `scripts/dev/pytest_shard.py` | dev | Split the pytest suite into N fixed shards of whole test modules, for CI's matrix. | cron: Refresh generated docs (via docs-refresh.sh) | — | — |
| `scripts/docs/reference/scripts.py` | docs | Generate docs/reference/scripts.md — every first-party script and what it is for. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/secrets_mgmt/secret_rotation.py` | secrets_mgmt | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | cron: Weekly secret rotation (auto tier) (via secret-rotate.sh) | `test_secret_rotation.py` | — |
| `scripts/docs/reference/secrets.py` | docs | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/service_catalog.py` | docs | Generate a single HTML page answering "what runs in this homelab". | build_docs.py (a cron runs it unattended) | `test_service_catalog.py` | — |
| `scripts/docs/reference/state.py` | docs | Generates docs/reference/state.md, "State of the lab" -- one row per autonomous loop. | build_docs.py (a cron runs it unattended) | — | — |

## Run automatically, on a commit, CI run, deploy or session

31 script(s) — every commit, CI run, deploy or Claude session runs it.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/validate/compose_templates.py` | validate | Render every configured container's docker-compose.yml.j2 and assert it parses as YAML. | prek hook (every commit) | — | — |
| `scripts/deploy.sh` | (top level) | the entry point every doc, skill, hook and consumer names; it execs deploy_run.py. | deploy: ansible/roles/k8s/monitor-bridge/files/gitops_markers.py | — | [0, 2, 3, 4, 20, 64, 75, 76, 77, 78, 79](#scriptsdeploysh) |
| `scripts/deploy_tools/deploy_detach_notify.py` | deploy_tools | Post-deploy notifier for `scripts/deploy.sh --detach`. | deploy_run.py (every commit, CI run, deploy or Claude session runs it) | `test_deploy_detach_notify.py` | — |
| `scripts/deploy_tools/deploy_run.py` | deploy_tools | Run an interactive Ansible deploy under the locks the automated deployers take. | every deploy (deploy.sh) | — | — |
| `scripts/deploy_tools/deploy_staleness.py` | deploy_tools | Refuse a deploy from a git tree that is behind origin/master. | deploy_run.py (every commit, CI run, deploy or Claude session runs it) | `test_deploy_staleness.py` | — |
| `scripts/deploy_tools/deploy_tags.py` | deploy_tools | Validate the --tags a deploy was given, before Ansible silently accepts them. | deploy: ansible/roles/setup/gitops_deploy/files/deploy_narrow.py | `test_deploy_tags.py` | — |
| `scripts/deploy_tools/digest_provable.py` | deploy_tools | Which k8s roles act ONLY through the bytes their render digest covers. | deploy: ansible/roles/setup/gitops_deploy/files/deploy_narrow.py | `test_digest_provable.py` | — |
| `scripts/backup/etcd_restore_drill.sh` | backup | prove an off-box etcd snapshot actually restores, without an outage. | deploy: ansible/roles/setup/hypervisor/tasks/etcd_drill.yml | — | — |
| `scripts/dev/fact_status.py` | dev | Status, verification and lint for the fact stores — the one entry point. | prek hook (every commit) | — | — |
| `scripts/dev/gen_hook_settings.py` | dev | Render the `hooks` key of `.claude/settings.json` from the hook files' own declarations. | prek hook (every commit) | `test_gen_hook_settings.py` | — |
| `scripts/docs/gen_role_glance.py` | docs | Generate the mechanical half of every role's `## At a glance` block, in place. | prek hook (every commit) | `test_gen_role_glance.py` | — |
| `scripts/deploy_tools/gitops_state.py` | deploy_tools | Operate on the GitOps deployer's own state markers, from the deploy host's shell. | deploy: ansible/roles/k8s/monitor-bridge/files/gitops_markers.py | `test_gitops_state.py` | — |
| `scripts/validate/grafana_dashboards.py` | validate | Validate that every provisioned Grafana dashboard's datasource uid resolves to a real one. | prek hook (every commit) | — | — |
| `scripts/grafana/inject_dashboard_annotations.py` | grafana | Add the deploy-annotation query to every provisioned Grafana dashboard, from one place. | deploy: ansible/roles/k8s/observability/tasks/dashboards.yml | `test_inject_dashboard_annotations.py` | — |
| `scripts/validate/jinja_bash_collisions.py` | validate | Flag bash's `${#var}` length expansion in a Jinja template, before anything renders it. | prek hook (every commit) | — | — |
| `scripts/validate/k8s_manifests.py` | validate | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | prek hook (every commit) | — | — |
| `scripts/deploy_tools/land.py` | deploy_tools | Follow a merged PR through to a verified deploy, in one invocation. | land.sh (every commit, CI run, deploy or Claude session runs it) | — | — |
| `scripts/deploy_tools/land.sh` | deploy_tools | the entry point every doc, skill and hook names; it execs land.py beside it. | Claude hook: fanout-stop.py | — | [0, 1, 64, 75](#scriptsdeploytoolslandsh) |
| `scripts/deploy_tools/narrow_setup.py` | deploy_tools | Which narrow `--tags` value a setup-role change needs, or a refusal to guess. | deploy: ansible/roles/setup/gitops_deploy/files/deploy_narrow.py | `test_narrow_setup.py` | — |
| `scripts/deploy_tools/prune_releases.py` | deploy_tools | Remove old host-script release directories, never the one in use. | deploy: ansible/roles/setup/common/tasks/release_bin.yml | `test_prune_releases.py` | — |
| `scripts/deploy_tools/render_targets.py` | deploy_tools | The k8s services a render-record run renders on one host, derived from the tree. | deploy: ansible/roles/setup/render_records/files/render_records.py | `test_render_targets.py` | — |
| `scripts/validate/root_ignored_files.py` | validate | Flag a root-level file that `.gitignore`'s `/*` rule hides from git. | prek hook (every commit) | — | — |
| `scripts/deploy_tools/setup_routing.py` | deploy_tools | Which playbook and `--tags` value apply each setup role, and whether the tick's host runs it. | deploy: ansible/roles/setup/gitops_deploy/files/deploy_setup_roles.py | `test_setup_routing.py` | — |
| `scripts/validate/setup_templates.py` | validate | Render every setup-plane Jinja template and fail on a variable nothing defines. | prek hook (every commit) | — | — |
| `scripts/deploy_tools/shared_role_callers.py` | deploy_tools | Which deploy tags run a shared k8s role, record or no record. | deploy: ansible/roles/setup/gitops_deploy/files/deploy_narrow.py | `test_shared_role_callers.py` | — |
| `scripts/validate/shell_templates.py` | validate | Render every Jinja-templated shell script under ansible/roles/ and lint the output. | prek hook (every commit) | — | — |
| `scripts/dev/smoke_extract.py` | dev | Extract newly-added container image references from a unified git diff. | CI: image-smoke.yml | `test_smoke_extract.py` | — |
| `scripts/dev/tighten_ratchets.py` | dev | Lower every ratchet allowlist entry to what its file is today. | prek hook (every commit) | — | — |
| `scripts/validate/unit_templates.py` | validate | Render every systemd unit template under ansible/roles/ and verify the output. | prek hook (every commit) | — | — |
| `scripts/validate/vale.sh` | validate | Provision the pinned Google style package, then run Vale over the files prek hands us. | prek hook (every commit) | — | — |
| `scripts/home_assistant/validate_ha_config.py` | home_assistant | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | prek hook (every commit) | `test_validate_ha_config.py` | — |

## Imported, never run on their own

201 script(s) — imported by another script — not an entry point.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/dev/fanout_lib/abandon.py` | dev | Stop or abandon fan-out batches: `fanout.py place stop` and `fanout.py place abandon`. | imported by place.py | — | — |
| `scripts/diagnostics/probe_lib/alerts.py` | diagnostics | `probe.py alerts` -- DOWN history reconstructed from Loki, since Kuma keeps only current state. | imported by subcommands.py | — | — |
| `scripts/lib/ansible_inventory.py` | lib | The one reading of ``containers_list`` and ``hosts.ini`` every script under ``scripts/`` shares. | imported by brief.py, compose_templates.py, constants.py, core.py, diagram.py, estate.py, fragments_hosts.py, gen_doc_fragments.py, glance_facts.py, health_docker.py, inventory.py, k8s_manifests.py, k8s_roles.py, landing_blockers.py, launch_gates.py, lint.py, narrow_containers.py, new_k8s_service.py, render_guard.py, setup_gates.py | `test_ansible_inventory.py` | — |
| `scripts/lib/ansible_jinja_env.py` | lib | The Jinja environment every render guard uses, carrying ansible-core's own filters. | imported by compose_templates.py, k8s_context.py, k8s_manifests.py, setup_templates.py, shell_lint.py, shell_templates.py, unit_templates.py | `test_ansible_jinja_env.py` | — |
| `scripts/dev/fanout_lib/review/api.py` | dev | The review pipeline's public surface: what code outside `fanout_lib/review/` may import. | imported by place.py, review_stats.py, review_unit.py | — | — |
| `scripts/diagnostics/probe_lib/arr.py` | diagnostics | `probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr. | imported by cli_parser.py, postflight.py, subcommands.py | — | — |
| `scripts/lib/facts/atoms.py` | lib | Resolve a citation against a checkout and hash the thing it names. | imported by evidence.py, lint.py, lock.py | — | — |
| `scripts/deploy_tools/await_ci.py` | deploy_tools | Wait for master CI to reach a verdict on one SHA. | imported by tools.py | `test_await_ci.py` | [0, 1, 2, 75](#scriptsdeploytoolsawaitcipy) |
| `scripts/lib/b2.py` | lib | One Backblaze B2 native-API session: authorize, list a prefix, delete a version. | imported by b2_api.py, b2_drain.py | `test_b2.py` | — |
| `scripts/diagnostics/probe_lib/b2_api.py` | diagnostics | The Longhorn listing from B2 and its parser. | imported by longhorn.py | — | — |
| `scripts/diagnostics/probe_lib/b2_ledger.py` | diagnostics | The B2 spend ledger: what maintenance tools spent, since B2 publishes no usage API. | imported by longhorn.py, subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/b2_spend.py` | diagnostics | The `b2-spend` report: measured Class B backup spend, per volume and per backup target. | imported by b2_ledger.py | — | — |
| `scripts/dev/fanout_lib/review/base_check.py` | dev | Which of a PR's new tests still pass with its code changes taken out. | imported by _review_fakes.py, hunk_check.py, review.py | — | — |
| `scripts/dev/findings_lib/boundaries.py` | dev | Every process boundary `findings.py` crosses, as one injectable object. | imported by _findings_fakes.py, claim_cli.py, export_cli.py, findings.py, gh_calls.py, history_cli.py, target.py | — | — |
| `scripts/dev/fanout_lib/brief.py` | dev | The brief a headless fan-out agent reads on stdin. | imported by _review_fakes.py, claims.py, launch_gates.py, place.py, red_gate.py, review.py, review_prompts.py, review_record.py, status.py, transport.py | — | — |
| `scripts/docs/catalog_backup.py` | docs | Longhorn backup tier and GitOps auto-deploy eligibility for one ``containers_list`` entry. | imported by gen_role_glance.py, service_catalog.py | `test_catalog_backup.py` | — |
| `scripts/docs/catalog_facts.py` | docs | Route and auth-tier derivations for one ``containers_list`` entry. | imported by gen_role_glance.py, service_catalog.py | — | — |
| `scripts/docs/catalog_model.py` | docs | The service catalogue's row type, its "cannot be derived" marker, and its path anchors. | imported by catalog_backup.py, catalog_facts.py, catalog_render.py, gen_role_glance.py, service_catalog.py | — | — |
| `scripts/docs/catalog_render.py` | docs | The two renderings of the service catalogue: the MkDocs page and the standalone HTML. | imported by service_catalog.py | — | — |
| `scripts/deploy_tools/land_lib/ci.py` | deploy_tools | Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose. | imported by deploy.py, pipeline.py | — | — |
| `scripts/lib/facts/citations.py` | lib | The closed citation grammar for the two fact stores. | imported by atoms.py, evidence.py, lint.py, lock.py, report.py | — | — |
| `scripts/dev/findings_lib/claim.py` | dev | Whether a claim on an issue is still live, decided from the worktree that holds it. | imported by claim_cli.py, findings.py | — | — |
| `scripts/dev/findings_lib/claim_cli.py` | dev | The four claim subcommands: `claim`, `release`, `claims` and `reap`. | imported by findings.py | — | — |
| `scripts/dev/fanout_lib/claims.py` | dev | The claims a fan-out takes under the orchestrator's own branch, and the batch specs they cover. | imported by place.py | — | — |
| `scripts/deploy_tools/land_lib/classify.py` | deploy_tools | Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait. | imported by pipeline.py | — | — |
| `scripts/dev/fanout_lib/clean.py` | dev | Remove a finished batch's worktree, by lib.worktrees' content check — spec §4. | imported by abandon.py, place.py | — | — |
| `scripts/dev/findings_lib/cli.py` | dev | The argparse construction for `findings.py`: every subparser, no boundary calls. | imported by findings.py | — | — |
| `scripts/lib/cli_help.py` | lib | The `--help` contract every entry point under `scripts/` answers. | imported by deploy_run.py, export_grafana_dashboards.py, longhorn_reap_orphan_backups.py, longhorn_reap_orphan_snapshots.py, refresh_vendored_schemas.py, renovate_rebase.py, runbook_gates.py, smoke_extract.py | — | — |
| `scripts/diagnostics/probe_lib/cli_parser.py` | diagnostics | probe.py's argparse surface: every subparser, plus the `cert` openssl stage builder. | imported by curl_pipeline.py, probe.py | — | — |
| `scripts/lib/cli_registry.py` | lib | A small named-entry registry shared by this repo's CLI dispatchers. | imported by subcommands.py | `test_cli_registry.py` | — |
| `scripts/infra_map/constants.py` | infra_map | Constants shared by the infra-map inventory, live, model and render stages. | imported by gen_infra_map.py, inventory.py, live.py, model.py, render.py | — | — |
| `scripts/secrets_mgmt/consumers.py` | secrets_mgmt | Who holds a copy of a secret, by two deliberately different mechanisms. | imported by secret_rotation.py | — | — |
| `scripts/diagnostics/probe_lib/core.py` | diagnostics | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | imported by _alert_fixtures.py, alerts.py, arr.py, b2_api.py, b2_ledger.py, cli_parser.py, curl_pipeline.py, fetch_grafana_dashboards.py, ha.py, ha_state_model.py, health.py, health_docker.py, kuma_live.py, longhorn.py, metrics.py, monitors.py, pi_plane.py, postflight.py, probe.py, readonly_rbac.py, shed_set.py, ui_login.py | — | — |
| `scripts/validate/validate_lib/cron_checks.py` | validate | The three cron-environment rules a rendered shell template must satisfy. | imported by shell_templates.py | — | — |
| `scripts/validate/validate_lib/cron_targets.py` | validate | Resolve which shell templates under `ansible/roles/` are scheduled as cron `job:` targets. | imported by cron_checks.py, shell_templates.py | — | — |
| `scripts/diagnostics/probe_lib/curl_pipeline.py` | diagnostics | The streaming half of probe.py: argv -> curl/openssl stages -> a piped run. | imported by probe.py | — | — |
| `scripts/deploy_tools/land_lib/deploy.py` | deploy_tools | Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree. | imported by pipeline.py | — | — |
| `scripts/deploy_tools/deploy_detach.py` | deploy_tools | `deploy.sh --detach`: take the locks here, then run the playbook in a forked child. | imported by deploy_run.py | — | — |
| `scripts/deploy_tools/deploy_flags.py` | deploy_tools | What `deploy.sh` refuses a command line over, and the exception its gates refuse with. | imported by deploy_run.py | `test_deploy_flags.py` | — |
| `scripts/deploy_tools/deploy_playbook.py` | deploy_tools | The playbook run of a foreground deploy, and what a finished one records. | imported by deploy_detach.py, deploy_under_locks.py | — | — |
| `scripts/deploy_tools/deploy_under_locks.py` | deploy_tools | The locked half of a foreground deploy: tree lock, snapshot, service locks, playbook. | imported by deploy_detach.py, deploy_run.py | — | — |
| `scripts/lib/deployer_park.py` | lib | What the GitOps deployer's own markers say it has deferred: a park, or a pending role. | imported by deploy_staleness.py, gitops_view.py, landing_blockers.py | `test_deployer_park.py` | — |
| `scripts/deploy_tools/land_lib/detach.py` | deploy_tools | `land.sh --detach`: fork the landing into a logged child, and record where to find it. | imported by land.py, land_probe.py, landing_blockers.py | — | — |
| `scripts/lib/detach_fork.py` | lib | Run work that outlives its caller: `land.sh --detach` and `deploy.sh --detach` share this. | imported by deploy_detach.py, deploy_probe.py, detach.py | `test_detach_fork.py` | — |
| `scripts/infra_map/diagram.py` | infra_map | The architecture figure: how a request reaches a workload, and on what it runs. | imported by render.py | — | — |
| `scripts/lib/doc_freshness.py` | lib | How old a hand-written doc is, and whether the files it names have moved under it. | imported by _mkdocs_freshness.py, freshness.py | `test_doc_freshness.py` | — |
| `scripts/lib/docs_provenance.py` | lib | The provenance banner every generated documentation page opens with. | imported by backlog.py, catalog_render.py, crons.py, decisions.py, fragments_deploy.py, freshness.py, gen_doc_fragments.py, gen_infra_map.py, hosts.py, networking.py, scripts.py, secrets.py, service_catalog.py, state.py | `test_docs_provenance.py` | — |
| `scripts/lib/estate.py` | lib | The inventory as the docs generators read it: hosts, each host's vars, and its entries. | imported by catalog_backup.py, fragment_readers.py, fragments_hosts.py, fragments_storage.py, gen_doc_fragments.py, gen_role_glance.py, hosts.py, k8s_roles.py, networking.py, render_context.py, service_catalog.py | `test_estate.py` | — |
| `scripts/lib/facts/evidence.py` | lib | Diff evidence for a ``moved`` finding: which removed identifiers the section's prose names. | imported by lock.py | — | — |
| `scripts/lib/exit_codes.py` | lib | Every exit-code contract this repo's entry points share, named once. | imported by _land_fakes.py, ci.py, deploy.py, deploy_detach.py, deploy_detach_notify.py, deploy_flags.py, deploy_playbook.py, deploy_run.py, deploy_staleness.py, deploy_tags.py, deploy_under_locks.py, fragments_deploy.py, land.py, merge.py, narrow_broad.py, outcome.py, prune_worktrees.py, publish_pr.py, renovate_rebase.py, runbook_gates.py, scripts.py, tick.py, tools.py | `test_exit_codes.py` | — |
| `scripts/dev/findings_lib/export_cli.py` | dev | The `export` subcommand: the whole `claude` register, every state, written as one JSON file. | imported by findings.py | — | — |
| `scripts/dev/fanout.py` | dev | The issue-fanout tooling: place batches, wait on them, run a review unit, sum the reviews. | imported by fanout_place.py, fanout_probe.py, fanout_review.py, fanout_review_stats.py | — | — |
| `scripts/dev/foreign_owned.py` | dev | Find the paths under a directory that another uid owns, and say how to clear them. | imported by prune_worktrees.py | `test_foreign_owned.py` | — |
| `scripts/docs/fragment_readers.py` | docs | The readers behind the doc fragments: the tree, parsed, never imported. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragment_renderers.py` | docs | The renderers behind the doc fragments: pure functions from plain values to markdown. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragments_lib/fragments_bridge.py` | docs | Doc fragments for the monitor-bridge pages (docs/monitor-bridge-checks.md, docs/monitor-bridge-internals.md). | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragments_lib/fragments_deploy.py` | docs | Doc fragments for the deploy, landing and issue-claiming pages. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragments_lib/fragments_hosts.py` | docs | Doc fragments for the host, network and per-service pin pages. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragments_lib/fragments_storage.py` | docs | Doc fragments for the Longhorn, snapshot and deadman pages. | imported by gen_doc_fragments.py | — | — |
| `scripts/lib/gh.py` | lib | One way to run the GitHub CLI from a script, with no prompt and no notifier. | imported by boundaries.py, publish_pr.py, renovate_branch_sweep.py, renovate_rebase.py, signing.py, tools.py, transport.py | `test_gh.py` | — |
| `scripts/dev/findings_lib/gh_calls.py` | dev | The gh reads and writes `findings.py` makes, and the one place a plan is executed. | imported by backlog.py, claim_cli.py, export_cli.py, findings.py, history_cli.py | — | — |
| `scripts/lib/git.py` | lib | One way to run git from a script, with the repository chosen by ``cwd`` alone. | imported by await_ci.py, citations.py, clean.py, decisions.py, deploy_run.py, deploy_staleness.py, deploy_tags.py, deploy_under_locks.py, doc_freshness.py, docs_provenance.py, evidence.py, fact_status.py, land.py, lint.py, narrow_broad.py, narrow_filters.py, narrow_git.py, narrow_paths.py, narrow_setup.py, narrow_setup_index.py, narrow_templates.py, place.py, prune_worktrees.py, publish_pr.py, pytest_shard.py, releases.py, releases_diff.py, releases_render.py, removed.py, render_guard.py, renovate_branch_sweep.py, report.py, root_ignored_files.py, rotation_tools.py, setup_role_chains.py, shared_role_reach.py, state.py, tools.py, tracked_paths.py, transport.py, worktrees.py | `test_git.py` | — |
| `scripts/secrets_mgmt/git_dates.py` | secrets_mgmt | When each secret's ciphertext last changed, read out of git. | imported by secret_rotation.py | — | — |
| `scripts/dev/fanout_lib/review/git_state.py` | dev | The git state outside a red batch's worktree that the red author could write (#3864, #3879). | imported by review.py | — | — |
| `scripts/lib/git_testing.py` | lib | Build a throwaway git repository for a test, with the inherited `GIT_*` environment gone. | imported by _deploy_sh_fakes.py, _narrow_fixtures.py, _release_fixtures.py, _scratch_pytest.py | `test_git_testing.py` | — |
| `scripts/diagnostics/probe_lib/gitops_view.py` | diagnostics | `probe.py gitops-state` — every marker the GitOps deployer keeps, read-only, with its way out. | imported by subcommands.py | — | — |
| `scripts/docs/glance_facts.py` | docs | The setup-plane and Pi-plane fact readers behind `gen_role_glance.py`, and what every plane shares. | imported by gen_role_glance.py | — | — |
| `scripts/diagnostics/grafana_panel_report.py` | diagnostics | Classify what a Grafana dashboard page actually rendered. | no `__main__` guard; imported by test_grafana_panel_report.py, test_ui_smoke_grafana.py | `test_grafana_panel_report.py` | — |
| `scripts/infra_map/groups.py` | infra_map | The functional grouping behind the workload strip under the diagram. | imported by render.py | — | — |
| `scripts/diagnostics/probe_lib/ha.py` | diagnostics | Home Assistant: live state, automations, and the read-only WebSocket trace client. | imported by ha_state_model.py, postflight.py, subcommands.py | — | — |
| `scripts/home_assistant/ha_state_checks.py` | home_assistant | Guardrail checks over the HA state model built by `ha_state_model.py`. | imported by ha_state_model.py, validate_ha_config.py | `test_ha_state_checks.py` | — |
| `scripts/home_assistant/ha_state_model.py` | home_assistant | Derived state model for the Home Assistant bedroom control plane. | imported by ha.py, ha_state_checks.py | `test_ha_state_model.py` | — |
| `scripts/deploy_tools/land_lib/handoff.py` | deploy_tools | Hand a landing to a lander unit, for a user who cannot land a PR itself. | imported by land.py | — | — |
| `scripts/dev/fanout_lib/review/hardened_runs.py` | dev | The git and pytest calls the red/green measures make, so nothing the agent wrote steers them. | imported by base_check.py, hunk_check.py, red_gate.py, review.py, worktree_reset.py | — | — |
| `scripts/diagnostics/probe_lib/health.py` | diagnostics | `probe.py health <svc>` — the post-deploy gate, from a deploy tag to a per-workload verdict. | imported by probe.py, shed_set.py, subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/health_cronjob.py` | diagnostics | The CronJob half of `probe.py health` — a role with a CronJob and no rollout to gate on. | imported by health.py | — | — |
| `scripts/diagnostics/probe_lib/health_docker.py` | diagnostics | `probe.py health --docker <svc>`, and the direct-address lookups the arr probes need. | imported by arr.py, health.py, postflight.py | — | — |
| `scripts/diagnostics/probe_lib/health_kubectl.py` | diagnostics | The kubectl argv builders and the pod selector `probe.py health` runs its queries through. | imported by health.py, kuma_live.py | — | — |
| `scripts/diagnostics/probe_lib/health_rollout.py` | diagnostics | The rollout half of `probe.py health` — a Deployment or DaemonSet's verdict. | imported by health.py, health_cronjob.py, kuma_live.py | — | — |
| `scripts/deploy_tools/land_lib/health_verdict.py` | deploy_tools | Step 6: the health verdict, and the two halves a healthy deploy can still leave open. | imported by pipeline.py | — | — |
| `scripts/dev/fanout_lib/review/held_hooks.py` | dev | The project hooks and settings a review pipeline holds from start, outside the worktree. | imported by review.py | — | — |
| `scripts/dev/findings_lib/history_cli.py` | dev | The two history subcommands: `history` searches past findings, `show` prints one. | imported by findings.py | — | — |
| `scripts/infra_map/html_views.py` | infra_map | The HTML host panels: one row per service, one panel per host. | imported by render.py | — | — |
| `scripts/dev/fanout_lib/review/hunk_check.py` | dev | Whether the red tests notice each hunk of the fix when it alone is taken out. | imported by _review_fakes.py, review.py | — | — |
| `scripts/infra_map/inventory.py` | infra_map | Declared state: what ``containers_list`` and the role trees say should run. | imported by gen_infra_map.py, model.py | — | — |
| `scripts/lib/invocation_sites.py` | lib | Where a `scripts/...` path can be executed from, read once for two different questions. | imported by script_classify.py | `test_invocation_sites.py` | — |
| `scripts/dev/findings_lib/issue_model.py` | dev | The finding vocabulary and the pure reads over a gh issue: no gh, no shell, no argv. | imported by _findings_fakes.py, backlog.py, brief.py, claim.py, claim_cli.py, cli.py, export_cli.py, findings.py, gh_calls.py, history_cli.py, launch_gates.py, plans.py, solo_only.py, transport.py, verify.py | — | — |
| `scripts/lib/jinja_comments.py` | lib | Blank a template's `{# … #}` comments so a text reader cannot match inside one. | imported by catalog_backup.py, glance_facts.py, k8s_roles.py, route_facts.py | `test_jinja_comments.py` | — |
| `scripts/lib/jinja_defaults.py` | lib | Resolve a generated doc's `{{ … }}` to the value every host would get, or leave it as written. | imported by crons.py, glance_facts.py | `test_jinja_defaults.py` | — |
| `scripts/lib/json_types.py` | lib | The type of a parsed JSON document, and the narrowing helpers that go with it. | imported by boundaries.py, export_cli.py, gh.py, gh_calls.py, glance_facts.py, history_cli.py, kubectl.py, kuma_live.py, landing.py, merge.py, policy.py, pr_json.py, tools.py | `test_json_types.py` | — |
| `scripts/lib/k8s_context.py` | lib | Ansible's variable semantics, reproduced for the k8s manifest render guard. | imported by k8s_manifests.py, render_context.py | `test_k8s_context.py` | — |
| `scripts/validate/validate_lib/k8s_net_rules.py` | validate | The two semantic rules on rendered manifests that no schema can make. | imported by k8s_manifests.py | — | — |
| `scripts/lib/k8s_roles.py` | lib | Which roles exist under ``ansible/roles/k8s/``, and which of them each reader skips. | imported by catalog_backup.py, catalog_facts.py, deploy_run.py, glance_facts.py, k8s_autodeploy_counts.py, k8s_manifests.py, kuma_live.py, land_shared.py, monitors.py, narrow_broad.py, postflight.py, releases_consumers.py, releases_retired.py, route_facts.py, service_tiers.py, setup_routing.py | — | — |
| `scripts/validate/validate_lib/k8s_schema.py` | validate | Schema validation for a rendered k8s object: the core OpenAPI check and the vendored CRDs. | imported by k8s_manifests.py | — | — |
| `scripts/lib/k8s_yaml.py` | lib | YAML parsing for rendered k8s manifests, and the PVC names they declare and reference. | imported by ansible_jinja_env.py, k8s_manifests.py | `test_k8s_yaml.py` | — |
| `scripts/lib/kubectl.py` | lib | One way to run kubectl from a script, and every call names the cluster it must reach. | imported by _gates_fakes.py, arr.py, b2_ledger.py, cli_parser.py, export_grafana_dashboards.py, gen_infra_map.py, health.py, health_docker.py, k3s_etcd_restore_gates.py, k3s_upgrade_gates.py, kuma_live.py, live.py, longhorn.py, longhorn_cluster.py, longhorn_dr_gates.py, longhorn_upgrade_gates.py, monitors.py, pinned_rotation_gates.py, postflight.py, probe.py, readonly_rbac.py, runbook_gates.py, vip_placement.py | `test_kubectl.py` | — |
| `scripts/diagnostics/probe_lib/kuma_live.py` | diagnostics | What the live Kuma says about itself: its pod's age and the monitors it holds. | imported by monitors.py, postflight.py | — | — |
| `scripts/diagnostics/probe_lib/kuma_table_loop.py` | diagnostics | The two table loops in the static-monitors template, as `parse_declared_monitors` reads them. | imported by monitors.py | — | — |
| `scripts/deploy_tools/land_platform.py` | deploy_tools | Which of a PR's derived tags its own changed paths PROVE are a k3s change. | imported by deploy.py, tools.py | `test_land_platform.py` | — |
| `scripts/deploy_tools/land_reach.py` | deploy_tools | Which hosts a self-applied setup-role change still owes a hand, beyond the tick's own host. | imported by land_tags.py, tools.py | `test_land_reach.py` | — |
| `scripts/deploy_tools/land_rerolls.py` | deploy_tools | Whether a later deploy re-rolled a landing's workloads under its health gate (#3812). | imported by tools.py | `test_land_rerolls.py` | — |
| `scripts/deploy_tools/land_shared.py` | deploy_tools | The shared-role expansion a landing makes, and the #3124 narrowing. | imported by land_tags.py, tools.py | — | — |
| `scripts/deploy_tools/land_tags.py` | deploy_tools | Derive deploy tags from a merged PR's own file list. | imported by _land_fakes.py, classify.py, land_platform.py, landing.py, shared_role_reach.py, tools.py | `test_land_tags.py` | — |
| `scripts/deploy_tools/land_lib/landing.py` | deploy_tools | One PR's landing: the state every phase reads and writes, and the ways it ends. | imported by _land_fakes.py, ci.py, classify.py, deploy.py, health_verdict.py, land.py, merge.py, pipeline.py, policy.py, tick.py | — | — |
| `scripts/diagnostics/probe_lib/landing_blockers.py` | diagnostics | `probe.py landing` — what would stop a landing now, and who else is working the repo. | imported by subcommands.py | — | — |
| `scripts/dev/fanout_lib/launch.py` | dev | Create the worktree, write the brief, start the transient service — spec §3. | imported by place.py, review.py | — | — |
| `scripts/dev/fanout_lib/launch_gates.py` | dev | The refusals `launch` applies to a batch grouping before it spends an ssh connection. | imported by place.py | — | — |
| `scripts/deploy_tools/land_lib/ledger.py` | deploy_tools | What the Landings board reads: one logfmt line per landing, from a Ledger of stamps. | imported by land.py, landing.py | — | — |
| `scripts/lib/facts/lint.py` | lib | Lint for the fact stores: the forms the spec excludes, and the atoms that do not resolve. | imported by fact_status.py, report.py | — | — |
| `scripts/infra_map/live.py` | infra_map | Live state: what the cluster and the Pi report is actually running. | imported by gen_infra_map.py | — | — |
| `scripts/lib/facts/lock.py` | lib | ``docs/facts.lock``: the recorded hashes for every verified CLAUDE.md section. | imported by fact_status.py, lint.py, report.py | — | — |
| `scripts/diagnostics/probe_lib/longhorn.py` | diagnostics | Longhorn's B2 backup objects: what the estate holds and what it costs. | imported by b2_ledger.py, cli_parser.py, subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/longhorn_budget.py` | diagnostics | What one Longhorn retention prune costs B2 in Class C transactions, per weekly shard. | imported by longhorn.py | — | — |
| `scripts/diagnostics/probe_lib/longhorn_cluster.py` | diagnostics | The live cluster objects the B2 reports read: Volume, Backup, PV and BackupTarget. | imported by b2_spend.py, longhorn.py | — | — |
| `scripts/backup/longhorn_reap_logic.py` | backup | Pure decision core shared by the two Longhorn reap-orphan entry points. | imported by longhorn_reap_orphan_backups.py, longhorn_reap_orphan_snapshots.py, longhorn_reap_selectors.py | `test_longhorn_reap_logic.py` | — |
| `scripts/backup/longhorn_reap_selectors.py` | backup | The two operator-selected Backup CR sets: a migrated volume's old chain, and retired seeds. | imported by longhorn_reap_orphan_backups.py | `test_longhorn_reap_selectors.py` | — |
| `scripts/dev/fanout_lib/manifest.py` | dev | The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4. | imported by abandon.py, boundaries.py, clean.py, launch.py, launch_gates.py, place.py, status.py, wait_probe.py | — | — |
| `scripts/deploy_tools/land_lib/merge.py` | deploy_tools | The merge phase: check the PR here, then merge it directly once its CI is green. | imported by pipeline.py | — | — |
| `scripts/diagnostics/probe_lib/metrics.py` | diagnostics | `probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries. | imported by subcommands.py | — | — |
| `scripts/infra_map/model.py` | infra_map | Reconciliation: overlay live state onto the declared skeleton. | imported by gen_infra_map.py | — | — |
| `scripts/diagnostics/probe_lib/monitors.py` | diagnostics | `probe.py monitors` and `probe.py kuma-drift` -- what is down, and what is missing. | imported by postflight.py, subcommands.py | — | — |
| `scripts/deploy_tools/narrow_broad.py` | deploy_tools | Which service tags a deploy-plane change actually reaches, or a refusal to guess. | imported by _narrow_fixtures.py, deploy_tags.py, releases.py, shared_role_callers.py | — | — |
| `scripts/deploy_tools/narrow_containers.py` | deploy_tools | The `containers_list` half of `narrow_broad`: the entry diff, and which hits read the list. | imported by narrow_broad.py | — | — |
| `scripts/deploy_tools/narrow_filters.py` | deploy_tools | What a filter plugin defines, and which grep hits for its filter names are callers (#3843). | imported by narrow_broad.py, narrow_setup.py | — | — |
| `scripts/lib/narrow_git.py` | lib | The reads the two narrowing derivations share: a refusal, a `git show`, and a key diff. | imported by narrow_broad.py, narrow_filters.py, narrow_paths.py, narrow_setup.py, narrow_setup_index.py, narrow_templates.py, shared_role_reach.py | `test_narrow_git.py` | — |
| `scripts/deploy_tools/narrow_paths.py` | deploy_tools | The rules `narrow_broad` keeps outside itself: a path shape, and a role-directory read. | imported by narrow_broad.py, narrow_setup.py | — | — |
| `scripts/deploy_tools/narrow_setup_index.py` | deploy_tools | One setup role's task files, templates and vars, read at a single git ref. | imported by narrow_setup.py | — | — |
| `scripts/deploy_tools/narrow_setup_playbook.py` | deploy_tools | What a playbook and a task list say about tags, without touching git. | imported by narrow_setup.py, narrow_setup_index.py | — | — |
| `scripts/deploy_tools/narrow_templates.py` | deploy_tools | Two importer edges `narrow_broad.template_importers` must not take at face value. | imported by narrow_broad.py, narrow_filters.py | — | — |
| `scripts/diagnostics/probe_lib/obs_api.py` | diagnostics | The Prometheus and Loki query client every caller shares: URL builders, window, transport. | no `__main__` guard; carried by deploy: ansible/roles/k8s/homelab-mcp/tasks/main.yml | — | — |
| `scripts/deploy_tools/land_lib/options.py` | deploy_tools | Everything the command line sets, plus the budgets a test shortens. | imported by _land_fakes.py, handoff.py, land.py, landing.py | — | — |
| `scripts/deploy_tools/land_lib/outcome.py` | deploy_tools | The words a landing ends with: the verdict set, the cause set, the Outcome, and say(). | imported by ci.py, classify.py, deploy.py, health_verdict.py, land.py, land_probe.py, landing.py, ledger.py, merge.py, pipeline.py, policy.py, tick.py | — | — |
| `scripts/diagnostics/probe_lib/pi_plane.py` | diagnostics | `probe.py targets --pi` and `probe.py pi containers` — first-command triage for daniel-pi. | imported by subcommands.py | — | — |
| `scripts/lib/facts/pins.py` | lib | The YAML keys whose value Renovate rewrites, from ``renovate.json`` and its built-in managers. | imported by lint.py | — | — |
| `scripts/deploy_tools/land_lib/pipeline.py` | deploy_tools | The phase order, the step headers, and nothing else. | imported by land.py | — | — |
| `scripts/dev/fanout_lib/place.py` | dev | Place issue-fanout batches on the session host with the most memory headroom. | imported by wait_probe.py | — | — |
| `scripts/dev/fanout_lib/placement.py` | dev | Pure placement over (host, cap, current, live_agents). | imported by place.py, transport.py | — | — |
| `scripts/dev/findings_lib/plans.py` | dev | What `findings.py` decides to do, as gh argv nobody has run yet. | imported by claim_cli.py, findings.py, gh_calls.py | — | — |
| `scripts/deploy_tools/land_lib/policy.py` | deploy_tools | The landing policy a lander unit sets for an agent's PRs, checked before any merge call. | imported by merge.py | — | — |
| `scripts/deploy_tools/land_lib/pr_json.py` | deploy_tools | The fields the lander reads from a PR's JSON, typed once where `gh_json` returns them. | imported by landing.py, policy.py | — | — |
| `scripts/dev/findings_lib/precomputed.py` | dev | What a session working an issue would otherwise rediscover: the deploy plane and the tests. | imported by brief.py, history_cli.py | — | — |
| `scripts/lib/proc_testing.py` | lib | Launch a subprocess from a test, and build the fake binaries it finds on `PATH`. | imported by _deploy_sh_fakes.py, _reap_entrypoint_harness.py, _scratch_pytest.py | `test_proc_testing.py` | — |
| `scripts/dev/fanout_lib/review/processes.py` | dev | The review pipeline's process boundary, and the reap that ends a red phase. | imported by review.py | — | — |
| `scripts/deploy_tools/reach.py` | deploy_tools | Which services a changed-path list reaches, answered in one module (#3660). | imported by deploy_staleness.py, deploy_tags.py, land_platform.py, land_reach.py, land_shared.py, land_tags.py, narrow_broad.py | `test_reach.py` | — |
| `scripts/diagnostics/probe_lib/readonly_rbac.py` | diagnostics | `probe.py readonly-rbac` — is the read-only ServiceAccount still read-only? | imported by subcommands.py | — | — |
| `scripts/dev/fanout_lib/review/red_gate.py` | dev | The red and green gates a `red-green` fan-out batch passes through (#3674). | imported by _review_fakes.py, api.py, hunk_check.py, review.py | — | — |
| `scripts/dev/findings_lib/red_green.py` | dev | Which findings `open` labels `red-green`, so a `--review` fan-out gives them a red phase. | imported by findings.py, hunk_check.py, plans.py, red_gate.py | — | — |
| `scripts/dev/fanout_lib/review/red_tests.py` | dev | The two rules a red test is held to: why it failed, and what a fix may change in its file. | imported by hunk_check.py, red_gate.py | — | — |
| `scripts/lib/facts/relations.py` | lib | The status algebra: the spec's Datalog rules as set comprehensions over finite relations. | imported by fact_status.py, lock.py | — | — |
| `scripts/lib/release_bin_groups.py` | lib | Resolve which source files a `release_bin.yml` group deploys. | imported by cron_targets.py | — | — |
| `scripts/diagnostics/probe_lib/releases.py` | diagnostics | `probe.py releases` -- which commit produced the manifests each k8s service is running. | imported by health.py, land_rerolls.py, subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/releases_consumers.py` | diagnostics | Which services a byte-supplying shared k8s role can actually make stale. | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_diff.py` | diagnostics | The one staleness question a path cannot answer: did this `tasks/` diff move any bytes? | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_format.py` | diagnostics | Renderers for `probe.py releases` -- the table, the cron-facing list and the Kuma push line. | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_render.py` | diagnostics | The render digest, which is `probe.py releases`' primary answer on staleness. | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_retired.py` | diagnostics | Release records for services the inventory no longer declares. | imported by releases.py | — | — |
| `scripts/lib/facts/removed.py` | lib | Identifier-shaped tokens a range of commits removed, and whether the tree still holds them. | imported by evidence.py, lint.py | — | — |
| `scripts/infra_map/render.py` | infra_map | Rendering: turn the reconciled model into one self-contained HTML page. | imported by gen_infra_map.py | — | — |
| `scripts/lib/render_context.py` | lib | The variable context a role template renders with, in Ansible's precedence. | imported by compose_templates.py, k8s_manifests.py, monitors.py, setup_templates.py, shell_templates.py, unit_templates.py | `test_render_context.py` | — |
| `scripts/lib/render_guard.py` | lib | Shared helpers for the render-guard scripts and other Ansible-inventory readers. | imported by ansible_jinja_env.py, catalog_backup.py, catalog_facts.py, compose_templates.py, deploy_tags.py, estate.py, gen_role_glance.py, glance_facts.py, k8s_context.py, k8s_manifests.py, k8s_yaml.py, land_tags.py, monitors.py, narrow_broad.py, narrow_containers.py, render_context.py, render_targets.py, service_tiers.py, setup_templates.py, shared_role_callers.py, shell_templates.py, unit_templates.py | `test_render_guard.py` | — |
| `scripts/lib/renovate_annotations.py` | lib | The `# renovate:` annotations in the roles' defaults, read the way Renovate reads them. | imported by _renovate.py, asset_pins.py | `test_renovate_annotations.py` | — |
| `scripts/lib/repo_paths.py` | lib | The repo path anchors a script under ``scripts/`` reads the Ansible tree through. | imported by _deploy_sh_fakes.py, _inventory_path_rules.py, _render_helper_rules.py, _renovate.py, ansible_inventory.py, asset_pins.py, await_ci.py, build_docs.py, catalog_backup.py, catalog_model.py, classify.py, constants.py, consumers.py, core.py, cron_checks.py, cron_targets.py, crons.py, decisions.py, deploy_detach_notify.py, deploy_playbook.py, deploy_run.py, deploy_tags.py, deploy_under_locks.py, deployer_park.py, docs_provenance.py, estate.py, fact_status.py, fragment_readers.py, fragments_bridge.py, fragments_deploy.py, fragments_hosts.py, fragments_storage.py, freshness.py, gen_doc_fragments.py, gen_gitops_markers.py, gen_hook_settings.py, gen_role_glance.py, gitops_state.py, gitops_view.py, glance_facts.py, grafana_dashboards.py, ha.py, health.py, hosts.py, invocation_sites.py, jinja_bash_collisions.py, jinja_defaults.py, k3s_upgrade_gates.py, k8s_autodeploy_counts.py, k8s_roles.py, k8s_schema.py, kuma_table_loop.py, land_reach.py, land_rerolls.py, land_shared.py, land_tags.py, landing.py, landing_blockers.py, longhorn_budget.py, longhorn_cluster.py, longhorn_dr_gates.py, longhorn_reap_logic.py, longhorn_reap_orphan_backups.py, longhorn_reap_orphan_snapshots.py, longhorn_upgrade_gates.py, monitors.py, narrow_broad.py, narrow_paths.py, narrow_setup.py, networking.py, new_k8s_service.py, options.py, outcome.py, pi_plane.py, pinned_rotation_gates.py, pytest_shard.py, reach.py, refresh_vendored_schemas.py, releases.py, releases_format.py, releases_render.py, render_guard.py, render_targets.py, renovate_branch_sweep.py, report.py, review_metrics.py, root_ignored_files.py, rotation_tools.py, route_facts.py, runbook_gates.py, script_classify.py, scripts.py, secret_bearing_host_paths.py, secrets.py, service_catalog.py, service_tiers.py, setup_gates.py, setup_routing.py, setup_templates.py, shared_role_callers.py, shared_role_reach.py, shell_templates.py, state.py, target.py, tools.py, validate_ha_config.py, worktrees.py | — | — |
| `scripts/lib/facts/report.py` | lib | ``fact_status.py report``: whether ``docs/facts.lock`` pays for itself, read from git and the log. | imported by fact_status.py | — | — |
| `scripts/dev/fanout_lib/review/review.py` | dev | The review pipeline a `launch --review` batch runs in place of a single `claude -p`. | imported by _review_fakes.py, api.py | — | — |
| `scripts/dev/fanout_lib/review/review_land.py` | dev | The review pipeline's landing: `land.sh` run by the pipeline, the model resumed only on need. | imported by review.py | — | — |
| `scripts/dev/fanout_lib/review/review_prompts.py` | dev | What the review pipeline asks each phase it resumes or starts, and the reviewer's schema. | imported by review.py, review_record.py | — | — |
| `scripts/dev/fanout_lib/review/review_record.py` | dev | What one `--review` batch records: its phases, the findings, and the PR comment built from them. | imported by api.py, review.py, review_prompts.py | — | — |
| `scripts/dev/fanout_lib/review_stats.py` | dev | Sum the fan-out review records into the measures that decide whether the red phase stays. | no `__main__` guard; imported by test_fanout_review_stats.py | — | — |
| `scripts/dev/fanout_lib/review_unit.py` | dev | Run a `launch --review` fan-out batch: implement, review, fix, then land. | no `__main__` guard; imported by test_fanout_git_state.py, test_fanout_review.py | — | — |
| `scripts/secrets_mgmt/rotation_tools.py` | secrets_mgmt | Every process boundary `secret_rotation.py` crosses, as one injectable object. | imported by _rotation_fakes.py, consumers.py, git_dates.py, secret_registry.py, secret_rotation.py, secrets.py | `test_rotation_tools.py` | — |
| `scripts/docs/route_facts.py` | docs | Shared route facts for the reference generators. | imported by catalog_facts.py, catalog_render.py, networking.py | `test_route_facts.py` | — |
| `scripts/deploy_tools/runbook_gates.py` | deploy_tools | The runner and the shared verdicts behind every `scripts/deploy_tools/*_gates.py`. | imported by k3s_etcd_restore_gates.py, k3s_upgrade_gates.py, longhorn_dr_gates.py, longhorn_upgrade_gates.py, pinned_rotation_gates.py | — | — |
| `scripts/lib/script_classify.py` | lib | How every first-party script under ``scripts/`` is run, derived from the tree. | imported by scripts.py | — | — |
| `scripts/lib/script_imports.py` | lib | Which scripts under ``scripts/`` import which, read from the source without running it. | imported by script_classify.py | `test_script_imports.py` | — |
| `scripts/secrets_mgmt/secrets_lib/secret_classify.py` | secrets_mgmt | Which rotation tier a secret's NAME puts it in. | imported by secret_registry.py | — | — |
| `scripts/secrets_mgmt/secrets_lib/secret_registry.py` | secrets_mgmt | Pure logic over the rotation registry: seeding, reconciliation, due dates and drift. | imported by secret_rotation.py, secrets.py | — | — |
| `scripts/lib/service_tiers.py` | lib | The sets derived from containers_list ``tier``, for readers outside Ansible. | imported by catalog_backup.py, fragments_storage.py, gen_doc_fragments.py, shed_set.py | — | — |
| `scripts/deploy_tools/setup_gates.py` | deploy_tools | Read a playbook entry's `when:` for one host, the way Ansible would. | imported by land_reach.py, setup_routing.py | — | — |
| `scripts/deploy_tools/setup_role_chains.py` | deploy_tools | Reading a setup role's `tasks/` tree for the `when:` chains a changed file sits behind. | imported by land_reach.py, setup_routing.py | — | — |
| `scripts/dev/shard_weight_gate.py` | dev | The measured shard-weight gate: what CI's `pytest_shard.py --check-durations` step decides. | imported by pytest_shard.py | — | — |
| `scripts/deploy_tools/shared_role_reach.py` | deploy_tools | Which changed SHARED k8s roles move no rendered manifest, so no hand has to apply them. | imported by tools.py | `test_shared_role_reach.py` | — |
| `scripts/diagnostics/probe_lib/shed_set.py` | diagnostics | `probe.py shed-set` — the k8s workloads to scale down when one node is lost. | imported by subcommands.py | — | — |
| `scripts/validate/validate_lib/shell_lint.py` | validate | Render a Jinja-templated shell script, then lint the output with `bash -n` and shellcheck. | imported by shell_templates.py | — | — |
| `scripts/dev/fanout_lib/signing.py` | dev | The signing-key gate: refuse a placement host whose commit signatures GitHub rejects. | imported by place.py, transport.py | — | — |
| `scripts/dev/findings_lib/solo_only.py` | dev | Which issues are solo-only: they cite the fan-out tooling, so no fan-out batch takes them. | imported by findings.py, launch_gates.py | — | — |
| `scripts/secrets_mgmt/sops_io.py` | secrets_mgmt | The push-token shape check, over an already-decrypted mapping. | imported by secret_rotation.py | — | — |
| `scripts/dev/fanout_lib/status.py` | dev | Read every batch on one host in one call, and stop one. | imported by abandon.py, place.py, review.py, review_record.py, wait_probe.py | — | — |
| `scripts/infra_map/style.py` | infra_map | The page's stylesheet, its status vocabulary, and the escape every view calls. | imported by diagram.py, html_views.py, render.py | — | — |
| `scripts/diagnostics/probe_lib/subcommands.py` | diagnostics | The `SUBCOMMANDS` table and the `REGISTRY` built from it: probe.py's dispatch and `--list`. | imported by probe.py | — | — |
| `scripts/dev/fanout_lib/target.py` | dev | Which repo a batch works: its register, its primary checkout and the branch it merges to. | imported by _fanout_fakes.py, _review_fakes.py, abandon.py, brief.py, claim_cli.py, claims.py, clean.py, launch.py, manifest.py, place.py, review.py, review_unit.py, status.py, transport.py | — | — |
| `scripts/deploy_tools/land_lib/tick.py` | deploy_tools | Step 4, the GitOps tick, retried while the unit's own flock gives up. | imported by deploy.py, health_verdict.py, pipeline.py | — | — |
| `scripts/deploy_tools/land_lib/tools.py` | deploy_tools | Every process boundary a landing crosses, as one injectable object. | imported by _land_fakes.py, land.py, landing.py | — | — |
| `scripts/dev/findings_lib/tracked_paths.py` | dev | Resolve a path an issue cites relative to some directory to the tracked file it names. | imported by launch_gates.py, place.py, red_green.py | — | — |
| `scripts/dev/fanout_lib/transport.py` | dev | The transport seam: every process boundary the dispatcher crosses, as one injectable object. | imported by _fanout_fakes.py, abandon.py, claims.py, clean.py, launch.py, place.py, review_unit.py, status.py | — | — |
| `scripts/dev/findings_lib/verify.py` | dev | Verify-by: the prose an issue stores about how to check it, and how `verify` reports it. | imported by findings.py | — | — |
| `scripts/diagnostics/probe_lib/vip_placement.py` | diagnostics | `probe.py vip-placement` — does every ETP=Local VIP have a Ready endpoint on an announcing node? | imported by subcommands.py | — | — |
| `scripts/dev/fanout_lib/wait_probe.py` | dev | The `fanout` source for cc-wait: a fan-out run's batches, read through `fanout.py place status`. | no `__main__` guard; imported by test_fanout_probe.py | — | — |
| `scripts/lib/worktree_owner.py` | lib | Which Unix user's clone a worktree branch belongs to, read from the branch name alone. | imported by claim.py, target.py | — | — |
| `scripts/dev/fanout_lib/review/worktree_reset.py` | dev | The reset that returns a red batch's worktree to one commit. | imported by review.py | — | — |
| `scripts/lib/worktrees.py` | lib | What a Claude session worktree is, and whether it is done with: the worktree library. | imported by _findings_fakes.py, boundaries.py, claim.py, clean.py, place.py, prune_worktrees.py, target.py, transport.py | `test_worktrees.py` | — |
| `scripts/lib/yaml_fast.py` | lib | `yaml.safe_load` backed by libyaml, which parses the same schema an order faster. | imported by asset_pins.py, atoms.py, compose_templates.py, cron_targets.py, crons.py, digest_provable.py, fragment_readers.py, git_dates.py, glance_facts.py, ha_state_checks.py, ha_state_model.py, inventory.py, invocation_sites.py, jinja_defaults.py, k8s_autodeploy_counts.py, k8s_roles.py, kuma_live.py, kuma_table_loop.py, land_reach.py, landing_blockers.py, longhorn_upgrade_gates.py, monitors.py, narrow_git.py, narrow_setup_index.py, narrow_setup_playbook.py, pinned_rotation_gates.py, release_bin_groups.py, render_guard.py, rotation_tools.py, route_facts.py, secret_bearing_host_paths.py, setup_gates.py, setup_role_chains.py, setup_routing.py, setup_templates.py, state.py, validate_ha_config.py | `test_yaml_fast.py` | — |

## Run by hand

33 script(s) — a person runs it.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/validate/asset_pins.py` | validate | Fetch every pinned download in the roles' `defaults/` and check its checksum. | no automated caller in the tree | — | — |
| `scripts/backup/b2_drain.py` | backup | Delete a stranded Longhorn backup prefix directly through the B2 API. | playbook: ansible/prune_backups.yml | `test_b2_drain.py` | — |
| `scripts/deploy_tools/deploy_probe.py` | deploy_tools | The `deploy` source for cc-wait: one `deploy.sh --detach` run's state, read from its log. | no automated caller in the tree | `test_deploy_probe.py` | — |
| `scripts/grafana/export_grafana_dashboards.py` | grafana | Export the *customized* Grafana dashboards from the live DB into code. | no automated caller in the tree | `test_export_grafana_dashboards.py` | — |
| `scripts/dev/fanout_place.py` | dev | Forwarding shim: `fanout_place.py <args>` runs `fanout.py place <args>` (#4346). | clean.py (a person runs it) | — | — |
| `scripts/dev/fanout_probe.py` | dev | Forwarding shim: `fanout_probe.py <args>` runs `fanout.py probe <args>` (#4346). | no automated caller in the tree | `test_fanout_probe.py` | — |
| `scripts/dev/fanout_review.py` | dev | Forwarding shim: `fanout_review.py <args>` runs `fanout.py review <args>` (#4346). | no automated caller in the tree | `test_fanout_review.py` | — |
| `scripts/dev/fanout_review_stats.py` | dev | Forwarding shim: `fanout_review_stats.py <args>` runs `fanout.py stats <args>` (#4346). | no automated caller in the tree | `test_fanout_review_stats.py` | — |
| `scripts/grafana/fetch_grafana_dashboards.py` | grafana | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | no automated caller in the tree | `test_fetch_grafana_dashboards.py` | — |
| `scripts/dev/gen_gitops_markers.py` | dev | Copy the deployer's marker modules into monitor-bridge, the one tree that cannot ship them. | no automated caller in the tree | — | — |
| `scripts/deploy_tools/gitops_tick.sh` | deploy_tools | trigger a GitOps deploy tick by hand and report what it did. | exit_codes.py (a person runs it) | — | [0, 1, 2, 3, 4, 64, 75](#scriptsdeploytoolsgitopsticksh) |
| `scripts/deploy_tools/k3s_etcd_restore_gates.py` | deploy_tools | Run the stop conditions of `docs/k3s-etcd-restore.md` in order, exit code naming the first failure. | no automated caller in the tree | `test_k3s_etcd_restore_gates.py` | — |
| `scripts/deploy_tools/k3s_upgrade_gates.py` | deploy_tools | Run the four stop conditions of `docs/k3s-upgrade.md` in order, exit code naming the first failure. | no automated caller in the tree | `test_k3s_upgrade_gates.py` | — |
| `scripts/deploy_tools/land_probe.py` | deploy_tools | The `land` source for cc-wait: one detached landing's state, read from its log. | no automated caller in the tree | `test_land_probe.py` | — |
| `scripts/deploy_tools/longhorn_dr_gates.py` | deploy_tools | Run the stop conditions of `docs/longhorn-disaster-recovery.md` in order, exit code naming the first failure. | no automated caller in the tree | `test_longhorn_dr_gates.py` | — |
| `scripts/backup/longhorn_reap_orphan_backups.py` | backup | Delete Longhorn Backup objects that no RecurringJob will ever prune. | no automated caller in the tree | — | — |
| `scripts/backup/longhorn_reap_orphan_snapshots.py` | backup | Reap Longhorn Snapshots left stranded by a tier move. | no automated caller in the tree | — | — |
| `scripts/deploy_tools/longhorn_upgrade_gates.py` | deploy_tools | Run the stop conditions of `docs/longhorn-upgrade.md` in order, exit code naming the first failure. | no automated caller in the tree | `test_longhorn_upgrade_gates.py` | — |
| `scripts/dev/new_k8s_service.py` | dev | Scaffold a k3s service role from its name, image and port, instead of copying a sibling. | no automated caller in the tree | `test_new_k8s_service.py` | — |
| `scripts/deploy_tools/pinned_rotation_gates.py` | deploy_tools | Run the stop conditions of the pinned-secret procedure in `docs/secret-rotation.md`, exit code naming the first failure. | no automated caller in the tree | `test_pinned_rotation_gates.py` | — |
| `scripts/diagnostics/postflight.py` | diagnostics | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | playbook: ansible/bring-up.sh | `test_postflight.py` | — |
| `scripts/dev/pr_changed_files.sh` | dev | List the files a pull request changes, and say whether its scoped CI run can be trusted. | no automated caller in the tree | — | — |
| `scripts/validate/refresh_vendored_schemas.py` | validate | Re-download the vendored JSON schemas this repo checks rendered config against. | no automated caller in the tree | `test_refresh_vendored_schemas.py` | — |
| `scripts/dev/renovate_branch_sweep.py` | dev | Census the `renovate/*` branches no open PR and no Dependency Dashboard entry speaks for. | no automated caller in the tree | `test_renovate_branch_sweep.py` | — |
| `scripts/validate/renovate_config.sh` | validate | Validate renovate.json against a freshly-resolved Renovate, riding out npm's publish window. | no automated caller in the tree | — | — |
| `scripts/dev/renovate_rebase.py` | dev | Tick the rebase checkbox in a Renovate PR's body so Renovate refreshes the branch. | no automated caller in the tree | `test_renovate_rebase.py` | — |
| `scripts/dev/review_metrics.py` | dev | Print the /homelab-review outcome trend: false-positive and fix-refusal rates. | no automated caller in the tree | `test_review_metrics.py` | — |
| `scripts/dev/run_as_cron.sh` | dev | run a command in the environment cron actually gives it. | no automated caller in the tree | `test_run_as_cron.py` | — |
| `scripts/secrets_mgmt/secret_bearing_host_paths.py` | secrets_mgmt | Deployed host paths whose content embeds a credential, derived from the tree. | held_hooks.py (a person runs it) | `test_secret_bearing_host_paths.py` | — |
| `scripts/z2m/set_device_option.sh` | z2m | set one Zigbee2MQTT device option over MQTT and confirm it applied. | no automated caller in the tree | `test_set_device_option.py` | — |
| `scripts/dev/split_module.py` | dev | Split a large Python module along its seams: show the references, then move names by spec. | no automated caller in the tree | `test_split_module.py` | — |
| `scripts/diagnostics/ui_login.py` | diagnostics | Mint a Playwright storage-state file holding a logged-in Authelia session. | ui_mcp.sh (a person runs it) | `test_ui_login.py` | — |
| `scripts/diagnostics/ui_mcp.sh` | diagnostics | Launch @playwright/mcp against this homelab's LAN routes. | no automated caller in the tree | — | — |

## Exit codes

5 entry point(s) declare a contract in `scripts/lib/exit_codes.py`, which is where these tables are rendered from. Every other script here exits 0 or non-zero and says nothing more; 64 is a usage error and 75 a temporary failure wherever they appear, because each family names the same spine.


### `scripts/deploy.sh`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `DEPLOY_OK` | deployed, and the play reached PLAY RECAP naming a host. | — |
| 2 | `DEPLOY_TAG_MISS` | a --tags value matched no service in containers_list, so NOTHING was deployed. | --list-services prints every valid value. |
| 3 | `DEPLOY_BROAD` | the change is broad (shared templates, inventory, the setup plane) and maps to no single service, so NOTHING was deployed. | --changed refuses it by design; apply the plane by hand. `deploy_tags.py narrow <old> <new>` prints, read-only, the services the tick would map the range to. |
| 4 | `DEPLOY_STALE` | the tree is behind origin/master, so NOTHING was deployed. | A stale tree renders stale templates and reverts live config while every repo-side check still reads green. Pull first; never --skip-staleness-check. Staleness is decided before --tags is validated, so a stale tree carrying a tag it does not know yet (a new role's first landing) reports this, not a tag miss. |
| 20 | `DEPLOY_PLAYBOOK_FAILED` | the playbook RAN and a task failed, so this is the one deploy exit where changes ARE live — everything applied before the failing task took effect. | Read the PLAY RECAP and the failing TASK. Do not treat it as a tag, staleness or lock refusal, and do not assume a re-run is safe. |
| 64 | `DEPLOY_BAD_FLAGS` | the command line is wrong, so NOTHING was deployed and a retry changes nothing. | Read the usage above; fix the flags rather than re-running. |
| 75 | `DEPLOY_LOCK_BUSY` | a deploy lock stayed busy, so NOTHING was deployed — either the git-tree lock (deploy_locks.TREE_LOCK) or one of this run's own /var/lock/server-deploy-<tag>.lock files. | The GitOps timer or another session holds it. This is a resume point, not a playbook failure — re-run the same command shortly. |
| 76 | `DEPLOY_LOCK_UNAVAILABLE` | flock failed on the lock file ITSELF, so NOTHING was deployed. This is not contention — no deploy holds the lock. | Check that the git-tree lock file (deploy_locks.TREE_LOCK) exists and is writable by this user; retrying alone changes nothing. |
| 77 | `DEPLOY_SNAPSHOT_FAILED` | the snapshot worktree could not be created, so NOTHING was deployed. | The playbook renders from a detached worktree of HEAD under /tmp/homelab-deploy-snapshots; the message above carries the failing command's own stderr (the `fatal:` line), so fix what it names — retrying changes nothing. |
| 78 | `DEPLOY_NO_HOSTS` | the playbook matched NO host, so NOTHING was deployed. | ansible exits 0 for a run where no play matched, so the wrapper reads the PLAY RECAP itself. Read the [WARNING] lines above — an inventory that failed to parse, or a host pattern that matched nothing — fix that, then re-run. |
| 79 | `DEPLOY_LOCK_PLAN_FAILED` | `deploy_locks.py plan` did not print this run's service locks, so the wrapper had nothing to take and NOTHING was deployed. | It never falls back to a lock order of its own. Run `uv run python ansible/roles/setup/gitops_deploy/files/deploy_locks.py plan <tag>` by hand to see why, fix that, then re-run; nothing was held while it ran. |

### `scripts/deploy_tools/land.sh`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `LAND_SETTLED` | deployed and settled, or there was nothing to deploy. | — |
| 1 | `LAND_FAILED` | CI red, blocked by a change needing a hand, the deploy failed, the health gate failed, or the PR was closed without merging, conflicts with master, or its own CI is red. | — |
| 64 | `LAND_BAD_ARGS` | the command line is wrong. | — |
| 75 | `LAND_GAVE_UP` | gave up waiting — a budget elapsed, the deploy lock stayed busy, the tick was skipped for lock contention every time, master merged faster than one tick-and-deploy cycle, or the tick has not yet crossed origin. | — |

### `scripts/deploy_tools/gitops_tick.sh`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `TICK_OK` | the tick ran to completion; read its journal for what it did. A noop, a deferral and a real deploy all complete successfully. | — |
| 1 | `TICK_FAILED` | the unit failed, or it could not be started at all. | gitops-deploy-alert.service has already posted to Discord via OnFailure. An `Interactive authentication required` on the start means the polkit rule is missing: apply it with `initial_setup.yml --tags gitops_deploy`. |
| 2 | `TICK_NOT_INSTALLED` | gitops-deploy.service is not installed on this host. | The deployer runs only where `has_gitops` is true (daniel-box). Run it there. |
| 3 | `TICK_LOCK_CONTENTION` | the tick was skipped for lock contention, so nothing deployed and nothing alerted. | Re-run once the tree-lock holder finishes (docs/deploying.md lists them); `last_run` is untouched, and no alert fires for this. |
| 4 | `TICK_JOINED` | --no-wait joined a run already in flight and started none. That run fetched before this request, so a commit merged since is not in it. | Re-run once it ends, or wait for the timer. |
| 64 | `TICK_BAD_ARGS` | the command line is wrong. | `--wait <seconds>` and `--no-wait` are the only flags. |
| 75 | `TICK_STILL_RUNNING` | the wait budget elapsed and the wrapper stopped watching a run still in flight. | The run itself is fine. Follow it with `journalctl -u gitops-deploy.service`. |

### `scripts/deploy_tools/await_ci.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `CI_GREEN` | every required check on the SHA concluded green. | — |
| 1 | `CI_RED` | a required check concluded red. | — |
| 2 | `CI_DISARMED` | the CI gate is disarmed, so no verdict was formed. | — |
| 75 | `CI_PENDING` | the budget elapsed with checks still running. | — |

### `scripts/deploy_tools/publish_pr.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `PUBLISH_PUBLISHED` | `publish`: pushed and a PR is open. | — |
| 1 | `PUBLISH_STILL_LOCAL` | `publish`: the commit is still local and there is nothing to clean up on origin. `unlanded`: origin was unreadable. | — |
| 2 | `PUBLISH_PUSHED_NO_PR` | `publish`: pushed but no PR was opened. `unlanded`: a PR is open. | — |
| 3 | `UNLANDED_NO_PR` | `unlanded`: the branch is on origin with no PR. | — |

## Usage

21 script(s) document how to invoke themselves. The rest take `--help`, which every catalogued entry point answers with exit 0 (`scripts/lib/tests/test_entry_points_answer_help.py`).


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

### `scripts/deploy_tools/deploy_run.py`

```
deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
deploy.sh --changed [<ref>]          # derive --tags from the diff against <ref>
deploy.sh --at <sha> --tags <svc>    # render a snapshot of <sha>, not HEAD
deploy.sh --check | --dry-run ...    # unlocked, from the working tree
deploy.sh --detach --tags <svc>      # background the playbook once the locks are held
deploy.sh --list-services            # every valid --tags value
```

### `scripts/dev/fanout.py`

```
fanout.py place read
fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--review]
fanout.py place status <run-id>
fanout.py probe [--describe] <run-id>...
fanout.py review --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
fanout.py stats [--dir DIR ...] [--since YYYY-MM-DD] [--json]
```

### `scripts/docs/reference/freshness.py`

```
uv run python scripts/docs/reference/freshness.py --out docs/reference/freshness.md
```

### `scripts/docs/gen_doc_fragments.py`

```
uv run python scripts/docs/gen_doc_fragments.py --out-dir docs/assets/generated/fragments
uv run python scripts/docs/gen_doc_fragments.py --fix  # write, exit 1 if any was stale
```

### `scripts/infra_map/gen_infra_map.py`

```
uv run python scripts/infra_map/gen_infra_map.py                     # default output path
uv run python scripts/infra_map/gen_infra_map.py -o /tmp/map.html
uv run python scripts/infra_map/gen_infra_map.py --no-live           # declared state only
```

### `scripts/docs/gen_role_glance.py`

```
uv run python scripts/docs/gen_role_glance.py          # write every stale block
uv run python scripts/docs/gen_role_glance.py --check  # list stale docs, write nothing
uv run python scripts/docs/gen_role_glance.py --fix    # write, exit 1 if any was stale
```

### `scripts/docs/reference/hosts.py`

```
uv run python scripts/docs/reference/hosts.py --out docs/reference/hosts.md
```

### `scripts/deploy_tools/land.py`

```
land.sh --pr 574 --since <pre-merge-sha>
land.sh --pr 574 --since <sha> --await-merge   # wait for a PR merged some other way
land.sh --pr 574 --arm-merge --await-merge --since <sha>   # arm the merge INSIDE this script
land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand
land.sh --pr 574 --arm-merge --await-merge --detach && cc-wait land 574
# the one-command form: own logfile, shared wait
land.sh --pr 574 --detach --log-dir .fanout && cc-wait land 574 --log-dir .fanout
# put the log somewhere else
```

### `scripts/docs/reference/networking.py`

```
uv run python scripts/docs/reference/networking.py --out docs/reference/networking.md
```

### `scripts/dev/new_k8s_service.py`

```
uv run python scripts/dev/new_k8s_service.py miniflux \
--image ghcr.io/miniflux/miniflux:2.2.16 --port 8080 --authelia one_factor
```

### `scripts/dev/fanout_lib/place.py`

```
fanout.py place read
fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--review]
fanout.py place launch --repo DanielH2018/dotfiles --batch 763
fanout.py place claim --batch 1345,1386 [--batch 1288]
fanout.py place status <run-id>
fanout.py place stop <run-id> [batch]
fanout.py place clean <run-id>
```

### `scripts/dev/fanout_lib/review_stats.py`

```
fanout.py stats [--dir DIR ...] [--since YYYY-MM-DD] [--json]
```

### `scripts/dev/fanout_lib/review_unit.py`

```
fanout.py review --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
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
