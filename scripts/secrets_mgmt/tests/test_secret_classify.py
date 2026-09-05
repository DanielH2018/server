"""Tests for the name-to-tier classification in scripts/secrets_mgmt/secret_classify.py.

One test per rule, plus the default. The default is the interesting one: an unrecognised
secret must land in `assisted` (reminds, never touches) rather than in a tier the tool would
rotate unattended.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_classify.py
"""

from secrets_mgmt.secret_classify import classify


def test_push_tokens_are_auto():
    assert classify("monitor_bridge_cpu_push_token") == "auto"
    assert classify("pi_sd_health_push_token") == "auto"


def test_provider_creds_are_external():
    assert classify("cloudflare_dns_token") == "external"
    assert classify("monitor_discord_webhook_url") == "external"
    assert classify("mullvad_account") == "external"


def test_pinned_secrets_need_special_procedure():
    assert classify("authelia_storage") == "pinned"
    assert classify("zigbee_network_key") == "pinned"


def test_usernames_and_config_are_ignored():
    assert classify("authelia_user") == "ignore"
    assert classify("freshrss_username") == "ignore"
    assert classify("domain") == "ignore"


def test_unknown_app_secret_defaults_to_assisted():
    assert classify("some_new_app_password") == "assisted"
    assert classify("grafana_admin_password") == "assisted"
