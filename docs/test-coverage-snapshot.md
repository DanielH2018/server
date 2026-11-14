# Test coverage snapshot

One coverage run of the whole pytest suite, with per-test contexts, measured on 2026-11-14 at
commit `ec7b15021` (#8516). It ran outside CI and gates nothing. Coverage is not part of CI and
has no threshold.

This page lists deletion candidates for later issues. An entry is not a verdict: a later issue
reads each test file before it deletes anything.

## Summary

| Measure | Count |
|---|---:|
| Test files run | 1,424 |
| Test files that cover at least one measured line inside a test | 1,306 |
| Test files with no covered line that another test file does not also cover (list 1) | 704 |
| Test files that cover no measured line inside a test | 118 |
| Modules in scope for list 2 | 562 |
| Modules with no covered line (list 2) | 1 |
| Modules reached only outside any test's context | 13 |

## How it was measured

The run used `coverage` 7.17.3 with `pytest-cov` 7.1.0, added for the run only through
`uv run --with pytest-cov`. Neither is a project dependency. The coverage configuration
lived outside the repository:

```ini
[run]
parallel = true
core = ctrace
patch = subprocess
relative_files = true
source = scripts, hosts, k8s, .claude/hooks
omit = */tests/*, */conftest.py, */test_*.py
```

The suite ran as the six CI shard lists that `scripts/dev/pytest_shard.py --shard N --of 6`
writes, one pytest run per list, because the pytest hook refuses a run of more than 300 test
modules. Each run appended to one data file:

```bash
uv run --with pytest-cov python -m pytest -p pytest_cov --cov --cov-append \
  --cov-config=<config> --cov-context=test --cov-report= @<shard list>
```

All six runs passed: 21,518 tests passed and 80 skipped, with 35,240 contexts recorded.

- **Only non-test code is measured.** The `omit` patterns drop every test module, conftest and
  shared helper under a `tests/` directory. Otherwise each test file's own lines would make it
  look unique.
- **`core = ctrace` is required.** coverage's default `sysmon` core on Python 3.15 does not
  record dynamic contexts.
- **A context names a test, so a line belongs to a test file.** pytest-cov records each test's
  setup, call and teardown as `<node id>|<phase>`. A test file covers a line when any context
  under its path does.
- **Module-level code and child processes have no test context.** Code that runs at import or
  collection, and code a test runs in a child process, lands in the empty context. It counts
  for list 2 but belongs to no test file, so it plays no part in list 1.
- **Child processes are measured only when they inherit the environment.** `patch = subprocess`
  starts coverage in a child Python that inherits the variable it sets and runs the same
  interpreter. A child given an environment built from scratch, or run under `.venv/bin/python`
  or another interpreter, is not measured. A module that only such a child runs reads as
  uncovered.
- **`evals/` was not measured.** Its tests read as covering nothing.

## List 1: test files whose every covered line another test file also covers

A test file is listed when it covers at least one measured line inside a test, and every one of
those lines is also covered by at least one other test file.

- **No entry is safe to delete alone.** Two test files that cover the same lines
  each appear here, and deleting both loses the lines. The groups below have identical covered
  sets.
- **Line coverage ignores assertions.** Two test files can run the same lines and check
  different things about them. A static ratchet or census test that asserts over the tree
  rather than calling code is not a candidate on coverage grounds alone.
- **138 entries cover five lines or fewer.** Most of those read a file through
  `scripts/lib/yaml_fast.py` or reach one line of `scripts/lib/kubectl.py`, then assert over
  the tree. They belong with the census tests above rather than with redundant ones.

The table sorts by the number of lines a file covers, largest first. The last column counts
the other test files that cover at least one of those lines.

| Test file | Lines it covers | Other test files that cover them |
|---|---:|---:|
| `scripts/renderer/tests/test_renderer_job_census.py` | 3169 | 1076 |
| `hosts/tests/test_lander_deadlines_cover_the_soak.py` | 3143 | 723 |
| `scripts/renderer/tests/test_renderer_crd_census.py` | 2693 | 1076 |
| `scripts/renderer/tests/test_renderer_bootstrap_documents.py` | 2589 | 1075 |
| `tests/services/test_recreate_media_and_game_roles_prepull.py` | 2577 | 711 |
| `scripts/renderer/tests/test_renderer_self_rollout_restart.py` | 2575 | 1075 |
| `tests/services/test_karakeep_prepulls_its_images.py` | 2553 | 711 |
| `tests/services/test_recreate_docker_hub_roles_prepull.py` | 2479 | 711 |
| `scripts/renderer/tests/test_renderer_precheck.py` | 2343 | 1074 |
| `tests/services/test_tdarr_prepulls_its_image.py` | 2339 | 711 |
| `tests/longhorn/test_longhorn_backup_render.py` | 2205 | 827 |
| `k8s/roles/configarr/tests/test_configarr_gate.py` | 2178 | 710 |
| `scripts/renderer/tests/test_renderer_monitor_bridge_script.py` | 2164 | 1074 |
| `scripts/renderer/tests/test_renderer_autofix_bridge_script.py` | 2147 | 1074 |
| `tests/k8s/test_step6_tags_render_into_created_namespaces.py` | 2117 | 711 |
| `scripts/renderer/tests/test_renderer_game_stats_restart.py` | 2111 | 1074 |
| `scripts/renderer/tests/test_renderer_cross_role_restart.py` | 2079 | 1074 |
| `tests/k8s/test_restore_drill_stamps.py` | 2060 | 710 |
| `tests/longhorn/test_longhorn_restore_drill_deadline.py` | 2058 | 814 |
| `tests/services/test_nut_fsd_login_confined.py` | 1489 | 697 |
| `scripts/renderer/tests/test_renderer_collision_census.py` | 1439 | 1069 |
| `scripts/renderer/tests/test_renderer_env_secret_restart.py` | 1435 | 1069 |
| `k8s/roles/pihole/tests/test_pihole_precheck.py` | 1204 | 799 |
| `tests/services/test_loki_stream_labels.py` | 945 | 697 |
| `hosts/tests/test_k3s_control_plane_hardening.py` | 928 | 686 |
| `hosts/lib/tests/test_nothing_waits_on_reconcile.py` | 895 | 682 |
| `hosts/lib/tests/test_box_firewall_and_maintenance.py` | 889 | 933 |
| `tests/staging/test_staging_egress_fence.py` | 888 | 682 |
| `hosts/lib/tests/test_persisted_outputs_census.py` | 884 | 682 |
| `hosts/tests/test_integrity_scan_rule_literals.py` | 883 | 682 |
| `hosts/tests/test_rkhunter_propupd.py` | 882 | 933 |
| `scripts/renderer/tests/test_apt_source_components.py` | 882 | 1059 |
| `hosts/tests/test_journald_syslog_forwarding.py` | 865 | 682 |
| `hosts/lib/tests/test_box_host_hardening.py` | 859 | 682 |
| `scripts/renderer/tests/test_sops_on_path.py` | 859 | 1059 |
| `hosts/lib/tests/test_host_package_tmpfiles_users.py` | 858 | 682 |
| `hosts/lib/tests/test_pi_heartbeats_component.py` | 858 | 682 |
| `hosts/tests/test_k3s_unit_logging.py` | 858 | 682 |
| `hosts/tests/test_worktree_holders_install.py` | 858 | 682 |
| `hosts/lib/tests/test_pi_updater_poll_component.py` | 857 | 933 |
| `hosts/tests/test_pi_recovery_restarts_and_reports.py` | 836 | 933 |
| `hosts/lib/tests/test_box_sshd_apt_and_tuning.py` | 834 | 682 |
| `hosts/lib/tests/test_server_host_hardening.py` | 828 | 682 |
| `hosts/lib/tests/test_server_sshd_apt_and_tuning.py` | 828 | 682 |
| `hosts/tests/test_k3s_server_component.py` | 817 | 683 |
| `hosts/lib/tests/test_pi_firewall_and_maintenance.py` | 810 | 933 |
| `tests/k8s/test_vip_pins.py` | 808 | 707 |
| `tests/k8s/test_k8s_render_context_whole_dict_pass.py` | 805 | 697 |
| `tests/services/test_pi_peer_backup_forced_command.py` | 803 | 933 |
| `hosts/lib/tests/test_homelab_reconcile_restart.py` | 802 | 933 |
| `hosts/lib/tests/test_pi_sshd_apt_and_tuning.py` | 802 | 933 |
| `tests/staging/test_etcd_drill_vm.py` | 802 | 934 |
| `hosts/lib/tests/test_server_firewall_and_maintenance.py` | 798 | 933 |
| `hosts/tests/test_agent_dotfiles.py` | 796 | 952 |
| `hosts/tests/test_artifacts_sync_alerts_once_per_outage.py` | 793 | 789 |
| `hosts/tests/test_k3s_agent_component.py` | 792 | 939 |
| `hosts/tests/test_node_resolv_order.py` | 790 | 682 |
| `hosts/lib/tests/test_pi_compose_wg_easy.py` | 787 | 682 |
| `hosts/lib/tests/test_claude_artifacts_node_timers.py` | 786 | 682 |
| `hosts/lib/tests/test_pi_compose_alloy.py` | 786 | 682 |
| `hosts/lib/tests/test_pi_tuning_component.py` | 785 | 682 |
| `hosts/tests/test_claude_agent_ssh_login.py` | 785 | 682 |
| `hosts/lib/tests/test_pi_compose_canary.py` | 784 | 682 |
| `tests/services/test_crowdsec_edge_probe_contract.py` | 784 | 872 |
| `hosts/lib/tests/test_crowdsec_probe_component.py` | 780 | 682 |
| `hosts/lib/tests/test_pi_compose_autoheal.py` | 780 | 682 |
| `hosts/lib/tests/test_pi_compose_docker_proxy.py` | 780 | 682 |
| `hosts/lib/tests/test_pi_watchdog_dropin.py` | 780 | 682 |
| `hosts/tests/test_docker_daemons_gomemlimit_headroom.py` | 780 | 682 |
| `hosts/tests/test_pi_node_exporter_host_unit.py` | 780 | 682 |
| `hosts/tests/test_pi_service_lists_census.py` | 780 | 682 |
| `tests/deploy/test_platform_filter_real_inventory.py` | 780 | 682 |
| `hosts/tests/test_netplan_static_address.py` | 777 | 682 |
| `hosts/lib/tests/test_claude_code_on_daniel_box.py` | 776 | 682 |
| `hosts/lib/tests/test_h737_box_components.py` | 775 | 933 |
| `hosts/lib/tests/test_bootstrap_host_k3s.py` | 772 | 872 |
| `tests/k8s/test_registry_ghcr_mirror.py` | 771 | 693 |
| `hosts/lib/tests/test_box_dev_tooling.py` | 768 | 682 |
| `hosts/lib/tests/test_server_dev_tooling.py` | 768 | 682 |
| `hosts/tests/test_agent_github_identity.py` | 768 | 769 |
| `hosts/tests/test_aide_check_names_its_config.py` | 768 | 682 |
| `hosts/tests/test_k3s_registries_mirror_ghcr.py` | 768 | 682 |
| `hosts/tests/test_netplan_wifi_fallback.py` | 768 | 682 |
| `hosts/lib/tests/test_box_k3s_host_half.py` | 767 | 933 |
| `hosts/lib/tests/test_deploy_ui_component.py` | 766 | 933 |
| `hosts/lib/tests/test_k3s_restart_window.py` | 761 | 682 |
| `hosts/tests/test_claude_rc_unit.py` | 760 | 682 |
| `hosts/lib/tests/test_claude_agent_home.py` | 759 | 872 |
| `hosts/tests/test_agent_user_dev_tools.py` | 755 | 788 |
| `hosts/tests/test_claude_fleet_slice_cap.py` | 755 | 682 |
| `hosts/tests/test_nut_host_secondary.py` | 755 | 788 |
| `hosts/components/k3s-host-checks/tests/test_longhorn_lib_install.py` | 754 | 872 |
| `hosts/tests/test_agent_homelab_ui_login.py` | 753 | 682 |
| `hosts/lib/tests/test_claude_agent_browser.py` | 752 | 872 |
| `hosts/tests/test_agent_user.py` | 752 | 682 |
| `hosts/tests/test_claude_code_on_daniel_server.py` | 752 | 682 |
| `hosts/tests/test_claude_fanout_server_lander.py` | 752 | 682 |
| `hosts/tests/test_claude_memory_sync.py` | 752 | 682 |
| `hosts/tests/test_claude_pipeline_timeline.py` | 752 | 682 |
| `hosts/components/registry-gc/tests/test_registry_gc_component.py` | 751 | 682 |
| `hosts/lib/tests/test_k3s_restart_component.py` | 749 | 682 |
| `hosts/components/fake-remux/tests/test_fake_remux_entrypoint.py` | 743 | 682 |
| `hosts/tests/test_etcd_restore_drill_cron.py` | 743 | 682 |
| `hosts/tests/test_k3s_oidc_trusts_authelia.py` | 743 | 682 |
| `hosts/tests/test_k3s_server_flags_migration.py` | 743 | 682 |
| `hosts/lib/tests/test_k3s_gates_component.py` | 738 | 682 |
| `hosts/lib/tests/test_nut_secondary_component.py` | 738 | 682 |
| `hosts/tests/test_k3s_agent_flags_migration.py` | 738 | 682 |
| `tests/k8s/test_netpol_from.py` | 736 | 797 |
| `tests/k8s/test_flux_jobs_pull_if_not_present.py` | 734 | 797 |
| `scripts/dev/tests/test_fanout_review_rules.py` | 733 | 594 |
| `scripts/renderer/tests/test_renderer_homelab_namespace.py` | 723 | 1058 |
| `scripts/deploy_tools/tests/test_promote_forge_boundary.py` | 720 | 1129 |
| `scripts/deploy_tools/tests/test_land_one_line_per_fact.py` | 707 | 760 |
| `scripts/dev/tests/test_fanout_pytest_width.py` | 697 | 594 |
| `hosts/lib/tests/test_pi_compose_unit.py` | 656 | 933 |
| `hosts/lib/tests/test_pi_compose_census.py` | 655 | 682 |
| `scripts/dev/tests/test_fanout_review_otel.py` | 654 | 594 |
| `scripts/renderer/tests/test_renderer_bundle_content.py` | 651 | 1043 |
| `scripts/renderer/tests/test_renderer_digest_fields.py` | 649 | 1043 |
| `tests/deploy/test_manifests_prune.py` | 644 | 702 |
| `tests/services/test_dashboard_configmaps.py` | 631 | 797 |
| `tests/deploy/test_cronjob_only_roles_include_the_gate.py` | 617 | 704 |
| `scripts/renderer/tests/test_renderer_placeholders.py` | 605 | 1075 |
| `scripts/diagnostics/tests/test_probe_health_resolver.py` | 596 | 1067 |
| `scripts/deploy_tools/tests/test_land_flip_gate_tree.py` | 588 | 760 |
| `scripts/dev/tests/test_fanout_review_default.py` | 574 | 618 |
| `hosts/lib/tests/test_builder_component.py` | 547 | 682 |
| `k8s/roles/arr-notification/tests/test_seed_job.py` | 529 | 798 |
| `k8s/roles/sonarr/tests/test_sonarr_verify.py` | 529 | 798 |
| `scripts/renderer/tests/test_renderer_live_read_census.py` | 526 | 1068 |
| `tests/longhorn/test_jellyfin_snapshot_max_size.py` | 509 | 785 |
| `hosts/lib/tests/test_eval_sweep_component.py` | 491 | 682 |
| `hosts/lib/tests/test_docs_refresh_component.py` | 487 | 682 |
| `hosts/lib/tests/test_loki_route_witness_component.py` | 487 | 682 |
| `hosts/lib/tests/test_secret_rotation_component.py` | 483 | 682 |
| `hosts/lib/tests/test_worktree_sweep_component.py` | 482 | 682 |
| `hosts/lib/tests/test_infra_map_component.py` | 481 | 682 |
| `tests/deploy/test_homelab_namespace_is_a_shared_kind.py` | 478 | 653 |
| `hosts/lib/tests/test_checkout_sync_component.py` | 467 | 682 |
| `hosts/lib/tests/test_updater_package.py` | 452 | 685 |
| `scripts/dev/tests/test_findings_reap_peer_host.py` | 443 | 616 |
| `tests/services/test_terraria_stats_follows_the_server.py` | 437 | 699 |
| `scripts/dev/tests/test_findings_open_project_warning.py` | 429 | 617 |
| `tests/k8s/test_root_needs_dac_capability.py` | 417 | 690 |
| `hosts/lib/tests/test_reconcile_component.py` | 415 | 685 |
| `tests/repo/test_shared_render_accessors.py` | 410 | 691 |
| `tests/services/test_extra_probe_targets.py` | 384 | 699 |
| `scripts/docs/tests/test_gen_role_glance_ghcr.py` | 381 | 1059 |
| `tests/services/test_blackbox_ingress_targets.py` | 375 | 699 |
| `tests/services/test_crowdsec_optional.py` | 369 | 697 |
| `tests/services/test_pi_peer_backup_verdict_export.py` | 369 | 943 |
| `tests/services/test_pi_publishing_containers.py` | 369 | 699 |
| `hosts/tests/test_etcd_metrics_single_switch.py` | 366 | 688 |
| `tests/repo/test_python_version_consistency.py` | 362 | 690 |
| `tests/services/test_code_server_build_is_pinned.py` | 362 | 690 |
| `tests/services/test_homelab_mcp_pins.py` | 362 | 690 |
| `tests/services/test_deploy_pipeline_scrape.py` | 359 | 699 |
| `k8s/roles/longhorn-reconcile/tests/test_longhorn_reconcile_render.py` | 354 | 699 |
| `tests/services/test_authelia_claude_ui_user.py` | 349 | 697 |
| `tests/services/test_homelab_mcp_source_mounts.py` | 349 | 686 |
| `k8s/roles/monitor-bridge/tests/test_check_table.py` | 348 | 693 |
| `tests/k8s/test_shared_manifest_defaults.py` | 348 | 693 |
| `tests/longhorn/test_daily_group_membership_is_the_r2_list.py` | 347 | 700 |
| `tests/services/test_pods_rule_literals.py` | 346 | 697 |
| `tests/longhorn/test_longhorn_restore_drill.py` | 340 | 804 |
| `tests/services/test_n8n_deprecated_defaults_are_pinned.py` | 339 | 686 |
| `tests/k8s/test_no_secret_in_a_moved_jobs_configmap.py` | 334 | 697 |
| `hosts/tests/test_claude_triage.py` | 333 | 690 |
| `scripts/deploy_tools/tests/test_land_options.py` | 331 | 754 |
| `tests/k8s/test_namespace_resource_defaults.py` | 318 | 789 |
| `k8s/roles/system-upgrade/tests/test_system_upgrade_role.py` | 314 | 697 |
| `tests/services/test_alertmanager_host_check_inhibit.py` | 311 | 686 |
| `tests/services/test_alertmanager_routing_b2_b4_b6b.py` | 307 | 686 |
| `tests/services/test_alertmanager_routing.py` | 301 | 686 |
| `tests/services/test_authelia_oidc_jwk_secret.py` | 301 | 686 |
| `tests/services/test_pihole_node_host_records.py` | 300 | 687 |
| `k8s/roles/monitor-bridge/tests/test_bridge_b2_rules.py` | 299 | 686 |
| `tests/services/test_alertmanager_reboot_mute.py` | 299 | 686 |
| `tests/services/test_alertmanager_routing_b3_gitops.py` | 299 | 686 |
| `tests/services/test_alertmanager_routing_b5_pi.py` | 299 | 686 |
| `tests/services/test_alertmanager_routing_fanout_idle.py` | 299 | 686 |
| `scripts/secrets_mgmt/tests/test_cluster_vars_tree.py` | 298 | 1050 |
| `tests/k8s/test_staging_opt_out_flags_render.py` | 295 | 697 |
| `tests/services/test_flux_rule_literals.py` | 294 | 697 |
| `tests/services/test_traefik_acme_optional.py` | 294 | 686 |
| `tests/services/test_traefik_edge_selfcheck.py` | 294 | 686 |
| `tests/services/test_livesync_gate_optional.py` | 292 | 686 |
| `tests/k8s/test_crowdsec_sidecars_are_scraped.py` | 291 | 697 |
| `k8s/roles/coredns/tests/test_coredns_render.py` | 290 | 686 |
| `tests/services/test_wg_easy_peer_cluster_drop.py` | 290 | 697 |
| `k8s/roles/karma/tests/test_karma_config.py` | 289 | 697 |
| `tests/services/test_ksm_flux_resource_state.py` | 289 | 697 |
| `tests/deploy/test_image_builder_content_tag.py` | 287 | 683 |
| `tests/services/test_artifacts_agent_mount.py` | 287 | 686 |
| `scripts/dev/tests/test_fanout_prompt_tool.py` | 285 | 795 |
| `tests/repo/test_k8s_render_role_template.py` | 284 | 686 |
| `tests/services/test_traefik_http_entrypoint_crowdsec.py` | 284 | 686 |
| `tests/services/test_mergeversions_install.py` | 282 | 697 |
| `tests/k8s/test_flaresolverr_disable_media.py` | 281 | 697 |
| `tests/services/test_traefik_watched_namespaces.py` | 281 | 697 |
| `tests/longhorn/test_longhorn_reap_opt_out_groups.py` | 279 | 699 |
| `tests/k8s/test_k8s_manifests_routes.py` | 278 | 695 |
| `hosts/lib/tests/test_host_package_builder_ci_cache.py` | 276 | 26 |
| `hosts/tests/test_coredns_image_follows_bundled_manifest.py` | 275 | 686 |
| `tests/services/test_anisync_pin_matches_server.py` | 275 | 697 |
| `tests/services/test_introskipper_install.py` | 275 | 697 |
| `tests/services/test_mediacleaner_install.py` | 275 | 697 |
| `tests/services/test_tdarr_vaapi_encode_init_container.py` | 275 | 686 |
| `tests/services/test_webhook_plugin_install.py` | 275 | 697 |
| `tests/services/test_pod_producer_rule_literals.py` | 274 | 697 |
| `tests/k8s/test_service_macro_comment_slot.py` | 273 | 686 |
| `hosts/tests/test_claude_lander.py` | 270 | 803 |
| `scripts/dev/tests/test_fanout_status_relaunch.py` | 250 | 614 |
| `tests/k8s/test_image_builder_security_context.py` | 248 | 683 |
| `scripts/renderer/tests/test_renderer_context.py` | 246 | 1060 |
| `tests/k8s/test_k8s_manifests.py` | 244 | 683 |
| `scripts/deploy_tools/tests/test_promote_land_wait_grouped_progress.py` | 242 | 758 |
| `scripts/dev/tests/test_fanout_clean_landed.py` | 234 | 830 |
| `hosts/tests/test_worktree_sweep_cron_reports_its_exit.py` | 232 | 952 |
| `scripts/validate/tests/test_backup_health_shim.py` | 230 | 1178 |
| `hosts/tests/test_k3s_cron_textfiles.py` | 223 | 933 |
| `hosts/tests/test_agent_clone_fast_forward.py` | 219 | 822 |
| `hosts/tests/test_other_setup_check_textfiles.py` | 219 | 933 |
| `scripts/deploy_tools/tests/test_verify_promotion_soak_gates.py` | 219 | 835 |
| `hosts/tests/test_disk_health_textfile.py` | 217 | 933 |
| `hosts/tests/test_otlp_door_textfile.py` | 217 | 933 |
| `hosts/components/k3s-host-checks/tests/test_longhorn_host_readers_follow_the_cluster.py` | 210 | 872 |
| `hosts/tests/test_aide_check.py` | 209 | 933 |
| `hosts/tests/test_aide_check_classify.py` | 207 | 933 |
| `hosts/tests/test_aide_check_package_activity.py` | 207 | 933 |
| `hosts/tests/test_aide_check_snapd_udev.py` | 207 | 933 |
| `hosts/tests/test_eval_sweep_cron.py` | 207 | 933 |
| `hosts/tests/test_rkhunter_check.py` | 207 | 933 |
| `hosts/tests/test_host_check_textfiles.py` | 206 | 933 |
| `hosts/tests/test_loki_route_witness.py` | 206 | 933 |
| `hosts/tests/test_pi_audit_health.py` | 206 | 933 |
| `hosts/tests/test_pi_gz_integrity_sweep.py` | 205 | 933 |
| `hosts/tests/test_pi_heartbeat_textfiles.py` | 205 | 933 |
| `hosts/tests/test_claude_guardrail_audit.py` | 204 | 872 |
| `tests/k8s/test_arr_monitoring_routes_admit_postflight.py` | 203 | 532 |
| `hosts/components/claude-code/tests/test_rootq_install.py` | 202 | 952 |
| `tests/services/test_b6a_rule_literals.py` | 202 | 682 |
| `hosts/tests/test_disk_health_deadline.py` | 200 | 682 |
| `tests/services/test_b1_rule_literals.py` | 199 | 682 |
| `hosts/tests/test_setup_render_matches_package.py` | 198 | 682 |
| `scripts/lib/tests/test_script_classify_reads_once.py` | 198 | 584 |
| `tests/k8s/test_traefik_cloudflare_realip.py` | 198 | 693 |
| `scripts/lib/tests/test_script_classify_in_process.py` | 197 | 584 |
| `hosts/tests/test_b4_rule_literals.py` | 196 | 682 |
| `hosts/tests/test_homelab_check_lib.py` | 195 | 788 |
| `hosts/lib/tests/test_nut_secondary_packaged_watchdog.py` | 194 | 682 |
| `tests/k8s/test_registry_netpol_grants_follow_the_pinned_node.py` | 194 | 529 |
| `hosts/tests/test_github_interaction_limit.py` | 193 | 872 |
| `hosts/tests/test_github_ruleset_drift.py` | 193 | 872 |
| `tests/services/test_disk_health_rule_literals.py` | 193 | 682 |
| `tests/staging/test_suc_rehearsal_mode.py` | 193 | 872 |
| `tests/k8s/test_rbac_against_script_drill.py` | 190 | 530 |
| `tests/longhorn/test_longhorn_restore_drill_state.py` | 190 | 799 |
| `tests/staging/test_bootstrap_drill_mode.py` | 190 | 872 |
| `tests/staging/test_staging_egress_fence_fires.py` | 190 | 872 |
| `tests/longhorn/test_longhorn_restore_drill_empty_waiver.py` | 189 | 789 |
| `tests/longhorn/test_longhorn_restore_drill_sqlite.py` | 189 | 789 |
| `tests/longhorn/test_longhorn_restore_drill_nopvc.py` | 188 | 630 |
| `tests/longhorn/test_longhorn_restore_drill_operator_pin.py` | 188 | 630 |
| `tests/longhorn/test_longhorn_restore_drill_oversize.py` | 188 | 630 |
| `tests/longhorn/test_longhorn_restore_drill_retry.py` | 188 | 630 |
| `scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py` | 184 | 993 |
| `hosts/tests/test_aide_check_role_written.py` | 183 | 682 |
| `hosts/tests/test_claude_timer_rule_literals.py` | 183 | 682 |
| `tests/services/test_not_reporting_rule_deadlines.py` | 183 | 682 |
| `tests/services/test_gitops_check_rule_literals.py` | 182 | 682 |
| `tests/longhorn/test_longhorn_restore_drill_signal_timeout.py` | 176 | 488 |
| `scripts/lib/tests/test_script_classify_assembled_paths.py` | 173 | 583 |
| `tests/k8s/test_alertmanager_read_route.py` | 172 | 529 |
| `hosts/tests/test_claude_fanout_alert.py` | 171 | 683 |
| `hosts/tests/test_claude_stop_alert.py` | 170 | 682 |
| `hosts/components/claude-code/tests/test_takeover_check_install.py` | 167 | 682 |
| `hosts/tests/test_claude_login_slice_caps.py` | 167 | 682 |
| `hosts/tests/test_coredns_metrics_binds_wildcard.py` | 167 | 682 |
| `hosts/tests/test_renovate_agent_unit.py` | 167 | 682 |
| `tests/services/test_scrape_target_rule_jobs.py` | 167 | 678 |
| `hosts/tests/test_agent_operator_config.py` | 166 | 487 |
| `hosts/tests/test_claude_bg_resume_unit.py` | 166 | 487 |
| `hosts/tests/test_netplan_dns_overrides.py` | 166 | 487 |
| `tests/services/test_navidrome_parked_route.py` | 164 | 696 |
| `tests/services/test_traefik_bouncer_metrics_ticker_off.py` | 164 | 529 |
| `.claude/hooks/tests/test_session_health_instructions.py` | 162 | 357 |
| `tests/k8s/test_k8s_manifests_rbac.py` | 158 | 693 |
| `tests/k8s/test_loki_read_route_admits_probe_py.py` | 158 | 529 |
| `tests/services/test_slskd_resolves_cluster_names.py` | 158 | 693 |
| `tests/k8s/test_headlamp_plugins_install.py` | 157 | 682 |
| `tests/k8s/test_k8s_headlamp_oidc.py` | 157 | 682 |
| `tests/services/test_slskd_share_switch.py` | 157 | 529 |
| `hosts/lib/tests/test_k3s_restart.py` | 144 | 8 |
| `scripts/secrets_mgmt/tests/test_sops_type_proof_fixture.py` | 137 | 1077 |
| `tests/services/test_host_check_rule_literals.py` | 135 | 665 |
| `scripts/lib/tests/test_render_context.py` | 127 | 1051 |
| `scripts/validate/tests/test_failure_lines_go_to_stderr.py` | 124 | 824 |
| `scripts/deploy_tools/tests/test_land_suspended_gate_reason.py` | 123 | 725 |
| `scripts/renderer/tests/test_renderer_trailing_newlines.py` | 122 | 918 |
| `scripts/lib/tests/test_facts_citations.py` | 111 | 608 |
| `scripts/lib/tests/test_script_imports.py` | 110 | 583 |
| `scripts/tests/test_renovate_annotated_pins.py` | 110 | 1026 |
| `scripts/validate/tests/test_bootstrap_k3s_download_case.py` | 109 | 723 |
| `tests/services/test_strangler_bridge.py` | 109 | 679 |
| `scripts/renderer/tests/test_renderer_determinism_0.py` | 108 | 1112 |
| `scripts/renderer/tests/test_renderer_determinism_1.py` | 108 | 1112 |
| `scripts/renderer/tests/test_renderer_determinism_2.py` | 108 | 1112 |
| `scripts/renderer/tests/test_renderer_determinism_3.py` | 108 | 1112 |
| `k8s/roles/longhorn-reconcile/tests/test_longhorn_reconcile_no_flat_weekly.py` | 104 | 211 |
| `tests/services/test_monitor_bridge_healthchecks_expected.py` | 102 | 667 |
| `tests/longhorn/test_weekly_and_nobackup_tiers_are_derived.py` | 101 | 677 |
| `k8s/roles/monitor-bridge/tests/test_bridge_wan_rules.py` | 99 | 665 |
| `tests/services/test_alertmanager_autofix_inhibit.py` | 99 | 665 |
| `tests/services/test_alertmanager_blackbox_inhibit.py` | 99 | 665 |
| `tests/services/test_alertmanager_flux_inhibit.py` | 99 | 665 |
| `tests/services/test_alertmanager_pi_inhibit.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_a1_hosts.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_a6_bridge.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_b1_k3s_crons.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_b6a_host_crons.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_b6c_registry_gc.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_b7_pods.py` | 99 | 665 |
| `tests/services/test_alertmanager_routing_b8_probes.py` | 99 | 665 |
| `tests/services/test_homepage_block_label_overrides.py` | 99 | 665 |
| `tests/services/test_smtp_wiring_docstrings.py` | 99 | 665 |
| `tests/services/test_warning_escalation_exemptions.py` | 99 | 665 |
| `scripts/dev/tests/test_fanout_clean_chain.py` | 97 | 799 |
| `scripts/docs/tests/test_fragments_hosts.py` | 96 | 1049 |
| `scripts/deploy_tools/tests/test_forge_status_pages.py` | 95 | 723 |
| `scripts/renderer/tests/test_renderer_string_values_census.py` | 95 | 1058 |
| `tests/services/test_blackbox_exporter_host_aliases.py` | 95 | 666 |
| `.claude/hooks/tests/test_session_health_json_output.py` | 94 | 320 |
| `scripts/deploy_tools/tests/test_land_broad_fallback_verdict.py` | 92 | 725 |
| `tests/k8s/test_crowdsec_agent_sidecar_shares_one_macro.py` | 92 | 669 |
| `tests/deploy/test_scaffolded_role_rollout_gate.py` | 91 | 171 |
| `tests/k8s/test_reboot_window_cronjobs_age_out.py` | 91 | 462 |
| `tests/services/test_homepage_widget_urls_carry_no_credentials.py` | 91 | 462 |
| `tests/k8s/test_rendered_properties.py` | 90 | 663 |
| `tests/services/test_authelia_redis_sessions.py` | 87 | 665 |
| `scripts/docs/tests/test_fragments_bridge.py` | 83 | 586 |
| `scripts/validate/tests/test_rule_test_parts.py` | 81 | 1109 |
| `hosts/lib/tests/test_k3s_gates_parity.py` | 80 | 16 |
| `scripts/backup/tests/test_longhorn_reap_opt_out_snapshots.py` | 80 | 587 |
| `scripts/dev/tests/test_fanout_status_blocker_first.py` | 79 | 584 |
| `tests/repo/test_ansible_core_census.py` | 79 | 561 |
| `scripts/lib/tests/test_facts_pins.py` | 77 | 586 |
| `tests/k8s/test_public_routes_require_cloudflare_origin_pull.py` | 77 | 661 |
| `tests/k8s/test_traefik_netpol_https_origin_lock.py` | 75 | 462 |
| `scripts/renderer/tests/test_renderer_ansible_free.py` | 73 | 1095 |
| `scripts/renderer/tests/test_renderer_template_census.py` | 73 | 914 |
| `tests/k8s/test_checksum_annotations_documented.py` | 73 | 438 |
| `scripts/lib/tests/test_guard_jinja_env_py_table_retired.py` | 72 | 998 |
| `scripts/lib/tests/test_k8s_context.py` | 72 | 901 |
| `scripts/diagnostics/tests/test_probe_health_flux_reference.py` | 71 | 604 |
| `scripts/backup/tests/test_longhorn_reap_trim_label.py` | 69 | 588 |
| `scripts/dev/tests/test_fanout_batch_caps.py` | 69 | 586 |
| `tests/k8s/test_aggregated_clusterroles.py` | 67 | 461 |
| `tests/k8s/test_k8s_manifests_metallb.py` | 67 | 461 |
| `tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py` | 66 | 631 |
| `tests/longhorn/test_longhorn_backup_grace_visibility.py` | 64 | 12 |
| `tests/staging/test_staging_network.py` | 64 | 661 |
| `scripts/diagnostics/tests/test_probe_health_schedule_gate_checkout_age.py` | 61 | 605 |
| `tests/k8s/test_tls_cert_resolver_optional.py` | 61 | 673 |
| `tests/services/test_qbittorrent_mullvad_account_secret.py` | 61 | 661 |
| `tests/longhorn/test_longhorn_reap_guard.py` | 59 | 14 |
| `tests/deploy/test_k8s_toposort.py` | 56 | 432 |
| `scripts/secrets_mgmt/tests/test_secret_consumers.py` | 55 | 586 |
| `tests/repo/test_census_rows_roles.py` | 53 | 779 |
| `tests/longhorn/test_longhorn_storageclass.py` | 52 | 676 |
| `scripts/validate/tests/test_alert_tools_guard.py` | 51 | 798 |
| `tests/longhorn/test_every_longhorn_pvc_has_a_tier.py` | 51 | 662 |
| `tests/services/test_ingress_probe_targets.py` | 50 | 411 |
| `tests/deploy/test_manifests_apply_verdict.py` | 47 | 91 |
| `tests/services/test_pihole_redundancy.py` | 45 | 91 |
| `scripts/dev/tests/test_prune_worktrees_orphan_dirs.py` | 43 | 731 |
| `scripts/lib/tests/test_invocation_sites.py` | 43 | 1026 |
| `scripts/tests/test_invoker_paths_resolve.py` | 43 | 1026 |
| `tests/services/test_exportarr_sidecars.py` | 43 | 665 |
| `scripts/lib/tests/test_estate.py` | 42 | 1051 |
| `tests/deploy/test_k8s_autodeploy_batch_gates.py` | 41 | 669 |
| `k8s/roles/system-upgrade/tests/test_plan_health_exprs.py` | 40 | 624 |
| `tests/repo/test_ci_empty_pr_merge_fails.py` | 40 | 775 |
| `.claude/hooks/tests/test_rules_globs_match_a_tracked_file.py` | 39 | 438 |
| `scripts/lib/tests/test_renovate_annotations.py` | 39 | 584 |
| `tests/repo/test_host_package_build_step.py` | 39 | 913 |
| `tests/repo/test_host_package_cases_run_on_their_inputs.py` | 39 | 913 |
| `tests/repo/test_ci_tool_pins_do_not_move_backwards.py` | 38 | 253 |
| `tests/repo/test_module_length_ratchet.py` | 38 | 253 |
| `scripts/deploy_tools/tests/test_promote_forward_only_real_tags.py` | 37 | 1109 |
| `tests/k8s/test_derived_manifest_files.py` | 37 | 206 |
| `scripts/infra_map/tests/test_infra_map_pi_lists.py` | 35 | 584 |
| `scripts/secrets_mgmt/tests/test_departed_secrets_are_accounted_for.py` | 35 | 1026 |
| `scripts/lib/tests/test_git_testing.py` | 34 | 603 |
| `tests/deploy/test_ci_cancelled_is_not_a_verdict.py` | 33 | 20 |
| `scripts/dev/tests/test_repo_guards_survey.py` | 32 | 793 |
| `tests/deploy/test_k8s_autodeploy_guard.py` | 32 | 630 |
| `hosts/tests/test_pi_watchdog_alert.py` | 31 | 892 |
| `hosts/components/claude-code/tests/test_rootq_k3s_manifests.py` | 30 | 1 |
| `scripts/validate/tests/test_validate_jinja_bash_collisions.py` | 30 | 723 |
| `tests/services/test_monitor_bridge_mount_layout.py` | 29 | 763 |
| `scripts/renderer/tests/test_renderer_digest_log.py` | 28 | 720 |
| `tests/deploy/test_channel_tag_digest_pins_stay_off_autodeploy.py` | 28 | 630 |
| `tests/deploy/test_k3s_pin_routing.py` | 28 | 668 |
| `scripts/renderer/tests/test_renderer_targets.py` | 27 | 1049 |
| `.claude/hooks/tests/test_hook_imports_run_standalone.py` | 26 | 336 |
| `k8s/roles/crowdsec/tests/test_remote_allowlist_fixtures.py` | 26 | 208 |
| `scripts/deploy_tools/tests/test_promote_reconcile.py` | 26 | 1154 |
| `scripts/deploy_tools/tests/test_promote_settle.py` | 26 | 1154 |
| `scripts/deploy_tools/tests/test_promote_settle_owner.py` | 26 | 1154 |
| `scripts/deploy_tools/tests/test_promote_settle_retry.py` | 26 | 1154 |
| `hosts/tests/test_host_maintenance.py` | 25 | 892 |
| `hosts/tests/test_systemd_units_check.py` | 25 | 892 |
| `k8s/roles/zigbee2mqtt/tests/test_mqtt_broker_wait.py` | 25 | 570 |
| `scripts/dev/tests/test_ci_failures.py` | 25 | 793 |
| `scripts/lib/tests/test_proc_testing.py` | 25 | 793 |
| `tests/deploy/test_deploy_annotations.py` | 25 | 669 |
| `tests/repo/test_host_test_image.py` | 25 | 892 |
| `tests/repo/test_promote_drill_workflow.py` | 25 | 892 |
| `tests/repo/test_promote_workflow_retry.py` | 25 | 892 |
| `tests/services/test_netns_reset_restores_resolv_conf.py` | 25 | 892 |
| `hosts/tests/test_docs_refresh_records_shard_weights.py` | 24 | 422 |
| `scripts/deploy_tools/tests/test_land_annotation_stays_out_of_syslog.py` | 24 | 793 |
| `scripts/validate/tests/test_renovate_config_retry.py` | 24 | 793 |
| `scripts/validate/tests/test_vale_sync_guard.py` | 24 | 793 |
| `tests/services/test_qbittorrent_sidecar_resets_the_netns.py` | 24 | 422 |
| `.claude/hooks/tests/test_hook_scripts_executable.py` | 23 | 422 |
| `.claude/hooks/tests/test_run_hook.py` | 23 | 422 |
| `scripts/deploy_tools/tests/test_promote_ci_contexts.py` | 23 | 1109 |
| `scripts/renderer/tests/test_renderer_host_inputs.py` | 23 | 723 |
| `tests/deploy/test_deleted_widening_tripwires.py` | 23 | 7 |
| `tests/repo/test_census_rows_layout.py` | 23 | 212 |
| `tests/services/test_pi_peer_backup_host_key_failure.py` | 23 | 422 |
| `scripts/lib/tests/test_inventory_lists.py` | 22 | 1113 |
| `tests/deploy/test_landing_annotations.py` | 22 | 43 |
| `tests/k8s/test_built_images_name_the_content_tag.py` | 22 | 186 |
| `tests/repo/test_role_claude_md.py` | 22 | 279 |
| `tests/repo/test_documented_paths_exist.py` | 21 | 10 |
| `hosts/components/claude-code/tests/test_takeover_check.py` | 19 | 422 |
| `scripts/deploy_tools/tests/test_land_arm_merge_through_the_shim.py` | 18 | 731 |
| `scripts/deploy_tools/tests/test_land_fresh_slot.py` | 18 | 731 |
| `scripts/lib/tests/test_plain_scalar.py` | 18 | 583 |
| `tests/deploy/test_k8s_autodeploy_rollout_gates.py` | 18 | 669 |
| `scripts/lib/tests/test_facts_heading_pointers.py` | 17 | 585 |
| `scripts/lib/tests/test_k8s_roles_k8s_tags.py` | 17 | 1049 |
| `tests/services/test_autofix_bridge_modules.py` | 17 | 657 |
| `hosts/components/claude-code/tests/test_takeover_check_flux_selection.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_reconcile.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_reconcile_batched_reads.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_reconcile_timing.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_reconcile_udev.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_updater.py` | 16 | 419 |
| `hosts/lib/tests/test_homelab_updater_fetch.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_activation.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_hold_way_out.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_libvirt_arm.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_lines_arm.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_shadow_handoff.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_ufw_arm.py` | 16 | 419 |
| `hosts/lib/tests/test_reconcile_users_arm.py` | 16 | 419 |
| `scripts/renderer/tests/test_flux_job_connect.py` | 16 | 594 |
| `tests/k8s/test_alloy_redacts_access_log_query_credentials.py` | 16 | 7 |
| `hosts/lib/tests/test_homelab_secrets.py` | 15 | 319 |
| `scripts/lib/tests/test_k8s_roles_census.py` | 15 | 619 |
| `tests/repo/test_census_rows_text.py` | 15 | 742 |
| `tests/repo/test_host_package_case_inputs_census.py` | 15 | 674 |
| `tests/repo/test_root_entries_are_tracked_or_expected.py` | 15 | 208 |
| `k8s/roles/node-exporter/tests/test_dockerhub_mirror_pull.py` | 14 | 761 |
| `k8s/roles/node-exporter/tests/test_ghcr_mirror_pull.py` | 14 | 761 |
| `k8s/roles/node-exporter/tests/test_iscsi_sessions.py` | 14 | 761 |
| `tests/repo/test_role_census.py` | 14 | 76 |
| `.claude/hooks/tests/test_session_health_host_independent.py` | 13 | 323 |
| `hosts/tests/test_claude_cgroup_metrics.py` | 13 | 739 |
| `k8s/roles/monitor-bridge/tests/test_domain_registration_expiry.py` | 13 | 739 |
| `scripts/deploy_tools/tests/test_land_ci_budget.py` | 13 | 1109 |
| `scripts/deploy_tools/tests/test_land_ci_wait_has_no_label.py` | 13 | 723 |
| `scripts/dev/tests/test_findings_snapshot_import.py` | 13 | 720 |
| `scripts/lib/tests/test_claude_worktree_import.py` | 13 | 720 |
| `scripts/lib/tests/test_k8s_lookups.py` | 13 | 586 |
| `scripts/lib/tests/test_one_guard_env_factory.py` | 13 | 720 |
| `scripts/tests/test_census_pass_prebuild.py` | 13 | 720 |
| `scripts/tests/test_census_rows_parse_once.py` | 13 | 720 |
| `scripts/tests/test_renovate_auto_replace.py` | 13 | 720 |
| `scripts/tests/test_renovate_dockerfiles.py` | 13 | 720 |
| `scripts/tests/test_renovate_managers.py` | 13 | 720 |
| `scripts/tests/test_renovate_pi_compose_pins.py` | 13 | 720 |
| `scripts/tests/test_renovate_release_urls.py` | 13 | 720 |
| `scripts/tests/test_test_module_bootstraps_present.py` | 13 | 720 |
| `tests/longhorn/test_longhorn_restore_drill_byte_floor.py` | 13 | 739 |
| `tests/repo/test_adr_links.py` | 13 | 739 |
| `tests/repo/test_ci_arm64_tools_download_gates.py` | 13 | 739 |
| `tests/repo/test_ci_flux_cli_is_cached.py` | 13 | 739 |
| `tests/repo/test_ci_sops_kind_cosign_are_cached.py` | 13 | 739 |
| `tests/repo/test_host_tools_cache.py` | 13 | 739 |
| `tests/repo/test_testpaths_covers_every_test_file.py` | 13 | 208 |
| `hosts/components/registry-gc/tests/test_ghcr_mirror_prune.py` | 12 | 206 |
| `hosts/lib/tests/test_homelab_secrets_queue.py` | 12 | 319 |
| `hosts/lib/tests/test_hosts_tree_is_tracked.py` | 12 | 206 |
| `hosts/lib/tests/test_reconcile_stub_imports.py` | 12 | 206 |
| `hosts/tests/test_docs_refresh_failure_path.py` | 12 | 206 |
| `hosts/tests/test_etcd_restore_drill_verify.py` | 12 | 206 |
| `hosts/tests/test_eval_run_failure_path.py` | 12 | 206 |
| `hosts/tests/test_health_cron_boot_grace.py` | 12 | 206 |
| `scripts/backup/tests/test_longhorn_reap_backups_cli.py` | 12 | 723 |
| `scripts/backup/tests/test_longhorn_reap_backups_modes_cli.py` | 12 | 723 |
| `scripts/backup/tests/test_longhorn_reap_entrypoints.py` | 12 | 723 |
| `scripts/backup/tests/test_longhorn_reap_snapshots_cli.py` | 12 | 723 |
| `scripts/backup/tests/test_longhorn_reap_subcommands.py` | 12 | 723 |
| `scripts/deploy_tools/tests/test_bootstrap_drill_check_wiring.py` | 12 | 723 |
| `scripts/deploy_tools/tests/test_land_fakes_cleanup.py` | 12 | 723 |
| `scripts/deploy_tools/tests/test_land_imports.py` | 12 | 723 |
| `scripts/deploy_tools/tests/test_land_shim.py` | 12 | 723 |
| `scripts/deploy_tools/tests/test_land_shim_skips_lint_group.py` | 12 | 723 |
| `scripts/lib/tests/test_summary.py` | 12 | 583 |
| `scripts/validate/tests/test_every_validator_has_a_red_proof.py` | 12 | 723 |
| `scripts/z2m/tests/test_set_device_option.py` | 12 | 723 |
| `tests/deploy/test_entry_fields_filter.py` | 12 | 39 |
| `tests/k8s/test_uncovered_roles_justifications_resolve.py` | 12 | 206 |
| `tests/repo/test_census_rows_python.py` | 12 | 206 |
| `tests/repo/test_census_rows_suite.py` | 12 | 206 |
| `tests/repo/test_claude_md_paragraphs_have_one_home.py` | 12 | 206 |
| `tests/repo/test_cross_directory_importer_bootstraps_sys_path.py` | 12 | 206 |
| `tests/repo/test_docs_relative_links_resolve.py` | 12 | 206 |
| `tests/repo/test_flux_bootstrap_section.py` | 12 | 206 |
| `tests/repo/test_installed_units_log_level.py` | 12 | 206 |
| `tests/repo/test_k8s_render_prebuild.py` | 12 | 206 |
| `tests/repo/test_role_shipping_python_has_tests.py` | 12 | 206 |
| `tests/repo/test_row_table.py` | 12 | 206 |
| `tests/repo/test_scripts_suite_collects_on_its_own.py` | 12 | 206 |
| `tests/repo/test_tracked_python_hazards.py` | 12 | 206 |
| `tests/repo/test_ty_config_covers_the_repo.py` | 12 | 206 |
| `tests/services/test_dashboards_read_no_gitops_tick_series.py` | 12 | 206 |
| `tests/services/test_jellyfin_plugin_allowlist.py` | 12 | 206 |
| `tests/services/test_jellyfin_plugin_pins_hold_across_a_restart.py` | 12 | 206 |
| `tests/services/test_pi_peer_backup_push_line_is_readable.py` | 12 | 206 |
| `tests/services/test_registry_gc_dockerhub_mirror.py` | 12 | 206 |
| `tests/services/test_registry_gc_ghcr_mirror.py` | 12 | 206 |
| `tests/services/test_registry_gc_mirror_job_report.py` | 12 | 206 |
| `tests/services/test_registry_gc_state_file_is_json.py` | 12 | 206 |
| `.claude/hooks/tests/test_hook_shim_fail_open.py` | 11 | 320 |
| `.claude/hooks/tests/test_project_settings_shape.py` | 11 | 320 |
| `.claude/hooks/tests/test_session_health_no_wall_clock_bound.py` | 11 | 320 |
| `.claude/hooks/tests/test_setup_wiring.py` | 11 | 320 |
| `.claude/hooks/tests/test_uv_python_in_batch.py` | 11 | 320 |
| `hosts/components/k3s-host-checks/tests/test_longhorn_backup_grace_cron.py` | 11 | 320 |
| `hosts/components/k3s-host-checks/tests/test_longhorn_backup_health_reader.py` | 11 | 320 |
| `scripts/lib/tests/test_json_types.py` | 11 | 583 |
| `tests/deploy/test_restart_on_narrows_the_restart_signals.py` | 11 | 630 |
| `tests/k8s/test_forward_only_tags.py` | 11 | 630 |
| `tests/staging/test_bootstrap_drill_guest_run.py` | 11 | 320 |
| `tests/staging/test_suc_rehearsal_guest_races.py` | 11 | 320 |
| `k8s/roles/monitor-bridge/tests/test_check_b2_dashboard.py` | 10 | 1 |
| `k8s/roles/monitor-bridge/tests/test_check_streaks.py` | 10 | 10 |
| `tests/services/test_bridge_patch_boundary.py` | 10 | 76 |
| `scripts/lib/tests/test_cli_help_messages.py` | 9 | 614 |
| `tests/longhorn/test_longhorn_manager_ready_rule.py` | 9 | 2 |
| `tests/repo/test_doc_values_hook.py` | 9 | 1 |
| `hosts/lib/tests/test_pi_compose_scripts.py` | 8 | 317 |
| `scripts/lib/tests/test_progress.py` | 8 | 583 |
| `tests/repo/test_leakguard_child_env.py` | 8 | 317 |
| `scripts/lib/tests/test_yaml_fast.py` | 7 | 1043 |
| `tests/k8s/test_service_role_claude_md_has_at_a_glance.py` | 7 | 431 |
| `tests/k8s/test_netpol_probe_targets.py` | 6 | 662 |
| `tests/services/test_karakeep_chrome_rollout.py` | 6 | 163 |
| `tests/services/test_scrutiny_rollouts.py` | 6 | 163 |
| `tests/deploy/test_containers_list_names_unique.py` | 5 | 431 |
| `hosts/lib/tests/test_bootstrap_host.py` | 4 | 317 |
| `hosts/lib/tests/test_bootstrap_host_baseline.py` | 4 | 317 |
| `hosts/lib/tests/test_bootstrap_host_local_build.py` | 4 | 317 |
| `hosts/lib/tests/test_bootstrap_host_log2ram.py` | 4 | 317 |
| `hosts/lib/tests/test_bootstrap_host_seed.py` | 4 | 317 |
| `hosts/lib/tests/test_bootstrap_host_steps.py` | 4 | 317 |
| `tests/services/test_registry_gc_watchers_cover_the_worst_run.py` | 4 | 662 |
| `tests/services/test_host_plane_rule_literals.py` | 3 | 654 |
| `k8s/roles/crowdsec/tests/test_authenticated_app_whitelist.py` | 2 | 2 |
| `scripts/lib/tests/test_jinja_comments.py` | 2 | 584 |
| `scripts/lib/tests/test_tree_lock.py` | 2 | 1026 |
| `scripts/tests/test_renovate_flux_cli.py` | 2 | 1026 |
| `scripts/tests/test_renovate_homelab_tools_pins.py` | 2 | 1026 |
| `scripts/tests/test_renovate_host_file_copies.py` | 2 | 1026 |
| `scripts/tests/test_renovate_kind_pins.py` | 2 | 1026 |
| `scripts/tests/test_renovate_nfpm_pin.py` | 2 | 1026 |
| `tests/k8s/test_traefik_crds.py` | 2 | 647 |
| `tests/longhorn/test_longhorn_control_plane_resources.py` | 2 | 647 |
| `tests/longhorn/test_longhorn_csi_secret_grant.py` | 2 | 647 |
| `tests/repo/test_ci_contexts_match_workflows.py` | 2 | 628 |
| `tests/services/test_pihole_lan_reverse_forwarding.py` | 2 | 647 |
| `hosts/lib/tests/test_host_census.py` | 1 | 624 |
| `hosts/tests/test_coredns_metrics_port_agrees.py` | 1 | 624 |
| `hosts/tests/test_docker_engine_hold_list.py` | 1 | 624 |
| `hosts/tests/test_etcd_quota_and_its_monitor_agree.py` | 1 | 624 |
| `hosts/tests/test_host_python_pin.py` | 1 | 624 |
| `hosts/tests/test_k3s_host_has_no_docker.py` | 1 | 624 |
| `hosts/tests/test_k3s_join_port_symmetry.py` | 1 | 624 |
| `hosts/tests/test_server_names_daniel_pi.py` | 1 | 624 |
| `hosts/tests/test_ssh_rate_limit_lan_exempt.py` | 1 | 624 |
| `hosts/tests/test_unattended_origins_pattern.py` | 1 | 624 |
| `hosts/tests/test_wan_probe_urls_match_the_bridge.py` | 1 | 624 |
| `k8s/roles/monitor-bridge/tests/test_bridge_grace_rules.py` | 1 | 624 |
| `k8s/roles/monitor-bridge/tests/test_bridge_prometheus_gated_rules.py` | 1 | 624 |
| `k8s/roles/monitor-bridge/tests/test_bridge_ungated_rules.py` | 1 | 624 |
| `scripts/dev/tests/test_fanout_headless_imports.py` | 1 | 583 |
| `scripts/dev/tests/test_run_as_cron.py` | 1 | 583 |
| `scripts/diagnostics/tests/test_probe_bad_since_census.py` | 1 | 583 |
| `scripts/diagnostics/tests/test_probe_boundaries.py` | 1 | 583 |
| `scripts/diagnostics/tests/test_ui_smoke_helpers.py` | 1 | 583 |
| `scripts/docs/tests/test_catalog_lib_layout.py` | 1 | 583 |
| `scripts/docs/tests/test_fragments_lib_layout.py` | 1 | 583 |
| `scripts/lib/tests/test_ansible_bool_rationale.py` | 1 | 583 |
| `scripts/lib/tests/test_entry_point_census_collects_cheaply.py` | 1 | 583 |
| `scripts/lib/tests/test_help_check_rejects_work_output.py` | 1 | 583 |
| `scripts/lib/tests/test_script_classify_surface.py` | 1 | 583 |
| `scripts/renderer/tests/test_renderer_size_ceiling.py` | 1 | 583 |
| `scripts/secrets_mgmt/tests/test_secrets_lib_layout.py` | 1 | 583 |
| `scripts/tests/test_census_roots_cover_host_lib.py` | 1 | 583 |
| `scripts/tests/test_census_rows_test_modules.py` | 1 | 583 |
| `scripts/tests/test_census_rows_test_renders.py` | 1 | 583 |
| `scripts/tests/test_fqdn_links_js.py` | 1 | 583 |
| `scripts/tests/test_mkdocs_config.py` | 1 | 583 |
| `scripts/tests/test_prek_files_cover_entry_imports.py` | 1 | 583 |
| `scripts/tests/test_prek_files_cover_pythonpath_imports.py` | 1 | 583 |
| `scripts/tests/test_renovate_automerge_does_not_chase_master.py` | 1 | 583 |
| `scripts/tests/test_renovate_cluster_pin_rules.py` | 1 | 583 |
| `scripts/tests/test_renovate_cosign_release.py` | 1 | 583 |
| `scripts/tests/test_renovate_digest_soak_without_timestamp.py` | 1 | 583 |
| `scripts/tests/test_renovate_docker_engine_pins.py` | 1 | 583 |
| `scripts/tests/test_renovate_manager_overlap.py` | 1 | 583 |
| `scripts/tests/test_renovate_manual_rules_push_weekly.py` | 1 | 583 |
| `scripts/tests/test_renovate_nginx_digest_soak.py` | 1 | 583 |
| `scripts/tests/test_renovate_python_group.py` | 1 | 583 |
| `scripts/tests/test_renovate_renderer_closure_no_automerge.py` | 1 | 583 |
| `scripts/tests/test_script_bootstraps_present.py` | 1 | 583 |
| `scripts/tests/test_scripts_import_direction.py` | 1 | 583 |
| `scripts/tests/test_table_filter_js.py` | 1 | 583 |
| `scripts/tests/test_vale_scope.py` | 1 | 583 |
| `tests/k8s/test_alloy_ships_k3s_log.py` | 1 | 624 |
| `tests/k8s/test_cluster_base_mirrors_the_k3s_identity.py` | 1 | 624 |
| `tests/k8s/test_forwardauth_rebuilds_request_target.py` | 1 | 624 |
| `tests/k8s/test_jsonpath_quoting.py` | 1 | 185 |
| `tests/k8s/test_metallb_vendored_manifest.py` | 1 | 185 |
| `tests/k8s/test_pod_template_hygiene.py` | 1 | 185 |
| `tests/k8s/test_priorityclass_omits_defaulted_fields.py` | 1 | 185 |
| `tests/k8s/test_rate_limit_keys_on_the_client.py` | 1 | 624 |
| `tests/k8s/test_workload_service_overrides.py` | 1 | 624 |
| `tests/longhorn/test_pvc_sizes_match_block_size.py` | 1 | 624 |
| `tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py` | 1 | 624 |
| `tests/longhorn/test_volume_revert_include.py` | 1 | 624 |
| `tests/repo/test_ci_apt_fallback_mirror.py` | 1 | 624 |
| `tests/repo/test_ci_bootstrap_cases_own_job.py` | 1 | 624 |
| `tests/repo/test_ci_dispatch_sweeps_everything.py` | 1 | 624 |
| `tests/repo/test_ci_k3s_download_job_off_critical_path.py` | 1 | 624 |
| `tests/repo/test_ci_k3s_download_leg_does_not_gate.py` | 1 | 624 |
| `tests/repo/test_ci_lint_group.py` | 1 | 624 |
| `tests/repo/test_ci_master_runs_are_not_evictable.py` | 1 | 624 |
| `tests/repo/test_ci_pytest_job_fetch_depth.py` | 1 | 624 |
| `tests/repo/test_ci_release_downloads_are_cached.py` | 1 | 624 |
| `tests/repo/test_ci_skip_proven_master.py` | 1 | 624 |
| `tests/repo/test_every_ci_job_is_gated.py` | 1 | 624 |
| `tests/repo/test_failure_class_detectors.py` | 1 | 2 |
| `tests/repo/test_host_package_scans_run_on_every_pr.py` | 1 | 624 |
| `tests/repo/test_host_test_image_predownloads.py` | 1 | 624 |
| `tests/repo/test_kind_node_image_follows_k3s.py` | 1 | 624 |
| `tests/repo/test_lan_facts_have_one_source.py` | 1 | 624 |
| `tests/repo/test_promote_runtime_imports.py` | 1 | 624 |
| `tests/repo/test_promote_workflow.py` | 1 | 624 |
| `tests/repo/test_promote_workflow_bundle.py` | 1 | 624 |
| `tests/repo/test_promote_workflow_schedule.py` | 1 | 624 |
| `tests/repo/test_promote_workflow_starters.py` | 1 | 624 |
| `tests/repo/test_promote_workflow_uv_groups.py` | 1 | 624 |
| `tests/repo/test_promote_workflow_verify.py` | 1 | 624 |
| `tests/repo/test_prune_artifacts_workflow.py` | 1 | 624 |
| `tests/repo/test_renovate_canary_is_scheduled_not_per_merge.py` | 1 | 624 |
| `tests/repo/test_setup_action_uv_cache.py` | 1 | 624 |
| `tests/repo/test_sops_creation_rules.py` | 1 | 624 |
| `tests/repo/test_vale_matches_the_ci_pin.py` | 1 | 624 |
| `tests/services/test_alert_runbook_urls.py` | 1 | 624 |
| `tests/services/test_artifacts_configmap.py` | 1 | 624 |
| `tests/services/test_authelia_argon2_parameters_agree.py` | 1 | 624 |
| `tests/services/test_authelia_config_schema.py` | 1 | 624 |
| `tests/services/test_blackbox_exporter_modules.py` | 1 | 624 |
| `tests/services/test_committed_dashboards_are_a_fixed_point_of_the_exporter.py` | 1 | 1 |
| `tests/services/test_container_checks_rule_literals.py` | 1 | 624 |
| `tests/services/test_dashboard_folders_are_mounted.py` | 1 | 624 |
| `tests/services/test_deploy_pipeline_reboot_window_rule.py` | 1 | 624 |
| `tests/services/test_dri_resource_name_agrees.py` | 1 | 624 |
| `tests/services/test_edge_rule_literals.py` | 1 | 624 |
| `tests/services/test_every_jellyfin_plugin_target_abi_fits_the_image.py` | 1 | 624 |
| `tests/services/test_flux_not_ready_comment_pager.py` | 1 | 624 |
| `tests/services/test_grafana_authelia_oidc.py` | 1 | 624 |
| `tests/services/test_ha_trusted_proxies_cover_cloudflare.py` | 1 | 624 |
| `tests/services/test_homepage_calendar_tracks_follow_services_rows.py` | 1 | 624 |
| `tests/services/test_homepage_longhorn_widget_url.py` | 1 | 624 |
| `tests/services/test_host_job_series_have_a_board.py` | 1 | 624 |
| `tests/services/test_k8s_workloads_rule_literals.py` | 1 | 624 |
| `tests/services/test_lan_address_rules_agree.py` | 1 | 624 |
| `tests/services/test_metric_drop_regexes.py` | 1 | 624 |
| `tests/services/test_monitor_bridge_modules.py` | 1 | 624 |
| `tests/services/test_observability_cluster_ips_pinned.py` | 1 | 624 |
| `tests/services/test_observability_config_rolls_one_workload.py` | 1 | 624 |
| `tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py` | 1 | 624 |
| `tests/services/test_scrape_target_flap_rule.py` | 1 | 624 |
| `tests/services/test_smtp_wiring.py` | 1 | 624 |
| `tests/services/test_storage_rule_literals.py` | 1 | 624 |

### Test files with identical covered sets

Each group's files cover exactly the same measured lines.

- 76 files: `hosts/lib/tests/test_host_census.py`, `hosts/tests/test_coredns_metrics_port_agrees.py`, `hosts/tests/test_docker_engine_hold_list.py`, `hosts/tests/test_etcd_quota_and_its_monitor_agree.py`, `hosts/tests/test_host_python_pin.py`, `hosts/tests/test_k3s_host_has_no_docker.py`, `hosts/tests/test_k3s_join_port_symmetry.py`, `hosts/tests/test_server_names_daniel_pi.py`, `hosts/tests/test_ssh_rate_limit_lan_exempt.py`, `hosts/tests/test_unattended_origins_pattern.py`, `hosts/tests/test_wan_probe_urls_match_the_bridge.py`, `k8s/roles/monitor-bridge/tests/test_bridge_grace_rules.py`, `k8s/roles/monitor-bridge/tests/test_bridge_prometheus_gated_rules.py`, `k8s/roles/monitor-bridge/tests/test_bridge_ungated_rules.py`, `tests/k8s/test_alloy_ships_k3s_log.py`, `tests/k8s/test_cluster_base_mirrors_the_k3s_identity.py`, `tests/k8s/test_forwardauth_rebuilds_request_target.py`, `tests/k8s/test_rate_limit_keys_on_the_client.py`, `tests/k8s/test_workload_service_overrides.py`, `tests/longhorn/test_pvc_sizes_match_block_size.py`, `tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py`, `tests/longhorn/test_volume_revert_include.py`, `tests/repo/test_ci_apt_fallback_mirror.py`, `tests/repo/test_ci_bootstrap_cases_own_job.py`, `tests/repo/test_ci_dispatch_sweeps_everything.py`, `tests/repo/test_ci_k3s_download_job_off_critical_path.py`, `tests/repo/test_ci_k3s_download_leg_does_not_gate.py`, `tests/repo/test_ci_lint_group.py`, `tests/repo/test_ci_master_runs_are_not_evictable.py`, `tests/repo/test_ci_pytest_job_fetch_depth.py`, `tests/repo/test_ci_release_downloads_are_cached.py`, `tests/repo/test_ci_skip_proven_master.py`, `tests/repo/test_every_ci_job_is_gated.py`, `tests/repo/test_host_package_scans_run_on_every_pr.py`, `tests/repo/test_host_test_image_predownloads.py`, `tests/repo/test_kind_node_image_follows_k3s.py`, `tests/repo/test_lan_facts_have_one_source.py`, `tests/repo/test_promote_runtime_imports.py`, `tests/repo/test_promote_workflow.py`, `tests/repo/test_promote_workflow_bundle.py`, `tests/repo/test_promote_workflow_schedule.py`, `tests/repo/test_promote_workflow_starters.py`, `tests/repo/test_promote_workflow_uv_groups.py`, `tests/repo/test_promote_workflow_verify.py`, `tests/repo/test_prune_artifacts_workflow.py`, `tests/repo/test_renovate_canary_is_scheduled_not_per_merge.py`, `tests/repo/test_setup_action_uv_cache.py`, `tests/repo/test_sops_creation_rules.py`, `tests/repo/test_vale_matches_the_ci_pin.py`, `tests/services/test_alert_runbook_urls.py`, `tests/services/test_artifacts_configmap.py`, `tests/services/test_authelia_argon2_parameters_agree.py`, `tests/services/test_authelia_config_schema.py`, `tests/services/test_blackbox_exporter_modules.py`, `tests/services/test_container_checks_rule_literals.py`, `tests/services/test_dashboard_folders_are_mounted.py`, `tests/services/test_deploy_pipeline_reboot_window_rule.py`, `tests/services/test_dri_resource_name_agrees.py`, `tests/services/test_edge_rule_literals.py`, `tests/services/test_every_jellyfin_plugin_target_abi_fits_the_image.py`, `tests/services/test_flux_not_ready_comment_pager.py`, `tests/services/test_grafana_authelia_oidc.py`, `tests/services/test_ha_trusted_proxies_cover_cloudflare.py`, `tests/services/test_homepage_calendar_tracks_follow_services_rows.py`, `tests/services/test_homepage_longhorn_widget_url.py`, `tests/services/test_host_job_series_have_a_board.py`, `tests/services/test_k8s_workloads_rule_literals.py`, `tests/services/test_lan_address_rules_agree.py`, `tests/services/test_metric_drop_regexes.py`, `tests/services/test_monitor_bridge_modules.py`, `tests/services/test_observability_cluster_ips_pinned.py`, `tests/services/test_observability_config_rolls_one_workload.py`, `tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py`, `tests/services/test_scrape_target_flap_rule.py`, `tests/services/test_smtp_wiring.py`, `tests/services/test_storage_rule_literals.py`
- 34 files: `scripts/dev/tests/test_fanout_headless_imports.py`, `scripts/dev/tests/test_run_as_cron.py`, `scripts/diagnostics/tests/test_probe_bad_since_census.py`, `scripts/diagnostics/tests/test_probe_boundaries.py`, `scripts/diagnostics/tests/test_ui_smoke_helpers.py`, `scripts/docs/tests/test_catalog_lib_layout.py`, `scripts/docs/tests/test_fragments_lib_layout.py`, `scripts/lib/tests/test_ansible_bool_rationale.py`, `scripts/lib/tests/test_entry_point_census_collects_cheaply.py`, `scripts/lib/tests/test_help_check_rejects_work_output.py`, `scripts/lib/tests/test_script_classify_surface.py`, `scripts/renderer/tests/test_renderer_size_ceiling.py`, `scripts/secrets_mgmt/tests/test_secrets_lib_layout.py`, `scripts/tests/test_census_roots_cover_host_lib.py`, `scripts/tests/test_census_rows_test_modules.py`, `scripts/tests/test_census_rows_test_renders.py`, `scripts/tests/test_fqdn_links_js.py`, `scripts/tests/test_mkdocs_config.py`, `scripts/tests/test_prek_files_cover_entry_imports.py`, `scripts/tests/test_prek_files_cover_pythonpath_imports.py`, `scripts/tests/test_renovate_automerge_does_not_chase_master.py`, `scripts/tests/test_renovate_cluster_pin_rules.py`, `scripts/tests/test_renovate_cosign_release.py`, `scripts/tests/test_renovate_digest_soak_without_timestamp.py`, `scripts/tests/test_renovate_docker_engine_pins.py`, `scripts/tests/test_renovate_manager_overlap.py`, `scripts/tests/test_renovate_manual_rules_push_weekly.py`, `scripts/tests/test_renovate_nginx_digest_soak.py`, `scripts/tests/test_renovate_python_group.py`, `scripts/tests/test_renovate_renderer_closure_no_automerge.py`, `scripts/tests/test_script_bootstraps_present.py`, `scripts/tests/test_scripts_import_direction.py`, `scripts/tests/test_table_filter_js.py`, `scripts/tests/test_vale_scope.py`
- 29 files: `hosts/components/registry-gc/tests/test_ghcr_mirror_prune.py`, `hosts/lib/tests/test_hosts_tree_is_tracked.py`, `hosts/lib/tests/test_reconcile_stub_imports.py`, `hosts/tests/test_docs_refresh_failure_path.py`, `hosts/tests/test_etcd_restore_drill_verify.py`, `hosts/tests/test_eval_run_failure_path.py`, `hosts/tests/test_health_cron_boot_grace.py`, `tests/k8s/test_uncovered_roles_justifications_resolve.py`, `tests/repo/test_census_rows_python.py`, `tests/repo/test_census_rows_suite.py`, `tests/repo/test_claude_md_paragraphs_have_one_home.py`, `tests/repo/test_cross_directory_importer_bootstraps_sys_path.py`, `tests/repo/test_docs_relative_links_resolve.py`, `tests/repo/test_flux_bootstrap_section.py`, `tests/repo/test_installed_units_log_level.py`, `tests/repo/test_k8s_render_prebuild.py`, `tests/repo/test_role_shipping_python_has_tests.py`, `tests/repo/test_row_table.py`, `tests/repo/test_scripts_suite_collects_on_its_own.py`, `tests/repo/test_tracked_python_hazards.py`, `tests/repo/test_ty_config_covers_the_repo.py`, `tests/services/test_dashboards_read_no_gitops_tick_series.py`, `tests/services/test_jellyfin_plugin_allowlist.py`, `tests/services/test_jellyfin_plugin_pins_hold_across_a_restart.py`, `tests/services/test_pi_peer_backup_push_line_is_readable.py`, `tests/services/test_registry_gc_dockerhub_mirror.py`, `tests/services/test_registry_gc_ghcr_mirror.py`, `tests/services/test_registry_gc_mirror_job_report.py`, `tests/services/test_registry_gc_state_file_is_json.py`
- 15 files: `k8s/roles/monitor-bridge/tests/test_bridge_wan_rules.py`, `tests/services/test_alertmanager_autofix_inhibit.py`, `tests/services/test_alertmanager_blackbox_inhibit.py`, `tests/services/test_alertmanager_flux_inhibit.py`, `tests/services/test_alertmanager_pi_inhibit.py`, `tests/services/test_alertmanager_routing_a1_hosts.py`, `tests/services/test_alertmanager_routing_a6_bridge.py`, `tests/services/test_alertmanager_routing_b1_k3s_crons.py`, `tests/services/test_alertmanager_routing_b6a_host_crons.py`, `tests/services/test_alertmanager_routing_b6c_registry_gc.py`, `tests/services/test_alertmanager_routing_b7_pods.py`, `tests/services/test_alertmanager_routing_b8_probes.py`, `tests/services/test_homepage_block_label_overrides.py`, `tests/services/test_smtp_wiring_docstrings.py`, `tests/services/test_warning_escalation_exemptions.py`
- 14 files: `hosts/components/claude-code/tests/test_takeover_check_flux_selection.py`, `hosts/lib/tests/test_homelab_reconcile.py`, `hosts/lib/tests/test_homelab_reconcile_batched_reads.py`, `hosts/lib/tests/test_homelab_reconcile_timing.py`, `hosts/lib/tests/test_homelab_reconcile_udev.py`, `hosts/lib/tests/test_homelab_updater.py`, `hosts/lib/tests/test_homelab_updater_fetch.py`, `hosts/lib/tests/test_reconcile_activation.py`, `hosts/lib/tests/test_reconcile_hold_way_out.py`, `hosts/lib/tests/test_reconcile_libvirt_arm.py`, `hosts/lib/tests/test_reconcile_lines_arm.py`, `hosts/lib/tests/test_reconcile_shadow_handoff.py`, `hosts/lib/tests/test_reconcile_ufw_arm.py`, `hosts/lib/tests/test_reconcile_users_arm.py`
- 12 files: `scripts/backup/tests/test_longhorn_reap_backups_cli.py`, `scripts/backup/tests/test_longhorn_reap_backups_modes_cli.py`, `scripts/backup/tests/test_longhorn_reap_entrypoints.py`, `scripts/backup/tests/test_longhorn_reap_snapshots_cli.py`, `scripts/backup/tests/test_longhorn_reap_subcommands.py`, `scripts/deploy_tools/tests/test_bootstrap_drill_check_wiring.py`, `scripts/deploy_tools/tests/test_land_fakes_cleanup.py`, `scripts/deploy_tools/tests/test_land_imports.py`, `scripts/deploy_tools/tests/test_land_shim.py`, `scripts/deploy_tools/tests/test_land_shim_skips_lint_group.py`, `scripts/validate/tests/test_every_validator_has_a_red_proof.py`, `scripts/z2m/tests/test_set_device_option.py`
- 11 files: `scripts/dev/tests/test_findings_snapshot_import.py`, `scripts/lib/tests/test_claude_worktree_import.py`, `scripts/lib/tests/test_one_guard_env_factory.py`, `scripts/tests/test_census_pass_prebuild.py`, `scripts/tests/test_census_rows_parse_once.py`, `scripts/tests/test_renovate_auto_replace.py`, `scripts/tests/test_renovate_dockerfiles.py`, `scripts/tests/test_renovate_managers.py`, `scripts/tests/test_renovate_pi_compose_pins.py`, `scripts/tests/test_renovate_release_urls.py`, `scripts/tests/test_test_module_bootstraps_present.py`
- 9 files: `.claude/hooks/tests/test_hook_shim_fail_open.py`, `.claude/hooks/tests/test_project_settings_shape.py`, `.claude/hooks/tests/test_session_health_no_wall_clock_bound.py`, `.claude/hooks/tests/test_setup_wiring.py`, `.claude/hooks/tests/test_uv_python_in_batch.py`, `hosts/components/k3s-host-checks/tests/test_longhorn_backup_grace_cron.py`, `hosts/components/k3s-host-checks/tests/test_longhorn_backup_health_reader.py`, `tests/staging/test_bootstrap_drill_guest_run.py`, `tests/staging/test_suc_rehearsal_guest_races.py`
- 8 files: `hosts/lib/tests/test_crowdsec_probe_component.py`, `hosts/lib/tests/test_pi_compose_autoheal.py`, `hosts/lib/tests/test_pi_compose_docker_proxy.py`, `hosts/lib/tests/test_pi_watchdog_dropin.py`, `hosts/tests/test_docker_daemons_gomemlimit_headroom.py`, `hosts/tests/test_pi_node_exporter_host_unit.py`, `hosts/tests/test_pi_service_lists_census.py`, `tests/deploy/test_platform_filter_real_inventory.py`
- 8 files: `hosts/tests/test_claude_cgroup_metrics.py`, `k8s/roles/monitor-bridge/tests/test_domain_registration_expiry.py`, `tests/longhorn/test_longhorn_restore_drill_byte_floor.py`, `tests/repo/test_adr_links.py`, `tests/repo/test_ci_arm64_tools_download_gates.py`, `tests/repo/test_ci_flux_cli_is_cached.py`, `tests/repo/test_ci_sops_kind_cosign_are_cached.py`, `tests/repo/test_host_tools_cache.py`
- 6 files: `hosts/lib/tests/test_bootstrap_host.py`, `hosts/lib/tests/test_bootstrap_host_baseline.py`, `hosts/lib/tests/test_bootstrap_host_local_build.py`, `hosts/lib/tests/test_bootstrap_host_log2ram.py`, `hosts/lib/tests/test_bootstrap_host_seed.py`, `hosts/lib/tests/test_bootstrap_host_steps.py`
- 6 files: `hosts/tests/test_host_maintenance.py`, `hosts/tests/test_systemd_units_check.py`, `tests/repo/test_host_test_image.py`, `tests/repo/test_promote_drill_workflow.py`, `tests/repo/test_promote_workflow_retry.py`, `tests/services/test_netns_reset_restores_resolv_conf.py`
- 6 files: `scripts/lib/tests/test_tree_lock.py`, `scripts/tests/test_renovate_flux_cli.py`, `scripts/tests/test_renovate_homelab_tools_pins.py`, `scripts/tests/test_renovate_host_file_copies.py`, `scripts/tests/test_renovate_kind_pins.py`, `scripts/tests/test_renovate_nfpm_pin.py`
- 5 files: `hosts/lib/tests/test_box_dev_tooling.py`, `hosts/lib/tests/test_server_dev_tooling.py`, `hosts/tests/test_aide_check_names_its_config.py`, `hosts/tests/test_k3s_registries_mirror_ghcr.py`, `hosts/tests/test_netplan_wifi_fallback.py`
- 5 files: `hosts/tests/test_aide_check_classify.py`, `hosts/tests/test_aide_check_package_activity.py`, `hosts/tests/test_aide_check_snapd_udev.py`, `hosts/tests/test_eval_sweep_cron.py`, `hosts/tests/test_rkhunter_check.py`
- 5 files: `k8s/roles/monitor-bridge/tests/test_bridge_b2_rules.py`, `tests/services/test_alertmanager_reboot_mute.py`, `tests/services/test_alertmanager_routing_b3_gitops.py`, `tests/services/test_alertmanager_routing_b5_pi.py`, `tests/services/test_alertmanager_routing_fanout_idle.py`
- 4 files: `hosts/components/fake-remux/tests/test_fake_remux_entrypoint.py`, `hosts/tests/test_etcd_restore_drill_cron.py`, `hosts/tests/test_k3s_oidc_trusts_authelia.py`, `hosts/tests/test_k3s_server_flags_migration.py`
- 4 files: `hosts/lib/tests/test_host_package_tmpfiles_users.py`, `hosts/lib/tests/test_pi_heartbeats_component.py`, `hosts/tests/test_k3s_unit_logging.py`, `hosts/tests/test_worktree_holders_install.py`
- 4 files: `hosts/tests/test_claude_code_on_daniel_server.py`, `hosts/tests/test_claude_fanout_server_lander.py`, `hosts/tests/test_claude_memory_sync.py`, `hosts/tests/test_claude_pipeline_timeline.py`
- 4 files: `scripts/deploy_tools/tests/test_promote_reconcile.py`, `scripts/deploy_tools/tests/test_promote_settle.py`, `scripts/deploy_tools/tests/test_promote_settle_owner.py`, `scripts/deploy_tools/tests/test_promote_settle_retry.py`
- 4 files: `scripts/renderer/tests/test_renderer_determinism_0.py`, `scripts/renderer/tests/test_renderer_determinism_1.py`, `scripts/renderer/tests/test_renderer_determinism_2.py`, `scripts/renderer/tests/test_renderer_determinism_3.py`
- 4 files: `tests/k8s/test_jsonpath_quoting.py`, `tests/k8s/test_metallb_vendored_manifest.py`, `tests/k8s/test_pod_template_hygiene.py`, `tests/k8s/test_priorityclass_omits_defaulted_fields.py`
- 4 files: `tests/k8s/test_traefik_crds.py`, `tests/longhorn/test_longhorn_control_plane_resources.py`, `tests/longhorn/test_longhorn_csi_secret_grant.py`, `tests/services/test_pihole_lan_reverse_forwarding.py`
- 4 files: `tests/longhorn/test_longhorn_restore_drill_nopvc.py`, `tests/longhorn/test_longhorn_restore_drill_operator_pin.py`, `tests/longhorn/test_longhorn_restore_drill_oversize.py`, `tests/longhorn/test_longhorn_restore_drill_retry.py`
- 4 files: `tests/services/test_anisync_pin_matches_server.py`, `tests/services/test_introskipper_install.py`, `tests/services/test_mediacleaner_install.py`, `tests/services/test_webhook_plugin_install.py`
- 3 files: `.claude/hooks/tests/test_hook_scripts_executable.py`, `.claude/hooks/tests/test_run_hook.py`, `tests/services/test_pi_peer_backup_host_key_failure.py`
- 3 files: `hosts/lib/tests/test_k3s_gates_component.py`, `hosts/lib/tests/test_nut_secondary_component.py`, `hosts/tests/test_k3s_agent_flags_migration.py`
- 3 files: `hosts/tests/test_github_interaction_limit.py`, `hosts/tests/test_github_ruleset_drift.py`, `tests/staging/test_suc_rehearsal_mode.py`
- 3 files: `k8s/roles/node-exporter/tests/test_dockerhub_mirror_pull.py`, `k8s/roles/node-exporter/tests/test_ghcr_mirror_pull.py`, `k8s/roles/node-exporter/tests/test_iscsi_sessions.py`
- 3 files: `scripts/deploy_tools/tests/test_land_annotation_stays_out_of_syslog.py`, `scripts/validate/tests/test_renovate_config_retry.py`, `scripts/validate/tests/test_vale_sync_guard.py`
- 3 files: `tests/repo/test_python_version_consistency.py`, `tests/services/test_code_server_build_is_pinned.py`, `tests/services/test_homelab_mcp_pins.py`
- 2 files: `hosts/components/claude-code/tests/test_takeover_check_install.py`, `hosts/tests/test_claude_login_slice_caps.py`
- 2 files: `hosts/lib/tests/test_homelab_reconcile_restart.py`, `hosts/lib/tests/test_pi_sshd_apt_and_tuning.py`
- 2 files: `hosts/lib/tests/test_pi_compose_scripts.py`, `tests/repo/test_leakguard_child_env.py`
- 2 files: `hosts/lib/tests/test_server_host_hardening.py`, `hosts/lib/tests/test_server_sshd_apt_and_tuning.py`
- 2 files: `hosts/tests/test_agent_operator_config.py`, `hosts/tests/test_claude_bg_resume_unit.py`
- 2 files: `hosts/tests/test_aide_check_role_written.py`, `tests/services/test_not_reporting_rule_deadlines.py`
- 2 files: `hosts/tests/test_coredns_image_follows_bundled_manifest.py`, `tests/services/test_tdarr_vaapi_encode_init_container.py`
- 2 files: `hosts/tests/test_docs_refresh_records_shard_weights.py`, `tests/services/test_qbittorrent_sidecar_resets_the_netns.py`
- 2 files: `hosts/tests/test_host_check_textfiles.py`, `hosts/tests/test_loki_route_witness.py`
- 2 files: `hosts/tests/test_pi_gz_integrity_sweep.py`, `hosts/tests/test_pi_heartbeat_textfiles.py`
- 2 files: `k8s/roles/arr-notification/tests/test_seed_job.py`, `k8s/roles/sonarr/tests/test_sonarr_verify.py`
- 2 files: `scripts/deploy_tools/tests/test_land_arm_merge_through_the_shim.py`, `scripts/deploy_tools/tests/test_land_fresh_slot.py`
- 2 files: `scripts/dev/tests/test_ci_failures.py`, `scripts/lib/tests/test_proc_testing.py`
- 2 files: `scripts/lib/tests/test_invocation_sites.py`, `scripts/tests/test_invoker_paths_resolve.py`
- 2 files: `tests/deploy/test_restart_on_narrows_the_restart_signals.py`, `tests/k8s/test_forward_only_tags.py`
- 2 files: `tests/k8s/test_aggregated_clusterroles.py`, `tests/k8s/test_k8s_manifests_metallb.py`
- 2 files: `tests/k8s/test_headlamp_plugins_install.py`, `tests/k8s/test_k8s_headlamp_oidc.py`
- 2 files: `tests/k8s/test_reboot_window_cronjobs_age_out.py`, `tests/services/test_homepage_widget_urls_carry_no_credentials.py`
- 2 files: `tests/longhorn/test_longhorn_restore_drill_empty_waiver.py`, `tests/longhorn/test_longhorn_restore_drill_sqlite.py`
- 2 files: `tests/repo/test_ci_tool_pins_do_not_move_backwards.py`, `tests/repo/test_module_length_ratchet.py`
- 2 files: `tests/repo/test_host_package_build_step.py`, `tests/repo/test_host_package_cases_run_on_their_inputs.py`
- 2 files: `tests/repo/test_k8s_render_role_template.py`, `tests/services/test_traefik_http_entrypoint_crowdsec.py`
- 2 files: `tests/services/test_karakeep_chrome_rollout.py`, `tests/services/test_scrutiny_rollouts.py`
- 2 files: `tests/staging/test_bootstrap_drill_mode.py`, `tests/staging/test_staging_egress_fence_fires.py`

### Test files that cover no measured line inside a test

These are not deletion candidates on coverage grounds. Most are static ratchet or census
tests that read the tree, tests of code outside the measured source (`evals/`, the test
helpers), or tests whose subject runs only in an unmeasured child process.

- `evals/tests/test_ablate.py`
- `evals/tests/test_ablate_budget.py`
- `evals/tests/test_eval_cases.py`
- `evals/tests/test_ranking.py`
- `evals/tests/test_trend.py`
- `hosts/lib/tests/test_bootstrap_host_pins.py`
- `hosts/lib/tests/test_component_retired_stamp_fragments.py`
- `hosts/lib/tests/test_k3s_gates_operator_reads.py`
- `hosts/lib/tests/test_pi_compose_network_measurement.py`
- `hosts/lib/tests/test_reconcile_size_census.py`
- `hosts/tests/test_cron_scripts_publish_via_pr.py`
- `hosts/tests/test_crons_gate_their_commit_on_the_suite.py`
- `hosts/tests/test_docs_refresh_heartbeat.py`
- `hosts/tests/test_renovate_agent_modules_are_shipped.py`
- `hosts/tests/test_ufw_sysctl_keeps_log_martians.py`
- `k8s/roles/crowdsec/tests/test_jobs_image.py`
- `k8s/roles/home-assistant/tests/test_dashboard_display_and_ticker_traces.py`
- `k8s/roles/home-assistant/tests/test_diagnostics_macros.py`
- `k8s/roles/home-assistant/tests/test_fallback_wake_overrides_manual_off.py`
- `k8s/roles/home-assistant/tests/test_fan_macros.py`
- `k8s/roles/home-assistant/tests/test_ha_round_semantics.py`
- `k8s/roles/home-assistant/tests/test_lighting_macros.py`
- `k8s/roles/home-assistant/tests/test_logger_filter_drops_refresh_token.py`
- `k8s/roles/home-assistant/tests/test_lux_gate_sync.py`
- `k8s/roles/home-assistant/tests/test_macro_coverage.py`
- `k8s/roles/home-assistant/tests/test_manual_detectors_need_a_user_id.py`
- `k8s/roles/home-assistant/tests/test_morning_reset_fallback_relights.py`
- `k8s/roles/home-assistant/tests/test_notify_runs_after_state_changes.py`
- `k8s/roles/home-assistant/tests/test_recorder_keeps_automation_attribution.py`
- `k8s/roles/home-assistant/tests/test_tap_dial_condition_matches_branches.py`
- `k8s/roles/home-assistant/tests/test_threshold_sources_cover_batteries_and_watch.py`
- `k8s/roles/home-assistant/tests/test_ventilation_macros.py`
- `k8s/roles/home-assistant/tests/test_wake_gates_count_the_fallback.py`
- `k8s/roles/home-assistant/tests/test_warm_writes_arm_color_tracker.py`
- `k8s/roles/karakeep/tests/test_time_tagger_wiring.py`
- `k8s/roles/monitor-bridge/tests/test_bridge_common_docstrings.py`
- `k8s/roles/monitor-bridge/tests/test_bridge_size_cap.py`
- `k8s/roles/monitor-bridge/tests/test_config_credential_shape.py`
- `k8s/roles/monitor-bridge/tests/test_config_reads_the_rendered_env.py`
- `k8s/roles/n8n/tests/test_n8n_verify_closed_port.py`
- `scripts/diagnostics/tests/test_ui_smoke.py`
- `scripts/diagnostics/tests/test_ui_smoke_grafana.py`
- `scripts/diagnostics/tests/test_ui_state_reload.py`
- `tests/deploy/test_containers_list_roles_exist.py`
- `tests/deploy/test_extra_rollout_names.py`
- `tests/deploy/test_inventory_block_scalars_have_no_comment_shaped_lines.py`
- `tests/deploy/test_land_help_names_every_verdict.py`
- `tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py`
- `tests/deploy/test_renovate_edge_images_stay_one_per_pr.py`
- `tests/deploy/test_tree_lock_single_definition.py`
- `tests/k8s/test_alloy_labels_host_program.py`
- `tests/k8s/test_alloy_redacts_arr_apikey.py`
- `tests/k8s/test_alloy_redacts_ha_refresh_token.py`
- `tests/k8s/test_alloy_redacts_webhook_and_session_secrets.py`
- `tests/k8s/test_alloy_ships_audit_log.py`
- `tests/k8s/test_arr_deployments_share_one_macro.py`
- `tests/k8s/test_base_images_digest_pinned.py`
- `tests/k8s/test_configmap_keys_not_absorbed.py`
- `tests/k8s/test_container_security_context.py`
- `tests/k8s/test_container_security_context_uses_the_macro.py`
- `tests/k8s/test_deploy_strategy.py`
- `tests/k8s/test_failed_pods_outside_homelab_have_a_cleanup.py`
- `tests/k8s/test_flux_priority_classes_come_from_cluster_base.py`
- `tests/k8s/test_k8s_claims.py`
- `tests/k8s/test_netpol_baseline_exempt_gates.py`
- `tests/k8s/test_netpol_baseline_labels.py`
- `tests/k8s/test_readiness_coverage.py`
- `tests/k8s/test_readonly_identity_bindings.py`
- `tests/k8s/test_root_security_context_is_written_out_in_full.py`
- `tests/k8s/test_volume_names_descriptive.py`
- `tests/k8s/test_workload_shell_uses_the_macros.py`
- `tests/longhorn/test_engine_upgrade_limit_is_positive.py`
- `tests/longhorn/test_longhorn_reap_never_scheduled.py`
- `tests/longhorn/test_longhorn_restore_drill_image.py`
- `tests/longhorn/test_volume_cr_has_no_volumename.py`
- `tests/longhorn/test_volume_revert_runbook_text.py`
- `tests/repo/test_cold_start_runbooks_link_bootstrap.py`
- `tests/repo/test_component_contract_row.py`
- `tests/repo/test_entry_points_outside_scripts_answer_help.py`
- `tests/repo/test_fix_skeptic_pass_documented.py`
- `tests/repo/test_flux_recovery_runbook.py`
- `tests/repo/test_gitleaks_allowlist_paths_are_anchored.py`
- `tests/repo/test_gitleaks_manifest_digest_allowlist.py`
- `tests/repo/test_gitleaks_secret_key_name_allowlist.py`
- `tests/repo/test_guard_tests_read_renders_not_templates.py`
- `tests/repo/test_helpers.py`
- `tests/repo/test_leakguard.py`
- `tests/repo/test_phase_timing.py`
- `tests/repo/test_prek_hooks_run_only_at_commit.py`
- `tests/repo/test_prek_uv_entries_are_frozen.py`
- `tests/repo/test_process_waits.py`
- `tests/repo/test_ratchet_rules.py`
- `tests/repo/test_ratchet_tighten.py`
- `tests/repo/test_render_gate.py`
- `tests/repo/test_render_share.py`
- `tests/repo/test_repair_commands_cannot_deadlock_on_their_own_guard.py`
- `tests/repo/test_reviewer_agents_carry_no_hand_reflag_list.py`
- `tests/repo/test_shell_entry_points_answer_help.py`
- `tests/repo/test_static_ratchet_hook.py`
- `tests/repo/test_unit_kubeconfig_row.py`
- `tests/repo/test_uptime_robot_keywords.py`
- `tests/repo/test_xdist_width.py`
- `tests/services/test_alert_history_board_reads_alerts.py`
- `tests/services/test_claude_cgroup_consumers_agree_with_the_writer.py`
- `tests/services/test_crowdsec_config_install_seeds_staged_tree.py`
- `tests/services/test_crowdsec_hub_install_stages_the_hub_tree.py`
- `tests/services/test_dashboard_queries_match_this_clusters_labels.py`
- `tests/services/test_headlamp_widget_mapping_order.py`
- `tests/services/test_healthchecks_pings.py`
- `tests/services/test_ical_proxy_pins_match_uv_lock.py`
- `tests/services/test_karakeep_backend_policies.py`
- `tests/services/test_karakeep_public_bypass.py`
- `tests/services/test_karakeep_time_tagger_script.py`
- `tests/services/test_n8n_build_is_pinned.py`
- `tests/services/test_n8n_web_port_is_fenced.py`
- `tests/services/test_pi_peer_backup_ssh_key_secret.py`
- `tests/services/test_scrutiny_influxdb_policy.py`
- `tests/services/test_traefik_access_log_drop.py`

## List 2: modules with no covered line

In scope are the tracked `.py` files under `scripts/`, `hosts/lib/` and each role's
`k8s/roles/<role>/files/`, test modules excluded: 562 modules. One has no covered line in any
context, at import or in a child process included:

- `k8s/roles/karakeep/files/karakeep-time-tagger.py`, named in 11 test files

`karakeep-time-tagger.py` is upstream's script, vendored and pinned by sha256. The karakeep
tests parse it with `ast` and never run it, so it is not a deletion candidate.

### Modules reached only outside any test's context

These modules have covered lines, but none inside a test: they ran only at import, at
collection, or in a child process. A module listed here may be imported by a test and never
called by one.

- `hosts/lib/reconcile_lib/__init__.py`
- `k8s/roles/artifacts/files/_gui_html.py`
- `k8s/roles/monitor-bridge/files/check_table.py`
- `k8s/roles/sonarr/files/sonarr_verify.py`
- `scripts/backup/longhorn_reap.py`
- `scripts/dev/fanout_lib/review/api.py`
- `scripts/dev/tighten_ratchets.py`
- `scripts/diagnostics/probe_lib/subcommands.py`
- `scripts/docs/catalog_lib/catalog_model.py`
- `scripts/infra_map/constants.py`
- `scripts/lib/_claude_worktree.py`
- `scripts/lib/tree_lock.py`
- `scripts/renderer/renderer_lib/bundle_readme.py`
