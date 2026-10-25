"""Embedded etcd's database size against its backend quota, read through the apiserver.

etcd's own series — `etcd_mvcc_db_total_size_in_bytes` and the WAL fsync histograms — are
empty here, because `k3s_etcd_expose_metrics` is off and the comment at that switch
(group_vars/all.yml) records why it stays off. `apiserver_storage_size_bytes` is the proxy that
is scraped unconditionally, and #2403 reports it matching the etcd snapshot's size. The DB
filling its quota is the failure the restore runbook exists for: at the quota etcd goes
read-only and the whole control plane stops accepting writes.

Its own module for the reason `cluster_zero.py` is one: `checks/cluster.py` sits near the
600-line cap the module-length ratchet enforces. Config as `cfg.X`, the query through
the `src` argument (`bridge.sources.Sources`), the same layering as its neighbours.
"""

from bridge.config import Config
from bridge.sources import Sources


def check_etcd_db_size(cfg: Config, src: Sources) -> tuple[bool, str]:
    """etcd's DB size as a percentage of ETCD_DB_QUOTA_BYTES, down over ETCD_DB_MAX_PCT.

    Args:
      cfg: The bridge's configuration; reads PROM_URL and the two ETCD_DB_* fields.
      src: The query source; a test hands in a `FakeSources` that states the reading it means.

    `max(...)` over the bare series, not a `by` grouping: k3s runs the apiserver and the
    kubelet in one process, so until #6331 `apiserver_storage_size_bytes` was scraped TWICE,
    under `job="kubernetes-apiserver"` and `job="kubernetes-kubelet"`, with the same value
    (measured 2026-09-25: both read 56,389,632 for `storage_cluster_id="etcd-0"`). The kubelet
    job now drops the server node's `apiserver_` families, so one series is left, and `max`
    keeps `result[0]` well defined if a second copy returns. Selecting on `job` would tie this
    check to a scrape-job name instead of to the metric.

    An absent series is UP, which is the opposite of check_pvc_fullness's fail-closed arm, and
    the discriminator is partial coverage. There, volume stats come from one kubelet target per
    node over 43 claims, so one dead node target still leaves the other node's claims
    answering, and silence hides a real gap. Here one series comes from the apiserver job alone,
    so an empty vector means the apiserver is not being scraped at all — and paging a second
    monitor for that one root cause is what the gate sets exist to prevent. The compensating control was checked rather than assumed:
    `verdicts.cluster.targets_verdict` pages on ANY series with `up == 0`, not on a count floor
    alone, and `check_cluster_targets` selects `up{origin!="daniel-server"}`, which matches the
    apiserver target (it carries no `origin` label, and PromQL reads an absent label as empty).
    A 403 on that scrape — the failure prometheus.yaml.j2's RBAC note warns about — sets
    `up = 0` there, so it pages on Cluster Scrape Targets rather than going silent.

    No streak. A DB at 80% of its quota does not self-heal the way a Longhorn replica rebuild
    or a Recreate rollout does, so holding the verdict through consecutive cycles would only
    delay a real page — the same reasoning check_kubelet_plugin_readonly documents.
    """
    used = src.prom_scalar("max(apiserver_storage_size_bytes)")
    if used is None:
        return True, "apiserver_storage_size_bytes absent — see Scrape Targets"
    quota = cfg.ETCD_DB_QUOTA_BYTES
    pct = 100.0 * used / quota
    sizes = "%.1f MiB of the %.1f GiB quota" % (
        used / 1024**2,
        quota / 1024**3,
    )
    if pct >= cfg.ETCD_DB_MAX_PCT:
        return False, "etcd DB %.1f%% full: %s. At the quota etcd goes read-only" % (
            pct,
            sizes,
        )
    return True, "etcd DB %.1f%% of quota (%s)" % (pct, sizes)
