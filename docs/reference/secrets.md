---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-10-17 18:17 UTC
generated_sha: 4462b2540
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

201 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 200 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 580 |

## assisted

needs a human to mint the new value, then `secret_rotation.py rotate`.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 182 |
| `anthropic_api_key` | 2026-09-23 | 2027-08-28 | 315 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 317 |
| `authelia_agent_password` | 2026-08-27 | 2027-08-18 | 305 |
| `authelia_agent_password_argon2` | 2026-07-16 | 2027-06-18 | 244 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 63 |
| `authelia_claude_password_argon2` | 2025-12-25 | 2026-12-20 | 64 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 101 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | -32 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 50 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 106 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 194 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 261 |
| `authelia_password_argon2` | 2026-02-12 | 2027-01-18 | 93 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 229 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 52 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 311 |
| `become_password` | 2026-08-30 | 2027-08-29 | 316 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 309 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 206 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -51 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -48 |
| `claude_code_agent_gh_token` | 2026-02-12 | 2027-01-14 | 89 |
| `claude_code_triage_claude_oauth_token` | 2026-04-10 | 2027-03-23 | 157 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -38 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 152 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 316 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 301 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 209 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | 33 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 198 |
| `google_assistant_project_id` | 2026-06-25 | 2027-05-31 | 226 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 52 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 106 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 294 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 217 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 230 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 203 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 197 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | 24 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 307 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -68 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -50 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 302 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 109 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 100 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 143 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -57 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | 16 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -68 |
| `longhorn_b2_application_key` | 2026-05-29 | 2027-05-13 | 208 |
| `longhorn_b2_bucket` | 2026-05-29 | 2027-05-14 | 209 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 199 |
| `longhorn_b2_key_id` | 2026-05-29 | 2027-05-04 | 199 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | 2 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 215 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 163 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | -29 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | 17 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 137 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 144 |
| `peanut_password` | 2026-10-16 | 2027-10-03 | 351 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 163 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 259 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | 9 |
| `prometheus_kuma_api_key` | 2025-09-18 | 2026-08-21 | -57 |
| `prowlarr_api_key` | 2026-08-30 | 2027-08-10 | 297 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -50 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 183 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 175 |
| `radarr_api_key` | 2026-08-29 | 2027-08-05 | 292 |
| `renovate_agent_claude_oauth_token` | 2026-06-01 | 2027-05-21 | 216 |
| `renovate_agent_gh_token` | 2026-07-21 | 2027-07-13 | 269 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 255 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 115 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 189 |
| `sonarr_api_key` | 2026-08-29 | 2027-08-28 | 315 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 311 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -80 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 53 |
| `uptime_kuma_password` | 2026-03-24 | 2027-03-11 | 145 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 288 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 316 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 316 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 148 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 162 |
| `flux_github_status_token` | 2026-10-12 | 2027-09-16 | 334 |
| `gitops_deploy_discord_webhook` | 2026-10-04 | 2027-09-07 | 325 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 170 |
| `homelab_builder_github_token` | 2026-05-31 | 2027-05-21 | 216 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 155 |
| `monitor_bridge_deploy_pipeline_github_token` | 2026-10-11 | 2027-09-22 | 340 |
| `monitor_discord_webhook_url` | 2026-09-19 | 2027-08-27 | 314 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | 1 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 291 |
| `wifi_password` | 2026-07-20 | 2027-06-24 | 250 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 112 |

## external-90d

lives in a third-party system and expires after 90 days; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `promote_token` | 2026-10-12 | 2027-01-04 | 79 |

## auto

