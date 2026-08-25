"""Tests for the secret rotation registry tool (classification, staggering, audit, sync)."""

import datetime as dt
import subprocess

import yaml

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
    assert sr.classify("authelia_storage") == "pinned"
    assert sr.classify("zigbee_network_key") == "pinned"


def test_usernames_and_config_are_ignored():
    assert sr.classify("authelia_user") == "ignore"
    assert sr.classify("freshrss_username") == "ignore"
    assert sr.classify("domain") == "ignore"


def test_unknown_app_secret_defaults_to_assisted():
    assert sr.classify("some_new_app_password") == "assisted"
    assert sr.classify("grafana_admin_password") == "assisted"


# ── staggered seeding ───────────────────────────────────────────────────────
def test_seed_is_deterministic():
    today = dt.date(2026, 6, 11)
    assert sr.seed_last_rotated("x_push_token", "auto", today) == sr.seed_last_rotated(
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
        tier = sr.classify(name)
        seed = dt.date.fromisoformat(sr.seed_last_rotated(name, tier, today))
        due = seed + dt.timedelta(days=sr.TIER_DAYS[tier])
        assert due > today  # not overdue at registration
        assert due <= today + dt.timedelta(
            days=sr.TIER_DAYS[tier]
        )  # within one cadence


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
    return {
        "secrets": {
            name: {"tier": tier, "last_rotated": lr} for name, tier, lr in entries
        }
    }


def test_audit_flags_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("old_push_token", "auto", "2025-01-01"),  # long overdue
        ("fresh_push_token", "auto", "2026-06-01"),  # fine
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


def test_audit_summary_names_overdue_secrets():
    # The pushed Kuma msg must NAME which secret is overdue — a bare count can't tell a genuine
    # cron break from one of the consumer-less known-manual auto tokens merely coming due (M1).
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("secret_rotation_push_token", "auto", "2025-01-01"),
        ("fresh_push_token", "auto", "2026-06-01"),
    )
    summary = sr.audit_summary(sr.audit(reg, today), [], [])
    assert "secret_rotation_push_token" in summary
    assert "1 auto" in summary


def test_audit_summary_clean_when_nothing_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(("fresh_push_token", "auto", "2026-06-01"))
    assert (
        sr.audit_summary(sr.audit(reg, today), [], [])
        == "all secrets within rotation window"
    )


def test_audit_summary_caps_the_overdue_name_list():
    today = dt.date(2026, 6, 11)
    reg = _reg(*[("t%02d_push_token" % i, "auto", "2025-01-01") for i in range(8)])
    summary = sr.audit_summary(sr.audit(reg, today), [], [])
    assert "+3 more" in summary  # 8 overdue → first 5 named, then "+3 more"


# ── sync ────────────────────────────────────────────────────────────────────
def test_sync_adds_missing_and_preserves_existing():
    today = dt.date(2026, 6, 11)
    reg = _reg(("kept_push_token", "auto", "2026-05-05"))
    added, stale = sr.sync(reg, ["kept_push_token", "new_push_token"], today)
    assert added == ["new_push_token"]
    assert (
        reg["secrets"]["kept_push_token"]["last_rotated"] == "2026-05-05"
    )  # untouched
    assert reg["secrets"]["new_push_token"]["tier"] == "auto"


def test_sync_reports_stale_registry_entries():
    today = dt.date(2026, 6, 11)
    reg = _reg(("gone_push_token", "auto", "2026-05-05"))
    added, stale = sr.sync(reg, [], today)
    assert stale == ["gone_push_token"]


# ── registry drift (the `audit --check` CI gate) ─────────────────────────────
def test_registry_drift_detects_missing_and_stale():
    missing, stale = sr.registry_drift({"a", "b"}, {"b", "c"})
    assert missing == ["c"]  # in secrets.yml, not the registry (forgot `sync`)
    assert stale == ["a"]  # in the registry, secret removed from secrets.yml


def test_registry_drift_clean_when_in_sync():
    assert sr.registry_drift({"a", "b"}, {"a", "b"}) == ([], [])


# ── consumer mapping (which redeploy applies a rotated token) ────────────────
def test_consumer_tag_monitor_bridge_tokens():
    assert sr.consumer_tag("monitor_bridge_cpu_push_token") == "monitor-bridge"
    assert sr.consumer_tag("kopia_restore_drill_push_token") == "monitor-bridge"


def test_consumer_tag_cloudflare_ddns_tokens():
    assert sr.consumer_tag("cloudflare_ddns_proxied_push_token") == "cloudflare-ddns"


def test_consumer_tag_cross_host_tokens_are_manual():
    # Cross-host / self-referential — the unattended cron must NOT auto-rotate these.
    assert sr.consumer_tag("pi_sd_health_push_token") is None
    assert (
        sr.consumer_tag("pi_recovery_push_token") is None
    )  # Pi cron, manual Pi deploy
    assert sr.consumer_tag("secret_rotation_push_token") is None


