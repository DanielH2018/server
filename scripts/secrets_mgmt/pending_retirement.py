"""Keys whose retirement is scheduled but not yet due.

A leaf: it imports nothing, so both `consumers.py` and `cluster_vars_check.py` can read it
without a cycle.
"""

# Push tokens whose checks a cutover deleted, but whose keys stay in secrets.yml, the rotation
# registry and the cluster file for a week. Operator ruling, 2026-10-22: a token retirement lands
# a week after the cutover that frees it, because a revert of the cutover needs the token and a
# retired key cannot come back by revert. Each name has no consumer until then:
# `consumers.consumer_tags` returns nothing for it, `cluster_vars_check.names_problems` does not
# demand a cluster render for it, and `cluster_vars.write` keeps it in the cluster file (#5960).
# M5's PR 5 retired its three tokens on 2026-10-29. The last retirement deletes this set.
PENDING_RETIREMENT: frozenset[str] = frozenset(
    {
        # M3's PR 5 deleted the `daniel-box Disk` tile and the script's push. Its PR 6 retires
        # the key a week later and deletes this entry in the same commit.
        "daniel_box_disk_push_token",
        # M6 batch A3's Cutover-a1 deleted the Scrape Targets, Cluster Scrape Targets and Log
        # Shipper Dropped Entries tiles, and Cutover-a2 deleted their bridge rows. A3's token PR
        # retires the keys a week after Cutover-b and deletes these three entries with them.
        "monitor_bridge_targets_push_token",
        "monitor_bridge_cluster_targets_push_token",
        "monitor_bridge_promtail_dropped_push_token",
        # M6 batch A4's Cutover-a1 deleted the TLS Cert Expiry, Traefik 5xx, Traefik Latency,
        # Traefik 404 Flood and Traefik 421 tiles, and Cutover-a2 deleted their bridge rows.
        # A4's token PR retires the keys a week after Cutover-b and deletes these five entries
        # with them.
        "monitor_bridge_cert_push_token",
        "monitor_bridge_traefik_push_token",
        "monitor_bridge_traefik_latency_push_token",
        "monitor_bridge_traefik_404_push_token",
        "monitor_bridge_traefik_421_push_token",
        # M6 batch A2's Cutover-a1 deleted the k3s Container Restarts, k3s Container OOM and
        # k3s CPU Throttling tiles, and Cutover-a2 deleted their bridge rows. A2's token PR
        # retires the keys a week after Cutover-b and deletes these three entries with them.
        "monitor_bridge_restarts_push_token",
        "monitor_bridge_oom_push_token",
        "monitor_bridge_cpu_push_token",
        # M6 batch A5's Cutover-a1 deleted the Longhorn Volume Redundancy, k3s PVC Fullness,
        # Longhorn Snapshot Headroom, Kubelet CSI Mount Read-Only and etcd DB Size tiles, and
        # Cutover-a2 deleted their bridge rows. A5's token PR retires the keys a week after
        # Cutover-b and deletes these five entries with them.
        "monitor_bridge_longhorn_volumes_push_token",
        "monitor_bridge_pvc_push_token",
        "monitor_bridge_snapshot_headroom_push_token",
        "monitor_bridge_kubelet_readonly_push_token",
        "monitor_bridge_etcd_db_size_push_token",
    }
)
