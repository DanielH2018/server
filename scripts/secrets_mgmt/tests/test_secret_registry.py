"""Tests for the registry logic in scripts/secrets_mgmt/secret_registry.py.

Seeding, reconciliation, due dates and drift — all of it pure over a dict, so every test
builds its own registry rather than reading the committed one. The cadence table comes from
`rotation_tools.DEFAULT_TIER_DAYS`, which
`test_the_default_tier_table_is_the_one_secret_rotation_assigns` holds equal to the literal
`secret_rotation.py` publishes.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_registry.py
"""

import datetime as dt

from secrets_mgmt.secret_classify import classify
from secrets_mgmt.secret_registry import (
    audit,
    due_date,
    registry_drift,
    seed_last_rotated,
    sync,
)
from secrets_mgmt.rotation_tools import DEFAULT_TIER_DAYS


def _reg(*entries):
    return {
        "entries": {
            name: {"tier": tier, "last_rotated": lr} for name, tier, lr in entries
        }
    }


# ── staggered seeding ───────────────────────────────────────────────────────
def test_seed_is_deterministic():
    today = dt.date(2026, 6, 11)
    assert seed_last_rotated("x_push_token", "auto", today) == seed_last_rotated(
        "x_push_token", "auto", today
    )


def test_seed_never_immediately_overdue_and_within_window():
    today = dt.date(2026, 6, 11)
    for name in (
        "a_push_token",
        "b_push_token",
        "grafana_admin_password",
        "cloudflare_dns_token",
    ):
        tier = classify(name)
        seeded, cadence = (
            seed_last_rotated(name, tier, today),
            DEFAULT_TIER_DAYS[tier],
        )
        assert seeded is not None and cadence is not None, f"{name} has no cadence"
        due = dt.date.fromisoformat(seeded) + dt.timedelta(days=cadence)
        assert due > today  # not overdue at registration
        assert due <= today + dt.timedelta(days=cadence)  # within one cadence


def test_ignore_and_no_date_tiers_have_no_seed():
    assert seed_last_rotated("authelia_user", "ignore", dt.date(2026, 6, 11)) is None


def test_seeds_spread_due_dates_no_single_day_pileup():
    today = dt.date(2026, 6, 11)
    names = ["mb_%d_push_token" % i for i in range(20)]
    due = []
    cadence = DEFAULT_TIER_DAYS["auto"]
    assert cadence is not None
    for n in names:
        seeded = seed_last_rotated(n, "auto", today)
        assert seeded is not None
        due.append(dt.date.fromisoformat(seeded) + dt.timedelta(days=cadence))
    # 20 auto secrets must not all fall on the same day — expect many distinct due dates.
    assert len(set(due)) >= 12


# ── audit ───────────────────────────────────────────────────────────────────
def test_audit_flags_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("old_push_token", "auto", "2025-01-01"),  # long overdue
        ("fresh_push_token", "auto", "2026-06-01"),  # fine
    )
    res = audit(reg, today)
    overdue_names = [r[0] for r in res["overdue"]]
    assert "old_push_token" in overdue_names
    assert "fresh_push_token" not in overdue_names
    assert res["by_tier"].get("auto") == 1


def test_audit_ignores_tiers_without_a_cadence():
    today = dt.date(2026, 6, 11)
    reg = _reg(("authelia_user", "ignore", None))
    res = audit(today=today, reg=reg)
    assert res["all"] == []


def test_due_date_pinned_uses_long_cadence():
    entry = {"tier": "pinned", "last_rotated": "2026-01-01"}
    assert due_date(entry) == dt.date(2026, 1, 1) + dt.timedelta(days=730)


# ── sync ────────────────────────────────────────────────────────────────────
def test_sync_adds_missing_and_preserves_existing():
    today = dt.date(2026, 6, 11)
    reg = _reg(("kept_push_token", "auto", "2026-05-05"))
    added, _stale = sync(reg, ["kept_push_token", "new_push_token"], today)
    assert added == ["new_push_token"]
    assert (
        reg["entries"]["kept_push_token"]["last_rotated"] == "2026-05-05"
    )  # untouched
    assert reg["entries"]["new_push_token"]["tier"] == "auto"


def test_sync_reports_stale_registry_entries():
    today = dt.date(2026, 6, 11)
    reg = _reg(("gone_push_token", "auto", "2026-05-05"))
    _added, stale = sync(reg, [], today)
    assert stale == ["gone_push_token"]


def test_sync_preserves_a_manual_tier_override():
    today = dt.date(2026, 6, 11)
    # Operator downgraded a push token to ignore — sync must not reclassify it.
    reg = _reg(("special_push_token", "ignore", None))
    sync(reg, ["special_push_token"], today)
    assert reg["entries"]["special_push_token"]["tier"] == "ignore"


# ── registry drift (the `audit --check` CI gate) ─────────────────────────────
def test_registry_drift_detects_missing_and_stale():
    missing, stale = registry_drift({"a", "b"}, {"b", "c"})
    assert missing == ["c"]  # in secrets.yml, not the registry (forgot `sync`)
    assert stale == ["a"]  # in the registry, secret removed from secrets.yml


def test_registry_drift_clean_when_in_sync():
    assert registry_drift({"a", "b"}, {"a", "b"}) == ([], [])
