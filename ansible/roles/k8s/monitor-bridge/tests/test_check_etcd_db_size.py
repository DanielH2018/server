"""check_etcd_db_size — #2403's proxy for the etcd series k3s does not expose.

`k3s_etcd_expose_metrics` is off, so `etcd_mvcc_db_total_size_in_bytes` returns nothing and the
DB filling its backend quota was unwatched. `apiserver_storage_size_bytes` is scraped
unconditionally and carries the same number. At the quota etcd rejects every write and the
control plane stops, so the verdict pages rather than warns.

Two facts the query depends on, both measured against the live cluster Prometheus 2026-09-25:
k3s scrapes that series under BOTH `job="kubernetes-apiserver"` and `job="kubernetes-kubelet"`
(one process serves both endpoints), and it read 56,389,632 — 2.6% of the 2 GiB default quota.

Every test here hands the check its `fetch` seam rather than patching `bridge.net`, so each one
states the reading it means.
"""

from dataclasses import replace

import checks.cluster_etcd
import gates

QUOTA = 2 * 1024**3


def _reads(value):
    """A `fetch` that answers every query with `value`."""
    return lambda *a, **k: value


def _at(cfg, used, quota=QUOTA):
    return checks.cluster_etcd.check_etcd_db_size(
        replace(cfg, ETCD_DB_QUOTA_BYTES=quota), fetch=_reads(used)
    )


def test_the_check_is_cluster_dependent():
    assert "etcd_db_size" in gates.CLUSTER_DEPENDENT, (
        "prom_scalar raises on an unreachable cluster Prometheus and _evaluate turns that into "
        "a down — without this gate a Prometheus outage pages this monitor a second time for "
        "the one root cause the cluster_prometheus monitor already reports"
    )
    assert "etcd_db_size" not in gates.PROM_DEPENDENT, (
        "the check reads CLUSTER_PROM_URL, so its gate is cluster_prometheus; a wrong entry "
        "suppresses on the wrong outage"
    )


def test_a_db_well_under_the_quota_is_up(cfg):
    """The live reading: 2.6% of 2 GiB, flat for five weeks before the check existed."""
    ok, msg = _at(cfg, 56389632.0)
    assert ok
    assert "2.6%" in msg


def test_a_db_over_the_threshold_pages_naming_the_percentage(cfg):
    ok, msg = _at(cfg, 0.85 * QUOTA)
    assert not ok
    assert "85.0%" in msg
    assert "read-only" in msg


def test_the_threshold_itself_is_down_not_up(cfg):
    """`>=`, not `>`: a DB sitting exactly on the threshold is already the thing watched."""
    assert not _at(cfg, 0.80 * QUOTA)[0]


def test_a_breach_gets_no_grace_and_pages_on_the_first_cycle(cfg):
    # THE BUG THIS PINS: a consecutive-down streak here would delay the page without making it
    # less likely. A DB near its quota does not shrink on its own the way a Longhorn replica
    # rebuild or a Recreate rollout resolves itself.
    assert not _at(cfg, 0.9 * QUOTA)[0]
    assert not _at(cfg, 0.9 * QUOTA)[0]


def test_an_absent_series_is_up_and_points_at_scrape_targets(cfg):
    """Fail OPEN, unlike check_pvc_fullness's fail-closed arm, because coverage is all-or-nothing.

    The PVC arm pages on an empty vector because 27 of 43 claims survive a dead kubelet job, so
    silence there hides a partial gap. One series carried by two jobs has no partial case: empty
    means the apiserver is not scraped at all, which `targets` and `cluster_targets` page on.
    """
    ok, msg = _at(cfg, None)
    assert ok
    assert "Scrape Targets" in msg


def test_the_query_collapses_the_two_scrape_jobs_to_one_value(cfg):
    """`max(...)` over the bare series, so neither job name nor series order decides the value.

    k3s serves the apiserver and the kubelet from one process, so the series is scraped twice
    with the same value. A query selecting on `job` would tie the check to a scrape-job name;
    handing two series to prom_scalar would make `result[0]` an arbitrary pick.
    """
    seen = {}

    def _spy(_cfg, promql, base=None, source="prometheus"):
        seen.update(promql=promql, base=base)
        return 1.0

    checks.cluster_etcd.check_etcd_db_size(cfg, fetch=_spy)
    assert seen["promql"] == "max(apiserver_storage_size_bytes)"
    assert "job=" not in seen["promql"]
    assert seen["base"] == cfg.CLUSTER_PROM_URL


def test_the_in_code_quota_default_is_etcds_own(cfg):
    """The fallback an unset `ETCD_DB_QUOTA_BYTES` leaves the check on is etcd's own 2 GiB.

    This pins the DEFAULT only. Whether the deployed value still matches the quota the cluster
    actually runs is the two-sided pin in
    `ansible/tests/setup/test_etcd_quota_and_its_monitor_agree.py`, which reads
    `k3s_server_args` as well — this suite cannot, having no repo root.
    """
    assert cfg.ETCD_DB_QUOTA_BYTES == QUOTA
