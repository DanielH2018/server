"""Cluster storage checks for monitor-bridge — Longhorn volume redundancy and PVC fullness.

The Backblaze B2 and Cloudflare R2 checks that shared this module until 2026-09-01 are in
checks_b2.py and checks_r2.py, mirroring their test files. Reads config as `cfg.X`, the fetch
layer as `bridge_io.X` and the shared streak counter as `bridge_streaks.X`, so the tests'
patches on those modules reach it. Rule and enforcement: bridge_config.py's header.
"""

import bridge_config as cfg
import bridge_io
import bridge_streaks


def check_longhorn_volumes():
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
    volumes = bridge_io.prom_scalar(
        'count(longhorn_volume_robustness{state="healthy"})'
    )
    if not volumes:
        bridge_streaks._down_streaks["longhorn"], ok, msg = bridge_streaks.down_streak(
            bridge_streaks._down_streaks.get("longhorn", 0),
            cfg.LONGHORN_CONSECUTIVE,
            "no longhorn_volume_robustness series — replica redundancy is UNMONITORED "
            "(job=longhorn scrape down?), which is not the same as healthy",
            "scrape gap grace",
        )
        return ok, msg
    worst = {}
    for labels, _value in bridge_io.prom_vector(
        'longhorn_volume_robustness{state=~"degraded|faulted"} == 1'
    ):
        name = labels.get("pvc") or labels.get("volume", "?")
        state = labels.get("state", "?")
        # faulted outranks degraded if both ever report for one volume
        if worst.get(name) != "faulted":
            worst[name] = state
    if not worst:
        bridge_streaks._down_streaks["longhorn"] = 0
        return True, "%d volume(s) redundant, none degraded or faulted" % int(volumes)
    faulted = sorted(n for n, s in worst.items() if s == "faulted")
    degraded = sorted(n for n, s in worst.items() if s != "faulted")
    parts = []
    if faulted:
        parts.append("%d faulted (%s)" % (len(faulted), ", ".join(faulted[:5])))
    if degraded:
        parts.append(
            "%d degraded, single-copy (%s)" % (len(degraded), ", ".join(degraded[:5]))
        )
    bridge_streaks._down_streaks["longhorn"], ok, msg = bridge_streaks.down_streak(
        bridge_streaks._down_streaks.get("longhorn", 0),
        cfg.LONGHORN_CONSECUTIVE,
        "of %d volume(s): %s" % (int(volumes), "; ".join(parts)),
        "drain/reboot grace",
    )
    return ok, msg


def check_pvc_fullness():
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
    claims = bridge_io.prom_scalar(
        "count(count by (namespace, persistentvolumeclaim)"
        " (kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    vec = bridge_io.prom_vector(
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
    if not watched:
        # A DIFFERENT fault from a thin claim census below, and it must not reach the `worst`
        # report: the ratio query returned nothing at all, which looks exactly like "no claim is
        # full" and is not the same fact.
        bridge_streaks._down_streaks["pvc_fullness"], ok, msg = (
            bridge_streaks.down_streak(
                bridge_streaks._down_streaks.get("pvc_fullness", 0),
                cfg.PVC_CLAIMS_CONSECUTIVE,
                "no PVC reported a fullness ratio — PVC fullness is UNKNOWN, not OK",
                "kubelet scrape gap grace",
            )
        )
        return ok, msg
    # Fullest first, so a truncated message names the claims closest to failing.
    breaching = [
        "%s/%s %.0f%%" % (ns, pvc, pct)
        for pvc, ns, pct in sorted(watched, key=lambda w: w[2], reverse=True)
        if pct > cfg.PVC_MAX_PCT
    ]
    # The floor is the input assertion, and it is evaluated over ALL claims including the
    # excluded ones: it asserts the metric family is being scraped, which is a different
    # question from which claims this arm judges.
    shortfall = None
    if claims is None or claims < cfg.PVC_MIN_CLAIMS:
        seen = "no" if claims is None else "only %d" % int(claims)
        bridge_streaks._down_streaks["pvc_fullness"], short_ok, short_msg = (
            bridge_streaks.down_streak(
                bridge_streaks._down_streaks.get("pvc_fullness", 0),
                cfg.PVC_CLAIMS_CONSECUTIVE,
                "%s kubelet_volume_stats claims visible, below the floor of %d — PVC fullness is "
                "UNKNOWN, not OK" % (seen, cfg.PVC_MIN_CLAIMS),
                "kubelet scrape gap grace",
            )
        )
        shortfall = (short_ok, short_msg)
    else:
        bridge_streaks._down_streaks["pvc_fullness"] = 0
    # A claim that IS reporting and IS full outranks a complaint about the ones that are not —
    # same ordering as check_disk, and for the same reason.
    if breaching:
        return False, "PVC over %.0f%%: %s" % (
            cfg.PVC_MAX_PCT,
            ", ".join(breaching[:5]),
        )
    if shortfall is not None:
        return shortfall
    worst = max(watched, key=lambda w: w[2])
    return True, "%d claim(s) under %.0f%%, worst %s/%s %.0f%%" % (
        len(watched),
        cfg.PVC_MAX_PCT,
        worst[1],
        worst[0],
        worst[2],
    )
