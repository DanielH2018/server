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
    stagger_span,
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
        due = due_date(name, {"tier": tier, "last_rotated": seeded})
        assert due is not None
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
        due.append(due_date(n, {"tier": "auto", "last_rotated": seeded}))
    # 20 auto secrets must not all fall on the same day — expect many distinct due dates.
    assert len(set(due)) >= 12


# ── the stagger survives a rotation ─────────────────────────────────────────
# Accept/reject pair. `_is_clean` proves the real `due_date` fans a same-day batch out;
# `_is_flagged` runs the stagger-free formula the tool used before over the SAME batch and
# proves it piles up. Without the second half the first is only ever observed passing, and
# a stagger that stopped applying would read green.
BATCH = ["mb_%d_push_token" % i for i in range(30)]
ROTATED_ON = "2026-09-05"  # one `rotate --commit` run stamps today on every name


def test_same_day_batch_rotation_fans_due_dates_out_is_clean():
    """A batch rotation must not collapse the tier onto one due date.

    `rotate` stamps `now` on every secret it touches and `advance_last_rotated` snaps a
    hand-rotated one to its ciphertext's commit date, so `last_rotated` is identical across
    a batch. The spread has to come from the cadence, not from the stamp.
    """
    due = [due_date(n, {"tier": "auto", "last_rotated": ROTATED_ON}) for n in BATCH]
    assert len(set(due)) >= 10, "30 secrets rotated on one day share too few due dates"
    busiest = max(due.count(d) for d in set(due))
    assert busiest <= 5, "%d of 30 still land on one day" % busiest


def test_same_day_batch_without_the_stagger_piles_up_is_flagged():
    """The red proof: the pre-fix formula on the same batch gives one due date for all 30."""
    cadence = DEFAULT_TIER_DAYS["auto"]
    assert cadence is not None
    unstaggered = {
        dt.date.fromisoformat(ROTATED_ON) + dt.timedelta(days=cadence) for _ in BATCH
    }
    assert len(unstaggered) == 1, (
        "the pre-fix formula no longer piles up — retire this pair"
    )


def test_stagger_only_ever_pulls_a_due_date_earlier():
    """The cadence in docs/secret-rotation.md is a ceiling, so the stagger may only subtract.

    Adding to it would extend a published cadence and could flip an existing OVERDUE green.
    """
    for tier, cadence in DEFAULT_TIER_DAYS.items():
        if not cadence:
            continue
        for n in BATCH:
            d = due_date(n, {"tier": tier, "last_rotated": ROTATED_ON})
            assert d is not None
            naive = dt.date.fromisoformat(ROTATED_ON) + dt.timedelta(days=cadence)
            assert naive - dt.timedelta(days=stagger_span(cadence)) <= d <= naive


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
    due = due_date("authelia_storage_key", entry)
    assert due is not None
    assert (
        dt.date(2026, 1, 1) + dt.timedelta(days=730 - 61)
        <= due
        <= dt.date(2026, 1, 1) + dt.timedelta(days=730)
    )


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
