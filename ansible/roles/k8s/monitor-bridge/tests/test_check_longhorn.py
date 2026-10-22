"""Longhorn volume redundancy, and the extended-resource advertisement beside it.

Degradation is held for a streak before paging, because a replica rebuild is routine. An absent
metric is never green: the query returns nothing both when every volume is redundant and when
kube-state-metrics is not reporting at all.
"""

from dataclasses import replace

import checks.storage
import checks.cluster
from _fake_sources import FakeSources


def _longhorn_series(pvc, state, pod="longhorn-manager-a"):
    return ({"pvc": pvc, "state": state, "pod": pod, "volume": "pvc-" + pvc}, 1.0)


def _longhorn_sources(vector, volumes=43.0):
    return FakeSources(
        prom_scalar=lambda *a, **k: volumes, prom_vector=lambda *a, **k: vector
    )


def _arm_longhorn(cfg, vector, volumes=43.0, consecutive=3):
    return replace(cfg, LONGHORN_CONSECUTIVE=consecutive), _longhorn_sources(
        vector, volumes
    )


def test_longhorn_all_redundant_is_up_and_reports_the_volume_count(cfg):
    cfg, src = _arm_longhorn(cfg, [])
    ok, msg = checks.storage.check_longhorn_volumes(cfg, src)
    assert ok
    assert "43 volume(s) redundant" in msg


def test_longhorn_degraded_holds_up_until_the_threshold_then_pages(cfg):
    cfg, src = _arm_longhorn(cfg, [_longhorn_series("freshrss-config", "degraded")])
    # A node drain degrades every volume on the departing node by design, so the first
    # cycles must hold `up` — otherwise this monitor pages every Sunday reboot.
    ok1, msg1 = checks.storage.check_longhorn_volumes(cfg, src)
    ok2, _ = checks.storage.check_longhorn_volumes(cfg, src)
    ok3, msg3 = checks.storage.check_longhorn_volumes(cfg, src)
    assert ok1 and ok2
    assert "1/3" in msg1
    assert not ok3
    assert "freshrss-config" in msg3
    assert "single-copy" in msg3


def test_longhorn_recovery_resets_the_streak(cfg):
    cfg, src = _arm_longhorn(cfg, [_longhorn_series("freshrss-config", "degraded")])
    checks.storage.check_longhorn_volumes(cfg, src)
    assert src.state.down_streaks["longhorn"] == 1
    healthy = _longhorn_sources([])
    healthy.state = src.state
    assert checks.storage.check_longhorn_volumes(cfg, healthy)[0]
    assert src.state.down_streaks["longhorn"] == 0


def test_longhorn_absent_metric_is_not_green(cfg):
    # The whole point of the arm: an empty degraded-selector looks identical whether the
    # cluster is healthy or the longhorn scrape job is dead. The volume count is the input
    # assertion, so a missing family must fail closed rather than read as "none degraded".
    cfg, src = _arm_longhorn(cfg, [], volumes=None)
    ok1, msg1 = checks.storage.check_longhorn_volumes(cfg, src)
    assert ok1  # first cycle rides the grace, but says why
    assert "UNMONITORED" in msg1
    checks.storage.check_longhorn_volumes(cfg, src)
    ok3, msg3 = checks.storage.check_longhorn_volumes(cfg, src)
    assert not ok3
    assert "not the same as healthy" in msg3


def test_longhorn_dedupes_a_volume_reported_by_both_managers(cfg):
    # The two longhorn-manager pods report disjoint subsets today, but a volume moving
    # between them must not be double-counted into the message.
    cfg, src = _arm_longhorn(
        cfg,
        [
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-a"),
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-b"),
        ],
        consecutive=1,
    )
    ok, msg = checks.storage.check_longhorn_volumes(cfg, src)
    assert not ok
    assert "1 degraded" in msg


def test_longhorn_faulted_outranks_degraded_for_the_same_volume(cfg):
    cfg, src = _arm_longhorn(
        cfg,
        [
            _longhorn_series("valheim-data", "degraded"),
            _longhorn_series("valheim-data", "faulted"),
        ],
        consecutive=1,
    )
    ok, msg = checks.storage.check_longhorn_volumes(cfg, src)
    assert not ok
    assert "1 faulted" in msg
    assert "degraded" not in msg


def test_longhorn_ignores_healthy_and_detached_volumes_in_the_live_state_vector(
    cfg,
):
    # The vector carries every volume's live state since #3668, so the healthy and detached
    # rows reach longhorn_offenders and must be filtered there rather than by the selector.
    cfg, src = _arm_longhorn(
        cfg,
        [
            _longhorn_series("jellyfin-config", "healthy"),
            _longhorn_series("terraria-data", "unknown"),
        ],
        consecutive=1,
    )
    ok, msg = checks.storage.check_longhorn_volumes(cfg, src)
    assert ok, msg


def test_longhorn_pages_on_a_state_it_has_never_heard_of(cfg):
    # The `degraded|faulted` selector this replaced read a renamed or new Longhorn state as
    # green, while the runbook gates refused the same volume (#3668).
    cfg, src = _arm_longhorn(
        cfg, [_longhorn_series("n8n-data", "rebuilding")], consecutive=1
    )
    ok, msg = checks.storage.check_longhorn_volumes(cfg, src)
    assert not ok
    assert "n8n-data=rebuilding" in msg


def test_longhorn_selects_on_the_state_label_not_a_value_ordinal(cfg):
    # longhorn_volume_robustness is ONE-HOT over `state` with value 0/1. An earlier proposal
    # for this arm compared the value to 2 ("degraded"), which no series ever equals. Pin the
    # `== 1` selector, which keeps each volume's live state and reads it off the label.
    src = _longhorn_sources([])

    checks.storage.check_longhorn_volumes(cfg, src)

    assert src.queries("prom_vector") == ["longhorn_volume_robustness == 1"]


# ── host-coverage floor (HOST_ORIGINS_MIN) ────────────────────────────────────────────────────
# THE BUG THESE PIN: check_disk/check_mem group by origin but, without the floor, fail only on a
# WHOLLY empty vector, so when daniel-box's node-exporter is unreachable both checks evaluate over
# daniel-server alone and push OK — daniel-box's memory and /boot go unwatched behind two green
# tiles. Scrape Targets cannot stand in for this: node-exporter's normal failure
# mode is per-collector, which leaves `up == 1` and that check green.
