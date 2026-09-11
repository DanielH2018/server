"""Tests for the Pending-Status-Checks dwell check (issue #886).

Its own module rather than more of test_notify_logic.py: this check owns a real captured
dashboard fixture, and keeping the fixture beside the tests that read it makes the coupling
obvious. `dashboard_body.txt` is issue #3's body as of 2026-09-02, with the Detected
Dependencies listing truncated — nothing in notify_logic parses it.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import notify_logic as nl
import pending_logic as pl

FIXTURE = (pathlib.Path(__file__).resolve().parent / "dashboard_body.txt").read_text()
# The same dashboard a week later, after Renovate renamed the item marker `approvePr-branch=`
# to `unpend-branch=`. Both captures are kept: the check must read either rendering, and one
# fixture per marker is what proves it.
FIXTURE_2026_09_09 = (
    pathlib.Path(__file__).resolve().parent / "dashboard_body_2026-09-09.txt"
).read_text()

# Named members the fixture-driven parse MUST find. A count assertion alone would still pass
# if Renovate renamed the section and parse_pending returned {} — five guards in this repo
# broke exactly that way. These two are the extremes the check has to cover: a version bump
# and a mutable-tag digest bump.
_FIXTURE_MEMBERS = frozenset(
    {
        "renovate/k8s-image-grafanagrafana",
        "renovate/k8s-image-ghcr.iogethomepagehomepage",
    }
)

# Named members of the newer capture, chosen the same way: one version bump, one mutable-tag
# digest bump. A count alone would have passed all through the week the marker rename made the
# parse return nothing.
_FIXTURE_2026_09_09_MEMBERS = frozenset(
    {
        "renovate/k8s-image-traefik",
        "renovate/k8s-image-ghcr.iogethomepagehomepage",
    }
)

DAY = 86400.0


def test_parse_pending_finds_named_members_in_the_real_dashboard():
    parsed = pl.parse_pending(FIXTURE)
    missing = _FIXTURE_MEMBERS - set(parsed)
    assert not missing, "parse_pending lost %s" % sorted(missing)
    assert (
        parsed["renovate/k8s-image-grafanagrafana"]
        == "Update grafana/grafana Docker tag to v13.2.1"
    )


def test_parse_pending_reads_every_item_in_the_section():
    # Every approvePr-branch= checkbox in the captured body sits in this section; none may
    # be dropped.
    assert len(pl.parse_pending(FIXTURE)) == FIXTURE.count("approvePr-branch=")


def test_parse_pending_reads_the_renamed_unpend_marker():
    parsed = pl.parse_pending(FIXTURE_2026_09_09)
    missing = _FIXTURE_2026_09_09_MEMBERS - set(parsed)
    assert not missing, "parse_pending lost %s" % sorted(missing)
    assert (
        parsed["renovate/k8s-image-traefik"] == "Update traefik Docker tag to v3.7.13"
    )
    assert len(parsed) == FIXTURE_2026_09_09.count("unpend-branch=")


def test_pending_section_unreadable_is_clean_on_both_captures():
    assert pl.pending_section_unreadable(FIXTURE) is False
    assert pl.pending_section_unreadable(FIXTURE_2026_09_09) is False


def test_pending_section_unreadable_is_flagged_when_the_marker_is_renamed_again():
    # Header intact, every item carrying a marker this module does not know: the exact shape
    # that read 26 live items as zero. `dashboard_headers_unrecognized` cannot see it.
    body = (
        "## Pending Status Checks\n\n"
        " - [ ] <!-- someNewName-branch=renovate/foo -->Update foo to v2\n"
    )
    assert nl.dashboard_headers_unrecognized(body) is False
    assert pl.pending_section_unreadable(body) is True


def test_pending_section_unreadable_is_false_when_nothing_is_pending():
    # Renovate omits the header entirely when the section is empty, so no-header is the
    # healthy state and must not page.
    assert pl.pending_section_unreadable("## Open\n\nnothing pending") is False


def test_parse_pending_stops_at_the_next_section():
    # "Awaiting Schedule" sits ABOVE Pending Status Checks and carries an unschedule-branch
    # marker, not an approvePr-branch one; Detected Dependencies sits below.
    assert "renovate/lock-file-maintenance" not in pl.parse_pending(FIXTURE)


def test_parse_pending_keeps_an_item_that_already_has_a_pr_link():
    # kube-state-metrics renders as [Update ...](../pull/891) — still pending, still parsed.
    key = "renovate/k8s-image-registry.k8s.iokube-state-metricskube-state-metrics"
    assert key in pl.parse_pending(FIXTURE)


def test_parse_pending_absent_section_is_empty():
    assert pl.parse_pending("## Detected Dependencies\n\nnothing pending") == {}
    assert pl.parse_pending("") == {}


def test_dashboard_headers_unrecognized_is_clean_on_the_real_body():
    assert nl.dashboard_headers_unrecognized(FIXTURE) is False


def test_dashboard_headers_unrecognized_is_flagged_on_a_renamed_section():
    assert nl.dashboard_headers_unrecognized("## Soaking Updates\n\n - [ ] x") is True
    assert nl.dashboard_headers_unrecognized("") is True


def test_dashboard_body_distinguishes_absent_from_empty():
    issues = [{"title": "Dependency Dashboard", "user": {"login": "renovate[bot]"}}]
    assert nl.dashboard_body(issues) == ""
    assert nl.dashboard_body([]) is None


def test_item_soak_is_three_days_for_a_digest_bump():
    assert pl.item_soak_days("Update python:3.14-alpine Docker digest to c6ead21") == 3


def test_item_soak_is_seven_days_for_a_version_bump():
    assert pl.item_soak_days("Update grafana/grafana Docker tag to v13.2.1") == 7
    # Unrecognised wording takes the LONGER soak: a misread delays, never invents.
    assert pl.item_soak_days("something else entirely") == 7


def test_soak_constants_match_renovate_json():
    """The two soaks are read from renovate.json, not trusted to a comment.

    A minimumReleaseAge change there must fail here rather than silently leave notify_logic
    measuring against a soak that no longer applies.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[5]
    rules = json.loads((repo_root / "renovate.json").read_text())["packageRules"]
    digest = {
        r["minimumReleaseAge"]
        for r in rules
        if "minimumReleaseAge" in r and "digest" in (r.get("matchUpdateTypes") or [])
    }
    assert digest == {"%d days" % pl.DIGEST_SOAK_DAYS}
    non_digest = {
        r["minimumReleaseAge"]
        for r in rules
        if "minimumReleaseAge" in r
        and "digest" not in (r.get("matchUpdateTypes") or [])
    }
    assert "%d days" % pl.VERSION_SOAK_DAYS in non_digest


