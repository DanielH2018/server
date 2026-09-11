---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-09-11 06:17 UTC
generated_sha: 37772d46c
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

172 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 236 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 616 |

## assisted

needs a human to mint the new value, then `secret_rotation.py rotate`.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 218 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 353 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 99 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 137 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | 4 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 86 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 142 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 230 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 297 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 265 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 88 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 347 |
| `become_password` | 2026-08-30 | 2027-08-29 | 352 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 345 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 242 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -15 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -12 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -2 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 188 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 352 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 337 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 245 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | 69 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 234 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 88 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 142 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 330 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 253 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 266 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 239 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 233 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | 60 |
| `healthchecks_password` | 2026-08-23 | 2027-07-27 | 319 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 343 |
| `healthchecks_secret_key` | 2026-07-07 | 2027-06-10 | 272 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -32 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -14 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 338 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 145 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 136 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 179 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -21 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | 52 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -32 |
| `longhorn_b2_application_key` | 2026-05-29 | 2027-05-13 | 244 |
| `longhorn_b2_bucket` | 2026-05-29 | 2027-05-14 | 245 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 235 |
| `longhorn_b2_key_id` | 2026-05-29 | 2027-05-04 | 235 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | 38 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 251 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 199 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | 7 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | 53 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 173 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 180 |
| `peanut_password` | 2026-03-16 | 2027-03-03 | 173 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 199 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 295 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | 45 |
| `prometheus_kuma_api_key` | 2025-09-18 | 2026-08-21 | -21 |
| `prowlarr_api_key` | 2026-08-30 | 2027-08-10 | 333 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -14 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 219 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 211 |
| `radarr_api_key` | 2026-08-29 | 2027-08-05 | 328 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 291 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 151 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 225 |
| `sonarr_api_key` | 2026-08-29 | 2027-08-28 | 351 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 347 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -44 |
| `staging_gate_ssh_key` | 2026-08-29 | 2027-08-27 | 350 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 89 |
| `uptime_kuma_password` | 2026-03-24 | 2027-03-11 | 181 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 324 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 352 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 352 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 184 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 198 |
| `gitops_deploy_discord_webhook` | 2026-08-30 | 2027-08-03 | 326 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 206 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 191 |
| `monitor_discord_webhook_url` | 2026-05-29 | 2027-05-06 | 237 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | 37 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 327 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 148 |

## auto

