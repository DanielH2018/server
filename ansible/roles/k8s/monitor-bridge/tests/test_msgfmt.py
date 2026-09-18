"""bridge.msgfmt groups a multi-item DOWN by reason and elides names before reasons (#2013)."""

import pytest

from bridge import msgfmt

FLEET = [f"svc-{i:02d}" for i in range(57)]
ONE_REASON = "host_vars/daniel-pi.yml changed [every service: node-exporter removed]"


def test_identical_reasons_collapse_to_one_group_with_the_count_and_five_names():
    msg = msgfmt.format_down("service", "stale", dict.fromkeys(FLEET, ONE_REASON))
    assert msg.startswith("57 services stale — " + ONE_REASON + " (svc-00, svc-01, ")
    assert msg.endswith("svc-04, +52)")
    assert msg.count(ONE_REASON) == 1
    assert len(msg) < 200


def test_distinct_reasons_each_get_a_group_largest_first():
    items = {"a": "reason B", "b": "reason A", "c": "reason A"}
    msg = msgfmt.format_down("service", "stale", items, details="probe.py x")
    assert msg == (
        "3 services stale — 2 reasons. reason A (2: b, c). reason B (1: a)."
        " Details: probe.py x"
    )


def test_a_single_item_names_it_inline():
    msg = msgfmt.format_down(
        "service", "stale", {"pi-peer-backup": "no release record"}
    )
    assert msg == "1 service stale: pi-peer-backup — no release record"


def test_over_the_limit_names_are_elided_before_reasons():
    reasons = {f"r{i}": f"reason number {i} " + "x" * 40 for i in range(6)}
    items = {f"{r}-{j}": text for r, text in reasons.items() for j in range(20)}
    msg = msgfmt.format_down("target", "down", items, limit=600)
    assert len(msg) <= 600
    for text in reasons.values():
        assert text in msg, "elision dropped a reason before dropping names"
    assert "+15)" not in msg, "names should have been elided below five per reason"


def test_past_what_elision_can_recover_the_cut_carries_a_marker_within_the_limit():
    items = {f"n{i}": "reason " + str(i) * 60 for i in range(30)}
    msg = msgfmt.format_down("thing", "down", items, limit=300)
    assert len(msg) <= 300
    assert msg.endswith(" chars)")


def test_empty_items_is_refused():
    with pytest.raises(ValueError):
        msgfmt.format_down("service", "stale", {})
