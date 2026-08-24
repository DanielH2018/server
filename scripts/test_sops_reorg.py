"""Tests for the secrets.yml section regrouper.

Every fixture here uses fake keys and fake values -- the point of the script is that
it never inspects a value, so the tests never need a real one.
"""

import sops_reorg


def test_values_are_moved_verbatim():
    text = "b_key: some!! literal $value\na_key: another\n"
    result, _ = sops_reorg.reorganize(text)
    assert "b_key: some!! literal $value" in result
    assert "a_key: another" in result


def test_keys_are_sorted_within_a_section():
    text = "zigbee_network_key: z\nmqtt_username: m\nmqtt_password: p\n"
    result, _ = sops_reorg.reorganize(text)
    body = [line for line in result.splitlines() if line and not line.startswith("#")]
    assert body == ["mqtt_password: p", "mqtt_username: m", "zigbee_network_key: z"]


def test_push_tokens_are_consolidated_under_one_header():
    text = (
        "monitor_bridge_cpu_push_token: a\n"
        "grafana_admin_password: b\n"
        "monitor_bridge_ups_push_token: c\n"
    )
    result, _ = sops_reorg.reorganize(text)
    lines = result.splitlines()
    tokens = [i for i, line in enumerate(lines) if line.startswith("monitor_bridge_")]
    assert tokens == [1, 2], (
        "push tokens should be adjacent, directly under their header"
    )


def test_pruned_keys_are_dropped_and_counted():
    text = "traefik_user: u\ntraefik_password: p\ndomain: example.com\n"
    result, dropped = sops_reorg.reorganize(text)
    assert dropped == 2
    assert "traefik_" not in result
    assert "domain: example.com" in result


def test_prune_set_matches_the_audit():
    """Guards the reviewed list -- a key added here without an audit fails the suite."""
    assert sops_reorg.PRUNE == {
        "traefik_user",
        "traefik_password",
        "monitor_bridge_docker_user_push_token",
        "authelia_beszel_password_hash",
    }


def test_the_cloudflare_drift_token_is_kept():
    """Held on purpose: the cloudflare_ips allowlist it watched is still live."""
    assert "monitor_bridge_cloudflare_drift_push_token" not in sops_reorg.PRUNE


def test_a_pruned_key_does_not_take_its_section_with_it():
    text = "authelia_beszel_password_hash: h\nauthelia_jwt: j\n"
    result, dropped = sops_reorg.reorganize(text)
    assert dropped == 1
    assert "beszel" not in result
    assert "authelia_jwt: j" in result


def test_block_scalars_keep_their_continuation_lines():
    text = (
        "pi_peer_backup_ssh_key: |\n"
        "  -----BEGIN KEY-----\n"
        "  abcdef\n"
        "  -----END KEY-----\n"
        "domain: example.com\n"
    )
    result, _ = sops_reorg.reorganize(text)
    lines = result.splitlines()
    start = lines.index("pi_peer_backup_ssh_key: |")
    assert lines[start + 1 : start + 4] == [
        "  -----BEGIN KEY-----",
        "  abcdef",
        "  -----END KEY-----",
    ]


def test_stale_section_comments_are_dropped_but_annotations_survive():
    text = "# Kopia\n# My Calendar\ncalendar_1: url\n"
    result, _ = sops_reorg.reorganize(text)
    assert "# Kopia" not in result
    assert "# My Calendar" in result


def test_an_empty_section_emits_no_header():
    result, _ = sops_reorg.reorganize("domain: example.com\n")
    assert result.count("#") == 1


def test_no_key_falls_through_to_the_catch_all():
    """The live key inventory should be fully classified; a new prefix shows up here."""
    catch_all = sops_reorg.SECTIONS[-1][0]
    unclassified = []
    for key in LIVE_KEYS:
        for title, matches in sops_reorg.SECTIONS:
            if matches(key):
                if title == catch_all:
                    unclassified.append(key)
                break
    assert unclassified == []