rotated unattended by the weekly secret-rotate cron.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `aide_daniel_box_push_token` | 2026-08-24 | 2027-02-19 | 125 |
| `aide_daniel_pi_push_token` | 2026-07-07 | 2026-12-20 | 64 |
| `aide_daniel_server_push_token` | 2026-07-28 | 2027-01-17 | 92 |
| `arr_autoblock_push_token` | 2026-08-30 | 2027-02-26 | 132 |
| `artifacts_sync_push_token` | 2026-05-24 | 2026-11-12 | 26 |
| `claude_otel_push_token` | 2026-08-28 | 2027-02-14 | 120 |
| `cloudflare_ddns_direct_push_token` | 2026-08-30 | 2027-02-26 | 132 |
| `cloudflare_ddns_proxied_push_token` | 2026-08-30 | 2027-02-16 | 122 |
| `crowdsec_remote_allowlist_push_token` | 2026-06-05 | 2026-11-18 | 32 |
| `daniel_box_disk_push_token` | 2026-08-28 | 2027-02-16 | 122 |
| `docs_refresh_push_token` | 2026-08-06 | 2027-01-19 | 94 |
| `etcd_drill_fence_push_token` | 2026-07-26 | 2027-01-19 | 94 |
| `etcd_drill_full_push_token` | 2026-07-21 | 2027-01-13 | 88 |
| `etcd_snapshot_push_token` | 2026-08-28 | 2027-02-22 | 128 |
| `homelab_eval_push_token` | 2026-10-09 | 2027-04-01 | 166 |
| `interaction_limit_push_token` | 2026-05-15 | 2026-10-30 | 13 |
| `kuma_status_page_sync_push_token` | 2026-07-08 | 2026-12-28 | 72 |
| `live_drift_push_token` | 2026-08-15 | 2027-02-03 | 109 |
| `loki_route_witness_daniel_server_push_token` | 2026-05-14 | 2026-11-08 | 22 |
| `loki_route_witness_push_token` | 2026-05-03 | 2026-10-30 | 13 |
| `longhorn_backup_push_token` | 2026-08-28 | 2027-02-14 | 120 |
| `manifest_prune_push_token` | 2026-08-28 | 2027-02-13 | 119 |
| `mkv_attachment_repair_push_token` | 2026-04-02 | 2026-09-26 | -21 |
| `monitor_bridge_appsec_push_token` | 2026-08-28 | 2027-02-16 | 122 |
| `monitor_bridge_arr_queue_push_token` | 2026-08-30 | 2027-02-19 | 125 |
| `monitor_bridge_b2_reachable_push_token` | 2026-08-30 | 2027-02-17 | 123 |
| `monitor_bridge_b2_storage_push_token` | 2026-08-30 | 2027-02-25 | 131 |
| `monitor_bridge_bazarr_push_token` | 2026-08-16 | 2027-02-02 | 108 |
| `monitor_bridge_cert_push_token` | 2026-08-30 | 2027-02-19 | 125 |
| `monitor_bridge_cloudflare_drift_push_token` | 2026-08-28 | 2027-02-14 | 120 |
| `monitor_bridge_cluster_targets_push_token` | 2026-08-30 | 2027-02-20 | 126 |
| `monitor_bridge_configarr_push_token` | 2026-08-28 | 2027-02-17 | 123 |
| `monitor_bridge_cpu_push_token` | 2026-08-30 | 2027-02-20 | 126 |
| `monitor_bridge_discord_push_token` | 2026-08-30 | 2027-02-26 | 132 |
| `monitor_bridge_disk_push_token` | 2026-08-30 | 2027-02-15 | 121 |
| `monitor_bridge_etcd_db_size_push_token` | 2026-05-23 | 2026-11-09 | 23 |
| `monitor_bridge_etcd_drill_push_token` | 2026-04-28 | 2026-10-18 | 1 |
| `monitor_bridge_fake_remux_push_token` | 2026-08-28 | 2027-02-19 | 125 |
| `monitor_bridge_fake_remux_replace_push_token` | 2026-08-28 | 2027-02-17 | 123 |
| `monitor_bridge_gitops_alive_push_token` | 2026-08-30 | 2027-02-23 | 129 |
| `monitor_bridge_gitops_status_push_token` | 2026-08-30 | 2027-02-25 | 131 |
| `monitor_bridge_ha_push_token` | 2026-08-30 | 2027-02-14 | 120 |
| `monitor_bridge_healthchecks_drift_push_token` | 2026-07-31 | 2027-01-16 | 91 |
| `monitor_bridge_home_allowlist_push_token` | 2026-08-28 | 2027-02-10 | 116 |
| `monitor_bridge_host_packages_push_token` | 2026-07-23 | 2027-01-10 | 85 |
| `monitor_bridge_host_temp_push_token` | 2026-08-09 | 2027-02-01 | 107 |
| `monitor_bridge_janitorr_push_token` | 2026-08-30 | 2027-02-15 | 121 |
| `monitor_bridge_k8s_workloads_push_token` | 2026-08-30 | 2027-02-12 | 118 |
| `monitor_bridge_kubelet_readonly_push_token` | 2026-08-03 | 2027-01-18 | 93 |
| `monitor_bridge_kuma_notify_failures_push_token` | 2026-06-08 | 2026-11-27 | 41 |
| `monitor_bridge_loki_push_token` | 2026-08-30 | 2027-02-14 | 120 |
| `monitor_bridge_loki_reachable_push_token` | 2026-08-30 | 2027-02-17 | 123 |
| `monitor_bridge_longhorn_volumes_push_token` | 2026-09-13 | 2027-02-28 | 134 |
| `monitor_bridge_mem_push_token` | 2026-08-30 | 2027-02-23 | 129 |
| `monitor_bridge_n8n_push_token` | 2026-08-30 | 2027-02-23 | 129 |
| `monitor_bridge_oom_push_token` | 2026-08-30 | 2027-02-18 | 124 |
| `monitor_bridge_pi_peers_push_token` | 2026-08-30 | 2027-02-24 | 130 |
| `monitor_bridge_pi_push_token` | 2026-08-30 | 2027-02-25 | 131 |
| `monitor_bridge_prometheus_push_token` | 2026-08-30 | 2027-02-19 | 125 |
| `monitor_bridge_promtail_dropped_push_token` | 2026-08-30 | 2027-02-26 | 132 |
| `monitor_bridge_prowlarr_indexers_push_token` | 2026-08-30 | 2027-02-18 | 124 |
| `monitor_bridge_pvc_push_token` | 2026-05-28 | 2026-11-16 | 30 |
| `monitor_bridge_r2_usage_push_token` | 2026-08-30 | 2027-02-13 | 119 |
| `monitor_bridge_renovate_alive_push_token` | 2026-08-28 | 2027-02-13 | 119 |
| `monitor_bridge_restarts_push_token` | 2026-08-30 | 2027-02-21 | 127 |
| `monitor_bridge_scrutiny_push_token` | 2026-08-30 | 2027-02-15 | 121 |
| `monitor_bridge_snapshot_headroom_push_token` | 2026-05-31 | 2026-11-14 | 28 |
| `monitor_bridge_speedtest_push_token` | 2026-04-20 | 2026-10-17 | 0 |
| `monitor_bridge_swallowed_verdicts_push_token` | 2026-06-20 | 2026-12-04 | 48 |
| `monitor_bridge_targets_push_token` | 2026-08-30 | 2027-02-14 | 120 |
| `monitor_bridge_traefik_404_push_token` | 2026-06-05 | 2026-12-02 | 46 |
| `monitor_bridge_traefik_421_push_token` | 2026-06-20 | 2026-12-16 | 60 |
| `monitor_bridge_traefik_latency_push_token` | 2026-08-30 | 2027-02-24 | 130 |
| `monitor_bridge_traefik_push_token` | 2026-08-30 | 2027-02-18 | 124 |
| `monitor_bridge_ups_push_token` | 2026-08-30 | 2027-02-18 | 124 |
| `monitor_bridge_wan_reachable_push_token` | 2026-06-25 | 2026-12-18 | 62 |
| `pi_recovery_push_token` | 2026-08-30 | 2027-02-13 | 119 |
| `pi_sd_health_push_token` | 2026-08-30 | 2027-02-22 | 128 |
| `registry_gc_push_token` | 2026-05-10 | 2026-11-06 | 20 |
| `release_staleness_push_token` | 2026-04-05 | 2026-09-26 | -21 |
| `render_records_push_token` | 2026-08-17 | 2027-01-30 | 105 |
| `renovate_agent_kuma_push_token` | 2026-09-10 | 2027-03-04 | 138 |
| `rkhunter_daniel_box_push_token` | 2026-07-29 | 2027-01-13 | 88 |
| `rkhunter_daniel_pi_push_token` | 2026-07-12 | 2027-01-08 | 83 |
| `rkhunter_daniel_server_push_token` | 2026-10-12 | 2027-03-28 | 162 |
| `ruleset_drift_push_token` | 2026-09-01 | 2027-02-26 | 132 |
| `secret_rotation_push_token` | 2026-08-28 | 2027-02-24 | 130 |
| `setup_drift_push_token` | 2026-04-05 | 2026-09-24 | -23 |
| `ups_secondary_daniel_server_push_token` | 2026-06-13 | 2026-12-04 | 48 |
| `ups_secondary_push_token` | 2026-08-22 | 2027-02-10 | 116 |

## ignore

not rotated, and deliberately so.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_user` | None | never (no interval for this tier) | n/a |
| `crowdsec_username` | None | never (no interval for this tier) | n/a |
| `email` | 2026-01-12 | never (no interval for this tier) | n/a |
| `freshrss_username` | None | never (no interval for this tier) | n/a |
| `mqtt_username` | None | never (no interval for this tier) | n/a |
| `peanut_username` | None | never (no interval for this tier) | n/a |
| `qbittorrent_username` | None | never (no interval for this tier) | n/a |
| `r2_account_id` | None | never (no interval for this tier) | n/a |
| `r2_bucket` | None | never (no interval for this tier) | n/a |
| `uptime_kuma_username` | None | never (no interval for this tier) | n/a |
| `wifi_ssid` | None | never (no interval for this tier) | n/a |

## Rotating one

`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a secret means `sops ansible/vars/secrets.yml`, then `secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are the ones to read before touching anything in that tier.