def test_update_pending_seen_stamps_new_items_and_keeps_old_ones():
    seen = pl.update_pending_seen({"a": 100.0}, {"a": "x", "b": "y"}, 500.0)
    assert seen == {"a": 100.0, "b": 500.0}


def test_update_pending_seen_drops_departed_items():
    # The item left the section (its PR was raised), so its clock must not survive to re-page.
    assert pl.update_pending_seen({"a": 100.0}, {"b": "y"}, 500.0) == {"b": 500.0}


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


def test_stale_pending_uses_the_shorter_allowance_for_a_digest_item():
    now = 1_000_000.0
    digest = {"renovate/d": "Update foo:latest Docker digest to abc1234"}
    version = {"renovate/v": "Update foo Docker tag to v2"}
    seen = {"renovate/d": now - 12 * DAY, "renovate/v": now - 12 * DAY}
    # 12 days is past 3+7 but short of 7+7: the digest item fires, the version item does not.
    assert [i[0] for i in pl.stale_pending(seen, digest, now)] == ["renovate/d"]
    assert pl.stale_pending(seen, version, now) == []


def test_stale_pending_treats_an_unseen_item_as_first_seen_now():
    # The first run after this ships has an empty state file. Seeding it must not page for
    # every pending item at once.
    assert pl.stale_pending({}, pl.parse_pending(FIXTURE), 1_000_000.0) == []


def test_stale_pending_sorts_worst_offender_first():
    now = 1_000_000.0
    current = {"renovate/a": "tag to v1", "renovate/b": "tag to v2"}
    seen = {"renovate/a": now - 20 * DAY, "renovate/b": now - 111 * DAY}
    assert [i[0] for i in pl.stale_pending(seen, current, now)] == [
        "renovate/b",
        "renovate/a",
    ]


