---
generated_from: scripts/docs/reference/secrets.py
generated_at: 2026-11-03 06:17 UTC
generated_sha: 4f1e13f113
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/secrets.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Secrets

120 secret(s) in the rotation registry (`ansible/secret_rotation.yml`).

!!! note "Names and dates only"
    This page is generated from the plaintext rotation registry. No secret VALUE is read here, and the generator never opens the encrypted store or invokes the decryption tool — a test enforces that.


## pinned

DANGER — rotating it breaks decryption or locks out access. Follow the procedure in the runbook, never the generic rotate path.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `authelia_storage` | 2025-06-27 | 2027-05-05 | 183 |
| `zigbee_network_key` | 2026-05-26 | 2028-05-19 | 563 |

## assisted

needs a human to mint the new value, then `secret_rotation.py rotate`.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `alloy_pi_http_password` | 2026-04-25 | 2027-04-17 | 165 |
| `anthropic_api_key` | 2026-09-23 | 2027-08-28 | 298 |
| `arr_discord_webhook_url` | 2026-09-07 | 2027-08-30 | 300 |
| `authelia_agent_password` | 2026-08-27 | 2027-08-18 | 288 |
| `authelia_agent_password_argon2` | 2026-07-16 | 2027-06-18 | 227 |
| `authelia_claude_password` | 2026-01-05 | 2026-12-19 | 46 |
| `authelia_claude_password_argon2` | 2025-12-25 | 2026-12-20 | 47 |
| `authelia_claude_totp_secret` | 2026-02-03 | 2027-01-26 | 84 |
| `authelia_client_password_hash` | 2025-10-08 | 2026-09-15 | -49 |
| `authelia_jwt` | 2026-01-02 | 2026-12-06 | 33 |
| `authelia_oidc_hmac_secret` | 2026-02-04 | 2027-01-31 | 89 |
| `authelia_oidc_rsa_key_content` | 2026-05-15 | 2027-04-29 | 177 |
| `authelia_password` | 2026-07-23 | 2027-07-05 | 244 |
| `authelia_password_argon2` | 2026-02-12 | 2027-01-18 | 76 |
| `authelia_redis_password` | 2026-06-13 | 2027-06-03 | 212 |
| `authelia_secret` | 2025-12-15 | 2026-12-08 | 35 |
| `bazarr_api_key` | 2026-08-30 | 2027-08-24 | 294 |
| `become_password` | 2026-08-30 | 2027-08-29 | 299 |
| `calendar_1` | 2026-08-30 | 2027-08-22 | 292 |
| `calendar_2` | 2026-05-21 | 2027-05-11 | 189 |
| `calendar_3` | 2025-09-25 | 2026-08-27 | -68 |
| `calendar_4` | 2025-09-01 | 2026-08-30 | -65 |
| `claude_code_agent_gh_token` | 2026-02-12 | 2027-01-14 | 72 |
| `claude_code_triage_claude_oauth_token` | 2026-04-10 | 2027-03-23 | 140 |
| `claude_ha_token` | 2025-10-03 | 2026-09-09 | -55 |
| `cloudflare_analytics_token` | 2026-04-03 | 2027-03-18 | 135 |
| `code_server_password` | 2026-08-30 | 2027-08-29 | 299 |
| `code_server_sudo_password` | 2026-08-30 | 2027-08-14 | 284 |
| `crowdsec_k8s_agent_password` | 2026-05-23 | 2027-05-14 | 192 |
| `crowdsec_k8s_bouncer_api_key` | 2025-12-14 | 2026-11-19 | 16 |
| `freshrss_password` | 2026-06-01 | 2027-05-03 | 181 |
| `google_assistant_project_id` | 2026-06-25 | 2027-05-31 | 209 |
| `google_assistant_service_account` | 2025-12-31 | 2026-12-08 | 35 |
| `grafana_admin_password` | 2026-02-02 | 2027-01-31 | 89 |
| `grafana_oidc_client_secret` | 2026-08-18 | 2027-08-07 | 277 |
| `grafana_oidc_client_secret_hash` | 2026-06-08 | 2027-05-22 | 200 |
| `handy_master_secret` | 2026-07-03 | 2027-06-04 | 213 |
| `headlamp_oidc_client_secret` | 2026-05-17 | 2027-05-08 | 186 |
| `headlamp_oidc_client_secret_hash` | 2026-05-29 | 2027-05-02 | 180 |
| `healthchecks_api_read_only_key` | 2025-11-12 | 2026-11-10 | 7 |
| `healthchecks_ping_key` | 2026-08-31 | 2027-08-20 | 290 |
| `homelab_mcp_token` | 2025-09-01 | 2026-08-10 | -85 |
| `homepage_ha_token` | 2025-09-14 | 2026-08-28 | -67 |
| `jellyfin_api_key` | 2026-09-10 | 2027-08-15 | 285 |
| `k3s_readonly_token` | 2026-01-20 | 2027-01-14 | 72 |
| `karakeep_homepage_api_key` | 2026-03-03 | 2027-02-03 | 92 |
| `karakeep_meili_master_key` | 2026-02-17 | 2027-01-25 | 83 |
| `karakeep_nextauth_secret` | 2026-03-24 | 2027-03-09 | 126 |
| `karakeep_python_api_key` | 2025-09-17 | 2026-08-21 | -74 |
| `livesync_db_password` | 2025-11-16 | 2026-11-02 | -1 |
| `livesync_sync_token` | 2025-09-04 | 2026-08-10 | -85 |
| `longhorn_b2_application_key` | 2026-05-29 | 2027-05-13 | 191 |
| `longhorn_b2_bucket` | 2026-05-29 | 2027-05-14 | 192 |
| `longhorn_b2_endpoint` | 2026-05-29 | 2027-05-04 | 182 |
| `longhorn_b2_key_id` | 2026-05-29 | 2027-05-04 | 182 |
| `monitor_bridge_ha_token` | 2025-11-06 | 2026-10-19 | -15 |
| `mqtt_password` | 2026-06-08 | 2027-05-20 | 198 |
| `mqtt_password_hash` | 2026-04-23 | 2027-03-29 | 146 |
| `n8n_api_key` | 2025-10-10 | 2026-09-18 | -46 |
| `n8n_runner_auth_token` | 2025-11-09 | 2026-11-03 | 0 |
| `nut_ha_password` | 2026-03-23 | 2027-03-03 | 120 |
| `nut_monitor_password` | 2026-03-13 | 2027-03-10 | 127 |
| `peanut_password` | 2026-10-16 | 2027-10-03 | 334 |
| `pi_peer_backup_ssh_key` | 2026-03-31 | 2027-03-29 | 146 |
| `pihole_password` | 2026-07-23 | 2027-07-03 | 242 |
| `prometheus_ha_token` | 2025-11-06 | 2026-10-26 | -8 |
| `prometheus_kuma_api_key` | 2025-09-18 | 2026-08-21 | -74 |
| `prowlarr_api_key` | 2026-10-30 | 2027-10-10 | 341 |
| `qbittorrent_password` | 2025-09-20 | 2026-08-28 | -67 |
| `r2_access_key_id` | 2026-04-28 | 2027-04-18 | 166 |
| `r2_secret_access_key` | 2026-04-11 | 2027-04-10 | 158 |
| `radarr_api_key` | 2026-08-29 | 2027-08-05 | 275 |
| `renovate_agent_claude_oauth_token` | 2026-06-01 | 2027-05-21 | 199 |
| `renovate_agent_gh_token` | 2026-07-21 | 2027-07-13 | 252 |
| `scrutiny_influxdb_admin_password` | 2026-07-23 | 2027-06-29 | 238 |
| `scrutiny_influxdb_token` | 2026-03-10 | 2027-02-09 | 98 |
| `smtp_notify_app_password` | 2026-04-29 | 2027-04-24 | 172 |
| `sonarr_api_key` | 2026-08-29 | 2027-08-28 | 298 |
| `speedtest_api_token` | 2026-08-30 | 2027-08-24 | 294 |
| `speedtest_app_key` | 2025-08-25 | 2026-07-29 | -97 |
| `terraria_password` | 2025-12-26 | 2026-12-09 | 36 |
| `uptime_kuma_password` | 2026-03-24 | 2027-03-11 | 128 |
| `valheim_server_pass` | 2026-08-30 | 2027-08-01 | 271 |

