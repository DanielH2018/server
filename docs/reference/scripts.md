---
generated_from: scripts/docs/reference/scripts.py
generated_at: 2026-10-30 06:17 UTC
generated_sha: bae292fa8e
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/scripts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scripts

471 first-party script(s) in `scripts/`. Each summary is the script's own module docstring — change the docstring to change this page.

The sections below split them by **how each one is run**, which is derived from the tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a Claude hook, an Ansible task, or an import edge. The *Reached by* column is the evidence, so a wrong answer is a wrong answer about a real file. The filter bar above the first table narrows all four at once: by section, by directory, or by any text in a row. A header click sorts by that column.

!!! note "What this page does not tell you"
    Whether a script is safe to run. The summary is whatever its author wrote, and nothing here judges blast radius. For the ones that run unattended, and which of those change state, see [Scheduled jobs](crons.md).

!!! note "Where the Tests column looks"
    Only for a `test_<name>.py` in the script's `tests/` sibling or beside it, or, for a module inside a `*_lib` package or one of its subpackages, in the `tests/` beside that package, where `test_<family>_<name>.py` also counts: `land_lib/ci.py` is tested by `test_land_ci.py`. A script with an empty cell may still be exercised elsewhere: `gitops_tick.sh` has five tests in `test_gitops_manual_trigger.py`, and a module split out of a facade is run by the facade's suite. The column says where a script's own suite lives, not whether anything reaches it.


## Run automatically, on a schedule

