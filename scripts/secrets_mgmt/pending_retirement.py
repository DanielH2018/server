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
        # M6 batch A1's Cutover-a1 deleted the Root Disk, Memory, Host Temperature and UPS
        # Battery Health tiles, and Cutover-a2 deleted their bridge rows. A1's token PR retires
        # the keys a week after Cutover-b and deletes these four entries with them.
        "monitor_bridge_disk_push_token",
        "monitor_bridge_mem_push_token",
        "monitor_bridge_host_temp_push_token",
        "monitor_bridge_ups_push_token",
        # M6 batch B8's Cutover-a1 deleted the Pi Pressure tile, and Cutover-a2 deleted its
        # bridge row. B8's token PR retires the key a week after Cutover-b and deletes this
        # entry with it.
        "monitor_bridge_pi_push_token",
        # M6 batch A7's Cutover-a1 deleted the Workload Log Errors tile, and Cutover-a2 deleted
        # its bridge row. A7's token PR retires the key a week after Cutover-b and deletes this
        # entry with it.
        "monitor_bridge_k8s_workloads_push_token",
        # M6 batch B5's Cutover-1a deleted the Daniel Pi SD Health and Daniel Pi Recovery tiles,
        # and Cutover-2 drops the Pi package's two pushes. The Pi's host SOPS file and the
        # package's `credentials:` keep both keys until B5's token PR, which retires them a week
        # after Cutover-1b and deletes these two entries with them.
        "pi_sd_health_push_token",
        "pi_recovery_push_token",
        # M6 batch B6a's Cutover-1a deleted the Artifacts Peer Sync and k3s Telemetry OTLP Door
        # tiles. The two host crons keep pushing into the deleted tokens until Cutover-2 drops the
        # push. B6a's token PR retires the keys a week after Cutover-1b and deletes these two
        # entries with them.
        "artifacts_sync_push_token",
        "claude_otel_push_token",
    }
)
