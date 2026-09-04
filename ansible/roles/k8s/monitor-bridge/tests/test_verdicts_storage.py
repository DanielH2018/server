"""The storage verdicts as pure functions, one accept/reject pair per rule.

`checks/storage.py` fetches and applies the hysteresis; everything decided here is decided from
plain values. The end-to-end tests for the two checks live in test_check_longhorn.py and
test_check.py — these cover the decision on its own, which is where a wrong threshold shows up
as a wrong verdict rather than as a stubbing mistake.
"""

from verdicts.storage import (
    longhorn_offenders,
    longhorn_redundancy_verdict,
    pvc_fullness_verdict,
)


def _row(name, state, key="pvc"):
    return ({key: name, "state": state}, 1.0)


def test_offenders_dedupe_across_the_two_managers():
    # The two longhorn-manager pods report disjoint subsets, and a volume can appear in both.
    assert longhorn_offenders([_row("a", "degraded"), _row("a", "degraded")]) == {
        "a": "degraded"
    }


def test_offenders_let_faulted_outrank_degraded_in_either_order():
    assert longhorn_offenders([_row("a", "degraded"), _row("a", "faulted")]) == {
        "a": "faulted"
    }
    assert longhorn_offenders([_row("a", "faulted"), _row("a", "degraded")]) == {
        "a": "faulted"
    }


def test_offenders_fall_back_to_the_volume_label():
    # A volume with no PVC (a detached or manually created one) still has to be nameable.
    assert longhorn_offenders([({"volume": "v1", "state": "faulted"}, 1.0)]) == {
        "v1": "faulted"
    }


def test_redundancy_is_ok_with_volumes_and_no_offenders():
    ok, msg, grace = longhorn_redundancy_verdict(43, {})
    assert ok
    assert "43 volume(s) redundant" in msg
    assert grace == ""


def test_redundancy_absent_series_is_a_breach_not_green():
    """The rejecting half of the arm above, and the whole reason the census query exists.

    The degraded selector returns empty both when every volume is healthy and when the longhorn
    scrape job is dead, so the healthy answer has to come from a series count rather than from
    the absence of offenders.
    """
    for volumes in (None, 0):
        ok, msg, grace = longhorn_redundancy_verdict(volumes, {})
        assert not ok
        assert "UNMONITORED" in msg
        assert grace == "scrape gap grace"


def test_redundancy_names_faulted_and_degraded_separately():
    ok, msg, grace = longhorn_redundancy_verdict(
        10, {"a": "faulted", "b": "degraded", "c": "degraded"}
    )
    assert not ok
    assert "1 faulted (a)" in msg
    assert "2 degraded, single-copy (b, c)" in msg
    assert grace == "drain/reboot grace"


def test_pvc_all_clear_reports_the_worst_claim():
    breach, census, summary = pvc_fullness_verdict(
        [("sonarr-config", "homelab", 12.0), ("n8n-data", "homelab", 71.0)],
        43,
        85,
        32,
    )
    assert breach == ""
    assert census == ""
    assert "2 claim(s) under 85%" in summary
    assert "homelab/n8n-data 71%" in summary


def test_pvc_breach_names_the_fullest_first():
    breach, census, _ = pvc_fullness_verdict(
        [("a", "homelab", 86.0), ("b", "homelab", 99.0)], 43, 85, 32
    )
    assert breach.startswith("PVC over 85%: homelab/b 99%, homelab/a 86%")
    assert census == ""


def test_pvc_thin_census_is_unknown_not_ok():
    """The rejecting half of the all-clear: 27 claims all under the limit is not coverage.

    Losing the kubelet scrape job leaves the apiserver job answering for 27 of 43 claims, every
    one healthy, while daniel-server's go dark. PVC_MIN_CLAIMS (32) is sized above that number
    precisely so this reads as blind rather than green.
    """
    breach, census, summary = pvc_fullness_verdict([("a", "homelab", 12.0)], 27, 85, 32)
    assert breach == ""
    assert "only 27 kubelet_volume_stats claims visible" in census
    assert "UNKNOWN, not OK" in census
    # The summary is still produced; the caller prefers the census complaint over it.
    assert summary


def test_pvc_missing_census_is_unknown_too():
    _, census, _ = pvc_fullness_verdict([("a", "homelab", 12.0)], None, 85, 32)
    assert "no kubelet_volume_stats claims visible" in census


def test_pvc_no_ratio_at_all_is_its_own_fault_and_skips_the_floor():
    """An empty ratio vector looks exactly like "no claim is full" and is not the same fact.

    It also has to win over the census floor: with no claims reporting a ratio, both arms would
    otherwise fire and the message would name the floor rather than the blindness.
    """
    breach, census, summary = pvc_fullness_verdict([], 0, 85, 32)
    assert breach == ""
    assert (
        census == "no PVC reported a fullness ratio — PVC fullness is UNKNOWN, not OK"
    )
    assert summary == ""


def test_pvc_breach_and_thin_census_can_fire_together():
    # A cycle can be simultaneously blind and full; both arms must report so the caller can
    # advance the census streak while returning the breach.
    breach, census, _ = pvc_fullness_verdict([("a", "homelab", 99.0)], 27, 85, 32)
    assert breach
    assert census
