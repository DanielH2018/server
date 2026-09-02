"""Longhorn volume redundancy, and the extended-resource advertisement beside it.

Degradation is held for a streak before paging, because a replica rebuild is routine. An absent
metric is never green: the query returns nothing both when every volume is redundant and when
kube-state-metrics is not reporting at all.
"""

import bridge_config
import bridge_io
import bridge_streaks
import checks_storage
import checks_cluster


def _longhorn_series(pvc, state, pod="longhorn-manager-a"):
    return ({"pvc": pvc, "state": state, "pod": pod, "volume": "pvc-" + pvc}, 1.0)


def _arm_longhorn(monkeypatch, vector, volumes=43.0, consecutive=3):
    monkeypatch.setattr(bridge_config, "LONGHORN_CONSECUTIVE", consecutive)
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda *a, **k: volumes)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: vector)


def test_longhorn_all_redundant_is_up_and_reports_the_volume_count(monkeypatch):
    _arm_longhorn(monkeypatch, [])
    ok, msg = checks_storage.check_longhorn_volumes()
    assert ok
    assert "43 volume(s) redundant" in msg


def test_longhorn_degraded_holds_up_until_the_threshold_then_pages(monkeypatch):
    _arm_longhorn(monkeypatch, [_longhorn_series("freshrss-config", "degraded")])
    # A node drain degrades every volume on the departing node by design, so the first
    # cycles must hold `up` — otherwise this monitor pages every Sunday reboot.
    ok1, msg1 = checks_storage.check_longhorn_volumes()
    ok2, _ = checks_storage.check_longhorn_volumes()
    ok3, msg3 = checks_storage.check_longhorn_volumes()
    assert ok1 and ok2
    assert "1/3" in msg1
    assert not ok3
    assert "freshrss-config" in msg3
    assert "single-copy" in msg3


def test_longhorn_recovery_resets_the_streak(monkeypatch):
    _arm_longhorn(monkeypatch, [_longhorn_series("freshrss-config", "degraded")])
    checks_storage.check_longhorn_volumes()
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    assert checks_storage.check_longhorn_volumes()[0]
    assert bridge_streaks._down_streaks.get("longhorn", 0) == 0


def test_longhorn_absent_metric_is_not_green(monkeypatch):
    # The whole point of the arm: an empty degraded-selector looks identical whether the
    # cluster is healthy or the longhorn scrape job is dead. The volume count is the input
    # assertion, so a missing family must fail closed rather than read as "none degraded".
    _arm_longhorn(monkeypatch, [], volumes=None)
    ok1, msg1 = checks_storage.check_longhorn_volumes()
    assert ok1  # first cycle rides the grace, but says why
    assert "UNMONITORED" in msg1
    checks_storage.check_longhorn_volumes()
    ok3, msg3 = checks_storage.check_longhorn_volumes()
    assert not ok3
    assert "not the same as healthy" in msg3


def test_longhorn_dedupes_a_volume_reported_by_both_managers(monkeypatch):
    # The two longhorn-manager pods report disjoint subsets today, but a volume moving
    # between them must not be double-counted into the message.
    _arm_longhorn(
        monkeypatch,
        [
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-a"),
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-b"),
        ],
        consecutive=1,
    )
    ok, msg = checks_storage.check_longhorn_volumes()
    assert not ok
    assert "1 degraded" in msg


def test_longhorn_faulted_outranks_degraded_for_the_same_volume(monkeypatch):
    _arm_longhorn(
        monkeypatch,
        [
            _longhorn_series("valheim-data", "degraded"),
            _longhorn_series("valheim-data", "faulted"),
        ],
        consecutive=1,
    )
    ok, msg = checks_storage.check_longhorn_volumes()
    assert not ok
    assert "1 faulted" in msg
    assert "degraded" not in msg


def test_longhorn_selects_on_the_state_label_not_a_value_ordinal():
    # longhorn_volume_robustness is ONE-HOT over `state` with value 0/1. An earlier proposal
    # for this arm compared the value to 2 ("degraded"), which no series ever equals. Pin the
    # label-based selector so that mistake cannot come back.
    queries = []

    def record(promql, *a, **k):
        queries.append(promql)
        return []

    saved_vector, saved_scalar = bridge_io.prom_vector, bridge_io.prom_scalar
    try:
        bridge_io.prom_vector = record
        bridge_io.prom_scalar = lambda *a, **k: 43.0
        checks_storage.check_longhorn_volumes()
    finally:
        bridge_io.prom_vector, bridge_io.prom_scalar = saved_vector, saved_scalar
    assert len(queries) == 1
    assert 'state=~"degraded|faulted"' in queries[0]
    assert "== 2" not in queries[0]


