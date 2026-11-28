---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-11-28 18:17 UTC
generated_sha: 23c576cd4
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

127 secret(s) in the rotation registry (`secrets/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 158 |
| `livesync_e2ee_passphrase` | 2026-04-26 | 2028-04-18 | 507 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 538 |

## assisted

needs a human to mint the new value in the app, then `sops set` it.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `airflow_api_jwt_secret` | 2026-09-20 | 2027-08-27 | 272 |
| `airflow_github_token` | 2026-03-07 | 2027-02-15 | 79 |
| `airflow_ha_token` | 2026-08-20 | 2027-07-27 | 241 |
| `airflow_postgres_password` | 2026-09-17 | 2027-09-07 | 283 |
| `airflow_warehouse_reader_password` | 2026-06-13 | 2027-05-24 | 177 |
| `airflow_warehouse_writer_password` | 2026-05-09 | 2027-04-21 | 144 |
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 140 |
| `anthropic_api_key` | 2026-11-15 | 2027-10-20 | 326 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 275 |
| `authelia_agent_password` | 2026-08-27 | 2027-08-18 | 263 |
| `authelia_agent_password_argon2` | 2026-07-16 | 2027-06-18 | 202 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 21 |
| `authelia_claude_password_argon2` | 2025-12-25 | 2026-12-20 | 22 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 59 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | -74 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 8 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 64 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 152 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 219 |
| `authelia_password_argon2` | 2026-02-12 | 2027-01-18 | 51 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 187 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 10 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 269 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 267 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 164 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -93 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -90 |
| `claude_code_agent_gh_token` | 2026-11-13 | 2027-10-15 | 321 |
| `claude_code_triage_claude_oauth_token` | 2026-04-10 | 2027-03-23 | 115 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -80 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 110 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 274 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 259 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 167 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | -9 |
| `flux_github_app_id` | 2026-10-28 | 2027-10-24 | 330 |
| `flux_github_app_installation_id` | 2026-04-06 | 2027-03-08 | 100 |
| `flux_github_app_private_key` | 2026-05-21 | 2027-05-18 | 171 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 156 |
| `games_panel_token` | 2026-11-14 | 2027-11-01 | 338 |
| `google_assistant_project_id` | 2026-06-25 | 2027-05-31 | 184 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 10 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 64 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 252 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 175 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 188 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 161 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 155 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | -18 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 265 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -110 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -92 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 260 |
| `k3s_readonly_token` | 2026-01-20 | 2027-01-14 | 47 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 67 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 58 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 101 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -99 |
| `lidarr_api_key` | 2026-07-07 | 2027-07-07 | 221 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | -26 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -110 |
| `longhorn_b2_application_key` | 2026-11-22 | 2027-11-06 | 343 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 157 |
| `longhorn_b2_key_id` | 2026-11-22 | 2027-10-28 | 334 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | -40 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 173 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 121 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | -71 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | -25 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 95 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 102 |
| `peanut_password` | 2026-10-16 | 2027-10-03 | 309 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 121 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 217 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | -33 |
| `prowlarr_api_key` | 2026-10-30 | 2027-10-10 | 316 |
| `qbittorrent_api_key` | 2026-09-26 | 2027-09-09 | 285 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -92 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 141 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 133 |
| `radarr_api_key` | 2026-11-13 | 2027-10-20 | 326 |
| `renovate_agent_claude_oauth_token` | 2026-06-01 | 2027-05-21 | 174 |
| `renovate_agent_gh_read_token` | 2026-04-03 | 2027-04-02 | 125 |
| `renovate_agent_gh_token` | 2026-11-05 | 2027-10-28 | 334 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 213 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 73 |
| `slskd_wireguard_private_key` | 2026-04-12 | 2027-03-22 | 114 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 147 |
| `sonarr_api_key` | 2026-11-13 | 2027-11-12 | 349 |
| `soulseek_password` | 2026-04-20 | 2027-03-22 | 114 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 269 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -122 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 11 |
| `uptimerobot_api_key` | 2026-03-30 | 2027-03-27 | 119 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 246 |
| `vault_mcp_oidc_client_secret` | 2026-10-27 | 2027-10-27 | 333 |
| `vault_mcp_oidc_client_secret_hash` | 2026-08-04 | 2027-07-21 | 235 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 274 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 274 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 106 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 120 |
| `discord_alert_webhook` | 2026-10-04 | 2027-09-29 | 305 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 128 |
| `homelab_builder_github_token` | 2026-05-31 | 2027-05-21 | 174 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 113 |
| `monitor_bridge_deploy_pipeline_github_token` | 2026-10-11 | 2027-09-22 | 298 |
| `monitor_discord_webhook_url` | 2026-09-19 | 2027-08-27 | 272 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | -41 |
| `riot_api_key` | 2026-11-27 | 2027-11-10 | 347 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 249 |
| `wifi_password` | 2026-07-20 | 2027-06-24 | 208 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 70 |

## external-90d

lives in a third-party system and expires after 90 days; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `promote_token` | 2026-10-12 | 2027-01-04 | 37 |

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
| `soulseek_username` | None | never (no interval for this tier) | n/a |
| `wifi_ssid` | None | never (no interval for this tier) | n/a |

## Rotating one

`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a secret means `sops secrets/secrets.yml`, then `secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are the ones to read before touching anything in that tier.
