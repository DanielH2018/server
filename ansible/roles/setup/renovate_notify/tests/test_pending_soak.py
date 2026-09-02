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

FIXTURE = (pathlib.Path(__file__).resolve().parent / "dashboard_body.txt").read_text()

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

DAY = 86400.0


def test_parse_pending_finds_named_members_in_the_real_dashboard():
    parsed = nl.parse_pending(FIXTURE)
    missing = _FIXTURE_MEMBERS - set(parsed)
    assert not missing, "parse_pending lost %s" % sorted(missing)
    assert (
        parsed["renovate/k8s-image-grafanagrafana"]
        == "Update grafana/grafana Docker tag to v13.2.1"
    )


def test_parse_pending_reads_every_item_in_the_section():
    # Every approvePr-branch= checkbox in the captured body sits in this section; none may
    # be dropped.
    assert len(nl.parse_pending(FIXTURE)) == FIXTURE.count("approvePr-branch=")


def test_parse_pending_stops_at_the_next_section():
    # "Awaiting Schedule" sits ABOVE Pending Status Checks and carries an unschedule-branch
    # marker, not an approvePr-branch one; Detected Dependencies sits below.
    assert "renovate/lock-file-maintenance" not in nl.parse_pending(FIXTURE)


def test_parse_pending_keeps_an_item_that_already_has_a_pr_link():
    # kube-state-metrics renders as [Update ...](../pull/891) — still pending, still parsed.
    key = "renovate/k8s-image-registry.k8s.iokube-state-metricskube-state-metrics"
    assert key in nl.parse_pending(FIXTURE)


def test_parse_pending_absent_section_is_empty():
    assert nl.parse_pending("## Detected Dependencies\n\nnothing pending") == {}
    assert nl.parse_pending("") == {}


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
    assert nl.item_soak_days("Update python:3.14-alpine Docker digest to c6ead21") == 3


def test_item_soak_is_seven_days_for_a_version_bump():
    assert nl.item_soak_days("Update grafana/grafana Docker tag to v13.2.1") == 7
    # Unrecognised wording takes the LONGER soak: a misread delays, never invents.
    assert nl.item_soak_days("something else entirely") == 7


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
    assert digest == {"%d days" % nl.DIGEST_SOAK_DAYS}
    non_digest = {
        r["minimumReleaseAge"]
        for r in rules
        if "minimumReleaseAge" in r
        and "digest" not in (r.get("matchUpdateTypes") or [])
    }
    assert "%d days" % nl.VERSION_SOAK_DAYS in non_digest


def test_update_pending_seen_stamps_new_items_and_keeps_old_ones():
    seen = nl.update_pending_seen({"a": 100.0}, {"a": "x", "b": "y"}, 500.0)
    assert seen == {"a": 100.0, "b": 500.0}


def test_update_pending_seen_drops_departed_items():
    # The item left the section (its PR was raised), so its clock must not survive to re-page.
    assert nl.update_pending_seen({"a": 100.0}, {"b": "y"}, 500.0) == {"b": 500.0}


def test_stale_pending_is_clean_inside_the_allowance():
    now = 1_000_000.0
    current = {"renovate/x": "Update foo Docker tag to v2"}
    seen = {"renovate/x": now - 13 * DAY}  # 7-day soak + 7-day grace = 14
    assert nl.stale_pending(seen, current, now) == []


def test_stale_pending_is_flagged_past_the_allowance():
    now = 1_000_000.0
    current = {"renovate/x": "Update foo Docker tag to v2"}
    seen = {"renovate/x": now - 20 * DAY}
    assert nl.stale_pending(seen, current, now) == [
        ("renovate/x", "Update foo Docker tag to v2", 20)
    ]


def test_stale_pending_uses_the_shorter_allowance_for_a_digest_item():
    now = 1_000_000.0
    digest = {"renovate/d": "Update foo:latest Docker digest to abc1234"}
    version = {"renovate/v": "Update foo Docker tag to v2"}
    seen = {"renovate/d": now - 12 * DAY, "renovate/v": now - 12 * DAY}
    # 12 days is past 3+7 but short of 7+7: the digest item fires, the version item does not.
    assert [i[0] for i in nl.stale_pending(seen, digest, now)] == ["renovate/d"]
    assert nl.stale_pending(seen, version, now) == []


def test_stale_pending_treats_an_unseen_item_as_first_seen_now():
    # The first run after this ships has an empty state file. Seeding it must not page for
    # every pending item at once.
    assert nl.stale_pending({}, nl.parse_pending(FIXTURE), 1_000_000.0) == []


def test_stale_pending_sorts_worst_offender_first():
    now = 1_000_000.0
    current = {"renovate/a": "tag to v1", "renovate/b": "tag to v2"}
    seen = {"renovate/a": now - 20 * DAY, "renovate/b": now - 111 * DAY}
    assert [i[0] for i in nl.stale_pending(seen, current, now)] == [
        "renovate/b",
        "renovate/a",
    ]


def test_pending_fingerprint_repages_each_week_it_stays_stuck():
    week2 = nl.pending_fingerprint([("renovate/x", "d", 15)])
    week3 = nl.pending_fingerprint([("renovate/x", "d", 22)])
    assert week2 != week3
    # ...but stays silent on the daily ticks within one week.
    assert week2 == nl.pending_fingerprint([("renovate/x", "d", 17)])


def test_pending_fingerprint_is_sorted_and_stable():
    a = nl.pending_fingerprint([("renovate/a", "d", 20), ("renovate/b", "d", 20)])
    b = nl.pending_fingerprint([("renovate/b", "d", 20), ("renovate/a", "d", 20)])
    assert a == b


def test_render_pending_names_the_item_the_dwell_and_the_remedy():
    msg = nl.render_pending(
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