20 script(s) — a cron runs it unattended.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/docs/reference/backlog.py` | docs | Generate docs/reference/backlog.md — the findings Claude filed as GitHub Issues. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/build_docs.py` | docs | Regenerate the reference pages, then build the MkDocs site. | timer: homelab-docs-refresh.service.j2 (via docs-refresh.sh.j2) | `test_build_docs.py` | [0, 1, 2, 3, 64](#scriptsdocsbuilddocspy) |
| `scripts/docs/reference/crons.py` | docs | Generate docs/reference/crons.md — every scheduled job the tree installs. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/reference/decisions.py` | docs | Generate docs/reference/decisions.md — every `# DECIDED:` marker in the tree. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/dev/findings.py` | dev | File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues. | prune_worktrees.py (a cron runs it unattended) | `test_findings.py` | [0, 1, 3, 64](#scriptsdevfindingspy) |
| `scripts/docs/reference/freshness.py` | docs | Generates docs/reference/freshness.md, ranking hand-written pages by source staleness. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/gen_doc_fragments.py` | docs | Generate the fact tables the hand-written docs transclude. | build_docs.py (a cron runs it unattended) | `test_gen_doc_fragments.py` | — |
| `scripts/infra_map/gen_infra_map.py` | infra_map | Render a self-contained HTML map of the homelab infrastructure. | timer: homelab-infra-map.service.j2 | `test_gen_infra_map.py` | [0, 2, 64](#scriptsinframapgeninframappy) |
| `scripts/docs/reference/hosts.py` | docs | Generate docs/reference/hosts.md — the three hosts and what each one is. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/dev/k8s_autodeploy_counts.py` | dev | Print the k8s auto-deploy eligible and denylist counts, measured off the tree. | gen_doc_fragments.py (a cron runs it unattended) | — | — |
| `scripts/docs/reference/networking.py` | docs | Generate docs/reference/networking.md — what is routed, and what fronts it. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/diagnostics/probe.py` | diagnostics | Read-only homelab diagnostics. | cron: B2 deletion accounting | `test_probe.py` | [0, 1, 64, 75](#scriptsdiagnosticsprobepy) |
| `scripts/dev/prune_worktrees.py` | dev | Report and remove Claude session worktrees under .claude/worktrees/ that are done with. | timer: homelab-worktree-sweep.service.j2 (via worktree-sweep.sh.j2) | `test_prune_worktrees.py` | — |
| `scripts/deploy_tools/publish_pr.py` | deploy_tools | Publish a cron's local commit as a pull request, and start the landing that merges it. | timer: homelab-docs-refresh.service.j2 (via docs-refresh.sh.j2) | `test_publish_pr.py` | [0, 1, 2, 3, 4](#scriptsdeploytoolspublishprpy) |
| `scripts/dev/pytest_shard.py` | dev | Split the pytest suite into N fixed shards of whole test modules, for CI's matrix. | timer: homelab-docs-refresh.service.j2 (via docs-refresh.sh.j2) | — | — |
| `scripts/docs/reference/scripts.py` | docs | Generate docs/reference/scripts.md — every first-party script and what it is for. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/secrets_mgmt/secret_rotation.py` | secrets_mgmt | Secret rotation registry: audit + staggered rotation for ansible/vars/secrets.yml. | timer: homelab-secret-rotation-audit.service.j2 (via secret-rotation-audit.sh.j2) | `test_secret_rotation.py` | [0, 1, 3, 64](#scriptssecretsmgmtsecretrotationpy) |
| `scripts/docs/reference/secrets.py` | docs | Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value. | build_docs.py (a cron runs it unattended) | — | — |
| `scripts/docs/service_catalog.py` | docs | Generate a single HTML page answering "what runs in this homelab". | build_docs.py (a cron runs it unattended) | `test_service_catalog.py` | — |
| `scripts/docs/reference/state.py` | docs | Generates docs/reference/state.md, "State of the lab" -- one row per autonomous loop. | build_docs.py (a cron runs it unattended) | — | — |

## Run automatically, on a commit, CI run, deploy or session

52 script(s) — every commit, CI run, deploy or Claude session runs it.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/validate/alert_rules.py` | validate | Check the alert plane's inputs and loki-homelab's Alloy config with the tools that load them. | prek hook (every commit) | — | — |
| `scripts/validate/alert_tools.sh` | validate | Provision promtool, amtool, pint, lokitool and alloy, then run one of them. | alert_rules.py (every commit, CI run, deploy or Claude session runs it) | — | — |
| `scripts/deploy_tools/bootstrap_drill.py` | deploy_tools | Fixture bundle and verdicts for the bootstrap drill (D5 step 28, `bootstrap-drill.yml`). | CI: bootstrap-drill.yml | `test_bootstrap_drill.py` | — |
| `scripts/deploy_tools/ci_images.py` | deploy_tools | The promote workflow's `images` job: push each rendered image context to GHCR once (D5.4a2). | CI: promote.yml | `test_ci_images.py` | — |
| `scripts/validate/cluster_pins.py` | validate | Refuse a PR that moves a forward-only cluster pin the wrong way (H1.1f). | CI: ci.yml | — | — |
| `scripts/secrets_mgmt/cluster_vars.py` | secrets_mgmt | cluster_vars: keep the cluster's SOPS files in step with ansible/vars/secrets.yml (slice D3). | prek hook (every commit) | — | — |
| `scripts/validate/compose_templates.py` | validate | Render every Pi Compose service's compose.yaml.j2 and assert it parses as YAML. | prek hook (every commit) | — | — |
| `scripts/validate/cross_plane_order.py` | validate | Refuse a PR whose change spans both deploy planes along a dependency edge (D4.4). | CI: ci.yml | — | — |
| `scripts/renderer/deletion_drill.py` | renderer | Fixture bundles and verdicts for the Flux deletion drill (D6.11, `flux-deletion-drill.yml`). | CI: flux-deletion-drill.yml | `test_deletion_drill.py` | — |
| `scripts/deploy.sh` | (top level) | the entry point every doc, skill, hook and consumer names; it execs deploy_cli.py. | deploy: ansible/roles/k8s/monitor-bridge/files/gitops_markers.py | — | [0, 2, 3, 4, 5, 20, 64, 75, 76, 77, 78, 79](#scriptsdeploysh) |
| `scripts/deploy_tools/deploy_cli.py` | deploy_tools | The interactive deploy and its two helpers: run a deploy, map tags, read a detached run. | every deploy (deploy.sh) | — | — |
| `scripts/deploy_tools/deploy_tags.py` | deploy_tools | Forwarding shim: `deploy_tags.py <args>` runs `deploy_cli.py tags <args>` (#4347). | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py | `test_deploy_tags.py` | — |
| `scripts/deploy_tools/deploy_lib/detach_notify.py` | deploy_tools | Post-deploy notifier for `scripts/deploy.sh --detach`. | deploy_cli.py (every commit, CI run, deploy or Claude session runs it) | `test_deploy_detach_notify.py` | — |
| `scripts/deploy_tools/digest_covers.py` | deploy_tools | Whether a role's render digest covers everything a range changed for it (#4781). | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py | `test_digest_covers.py` | — |
| `scripts/deploy_tools/digest_provable.py` | deploy_tools | Which k8s roles act ONLY through the bytes their render digest covers. | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py | `test_digest_provable.py` | — |
| `scripts/backup/etcd_restore_drill.sh` | backup | prove an off-box etcd snapshot actually restores, without an outage. | deploy: ansible/roles/setup/hypervisor/tasks/etcd_drill.yml | — | — |
| `scripts/dev/fact_status.py` | dev | Status, verification and lint for the fact stores — the one entry point. | prek hook (every commit) | — | [0, 1, 64, 2](#scriptsdevfactstatuspy) |
| `scripts/dev/fanout.py` | dev | The issue-fanout tooling: place batches, wait on them, run a review unit, sum the reviews. | deploy: ansible/roles/setup/claude_code/templates/claude-fanout-server-lander.service.j2 | — | — |
| `scripts/dev/gen_hook_settings.py` | dev | Render the `hooks` key of `.claude/settings.json` from the hook files' own declarations. | prek hook (every commit) | `test_gen_hook_settings.py` | — |
| `scripts/docs/gen_role_glance.py` | docs | Generate the mechanical half of every role's `## At a glance` block, in place. | prek hook (every commit) | `test_gen_role_glance.py` | — |
| `scripts/deploy_tools/gitops_state.py` | deploy_tools | Operate on the GitOps deployer's own state markers, from the deploy host's shell. | deploy: ansible/roles/k8s/monitor-bridge/files/gitops_markers.py | `test_gitops_state.py` | — |
| `scripts/validate/grafana_dashboard_uids.py` | validate | Validate that every provisioned Grafana dashboard's datasource uid resolves to a real one. | prek hook (every commit) | — | — |
| `scripts/renderer/homelab_render.py` | renderer | homelab-render: render the k8s manifests and the host packages from the git tree alone. | CI: bootstrap-drill.yml | — | [0, 1, 2, 64](#scriptsrendererhomelabrenderpy) |
| `scripts/validate/host_package_cases.py` | validate | Run the host_packages job's container cases: the updater against real apt, dpkg and systemd. | CI: ci.yml | — | — |
| `scripts/validate/host_packages.py` | validate | Render, diff and lint the host packages: the checks behind CI's `host_packages` job. | CI: ci.yml | — | — |
| `scripts/validate/jinja_bash_collisions.py` | validate | Flag bash's `${#var}` length expansion in a Jinja template, before anything renders it. | prek hook (every commit) | — | — |
| `scripts/validate/k3s_binary_sum.py` | validate | Refuse a PR that moves `k3s_version` and leaves `k3s_binary_sha256_amd64` unchanged (H6.3e). | CI: ci.yml | — | — |
| `scripts/validate/k8s_manifests.py` | validate | Render every k8s manifest template with stubbed vars and assert each parses as valid YAML. | prek hook (every commit) | — | — |
| `scripts/deploy_tools/land.py` | deploy_tools | Follow a merged PR through to a verified deploy, in one invocation. | land.sh (every commit, CI run, deploy or Claude session runs it) | — | — |
| `scripts/deploy_tools/land.sh` | deploy_tools | the entry point every doc, skill and hook names; it execs land.py beside it. | Claude hook: fanout-stop.py | — | [0, 1, 64, 75](#scriptsdeploytoolslandsh) |
| `scripts/deploy_tools/land_lib/land_tags.py` | deploy_tools | Derive deploy tags from a merged PR's own file list. | setup_reach_hosts.py (every commit, CI run, deploy or Claude session runs it) | `test_land_tags.py` | — |
| `scripts/deploy_tools/narrow_setup.py` | deploy_tools | Which narrow `--tags` value a setup-role change needs, or a refusal to guess. | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py | `test_narrow_setup.py` | [0, 1, 5](#scriptsdeploytoolsnarrowsetuppy) |
| `scripts/dev/pipeline_timeline.py` | dev | Join each merged PR's stage timestamps into one row, and keep a dated snapshot of them. | deploy: ansible/roles/setup/claude_code/templates/claude-pipeline-timeline.service.j2 | `test_pipeline_timeline.py` | — |
| `scripts/deploy_tools/promote.py` | deploy_tools | Decide which published master commit the cluster's `prod` tag moves to, wait on it, prune. | CI: promote.yml | — | — |
| `scripts/deploy_tools/prune_releases.py` | deploy_tools | Remove old host-script release directories, never the one in use. | deploy: ansible/roles/setup/common/tasks/release_bin.yml | `test_prune_releases.py` | — |
| `scripts/deploy_tools/render_targets.py` | deploy_tools | The k8s services a render-record run renders on one host, derived from the tree. | deploy: ansible/roles/setup/render_records/files/render_records.py | `test_render_targets.py` | — |
| `scripts/validate/root_ignored_files.py` | validate | Flag a root-level file that `.gitignore`'s `/*` rule hides from git. | prek hook (every commit) | — | — |
| `scripts/deploy_tools/secret_setup_readers.py` | deploy_tools | Which setup-role files read a `secrets.yml` key a range moved (#5436). | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_setup_roles.py | — | — |
| `scripts/deploy_tools/setup_reach_hosts.py` | deploy_tools | Which hosts beyond the tick's own a setup-role change over a range still reaches (#6510). | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_setup_roles.py | `test_setup_reach_hosts.py` | — |
| `scripts/deploy_tools/setup_routing.py` | deploy_tools | Which playbook and `--tags` value apply each setup role, and whether the tick's host runs it. | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_setup_roles.py | `test_setup_routing.py` | — |
| `scripts/validate/setup_templates.py` | validate | Render every setup-plane Jinja template and fail on a variable nothing defines. | prek hook (every commit) | — | — |
| `scripts/renderer/shadow_diff.py` | renderer | Shadow-render tooling beside `homelab-render`: the stub file, and the comparison of two renders. | CI: ci.yml | — | [0, 1, 2, 64](#scriptsrenderershadowdiffpy) |
| `scripts/deploy_tools/shared_role_callers.py` | deploy_tools | Which deploy tags run a shared k8s role, record or no record. | deploy: ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py | `test_shared_role_callers.py` | — |
| `scripts/validate/shell_templates.py` | validate | Render every Jinja-templated shell script under ansible/roles/ and hosts/components/, then lint. | prek hook (every commit) | — | — |
| `scripts/dev/smoke_extract.py` | dev | Extract newly-added container image references from a unified git diff. | CI: image-smoke.yml | `test_smoke_extract.py` | — |
| `scripts/deploy_tools/deploy_lib/staleness.py` | deploy_tools | Refuse a deploy from a git tree that is behind origin/master. | deploy_cli.py (every commit, CI run, deploy or Claude session runs it) | `test_deploy_staleness.py` | — |
| `scripts/dev/takeover_census.py` | dev | List the managed fields a Flux takeover strips or leaves behind, from two JSON inputs (H1.1j). | deploy: ansible/roles/setup/claude_code/files/homelab-takeover-check.sh | `test_takeover_census.py` | — |
| `scripts/dev/tested_tree.py` | dev | Say whether a master push carries a tree that a green PR run already tested. | CI: ci.yml | `test_tested_tree.py` | — |
| `scripts/dev/tighten_ratchets.py` | dev | Lower every ratchet allowlist entry to what its file is today. | prek hook (every commit) | — | — |
| `scripts/validate/unit_templates.py` | validate | Render every systemd unit under ansible/roles/ and hosts/, and verify the output. | prek hook (every commit) | — | — |
| `scripts/validate/vale.sh` | validate | Provision the pinned Google style package, then run Vale over the files prek hands us. | prek hook (every commit) | — | — |
| `scripts/home_assistant/validate_ha_config.py` | home_assistant | Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency. | prek hook (every commit) | `test_validate_ha_config.py` | — |

## Imported, never run on their own

364 script(s) — imported by another script — not an entry point.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/dev/fanout_lib/abandon.py` | dev | Stop or abandon fan-out batches: `fanout.py place stop` and `fanout.py place abandon`. | imported by place.py | `test_fanout_abandon.py` | — |
| `scripts/validate/alert_conventions.py` | validate | Hold the observability role's Prometheus rule files to the conventions promtool cannot check. | imported by alert_rules.py | — | — |
| `scripts/diagnostics/probe_lib/alerts.py` | diagnostics | `probe.py alerts` -- DOWN history reconstructed from Loki, since Kuma keeps only current state. | imported by subcommands.py | `test_probe_alerts.py` | — |
| `scripts/lib/ansible_inventory.py` | lib | The one reading of ``containers_list`` and ``hosts.ini`` every script under ``scripts/`` shares. | imported by _live_read_census.py, _render_audit.py, applied.py, brief.py, bundle_census.py, cluster.py, compose_templates.py, constants.py, consumers.py, containers.py, context.py, core.py, cross_plane_order.py, diagram.py, estate.py, gen_doc_fragments.py, gen_role_glance.py, health_checkout.py, health_docker.py, images.py, inventory.py, k8s_manifests.py, k8s_roles.py, landing_blockers.py, launch_gates.py, lint.py, new_k8s_service.py, order_only.py, owners.py, placeholders.py, render_guard.py, setup_gates.py, shadow_diff.py | `test_ansible_inventory.py` | — |
| `scripts/lib/ansible_jinja_env.py` | lib | The Jinja environment every render guard uses, carrying ansible-core's own filters. | imported by _live_read_jinja.py, applied_facts.py, applied_tasks.py, cluster.py, compose_templates.py, digest_covers.py, environment.py, homelab_render.py, host_sources.py, k8s_context.py, k8s_manifests.py, setup_census_items.py, setup_templates.py, shell_lint.py, shell_templates.py, unit_templates.py | `test_ansible_jinja_env.py` | — |
| `scripts/deploy_tools/deploy_lib/ansible_tmp.py` | deploy_tools | The temp directories ansible-core leaves under `~/.ansible/tmp`: this run's, and stale ones. | imported by run.py | — | — |
| `scripts/dev/fanout_lib/review/api.py` | dev | The review pipeline's public surface: what code outside `fanout_lib/review/` may import. | imported by autoland.py, place.py, review_stats.py, review_unit.py | — | — |
| `scripts/diagnostics/probe_lib/applied.py` | diagnostics | `probe.py applied <role>` — whether a setup role's files on its hosts match the tree. | imported by cli_parser.py, subcommands.py | `test_probe_applied.py` | — |
| `scripts/diagnostics/probe_lib/applied_facts.py` | diagnostics | Which names `probe.py applied`'s walk can read statically, and the values it reads for them. | imported by applied.py, applied_tasks.py | `test_probe_applied_facts.py` | — |
| `scripts/diagnostics/probe_lib/applied_hosts.py` | diagnostics | Which task files of a setup role `probe.py applied` walks on each host. | imported by applied.py | `test_probe_applied_hosts.py` | — |
| `scripts/diagnostics/probe_lib/applied_tasks.py` | diagnostics | The files one setup role's tasks write on one host, as `probe.py applied` compares them. | imported by applied.py | — | — |
| `scripts/diagnostics/probe_lib/arr.py` | diagnostics | `probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr. | imported by cli_parser.py, postflight.py, subcommands.py | — | — |
| `scripts/deploy_tools/artifact_retention.py` | deploy_tools | Choose which versions of the manifest artifact `promote.py prune` deletes. | imported by pin_gate.py, pin_reads.py, prune.py, reads.py, reset.py, supersede.py, verify_promotion.py, wait_reads.py | — | — |
| `scripts/validate/asset_pins.py` | validate | Fetch every pinned download in the roles' `defaults/` and check its checksum. | imported by vendor_manifest.py | — | — |
| `scripts/lib/facts/atoms.py` | lib | Resolve a citation against a checkout and hash the thing it names. | imported by evidence.py, lint.py, lock.py, notice.py | — | — |
| `scripts/deploy_tools/await_ci.py` | deploy_tools | Wait for master CI to reach a verdict on one SHA. | imported by merge.py, tools.py | `test_await_ci.py` | [0, 1, 2, 4, 75](#scriptsdeploytoolsawaitcipy) |
| `scripts/lib/b2.py` | lib | One Backblaze B2 native-API session: authorize, list a prefix, delete a version. | imported by b2_api.py, b2_drain.py | `test_b2.py` | — |
| `scripts/diagnostics/probe_lib/b2_api.py` | diagnostics | The Longhorn listing from B2 and its parser. | imported by longhorn.py | — | — |
| `scripts/diagnostics/probe_lib/b2_ledger.py` | diagnostics | The B2 spend ledger: what maintenance tools spent, since B2 publishes no usage API. | imported by longhorn.py, subcommands.py | `test_probe_b2_ledger.py` | — |
| `scripts/diagnostics/probe_lib/b2_spend.py` | diagnostics | The `b2-spend` report: measured Class B backup spend, per volume and per backup target. | imported by b2_ledger.py | — | — |
| `scripts/backup/longhorn_reap_lib/backups.py` | backup | Delete Longhorn Backup objects that no RecurringJob will ever prune. | imported by longhorn_reap.py | — | — |
| `scripts/dev/fanout_lib/review/base_check.py` | dev | Which of a PR's new tests still pass with its code changes taken out. | imported by _review_fakes.py, hunk_check.py, review.py | `test_fanout_base_check.py` | — |
| `scripts/dev/fanout_lib/batch_holds.py` | dev | Which claimed issues a still-running fan-out batch is working, read from the run manifests. | imported by _findings_fakes.py, boundaries.py, claim.py | `test_fanout_batch_holds.py` | — |
| `scripts/validate/validate_lib/bootstrap_case.py` | validate | The container cases for `hosts/bootstrap-host.sh`, against real apt, dpkg and systemd (H7.15a). | imported by bootstrap_repo_case.py, host_package_cases.py | `test_bootstrap_case.py` | — |
| `scripts/validate/validate_lib/bootstrap_repo_case.py` | validate | The container case for `hosts/bootstrap-host.sh`'s repository path, against real apt (#6245). | imported by host_package_cases.py | — | — |
| `scripts/dev/findings_lib/boundaries.py` | dev | Every process boundary `findings.py` crosses, as one injectable object. | imported by _findings_fakes.py, claim_cli.py, export_cli.py, findings.py, gh_calls.py, history_cli.py, target.py | — | — |
| `scripts/dev/fanout_lib/branch_pr.py` | dev | Which PR is a batch's own: the one GitHub holds for its branch, never the first URL in its text. | imported by review.py, status.py, transport.py | `test_fanout_branch_pr.py` | — |
| `scripts/dev/fanout_lib/brief.py` | dev | The brief a headless fan-out agent reads on stdin. | imported by _review_fakes.py, claims.py, launch_gates.py, notify.py, place.py, red_gate.py, review.py, review_ci.py, review_prompts.py, review_record.py, status.py, transport.py | `test_fanout_brief.py` | — |
| `scripts/deploy_tools/narrow_lib/broad.py` | deploy_tools | Which service tags a deploy-plane change actually reaches, or a refusal to guess. | imported by _narrow_fixtures.py, digest_covers.py, releases.py, secret_setup_readers.py, shared_role_callers.py, tags.py | — | — |
| `scripts/diagnostics/probe_lib/builder.py` | diagnostics | `probe.py builder` — the host-package builder's state, its recent builds and its failures. | imported by cli_parser.py, subcommands.py | `test_probe_builder.py` | — |
| `scripts/renderer/renderer_lib/bundle.py` | renderer | The publish bundle beside `DIR/cluster/`: `DIR/flux/` and `DIR/digests.json` (D2.6b). | imported by _bundle_fixtures.py, _job_census.py, _land_wait_fakes.py, bundle_census.py, bundle_gates.py, cluster.py, cronjob_gate.py, cross_plane_order.py, deletion_drill.py, homelab_render.py, images.py, object_loss.py, prepull.py, promotion.py, snapshot.py, soak.py, sops_entries.py | `test_renderer_bundle.py` | — |
| `scripts/deploy_tools/bundle_census.py` | deploy_tools | `promote.py check-bundle`'s census: the bundle's `flux/` holds exactly what its inputs allow. | imported by promote.py | `test_bundle_census.py` | — |
| `scripts/deploy_tools/bundle_gates.py` | deploy_tools | `promote.py check-bundle`'s three publish gates: source, host and no-stub. | imported by bundle_census.py | `test_bundle_gates.py` | — |
| `scripts/renderer/renderer_lib/bundle_readme.py` | renderer | The bundle's root `README.md`: a static note on the layout `bundle.py` writes. | imported by bundle.py | — | — |
| `scripts/docs/catalog_lib/catalog_backup.py` | docs | Longhorn backup tier and GitOps auto-deploy eligibility for one ``containers_list`` entry. | imported by gen_role_glance.py, service_catalog.py | `test_catalog_backup.py` | — |
| `scripts/docs/catalog_lib/catalog_facts.py` | docs | Route and auth-tier derivations for one ``containers_list`` entry. | imported by gen_role_glance.py, service_catalog.py | — | — |
| `scripts/docs/catalog_lib/catalog_model.py` | docs | The service catalogue's row type, its "cannot be derived" marker, and its path anchors. | imported by catalog_backup.py, catalog_facts.py, catalog_render.py, gen_role_glance.py, service_catalog.py | — | — |
| `scripts/docs/catalog_lib/catalog_render.py` | docs | The two renderings of the service catalogue: the MkDocs page and the standalone HTML. | imported by service_catalog.py | — | — |
| `scripts/deploy_tools/chained_timeout.py` | deploy_tools | How long `promote.py verify` waits: the chained timeout (D4.3), capped at VERIFY_CAP_S. | imported by _land_wait_fakes.py, soak.py, storage_rollout.py, verify_promotion.py, wait.py, wait_reads.py | `test_chained_timeout.py` | — |
| `scripts/deploy_tools/changed_set.py` | deploy_tools | The changed set: which Kustomizations a commit's bundle changes, from two `digests.json` files. | imported by chained_timeout.py, cross_plane_order.py, forward_only.py, promote.py, stateful.py, supersede.py, verify_promotion.py, wait.py | `test_changed_set.py` | — |
| `scripts/deploy_tools/land_lib/ci.py` | deploy_tools | Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose. | imported by deploy.py, pipeline.py | `test_land_ci.py` | — |
| `scripts/deploy_tools/promote_lib/ci_contexts.py` | deploy_tools | The CI contexts a promotion requires, read from the checkout, and `promote.py ci-verdict`. | imported by promote.py, reads.py | `test_promote_ci_contexts.py` | — |
| `scripts/lib/facts/citations.py` | lib | The closed citation grammar for the two fact stores. | imported by atoms.py, evidence.py, lint.py, lock.py, notice.py, report.py | — | — |
| `scripts/dev/findings_lib/claim.py` | dev | Whether a claim on an issue is still live, decided from the worktree that holds it. | imported by claim_cli.py, findings.py | — | — |
| `scripts/dev/findings_lib/claim_cli.py` | dev | The four claim subcommands: `claim`, `release`, `claims` and `reap`. | imported by findings.py | `test_findings_claim_cli.py` | — |
| `scripts/dev/fanout_lib/claims.py` | dev | The claims a fan-out takes under the orchestrator's own branch, and the batch specs they cover. | imported by place.py | — | — |
| `scripts/deploy_tools/land_lib/classify.py` | deploy_tools | Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait. | imported by pipeline.py | `test_land_classify.py` | — |
| `scripts/dev/fanout_lib/clean.py` | dev | Remove a finished batch's worktree, by lib.worktrees' content check — spec §4. | imported by abandon.py, place.py | `test_fanout_clean.py` | — |
| `scripts/dev/findings_lib/cli.py` | dev | The argparse construction for `findings.py`: every subparser, no boundary calls. | imported by findings.py | — | — |
| `scripts/lib/cli_help.py` | lib | The `--help` contract every entry point under `scripts/` answers. | imported by abandon.py, alert_rules.py, asset_pins.py, autoland.py, await_ci.py, b2_drain.py, backlog.py, backups.py, bootstrap_drill.py, build_docs.py, ci_contexts.py, ci_images.py, claims.py, cli.py, cli_parser.py, cluster_pins.py, cluster_vars.py, compose_templates.py, crons.py, cross_plane_order.py, cross_plane_replay.py, decisions.py, deletion_drill.py, detach_notify.py, detach_probe.py, digest_covers.py, digest_provable.py, export_grafana_dashboards.py, fact_status.py, fanout.py, fetch_grafana_dashboards.py, flux_wait.py, freshness.py, gate_runner.py, gen_doc_fragments.py, gen_gitops_markers.py, gen_hook_settings.py, gen_infra_map.py, gen_role_glance.py, gitops_state.py, grafana_dashboard_uids.py, ha_state_model.py, headroom_probe.py, homelab_render.py, host_package_cases.py, host_packages.py, host_secrets.py, hosts.py, jinja_bash_collisions.py, k3s_binary_sum.py, k8s_autodeploy_counts.py, k8s_manifests.py, label_worktree.py, land_probe.py, land_tags.py, land_tags_cli.py, launch_gates.py, longhorn_reap.py, messages.py, narrow_setup.py, networking.py, new_k8s_service.py, notify.py, options.py, pipeline_timeline.py, place.py, postflight.py, promote.py, prune_worktrees.py, publish_pr.py, pytest_shard.py, red_gate.py, refresh_vendored_schemas.py, render_context.py, render_targets.py, renovate_branch_sweep.py, renovate_rebase.py, review_metrics.py, review_stats.py, review_unit.py, root_ignored_files.py, rotation_tools.py, run.py, runbook_gates.py, scripts.py, secret_bearing_host_paths.py, secret_rotation.py, secret_setup_readers.py, secrets.py, service_catalog.py, setup_census.py, setup_reach_hosts.py, setup_role_chains.py, setup_routing.py, setup_templates.py, shadow_diff.py, shared_role_callers.py, shell_templates.py, smoke_extract.py, snapshots.py, split_module.py, staleness.py, standalone_map.py, state.py, stateful.py, tags.py, takeover_census.py, tested_tree.py, tighten_ratchets.py, ui_login.py, unit_templates.py, validate_ha_config.py, vendor_manifest.py, verify_promotion.py, wait_probe.py, worktrees.py | — | — |
| `scripts/diagnostics/probe_lib/cli_parser.py` | diagnostics | probe.py's argparse surface: every subparser, plus the `cert` openssl stage builder. | imported by curl_pipeline.py, probe.py | — | — |
| `scripts/lib/cli_registry.py` | lib | A small named-entry registry shared by this repo's CLI dispatchers. | imported by runbook_gates.py, subcommands.py | `test_cli_registry.py` | — |
| `scripts/renderer/renderer_lib/cluster.py` | renderer | Render k8s roles into C1's cluster layout: `DIR/cluster/<tag>/` and its `kustomization.yaml`. | imported by _live_read_census.py, cluster_vars.py, cronjob_gate.py, cross_plane_order.py, deletion_drill.py, flux_probe.py, homelab_render.py, host.py, host_arms.py, host_compose.py, host_manifest.py, host_outputs.py, host_sources.py, host_tools.py, host_write.py, images.py, placeholders.py, prepull.py, prune_label.py, roundtrip.py, shadow.py, shadow_diff.py, snapshot.py, sops_entries.py, standalone_map.py | — | — |
| `scripts/secrets_mgmt/cluster_vars_check.py` | secrets_mgmt | The assertions of `cluster_vars.py check`, over file text alone (target slice D3). | imported by _cluster_vars_fixture.py, _rotation_fakes.py, _sops_type_proof.py, cluster_vars.py, host_secrets.py, restart.py, rotation_tools.py, sops_entries.py | `test_cluster_vars_check.py` | — |
| `scripts/deploy_tools/land_lib/compose_wait.py` | deploy_tools | H5.3's compose condition for the landing's host wait. | imported by host_wait.py | `test_land_compose_wait.py` | — |
| `scripts/validate/validate_lib/conffile_case.py` | validate | The container case for a line-arm edit in a conffile another package owns (#6088). | imported by host_package_cases.py | `test_conffile_case.py` | — |
| `scripts/infra_map/constants.py` | infra_map | Constants shared by the infra-map inventory, live, model and render stages. | imported by gen_infra_map.py, inventory.py, live.py, model.py, render.py | — | — |
| `scripts/secrets_mgmt/consumers.py` | secrets_mgmt | Who holds a copy of a secret, by two deliberately different mechanisms. | imported by secret_keys.py, secret_rotation.py | — | — |
| `scripts/deploy_tools/narrow_lib/containers.py` | deploy_tools | The `containers_list` half of `narrow_lib/broad.py`: the entry diff, and which hits read it. | imported by broad.py | — | — |
| `scripts/renderer/renderer_lib/context.py` | renderer | The variables one k8s role's templates see in the deploy play, built from the git tree alone. | imported by cluster.py, homelab_render.py | `test_renderer_context.py` | — |
| `scripts/deploy_tools/promote_lib/controller_rollout.py` | deploy_tools | `verify` during a Flux hop: when the controller's own replacement makes a failure provisional. | imported by verify_promotion.py | — | — |
| `scripts/diagnostics/probe_lib/core.py` | diagnostics | Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations. | imported by _alert_fixtures.py, alerts.py, applied.py, applied_hosts.py, arr.py, b2_api.py, b2_ledger.py, builder.py, cli_parser.py, curl_pipeline.py, fetch_grafana_dashboards.py, firing_ingress.py, gitops_view.py, gitops_why.py, ha.py, ha_state_model.py, health.py, health_alerts.py, health_backup_targets.py, health_docker.py, health_plumbing.py, host_reader.py, kuma_live.py, lander.py, longhorn.py, longhorn_cluster.py, metrics.py, monitors.py, pi_plane.py, postflight.py, probe.py, readonly_rbac.py, shed_set.py, takeover.py, ui_login.py | — | — |
| `scripts/validate/validate_lib/cron_checks.py` | validate | The three cron-environment rules a rendered shell template must satisfy. | imported by shell_templates.py | — | — |
| `scripts/validate/validate_lib/cron_targets.py` | validate | Resolve which shell templates under `ansible/roles/` are scheduled as cron `job:` targets. | imported by cron_checks.py, shell_templates.py | — | — |
| `scripts/renderer/renderer_lib/cronjob_gate.py` | renderer | The gate Job in `<tag>-verify`: one run of a CronJob per role revision, copied from it. | imported by _job_census.py, homelab_render.py | `test_renderer_cronjob_gate.py` | — |
| `scripts/diagnostics/probe_lib/curl_pipeline.py` | diagnostics | The streaming half of probe.py: argv -> curl/openssl stages -> a piped run. | imported by probe.py | `test_probe_curl_pipeline.py` | — |
| `scripts/deploy_tools/promote_lib/decision.py` | deploy_tools | The `promote` decision `decide` returns, and the half of it that runs once T is chosen. | imported by promote.py | — | — |
| `scripts/dev/fanout_lib/review/delta_confirm.py` | dev | Which findings the delta review leaves standing at the fix head, and which it only repeats. | imported by review.py | — | — |
| `scripts/deploy_tools/land_lib/deploy.py` | deploy_tools | Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree. | imported by pipeline.py | `test_land_deploy.py` | — |
| `scripts/lib/deployer_park.py` | lib | What the GitOps deployer's own markers say it has deferred: a park, or a pending role. | imported by builder.py, gitops_view.py, lander.py, landing_blockers.py, staleness.py | `test_deployer_park.py` | — |
| `scripts/deploy_tools/land_lib/detach.py` | deploy_tools | `land.sh --detach`: fork the landing into a logged child, and record where to find it. | imported by land.py, land_probe.py, landing_blockers.py | `test_land_detach.py` | — |
| `scripts/lib/detach_fork.py` | lib | Run work that outlives its caller: `land.sh --detach` and `deploy.sh --detach` share this. | imported by autoland.py, detach.py, detach_probe.py, detach_run.py | `test_detach_fork.py` | — |
| `scripts/deploy_tools/deploy_lib/detach_probe.py` | deploy_tools | The `deploy` source for cc-wait: one `deploy.sh --detach` run's state, read from its log. | imported by deploy_cli.py | — | — |
| `scripts/deploy_tools/deploy_lib/detach_run.py` | deploy_tools | `deploy.sh --detach`: take the locks here, then run the playbook in a forked child. | imported by run.py | — | — |
| `scripts/infra_map/diagram.py` | infra_map | The architecture figure: how a request reaches a workload, and on what it runs. | imported by render.py | — | — |
| `scripts/renderer/renderer_lib/digest8.py` | renderer | 8-hex-digit digests of a Job's spec and a ConfigMap's content, shared by snapshot and soak. | imported by _job_census.py, cronjob_gate.py, prepull.py, snapshot.py, soak.py | — | — |
| `scripts/lib/doc_freshness.py` | lib | How old a hand-written doc is, and whether the files it names have moved under it. | imported by _mkdocs_freshness.py, freshness.py | `test_doc_freshness.py` | — |
| `scripts/lib/docs_provenance.py` | lib | The provenance banner every generated documentation page opens with. | imported by backlog.py, catalog_render.py, crons.py, decisions.py, fragments_deploy.py, freshness.py, gen_doc_fragments.py, gen_infra_map.py, hosts.py, networking.py, scripts.py, secrets.py, service_catalog.py, state.py | `test_docs_provenance.py` | — |
| `scripts/renderer/renderer_lib/environment.py` | renderer | The strict Jinja environment the renderer renders templates and task expressions in. | imported by cluster.py, cronjob_gate.py, host.py, host_sources.py, images.py, placeholders.py, prepull.py, prune_label.py, restart.py, secret_checks.py, snapshot.py, standalone_map.py, task_inputs.py | — | — |
| `scripts/lib/estate.py` | lib | The inventory as the docs generators read it: hosts, each host's vars, and its entries. | imported by catalog_backup.py, context.py, fragment_readers.py, fragments_hosts.py, fragments_storage.py, gen_doc_fragments.py, gen_role_glance.py, host.py, host_fixtures.py, host_sources.py, hosts.py, k8s_roles.py, networking.py, render_context.py, service_catalog.py | `test_estate.py` | — |
| `scripts/deploy_tools/runbook_gates_lib/etcd_restore.py` | deploy_tools | Run the stop conditions of `docs/k3s-etcd-restore.md` in order, exit code naming the first failure. | no `__main__` guard; imported by test_runbook_gates_etcd_restore.py | `test_runbook_gates_etcd_restore.py` | — |
| `scripts/lib/facts/evidence.py` | lib | Diff evidence for a ``moved`` finding: which removed identifiers the section's prose names. | imported by lock.py | — | — |
| `scripts/lib/exit_codes.py` | lib | Every exit-code contract this repo's entry points share, named once. | imported by _land_fakes.py, applied.py, await_ci.py, b2_drain.py, backups.py, bootstrap_drill.py, broad.py, build_docs.py, builder.py, bundle_census.py, ci.py, ci_contexts.py, claim_cli.py, classify.py, cli_help.py, cli_parser.py, cluster_vars.py, core.py, deletion_drill.py, deploy.py, detach_notify.py, detach_run.py, exit_messages.py, fact_status.py, fanout.py, findings.py, firing_ingress.py, flags.py, flux.py, flux_probe.py, fragments_deploy.py, gate_runner.py, gen_infra_map.py, gitops_view.py, handoff.py, headroom_probe.py, health.py, health_backup_targets.py, history_cli.py, homelab_render.py, host_secrets.py, label_worktree.py, land.py, land_tags.py, lander.py, longhorn_reap.py, merge.py, monitors.py, narrow_setup.py, notify.py, outcome.py, owners.py, pi_plane.py, place.py, playbook.py, probe.py, promote.py, promotion.py, prune.py, prune_worktrees.py, publish_pr.py, push_rules.py, readonly_rbac.py, renovate_branch_sweep.py, renovate_rebase.py, reset.py, review_unit.py, roundtrip.py, run.py, runbook_gates.py, scripts.py, secret_rotation.py, secret_setup_readers.py, setup_reach_hosts.py, shadow.py, shadow_diff.py, snapshots.py, stale_retarget.py, staleness.py, standalone_map.py, stateful.py, status.py, tags.py, takeover_census.py, tick.py, tools.py, under_locks.py, vendor_manifest.py, verify_promotion.py, vip_placement.py, wait.py, wait_probe.py | `test_exit_codes.py` | — |
| `scripts/lib/exit_messages.py` | lib | What an entry point prints about its own exit code, read from `lib.exit_codes.CONTRACTS`. | imported by messages.py, probe.py | — | — |
| `scripts/dev/findings_lib/export_cli.py` | dev | The `export` subcommand: the whole `claude` register, every state, written as one JSON file. | imported by findings.py | — | — |
| `scripts/deploy_tools/narrow_lib/filters.py` | deploy_tools | What a filter plugin defines, and which grep hits for its filter names are callers (#3843). | imported by broad.py, digest_covers.py, narrow_setup.py | — | — |
| `scripts/diagnostics/probe_lib/firing_ingress.py` | diagnostics | `probe.py monitors`' second read: the firing ingress-probe alerts. | imported by monitors.py | — | — |
| `scripts/deploy_tools/deploy_lib/flags.py` | deploy_tools | What `deploy.sh` refuses a command line over, and the exception its gates refuse with. | imported by owners.py, run.py | `test_deploy_flags.py` | — |
| `scripts/diagnostics/probe_lib/flux.py` | diagnostics | `probe.py flux [tag...]` — each Flux Kustomization's Ready, applied commit and suspension. | imported by cli_parser.py, flux_wait.py, subcommands.py | `test_probe_flux.py` | — |
| `scripts/deploy_tools/flux_host.py` | deploy_tools | The two host-side boundaries `promote.py land-wait` crosses on daniel-box. | imported by wait_reads.py | — | — |
| `scripts/renderer/renderer_lib/flux_probe.py` | renderer | The fixture probe: what the pinned flux CLI does with the escapes placeholder mode writes. | imported by shadow_diff.py | — | — |
| `scripts/deploy_tools/narrow_lib/foreign_keys.py` | deploy_tools | Every name that carries a changed key of another role's vars file onward (#4361). | imported by narrow_setup.py, secret_keys.py | — | — |
| `scripts/dev/foreign_owned.py` | dev | Find the paths under a directory that another uid owns, and say how to clear them. | imported by prune_worktrees.py, worktree_orphans.py | `test_foreign_owned.py` | — |
| `scripts/deploy_tools/forge.py` | deploy_tools | The forge adapter: every GitHub and GHCR call that `promote.py` makes. | imported by _promote_fakes.py, ci_contexts.py, flux_host.py, forward_only.py, land_flux.py, pin_gate.py, pin_reads.py, promote.py, promote_state.py, prune.py, reads.py, reset.py, stateful.py, superseded.py, verify_promotion.py, wait.py, wait_reads.py | — | — |
| `scripts/deploy_tools/forge_registry.py` | deploy_tools | The GHCR half of `forge.py`: the OCI distribution reads `promote.py` makes of ghcr.io. | imported by forge.py | — | — |
| `scripts/deploy_tools/forge_transport.py` | deploy_tools | The HTTP layer under `forge.py`: one request, one response, and reading what came back. | imported by forge.py, forge_registry.py | — | — |
| `scripts/deploy_tools/promote_lib/forward_only.py` | deploy_tools | H1's forward-only rules in `promote.py decide`: the hold without a reset, the gate, the backstop. | imported by decision.py, promote.py, reads.py | `test_promote_forward_only.py` | — |
| `scripts/docs/fragment_readers.py` | docs | The readers behind the doc fragments: the tree, parsed, never imported. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragment_renderers.py` | docs | The renderers behind the doc fragments: pure functions from plain values to markdown. | imported by gen_doc_fragments.py | — | — |
| `scripts/docs/fragments_lib/fragments_bridge.py` | docs | Doc fragments for the monitor-bridge pages (docs/monitor-bridge-checks.md, docs/monitor-bridge-internals.md). | imported by gen_doc_fragments.py | `test_fragments_bridge.py` | — |
| `scripts/docs/fragments_lib/fragments_deploy.py` | docs | Doc fragments for the deploy, landing and issue-claiming pages. | imported by gen_doc_fragments.py | `test_fragments_deploy.py` | — |
| `scripts/docs/fragments_lib/fragments_hosts.py` | docs | Doc fragments for the host, network and per-service pin pages. | imported by gen_doc_fragments.py | `test_fragments_hosts.py` | — |
| `scripts/docs/fragments_lib/fragments_storage.py` | docs | Doc fragments for the Longhorn, snapshot and deadman pages. | imported by gen_doc_fragments.py | `test_fragments_storage.py` | — |
| `scripts/lib/function_imports.py` | lib | Whether each entry point's function-local imports execute, under each way it is loaded (#4403). | no `__main__` guard; imported by test_function_imports.py, test_function_imports_exemptions.py | `test_function_imports.py` | — |
| `scripts/deploy_tools/runbook_gates_lib/gate_runner.py` | deploy_tools | The runner and the shared verdicts behind every runbook `scripts/deploy_tools/runbook_gates.py` runs. | imported by etcd_restore.py, k3s_upgrade.py, longhorn_dr.py, longhorn_upgrade.py, metallb_upgrade.py, pinned_rotation.py | — | — |
| `scripts/lib/gh.py` | lib | One way to run the GitHub CLI from a script, with no prompt and no notifier. | imported by autoland.py, boundaries.py, clean.py, label_worktree.py, land_tested_base.py, pipeline_timeline.py, promote_state.py, publish_pr.py, push_rules.py, pytest_shard.py, renovate_branch_sweep.py, renovate_rebase.py, signing.py, tools.py, transport.py | `test_gh.py` | — |
| `scripts/dev/findings_lib/gh_calls.py` | dev | The gh reads and writes `findings.py` makes, and the one place a plan is executed. | imported by backlog.py, claim_cli.py, export_cli.py, findings.py, history_cli.py | — | — |
| `scripts/deploy_tools/land_lib/gh_read.py` | deploy_tools | A read-only `gh` call retried through a transient failure before the landing refuses (#5503). | imported by landing.py, merge.py, policy.py | — | — |
| `scripts/lib/git.py` | lib | One way to run git from a script, with the repository chosen by ``cwd`` alone. | imported by autoland.py, await_ci.py, bootstrap_case.py, broad.py, citations.py, clean.py, cluster_pins.py, cluster_vars.py, cross_plane_order.py, cross_plane_replay.py, decisions.py, digest_covers.py, doc_freshness.py, docs_provenance.py, evidence.py, fact_status.py, filters.py, flux.py, forward_only.py, health_checkout.py, host_packages.py, host_wait.py, k3s_binary_sum.py, label_worktree.py, land.py, land_tags_cli.py, lint.py, lock.py, narrow_git.py, narrow_setup.py, notice.py, order_only.py, owners.py, paths.py, place.py, prune_worktrees.py, publish_pr.py, push_rules.py, pytest_shard.py, reads.py, releases.py, releases_diff.py, releases_render.py, removed.py, render_guard.py, renovate_branch_sweep.py, replay.py, report.py, root_ignored_files.py, rotation_tools.py, run.py, secret_keys.py, setup_index.py, setup_reach_hosts.py, setup_role_chains.py, shared_role_reach.py, soak.py, staleness.py, state.py, tags.py, templates.py, tools.py, tracked_paths.py, transport.py, under_locks.py, wait_reads.py, worktree_orphans.py, worktrees.py | `test_git.py` | — |
| `scripts/secrets_mgmt/git_dates.py` | secrets_mgmt | When each secret's ciphertext last changed, read out of git. | imported by host_secrets.py, secret_rotation.py | — | — |
| `scripts/dev/fanout_lib/review/git_state.py` | dev | The git state outside a red batch's worktree that the red author could write (#3864, #3879). | imported by review.py | `test_fanout_git_state.py` | — |
| `scripts/lib/git_testing.py` | lib | Build a throwaway git repository for a test, with the inherited `GIT_*` environment gone. | imported by _deploy_sh_fakes.py, _narrow_fixtures.py, _release_fixtures.py, _scratch_pytest.py | `test_git_testing.py` | — |
| `scripts/diagnostics/probe_lib/gitops_view.py` | diagnostics | `probe.py gitops-state` — every marker the GitOps deployer keeps, read-only, with its way out. | imported by subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/gitops_why.py` | diagnostics | `probe.py gitops-state --why` — the failure lines of the GitOps tick that wrote `hold_sha`. | imported by cli_parser.py, gitops_view.py | `test_probe_gitops_why.py` | — |
| `scripts/docs/catalog_lib/glance_facts.py` | docs | The setup-plane fact readers behind `gen_role_glance.py`, and what every plane shares. | imported by gen_role_glance.py, setup_census_items.py | — | — |
| `scripts/diagnostics/grafana_panel_report.py` | diagnostics | Classify what a Grafana dashboard page actually rendered. | no `__main__` guard; imported by test_grafana_panel_report.py, test_ui_smoke_grafana.py | `test_grafana_panel_report.py` | — |
| `scripts/dev/fanout_lib/review/green_contest.py` | dev | The implementer's contest of a red test whose premise is false (#5400, #4874). | imported by review.py, review_record.py | `test_fanout_green_contest.py` | — |
| `scripts/infra_map/groups.py` | infra_map | The functional grouping behind the workload strip under the diagram. | imported by render.py | — | — |
| `scripts/deploy_tools/narrow_lib/guard_map.py` | deploy_tools | Which callers a change to the standalone guard map reaches: the rows that moved (#5571). | imported by broad.py | `test_narrow_guard_map.py` | — |
| `scripts/diagnostics/probe_lib/ha.py` | diagnostics | Home Assistant: live state, automations, and the read-only WebSocket trace client. | imported by ha_state_model.py, postflight.py, subcommands.py | `test_probe_ha.py` | — |
| `scripts/home_assistant/ha_state_checks.py` | home_assistant | Guardrail checks over the HA state model built by `ha_state_model.py`. | imported by ha_state_model.py, validate_ha_config.py | `test_ha_state_checks.py` | — |
| `scripts/home_assistant/ha_state_model.py` | home_assistant | Derived state model for the Home Assistant bedroom control plane. | imported by ha.py, ha_state_checks.py | `test_ha_state_model.py` | — |
| `scripts/deploy_tools/land_lib/handoff.py` | deploy_tools | Hand a landing to a lander unit, for a user who cannot land a PR itself. | imported by land.py | `test_land_handoff.py` | — |
| `scripts/dev/fanout_lib/review/hardened_runs.py` | dev | The git and pytest calls the red/green measures make, so nothing the agent wrote steers them. | imported by base_check.py, hunk_check.py, red_gate.py, review.py, worktree_reset.py | — | — |
| `scripts/dev/fanout_lib/headroom_probe.py` | dev | The `fanout-headroom` source for cc-wait: whether `fanout.py place launch` would place a batch. | no `__main__` guard; imported by test_fanout_headroom_probe.py | `test_fanout_headroom_probe.py` | — |
| `scripts/diagnostics/probe_lib/health.py` | diagnostics | `probe.py health <svc>` — the post-deploy gate, from a deploy tag to a per-workload verdict. | imported by probe.py, shed_set.py, subcommands.py | `test_probe_health.py` | — |
| `scripts/diagnostics/probe_lib/health_alerts.py` | diagnostics | The alert-plane half of `probe.py health <tag>`: firing alerts and ingress probes. | imported by health.py | `test_probe_health_alerts.py` | — |
| `scripts/diagnostics/probe_lib/health_backup_targets.py` | diagnostics | The `probe.py health longhorn-backup` verdict: do the BackupTargets read available? | imported by health.py | `test_probe_health_backup_targets.py` | — |
| `scripts/diagnostics/probe_lib/health_checkout.py` | diagnostics | Whether `probe.py health <tag>` runs from a checkout older than the tag's k8s role (#5909). | imported by health.py | `test_probe_health_checkout.py` | — |
| `scripts/diagnostics/probe_lib/health_cronjob.py` | diagnostics | The CronJob half of `probe.py health` — a role with a CronJob and no rollout to gate on. | imported by health.py | — | — |
| `scripts/diagnostics/probe_lib/health_docker.py` | diagnostics | `probe.py health --docker <svc>`, and the direct-address lookups the arr probes need. | imported by arr.py, health.py, postflight.py | — | — |
| `scripts/diagnostics/probe_lib/health_kubectl.py` | diagnostics | The kubectl argv builders and the pod selector `probe.py health` runs its queries through. | imported by health.py, kuma_live.py | — | — |
| `scripts/diagnostics/probe_lib/health_plumbing.py` | diagnostics | The cluster-plumbing checks `probe.py health` adds for the `coredns` and `longhorn` tags. | imported by health.py | `test_probe_health_plumbing.py` | — |
| `scripts/diagnostics/probe_lib/health_render.py` | diagnostics | The render context `probe.py health` resolves a role's workloads with, built once per process. | imported by health.py | — | — |
| `scripts/diagnostics/probe_lib/health_rollout.py` | diagnostics | The rollout half of `probe.py health` — a Deployment or DaemonSet's verdict. | imported by health.py, health_cronjob.py, kuma_live.py | — | — |
| `scripts/diagnostics/probe_lib/health_vendored.py` | diagnostics | The workloads a role's vendored files declare, for `probe.py health` and `probe.py shed-set`. | imported by health.py | `test_probe_health_vendored.py` | — |
| `scripts/deploy_tools/land_lib/health_verdict.py` | deploy_tools | Step 7: the health verdict, and the two halves a healthy deploy can still leave open. | imported by pipeline.py | `test_land_health_verdict.py` | — |
| `scripts/dev/fanout_lib/review/held_hooks.py` | dev | The project hooks and settings a review pipeline holds from start, outside the worktree. | imported by review.py | — | — |
| `scripts/dev/findings_lib/history_cli.py` | dev | The two history subcommands: `history` searches past findings, `show` prints one. | imported by findings.py | — | — |
| `scripts/renderer/renderer_lib/host.py` | renderer | Host and package modes: one host's package, or one shared package, rendered from the git tree. | imported by homelab_render.py, host_fixtures.py, secrets_case.py, setup_census.py, unit_templates.py | — | — |
| `scripts/renderer/renderer_lib/host_arms.py` | renderer | The `activate:` field of a file entry and the `ufw:` key, which feed the reconcile manifest. | imported by host.py, host_manifest.py | `test_renderer_host_arms.py` | — |
| `scripts/renderer/renderer_lib/host_compose.py` | renderer | The Pi's Compose project in host mode: the include renderer and the inputs hash (H5.2a1). | imported by host.py | — | — |
| `scripts/validate/validate_lib/host_container.py` | validate | A booted test container for the host_packages container cases, driven with `docker exec`. | imported by bootstrap_case.py, bootstrap_repo_case.py, conffile_case.py, host_package_cases.py, repos_case.py, secrets_case.py, shadow_case.py | — | — |
| `scripts/validate/validate_lib/host_fixtures.py` | validate | The packages and the repository the host_packages container cases serve. | imported by bootstrap_repo_case.py, conffile_case.py, host_container.py, host_package_cases.py, repos_case.py, secrets_case.py, shadow_case.py | — | — |
| `scripts/renderer/renderer_lib/host_inputs.py` | renderer | `HOST_INPUTS`: every repo path a host render reads (H3, *Steps*, H3.9a item 1). | imported by classify.py | `test_renderer_host_inputs.py` | — |
| `scripts/renderer/renderer_lib/host_manifest.py` | renderer | The manifest `homelab-reconcile` reads, and the three spec keys that feed it. | imported by host.py, host_outputs.py, host_package.py | `test_renderer_host_manifest.py` | — |
| `scripts/renderer/renderer_lib/host_outputs.py` | renderer | Secret-bearing files in host mode: declared outputs, compose files and credentials. | imported by host.py, host_compose.py | — | — |
| `scripts/renderer/renderer_lib/host_package.py` | renderer | The `Package` a host or shared package renders to, and the `nfpm.yaml` it writes. | imported by host.py, host_compose.py, host_outputs.py, host_write.py | — | — |
| `scripts/diagnostics/probe_lib/host_reader.py` | diagnostics | One read of a host's journal, files and unit state, made in a single process on that host. | imported by applied.py, builder.py, gitops_view.py, gitops_why.py, lander.py | `test_probe_host_reader.py` | — |
| `scripts/deploy_tools/land_lib/host_schedule.py` | deploy_tools | The schedule half of the host wait: when a host should have applied a merge. | imported by host_wait.py | — | — |
| `scripts/renderer/renderer_lib/host_sources.py` | renderer | Where a host package's files may come from, and the octal mode each installs with. | imported by host.py, host_outputs.py | — | — |
| `scripts/renderer/renderer_lib/host_tools.py` | renderer | The `tools:` key of a shared package spec: pinned upstream binaries, built once per architecture. | imported by host.py, host_package.py, host_write.py | `test_renderer_host_tools.py` | — |
| `scripts/deploy_tools/land_lib/host_wait.py` | deploy_tools | The landing's wait on the hosts a merge reaches (H3, *Steps*, H3.9a and H3.9b). | imported by pipeline.py | `test_land_host_wait.py` | — |
| `scripts/renderer/renderer_lib/host_write.py` | renderer | Writing rendered packages to disk: `host.write_packages` and the tree it lays out. | imported by host.py | — | — |
| `scripts/infra_map/html_views.py` | infra_map | The HTML host panels: one row per service, one panel per host. | imported by render.py | — | — |
| `scripts/dev/fanout_lib/review/hunk_check.py` | dev | Whether the red tests notice each hunk of the fix when it alone is taken out. | imported by _review_fakes.py, review.py | `test_fanout_hunk_check.py` | — |
| `scripts/renderer/renderer_lib/images.py` | renderer | The image contexts a cluster render writes under `DIR/images/` (D5.4a1, C1 *Cluster output*). | imported by homelab_render.py, placeholders.py | `test_renderer_images.py` | — |
| `scripts/infra_map/inventory.py` | infra_map | Declared state: what ``containers_list`` and the role trees say should run. | imported by gen_infra_map.py, model.py | — | — |
| `scripts/lib/invocation_sites.py` | lib | Where a `scripts/...` path can be executed from, read once for two different questions. | imported by script_classify.py | `test_invocation_sites.py` | — |
| `scripts/dev/findings_lib/issue_model.py` | dev | The finding vocabulary and the pure reads over a gh issue: no gh, no shell, no argv. | imported by _findings_fakes.py, backlog.py, brief.py, claim.py, claim_cli.py, cli.py, export_cli.py, findings.py, gh_calls.py, history_cli.py, launch_gates.py, plans.py, solo_only.py, transport.py, verify.py | — | — |
| `scripts/renderer/renderer_lib/jinja_aliases.py` | renderer | Rule 5's alias clauses: a name that carries a secret key is held to the key's forms. | imported by secret_checks.py, var_aliases.py | — | — |
| `scripts/lib/jinja_comments.py` | lib | Blank a template's `{# … #}` comments so a text reader cannot match inside one. | imported by catalog_backup.py, glance_facts.py, k8s_roles.py, route_facts.py | `test_jinja_comments.py` | — |
| `scripts/lib/jinja_defaults.py` | lib | Resolve a generated doc's `{{ … }}` to the value every host would get, or leave it as written. | imported by crons.py, glance_facts.py | `test_jinja_defaults.py` | — |
| `scripts/renderer/renderer_lib/jinja_uses.py` | renderer | Where each secret key appears in a template's syntax tree, and the forms it passes through. | imported by jinja_aliases.py, secret_checks.py, var_aliases.py | — | — |
| `scripts/lib/json_types.py` | lib | The type of a parsed JSON document, and the narrowing helpers that go with it. | imported by boundaries.py, export_cli.py, gh.py, gh_calls.py, glance_facts.py, history_cli.py, kubectl.py, kuma_live.py, label_worktree.py, land_tested_base.py, landing.py, merge.py, pace.py, policy.py, pr_json.py, setup_gates.py, tools.py | `test_json_types.py` | — |
| `scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py` | deploy_tools | Run the four stop conditions of `docs/k3s-upgrade.md` in order, exit code naming the first failure. | imported by metallb_upgrade.py | `test_runbook_gates_k3s_upgrade.py` | — |
| `scripts/lib/k8s_context.py` | lib | Ansible's variable semantics, reproduced for the k8s manifest render guard. | imported by cluster.py, context.py, k8s_manifests.py, render_context.py, restart.py | `test_k8s_context.py` | — |
| `scripts/validate/validate_lib/k8s_net_rules.py` | validate | The two semantic rules on rendered manifests that no schema can make. | imported by k8s_manifests.py | `test_k8s_net_rules.py` | — |
| `scripts/lib/k8s_role_callers.py` | lib | Which k8s roles reach another k8s role, read from the live tree: `role_callers`. | imported by k8s_roles.py | `test_k8s_role_callers.py` | — |
| `scripts/lib/k8s_roles.py` | lib | Which roles exist under ``ansible/roles/k8s/``, and which of them each reader skips. | imported by _live_read_census.py, broad.py, bundle.py, bundle_census.py, catalog_backup.py, catalog_facts.py, cluster.py, consumers.py, containers.py, gen_role_glance.py, glance_facts.py, health_checkout.py, k8s_autodeploy_counts.py, k8s_manifests.py, kuma_live.py, land_shared.py, monitors.py, owners.py, postflight.py, releases_consumers.py, releases_retired.py, render_targets.py, rotation_tools.py, route_facts.py, run.py, service_catalog.py, service_tiers.py, setup_census_items.py, setup_routing.py, shed_set.py, snapshot.py | — | — |
| `scripts/validate/validate_lib/k8s_schema.py` | validate | Schema validation for a rendered k8s object: the core OpenAPI check and the vendored CRDs. | imported by k8s_manifests.py | `test_k8s_schema.py` | — |
| `scripts/lib/k8s_yaml.py` | lib | YAML parsing for rendered k8s manifests, and the PVC names they declare and reference. | imported by ansible_jinja_env.py, environment.py, k8s_manifests.py | `test_k8s_yaml.py` | — |
| `scripts/lib/kubectl.py` | lib | One way to run kubectl from a script, and every call names the cluster it must reach. | imported by _gates_fakes.py, arr.py, b2_ledger.py, cli_parser.py, etcd_restore.py, export_grafana_dashboards.py, flux.py, flux_host.py, flux_wait.py, gate_runner.py, gen_infra_map.py, health.py, health_docker.py, health_plumbing.py, k3s_upgrade.py, kuma_live.py, land_flux.py, land_rerolls.py, live.py, longhorn.py, longhorn_cluster.py, longhorn_dr.py, longhorn_upgrade.py, metallb_upgrade.py, monitors.py, pinned_rotation.py, postflight.py, probe.py, promote_state.py, readonly_rbac.py, state.py, takeover.py, vip_placement.py | `test_kubectl.py` | — |
| `scripts/diagnostics/probe_lib/kuma_live.py` | diagnostics | What the live Kuma says about itself: its pod's age and the monitors it holds. | imported by monitors.py, postflight.py | `test_kuma_live.py` | — |
| `scripts/diagnostics/probe_lib/kuma_pi.py` | diagnostics | Which declared Kuma monitors are daniel-pi's own, for `probe.py kuma-drift --pi`. | imported by monitors.py | — | — |
| `scripts/diagnostics/probe_lib/kuma_table_loop.py` | diagnostics | The table loop in the static-monitors template, as `parse_declared_monitors` reads it. | imported by monitors.py | — | — |
| `scripts/deploy_tools/kustomizations.py` | deploy_tools | Fields of a Kustomization as `kubectl get -o json` lists it: its origin commit, Ready, edges. | imported by chained_timeout.py, flux.py, verify_promotion.py, wait.py, wait_reads.py | — | — |
| `scripts/deploy_tools/land_lib/land_flux.py` | deploy_tools | The process boundaries a landing crosses for a tag Flux applies. | imported by _land_fakes.py, tools.py | — | — |
| `scripts/deploy_tools/land_lib/land_platform.py` | deploy_tools | Which of a PR's derived tags its own changed paths PROVE are a k3s change. | imported by deploy.py, tools.py | `test_land_platform.py` | — |
| `scripts/deploy_tools/land_lib/land_reach.py` | deploy_tools | Which hosts a self-applied setup-role change still owes a hand, beyond the tick's own host. | imported by land_tags_cli.py, setup_census.py, setup_reach_hosts.py, tools.py | `test_land_reach.py` | — |
| `scripts/deploy_tools/land_lib/land_rerolls.py` | deploy_tools | Whether a later deploy re-rolled a landing's workloads under its health gate (#3812). | imported by tools.py | `test_land_rerolls.py` | — |
| `scripts/deploy_tools/land_lib/land_shared.py` | deploy_tools | The shared-role expansion a landing makes, and the #3124 narrowing. | imported by land_tags.py, precomputed.py, tools.py | — | — |
| `scripts/deploy_tools/land_lib/land_tested_base.py` | deploy_tools | The master commit a head's green `ci.yml` PR run tested it against (issue #5487). | imported by tools.py | — | — |
| `scripts/diagnostics/probe_lib/lander.py` | diagnostics | `probe.py lander` — what the daniel-server batch lander did with each PR, and how it ended. | imported by cli_parser.py, subcommands.py | `test_probe_lander.py` | — |
| `scripts/deploy_tools/land_lib/landing.py` | deploy_tools | One PR's landing: the state every phase reads and writes, and the ways it ends. | imported by _land_fakes.py, ci.py, classify.py, deploy.py, health_verdict.py, host_wait.py, land.py, merge.py, pace.py, pin_gate.py, pipeline.py, policy.py, promotion.py, stale_retarget.py, tick.py | `test_land_landing.py` | — |
| `scripts/diagnostics/probe_lib/landing_blockers.py` | diagnostics | `probe.py landing` — what would stop a landing now, and who else is working the repo. | imported by subcommands.py | — | — |
| `scripts/diagnostics/probe_lib/landing_verdicts.py` | diagnostics | `probe.py landing-verdicts [<pr>]` — the newest recorded `VERDICT:` line of each landed PR. | imported by cli_parser.py, subcommands.py | — | — |
| `scripts/dev/fanout_lib/launch.py` | dev | Create the worktree, write the brief, start the transient service — spec §3. | imported by place.py, review.py | `test_fanout_launch.py` | — |
| `scripts/dev/fanout_lib/launch_gates.py` | dev | The refusals `launch` applies to a batch grouping before it spends an ssh connection. | imported by autoland.py, place.py | `test_fanout_launch_gates.py` | — |
| `scripts/deploy_tools/land_lib/ledger.py` | deploy_tools | What the Landings board reads: one logfmt line per landing, from a Ledger of stamps. | imported by land.py, landing.py | `test_land_ledger.py` | — |
| `scripts/lib/facts/lint.py` | lib | Lint for the fact stores: the forms the spec excludes, and the atoms that do not resolve. | imported by fact_status.py, notice.py, report.py | — | — |
| `scripts/infra_map/live.py` | infra_map | Live state: what the cluster and the Pi report is actually running. | imported by gen_infra_map.py | — | — |
| `scripts/lib/facts/lock.py` | lib | ``docs/facts.lock/``: the recorded hashes for every verified CLAUDE.md section. | imported by fact_status.py, lint.py, report.py | — | — |
| `scripts/backup/longhorn_reap_lib/logic.py` | backup | Pure decision core shared by the two Longhorn reapers behind `longhorn_reap.py`. | imported by backups.py, selection.py, snapshots.py | `test_longhorn_reap_logic.py` | — |
| `scripts/diagnostics/probe_lib/longhorn.py` | diagnostics | Longhorn's B2 backup objects: what the estate holds and what it costs. | imported by b2_ledger.py, cli_parser.py, subcommands.py | `test_probe_longhorn.py` | — |
| `scripts/lib/longhorn_lib/longhorn_backup_health_logic.py` | lib | Pure decision core for the Longhorn backup-plane heartbeat (longhorn_backup_health.py). | no `__main__` guard; imported by test_longhorn_backup_health.py, test_longhorn_restore_drill_checks.py | — | — |
| `scripts/lib/longhorn_lib/longhorn_backups.py` | lib | Longhorn Backup CRs as records, and the backup group and target names, defined once. | imported by k3s_upgrade.py, logic.py, longhorn_backup_health_logic.py, longhorn_budget.py, longhorn_cluster.py, longhorn_cluster_state.py, longhorn_dr.py, longhorn_upgrade.py | `test_longhorn_backups.py` | — |
| `scripts/diagnostics/probe_lib/longhorn_budget.py` | diagnostics | What one Longhorn retention prune costs B2 in Class C transactions, per weekly shard. | imported by longhorn.py | — | — |
| `scripts/diagnostics/probe_lib/longhorn_cluster.py` | diagnostics | The live cluster objects the B2 reports read: Volume, Backup, PV and BackupTarget. | imported by b2_spend.py, longhorn.py | — | — |
| `scripts/lib/longhorn_lib/longhorn_cluster_state.py` | lib | What the backup-health reader reads off the cluster's BackupTargets and RecurringJobs. | no `__main__` guard; imported by test_longhorn_cluster_state.py | `test_longhorn_cluster_state.py` | — |
| `scripts/lib/longhorn_lib/longhorn_cron_evidence_logic.py` | lib | Checks 9 and 10 of the Longhorn backup-plane heartbeat — the two crons' evidence plane. | no `__main__` guard; imported by test_longhorn_backup_health_cron_evidence.py | — | — |
| `scripts/deploy_tools/runbook_gates_lib/longhorn_dr.py` | deploy_tools | Run the stop conditions of `docs/longhorn-disaster-recovery.md` in order, exit code naming the first failure. | no `__main__` guard; imported by test_runbook_gates_longhorn_dr.py | `test_runbook_gates_longhorn_dr.py` | — |
| `scripts/lib/longhorn_lib/longhorn_restore_drill_stamps.py` | lib | The restore drill's stamps, as the Longhorn backup-plane heartbeat and the other readers read them. | imported by longhorn_upgrade.py, state.py | — | — |
| `scripts/lib/longhorn_lib/longhorn_skipped_volumes_logic.py` | lib | Check 11 of the Longhorn backup-plane heartbeat: weekly volumes a RecurringJob skipped (#3968). | no `__main__` guard; imported by test_longhorn_skipped_volumes.py | — | — |
| `scripts/deploy_tools/runbook_gates_lib/longhorn_upgrade.py` | deploy_tools | Run the stop conditions of `docs/longhorn-upgrade.md` in order, exit code naming the first failure. | no `__main__` guard; imported by test_runbook_gates_longhorn_upgrade.py | `test_runbook_gates_longhorn_upgrade.py` | — |
| `scripts/dev/fanout_lib/manifest.py` | dev | The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4. | imported by abandon.py, batch_holds.py, boundaries.py, clean.py, headroom_probe.py, launch.py, launch_gates.py, notify.py, place.py, status.py, transport.py, unit_state.py, wait_probe.py | — | — |
| `scripts/deploy_tools/land_lib/merge.py` | deploy_tools | The merge phase: check the PR here, then merge it directly once its CI is green. | imported by pipeline.py | `test_land_merge.py` | — |
| `scripts/deploy_tools/deploy_lib/messages.py` | deploy_tools | The prefix every line `deploy.sh` prints carries, and the three ways it prints one. | imported by detach_run.py, owners.py, playbook.py, run.py, staleness.py, tags.py, under_locks.py | `test_deploy_messages.py` | — |
| `scripts/deploy_tools/runbook_gates_lib/metallb_upgrade.py` | deploy_tools | Run the stop conditions of a MetalLB pin bump in order, exit code naming the first failure. | no `__main__` guard; imported by test_runbook_gates_metallb_upgrade.py | `test_runbook_gates_metallb_upgrade.py` | — |
| `scripts/diagnostics/probe_lib/metrics.py` | diagnostics | `probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries. | imported by subcommands.py | — | — |
| `scripts/infra_map/model.py` | infra_map | Reconciliation: overlay live state onto the declared skeleton. | imported by gen_infra_map.py | — | — |
| `scripts/diagnostics/probe_lib/monitors.py` | diagnostics | `probe.py monitors` and `probe.py kuma-drift` -- what is down, and what is missing. | imported by postflight.py, subcommands.py | `test_probe_monitors.py` | — |
| `scripts/renderer/renderer_lib/namespace.py` | renderer | The shared kind `homelab-namespace`: the `homelab` Namespace and its LimitRange (D5 step 30). | imported by bundle.py, ordering.py | — | — |
| `scripts/lib/narrow_git.py` | lib | The reads the two narrowing derivations share: a refusal, a `git show`, and a key diff. | imported by broad.py, digest_covers.py, filters.py, foreign_keys.py, guard_map.py, narrow_setup.py, paths.py, releases_render.py, secret_keys.py, secret_setup_readers.py, setup_index.py, shared_role_reach.py, task_walk.py, templates.py | `test_narrow_git.py` | — |
| `scripts/lib/facts/notice.py` | lib | The facts diff notice: each ``CLAUDE.md`` section citing an atom that a diff moved. | imported by fact_status.py, replay.py, report.py | — | — |
| `scripts/validate/notification_renders.py` | validate | Hold the Alertmanager notification template to the text it must render. | imported by alert_rules.py | — | — |
| `scripts/dev/fanout_lib/notify.py` | dev | Page the operator when a fan-out batch's unit stops on any state but `done` (#4602). | imported by launch.py | `test_fanout_notify.py` | — |
| `scripts/deploy_tools/object_loss.py` | deploy_tools | `promote.py decide`'s object-loss check: a candidate may lose an object only on purpose. | imported by cross_plane_order.py, promote.py | — | — |
| `scripts/diagnostics/probe_lib/obs_api.py` | diagnostics | The Prometheus and Loki query client every caller shares: URL builders, window, transport. | no `__main__` guard; carried by deploy: ansible/roles/k8s/homelab-mcp/tasks/main.yml | `test_obs_api.py` | — |
| `scripts/validate/validate_lib/ok_lines.py` | validate | The per-template `[ok]` line every template validator prints, kept behind `-v` (#4448). | imported by compose_templates.py, k8s_manifests.py, setup_templates.py, shell_templates.py, unit_templates.py | — | — |
| `scripts/dev/fanout_lib/operator_waits.py` | dev | The textfile gauge of fan-out work that waits on a person, written by each lander pass (#5671). | imported by autoland.py, pipeline_timeline.py | `test_fanout_operator_waits.py` | — |
| `scripts/deploy_tools/land_lib/options.py` | deploy_tools | Everything the command line sets, plus the budgets a test shortens. | imported by _land_fakes.py, handoff.py, land.py, landing.py | `test_land_options.py` | — |
| `scripts/deploy_tools/narrow_lib/order_only.py` | deploy_tools | Whether a filter plugin's change only reorders the play, judged by running it (#4781, #4842). | imported by digest_covers.py | `test_order_only.py` | — |
| `scripts/deploy_tools/narrow_lib/order_only_runner.py` | deploy_tools | The script `order_only` runs in a subprocess at each end of a `toposort.py` change. | imported by order_only.py | — | — |
| `scripts/deploy_tools/narrow_lib/order_only_tables.py` | deploy_tools | The tables and process-state reader the `order_only` runner script opens with. | imported by order_only_runner.py | — | — |
| `scripts/renderer/renderer_lib/ordering.py` | renderer | The generated `dependsOn` of the bundle's Kustomizations (D4.2). | imported by _job_census.py, bundle.py, bundle_census.py, cluster.py, cronjob_gate.py, cross_plane_order.py, homelab_render.py, prepull.py, shadow.py, soak.py, standalone_map.py, stateful.py, verify_promotion.py, wait.py | — | — |
| `scripts/deploy_tools/land_lib/outcome.py` | deploy_tools | The words a landing ends with: the verdict set, the cause set, the Outcome, and say(). | imported by ci.py, classify.py, compose_wait.py, deploy.py, gh_read.py, handoff.py, health_verdict.py, host_wait.py, land.py, land_probe.py, landing.py, ledger.py, merge.py, pace.py, pin_gate.py, pipeline.py, policy.py, promotion.py, stale_retarget.py, tick.py | `test_land_outcome.py` | — |
| `scripts/deploy_tools/deploy_lib/owners.py` | deploy_tools | Which deploy tags are not Ansible's to deploy, and what `deploy.sh` does with each. | imported by land_flux.py, run.py, shared_role_callers.py, tags.py | — | — |
| `scripts/deploy_tools/land_lib/pace.py` | deploy_tools | Merge pacing: hold a green PR's merge while master CI already has a backlog (#5526). | imported by merge.py | `test_land_pace.py` | — |
| `scripts/validate/validate_lib/package_roots.py` | validate | The two scans of the `host_packages` job that read rendered package roots (H4.1c). | imported by host_packages.py | `test_package_roots.py` | — |
| `scripts/deploy_tools/narrow_lib/paths.py` | deploy_tools | The rules `narrow_broad` keeps outside itself: a path shape, and a role-directory read. | imported by broad.py, digest_covers.py, narrow_setup.py | — | — |
| `scripts/secrets_mgmt/pending_retirement.py` | secrets_mgmt | Keys whose retirement is scheduled but not yet due. | imported by cluster_vars.py, cluster_vars_check.py, consumers.py | — | — |
| `scripts/validate/validate_lib/pi_host_seed.py` | validate | Host state the real Pi has and the systemd test container lacks (H7.27, H7.25). | imported by secrets_case.py | — | — |
| `scripts/diagnostics/probe_lib/pi_plane.py` | diagnostics | `probe.py targets --pi` and `probe.py pi containers` — first-command triage for daniel-pi. | imported by subcommands.py | `test_probe_pi_plane.py` | — |
| `scripts/deploy_tools/land_lib/pin_gate.py` | deploy_tools | The pin gate: what `land.sh` checks before it merges a forward-only pin bump (H1.1g). | imported by land.py, merge.py | `test_land_pin_gate.py` | — |
| `scripts/deploy_tools/land_lib/pin_reads.py` | deploy_tools | The process boundaries the pin gate crosses (`pin_gate.py`), one `PinReads` field each. | imported by _land_fakes.py, land_flux.py, pin_gate.py | — | — |
| `scripts/deploy_tools/runbook_gates_lib/pinned_rotation.py` | deploy_tools | Run the stop conditions of the pinned-secret procedure in `docs/secret-rotation.md`, exit code naming the first failure. | no `__main__` guard; imported by test_runbook_gates_pinned_rotation.py | `test_runbook_gates_pinned_rotation.py` | — |
| `scripts/lib/facts/pins.py` | lib | The YAML keys whose value Renovate rewrites, from ``renovate.json`` and its built-in managers. | imported by lint.py | — | — |
| `scripts/deploy_tools/land_lib/pipeline.py` | deploy_tools | The phase order, the step headers, and nothing else. | imported by land.py | `test_land_pipeline.py` | — |
| `scripts/dev/fanout_lib/place.py` | dev | Place issue-fanout batches on the session host with the most memory headroom. | imported by headroom_probe.py, wait_probe.py | — | [0, 1, 3, 4, 5, 6, 7, 64, 75](#scriptsdevfanoutlibplacepy) |
| `scripts/renderer/renderer_lib/placeholders.py` | renderer | Placeholder mode: each secret reference renders as Flux's `${<key>}` (target slice R2). | imported by bundle_gates.py, cluster_vars.py, deletion_drill.py, homelab_render.py, host.py, host_compose.py, host_outputs.py, restart.py, roundtrip.py, secret_checks.py, shadow_diff.py, sops_entries.py, standalone_map.py | `test_renderer_placeholders.py` | — |
| `scripts/dev/fanout_lib/placement.py` | dev | Pure placement over (host, cap, current, live_agents). | imported by headroom_probe.py, launch_gates.py, place.py, transport.py | `test_fanout_placement.py` | — |
| `scripts/lib/plain_scalar.py` | lib | Whether a secret value survives Flux's substitution as the same string (target slice R2). | imported by cluster_vars.py, flux_probe.py | `test_plain_scalar.py` | — |
| `scripts/dev/findings_lib/plans.py` | dev | What `findings.py` decides to do, as gh argv nobody has run yet. | imported by claim_cli.py, findings.py, gh_calls.py | — | — |
| `scripts/deploy_tools/deploy_lib/playbook.py` | deploy_tools | The playbook run of a foreground deploy, and what a finished one records. | imported by detach_run.py, under_locks.py | — | — |
| `scripts/deploy_tools/land_lib/policy.py` | deploy_tools | The landing policy a lander unit sets for an agent's PRs, checked before any merge call. | imported by merge.py | `test_land_policy.py` | — |
| `scripts/deploy_tools/land_lib/pr_json.py` | deploy_tools | The fields the lander reads from a PR's JSON, typed once where `gh_json` returns them. | imported by landing.py, merge.py, policy.py | `test_land_pr_json.py` | — |
| `scripts/dev/findings_lib/precomputed.py` | dev | What a session working an issue would otherwise rediscover: the deploy plane and the tests. | imported by brief.py, history_cli.py | `test_findings_precomputed.py` | — |
| `scripts/renderer/renderer_lib/prepull.py` | renderer | The `<tag>-prepull` directory: Jobs that pull a role's images onto its node before `<tag>` applies. | imported by _job_census.py, homelab_render.py | `test_renderer_prepull.py` | — |
| `scripts/lib/proc_testing.py` | lib | Launch a subprocess from a test, and build the fake binaries it finds on `PATH`. | imported by _cluster_vars_fixture.py, _deploy_sh_fakes.py, _reap_entrypoint_harness.py, _render_audit.py, _scratch_pytest.py | `test_proc_testing.py` | — |
| `scripts/dev/fanout_lib/review/processes.py` | dev | The review pipeline's process boundary, and the reap that ends a red phase. | imported by review.py | — | — |
| `scripts/diagnostics/probe_lib/promote_state.py` | diagnostics | `probe.py landing`'s promotion lines: both holds, `prod` and `last-good`, then suspensions. | imported by landing_blockers.py | — | — |
| `scripts/deploy_tools/land_lib/promotion.py` | deploy_tools | Step 5's other half: a tag Flux applies lands through `promote`, never through deploy.sh. | imported by deploy.py, health_verdict.py | `test_land_promotion.py` | — |
| `scripts/dev/fanout_lib/prompt_tool.py` | dev | The flags that let a headless batch's permission prompts reach claude-guard's allow rules. | imported by launch.py, review.py | `test_fanout_prompt_tool.py` | — |
| `scripts/deploy_tools/promote_lib/prune.py` | deploy_tools | `promote.py prune`: delete the artifact versions no promotion or rollback can reach. | imported by ci_contexts.py, cross_plane_order.py, decision.py, forward_only.py, object_loss.py, pin_reads.py, promote.py, reads.py, reboot_window.py, reset.py, stateful.py | `test_promote_prune.py` | — |
| `scripts/renderer/renderer_lib/prune_label.py` | renderer | The prune exemption: `kustomize.toolkit.fluxcd.io/prune: disabled` on claims and Namespaces (D5.1a2). | imported by deletion_drill.py, homelab_render.py | `test_renderer_prune_label.py` | — |
| `scripts/diagnostics/probe_lib/push_rules.py` | diagnostics | `probe.py push-rules <branch>` — which GitHub ruleset a push of HEAD to a branch meets, and why. | imported by subcommands.py | `test_probe_push_rules.py` | — |
| `scripts/deploy_tools/reach.py` | deploy_tools | Which services a changed-path list reaches, answered in one module (#3660). | imported by broad.py, guard_map.py, land_platform.py, land_reach.py, land_shared.py, land_tags.py, staleness.py, tags.py | `test_reach.py` | — |
| `scripts/diagnostics/probe_lib/readonly_rbac.py` | diagnostics | `probe.py readonly-rbac` — is the read-only ServiceAccount still read-only? | imported by subcommands.py | `test_probe_readonly_rbac.py` | — |
| `scripts/deploy_tools/promote_lib/reads.py` | deploy_tools | Every fact `promote.py decide` reads, as `Reads`, and the live reads against the forge and git. | imported by decision.py, promote.py, reset.py, supersede.py | — | — |
| `scripts/deploy_tools/promote_lib/reboot_window.py` | deploy_tools | The weekly reboot's no-promotion window, for `decide`, `land-wait` and `promote.yml`'s cron. | imported by _promote_fakes.py, decision.py, reads.py, wait.py, wait_reads.py | `test_promote_reboot_window.py` | — |
| `scripts/dev/fanout_lib/review/red_gate.py` | dev | The red and green gates a `red-green` fan-out batch passes through (#3674). | imported by _review_fakes.py, api.py, delta_confirm.py, green_contest.py, hunk_check.py, review.py | `test_fanout_red_gate.py` | — |
| `scripts/dev/findings_lib/red_green.py` | dev | Which findings `open` labels `red-green`, so a `--review` fan-out gives them a red phase. | imported by findings.py, hunk_check.py, plans.py, red_gate.py | `test_findings_red_green.py` | — |
| `scripts/dev/fanout_lib/review/red_lock.py` | dev | What a fix may change in a red test file, and what it may delete from a conftest. | imported by red_gate.py | `test_fanout_red_lock.py` | — |
| `scripts/dev/fanout_lib/review/red_registry.py` | dev | What a fix may change in a patch registry the red commits touched (#4875). | imported by red_gate.py | — | — |
| `scripts/dev/fanout_lib/review/red_tests.py` | dev | The rules a red test is held to: why it failed and what it suppresses. | imported by hunk_check.py, red_gate.py, review.py | — | — |
| `scripts/lib/facts/relations.py` | lib | The status algebra: the spec's Datalog rules as set comprehensions over finite relations. | imported by fact_status.py, lock.py | — | — |
| `scripts/lib/release_bin_groups.py` | lib | Resolve which source files a `release_bin.yml` group deploys. | imported by cron_targets.py | — | — |
| `scripts/diagnostics/probe_lib/releases.py` | diagnostics | `probe.py releases` -- which commit produced the manifests each k8s service is running. | imported by health.py, land_rerolls.py, subcommands.py | `test_probe_releases.py` | — |
| `scripts/diagnostics/probe_lib/releases_consumers.py` | diagnostics | Which services a byte-supplying shared k8s role can actually make stale. | imported by releases.py | `test_probe_releases_consumers.py` | — |
| `scripts/diagnostics/probe_lib/releases_diff.py` | diagnostics | The staleness questions a path cannot answer: did this `tasks/` diff move any bytes? | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_format.py` | diagnostics | Renderers for `probe.py releases` -- the table, the cron-facing list and the Kuma push line. | imported by releases.py | — | — |
| `scripts/diagnostics/probe_lib/releases_render.py` | diagnostics | The render digest, which is `probe.py releases`' primary answer on staleness. | imported by releases.py | `test_probe_releases_render.py` | — |
| `scripts/diagnostics/probe_lib/releases_retired.py` | diagnostics | Release records for services the inventory no longer declares. | imported by health.py, releases.py, releases_consumers.py | — | — |
| `scripts/lib/facts/removed.py` | lib | Identifier-shaped tokens a range of commits removed, and whether the tree still holds them. | imported by evidence.py, lint.py | — | — |
| `scripts/lib/removes_object.py` | lib | The `Removes-Object:` line: a commit's declaration that it removes a cluster object on purpose. | imported by cross_plane_order.py, merge.py, reads.py | — | — |
| `scripts/infra_map/render.py` | infra_map | Rendering: turn the reconciled model into one self-contained HTML page. | imported by gen_infra_map.py | — | — |
| `scripts/lib/render_context.py` | lib | The variable context a role template renders with, in Ansible's precedence. | imported by applied.py, compose_templates.py, context.py, host.py, k8s_manifests.py, monitors.py, setup_census.py, setup_census_items.py, setup_templates.py, shell_templates.py, unit_templates.py | `test_render_context.py` | — |
| `scripts/lib/render_guard.py` | lib | Shared helpers for the render-guard scripts and other Ansible-inventory readers. | imported by _live_read_census.py, ansible_jinja_env.py, applied_facts.py, broad.py, catalog_backup.py, catalog_facts.py, cluster.py, compose_templates.py, containers.py, context.py, environment.py, estate.py, gen_role_glance.py, host.py, k8s_context.py, k8s_manifests.py, k8s_roles.py, k8s_yaml.py, land_tags.py, monitors.py, render_context.py, render_targets.py, restart.py, service_tiers.py, setup_templates.py, shared_role_callers.py, shed_set.py, shell_templates.py, tags.py, unit_templates.py, var_aliases.py | `test_render_guard.py` | — |
| `scripts/lib/renovate_annotations.py` | lib | The `# renovate:` annotations in the roles' defaults, read the way Renovate reads them. | imported by _renovate.py, asset_pins.py | `test_renovate_annotations.py` | — |
| `scripts/lib/facts/replay.py` | lib | The notice replay in ``fact_status.py report``: whether the diff notice lists every lock move. | imported by report.py | — | — |
| `scripts/lib/repo_paths.py` | lib | The repo path anchors a script under ``scripts/`` reads the Ansible tree through. | imported by _deploy_sh_fakes.py, _inventory_path_rules.py, _job_census.py, _land_wait_fakes.py, _live_read_census.py, _render_audit.py, _render_helper_rules.py, _renovate.py, alert_conventions.py, alert_rules.py, ansible_inventory.py, ansible_jinja_env.py, applied.py, applied_hosts.py, applied_tasks.py, asset_pins.py, await_ci.py, backups.py, bootstrap_case.py, broad.py, build_docs.py, builder.py, bundle.py, bundle_census.py, bundle_gates.py, catalog_backup.py, catalog_model.py, ci_contexts.py, classify.py, cluster.py, cluster_pins.py, cluster_vars.py, compose_templates.py, constants.py, consumers.py, context.py, core.py, cron_checks.py, cron_targets.py, cronjob_gate.py, crons.py, cross_plane_order.py, cross_plane_replay.py, decisions.py, deletion_drill.py, deployer_park.py, detach_notify.py, digest_covers.py, docs_provenance.py, estate.py, fact_status.py, fragment_readers.py, fragments_bridge.py, fragments_deploy.py, fragments_hosts.py, fragments_storage.py, freshness.py, function_imports.py, gate_runner.py, gen_doc_fragments.py, gen_gitops_markers.py, gen_hook_settings.py, gen_role_glance.py, gitops_state.py, gitops_view.py, gitops_why.py, glance_facts.py, grafana_dashboard_uids.py, ha.py, health.py, health_alerts.py, health_checkout.py, health_render.py, homelab_render.py, host.py, host_fixtures.py, host_packages.py, host_secrets.py, host_sources.py, host_wait.py, hosts.py, images.py, invocation_sites.py, jinja_bash_collisions.py, jinja_defaults.py, jinja_uses.py, k3s_binary_sum.py, k3s_upgrade.py, k8s_autodeploy_counts.py, k8s_role_callers.py, k8s_roles.py, k8s_schema.py, kuma_table_loop.py, land_reach.py, land_rerolls.py, land_shared.py, land_tags.py, land_tags_cli.py, lander.py, landing.py, landing_blockers.py, ledger.py, logic.py, longhorn_budget.py, longhorn_cluster.py, longhorn_dr.py, longhorn_upgrade.py, metallb_upgrade.py, monitors.py, namespace.py, narrow_setup.py, networking.py, new_k8s_service.py, notification_renders.py, options.py, ordering.py, outcome.py, owners.py, paths.py, pi_plane.py, pinned_rotation.py, placeholders.py, playbook.py, prepull.py, promote.py, promotion.py, publish_pr.py, pytest_shard.py, reach.py, reboot_window.py, refresh_vendored_schemas.py, releases.py, releases_consumers.py, releases_diff.py, releases_format.py, releases_render.py, render_guard.py, render_targets.py, renovate_branch_sweep.py, report.py, restart.py, review_metrics.py, role_installs.py, root_ignored_files.py, rotation_tools.py, roundtrip.py, route_facts.py, run.py, script_classify.py, scripts.py, secret_bearing_host_paths.py, secret_checks.py, secret_keys.py, secrets.py, service_catalog.py, service_tiers.py, setup_census.py, setup_census_items.py, setup_gates.py, setup_reach_hosts.py, setup_routing.py, setup_templates.py, shadow_diff.py, shared_role_callers.py, shared_role_reach.py, shell_templates.py, snapshot.py, snapshots.py, soak.py, sops_io.py, state.py, stateful.py, stdlib_only.py, tags.py, takeover.py, target.py, task_inputs.py, tighten_ratchets.py, tools.py, under_locks.py, validate_ha_config.py, var_aliases.py, vendor_manifest.py, wait.py, worktrees.py | — | — |
| `scripts/lib/facts/report.py` | lib | ``fact_status.py report``: whether ``docs/facts.lock`` pays for itself, read from git and the log. | imported by fact_status.py, replay.py | — | — |
| `scripts/validate/validate_lib/repos_case.py` | validate | The container case for dpkg taking over the apt sources and keys the roles wrote (H7.24a). | imported by host_package_cases.py | `test_repos_case.py` | — |
| `scripts/deploy_tools/promote_lib/reset.py` | deploy_tools | `promote.py reset-check`: whether a `reset_to_last_good` dispatch moves `prod` back. | imported by promote.py | `test_promote_reset.py` | — |
| `scripts/renderer/renderer_lib/restart.py` | renderer | `checksum/restart`: the pod annotation that rolls a flux-owned workload (target slice D3.3c). | imported by homelab_render.py, prune_label.py | `test_renderer_restart.py` | — |
| `scripts/dev/fanout_lib/review/review.py` | dev | The review pipeline a `launch --review` batch runs in place of a single `claude -p`. | imported by _review_fakes.py, api.py, review_ci.py | `test_fanout_review.py` | — |
| `scripts/dev/fanout_lib/review/review_ci.py` | dev | The review pipeline's CI wait: `cc-wait gh-ci` run by the pipeline before its PR lands. | imported by review.py, review_prompts.py | `test_fanout_review_ci.py` | — |
| `scripts/dev/fanout_lib/review/review_land.py` | dev | The review pipeline's landing: `land.sh` run by the pipeline, the model resumed only on need. | imported by api.py, review.py, review_ci.py | `test_fanout_review_land.py` | — |
| `scripts/dev/fanout_lib/review/review_prompts.py` | dev | What the review pipeline asks each phase it resumes or starts, and the reviewer's schema. | imported by review.py, review_ci.py, review_record.py | — | — |
| `scripts/dev/fanout_lib/review/review_record.py` | dev | What one `--review` batch records: its phases, the findings, and the PR comment built from them. | imported by api.py, green_contest.py, review.py, review_ci.py, review_prompts.py | — | — |
| `scripts/dev/fanout_lib/review_stats.py` | dev | Sum the fan-out review records into the measures that decide whether the red phase stays. | no `__main__` guard; imported by test_fanout_review_stats.py | `test_fanout_review_stats.py` | — |
| `scripts/dev/fanout_lib/review_unit.py` | dev | Run a `launch --review` fan-out batch: implement, review, fix, then land. | no `__main__` guard; imported by test_fanout_git_state.py, test_fanout_review.py | — | — |
| `scripts/lib/role_installs.py` | lib | Where a host installs each role script, and which of them a host runs `--no-project`. | imported by stdlib_only.py | — | — |
| `scripts/secrets_mgmt/rotation_tools.py` | secrets_mgmt | Every process boundary `secret_rotation.py` crosses, as one injectable object. | imported by _rotation_fakes.py, consumers.py, git_dates.py, host_secrets.py, secret_registry.py, secret_rotation.py, secrets.py | `test_rotation_tools.py` | — |
| `scripts/renderer/renderer_lib/roundtrip.py` | renderer | The round trip: build each placeholder directory and its stub twin with `flux build`, then compare. | imported by flux_probe.py, shadow_diff.py | `test_renderer_roundtrip.py` | — |
| `scripts/docs/catalog_lib/route_facts.py` | docs | Shared route facts for the reference generators. | imported by catalog_facts.py, catalog_render.py, networking.py | `test_route_facts.py` | — |
| `scripts/deploy_tools/deploy_lib/run.py` | deploy_tools | Run an interactive Ansible deploy under the locks the automated deployers take. | imported by _deploy_sh_fakes.py, deploy_cli.py | — | — |
| `scripts/lib/script_classify.py` | lib | How every first-party script under ``scripts/`` is run, derived from the tree. | imported by scripts.py | — | — |
| `scripts/lib/script_imports.py` | lib | Which scripts under ``scripts/`` import which, read from the source without running it. | imported by function_imports.py, script_classify.py | `test_script_imports.py` | — |
| `scripts/renderer/renderer_lib/secret_checks.py` | renderer | The renderer's secret checks: the five rules every placeholder render must pass (target slice R2). | imported by homelab_render.py | `test_renderer_secret_checks.py` | — |
| `scripts/secrets_mgmt/secrets_lib/secret_classify.py` | secrets_mgmt | Which rotation tier a secret's NAME puts it in. | imported by secret_registry.py | `test_secret_classify.py` | — |
| `scripts/deploy_tools/narrow_lib/secret_keys.py` | deploy_tools | Which deploy tags a `secrets.yml` change reaches: the consumers of each key it moved. | imported by broad.py, narrow_setup.py, secret_setup_readers.py | `test_narrow_secret_keys.py` | — |
| `scripts/secrets_mgmt/secrets_lib/secret_registry.py` | secrets_mgmt | Pure logic over the rotation registry: seeding, reconciliation, due dates and drift. | imported by secret_rotation.py, secrets.py | `test_secret_registry.py` | — |
| `scripts/validate/validate_lib/secrets_case.py` | validate | The container case for a package that carries `secrets-unit` (H4.1c). | imported by host_package_cases.py | `test_secrets_case.py` | — |
| `scripts/backup/longhorn_reap_lib/selection.py` | backup | The two operator-selected Backup CR sets: a migrated volume's old chain, and retired seeds. | imported by backups.py | — | — |
| `scripts/lib/service_tiers.py` | lib | The sets derived from containers_list ``tier``, for readers outside Ansible. | imported by catalog_backup.py, fragments_storage.py, gen_doc_fragments.py, shed_set.py | — | — |
| `scripts/deploy_tools/setup_lib/setup_census_items.py` | deploy_tools | The items the setup roles still own, derived from the tree (H7.2f). | imported by setup_census.py | — | — |
| `scripts/deploy_tools/setup_lib/setup_gates.py` | deploy_tools | Read a playbook entry's `when:` for one host, the way Ansible would. | imported by applied.py, applied_hosts.py, glance_facts.py, land_reach.py, setup_census.py, setup_routing.py | — | — |
| `scripts/deploy_tools/narrow_lib/setup_index.py` | deploy_tools | One setup role's task files, templates and vars, read at a single git ref. | imported by foreign_keys.py, land_reach.py, narrow_setup.py, secret_keys.py | — | — |
| `scripts/deploy_tools/narrow_lib/setup_playbook.py` | deploy_tools | What a playbook and a task list say about tags, without touching git. | imported by narrow_setup.py, setup_index.py | — | — |
| `scripts/deploy_tools/setup_lib/setup_role_chains.py` | deploy_tools | Reading a setup role's `tasks/` tree for the `when:` chains a changed file sits behind. | imported by land_reach.py, setup_routing.py | — | — |
| `scripts/renderer/renderer_lib/shadow.py` | renderer | Read two render trees and compare them: the library behind `shadow_diff.py`. | imported by _render_audit.py, bundle_gates.py, shadow_diff.py | — | — |
| `scripts/validate/validate_lib/shadow_case.py` | validate | The container case for a packaged unit that an Ansible-era copy under /etc shadows (H7.41a). | imported by host_package_cases.py | — | — |
| `scripts/dev/shard_weight_gate.py` | dev | The measured shard-weight gate: what CI's `pytest_shard.py --check-durations` step decides. | imported by pytest_shard.py | — | — |
| `scripts/deploy_tools/shared_role_reach.py` | deploy_tools | Which changed SHARED k8s roles move no rendered manifest, so no hand has to apply them. | imported by land_tags_cli.py, tools.py | `test_shared_role_reach.py` | — |
| `scripts/diagnostics/probe_lib/shed_set.py` | diagnostics | `probe.py shed-set` — the k8s workloads to scale down when one node is lost. | imported by subcommands.py | `test_probe_shed_set.py` | — |
| `scripts/validate/validate_lib/shell_lint.py` | validate | Render a Jinja-templated shell script, then lint the output with `bash -n` and shellcheck. | imported by shell_templates.py | — | — |
| `scripts/dev/fanout_lib/signing.py` | dev | The signing-key gate: refuse a placement host whose commit signatures GitHub rejects. | imported by place.py, transport.py | `test_fanout_signing.py` | — |
| `scripts/renderer/renderer_lib/snapshot.py` | renderer | The `<tag>-snapshot` directory: one Job that snapshots a role's claims before `<tag>` applies. | imported by cronjob_gate.py, homelab_render.py | `test_renderer_snapshot.py` | — |
| `scripts/backup/longhorn_reap_lib/snapshots.py` | backup | Reap Longhorn Snapshots left stranded by a tier move. | imported by longhorn_reap.py | — | — |
| `scripts/renderer/renderer_lib/soak.py` | renderer | The shared kind `soak`: one Job per revision that judges the services the revision changed. | imported by bundle.py, homelab_render.py | `test_renderer_soak.py` | — |
| `scripts/lib/socket_testing.py` | lib | Give a test a unix socket path the kernel accepts, and a listener that cannot leak. | no `__main__` guard; imported by test_fanout_clean_chain.py, test_socket_testing.py, test_worktree_privileged_holders.py | `test_socket_testing.py` | — |
| `scripts/dev/findings_lib/solo_only.py` | dev | Which issues are solo-only: they cite the fan-out tooling, so no fan-out batch takes them. | imported by findings.py, launch_gates.py | — | — |
| `scripts/renderer/renderer_lib/sops_entries.py` | renderer | The `sops-manifest-entries` check (target slice R2). | imported by cluster_vars.py, homelab_render.py, secret_checks.py | — | — |
| `scripts/secrets_mgmt/sops_io.py` | secrets_mgmt | The two SOPS-facing helpers that import no other secrets_mgmt module. | imported by cluster_vars.py, rotation_tools.py, secret_rotation.py | — | — |
| `scripts/deploy_tools/land_lib/stale_retarget.py` | deploy_tools | Step 5, when `deploy.sh --at <merge sha>` exits 4: deploy the newest green descendant instead. | imported by deploy.py | — | — |
| `scripts/deploy_tools/promote_lib/stateful.py` | deploy_tools | D5's stateful hold: the no-reset rule in `decide`, and the post `promote.py hold` sends. | imported by decision.py, promote.py | — | — |
| `scripts/dev/fanout_lib/status.py` | dev | Read every batch on one host in one call, and stop one. | imported by abandon.py, launch_gates.py, notify.py, place.py, review.py, review_ci.py, review_record.py, wait_probe.py | `test_fanout_status.py` | — |
| `scripts/lib/stdlib_only.py` | lib | The modules a no-venv caller reaches, and the two checks that they import with the stdlib alone. | no `__main__` guard; imported by test_stdlib_only.py | `test_stdlib_only.py` | — |
| `scripts/deploy_tools/promote_lib/storage_rollout.py` | deploy_tools | `verify` during a Longhorn hop: when another name's failure is provisional, not final. | imported by verify_promotion.py | — | — |
| `scripts/infra_map/style.py` | infra_map | The page's stylesheet, its status vocabulary, and the escape every view calls. | imported by diagram.py, html_views.py, render.py | — | — |
| `scripts/diagnostics/probe_lib/subcommands.py` | diagnostics | The `SUBCOMMANDS` table and the `REGISTRY` built from it: probe.py's dispatch and `--list`. | imported by probe.py | — | — |
| `scripts/deploy_tools/promote_lib/supersede.py` | deploy_tools | `decide` promoting T over a verification in flight: when it may, and against what (D7.5). | imported by promote.py | `test_promote_supersede.py` | — |
| `scripts/deploy_tools/promote_lib/superseded.py` | deploy_tools | `verify` after a later run moved `prod` past T: the superseded verdict (slice D7.2). | imported by verify_promotion.py | — | — |
| `scripts/renderer/renderer_lib/suspension.py` | renderer | The rule against `suspend: false` on a Flux object (C3 *Suspension*, D5.1b). | imported by bundle.py, cluster.py | — | — |
| `scripts/lib/systemd_timer.py` | lib | A systemd timer's triggers read from its unit text, and a daily `OnCalendar=` expanded. | imported by host_reader.py, host_schedule.py, host_wait.py, lander.py | — | — |
| `scripts/deploy_tools/deploy_lib/tags.py` | deploy_tools | Validate the --tags a deploy was given, before Ansible silently accepts them. | imported by deploy_cli.py, detach_notify.py, land_shared.py, land_tags.py, releases.py, releases_consumers.py, run.py, staleness.py | `test_deploy_tags.py` | — |
| `scripts/diagnostics/probe_lib/takeover.py` | diagnostics | `probe.py takeover <tag>` — the fields Flux's first apply of a tag strips (D5.1g). | imported by subcommands.py, takeover_census.py | `test_probe_takeover.py` | — |
| `scripts/dev/fanout_lib/target.py` | dev | Which repo a batch works: its register, its primary checkout and the branch it merges to. | imported by _fanout_fakes.py, _review_fakes.py, abandon.py, autoland.py, batch_holds.py, brief.py, claim_cli.py, claims.py, clean.py, launch.py, manifest.py, notify.py, place.py, review.py, review_ci.py, review_unit.py, status.py, transport.py | `test_fanout_target.py` | — |
| `scripts/renderer/renderer_lib/task_inputs.py` | renderer | The values Ansible computes in role tasks that the renderer must supply itself. | imported by _live_read_census.py, bundle_census.py, cluster.py, gen_role_glance.py, images.py, restart.py | `test_renderer_task_inputs.py` | — |
| `scripts/deploy_tools/narrow_lib/task_walk.py` | deploy_tools | What one parsed task list says about tags: the files it pulls in and the tags that select it. | imported by foreign_keys.py, narrow_setup.py, setup_index.py | — | — |
| `scripts/deploy_tools/narrow_lib/templates.py` | deploy_tools | Two importer edges `narrow_broad.template_importers` must not take at face value. | imported by broad.py, digest_covers.py, filters.py, releases_render.py, tags.py | — | — |
| `scripts/deploy_tools/land_lib/tick.py` | deploy_tools | Step 4, the GitOps tick, retried while the unit's own flock gives up. | imported by deploy.py, health_verdict.py, pipeline.py | `test_land_tick.py` | — |
| `scripts/deploy_tools/land_lib/tools.py` | deploy_tools | Every process boundary a landing crosses, as one injectable object. | imported by _land_fakes.py, host_wait.py, land.py, landing.py | `test_land_tools.py` | — |
| `scripts/dev/findings_lib/tracked_paths.py` | dev | Resolve a path an issue cites relative to some directory to the tracked file it names. | imported by launch_gates.py, place.py, red_green.py | — | — |
| `scripts/dev/fanout_lib/transport.py` | dev | The transport seam: every process boundary the dispatcher crosses, as one injectable object. | imported by _fanout_fakes.py, abandon.py, autoland.py, batch_holds.py, claims.py, clean.py, headroom_probe.py, launch.py, notify.py, place.py, review_unit.py, status.py | — | — |
| `scripts/deploy_tools/deploy_lib/under_locks.py` | deploy_tools | The locked half of a foreground deploy: tree lock, snapshot, service locks, playbook. | imported by detach_run.py, run.py | — | — |
| `scripts/dev/fanout_lib/unit_state.py` | dev | The read of a batch's systemd unit state, which rides a connection another read opened. | imported by status.py, transport.py | — | — |
| `scripts/renderer/renderer_lib/var_aliases.py` | renderer | Rule 5's second alias clause, over role defaults and inventory, and both clauses' members. | imported by secret_checks.py | — | — |
| `scripts/deploy_tools/land_lib/verdict_log.py` | deploy_tools | Every landing's verdict, one JSON line per landing, in a file that outlives the session. | imported by landing_verdicts.py, tools.py | `test_land_verdict_log.py` | — |
| `scripts/dev/findings_lib/verify.py` | dev | Verify-by: the prose an issue stores about how to check it, and how `verify` reports it. | imported by findings.py | `test_findings_verify.py` | — |
| `scripts/deploy_tools/promote_lib/verify_handoff.py` | deploy_tools | The files `decide` hands `verify`: both bundles and the record of `prod`'s statuses. | imported by verify_promotion.py | `test_verify_handoff.py` | — |
| `scripts/deploy_tools/promote_lib/verify_promotion.py` | deploy_tools | `promote.py verify T` and the rest of contract C2's *Verification*. | imported by _land_wait_fakes.py, decision.py, object_loss.py, promote.py, supersede.py | `test_verify_promotion.py` | — |
| `scripts/diagnostics/probe_lib/vip_placement.py` | diagnostics | `probe.py vip-placement` — does every ETP=Local VIP have a Ready endpoint on an announcing node? | imported by subcommands.py | `test_probe_vip_placement.py` | — |
| `scripts/deploy_tools/promote_lib/wait.py` | deploy_tools | `promote.py land-wait`: wait until a promotion covers a merge commit. | imported by _land_fakes.py, _land_wait_fakes.py, land_flux.py, promote.py, reads.py, wait_reads.py | — | — |
| `scripts/dev/fanout_lib/wait_probe.py` | dev | The `fanout` source for cc-wait: a fan-out run's batches, read through `fanout.py place status`. | no `__main__` guard; imported by test_fanout_probe.py | — | — |
| `scripts/deploy_tools/promote_lib/wait_reads.py` | deploy_tools | The live reads `promote.py land-wait` makes, against the forge, the host and the checkout. | imported by land_flux.py, promote.py | — | — |
| `scripts/lib/worktree_label.py` | lib | The `.claim` label a session writes into a worktree whose name Claude Code generated. | imported by label_worktree.py, prune_worktrees.py | `test_worktree_label.py` | — |
| `scripts/dev/worktree_orphans.py` | dev | Judge the directories under .claude/worktrees/ that git no longer registers. | imported by prune_worktrees.py | — | — |
| `scripts/lib/worktree_owner.py` | lib | Which Unix user's clone a worktree branch belongs to, read from the branch name alone. | imported by claim.py, target.py | — | — |
| `scripts/dev/fanout_lib/review/worktree_reset.py` | dev | The reset that returns a red batch's worktree to one commit. | imported by review.py | — | — |
| `scripts/lib/worktrees.py` | lib | What a Claude session worktree is, and whether it is done with: the worktree library. | imported by _findings_fakes.py, boundaries.py, claim.py, clean.py, place.py, prune_worktrees.py, target.py, transport.py, worktree_orphans.py | `test_worktrees.py` | — |
| `scripts/lib/yaml_fast.py` | lib | `yaml.safe_load` backed by libyaml, which parses the same schema an order faster. | imported by _job_census.py, _land_wait_fakes.py, _live_read_census.py, _sops_type_proof.py, alert_conventions.py, applied.py, applied_hosts.py, applied_tasks.py, asset_pins.py, atoms.py, bootstrap_drill.py, bundle_census.py, bundle_gates.py, ci_contexts.py, cluster.py, cluster_pins.py, cluster_vars.py, cluster_vars_check.py, compose_templates.py, consumers.py, context.py, cron_targets.py, cronjob_gate.py, crons.py, cross_plane_order.py, deletion_drill.py, digest_covers.py, digest_provable.py, flux_probe.py, foreign_keys.py, forward_only.py, fragment_readers.py, fragments_hosts.py, git_dates.py, glance_facts.py, ha_state_checks.py, ha_state_model.py, health_plumbing.py, health_vendored.py, homelab_render.py, host.py, host_compose.py, host_packages.py, host_secrets.py, images.py, inventory.py, invocation_sites.py, jinja_defaults.py, k3s_binary_sum.py, k8s_autodeploy_counts.py, k8s_role_callers.py, k8s_roles.py, kuma_live.py, land_reach.py, lander.py, landing_blockers.py, longhorn_upgrade.py, monitors.py, namespace.py, narrow_git.py, object_loss.py, order_only.py, ordering.py, package_roots.py, pinned_rotation.py, placeholders.py, prepull.py, reboot_window.py, release_bin_groups.py, render_guard.py, restart.py, role_installs.py, rotation_tools.py, roundtrip.py, route_facts.py, secret_bearing_host_paths.py, setup_census_items.py, setup_gates.py, setup_index.py, setup_playbook.py, setup_role_chains.py, setup_routing.py, setup_templates.py, shadow.py, shadow_diff.py, shell_templates.py, snapshot.py, soak.py, sops_entries.py, state.py, stateful.py, suspension.py, takeover.py, task_inputs.py, task_walk.py, validate_ha_config.py, var_aliases.py, vendor_manifest.py, verify_promotion.py | `test_yaml_fast.py` | — |

## Run by hand

35 script(s) — a person runs it.

| Script | Directory | What it does | Reached by | Tests | Exit codes |
|---|---|---|---|---|---|
| `scripts/dev/fanout_lib/autoland.py` | dev | Land the PRs of finished daniel-server fan-out batches that are safe to land unattended (#5496). | no automated caller in the tree | `test_fanout_autoland.py` | — |
| `scripts/backup/b2_drain.py` | backup | Delete a stranded Longhorn backup prefix directly through the B2 API. | playbook: ansible/prune_backups.yml | `test_b2_drain.py` | [0, 1, 2, 64](#scriptsbackupb2drainpy) |
| `scripts/dev/cross_plane_replay.py` | dev | Replay the cross-plane gate over the master commits since a date (D4 Open question 9). | no automated caller in the tree | `test_cross_plane_replay.py` | — |
| `scripts/grafana/export_grafana_dashboards.py` | grafana | Export the *customized* Grafana dashboards from the live DB into code. | no automated caller in the tree | `test_export_grafana_dashboards.py` | — |
| `scripts/dev/fanout_place.py` | dev | Forwarding shim: `fanout_place.py <args>` runs `fanout.py place <args>` (#4346). | clean.py (a person runs it) | — | — |
| `scripts/dev/fanout_probe.py` | dev | Forwarding shim: `fanout_probe.py <args>` runs `fanout.py probe <args>` (#4346). | no automated caller in the tree | `test_fanout_probe.py` | — |
| `scripts/dev/fanout_review.py` | dev | Forwarding shim: `fanout_review.py <args>` runs `fanout.py review <args>` (#4346). | no automated caller in the tree | `test_fanout_review.py` | — |
| `scripts/dev/fanout_review_stats.py` | dev | Forwarding shim: `fanout_review_stats.py <args>` runs `fanout.py stats <args>` (#4346). | no automated caller in the tree | `test_fanout_review_stats.py` | — |
| `scripts/grafana/fetch_grafana_dashboards.py` | grafana | Fetch + adapt Grafana community dashboards for headless (provisioned) use. | no automated caller in the tree | `test_fetch_grafana_dashboards.py` | — |
| `scripts/diagnostics/flux_wait.py` | diagnostics | The `flux` source for cc-wait: one Flux Kustomization, Ready at an expected commit. | no automated caller in the tree | — | — |
| `scripts/dev/gen_gitops_markers.py` | dev | Copy the deployer's marker modules into monitor-bridge, the one tree that cannot ship them. | no automated caller in the tree | — | — |
| `scripts/deploy_tools/gitops_tick.sh` | deploy_tools | trigger a GitOps deploy tick by hand and report what it did. | no automated caller in the tree | — | [0, 1, 2, 3, 4, 64, 75](#scriptsdeploytoolsgitopsticksh) |
| `scripts/secrets_mgmt/host_secrets.py` | secrets_mgmt | Per-host SOPS files: `ansible/vars/hosts/<host>.sops.yaml`, each copied from secrets.yml. | no automated caller in the tree | `test_host_secrets.py` | — |
| `scripts/dev/label_worktree.py` | dev | Label this worktree with what it works on, for a worktree whose name Claude Code chose. | no automated caller in the tree | `test_label_worktree.py` | — |
| `scripts/deploy_tools/land_lib/land_probe.py` | deploy_tools | The `land` source for cc-wait: one detached landing's state, read from its log. | no automated caller in the tree | `test_land_probe.py` | — |
| `scripts/deploy_tools/land_lib/land_tags_cli.py` | deploy_tools | Print what `land.sh` derives from a PR's file list -- tags, plane note, or self-applied flag. | no automated caller in the tree | `test_land_tags_cli.py` | — |
| `scripts/backup/longhorn_reap.py` | backup | Delete Longhorn Backup and Snapshot objects that no RecurringJob will ever prune. | backups.py (a person runs it) | — | — |
| `scripts/dev/new_k8s_service.py` | dev | Scaffold a k3s service role from its name, image and port, instead of copying a sibling. | no automated caller in the tree | `test_new_k8s_service.py` | — |
| `scripts/diagnostics/postflight.py` | diagnostics | Verify the post-deploy setup that Ansible can't do (ansible/README.md §9). | playbook: ansible/bring-up.sh | `test_postflight.py` | — |
| `scripts/dev/pr_changed_files.sh` | dev | List the files a pull request changes, and say whether its scoped CI run can be trusted. | no automated caller in the tree | — | — |
| `scripts/validate/refresh_vendored_schemas.py` | validate | Re-download the vendored JSON schemas this repo checks rendered config against. | no automated caller in the tree | `test_refresh_vendored_schemas.py` | — |
| `scripts/dev/renovate_branch_sweep.py` | dev | Census the `renovate/*` branches no open PR and no Dependency Dashboard entry speaks for. | no automated caller in the tree | `test_renovate_branch_sweep.py` | [0, 2, 64](#scriptsdevrenovatebranchsweeppy) |
| `scripts/validate/renovate_config.sh` | validate | Validate renovate.json against a freshly-resolved Renovate, riding out npm's publish window. | no automated caller in the tree | — | — |
| `scripts/dev/renovate_rebase.py` | dev | Tick the rebase checkbox in a Renovate PR's body so Renovate refreshes the branch. | no automated caller in the tree | `test_renovate_rebase.py` | [0, 1, 2, 3, 64](#scriptsdevrenovaterebasepy) |
| `scripts/dev/review_metrics.py` | dev | Print the /homelab-review outcome trend: false-positive and fix-refusal rates. | no automated caller in the tree | `test_review_metrics.py` | — |
| `scripts/dev/run_as_cron.sh` | dev | run a command in the environment cron actually gives it. | no automated caller in the tree | `test_run_as_cron.py` | — |
| `scripts/deploy_tools/runbook_gates.py` | deploy_tools | Run one runbook's stop conditions in order, exit code naming the first failure. | etcd_restore.py (a person runs it) | — | [0, 64, 69](#scriptsdeploytoolsrunbookgatespy) |
| `scripts/secrets_mgmt/secret_bearing_host_paths.py` | secrets_mgmt | Deployed host paths whose content embeds a credential, derived from the tree. | held_hooks.py (a person runs it) | `test_secret_bearing_host_paths.py` | — |
| `scripts/z2m/set_device_option.sh` | z2m | set one Zigbee2MQTT device option over MQTT and confirm it applied. | no automated caller in the tree | `test_set_device_option.py` | — |
| `scripts/deploy_tools/setup_lib/setup_census.py` | deploy_tools | Print the items the setup roles still own on each host, and where each must go (H7.2f). | no automated caller in the tree | — | — |
| `scripts/dev/split_module.py` | dev | Split a large Python module along its seams: show the references, then move names by spec. | no automated caller in the tree | `test_split_module.py` | — |
| `scripts/renderer/standalone_map.py` | renderer | Write the standalone-value guard's key map from a placeholder render (target slice R2). | no automated caller in the tree | — | — |
| `scripts/diagnostics/ui_login.py` | diagnostics | Mint a Playwright storage-state file holding a logged-in Authelia session. | ui_mcp.sh (a person runs it) | `test_ui_login.py` | — |
| `scripts/diagnostics/ui_mcp.sh` | diagnostics | Launch @playwright/mcp against this homelab's LAN routes. | no automated caller in the tree | — | — |
| `scripts/dev/vendor_manifest.py` | dev | Vendor an upstream manifest into a role, pinned to the sha256 of the bytes it fetched. | no automated caller in the tree | `test_vendor_manifest.py` | — |

## Exit codes

19 entry point(s) declare a contract in `scripts/lib/exit_codes.py`, which is where these tables are rendered from. Every other script here exits 0 or non-zero and says nothing more; 64 is a usage error and 75 a temporary failure wherever they appear, because each family names the same spine.


### `scripts/deploy.sh`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `DEPLOY_OK` | deployed, and the play reached PLAY RECAP naming a host. | — |
| 2 | `DEPLOY_TAG_MISS` | a --tags value matched no service in containers_list, so NOTHING was deployed. | --list-services prints every valid value. |
| 3 | `DEPLOY_BROAD` | the change is broad (shared templates, inventory, the setup plane) and maps to no single service, so NOTHING was deployed. | --changed refuses it by design; apply the plane by hand. `deploy_tags.py narrow <old> <new>` prints, read-only, the services the tick would map the range to. |
| 4 | `DEPLOY_STALE` | the tree is behind origin/master, so NOTHING was deployed. | A stale tree renders stale templates and reverts live config while every repo-side check still reads green. Pull first; never --skip-staleness-check. Staleness is decided before --tags is validated, so a stale tree carrying a tag it does not know yet (a new role's first landing) reports this, not a tag miss. |
| 5 | `DEPLOY_FLUX_OWNED` | the tag is applied by Flux through `promote`, so NOTHING was deployed. | Its containers_list entry carries `owner`, so no Ansible path may apply it. Merge the change and let `promote` publish it; drop the tag to deploy the rest. |
| 20 | `DEPLOY_PLAYBOOK_FAILED` | the playbook RAN and a task failed, so this is the one deploy exit where changes ARE live — everything applied before the failing task took effect. | Read the PLAY RECAP and the failing TASK. Do not treat it as a tag, staleness or lock refusal, and do not assume a re-run is safe. |
| 64 | `DEPLOY_BAD_FLAGS` | the command line is wrong, so NOTHING was deployed and a retry changes nothing. | Read the usage above; fix the flags rather than re-running. |
| 75 | `DEPLOY_LOCK_BUSY` | a deploy lock stayed busy, so NOTHING was deployed — either the git-tree lock (deploy_locks.TREE_LOCK) or one of this run's own /var/lock/server-deploy-<tag>.lock files. | The GitOps timer or another session holds it. This is a resume point, not a playbook failure — re-run the same command shortly. |
| 76 | `DEPLOY_LOCK_UNAVAILABLE` | flock failed on the lock file ITSELF, so NOTHING was deployed. This is not contention — no deploy holds the lock. | Check that the git-tree lock file (deploy_locks.TREE_LOCK) exists and is writable by this user; retrying alone changes nothing. |
| 77 | `DEPLOY_SNAPSHOT_FAILED` | the snapshot worktree could not be created, so NOTHING was deployed. | The playbook renders from a detached worktree of HEAD under /tmp/homelab-deploy-snapshots; the message above carries the failing command's own stderr (the `fatal:` line), so fix what it names — retrying changes nothing. |
| 78 | `DEPLOY_NO_HOSTS` | the playbook matched NO host, so NOTHING was deployed. | ansible exits 0 for a run where no play matched, so the wrapper reads the PLAY RECAP itself. Read the [WARNING] lines above — an inventory that failed to parse, or a host pattern that matched nothing — fix that, then re-run. |
| 79 | `DEPLOY_LOCK_PLAN_FAILED` | `deploy_locks.py plan` did not print this run's service locks, so the wrapper had nothing to take and NOTHING was deployed. | It never falls back to a lock order of its own. Run `uv run python ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_locks.py plan <tag>` by hand to see why, fix that, then re-run; nothing was held while it ran. |

### `scripts/deploy_tools/land.sh`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `LAND_SETTLED` | deployed and settled, or there was nothing to deploy. | — |
| 1 | `LAND_FAILED` | CI red or unproven, blocked by a change needing a hand, the deploy failed, the health gate failed, or the PR was closed without merging, conflicts with master, or its own CI is red. | — |
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
| 2 | `CI_DISARMED` | the CI gate is disarmed and checks nothing. | — |
| 4 | `CI_UNPROVEN` | no CI verdict can ever arrive for the SHA. | — |
| 75 | `CI_PENDING` | the budget elapsed with checks still running. | — |

### `scripts/deploy_tools/publish_pr.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `PUBLISH_PUBLISHED` | `publish`: pushed and a PR is open. | — |
| 1 | `PUBLISH_STILL_LOCAL` | `publish`: the commit is still local and there is nothing to clean up on origin. `unlanded`: origin was unreadable. | — |
| 2 | `PUBLISH_PUSHED_NO_PR` | `publish`: pushed but no PR was opened. `unlanded`: a PR is open. | — |
| 3 | `UNLANDED_NO_PR` | `unlanded`: a branch with no PR. | — |
| 4 | `UNLANDED_PR_RED` | `unlanded`: its PR failed a check. | — |

### `scripts/deploy_tools/narrow_setup.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `NARROW_SETUP_NARROWED` | printed the comma-separated tags the range needs from the role. | — |
| 1 | `NARROW_SETUP_REFUSED` | refused to narrow, so the caller applies the whole role tag. | stderr names the path or tag that made the derivation uncertain. |
| 5 | `NARROW_SETUP_NOTHING` | the range reaches no host through the role, so the role needs no tag at all; stdout is `narrowed-to-nothing`. | — |

### `scripts/diagnostics/probe.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `PROBE_OK` | the probe read its source and observed nothing wrong. | — |
| 1 | `PROBE_FAILED` | the probe read its source and observed a bad verdict. | The lines above the verdict name what failed. |
| 64 | `PROBE_USAGE_ERROR` | an argument the probe cannot act on, so it read nothing. | stderr names the argument and the forms it accepts. Fix the invocation; a retry of the same one never succeeds. |
| 75 | `PROBE_UNREADABLE` | the probe could not read its source, so it observed nothing. | stderr carries the failed read. Retry once the source answers; this is not a verdict about the thing probed. |

### `scripts/dev/fanout_lib/place.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `FANOUT_PLACE_OK` | `launch`: every batch launched with every issue claimed. `status`: every batch is running, done, landed or cleaned. | — |
| 1 | `FANOUT_PLACE_FAILED` | `launch`: refused before placing (a claim holder, a live batch, an issue fetch or label, a solo-only or shared-file issue), or a batch failed to launch. `status`: a batch needs a hand (needs-input, no-pr, no-verdict, no-report). | stderr names the refusal; for `status`, read the batch's line. |
| 3 | `FANOUT_PLACE_NOTHING_PLACED` | `launch` placed nothing: no host had headroom, the ssh budget or the live-batch cap refused, or every issue's claim was refused. | — |
| 4 | `FANOUT_PLACE_NO_HOST_READABLE` | no host's headroom could be read. | — |
| 5 | `FANOUT_PLACE_BATCH_FAILED` | `status`: a batch failed: its unit exited non-zero or its session set is_error. | Read the stderr tail on the batch's line. |
| 6 | `FANOUT_PLACE_NO_SIGNING_HOST` | no candidate host signs commits GitHub verifies, or the registered signing keys could not be read. | — |
| 7 | `FANOUT_PLACE_PARTLY_LAUNCHED` | `launch` started batches, but at least one issue's claim was refused and dropped. | Each `dropped` line on stderr names an issue no batch carries. |
| 64 | `FANOUT_PLACE_BAD_ARGS` | the command line is wrong: a malformed --batch, an unregistered --repo, or a run-id with no manifest. | — |
| 75 | `FANOUT_PLACE_UNREAD` | `status`: a host's read timed out and no batch reported anything worse. | Run `status` again; the batches on that host may still be running. |

### `scripts/docs/build_docs.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `BUILD_DOCS_OK` | every generator ran and the site built. | — |
| 1 | `BUILD_DOCS_CRASHED` | build_docs itself crashed. | — |
| 2 | `BUILD_DOCS_GENERATOR_FAILED` | one or more generators failed, so the published pages are incomplete. It wins over 3 when both halves fail. | — |
| 3 | `BUILD_DOCS_SITE_FAILED` | every generator ran but the site build failed, so the docs site still serves its previous build. | — |
| 64 | `BUILD_DOCS_BAD_ARGS` | the command line is wrong. | — |

### `scripts/dev/findings.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `FINDINGS_OK` | done. | — |
| 1 | `FINDINGS_FAILED` | gh failed, or `reap` refused a git read failure rather than call it "nothing is claimed". | — |
| 3 | `FINDINGS_REFUSED` | nothing was written because the issue refuses it: closed, manual, deferred, outside the register, held by another worktree, not claimed, or lost a race. | — |
| 64 | `FINDINGS_BAD_ARGS` | the command line is wrong, including a --worktree name the claim trailer could not carry. | — |

### `scripts/dev/fact_status.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `FACT_STATUS_OK` | every section read is IN, the write is done, or `notice` ran. | — |
| 1 | `FACT_STATUS_OUT` | a section is OUT, a lint finding blocks, or `reverify` wrote the lock. | — |
| 64 | `FACT_STATUS_BAD_ARGS` | the command line is wrong. | — |
| 2 | `FACT_STATUS_BAD_REF` | `notice` found no merge base. | — |

### `scripts/deploy_tools/runbook_gates.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `RUNBOOK_GATES_OK` | every gate run passed. | — |
| 64 | `RUNBOOK_GATES_BAD_ARGS` | the command line is wrong, including a missing or unknown runbook. | — |
| 69 | `RUNBOOK_GATES_UNAVAILABLE` | the cluster could not be asked, so no gate graded anything. | Check that kubectl is installed and the kubeconfig names the production cluster, then re-run. |

### `scripts/secrets_mgmt/secret_rotation.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `SECRET_ROTATION_OK` | done. | — |
| 1 | `SECRET_ROTATION_FAILED` | the registry is out of sync under --check, --push pushed down, or a --deploy redeploy failed after the new tokens were written. | — |
| 3 | `SECRET_ROTATION_SOPS_FAILED` | a `sops set` failed or timed out partway through the rotate batch. | — |
| 64 | `SECRET_ROTATION_BAD_ARGS` | the command line is wrong: a name or date the subcommand refuses, or --push without SECRET_ROTATION_KUMA set. | — |

### `scripts/backup/b2_drain.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `B2_DRAIN_OK` | every requested volume drained, or would be. | — |
| 1 | `B2_DRAIN_REFUSED` | at least one requested volume was refused, for example because it is still live. | — |
| 2 | `B2_DRAIN_ERRORED` | the drain or a B2 API call failed mid-run, including a failed post-delete verification. | — |
| 64 | `B2_DRAIN_BAD_ARGS` | the command line is wrong: a missing credential, an unreadable input file, or no volumes named. | — |

### `scripts/infra_map/gen_infra_map.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `INFRA_MAP_OK` | the map was written, or was already current. | — |
| 2 | `INFRA_MAP_LIVE_UNREADABLE` | live collection lacked a required tool, so the previous map was left in place. | — |
| 64 | `INFRA_MAP_BAD_ARGS` | the command line is wrong. | — |

### `scripts/dev/renovate_rebase.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `RENOVATE_REBASE_OK` | ticked, already ticked, or reticked. | — |
| 1 | `RENOVATE_REBASE_NO_CHECKBOX` | the PR body carries no rebase checkbox. | — |
| 2 | `RENOVATE_REBASE_GH_FAILED` | `gh` failed; its stderr is printed. | — |
| 3 | `RENOVATE_REBASE_SOAKING` | the PR is still soaking, or under --retick its soak status does not read SUCCESS, so nothing was written. | — |
| 64 | `RENOVATE_REBASE_BAD_ARGS` | the arguments are not one PR number and an optional --retick. | — |

### `scripts/dev/renovate_branch_sweep.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `RENOVATE_SWEEP_OK` | the report printed; an empty orphan set is the healthy answer. | — |
| 2 | `RENOVATE_SWEEP_GH_FAILED` | `gh` or `git` failed, or no open Dependency Dashboard was found. | — |
| 64 | `RENOVATE_SWEEP_BAD_ARGS` | the command line is wrong. | — |

### `scripts/renderer/homelab_render.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `RENDER_OK` | every requested tag or package rendered in DIR. | — |
| 1 | `RENDER_FAILED` | a render or a secret check failed; nothing was written. | The message names the template and the error, or the rule, tag, file and key. |
| 2 | `RENDER_REFUSED` | the render was refused before anything was written. | Pass a fresh stubs file, name only renderable tags or packages that exist, and render into an empty --out. |
| 64 | `RENDER_BAD_ARGS` | the command line is wrong. | — |

### `scripts/renderer/shadow_diff.py`

| Exit | Name | Meaning | What to do |
|---|---|---|---|
| 0 | `SHADOW_OK` | done, and the subcommand found nothing. | — |
| 1 | `SHADOW_DIFFERENT` | a difference, a file lacking a stub token, or a failed fluxprobe case. | Never copy or post a tree stubcheck fails. |
| 2 | `SHADOW_REFUSED` | refused: an empty set, a missing named member or file, a busy --out, no flux. | Render every tag on both sides, then compare again. |
| 64 | `SHADOW_BAD_ARGS` | the command line is wrong. | — |

## Usage

23 script(s) document how to invoke themselves. The rest take `--help`, which every catalogued entry point answers with exit 0 (`scripts/lib/tests/test_entry_points_answer_help.py`).


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

### `scripts/deploy_tools/deploy_cli.py`

```
deploy_cli.py [run] --tags "<service>" [...]   # what ./scripts/deploy.sh execs
deploy_cli.py tags list|describe|validate|changed|narrow|blockers|hosts ...
deploy_cli.py probe [--describe] <tags> [--log PATH | --log-dir DIR]
```

### `scripts/dev/fanout.py`

```
fanout.py place read
fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--no-review]
fanout.py place status <run-id>
fanout.py probe [--describe] <run-id>...
fanout.py headroom [--describe] [--batches N] [--host HOST]
fanout.py review --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
fanout.py stats [--dir DIR ...] [--since YYYY-MM-DD] [--json]
fanout.py land-server [--dry-run]
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
land.sh --regate 574                     # re-run a merged pin bump's gates and
# re-post homelab/upgrade-gates; never merges
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

### `scripts/dev/fanout_lib/notify.py`

```
notify.py --batch 4602 --unit fanout-4602 --branch worktree-fanout-4602         --repo DanielH2018/server
```

### `scripts/dev/fanout_lib/place.py`

```
fanout.py place read
fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--no-review]
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

### `scripts/deploy_tools/deploy_lib/run.py`

```
deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
deploy.sh --changed [<ref>]          # derive --tags from the diff against <ref>
deploy.sh --at <sha> --tags <svc>    # render a snapshot of <sha>, not HEAD
deploy.sh --check | --dry-run ...    # unlocked, from the working tree
deploy.sh --detach --tags <svc>      # background the playbook once the locks are held
deploy.sh --list-services            # every valid --tags value
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