## external

lives in a third-party system; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `cloudflare_dns_token` | 2026-08-30 | 2027-08-29 | 299 |
| `coinmarket_api_key` | 2026-08-30 | 2027-08-29 | 299 |
| `crowdsec_discord_webhook_url` | 2026-03-17 | 2027-03-14 | 131 |
| `crowdsec_mapquest_api_key` | 2026-04-05 | 2027-03-28 | 145 |
| `flux_github_status_token` | 2026-10-12 | 2027-09-16 | 317 |
| `gitops_deploy_discord_webhook` | 2026-10-04 | 2027-09-07 | 308 |
| `healthchecks_discord_webhook_url` | 2026-04-17 | 2027-04-05 | 153 |
| `homelab_builder_github_token` | 2026-05-31 | 2027-05-21 | 199 |
| `karakeep_gemini_api_key` | 2026-04-17 | 2027-03-21 | 138 |
| `monitor_bridge_deploy_pipeline_github_token` | 2026-10-11 | 2027-09-22 | 323 |
| `monitor_discord_webhook_url` | 2026-09-19 | 2027-08-27 | 297 |
| `mullvad_account` | 2025-11-16 | 2026-10-18 | -16 |
| `weather_api_key` | 2026-08-30 | 2027-08-04 | 274 |
| `wifi_password` | 2026-07-20 | 2027-06-24 | 233 |
| `wireguard_interface_private_key` | 2026-02-09 | 2027-02-06 | 95 |

## external-90d

lives in a third-party system and expires after 90 days; rotate there first.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `promote_token` | 2026-10-12 | 2027-01-04 | 62 |

## auto

rotated unattended by the weekly secret-rotate cron.

| Secret | Last rotated | Due | Days left |
|---|---|---|---|
| `aide_daniel_box_push_token` | 2026-08-24 | 2027-02-19 | 108 |
| `aide_daniel_pi_push_token` | 2026-07-07 | 2026-12-20 | 47 |
| `aide_daniel_server_push_token` | 2026-07-28 | 2027-01-17 | 75 |
| `kuma_status_page_sync_push_token` | 2026-07-08 | 2026-12-28 | 55 |
| `render_records_push_token` | 2026-08-17 | 2027-01-30 | 88 |
| `rkhunter_daniel_box_push_token` | 2026-07-29 | 2027-01-13 | 71 |
| `rkhunter_daniel_pi_push_token` | 2026-07-12 | 2027-01-08 | 66 |
| `rkhunter_daniel_server_push_token` | 2026-10-12 | 2027-03-28 | 145 |

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
