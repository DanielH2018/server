"""Properties of `desired_labels` over any label set and any disjoint tier lists."""

import longhorn_reconcile as lr
from hypothesis import example, given
from hypothesis import strategies as st

CLAIMS = ("ns/a", "ns/b", "ns/c", "ns/d")
OTHER_KEYS = ("app", "longhorn.io/other")


@st.composite
def worlds(draw):
    placement = draw(
        st.fixed_dictionaries(
            {
                claim: st.one_of(
                    st.none(), st.just("nobackup"), st.sampled_from(lr.SHARDS)
                )
                for claim in CLAIMS
            }
        )
    )
    tiers = {
        "r2": set(),
        "weekly": {c: p for c, p in placement.items() if isinstance(p, int)},
        "nobackup": {c for c, p in placement.items() if p == "nobackup"},
    }
    keys = draw(st.sets(st.sampled_from(sorted(lr.label_keys()))))
    labels = dict.fromkeys(keys, lr.ENABLED)
    labels.update(draw(st.dictionaries(st.sampled_from(OTHER_KEYS), st.text())))
    return (
        labels,
        draw(st.sampled_from(CLAIMS)),
        tiers,
        draw(st.sets(st.sampled_from(CLAIMS))),
        draw(st.booleans()),
    )


def _groups(labels):
    return {k for k in lr.group_keys() if labels.get(k) == lr.ENABLED}


# The issue's reproduction: a no-backup claim whose volume holds only a stale shard label.
STALE_SHARD = (
    {lr.label(lr.shard_group(3)): lr.ENABLED},
    "ns/c",
    {"r2": set(), "weekly": {}, "nobackup": {"ns/c"}},
    set(),
    False,
)


@given(worlds())
@example(STALE_SHARD)
def test_a_second_pass_changes_nothing(world):
    labels, claim, tiers, class_claims, trim = world
    once = lr.desired_labels(labels, claim, tiers, class_claims, trim)
    assert lr.desired_labels(once, claim, tiers, class_claims, trim) == once


@given(worlds())
@example(STALE_SHARD)
def test_a_no_backup_claim_in_any_backup_group_ends_in_no_backup_alone(world):
    labels, claim, tiers, class_claims, trim = world
    tiers = {
        "r2": set(),
        "weekly": {c: s for c, s in tiers["weekly"].items() if c != claim},
        "nobackup": tiers["nobackup"] | {claim},
    }
    if not _groups(labels):
        return
    after = lr.desired_labels(labels, claim, tiers, class_claims, trim)
    assert _groups(after) == {lr.label(lr.NO_BACKUP)}


@given(worlds())
def test_a_weekly_claim_outside_no_backup_ends_in_its_shard_alone(world):
    labels, claim, tiers, class_claims, trim = world
    shard = tiers["weekly"].get(claim, 0)
    tiers = {
        "r2": set(),
        "weekly": {**tiers["weekly"], claim: shard},
        "nobackup": tiers["nobackup"] - {claim},
    }
    if not _groups(labels) or labels.get(lr.label(lr.NO_BACKUP)) == lr.ENABLED:
        return
    after = lr.desired_labels(labels, claim, tiers, class_claims, trim)
    assert _groups(after) == {lr.label(lr.shard_group(shard))}


@given(worlds())
def test_labels_outside_the_group_and_trim_keys_come_back_unchanged(world):
    labels, claim, tiers, class_claims, trim = world
    after = lr.desired_labels(labels, claim, tiers, class_claims, trim)
    owned = lr.label_keys()
    assert {k: v for k, v in after.items() if k not in owned} == {
        k: v for k, v in labels.items() if k not in owned
    }
