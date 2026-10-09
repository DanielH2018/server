"""Every check and gate monitor-bridge runs, and the Kuma push tile each one feeds (#3659).

This table is the one place a check is declared. Everything else derives from it:

- `registry.build_checks` runs every row without `is_gate`, in table order.
- `gates.py` builds `PROM_DEPENDENT`, `LOKI_DEPENDENT`, `B2_DEPENDENT`, `WAN_DEPENDENT` and
  `STARTUP_GRACE` from the `gate` column, and `GATE_DEPENDENTS` from the `is_gate` rows.
  `EXPORTER_DEPENDENT` stays in `gates.py`: it is keyed by scrape job, and one check can sit
  under two jobs.
- `templates/env-secret.yaml.j2` renders one `KUMA_PUSH_<NAME>` per row, the env var
  `bridge.types.push_env(name)` reads.
- `k8s/uptime-kuma/templates/static-monitors.yaml.j2` renders one push tile per row.
- `k8s/uptime-kuma/templates/status-page-sync-configmap.yaml.j2` pins each tile into the status
  page group its `status_group` names.
- `probe.py kuma-drift` expands the same rows when it reads that template.

The two templates read this file through `ansible/filter_plugins/py_table.py`, which parses it
without running it. That is why every field but `fn` is a literal, and why this module imports
only check bodies.

Adding a check takes one row here, one `check_*` body, and its push-token secret
(`/add-secret`, which also runs `secret_rotation.py sync`).

DECIDED: `token` is spelled out on every row rather than derived from `name`.
scripts/secrets_mgmt/consumers.py finds who to redeploy after a rotation by grepping each
role's files for the secret's name, so a derived name would drop monitor-bridge from that census
and leave the pod pushing a revoked token. uptime-kuma renders the same tokens from this file;
the census credits it through the cross-role read `deploy_changes.cross_role_readers` derives.
Ten rows predate the `monitor_bridge_<name>_push_token` rule and keep their old name, because
renaming a secret is a rotation.

`kuma_id`, `display` and `description` are what AutoKuma compares, and it runs with
`ON_DELETE=delete` keyed by id, so changing an id deletes the live monitor and its history.

Stdlib only, like every module under files/.
"""

from bridge.types import PushCheck
from checks import (
    b2,
    cloudflare_ips,
    cluster,
    cluster_etcd,
    cluster_traefik,
    gitops,
    healthchecks,
    host,
    host_edge,
    host_thermal,
    logs,
    notify,
    r2,
    service,
    storage,
    wan,
)