def test_consumer_tag_autofix_bridge_token():
    # Single-host, single-redeploy auto token — must auto-rotate, not false-skip as cross-host.
    assert sr.consumer_tag("arr_autoblock_push_token") == "autofix-bridge"


def test_every_auto_tier_token_resolves_a_consumer_or_is_known_manual():
    # Registry-driven guard: a new single-host `auto` push token must resolve a consumer_tag
    # (so the unattended weekly `rotate --commit --deploy` cron actually rotates it) or sit in
    # the explicit known-manual allowlist. Without this, a token whose consumer_tag falls
    # through to None silently drops out of rotation and only surfaces months later as an
    # OVERDUE page — exactly how arr_autoblock_push_token slipped in when autofix-bridge landed.
    # Derived from the script's own CROSS_HOST_PUSH_TOKENS (single source — the fifth
    # cross-host token, daniel_box_disk, is when the hand-list here moved there): each entry
    # documents its pusher/label host pair beside the name.
    known_manual = sr.CROSS_HOST_PUSH_TOKENS
    reg = sr.load_registry()
    auto = [n for n, m in reg["secrets"].items() if m.get("tier") == "auto"]
    assert auto  # sanity: the registry has auto-tier tokens
    unrotatable = [
        n for n in auto if sr.consumer_tag(n) is None and n not in known_manual
    ]
    assert not unrotatable, (
        "auto-tier tokens with no consumer_tag and not known-manual — they silently drop "
        "out of unattended rotation: %s" % unrotatable
    )


def test_no_cross_host_token_is_badly_overdue():
    # 2026-08-24 review M-3, second run of the same finding. The test above proves each
    # cross-host token is DECLARED manual; nothing proved anyone was doing the manual part.
    # `consumer_tag` returning None is deliberate and documented (secret_rotation.py:105-108):
    # the pusher and the AutoKuma label live on different hosts, so one redeploy cannot update
    # both halves atomically. But the design that skips them assumes an operator picks them up,
    # and the only thing asking was the daily audit line — which reports the whole registry and
    # is easy to skim past. Two consecutive reviews found the same tokens unrotated.
    #
    # So the reminder becomes a CI failure. This is deliberately NOT the audit's own due-date:
    # the point is to catch sustained neglect, not to fail the build the day something comes
    # due. Rotating one is a manual, two-host procedure — see docs/secret-rotation.md.
    #
    # NOT the fix the reviewer proposed. That was to give CROSS_HOST_PUSH_TOKENS a two-tag
    # consumer list, which is not representable: consumer_tag is typed `str | None` and returns
    # a SINGLE tag across four call sites, and a multi-tag return contradicts the rationale
    # above rather than implementing it.
    grace_days = 30
    reg = sr.load_registry()
    today = dt.date.today()
    res = sr.audit(reg, today)
    badly_overdue = [
        (name, -days)
        for name, _tier, _due, days in res["all"]
        if name in sr.CROSS_HOST_PUSH_TOKENS and days < -grace_days
    ]
    assert not badly_overdue, (
        f"cross-host push tokens more than {grace_days} days overdue: {badly_overdue}. These "
        f"are skipped by the unattended weekly cron BY DESIGN, so nothing rotates them but a "
        f"person. Rotate them (docs/secret-rotation.md), then `uv run python "
        f"scripts/secret_rotation.py sync` and commit."
    )


def test_unattended_rotation_picks_tokens_up_before_they_go_overdue():
    # Weekly cron + rotate-only-when-overdue left every token overdue up to 6 days while
    # the daily audit paged DOWN on it (2026-07-09 review). The pick-up window must catch
    # anything due within the next cron interval, and still catch a genuinely missed one.
    rows = [
        ("due_next_week_push_token", "auto", dt.date(2026, 7, 14), 5),
        ("missed_push_token", "auto", dt.date(2026, 7, 6), -3),
        ("not_due_push_token", "auto", dt.date(2026, 9, 7), 60),
        ("app_password", "assisted", dt.date(2026, 7, 10), 1),  # never auto-rotated
    ]
    names = [r[0] for r in sr.unattended_due(rows)]
    assert "due_next_week_push_token" in names  # rotates BEFORE going overdue
    assert "missed_push_token" in names  # a missed rotation still gets caught
    assert "not_due_push_token" not in names  # staggering preserved
    assert "app_password" not in names
    assert len(sr.unattended_due(rows, rotate_all=True)) == 3  # --all: every auto row


def test_unattended_rotation_lead_exceeds_the_cron_interval():
    # The lead window must be longer than the weekly cron interval, else a token due the
    # day after a Sunday run goes overdue before the next run — the exact gap this fixes.
    assert sr.ROTATE_LEAD_DAYS > 7


def test_sync_preserves_a_manual_tier_override():
    today = dt.date(2026, 6, 11)
    # Operator downgraded a push token to ignore — sync must not reclassify it.
    reg = _reg(("special_push_token", "ignore", None))
    sr.sync(reg, ["special_push_token"], today)
    assert reg["secrets"]["special_push_token"]["tier"] == "ignore"


