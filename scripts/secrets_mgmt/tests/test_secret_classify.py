"""Tests for the name-to-tier classification in scripts/secrets_mgmt/secrets_lib/secret_classify.py.

One test per suffix rule, plus the default. The default is the interesting one: an unrecognised
secret must land in `assisted`, the tier whose audit reminds a human to rotate it.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_classify.py
"""

from secrets_mgmt.secrets_lib.secret_classify import classify


def test_usernames_are_ignored():
    assert classify("authelia_user") == "ignore"
    assert classify("freshrss_username") == "ignore"


def test_unknown_app_secret_defaults_to_assisted():
    assert classify("some_new_app_password") == "assisted"
    assert classify("grafana_admin_password") == "assisted"
    assert classify("monitor_bridge_cpu_push_token") == "assisted"


def test_provider_and_pinned_names_are_not_recognised_by_name():
    # The registry's `tier` is the only place a key becomes external or pinned (#3749).
    assert classify("cloudflare_dns_token") == "assisted"
    assert classify("zigbee_network_key") == "assisted"
