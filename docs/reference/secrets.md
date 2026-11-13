---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-11-13 18:17 UTC
generated_sha: c9de2bd37
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

116 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 173 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 553 |

## assisted

needs a human to mint the new value in the app, then `sops set` it.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 155 |
| `anthropic_api_key` | 2026-09-23 | 2027-08-28 | 288 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 290 |
| `authelia_agent_password` | 2026-08-27 | 2027-08-18 | 278 |
| `authelia_agent_password_argon2` | 2026-07-16 | 2027-06-18 | 217 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 36 |
| `authelia_claude_password_argon2` | 2025-12-25 | 2026-12-20 | 37 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 74 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | -59 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 23 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 79 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 167 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 234 |
| `authelia_password_argon2` | 2026-02-12 | 2027-01-18 | 66 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 202 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 25 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 284 |
| `become_password` | 2026-08-30 | 2027-08-29 | 289 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 282 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 179 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -78 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -75 |
| `claude_code_agent_gh_token` | 2026-11-13 | 2027-10-15 | 336 |
| `claude_code_triage_claude_oauth_token` | 2026-04-10 | 2027-03-23 | 130 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -65 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 125 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 289 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 274 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 182 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | 6 |
| `flux_github_app_id` | 2026-10-28 | 2027-10-24 | 345 |
| `flux_github_app_installation_id` | 2026-04-06 | 2027-03-08 | 115 |
| `flux_github_app_private_key` | 2026-05-21 | 2027-05-18 | 186 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 171 |
| `google_assistant_project_id` | 2026-06-25 | 2027-05-31 | 199 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 25 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 79 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 267 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 190 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 203 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 176 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 170 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | -3 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 280 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -95 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -77 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 275 |
| `k3s_readonly_token` | 2026-01-20 | 2027-01-14 | 62 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 82 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 73 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 116 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -84 |
| `lidarr_api_key` | 2026-07-07 | 2027-07-07 | 236 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | -11 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -95 |
| `longhorn_b2_application_key` | 2026-05-29 | 2027-05-13 | 181 |
| `longhorn_b2_bucket` | 2026-05-29 | 2027-05-14 | 182 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 172 |
| `longhorn_b2_key_id` | 2026-05-29 | 2027-05-04 | 172 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | -25 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 188 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 136 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | -56 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | -10 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 110 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 117 |
| `peanut_password` | 2026-10-16 | 2027-10-03 | 324 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 136 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 232 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | -18 |
| `prowlarr_api_key` | 2026-10-30 | 2027-10-10 | 331 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -77 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 156 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 148 |
| `radarr_api_key` | 2026-11-13 | 2027-10-20 | 341 |
| `renovate_agent_claude_oauth_token` | 2026-06-01 | 2027-05-21 | 189 |
| `renovate_agent_gh_token` | 2026-11-05 | 2027-10-28 | 349 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 228 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 88 |
| `slskd_wireguard_private_key` | 2026-04-12 | 2027-03-22 | 129 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 162 |
| `sonarr_api_key` | 2026-11-13 | 2027-11-12 | 364 |
| `soulseek_password` | 2026-04-20 | 2027-03-22 | 129 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 284 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -107 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 26 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 261 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 289 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 289 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 121 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 135 |
| `discord_alert_webhook` | 2026-10-04 | 2027-09-29 | 320 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 143 |
| `homelab_builder_github_token` | 2026-05-31 | 2027-05-21 | 189 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 128 |
| `monitor_bridge_deploy_pipeline_github_token` | 2026-10-11 | 2027-09-22 | 313 |
| `monitor_discord_webhook_url` | 2026-09-19 | 2027-08-27 | 287 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | -26 |
| `tested_tree_app_private_key` | 2026-11-13 | 2027-11-04 | 356 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 264 |
| `wifi_password` | 2026-07-20 | 2027-06-24 | 223 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 85 |

## external-90d

lives in a third-party system and expires after 90 days; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `promote_token` | 2026-10-12 | 2027-01-04 | 52 |

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

`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a secret means `sops ansible/vars/secrets.yml`, then `secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are the ones to read before touching anything in that tier.
