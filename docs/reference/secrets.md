---
generated_from: scripts/docs/gen_reference_secrets.py
generated_at: 2026-08-26 06:17 UTC
generated_sha: 945ff2bc
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/gen_reference_secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

143 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-06-27 | 305 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-25 | 638 |

## assisted

needs a human to mint the new value, then `secret_rotation.py rotate`.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `arr_discord_webhook_url` | 2026-04-27 | 2027-04-27 | 244 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-10-08 | 43 |
| `authelia_jwt` | 2026-01-02 | 2027-01-02 | 129 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-02-04 | 162 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-05-15 | 262 |
| `authelia_password` | 2026-07-23 | 2027-07-23 | 331 |
| `authelia_secret` | 2025-12-15 | 2026-12-15 | 111 |
| `become_password` | 2025-10-18 | 2026-10-18 | 53 |
| `calendar_1` | 2025-08-24 | 2026-08-24 | -2 |
| `calendar_2` | 2026-05-21 | 2027-05-21 | 268 |
| `calendar_3` | 2025-09-25 | 2026-09-25 | 30 |
| `calendar_4` | 2025-09-01 | 2026-09-01 | 6 |
| `claude_ha_token` | 2025-10-03 | 2026-10-03 | 38 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-04-03 | 220 |
| `code_server_password` | 2025-12-17 | 2026-12-17 | 113 |
| `code_server_sudo_password` | 2025-10-21 | 2026-10-21 | 56 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-23 | 270 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-12-14 | 110 |
| `crowdsec_password` | 2025-09-17 | 2026-09-17 | 22 |
| `freshrss_password` | 2026-06-01 | 2027-06-01 | 279 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-31 | 127 |
| `grafana_admin_password` | 2026-02-02 | 2027-02-02 | 160 |
| `handy_master_secret` | 2026-07-03 | 2027-07-03 | 311 |
| `healthchecks_password` | 2026-08-23 | 2027-08-23 | 362 |
| `healthchecks_ping_key` | 2026-04-20 | 2027-04-20 | 237 |
| `homelab_mcp_token` | 2025-09-01 | 2026-09-01 | 6 |
| `homepage_ha_token` | 2025-09-14 | 2026-09-14 | 19 |
| `jellyfin_api_key` | 2025-10-15 | 2026-10-15 | 50 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-03-03 | 189 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-02-17 | 175 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-24 | 210 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-09-17 | 22 |
| `kopia_b2_application_key` | 2025-09-07 | 2026-09-07 | 12 |
| `kopia_b2_bucket` | 2025-10-13 | 2026-10-13 | 48 |
| `kopia_b2_endpoint` | 2026-04-06 | 2027-04-06 | 223 |
| `kopia_b2_key_id` | 2026-01-25 | 2027-01-25 | 152 |
| `livesync_db_password` | 2025-11-16 | 2026-11-16 | 82 |
| `livesync_sync_token` | 2025-09-04 | 2026-09-04 | 9 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-11-06 | 72 |
| `mqtt_password` | 2026-06-08 | 2027-06-08 | 286 |
| `mqtt_password_hash` | 2026-04-23 | 2027-04-23 | 240 |
| `n8n_api_key` | 2025-10-10 | 2026-10-10 | 45 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-09 | 75 |
| `nut_ha_password` | 2026-03-23 | 2027-03-23 | 209 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-13 | 199 |
| `peanut_password` | 2026-03-16 | 2027-03-16 | 202 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-31 | 217 |
| `pihole_password` | 2026-07-23 | 2027-07-23 | 331 |
| `prometheus_ha_token` | 2025-11-06 | 2026-11-06 | 72 |
| `prometheus_kuma_api_key` | 2025-09-18 | 2026-09-18 | 23 |
| `prowlarr_api_key` | 2025-08-31 | 2026-08-31 | 5 |
| `qbittorrent_password` | 2025-09-20 | 2026-09-20 | 25 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-28 | 245 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-11 | 228 |
| `radarr_api_key` | 2026-01-06 | 2027-01-06 | 133 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-07-23 | 331 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-03-10 | 196 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-29 | 246 |
| `sonarr_api_key` | 2026-02-19 | 2027-02-19 | 177 |
| `speedtest_api_token` | 2026-06-08 | 2027-06-08 | 286 |
| `speedtest_app_key` | 2025-08-25 | 2026-08-25 | -1 |
| `terraria_password` | 2025-12-26 | 2026-12-26 | 122 |
| `uptime_kuma_password` | 2026-03-24 | 2027-03-24 | 210 |
| `valheim_server_pass` | 2026-05-14 | 2027-05-14 | 261 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2025-12-25 | 2026-12-25 | 121 |
| `coinmarket_api_key` | 2026-08-13 | 2027-08-13 | 352 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-17 | 203 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-04-05 | 222 |
| `email` | 2026-01-12 | 2027-01-12 | 139 |
| `gitops_deploy_discord_webhook` | 2025-12-30 | 2026-12-30 | 126 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-17 | 234 |
| `healthchecks_smtp_password` | 2026-08-23 | 2027-08-23 | 362 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-04-17 | 234 |
| `monitor_discord_webhook_url` | 2026-05-29 | 2027-05-29 | 276 |
| `mullvad_account` | 2025-11-16 | 2026-11-16 | 82 |
| `weather_api_key` | 2025-12-22 | 2026-12-22 | 118 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-09 | 167 |

