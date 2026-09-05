"""Cluster storage checks for monitor-bridge — Longhorn volume redundancy and PVC fullness.

The Backblaze B2 and Cloudflare R2 checks that shared this module until 2026-09-01 are in
checks/b2.py and checks/r2.py, mirroring their test files. Reads config as `cfg.X`, the fetch
layer as `bridge.net.X` and the shared streak counter as `bridge.streaks.X`, so the tests'
patches on those modules reach it. Rule and enforcement: bridge/config.py's header.
"""

from bridge.config import Config
import bridge.net
import bridge.streaks
from verdicts.storage import (
    longhorn_offenders,
    longhorn_redundancy_verdict,
    pvc_fullness_verdict,
)


def check_kubelet_plugin_readonly(cfg: Config) -> tuple[bool, str]:
    """CSI global-mount filesystems ext4 remounted read-only, named by host and mountpoint.

    #1243: a reclaim stall dropped Longhorn's iSCSI sessions, and the replacement_timeout expiry
    that followed aborted several ext4 journals and remounted them read-only. All of it happened
    behind Volume CRs that read `attached healthy` throughout, Ready pods, and 50 minutes of
    silence from every existing monitor — Longhorn's own state is structurally blind to a
    filesystem-level fault under it. `node_filesystem_readonly` already carries this the instant
    it happens; the only reason it went unwatched is the node-exporter daemonset excluding all of
    var/lib/kubelet (fixed alongside this check, daemonset.yaml.j2), which hid the CSI global
    mounts under var/lib/kubelet/plugins/ that this check now scopes to.

    No grace/hysteresis, deliberately, matching check_pvc_fullness's fullness-breach arm: a
    read-only remount does not self-heal like a Longhorn replica rebuild or a kubelet restart
    does, so holding it through a streak only delays a real page. The schedule is fast enough
    that a streak would buy nothing anyway — node-exporter's `node` job scrapes at 1m
    (claude-otel/templates/prometheus.yaml.j2) and this check runs on the bridge's own
    INTERVAL-second cadence (300s deployed), so a fault is visible on the very first cycle
    that follows it, not several scrapes later.

    host_metric_sel (not origin_sel) because this is a node_filesystem_* family: PROM_ORIGIN
    resolves to `origin="daniel-server"` in the deployed env, and pinning would hide the same
    fault on daniel-box behind a green tile — the exact mistake HOST_ORIGINS_MIN was added to
    stop for check_disk/check_mem. An absent series is genuinely healthy here (no CSI global
    mount is read-only), unlike the Longhorn/PVC arms above: node-exporter being entirely down
    is check_targets_down's/check_disk's job, not this one's.
    """
    sel = bridge.net.host_metric_sel(cfg, 'mountpoint=~"/var/lib/kubelet/plugins/.*"')
    vec = bridge.net.prom_vector(cfg, "node_filesystem_readonly%s == 1" % sel)
    if not vec:
        return True, "no read-only CSI global mounts"
    offenders = sorted(
        "%s %s" % (bridge.net._origin_name(labels), labels.get("mountpoint", "?"))
        for labels, _ in vec
    )
    return False, "%d CSI global mount(s) read-only: %s" % (
        len(offenders),
        ", ".join(offenders[:5]),
    )