# One row per three lines rather than ruff's one argument per line, which ran the table past
# the 600-line module cap with room for three more checks.
# fmt: off
CHECKS: tuple[PushCheck, ...] = (
    PushCheck(
        name="disk", fn=host.check_disk, token="monitor_bridge_disk_push_token",
        kuma_id="monitor-bridge-disk", display="Root Disk", status_group="Hosts & Power", gate="prometheus", critical=True,
        description="node-exporter via monitor-bridge: `/`, `/boot` or `/boot/efi` over DISK_MAX_PCT on a cluster node, or one node's exporter missing from the census. `df -h` on the named host.",
    ),
    PushCheck(
        name="cert", fn=host.check_cert, token="monitor_bridge_cert_push_token",
        kuma_id="monitor-bridge-cert", display="TLS Cert Expiry", status_group="Hosts & Power", gate="prometheus", critical=True,
        description="monitor-bridge reads traefik_tls_certs_not_after from Prometheus every 5 min. DOWN: a certificate Traefik serves expires within CERT_MIN_DAYS; the message names it. Check Traefik's ACME log and `probe.py cert <host>` for the served chain.",
    ),
    PushCheck(
        name="memory", fn=host.check_mem, token="monitor_bridge_mem_push_token",
        kuma_id="monitor-bridge-mem", display="Memory", status_group="Hosts & Power", gate="prometheus",
        description="node-exporter via monitor-bridge: host memory pressure on a cluster node, a Claude Code cgroup stalling or OOM-killing, or one node missing from the census. The message names host and cgroup.",
    ),
    # restarts, oom and cpu re-armed 2026-08-14 (Phase G) against kubernetes-cadvisor, grouped by
    # pod, on their Docker-era tokens. They add OOM and throttle depth to k8s_workloads' crashloops.
    PushCheck(
        name="restarts", fn=cluster.check_restarts, token="monitor_bridge_restarts_push_token",
        kuma_id="k3s-container-restarts", display="k3s Container Restarts", status_group="Cluster Health", gate="prometheus",
        description="cAdvisor via monitor-bridge: a container restarted more than RESTART_MAX times in 15m. The message names it. `kubectl -n homelab describe pod` for the last state.",
    ),
    PushCheck(
        name="oom", fn=cluster.check_oom, token="monitor_bridge_oom_push_token",
        kuma_id="k3s-container-oom", display="k3s Container OOM", status_group="Cluster Health", gate="prometheus",
        description="cAdvisor via monitor-bridge: a container was OOM-killed in the last hour. The message names it; the tile clears one hour after the kill, so read the Discord alert, not the tile, for the evidence.",
    ),
    PushCheck(
        name="cpu", fn=cluster.check_cpu_throttle, token="monitor_bridge_cpu_push_token",
        kuma_id="k3s-cpu-throttling", display="k3s CPU Throttling", status_group="Cluster Health", gate="prometheus",
        description="cAdvisor via monitor-bridge: a container pinned at its CPU limit for 3 cycles (~15m). Raise the limit in its role or accept it; short bursts never page.",
    ),
    PushCheck(
        name="targets", fn=cluster.check_targets_down, token="monitor_bridge_targets_push_token",
        kuma_id="monitor-bridge-targets", display="Scrape Targets", status_group="Observability", gate="prometheus",
        description="monitor-bridge reads `up == 0` on daniel-server's scrape jobs every 5 min. DOWN names the dead job. An exporter that is up but missing a collector is not caught here; that is Root Disk's and Memory's census floor.",
    ),
    PushCheck(
        name="traefik5xx", fn=cluster.check_traefik_5xx, token="monitor_bridge_traefik_push_token",
        kuma_id="monitor-bridge-traefik", display="Traefik 5xx", status_group="Observability", gate="prometheus",
        description="monitor-bridge: a backend's 5xx share over 5 min per Traefik service, behind a TRAEFIK_MIN_RPS volume floor. DOWN names the erroring service. Loki `{container=\"traefik\"}` has the status codes; the backend's own log has the cause.",
    ),
    # Gated since #2778: prom_vector raises on a Prometheus outage, so ungated it co-fired with
    # the `prometheus` gate, one root cause paging twice.
    PushCheck(
        name="traefik_latency", fn=cluster.check_traefik_latency, token="monitor_bridge_traefik_latency_push_token",
        kuma_id="monitor-bridge-traefik-latency", display="Traefik Latency", status_group="Observability", gate="prometheus",
        description="monitor-bridge: share of a service's requests slower than the 5.0s histogram bucket, behind TRAEFIK_MIN_RPS and a 3-slow-request floor; long-lived-connection services (headlamp, HA, Kuma) are exempt. DOWN names the slow backend, not the edge.",
    ),
    # #1322: a total-404 edge erases the traefik_service_* series the two rows above iterate, so
    # both read green through a 3.5-hour outage. This reads the entrypoint counter, which survives.
    PushCheck(
        name="traefik_404", fn=cluster.check_traefik_404_flood, token="monitor_bridge_traefik_404_push_token",
        kuma_id="monitor-bridge-traefik-404", display="Traefik 404 Flood", status_group="Observability", gate="prometheus",
        description="monitor-bridge: 404 share of entrypoint traffic over 5 min above 90%, the shape of an edge that lost its routers (a missing CrowdSec plugin, #1322). DOWN means most requests to the edge are 404; check Traefik's startup log for a router or plugin error.",
    ),
    # #2747/#2749: a client wedged on a connection SNICheck pinned wrong gets 421 at the router
    # and never reaches a service, so none of the three Traefik rows above counts it.
    PushCheck(
        name="traefik_421", fn=cluster_traefik.check_traefik_421, token="monitor_bridge_traefik_421_push_token",
        kuma_id="monitor-bridge-traefik-421", display="Traefik 421", status_group="Observability", gate="prometheus",
        description="monitor-bridge: a Traefik router serving 421 above 0.02 rps for 3 cycles (15 min). A long-lived client connected while Traefik was still reconciling routes and SNICheck pinned the wrong TLS options on that connection; it gets 421 until it redials. DOWN names the router: restart the client that calls it (a Traefik config change does not help). #2747, #2749.",
    ),
    PushCheck(
        name="n8n", fn=service.check_n8n, token="monitor_bridge_n8n_push_token",
        kuma_id="monitor-bridge-n8n", display="n8n Prod Workflows", status_group="Services", gate="startup_grace",
        description="monitor-bridge reads n8n's executions API every 5 min: DOWN when an active workflow failed 3 times in a row, or 2+ workflows are each failing repeatedly. The message names the workflow; open its last execution in n8n.",
    ),
    # Not startup-graced: its own ARR_FETCH_CONSECUTIVE streak covers the reboot transient for
    # longer, and compounding both would only delay a page.
    PushCheck(
        name="arr_queue", fn=service.check_arr_queue, token="monitor_bridge_arr_queue_push_token",
        kuma_id="monitor-bridge-arr-queue", display="Arr Queue Warnings", status_group="Media Automation",
        description="monitor-bridge reads sonarr's and radarr's /api/v3/queue every 5 min. DOWN: an item needs review (warning, importBlocked, or importPending with a status message) — the shape of the 2026-07-01 fake-episode .exe. The message names the release and the app.",
    ),
    PushCheck(
        name="bazarr", fn=service.check_bazarr, token="monitor_bridge_bazarr_push_token",
        kuma_id="monitor-bridge-bazarr", display="Bazarr Health", status_group="Media Automation", gate="startup_grace",
        description="monitor-bridge reads bazarr's system status and health every 5 min. DOWN: bazarr's own copy of the sonarr or radarr API key is rejected (an empty peer version field), or bazarr reports a health issue. Bazarr holds those keys in its UI, so a key rotation misses it.",
    ),
    PushCheck(
        name="prowlarr_indexers", fn=service.check_prowlarr_indexers, token="monitor_bridge_prowlarr_indexers_push_token",
        kuma_id="monitor-bridge-prowlarr-indexers", display="Prowlarr Indexers", status_group="Media Automation", gate="startup_grace",
        description="monitor-bridge reads Prowlarr's indexer status every 5 min. DOWN: an indexer has been failing for a week or more by Prowlarr's own initialFailure; The Pirate Bay and 1337x are ignored as chronic flappers. Short outages never page.",
    ),
    PushCheck(
        name="gitops_alive", fn=gitops.check_gitops_alive, token="monitor_bridge_gitops_alive_push_token",
        kuma_id="gitops-deploy-alive", display="GitOps Deploy — Alive", status_group="Automation & Drift",
        description="The pull-based deployer on daniel-box has not ticked in GITOPS_MAX_AGE_MIN. Check `systemctl status gitops-deploy.timer` and the SessionStart banner's parked state.",
    ),
    PushCheck(
        name="gitops_status", fn=gitops.check_gitops_status, token="monitor_bridge_gitops_status_push_token",
        kuma_id="gitops-deploy-status", display="GitOps Deploy — Status", status_group="Automation & Drift",
        description="The deployer is parked or owes an apply: a held SHA after a failed health gate, a diverged tree, a busy service lock, a tree behind origin for 6h, a setup role only a hand can apply, or an image bump a broad tick deferred for lack of budget. The message names which. `gitops_state.py` clears a marker once the cause is fixed.",
    ),
    # Reads the weekly drill's stamp off a hostPath, fail-closed, every cycle; the staleness window
    # is ETCD_DRILL_MAX_AGE_S, so the tile's interval is the bridge's.
    PushCheck(
        name="etcd_restore_drill", fn=service.check_etcd_restore_drill, token="monitor_bridge_etcd_drill_push_token",
        kuma_id="etcd-restore-drill", display="etcd Restore Drill", status_group="Backups & Storage", runbook="k3s-etcd-restore",
        description="monitor-bridge reads the last-success-list-only stamp written by the weekly (Monday 10:20) etcd restore drill cron on daniel-box. DOWN means no list-only drill has ever passed, the stamp is older than 8 days, or the pod cannot read it. Run journalctl on daniel-box for etcd-restore-drill and check the stamp mode.",
    ),
    # #2403: etcd goes read-only at its backend quota, so this is critical: the fix is an operator
    # compacting or raising the quota. Reads the apiserver's proxy for the db size, because etcd's
    # own series is empty while k3s_etcd_expose_metrics is off. Both the apiserver and kubelet job
    # carry that series, so there is no partial-coverage case and the gate suppresses it whole.
    PushCheck(
        name="etcd_db_size", fn=cluster_etcd.check_etcd_db_size, token="monitor_bridge_etcd_db_size_push_token",
        kuma_id="etcd-db-size", display="etcd DB Size", status_group="Backups & Storage", gate="prometheus", critical=True, runbook="k3s-etcd-restore",
        description="monitor-bridge reads apiserver_storage_size_bytes against etcd's 2 GiB backend quota, because etcd's own db-size series is empty while k3s_etcd_expose_metrics is off. DOWN means the DB is over ETCD_DB_MAX_PCT of the quota; at the quota etcd rejects every write and the control plane stops. Compact and defragment, or raise --etcd-arg=quota-backend-bytes.",
    ),
    PushCheck(
        name="scrutiny", fn=host_thermal.check_scrutiny, token="monitor_bridge_scrutiny_push_token",
        kuma_id="monitor-bridge-scrutiny", display="SMART Data / Health", status_group="Hosts & Power", gate="startup_grace", critical=True,
        description="monitor-bridge reads Scrutiny's summary every 5 min. DOWN: a drive's collector data is older than 26h, its SMART self-assessment or an attribute threshold failed, or NVMe wear passed 80%. Scrutiny's own notifier is unconfigured, so this is the only drive-failure page.",
    ),
    # HWMON_TEMP_CONSECUTIVE (an hour) holds a boost excursion off the tile, not max_retries.
    # Gated because its empty-vector branch pages on a blind hwmon collector.
    PushCheck(
        name="host_temp", fn=host_thermal.check_host_temp, token="monitor_bridge_host_temp_push_token",
        kuma_id="host-temperature", display="Host Temperature", status_group="Hosts & Power", gate="prometheus",
        description="hwmon via monitor-bridge: a board or CPU sensor over 90% of its declared, rated or fallback limit for an hour, the Pi's undervoltage alarm, or kernel CPU thermal throttling for 15m. The message names the sensor and which limit set the bar.",
    ),
    # Reads HA's Prometheus-scraped UPS battery sensors.
    PushCheck(
        name="ups", fn=host_thermal.check_ups, token="monitor_bridge_ups_push_token",
        kuma_id="monitor-bridge-ups", display="UPS Battery Health", status_group="Hosts & Power", gate="prometheus", critical=True,
        description="NUT via monitor-bridge: mains lost, charge under 50%, runtime under 5 min, or the UPS asking for a battery replacement. Mains loss is judged first and alone.",
    ),
    # Reads the Pi's `node-pi` job since #2004, and its absent-series branch pages, so it is
    # gated; that also took it out of the startup grace, which must stay disjoint from the gates.
    PushCheck(
        name="pi_pressure", fn=host_edge.check_pi_pressure, token="monitor_bridge_pi_push_token",
        kuma_id="monitor-bridge-pi", display="Pi Pressure", status_group="Raspberry Pi", gate="prometheus",
        description="daniel-pi's node-exporter via monitor-bridge: load per core, free memory, SD card or boot partition fullness, or a published container port not listening after a reboot. A dead port needs a container recreate, not a restart.",
    ),
    PushCheck(
        name="ha_heartbeat", fn=service.check_ha_heartbeat, token="monitor_bridge_ha_push_token",
        kuma_id="monitor-bridge-ha", display="Home Assistant Automations", status_group="Services",
        description="HA's scheduler heartbeat helper is stale (HA up, automations not running) or HA banned an IP in the last hour — a ban of a cluster gateway locks out everything behind it. Read /config/ip_bans.yaml on a ban.",
    ),
    # The producer is the bridge re-reading the newest result every cycle, not the 6-hourly run,
    # so the tile's deadline is the bridge's; the check's staleness arm catches a dead scheduler.
    PushCheck(
        name="speedtest", fn=host_edge.check_speedtest, token="monitor_bridge_speedtest_push_token",
        kuma_id="k3s-speedtest", display="k3s Speedtest", status_group="Services", gate="startup_grace",
        description="monitor-bridge reads the newest speedtest-tracker result every 5 min; the app itself runs about every 6 h. DOWN means the last run is older than 8 h, did not complete, or downloaded under 100 Mbps; an unreachable API is tolerated for 2 cycles. Open the speedtest UI and check its scheduler and last result.",
    ),
    PushCheck(
        name="loki_ingestion", fn=logs.check_loki_ingestion, token="monitor_bridge_loki_push_token",
        kuma_id="monitor-bridge-loki", display="Loki Log Ingestion", status_group="Observability", gate="loki_reachable",
        description="One of Loki's three stream arms went silent: the file-tailed syslog/authlog union, the container stream, or daniel-pi's Alloy. Loki itself answers; the shipper does not.",
    ),
    # The token's SOPS name predates the Alloy cutover; renaming a secret is a rotation.
    PushCheck(
        name="shipper_dropped", fn=logs.check_shipper_dropped, token="monitor_bridge_promtail_dropped_push_token",
        kuma_id="monitor-bridge-promtail-dropped", display="Log Shipper Dropped Entries", status_group="Observability", gate="prometheus",
        description="Alloy dropped entries client-side or Loki discarded them server-side, over 3000 in an hour, or the OTel collector failed to export telemetry at all in the last 15 min. `too_far_behind` after a shipper restart is a re-tail, not loss; anything else is throughput or limits.",
    ),
    # #1869: kuma-push-lib.sh logs a lost push and returns 0, so a lost DOWN surfaced only at the
    # tile's deadline, a day late for the daily producers. Reads that log line out of Loki and
    # pages only when a sibling cron's push landed in the same window, or when Kuma itself
    # rejected the push (`by=kuma`): a fleet-wide loss is the edge or host tiles' page.
    PushCheck(
        name="swallowed_verdicts", fn=logs.check_swallowed_verdicts, token="monitor_bridge_swallowed_verdicts_push_token",
        kuma_id="monitor-bridge-swallowed-verdicts", display="Swallowed Push Verdicts", status_group="Observability", gate="loki_reachable",
        description="A host cron pushed status=down and the push failed while a sibling cron's push landed, so the verdict was lost rather than the network. The message names the cron; run it by hand and read its logger lines.",
    ),
    # #1891: Kuma logs `Cannot send notification to <name>` and never retries, and check_discord
    # cannot see a dropped POST. Reads that line out of Loki. Critical, so it pages email too:
    # a page about a dropped Discord send that goes only over Discord is the failure it reports.
    PushCheck(
        name="kuma_notify_failures", fn=logs.check_kuma_notify_failures, token="monitor_bridge_kuma_notify_failures_push_token",
        kuma_id="monitor-bridge-kuma-notify-failures", display="Kuma Notification Delivery", status_group="Observability", gate="loki_reachable", critical=True,
        description="Kuma tried to send a notification and dropped it (`Cannot send notification to`), usually a Discord 429 or 400. Kuma does not retry, so the alert behind it reached nobody — read the tile list for what fired in that window. Also emails.",
    ),
    PushCheck(
        name="discord", fn=notify.check_discord, token="monitor_bridge_discord_push_token",
        kuma_id="monitor-bridge-discord", display="Discord Delivery", status_group="Observability", gate="wan_reachable", critical=True,
        description="One of the five Discord webhooks (Kuma, CrowdSec, gitops-deploy, *arr, healthchecks) no longer resolves, or the Gmail SMTP backstop login failed. Rotate the named webhook in SOPS and redeploy its consumer.",
    ),
    # WAN-gated rather than startup-graced since #2784; the WAN gate covers the reboot transient.
    PushCheck(
        name="r2_usage", fn=r2.check_r2_usage, token="monitor_bridge_r2_usage_push_token",
        kuma_id="monitor-bridge-r2-usage", display="R2 Free Tier Headroom", status_group="Backups & Storage", gate="wan_reachable", critical=True, runbook="longhorn-backup-tiering",
        description="monitor-bridge reads Cloudflare's GraphQL analytics every 30 min: month-to-date R2 storage, Class A and Class B operations and incomplete multipart uploads against the free tier. DOWN past 80% on any arm. Cloudflare offers no cap on R2; this is the only boundary.",
    ),
    # cloudflare_ips gates Traefik's forwardedHeaders.trustedIPs. The bridge caches a success for a
    # day and re-probes a failure every cycle, so the tile's interval is the bridge's.
    PushCheck(
        name="cloudflare_ips_drift", fn=cloudflare_ips.check_cloudflare_ips_drift, token="monitor_bridge_cloudflare_drift_push_token",
        kuma_id="cloudflare-ip-drift", display="Cloudflare IP Drift", status_group="Hosts & Power", gate="wan_reachable",
        description="monitor-bridge fetches the Cloudflare published IPv4 and IPv6 ranges and compares them to cloudflare_ips in group_vars/all.yml, which sets traefik trustedIPs and the netpol allow-list. A match is re-checked daily; a failure every cycle. DOWN means the list drifted or the fetch failed. Update cloudflare_ips and redeploy traefik, netpol-baseline and monitor-bridge.",
    ),
    # #2566. Same cache shape as cloudflare_ips_drift. The `-drift-check` id suffix places the tile
    # in the status page's Automation & Drift group.
    PushCheck(
        name="healthchecks_drift", fn=healthchecks.check_healthchecks_drift, token="monitor_bridge_healthchecks_drift_push_token",
        kuma_id="healthchecks-io-drift-check", display="Healthchecks.io Console Drift", status_group="Automation & Drift", gate="wan_reachable",
        description="monitor-bridge reads the Healthchecks.io console (read-only API key) and compares every off-site dead-man check's schedule type, period or cron, timezone and grace against docs/healthchecks-io-deadman.md. A match is re-checked daily; a failure every cycle. DOWN names each slug and field that differs, or a check missing from the console. Fix the console, or change the doc and monitor_bridge_healthchecks_expected together and redeploy monitor-bridge.",
    ),
    PushCheck(
        name="b2_storage", fn=b2.check_b2_storage, token="monitor_bridge_b2_storage_push_token",
        kuma_id="monitor-bridge-b2-storage", display="B2 Free Tier Headroom", status_group="Backups & Storage", gate="b2_reachable", critical=True, runbook="longhorn-backup-tiering",
        description="monitor-bridge reads B2's bucket usage against the free tier, gated behind B2 Reachable so a cap incident pages once. DOWN: storage past the threshold. A drain is nearly free; a prune pays per deleted backup, so read longhorn-backup-tiering before deleting.",
    ),
    # k8s_workloads, cluster_targets and pvc_fullness sat behind a second gate until #2825 folded
    # it into `prometheus`. Each keeps its own fail-closed floor for "Prometheus answers but
    # kube-state-metrics or the kubelet stats are not scraped", which no gate can see.
    PushCheck(
        name="k8s_workloads", fn=cluster.check_k8s_workloads, token="monitor_bridge_k8s_workloads_push_token",
        kuma_id="monitor-bridge-k8s-workloads", display="k3s Workload Health", status_group="Cluster Health", gate="prometheus",
        description="kube-state-metrics via monitor-bridge: a Deployment or DaemonSet with unavailable replicas (held 3 cycles for a rollout), a crash loop, a stalled rollout, zero available replicas, or a deregistered `devic.es/dri`. The message names the workload. `probe.py health <svc>` for the rollout state; roles/k8s/monitor-bridge/CLAUDE.md.",
    ),
    PushCheck(
        name="cluster_targets", fn=cluster.check_cluster_targets, token="monitor_bridge_cluster_targets_push_token",
        kuma_id="monitor-bridge-cluster-targets", display="Cluster Scrape Targets", status_group="Observability", gate="prometheus",
        description="monitor-bridge reads `up` for every non-daniel-server scrape job via the cluster Prometheus, held 3 cycles so a rolling pod's scrape gap does not page. DOWN names the target that stayed down 15 min, or reports UNKNOWN when the `up` vector emptied.",
    ),
    # Gated because its absent-metric branch pages when the longhorn job dies, so a Prometheus
    # outage would page it a second time.
    PushCheck(
        name="longhorn_volumes", fn=storage.check_longhorn_volumes, token="monitor_bridge_longhorn_volumes_push_token",
        kuma_id="monitor-bridge-longhorn-volumes", display="Longhorn Volume Redundancy", status_group="Backups & Storage", gate="prometheus", critical=True, runbook="longhorn-disaster-recovery",
        description="monitor-bridge reads Longhorn's volume metrics via the cluster Prometheus, held 3 cycles for a replica rebuild. DOWN: a volume is degraded or faulted, or the census emptied. The message names the volume; `longhorn-disaster-recovery` has the revert and rebuild procedures.",
    ),
    # No EXPORTER_DEPENDENT entry on job="kubernetes-kubelet": the claims are scraped under two
    # jobs, so a dead kubelet job is a PARTIAL blindness PVC_MIN_CLAIMS is sized to page on.
    PushCheck(
        name="pvc_fullness", fn=storage.check_pvc_fullness, token="monitor_bridge_pvc_push_token",
        kuma_id="monitor-bridge-pvc-fullness", display="k3s PVC Fullness", status_group="Backups & Storage", gate="prometheus", critical=True, runbook="longhorn-disaster-recovery",
        description="kubelet volume stats via monitor-bridge: a Longhorn PVC over 85%, or a named claim under its declared free-bytes floor. A PVC cannot borrow space — expand the volume in its role.",
    ),
    # #1627: snapshots live in the Longhorn backend, so a volume can reach its snapshotMaxSize while
    # the claim reads empty; a reached cap refuses new snapshots and fails every deploy (#1560).
    PushCheck(
        name="snapshot_headroom", fn=storage.check_snapshot_headroom, token="monitor_bridge_snapshot_headroom_push_token",
        kuma_id="monitor-bridge-snapshot-headroom", display="Longhorn Snapshot Headroom", status_group="Backups & Storage", gate="prometheus", critical=True, runbook="longhorn-disaster-recovery",
        description="monitor-bridge sums a volume's snapshot bytes against the cap declared in SNAPSHOT_CAPS (only jellyfin-config carries one). DOWN: the cap is near, and Longhorn refuses new snapshots at it, which fails every deploy of that service until snapshots are deleted by hand.",
    ),
    # #1243: CSI global mounts remounted ext4 read-only with every monitor silent for 50 minutes.
    PushCheck(
        name="kubelet_plugin_readonly", fn=storage.check_kubelet_plugin_readonly, token="monitor_bridge_kubelet_readonly_push_token",
        kuma_id="monitor-bridge-kubelet-readonly", display="Kubelet CSI Mount Read-Only", status_group="Backups & Storage", gate="prometheus", critical=True, runbook="longhorn-disaster-recovery",
        description="monitor-bridge reads node_filesystem_readonly for /var/lib/kubelet/plugins mounts. DOWN: a Longhorn volume's ext4 remounted read-only after an iSCSI stall (#1243); Longhorn's own state stays healthy through it. Scale the workload to 0, wait for detached, scale back.",
    ),
    # The four reachability gates. A gate has no gate of its own: a tile that cannot go red is
    # not coverage (#2825).
    PushCheck(
        name="prometheus", fn=cluster.check_prometheus, token="monitor_bridge_prometheus_push_token",
        kuma_id="monitor-bridge-prometheus", display="Prometheus Reachable", status_group="Observability", is_gate=True,
        description="monitor-bridge's gate for every Prometheus-backed check: a vector(1) query every 5 min. DOWN: Prometheus itself is unreachable, and every dependent tile is held green with a `skipped` message so this one pages alone. Check the prometheus pod first.",
    ),
    PushCheck(
        name="loki_reachable", fn=logs.check_loki_reachable, token="monitor_bridge_loki_reachable_push_token",
        kuma_id="monitor-bridge-loki-reachable", display="Loki Reachable", status_group="Observability", is_gate=True,
        description="monitor-bridge's gate for the Loki-reading checks: a /loki/api/v1/labels probe every 5 min. DOWN: Loki does not answer, and the dependent tiles are held green with a `skipped` message. Loki up but nothing shipping is Loki Log Ingestion's page.",
    ),
    PushCheck(
        name="b2_reachable", fn=b2.check_b2_reachable, token="monitor_bridge_b2_reachable_push_token",
        kuma_id="monitor-bridge-b2-reachable", display="B2 Reachable", status_group="Backups & Storage", is_gate=True, critical=True, runbook="longhorn-backup-tiering",
        description="monitor-bridge authenticates against Backblaze B2 at most every 30 min; a billed answer is cached, a transport failure retried next cycle. DOWN: B2 refuses the key or the transaction cap is exceeded — read the message before assuming data loss, a cap denial looks the same.",
    ),
    PushCheck(
        name="wan_reachable", fn=wan.check_wan_reachable, token="monitor_bridge_wan_reachable_push_token",
        kuma_id="monitor-bridge-wan-reachable", display="WAN Reachable", status_group="Observability", is_gate=True, critical=True,
        description="monitor-bridge's gate for the internet-reaching checks: two independent providers fetched by hostname every 5 min, DOWN only when neither answers. DOWN means this house has no working link or no DNS, and R2 Free Tier Headroom, Cloudflare IP Drift, Healthchecks.io Console Drift and Discord Delivery are held green with a `skipped` message. B2 Reachable is its peer, not behind it, so a WAN outage lights those two tiles.",
    ),
)
# fmt: on
