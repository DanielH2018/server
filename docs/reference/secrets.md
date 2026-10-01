---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-10-01 06:17 UTC
generated_sha: 89bf63ff9
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

180 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 216 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 596 |

## assisted

needs a human to mint the new value, then `secret_rotation.py rotate`.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 198 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 333 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 79 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 117 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | -16 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 66 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 122 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 210 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 277 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 245 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 68 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 327 |
| `become_password` | 2026-08-30 | 2027-08-29 | 332 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 325 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 222 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -35 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -32 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -22 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 168 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 332 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 317 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 225 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | 49 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 214 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 68 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 122 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 310 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 233 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 246 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 219 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 213 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | 40 |
| `healthchecks_password` | 2026-08-23 | 2027-07-27 | 299 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 323 |
| `healthchecks_secret_key` | 2026-07-07 | 2027-06-10 | 252 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -52 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -34 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 318 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 125 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 116 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 159 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -41 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | 32 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -52 |
| `longhorn_b2_application_key` | 2026-05-29 | 2027-05-13 | 224 |
| `longhorn_b2_bucket` | 2026-05-29 | 2027-05-14 | 225 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 215 |
| `longhorn_b2_key_id` | 2026-05-29 | 2027-05-04 | 215 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | 18 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 231 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 179 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | -13 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | 33 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 153 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 160 |
| `peanut_password` | 2026-03-16 | 2027-03-03 | 153 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 179 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 275 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | 25 |
| `prometheus_kuma_api_key` | 2025-09-18 | 2026-08-21 | -41 |
| `prowlarr_api_key` | 2026-08-30 | 2027-08-10 | 313 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -34 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 199 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 191 |
| `radarr_api_key` | 2026-08-29 | 2027-08-05 | 308 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 271 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 131 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 205 |
| `sonarr_api_key` | 2026-08-29 | 2027-08-28 | 331 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 327 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -64 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 69 |
| `uptime_kuma_password` | 2026-03-24 | 2027-03-11 | 161 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 304 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 332 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 332 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 164 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 178 |
| `gitops_deploy_discord_webhook` | 2026-08-30 | 2027-08-03 | 306 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 186 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 171 |
| `monitor_discord_webhook_url` | 2026-09-19 | 2027-08-27 | 330 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | 17 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 307 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 128 |

## auto