# The registry is the single plaintext source of names/tiers/dates. A save/load
# corruption is SILENT (the next sync/audit reads garbage), so pin the contract:
# round-trips losslessly, keeps the MANAGED header, and sorts keys deterministically
# (sort_keys=True keeps the committed file diff-stable as secrets are added).


def test_registry_round_trips_losslessly(tmp_path):
    reg = {
        "secrets": {
            "b_token": {"tier": "auto", "last_rotated": "2026-06-01"},
            "a_token": {"tier": "assisted", "last_rotated": "2026-05-15"},
        }
    }
    path = str(tmp_path / "reg.yml")
    sr.save_registry(reg, path)
    assert sr.load_registry(path) == reg


def test_saved_registry_keeps_managed_header_and_sorts_keys(tmp_path):
    path = str(tmp_path / "reg.yml")
    sr.save_registry(
        {"secrets": {"z_tok": {"tier": "auto"}, "a_tok": {"tier": "auto"}}}, path
    )
    text = (tmp_path / "reg.yml").read_text()
    assert text.startswith("# Secret rotation registry — MANAGED")
    assert text.index("\n  a_tok:") < text.index("\n  z_tok:")  # sort_keys=True


def test_load_registry_missing_file_returns_empty_skeleton(tmp_path):
    assert sr.load_registry(str(tmp_path / "does-not-exist.yml")) == {"secrets": {}}


# ── rotation dates derived from git ─────────────────────────────────────────
def _fake_history(monkeypatch, revs):
    """revs: newest-first [(sha, "YYYY-MM-DD", {name: ciphertext})]."""
    log = "\n".join("%s %s" % (sha, day) for sha, day, _ in revs)
    blobs = {sha: values for sha, _, values in revs}

    def fake_git(*args):
        if args[0] == "log":
            return log + "\n"
        return yaml.safe_dump(blobs[args[1].split(":", 1)[0]])

    monkeypatch.setattr(sr, "_git", fake_git)


def test_derived_date_is_the_commit_that_changed_the_value(monkeypatch):
    _fake_history(
        monkeypatch,
        [
            ("c", "2026-08-01", {"tok": "ENC[new]"}),
            ("b", "2026-05-01", {"tok": "ENC[old]"}),
            ("a", "2026-01-01", {"tok": "ENC[old]"}),
        ],
    )
    assert sr.ciphertext_rotation_dates()["tok"] == dt.date(2026, 8, 1)


def test_unchanged_value_dates_to_the_oldest_revision(monkeypatch):
    _fake_history(
        monkeypatch,
        [
            ("b", "2026-08-01", {"tok": "ENC[same]"}),
            ("a", "2026-01-01", {"tok": "ENC[same]"}),
        ],
    )
    assert sr.ciphertext_rotation_dates()["tok"] == dt.date(2026, 1, 1)


def test_reordering_does_not_count_as_a_rotation(monkeypatch):
    """A regroup rewrites most of the file's lines while changing no value. Comparing the
    parsed value per key is what stops that marking every secret freshly rotated."""
    _fake_history(
        monkeypatch,
        [
            ("b", "2026-08-01", {"b_tok": "ENC[b]", "a_tok": "ENC[a]"}),
            ("a", "2026-01-01", {"a_tok": "ENC[a]", "b_tok": "ENC[b]"}),
        ],
    )
    dates = sr.ciphertext_rotation_dates()
    assert dates["a_tok"] == dt.date(2026, 1, 1)
    assert dates["b_tok"] == dt.date(2026, 1, 1)


def test_advance_moves_a_stale_date_forward():
    reg = {"secrets": {"tok": {"tier": "assisted", "last_rotated": "2025-08-24"}}}
    advanced = sr.advance_last_rotated(reg, {"tok": dt.date(2026, 3, 13)})
    assert advanced == [("tok", "2025-08-24", "2026-03-13")]
    assert reg["secrets"]["tok"]["last_rotated"] == "2026-03-13"


def test_advance_never_moves_a_date_backward():
    """Advance-only is what stops this creating an overdue secret: a registry date newer
    than git's — a rotation recorded before its commit landed — must survive."""
    reg = {"secrets": {"tok": {"tier": "assisted", "last_rotated": "2026-08-25"}}}
    assert sr.advance_last_rotated(reg, {"tok": dt.date(2026, 3, 13)}) == []
    assert reg["secrets"]["tok"]["last_rotated"] == "2026-08-25"


def test_advance_ignores_secrets_git_has_no_date_for():
    reg = {"secrets": {"tok": {"tier": "assisted", "last_rotated": "2025-08-24"}}}
    assert sr.advance_last_rotated(reg, {}) == []
    assert reg["secrets"]["tok"]["last_rotated"] == "2025-08-24"


def test_derivation_failure_degrades_to_recorded_dates(monkeypatch):
    """A cron that cannot read git must fall back, not fail — a broken derivation taking
    the monitor down would be a worse outage than the drift it corrects."""

    def boom(*args):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(sr, "_git", boom)
    assert sr.derived_rotation_dates() == {}
