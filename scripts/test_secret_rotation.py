"""Tests for the secret rotation registry tool (classification, staggering, audit, sync)."""
import datetime as dt

import secret_rotation as sr


# ── classification ──────────────────────────────────────────────────────────
def test_push_tokens_are_auto():
    assert sr.classify("monitor_bridge_cpu_push_token") == "auto"
    assert sr.classify("pi_sd_health_push_token") == "auto"


def test_provider_creds_are_external():
    assert sr.classify("cloudflare_dns_token") == "external"
    assert sr.classify("monitor_discord_webhook_url") == "external"
    assert sr.classify("mullvad_account") == "external"


def test_pinned_secrets_need_special_procedure():
    assert sr.classify("kopia_password") == "pinned"
    assert sr.classify("authelia_storage") == "pinned"


def test_usernames_and_config_are_ignored():
    assert sr.classify("authelia_user") == "ignore"
    assert sr.classify("freshrss_username") == "ignore"
    assert sr.classify("domain") == "ignore"
    assert sr.classify("wireguard_interface_address") == "ignore"


def test_unknown_app_secret_defaults_to_assisted():
    assert sr.classify("some_new_app_password") == "assisted"
    assert sr.classify("grafana_admin_password") == "assisted"


# ── staggered seeding ───────────────────────────────────────────────────────
def test_seed_is_deterministic():
    today = dt.date(2026, 6, 11)
    assert sr.seed_last_rotated("x_push_token", "auto", today) == \
           sr.seed_last_rotated("x_push_token", "auto", today)


def test_seed_never_immediately_overdue_and_within_window():
    today = dt.date(2026, 6, 11)
    for name in ("a_push_token", "b_push_token", "grafana_admin_password", "cloudflare_dns_token"):
        tier = sr.classify(name)
        seed = dt.date.fromisoformat(sr.seed_last_rotated(name, tier, today))
        due = seed + dt.timedelta(days=sr.TIER_DAYS[tier])
        assert due > today                          # not overdue at registration
        assert due <= today + dt.timedelta(days=sr.TIER_DAYS[tier])  # within one cadence


def test_ignore_and_no_date_tiers_have_no_seed():
    assert sr.seed_last_rotated("authelia_user", "ignore", dt.date(2026, 6, 11)) is None


def test_seeds_spread_due_dates_no_single_day_pileup():
    today = dt.date(2026, 6, 11)
    names = ["mb_%d_push_token" % i for i in range(20)]
    due = []
    for n in names:
        seed = dt.date.fromisoformat(sr.seed_last_rotated(n, "auto", today))
        due.append(seed + dt.timedelta(days=sr.TIER_DAYS["auto"]))
    # 20 auto secrets must not all fall on the same day — expect many distinct due dates.
    assert len(set(due)) >= 12


# ── audit ───────────────────────────────────────────────────────────────────
def _reg(*entries):
    return {"secrets": {name: {"tier": tier, "last_rotated": lr} for name, tier, lr in entries}}


def test_audit_flags_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("old_push_token", "auto", "2025-01-01"),       # long overdue
        ("fresh_push_token", "auto", "2026-06-01"),      # fine
    )
    res = sr.audit(reg, today)
    overdue_names = [r[0] for r in res["overdue"]]
    assert "old_push_token" in overdue_names
    assert "fresh_push_token" not in overdue_names
    assert res["by_tier"].get("auto") == 1


def test_audit_ignores_tiers_without_a_cadence():
    today = dt.date(2026, 6, 11)
    reg = _reg(("authelia_user", "ignore", None))
    res = sr.audit(today=today, reg=reg)
    assert res["all"] == []


def test_due_date_pinned_uses_long_cadence():
    entry = {"tier": "pinned", "last_rotated": "2026-01-01"}
    assert sr.due_date(entry) == dt.date(2026, 1, 1) + dt.timedelta(days=730)


# ── sync ────────────────────────────────────────────────────────────────────
def test_sync_adds_missing_and_preserves_existing():
    today = dt.date(2026, 6, 11)
    reg = _reg(("kept_push_token", "auto", "2026-05-05"))
    added, stale = sr.sync(reg, ["kept_push_token", "new_push_token"], today)
    assert added == ["new_push_token"]
    assert reg["secrets"]["kept_push_token"]["last_rotated"] == "2026-05-05"  # untouched
    assert reg["secrets"]["new_push_token"]["tier"] == "auto"


def test_sync_reports_stale_registry_entries():
    today = dt.date(2026, 6, 11)
    reg = _reg(("gone_push_token", "auto", "2026-05-05"))
    added, stale = sr.sync(reg, [], today)
    assert stale == ["gone_push_token"]


def test_sync_preserves_a_manual_tier_override():
    today = dt.date(2026, 6, 11)
    # Operator downgraded a push token to ignore — sync must not reclassify it.
    reg = _reg(("special_push_token", "ignore", None))
    sr.sync(reg, ["special_push_token"], today)
    assert reg["secrets"]["special_push_token"]["tier"] == "ignore"