#
# dri-device-plugin has no probe, and a container without a readinessProbe is Ready the instant it
# starts. So a plugin that wedges internally keeps a Running, Ready, fully-available DaemonSet
# while kubelet deregisters the extended resource - the DaemonSet arm is structurally blind to it,
# and the only other evidence (jellyfin and tdarr unschedulable) does not appear until they next
# reschedule. The repo recorded this omission as "covered by monitor-bridge's check", which was a
# true sentence about a check that reads a different metric.


def test_the_query_uses_the_label_kube_state_metrics_actually_emits():
    """KSM sanitizes the resource name into the label, so the configured name never matches.

    Live on 2026-08-20: both nodes advertised `devic.es/dri: 4`, KSM emitted
    `resource="devic_es_dri"`, and the query for the unsanitised name matched nothing - which this
    check reads as the plugin having deregistered. The monitor went DOWN on a healthy cluster and
    stayed there until the sanitiser landed. The operator-facing name stays the one
    `kubectl describe node` prints; only the query is sanitised.
    """
    assert checks_cluster.ksm_resource_label("devic.es/dri") == "devic_es_dri"
    assert checks_cluster.ksm_resource_label("nvidia.com/gpu") == "nvidia_com_gpu"
    assert checks_cluster.ksm_resource_label("cpu") == "cpu"


def test_missing_extended_resource_names_both_the_resource_and_its_label():
    """A false fault and a real one look identical unless the alert names the label it queried."""
    ok, msg = checks_cluster.extended_resource_verdict(
        ["devic.es/dri"], {"devic.es/dri": 0}, 12
    )
    assert ok is False
    assert "devic.es/dri" in msg
    assert "devic_es_dri" in msg


def test_resource_absent_from_the_map_is_a_fault():
    """An absent key and a zero count mean the same thing: nothing advertises it."""
    ok, _ = checks_cluster.extended_resource_verdict(["devic.es/dri"], {}, 12)
    assert ok is False


def test_advertised_resource_passes_and_reports_node_count():
    ok, msg = checks_cluster.extended_resource_verdict(
        ["devic.es/dri"], {"devic.es/dri": 1}, 12
    )
    assert ok is True
    assert "1 node(s)" in msg


def test_no_series_at_all_is_inert_not_green_and_not_red():
    """The collector not running must not read as health or as fault - it must say so.

    Passing silently would be exactly the "check that cannot read its input answers anyway"
    failure this arm exists to fix. Failing would page for a kube-state-metrics config change
    nobody made. Naming it is the only honest option.
    """
    ok, msg = checks_cluster.extended_resource_verdict(["devic.es/dri"], {}, 0)
    assert ok is True
    assert "INERT" in msg
    assert "devic.es/dri" in msg


def test_the_inert_arm_takes_prom_scalars_real_empty_value():
    """prom_scalar returns None on an empty query, never 0 - so None is what production feeds here.

    Testing only 0 would leave the real call path unexercised: both are falsy today, but the
    fixture would stop matching the producer the moment that branch grew anything sharper than a
    truthiness test.
    """
    ok, msg = checks_cluster.extended_resource_verdict(["devic.es/dri"], {}, None)
    assert ok is True
    assert "INERT" in msg


def test_several_resources_are_all_checked():
    ok, msg = checks_cluster.extended_resource_verdict(
        ["devic.es/dri", "example.com/fpga"],
        {"devic.es/dri": 2, "example.com/fpga": 0},
        12,
    )
    assert ok is False
    assert "example.com/fpga" in msg


def test_nothing_expected_is_trivially_ok():
    ok, _ = checks_cluster.extended_resource_verdict([], {}, 12)
    assert ok is True


# ── host-coverage floor (HOST_ORIGINS_MIN) ────────────────────────────────────────────────────
# THE BUG THESE PIN (2026-08-23): check_disk/check_mem grouped by origin but failed only on a
# WHOLLY empty vector, so when daniel-box's node-exporter became unreachable for 5.4h both checks
# evaluated over daniel-server alone and pushed OK — daniel-box's memory and /boot were unwatched
# behind two green tiles. Scrape Targets cannot stand in for this: node-exporter's normal failure
# mode is per-collector, which leaves `up == 1` and that check green.
