"""Every check and gate monitor-bridge runs, and the Kuma push tile each one feeds (#3659).

This table is the one place a check is declared. Everything else derives from it:

- `registry.build_checks` runs every row without `is_gate`, in table order.
- `gates.py` builds `PROM_DEPENDENT`, `B2_DEPENDENT`, `WAN_DEPENDENT` and
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
(`/add-secret`, which also runs `secret_rotation.py sync`). The `regen-doc-fragments` prek hook
rewrites the secret-tiers fragment that sync moves, in the same commit.

DECIDED: `token` is spelled out on every row rather than derived from `name`.
scripts/secrets_mgmt/consumers.py finds who to redeploy after a rotation by grepping each
role's files for the secret's name, so a derived name would drop monitor-bridge from that census
and leave the pod pushing a revoked token. uptime-kuma renders the same tokens from this file;
the census credits it through the cross-role read `deploy_changes.cross_role_readers` derives.
Four rows predate the `monitor_bridge_<name>_push_token` rule and keep their old name, because
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
    gitops,
    healthchecks,
    host_edge,
    host_thermal,
    logs,
    notify,
    r2,
    service,
    wan,
)

# One row per three lines rather than ruff's one argument per line, which ran the table past
# the 600-line module cap with room for three more checks.
# fmt: off
CHECKS: tuple[PushCheck, ...] = (
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
    # A row of its own, not an arm of gitops_status: that check reads the deployer's state
    # directory outside the Prometheus gate, and an arm reading Prometheus there would page on
    # every Prometheus outage. Under EXPORTER_DEPENDENT["node"] in gates.py as well.
    PushCheck(
        name="host_packages", fn=gitops.check_host_packages, token="monitor_bridge_host_packages_push_token",
        kuma_id="host-packages-status", display="Host Packages — Status", status_group="Automation & Drift", gate="prometheus",
        description="The host-package builder on daniel-box has not fetched master in 30 min, or has left a commit unbuilt for 2 h. The message names the builder's `reason` and `sha`: a hold, a refusal, a rewritten master, an open rollout gate or a walk with no green commit. `docs/host-packages.md`, The builder's pages, has the fix for each.",
    ),
    # Reads the weekly drill's stamp off a hostPath, fail-closed, every cycle; the staleness window
    # is ETCD_DRILL_MAX_AGE_S, so the tile's interval is the bridge's.
    PushCheck(
        name="etcd_restore_drill", fn=service.check_etcd_restore_drill, token="monitor_bridge_etcd_drill_push_token",
        kuma_id="etcd-restore-drill", display="etcd Restore Drill", status_group="Backups & Storage", runbook="k3s-etcd-restore",
        description="monitor-bridge reads the last-success-list-only stamp written by the weekly (Monday 10:20) etcd restore drill cron on daniel-box. DOWN means no list-only drill has ever passed, the stamp is older than 8 days, or the pod cannot read it. Run journalctl on daniel-box for etcd-restore-drill and check the stamp mode.",
    ),
    PushCheck(
        name="scrutiny", fn=host_thermal.check_scrutiny, token="monitor_bridge_scrutiny_push_token",
        kuma_id="monitor-bridge-scrutiny", display="SMART Data / Health", status_group="Hosts & Power", gate="startup_grace", critical=True,
        description="monitor-bridge reads Scrutiny's summary every 5 min. DOWN: a drive's collector data is older than 26h, its SMART self-assessment or an attribute threshold failed, or NVMe wear passed 80%. Scrutiny's own notifier is unconfigured, so this is the only drive-failure page.",
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
    # #1869: kuma-push-lib.sh logs a lost push and returns 0, so a lost DOWN surfaced only at the
    # tile's deadline, a day late for the daily producers. Reads that log line out of Loki and
    # pages only when a sibling cron's push landed in the same window, or when Kuma itself
    # rejected the push (`by=kuma`): a fleet-wide loss is the edge or host tiles' page.
    PushCheck(
        name="swallowed_verdicts", fn=logs.check_swallowed_verdicts, token="monitor_bridge_swallowed_verdicts_push_token",
        kuma_id="monitor-bridge-swallowed-verdicts", display="Swallowed Push Verdicts", status_group="Observability", gate="startup_grace",
        description="A host cron pushed status=down and the push failed while a sibling cron's push landed, so the verdict was lost rather than the network. The message names the cron; run it by hand and read its logger lines.",
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
    # The Kubernetes arms of the old k8s_workloads row now live in the k8s-workloads rules. This
    # row keeps only the log-pattern arm, which reads Loki; M6's batch A7 ports it to loki-homelab.
    # `startup_grace` rather than a gate: the row reads no Prometheus, and the arm fails open on a
    # Loki error by itself, so it names no gate.
    PushCheck(
        name="workload_log_errors", fn=logs.check_workload_log_errors, token="monitor_bridge_k8s_workloads_push_token",
        kuma_id="monitor-bridge-k8s-workloads", display="Workload Log Errors", status_group="Cluster Health", gate="startup_grace",
        description="Loki via monitor-bridge: a container logged more than LOG_ERROR_MAX (20) lines matching panic, fatal, traceback or out of memory in the last hour. The message names the container. The Kubernetes arms of the old k3s Workload Health tile are the k8s-workloads alert rules.",
    ),
    # The three reachability gates. A gate has no gate of its own: a tile that cannot go red is
    # not coverage (#2825).
    PushCheck(
        name="prometheus", fn=cluster.check_prometheus, token="monitor_bridge_prometheus_push_token",
        kuma_id="monitor-bridge-prometheus", display="Prometheus Reachable", status_group="Observability", is_gate=True,
        description="monitor-bridge's gate for every Prometheus-backed check: a vector(1) query every 5 min. DOWN: Prometheus itself is unreachable, and every dependent tile is held green with a `skipped` message so this one pages alone. Check the prometheus pod first.",
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