# Key names only, from `sops -d ansible/vars/secrets.yml | grep -oE '^[a-z_]+:'`
# as of 2026-08-15. No values -- this is the classification surface, not data.
LIVE_KEYS = [
    "domain",
    "email",
    "become_password",
    "cloudflare_dns_token",
    "cloudflare_ddns_proxied_push_token",
    "cloudflare_ddns_direct_push_token",
    "cloudflare_analytics_token",
    "authelia_user",
    "authelia_password",
    "authelia_secret",
    "authelia_storage",
    "authelia_jwt",
    "authelia_oidc_hmac_secret",
    "authelia_client_password_hash",
    "authelia_beszel_password_hash",
    "authelia_oidc_rsa_key_content",
    "code_server_password",
    "code_server_sudo_password",
    "pihole_password",
    "crowdsec_username",
    "crowdsec_password",
    "crowdsec_discord_webhook_url",
    "crowdsec_mapquest_api_key",
    "crowdsec_k8s_bouncer_api_key",
    "crowdsec_k8s_agent_password",
    "monitor_discord_webhook_url",
    "healthchecks_password",
    "healthchecks_smtp_user",
    "healthchecks_smtp_password",
    "healthchecks_discord_webhook_url",
    "healthchecks_ping_key",
    "weather_api_key",
    "coinmarket_api_key",
    "sonarr_api_key",
    "radarr_api_key",
    "jellyfin_api_key",
    "prowlarr_api_key",
    "arr_discord_webhook_url",
    "arr_autoblock_push_token",
    "qbittorrent_username",
    "qbittorrent_password",
    "freshrss_username",
    "freshrss_password",
    "wireguard_interface_private_key",
    "mullvad_account",
    "speedtest_app_key",
    "terraria_password",
    "valheim_server_pass",
    "n8n_runner_auth_token",
    "n8n_api_key",
    "livesync_db_password",
    "livesync_sync_token",
    "nut_monitor_password",
    "nut_ha_password",
    "peanut_username",
    "peanut_password",
    "uptime_kuma_username",
    "uptime_kuma_password",
    "karakeep_gemini_api_key",
    "karakeep_meili_master_key",
    "karakeep_nextauth_secret",
    "karakeep_python_api_key",
    "karakeep_homepage_api_key",
    "calendar_1",
    "calendar_2",
    "calendar_3",
    "calendar_4",
    "kopia_b2_key_id",
    "kopia_b2_application_key",
    "kopia_b2_bucket",
    "kopia_b2_endpoint",
    "r2_access_key_id",
    "r2_secret_access_key",
    "r2_account_id",
    "r2_bucket",
    "grafana_admin_password",
    "scrutiny_influxdb_token",
    "scrutiny_influxdb_admin_password",
    "prometheus_ha_token",
    "prometheus_kuma_api_key",
    "gitops_deploy_discord_webhook",
    "secret_rotation_push_token",
    "manifest_prune_push_token",
    "mqtt_password_hash",
    "mqtt_password",
    "mqtt_username",
    "zigbee_network_key",
    "claude_ha_token",
    "homepage_ha_token",
    "google_assistant_service_account",
    "claude_otel_push_token",
    "homelab_mcp_token",
    "pi_sd_health_push_token",
    "pi_recovery_push_token",
    "pi_peer_backup_ssh_key",
    "daniel_box_disk_push_token",
    "longhorn_backup_push_token",
    "smtp_notify_app_password",
    "handy_master_secret",
    "monitor_bridge_disk_push_token",
    "monitor_bridge_cert_push_token",
    "monitor_bridge_mem_push_token",
    "monitor_bridge_cpu_push_token",
    "monitor_bridge_restarts_push_token",
    "monitor_bridge_oom_push_token",
    "monitor_bridge_targets_push_token",
    "monitor_bridge_traefik_push_token",
    "monitor_bridge_gitops_alive_push_token",
    "monitor_bridge_gitops_status_push_token",
    "monitor_bridge_scrutiny_push_token",
    "monitor_bridge_pi_push_token",
    "monitor_bridge_ha_token",
    "monitor_bridge_ha_push_token",
    "monitor_bridge_renovate_alive_push_token",
    "monitor_bridge_loki_push_token",
    "monitor_bridge_discord_push_token",
    "monitor_bridge_prometheus_push_token",
    "monitor_bridge_arr_queue_push_token",
    "monitor_bridge_prowlarr_indexers_push_token",
    "monitor_bridge_janitorr_push_token",
    "monitor_bridge_loki_reachable_push_token",
    "monitor_bridge_pi_peers_push_token",
    "monitor_bridge_home_allowlist_push_token",
    "monitor_bridge_promtail_dropped_push_token",
    "monitor_bridge_ups_push_token",
    "monitor_bridge_appsec_push_token",
    "monitor_bridge_fake_remux_push_token",
    "monitor_bridge_configarr_push_token",
    "monitor_bridge_fake_remux_replace_push_token",
    "monitor_bridge_traefik_latency_push_token",
    "monitor_bridge_b2_reachable_push_token",
    "monitor_bridge_k8s_workloads_push_token",
    "monitor_bridge_cluster_prometheus_push_token",
    "monitor_bridge_cluster_targets_push_token",
    "monitor_bridge_n8n_push_token",
    "monitor_bridge_r2_usage_push_token",
    "monitor_bridge_docker_user_push_token",
    "monitor_bridge_cloudflare_drift_push_token",
]