## auto

rotated unattended by the weekly secret-rotate cron.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `arr_autoblock_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `claude_otel_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `cloudflare_ddns_direct_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `cloudflare_ddns_proxied_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `daniel_box_disk_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `etcd_snapshot_push_token` | 2026-07-04 | 2026-12-31 | 127 |
| `live_drift_push_token` | 2026-08-15 | 2027-02-11 | 169 |
| `longhorn_backup_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `manifest_prune_push_token` | 2026-06-15 | 2026-12-12 | 108 |
| `monitor_bridge_appsec_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_arr_queue_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_b2_reachable_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_b2_storage_push_token` | 2026-07-21 | 2027-01-17 | 144 |
| `monitor_bridge_cert_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_cloudflare_drift_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_cluster_prometheus_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_cluster_targets_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_configarr_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_cpu_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_discord_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_disk_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_fake_remux_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_fake_remux_replace_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_gitops_alive_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_gitops_status_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_ha_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_home_allowlist_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_janitorr_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_k8s_workloads_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_loki_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_loki_reachable_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_longhorn_volumes_push_token` | 2026-03-15 | 2026-09-11 | 16 |
| `monitor_bridge_mem_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_n8n_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_oom_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_pi_peers_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_pi_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_prometheus_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_promtail_dropped_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_prowlarr_indexers_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_r2_usage_push_token` | 2026-06-14 | 2026-12-11 | 107 |
| `monitor_bridge_renovate_alive_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_restarts_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_scrutiny_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_speedtest_push_token` | 2026-04-20 | 2026-10-17 | 52 |
| `monitor_bridge_targets_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_traefik_latency_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_traefik_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `monitor_bridge_ups_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `pi_recovery_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `pi_sd_health_push_token` | 2026-08-10 | 2027-02-06 | 164 |
| `remember_logs_push_token` | 2026-04-16 | 2026-10-13 | 48 |
| `secret_rotation_push_token` | 2026-08-10 | 2027-02-06 | 164 |

## ignore

not rotated, and deliberately so.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_user` | None | never (no interval for this tier) | n/a |
| `crowdsec_username` | None | never (no interval for this tier) | n/a |
| `domain` | None | never (no interval for this tier) | n/a |
| `freshrss_username` | None | never (no interval for this tier) | n/a |
| `healthchecks_smtp_user` | None | never (no interval for this tier) | n/a |
| `mqtt_username` | None | never (no interval for this tier) | n/a |
| `peanut_username` | None | never (no interval for this tier) | n/a |
| `qbittorrent_username` | None | never (no interval for this tier) | n/a |
| `r2_account_id` | None | never (no interval for this tier) | n/a |
| `r2_bucket` | None | never (no interval for this tier) | n/a |
| `uptime_kuma_username` | None | never (no interval for this tier) | n/a |

## Rotating one

`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a secret means `sops ansible/vars/secrets.yml`, then `secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are the ones to read before touching anything in that tier.