def check_longhorn_volumes(cfg: Config) -> tuple[bool, str]:
    """Longhorn volumes that have lost replica redundancy, named by PVC.

    `k3s_longhorn_replica_count` is 2, so a volume reading `degraded` is down to a single copy —
    still serving, one more failure from data loss — and `faulted` means no healthy replica is
    left at all. Nothing else in CHECKS covers this: k8s_workloads watches the pod, not the
    volume under it, and the backup-plane cron watches Backup objects rather than live replica
    state. Until this arm landed (2026-08-17) replica loss was silent.

    `longhorn_volume_robustness` is ONE-HOT over a `state` label (healthy/degraded/faulted/
    unknown) with value 0 or 1 — four series per volume. So this selects on the label and never
    compares the value to a state ordinal, which is the mistake an earlier proposal made.
    `unknown` means detached and is deliberately not a fault: 6 of 43 volumes read it here,
    including the intentionally scaled-to-zero game servers.

    The two longhorn-manager pods report DISJOINT volume subsets (43 volumes total across both,
    not 43 each), so offenders are deduped by name rather than counted — a raw count would
    change meaning the moment a volume moved between managers.

    An absent metric is treated as a breach, NOT as green. If the longhorn scrape job dies the
    degraded selector returns empty for exactly the same reason a healthy cluster does, and a
    check that cannot distinguish "nothing is wrong" from "I cannot see" is the failure mode
    this estate keeps rediscovering (manifest-prune's unreadable staged dirs, the backup
    reaper's unpopulated owner map). The volume count doubles as that input assertion: the
    one-hot shape guarantees a `state="healthy"` series per volume even when its value is 0.
    """
    volumes = bridge.net.prom_scalar(
        cfg, 'count(longhorn_volume_robustness{state="healthy"})'
    )
    # Only fetch the offender vector when the census says the job is answering: with no series
    # at all the verdict is already decided, and a second query would spend a request to learn
    # the same thing.
    offenders = (
        longhorn_offenders(
            bridge.net.prom_vector(
                cfg, 'longhorn_volume_robustness{state=~"degraded|faulted"} == 1'
            )
        )
        if volumes
        else {}
    )
    ok, msg, grace = longhorn_redundancy_verdict(volumes, offenders)
    if ok:
        bridge.streaks._down_streaks["longhorn"] = 0
        return ok, msg
    bridge.streaks._down_streaks["longhorn"], ok, msg = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("longhorn", 0),
        cfg.LONGHORN_CONSECUTIVE,
        msg,
        grace,
    )
    return ok, msg


def check_pvc_fullness(cfg: Config) -> tuple[bool, str]:
    """Filesystem fullness of every PersistentVolumeClaim the kubelet reports stats for.

    Nothing else covered this. check_disk iterates DISK_MOUNTPOINTS — `/`, `/boot`, `/boot/efi`
    — which are host filesystems, and check_longhorn_volumes reads longhorn_volume_robustness,
    which is replica redundancy rather than space. A Longhorn PVC has its own filesystem at a
    fixed capacity, so a 2 Gi claim can reach 100% while both hosts report hundreds of GB free:
    the app starts failing writes and every existing monitor stays green.

    Reads the CLUSTER Prometheus like check_k8s_workloads, so it belongs to CLUSTER_DEPENDENT
    rather than PROM_DEPENDENT — the gate has to be the one watching this check's own source.

    `max by (namespace, persistentvolumeclaim)` — not `sum` or `avg` — because daniel-box's
    claims are scraped TWICE. k3s serves the kubelet's metric registry on the supervisor's
    /metrics as well, so the same series arrives under job="kubernetes-kubelet" and under
    job="kubernetes-apiserver" (measured 2026-09-01: 43 + 27 = 70 series over 43 claims).
    `max` of two copies of one ratio is that ratio, so the double scrape is harmless; `sum`
    would report a double-scraped claim at twice its real fullness and `avg` would silently
    change meaning the day one job's coverage moved. Grouping is also what makes the count
    below a claim census rather than a scrape-job artifact.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "PVC fullness check disabled (no CLUSTER_PROMETHEUS_URL)"
    claims = bridge.net.prom_scalar(
        cfg,
        "count(count by (namespace, persistentvolumeclaim)"
        " (kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    vec = bridge.net.prom_vector(
        cfg,
        "max by (namespace, persistentvolumeclaim) (100 *"
        " (1 - kubelet_volume_stats_available_bytes"
        " / kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    watched = [
        (labels.get("persistentvolumeclaim", "?"), labels.get("namespace", "?"), pct)
        for labels, pct in vec
        if labels.get("persistentvolumeclaim") not in cfg.PVC_EXCLUDE
    ]
    breach_msg, census_msg, summary = pvc_fullness_verdict(
        watched, claims, cfg.PVC_MAX_PCT, cfg.PVC_MIN_CLAIMS
    )
    # The census arm rides the streak; a fullness breach does not, because it is monotonic
    # rather than flappy. Both are advanced BEFORE the breach is reported, so a cycle that is
    # simultaneously blind and full still moves the census streak.
    graced = None
    if census_msg:
        bridge.streaks._down_streaks["pvc_fullness"], census_ok, census_msg = (
            bridge.streaks.down_streak(
                bridge.streaks._down_streaks.get("pvc_fullness", 0),
                cfg.PVC_CLAIMS_CONSECUTIVE,
                census_msg,
                "kubelet scrape gap grace",
            )
        )
        graced = (census_ok, census_msg)
    else:
        bridge.streaks._down_streaks["pvc_fullness"] = 0
    if breach_msg:
        return False, breach_msg
    if graced is not None:
        return graced
    return True, summary