rotated unattended by the weekly secret-rotate cron.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `arr_autoblock_push_token` | 2026-08-30 | 2027-02-26 | 168 |
| `claude_otel_push_token` | 2026-08-28 | 2027-02-14 | 156 |
| `cloudflare_ddns_direct_push_token` | 2026-08-30 | 2027-02-26 | 168 |
| `cloudflare_ddns_proxied_push_token` | 2026-08-30 | 2027-02-16 | 158 |
| `daniel_box_disk_push_token` | 2026-08-28 | 2027-02-16 | 158 |
| `docs_refresh_push_token` | 2026-08-06 | 2027-01-19 | 130 |
| `etcd_snapshot_push_token` | 2026-08-28 | 2027-02-22 | 164 |
| `interaction_limit_push_token` | 2026-05-15 | 2026-10-30 | 49 |
| `kuma_status_page_sync_push_token` | 2026-07-08 | 2026-12-28 | 108 |
| `live_drift_push_token` | 2026-08-15 | 2027-02-03 | 145 |
| `loki_route_witness_daniel_server_push_token` | 2026-05-14 | 2026-11-08 | 58 |
| `loki_route_witness_push_token` | 2026-05-03 | 2026-10-30 | 49 |
| `longhorn_backup_push_token` | 2026-08-28 | 2027-02-14 | 156 |
| `manifest_prune_push_token` | 2026-08-28 | 2027-02-13 | 155 |
| `mkv_attachment_repair_push_token` | 2026-04-02 | 2026-09-26 | 15 |
| `monitor_bridge_appsec_push_token` | 2026-08-28 | 2027-02-16 | 158 |
| `monitor_bridge_arr_queue_push_token` | 2026-08-30 | 2027-02-19 | 161 |
| `monitor_bridge_b2_reachable_push_token` | 2026-08-30 | 2027-02-17 | 159 |
| `monitor_bridge_b2_storage_push_token` | 2026-08-30 | 2027-02-25 | 167 |
| `monitor_bridge_bazarr_push_token` | 2026-08-16 | 2027-02-02 | 144 |
| `monitor_bridge_cert_push_token` | 2026-08-30 | 2027-02-19 | 161 |
| `monitor_bridge_cloudflare_drift_push_token` | 2026-08-28 | 2027-02-14 | 156 |
| `monitor_bridge_cluster_prometheus_push_token` | 2026-08-30 | 2027-02-20 | 162 |
| `monitor_bridge_cluster_targets_push_token` | 2026-08-30 | 2027-02-20 | 162 |
| `monitor_bridge_configarr_push_token` | 2026-08-28 | 2027-02-17 | 159 |
| `monitor_bridge_cpu_push_token` | 2026-08-30 | 2027-02-20 | 162 |
| `monitor_bridge_discord_push_token` | 2026-08-30 | 2027-02-26 | 168 |
| `monitor_bridge_disk_push_token` | 2026-08-30 | 2027-02-15 | 157 |
| `monitor_bridge_etcd_drill_push_token` | 2026-04-28 | 2026-10-18 | 37 |
| `monitor_bridge_fake_remux_push_token` | 2026-08-28 | 2027-02-19 | 161 |
| `monitor_bridge_fake_remux_replace_push_token` | 2026-08-28 | 2027-02-17 | 159 |
| `monitor_bridge_gitops_alive_push_token` | 2026-08-30 | 2027-02-23 | 165 |
| `monitor_bridge_gitops_status_push_token` | 2026-08-30 | 2027-02-25 | 167 |
| `monitor_bridge_ha_push_token` | 2026-08-30 | 2027-02-14 | 156 |
| `monitor_bridge_home_allowlist_push_token` | 2026-08-28 | 2027-02-10 | 152 |
| `monitor_bridge_host_temp_push_token` | 2026-08-09 | 2027-02-01 | 143 |
| `monitor_bridge_janitorr_push_token` | 2026-08-30 | 2027-02-15 | 157 |
| `monitor_bridge_k8s_workloads_push_token` | 2026-08-30 | 2027-02-12 | 154 |
| `monitor_bridge_kubelet_readonly_push_token` | 2026-08-03 | 2027-01-18 | 129 |
| `monitor_bridge_loki_push_token` | 2026-08-30 | 2027-02-14 | 156 |
| `monitor_bridge_loki_reachable_push_token` | 2026-08-30 | 2027-02-17 | 159 |
| `monitor_bridge_longhorn_volumes_push_token` | 2026-03-15 | 2026-08-30 | -12 |
| `monitor_bridge_mem_push_token` | 2026-08-30 | 2027-02-23 | 165 |
| `monitor_bridge_n8n_push_token` | 2026-08-30 | 2027-02-23 | 165 |
| `monitor_bridge_oom_push_token` | 2026-08-30 | 2027-02-18 | 160 |
| `monitor_bridge_pi_peers_push_token` | 2026-08-30 | 2027-02-24 | 166 |
| `monitor_bridge_pi_push_token` | 2026-08-30 | 2027-02-25 | 167 |
| `monitor_bridge_prometheus_push_token` | 2026-08-30 | 2027-02-19 | 161 |
| `monitor_bridge_promtail_dropped_push_token` | 2026-08-30 | 2027-02-26 | 168 |
| `monitor_bridge_prowlarr_indexers_push_token` | 2026-08-30 | 2027-02-18 | 160 |
| `monitor_bridge_pvc_push_token` | 2026-05-28 | 2026-11-16 | 66 |
| `monitor_bridge_r2_usage_push_token` | 2026-08-30 | 2027-02-13 | 155 |
| `monitor_bridge_renovate_alive_push_token` | 2026-08-28 | 2027-02-13 | 155 |
| `monitor_bridge_restarts_push_token` | 2026-08-30 | 2027-02-21 | 163 |
| `monitor_bridge_scrutiny_push_token` | 2026-08-30 | 2027-02-15 | 157 |
| `monitor_bridge_snapshot_headroom_push_token` | 2026-05-31 | 2026-11-14 | 64 |
| `monitor_bridge_speedtest_push_token` | 2026-04-20 | 2026-10-17 | 36 |
| `monitor_bridge_staging_backfill_push_token` | 2026-04-11 | 2026-09-24 | 13 |
| `monitor_bridge_targets_push_token` | 2026-08-30 | 2027-02-14 | 156 |
| `monitor_bridge_traefik_404_push_token` | 2026-06-05 | 2026-12-02 | 82 |
| `monitor_bridge_traefik_latency_push_token` | 2026-08-30 | 2027-02-24 | 166 |
| `monitor_bridge_traefik_push_token` | 2026-08-30 | 2027-02-18 | 160 |
| `monitor_bridge_ups_push_token` | 2026-08-30 | 2027-02-18 | 160 |
| `pi_recovery_push_token` | 2026-08-30 | 2027-02-13 | 155 |
| `pi_sd_health_push_token` | 2026-08-30 | 2027-02-22 | 164 |
| `release_staleness_push_token` | 2026-04-05 | 2026-09-26 | 15 |
| `remember_logs_push_token` | 2026-04-16 | 2026-10-02 | 21 |
| `renovate_agent_kuma_push_token` | 2026-09-10 | 2027-03-04 | 174 |
| `ruleset_drift_push_token` | 2026-09-01 | 2027-02-26 | 168 |
| `secret_rotation_push_token` | 2026-08-28 | 2027-02-24 | 166 |
| `setup_drift_push_token` | 2026-04-05 | 2026-09-24 | 13 |
| `ups_secondary_daniel_server_push_token` | 2026-06-13 | 2026-12-04 | 84 |
| `ups_secondary_push_token` | 2026-08-22 | 2027-02-10 | 152 |

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