def test_pending_fingerprint_repages_each_week_it_stays_stuck():
    week2 = pl.pending_fingerprint([("renovate/x", "d", 15)])
    week3 = pl.pending_fingerprint([("renovate/x", "d", 22)])
    assert week2 != week3
    # ...but stays silent on the daily ticks within one week.
    assert week2 == pl.pending_fingerprint([("renovate/x", "d", 17)])


def test_pending_fingerprint_is_sorted_and_stable():
    a = pl.pending_fingerprint([("renovate/a", "d", 20), ("renovate/b", "d", 20)])
    b = pl.pending_fingerprint([("renovate/b", "d", 20), ("renovate/a", "d", 20)])
    assert a == b


def test_render_pending_names_the_item_the_dwell_and_the_remedy():
    msg = pl.render_pending(
        [
            (
                "renovate/k8s-image-grafanapromtail",
                "Update grafana/promtail to 3.6.11",
                111,
            )
        ]
    )
    assert "Update grafana/promtail to 3.6.11" in msg
    assert "111 days" in msg
    assert "renovate/k8s-image-grafanapromtail" in msg
    assert "Tick its box" in msg


def test_render_pending_stays_under_discords_cap_with_the_whole_section_stuck():
    """The state this check exists to report is also the one that overflows Discord.

    Every item in the captured dashboard, stuck at once: ~22 lines of ~145 characters is past
    the 2000-character cap, and an over-long post is REJECTED — which leaves the fingerprint
    unadvanced and re-posts the same oversized message daily.
    """
    now = 1_000_000.0
    current = pl.parse_pending(FIXTURE)
    seen = {branch: now - 40 * DAY for branch in current}
    items = pl.stale_pending(seen, current, now)
    assert len(items) == len(current), (
        "the whole section must be stuck for this test to bite"
    )
    msg = pl.render_pending(items)
    assert len(msg) <= 1500
    assert "…and" in msg, "a truncated render must say how many it dropped"
    assert pl.PENDING_REMEDY in msg, "the remedy line must survive truncation"


def test_render_pending_keeps_the_worst_offender_when_it_truncates():
    now = 1_000_000.0
    current = pl.parse_pending(FIXTURE)
    seen = {branch: now - 40 * DAY for branch in current}
    worst = "renovate/k8s-image-grafanagrafana"
    seen[worst] = now - 400 * DAY
    msg = pl.render_pending(pl.stale_pending(seen, current, now))
    assert worst in msg
    assert "400 days" in msg


def test_render_pending_does_not_truncate_a_short_list():
    msg = pl.render_pending([("renovate/x", "Update foo to v2", 20)])
    assert "…and" not in msg


# --- Dwell-state loss (issue #1526) ---------------------------------------------------------
# A reset is only useful if it names when the check is trustworthy again, so the dates are
# asserted as literal strings against a fixed epoch: deriving them from VERSION_SOAK_DAYS +
# PENDING_GRACE_DAYS inside the test would assert the file equals itself and would still pass
# if either constant drifted.
_RESET_NOW = (
    1_788_990_155.26  # 2026-09-09 21:42 UTC — the run that reset all 27 live clocks
)


def test_pending_clock_ready_names_the_date_a_version_clock_becomes_usable():
    assert pl.pending_clock_ready(_RESET_NOW, pl.VERSION_SOAK_DAYS) == "2026-09-23"


def test_pending_clock_ready_names_a_nearer_date_for_a_digest_clock():
    assert pl.pending_clock_ready(_RESET_NOW, pl.DIGEST_SOAK_DAYS) == "2026-09-19"


def test_render_pending_reset_carries_both_dates_and_the_dashboard():
    msg = pl.render_pending_reset(_RESET_NOW, "o/r")
    assert "2026-09-23" in msg
    assert "2026-09-19" in msg
    assert "https://github.com/o/r/issues/3" in msg


def test_pending_reset_fingerprint_is_stable_within_one_day():
    """The same loss must page once, not on every run of the day it happened."""
    assert pl.pending_reset_fingerprint(_RESET_NOW) == pl.pending_reset_fingerprint(
        _RESET_NOW + 3600
    )


def test_pending_reset_fingerprint_moves_on_a_second_loss():
    """A fresh loss during the blind window pushes the ready date out, so it re-pages."""
    assert pl.pending_reset_fingerprint(_RESET_NOW) != pl.pending_reset_fingerprint(
        _RESET_NOW + 2 * DAY
    )
