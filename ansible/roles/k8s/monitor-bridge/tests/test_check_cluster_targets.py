"""check_cluster_targets: the hysteresis that separates a rollout's scrape gap from a dead target.

66 DOWN episodes in the 30 days to 2026-09-11 — the highest count in the estate — nearly all of
them one cycle long and naming a single target that was rolling at the time. `up` goes to 0 for
a scrape or two whenever a workload restarts, and this check could not tell that from an
exporter that died.

Each pair is one input the gate must hold and one it must let through, so a gate that suppressed
everything and one that suppressed nothing are distinguishable from the passing side.

The check takes its fetcher as `fetch`, the seam `checks/host_edge.py` already uses, so these
tests hand it an `up` vector rather than patching the module bridge.net.
"""

from dataclasses import replace

import bridge.streaks
import pytest
from checks.cluster import check_cluster_targets


@pytest.fixture
def ccfg(cfg):
    """cfg with a cluster Prometheus configured — without one the check short-circuits."""
    return replace(cfg, CLUSTER_PROM_URL="http://cluster-prometheus:9090")


def up_vector(*values):
    """A fetch stub answering with one `up` series per value, jobs named job0..jobN."""

    def _fetch(_cfg, _promql, **_kw):
        return [({"job": "job%d" % i}, v) for i, v in enumerate(values)]

    return _fetch


def test_all_targets_up_is_clean(ccfg):
    ok, msg = check_cluster_targets(ccfg, fetch=up_vector(1, 1, 1, 1))
    assert ok, msg
    assert "all 4 targets up" in msg


def test_one_down_cycle_is_held_not_paged(ccfg):
    # The rollout case: a single scrape gap on one target. This is what produced nearly every one
    # of the 66 episodes, and it must not open an episode now.
    ok, msg = check_cluster_targets(ccfg, fetch=up_vector(1, 1, 1, 0))
    assert ok, msg
    assert "down streak 1/3" in msg
    assert "rollout scrape gap" in msg


def test_the_third_straight_down_cycle_pages(ccfg):
    # The red proof: the gate delays, it does not suppress. A target still down after
    # CLUSTER_TARGETS_CONSECUTIVE cycles is an exporter that died, and it pages.
    fetch = up_vector(1, 1, 1, 0)
    for _ in range(2):
        assert check_cluster_targets(ccfg, fetch=fetch)[0]
    ok, msg = check_cluster_targets(ccfg, fetch=fetch)
    assert not ok
    assert "job3" in msg
    assert "3 cycles" in msg


def test_one_ok_cycle_resets_the_streak(ccfg):
    # Without the reset a check that flickers down-up-down-up over hours would accumulate to the
    # threshold and page, which is the opposite of what the gate is for.
    check_cluster_targets(ccfg, fetch=up_vector(1, 1, 1, 0))
    check_cluster_targets(ccfg, fetch=up_vector(1, 1, 1, 1))
    assert bridge.streaks._down_streaks["cluster_targets"] == 0
    ok, msg = check_cluster_targets(ccfg, fetch=up_vector(1, 1, 1, 0))
    assert ok, msg
    assert "down streak 1/3" in msg


def test_a_threshold_of_one_pages_immediately(ccfg):
    # The gate is configuration, not a hardcoded 3: set to 1 it restores the old behaviour.
    # Without this, a CLUSTER_TARGETS_CONSECUTIVE that stopped being read would read green above.
    ok, msg = check_cluster_targets(
        replace(ccfg, CLUSTER_TARGETS_CONSECUTIVE=1), fetch=up_vector(1, 1, 1, 0)
    )
    assert not ok
    assert "job3" in msg


def test_too_few_targets_still_fails_closed(ccfg):
    # The floor is a different fault from a down target — the metrics estate has vanished, so
    # target health is UNKNOWN. It rides the same streak, which is correct: an emptied `up`
    # during a Prometheus roll is the same transient.
    fetch = up_vector(1)
    for _ in range(2):
        check_cluster_targets(ccfg, fetch=fetch)
    ok, msg = check_cluster_targets(ccfg, fetch=fetch)
    assert not ok
    assert "below the floor" in msg


def test_no_cluster_prometheus_disables_the_check(cfg):
    # The empty-URL short circuit runs ahead of the streak, so a disabled check never accumulates
    # one — otherwise enabling it later would page on its first real down cycle.
    ok, msg = check_cluster_targets(cfg, fetch=up_vector(0, 0, 0))
    assert ok, msg
    assert "disabled" in msg
    assert "cluster_targets" not in bridge.streaks._down_streaks