rotated unattended by the weekly secret-rotate cron.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `arr_autoblock_push_token` | 2026-08-30 | 2027-02-26 | 148 |
| `artifacts_sync_push_token` | 2026-05-24 | 2026-11-12 | 42 |
| `claude_otel_push_token` | 2026-08-28 | 2027-02-14 | 136 |
| `cloudflare_ddns_direct_push_token` | 2026-08-30 | 2027-02-26 | 148 |
| `cloudflare_ddns_proxied_push_token` | 2026-08-30 | 2027-02-16 | 138 |
| `crowdsec_remote_allowlist_push_token` | 2026-06-05 | 2026-11-18 | 48 |
| `daniel_box_disk_push_token` | 2026-08-28 | 2027-02-16 | 138 |
| `docs_refresh_push_token` | 2026-08-06 | 2027-01-19 | 110 |
| `etcd_drill_fence_push_token` | 2026-07-26 | 2027-01-19 | 110 |
| `etcd_drill_full_push_token` | 2026-07-21 | 2027-01-13 | 104 |
| `etcd_snapshot_push_token` | 2026-08-28 | 2027-02-22 | 144 |
| `interaction_limit_push_token` | 2026-05-15 | 2026-10-30 | 29 |
| `kuma_status_page_sync_push_token` | 2026-07-08 | 2026-12-28 | 88 |
| `live_drift_push_token` | 2026-08-15 | 2027-02-03 | 125 |
| `loki_route_witness_daniel_server_push_token` | 2026-05-14 | 2026-11-08 | 38 |
| `loki_route_witness_push_token` | 2026-05-03 | 2026-10-30 | 29 |
| `longhorn_backup_push_token` | 2026-08-28 | 2027-02-14 | 136 |
| `manifest_prune_push_token` | 2026-08-28 | 2027-02-13 | 135 |
| `mkv_attachment_repair_push_token` | 2026-04-02 | 2026-09-26 | -5 |
| `monitor_bridge_appsec_push_token` | 2026-08-28 | 2027-02-16 | 138 |
| `monitor_bridge_arr_queue_push_token` | 2026-08-30 | 2027-02-19 | 141 |
| `monitor_bridge_b2_reachable_push_token` | 2026-08-30 | 2027-02-17 | 139 |
| `monitor_bridge_b2_storage_push_token` | 2026-08-30 | 2027-02-25 | 147 |
| `monitor_bridge_bazarr_push_token` | 2026-08-16 | 2027-02-02 | 124 |
| `monitor_bridge_cert_push_token` | 2026-08-30 | 2027-02-19 | 141 |
| `monitor_bridge_cloudflare_drift_push_token` | 2026-08-28 | 2027-02-14 | 136 |
| `monitor_bridge_cluster_targets_push_token` | 2026-08-30 | 2027-02-20 | 142 |
| `monitor_bridge_configarr_push_token` | 2026-08-28 | 2027-02-17 | 139 |
| `monitor_bridge_cpu_push_token` | 2026-08-30 | 2027-02-20 | 142 |
| `monitor_bridge_discord_push_token` | 2026-08-30 | 2027-02-26 | 148 |
| `monitor_bridge_disk_push_token` | 2026-08-30 | 2027-02-15 | 137 |
| `monitor_bridge_etcd_db_size_push_token` | 2026-05-23 | 2026-11-09 | 39 |
| `monitor_bridge_etcd_drill_push_token` | 2026-04-28 | 2026-10-18 | 17 |
| `monitor_bridge_fake_remux_push_token` | 2026-08-28 | 2027-02-19 | 141 |
| `monitor_bridge_fake_remux_replace_push_token` | 2026-08-28 | 2027-02-17 | 139 |
| `monitor_bridge_gitops_alive_push_token` | 2026-08-30 | 2027-02-23 | 145 |
| `monitor_bridge_gitops_status_push_token` | 2026-08-30 | 2027-02-25 | 147 |
| `monitor_bridge_ha_push_token` | 2026-08-30 | 2027-02-14 | 136 |
| `monitor_bridge_healthchecks_drift_push_token` | 2026-07-31 | 2027-01-16 | 107 |
| `monitor_bridge_home_allowlist_push_token` | 2026-08-28 | 2027-02-10 | 132 |
| `monitor_bridge_host_temp_push_token` | 2026-08-09 | 2027-02-01 | 123 |
| `monitor_bridge_janitorr_push_token` | 2026-08-30 | 2027-02-15 | 137 |
| `monitor_bridge_k8s_workloads_push_token` | 2026-08-30 | 2027-02-12 | 134 |
| `monitor_bridge_kubelet_readonly_push_token` | 2026-08-03 | 2027-01-18 | 109 |
| `monitor_bridge_kuma_notify_failures_push_token` | 2026-06-08 | 2026-11-27 | 57 |
| `monitor_bridge_loki_push_token` | 2026-08-30 | 2027-02-14 | 136 |
| `monitor_bridge_loki_reachable_push_token` | 2026-08-30 | 2027-02-17 | 139 |
| `monitor_bridge_longhorn_volumes_push_token` | 2026-09-13 | 2027-02-28 | 150 |
| `monitor_bridge_mem_push_token` | 2026-08-30 | 2027-02-23 | 145 |
| `monitor_bridge_n8n_push_token` | 2026-08-30 | 2027-02-23 | 145 |
| `monitor_bridge_oom_push_token` | 2026-08-30 | 2027-02-18 | 140 |
| `monitor_bridge_pi_peers_push_token` | 2026-08-30 | 2027-02-24 | 146 |
| `monitor_bridge_pi_push_token` | 2026-08-30 | 2027-02-25 | 147 |
| `monitor_bridge_prometheus_push_token` | 2026-08-30 | 2027-02-19 | 141 |
| `monitor_bridge_promtail_dropped_push_token` | 2026-08-30 | 2027-02-26 | 148 |
| `monitor_bridge_prowlarr_indexers_push_token` | 2026-08-30 | 2027-02-18 | 140 |
| `monitor_bridge_pvc_push_token` | 2026-05-28 | 2026-11-16 | 46 |
| `monitor_bridge_r2_usage_push_token` | 2026-08-30 | 2027-02-13 | 135 |
| `monitor_bridge_renovate_alive_push_token` | 2026-08-28 | 2027-02-13 | 135 |
| `monitor_bridge_restarts_push_token` | 2026-08-30 | 2027-02-21 | 143 |
| `monitor_bridge_scrutiny_push_token` | 2026-08-30 | 2027-02-15 | 137 |
| `monitor_bridge_snapshot_headroom_push_token` | 2026-05-31 | 2026-11-14 | 44 |
| `monitor_bridge_speedtest_push_token` | 2026-04-20 | 2026-10-17 | 16 |
| `monitor_bridge_swallowed_verdicts_push_token` | 2026-06-20 | 2026-12-04 | 64 |
| `monitor_bridge_targets_push_token` | 2026-08-30 | 2027-02-14 | 136 |
| `monitor_bridge_traefik_404_push_token` | 2026-06-05 | 2026-12-02 | 62 |
| `monitor_bridge_traefik_421_push_token` | 2026-06-20 | 2026-12-16 | 76 |
| `monitor_bridge_traefik_latency_push_token` | 2026-08-30 | 2027-02-24 | 146 |
| `monitor_bridge_traefik_push_token` | 2026-08-30 | 2027-02-18 | 140 |
| `monitor_bridge_ups_push_token` | 2026-08-30 | 2027-02-18 | 140 |
| `monitor_bridge_wan_reachable_push_token` | 2026-06-25 | 2026-12-18 | 78 |
| `pi_recovery_push_token` | 2026-08-30 | 2027-02-13 | 135 |
| `pi_sd_health_push_token` | 2026-08-30 | 2027-02-22 | 144 |
| `registry_gc_push_token` | 2026-05-10 | 2026-11-06 | 36 |
| `release_staleness_push_token` | 2026-04-05 | 2026-09-26 | -5 |
| `render_records_push_token` | 2026-08-17 | 2027-01-30 | 121 |
| `renovate_agent_kuma_push_token` | 2026-09-10 | 2027-03-04 | 154 |
| `ruleset_drift_push_token` | 2026-09-01 | 2027-02-26 | 148 |
| `secret_rotation_push_token` | 2026-08-28 | 2027-02-24 | 146 |
| `setup_drift_push_token` | 2026-04-05 | 2026-09-24 | -7 |
| `ups_secondary_daniel_server_push_token` | 2026-06-13 | 2026-12-04 | 64 |
| `ups_secondary_push_token` | 2026-08-22 | 2027-02-10 | 132 |

## ignore

not rotated, and deliberately so.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_user` | None | never (no interval for this tier) | n/a |
| `crowdsec_username` | None | never (no interval for this tier) | n/a |
| `domain` | None | never (no interval for this tier) | n/a |
| `email` | 2026-01-12 | never (no interval for this tier) | n/a |
| `freshrss_username` | None | never (no interval for this tier) | n/a |
| `mqtt_username` | None | never (no interval for this tier) | n/a |
| `peanut_username` | None | never (no interval for this tier) | n/a |
| `qbittorrent_username` | None | never (no interval for this tier) | n/a |
| `r2_account_id` | None | never (no interval for this tier) | n/a |
| `r2_bucket` | None | never (no interval for this tier) | n/a |
| `uptime_kuma_username` | None | never (no interval for this tier) | n/a |

## Rotating one

`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a secret means `sops ansible/vars/secrets.yml`, then `secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are the ones to read before touching anything in that tier.
