"""Tests for the churn arm — the floor under a dwell keyed on the digest.

Its own module rather than more of test_pending_soak.py, which is at its length cap. The seam
is the clock each arm reads: the dwell arm times the digest on the branch, this one times the
branch itself, and only the second can page for a tag re-pushed faster than its own soak.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import pending_logic as pl

DAY = 86400.0


def test_churning_pending_fires_on_a_branch_whose_digest_never_ages_out():
    """The hole the digest clock opens, and the floor under it.

    `nginx:alpine` is re-pushed about every 3.6 days against a 1-day soak, so no digest ever
    reaches the 1+7 allowance. The branch clock must still page.
    """
    now = 1_000_000.0
    desc = "Update nginx:alpine Docker digest to bbbbbbb"
    current = {"renovate/nginx": desc}
    seen = {
        "renovate/nginx": now - 30 * DAY,
        pl.content_key("renovate/nginx", desc): now - 2 * DAY,
    }
    assert pl.stale_pending(seen, current, now) == [], (
        "no digest is old enough on its own"
    )
    assert pl.churning_pending(seen, current, now) == [("renovate/nginx", desc, 30, 2)]


def test_churning_pending_is_clean_inside_the_churn_allowance():
    now = 1_000_000.0
    desc = "Update nginx:alpine Docker digest to bbbbbbb"
    current = {"renovate/nginx": desc}
    # (1-day soak + 7-day grace) * 3 = 24 days.
    seen = {
        "renovate/nginx": now - 23 * DAY,
        pl.content_key("renovate/nginx", desc): now - 2 * DAY,
    }
    assert pl.churning_pending(seen, current, now) == []


def test_churning_pending_leaves_a_row_the_dwell_arm_already_reports():
    """One row, one line: the arms page about the same item for different reasons."""
    now = 1_000_000.0
    desc = "Update foo Docker tag to v2"
    current = {"renovate/x": desc}
    seen = {
        "renovate/x": now - 60 * DAY,
        pl.content_key("renovate/x", desc): now - 60 * DAY,
    }
    assert [i[0] for i in pl.stale_pending(seen, current, now)] == ["renovate/x"]
    assert pl.churning_pending(seen, current, now) == []


def test_churn_fingerprint_repages_weekly_and_ignores_the_digest_dwell():
    week4 = pl.churn_fingerprint([("renovate/x", "d", 30, 2)])
    assert week4 == pl.churn_fingerprint([("renovate/x", "d", 31, 5)]), (
        "a re-push inside the same week must not re-page"
    )
    assert week4 != pl.churn_fingerprint([("renovate/x", "d", 37, 2)])


def test_render_churning_names_both_dwells_and_the_remedy():
    msg = pl.render_churning(
        [("renovate/nginx", "Update nginx:alpine digest to bbb", 30, 2)]
    )
    assert "30 days" in msg
    assert "only 2" in msg
    assert "renovate/nginx" in msg
    assert pl.CHURN_REMEDY in msg


def test_render_churning_stays_bounded_with_a_whole_section_churning():
    now = 1_000_000.0
    current = {
        "renovate/b%02d" % i: "Update pkg%02d Docker digest to abcdef%02d" % (i, i)
        for i in range(30)
    }
    seen = {branch: now - 90 * DAY for branch in current}
    # Each digest arrived today, so only the branch clock can page for any of them.
    seen.update({pl.content_key(b, d): now for b, d in current.items()})
    items = pl.churning_pending(seen, current, now)
    assert len(items) == 30
    msg = pl.render_churning(items)
    assert len(msg) <= 1200
    assert "\u2026and " in msg, "a 30-item list must truncate rather than be dropped"
    assert pl.CHURN_REMEDY in msg


def test_stale_pending_is_clean_inside_the_allowance():
    now = 1_000_000.0
    current = {"renovate/x": "Update foo Docker tag to v2"}
    seen = {"renovate/x": now - 13 * DAY}  # 7-day soak + 7-day grace = 14
    assert pl.stale_pending(seen, current, now) == []


def test_stale_pending_is_flagged_past_the_allowance():
    now = 1_000_000.0
    current = {"renovate/x": "Update foo Docker tag to v2"}
    seen = {"renovate/x": now - 20 * DAY}
    assert pl.stale_pending(seen, current, now) == [
        ("renovate/x", "Update foo Docker tag to v2", 20)
    ]
