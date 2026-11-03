"""Every check and gate monitor-bridge runs (#3659).

This table is the one place a check is declared. Everything else derives from it:

- `registry.build_checks` turns every row into a `Check` carrying its `gate`, the two gate
  rows first. `check.run_once` skips a row while the gate row it names reads down.
  `EXPORTER_DEPENDENT` stays in `check.py`: it is keyed by scrape job, and one check can sit
  under two jobs.
- `cli.main` counts the table's enabled rows into `homelab_check_rows`, which `BridgeCheckStale`
  compares with the rows that export a verdict.
- `scripts/docs/fragments_lib/fragments_bridge.py` reads it through
  `ansible/filter_plugins/py_table.py` for the generated bridge fragments. That filter parses
  this file without running it, which is why every field but `fn` is a literal and why this
  module imports only check bodies.

Adding a check takes one row here, one `check_*` body, and one rule in
`k8s/observability/files/rules/bridge-exporter.yaml`.

Stdlib only, like every module under files/.
"""

from bridge.types import PushCheck
from checks import (
    b2,
    cloudflare_ips,
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

# One row per line rather than ruff's one argument per line, which keeps the table short.
# fmt: off
CHECKS: tuple[PushCheck, ...] = (
    PushCheck(name="n8n", fn=service.check_n8n, gate="startup_grace"),
    # Not startup-graced: its own ARR_FETCH_CONSECUTIVE streak covers the reboot transient for
    # longer, and compounding both would only delay a page.
    PushCheck(name="arr_queue", fn=service.check_arr_queue),
    PushCheck(name="bazarr", fn=service.check_bazarr, gate="startup_grace"),
    PushCheck(name="prowlarr_indexers", fn=service.check_prowlarr_indexers, gate="startup_grace"),
    PushCheck(name="gitops_alive", fn=gitops.check_gitops_alive),
    PushCheck(name="gitops_status", fn=gitops.check_gitops_status),
    # A row of its own, not an arm of gitops_status: that check reads the deployer's state
    # directory outside the Prometheus gate, and an arm reading Prometheus there would page on
    # every Prometheus outage. Under EXPORTER_DEPENDENT["node"] in check.py as well.
    PushCheck(name="host_packages", fn=gitops.check_host_packages, gate="prometheus"),
    # Reads the weekly drill's stamp off a hostPath, fail-closed, every cycle; the staleness window
    # is ETCD_DRILL_MAX_AGE_S.
    PushCheck(name="etcd_restore_drill", fn=service.check_etcd_restore_drill, runbook="k3s-etcd-restore"),
    PushCheck(name="scrutiny", fn=host_thermal.check_scrutiny, gate="startup_grace", critical=True),
    PushCheck(name="ha_heartbeat", fn=service.check_ha_heartbeat),
    # The producer is the bridge re-reading the newest result every cycle, not the 6-hourly run;
    # the check's staleness arm catches a dead scheduler.
    PushCheck(name="speedtest", fn=host_edge.check_speedtest, gate="startup_grace"),
    # #1869: kuma-push-lib.sh logs a lost push and returns 0, so a lost DOWN surfaced only at the
    # monitor's deadline, a day late for the daily producers. Reads that log line out of Loki and
    # pages only when a sibling cron's push landed in the same window, or when Kuma itself
    # rejected the push (`by=kuma`): a fleet-wide loss is the edge or host monitors' page.
    PushCheck(name="swallowed_verdicts", fn=logs.check_swallowed_verdicts, gate="startup_grace"),
    PushCheck(name="discord", fn=notify.check_discord, gate="wan_reachable", critical=True),
    # WAN-gated rather than startup-graced since #2784; the WAN gate covers the reboot transient.
    PushCheck(name="r2_usage", fn=r2.check_r2_usage, gate="wan_reachable", critical=True, runbook="longhorn-backup-tiering"),
    # cloudflare_ips gates Traefik's forwardedHeaders.trustedIPs. The bridge caches a success for a
    # day and re-probes a failure every cycle.
    PushCheck(name="cloudflare_ips_drift", fn=cloudflare_ips.check_cloudflare_ips_drift, gate="wan_reachable"),
    # #2566. Same cache shape as cloudflare_ips_drift.
    PushCheck(name="healthchecks_drift", fn=healthchecks.check_healthchecks_drift, gate="wan_reachable"),
    PushCheck(name="b2_storage", fn=b2.check_b2_storage, gate="b2_reachable", critical=True, runbook="longhorn-backup-tiering"),
    # The two reachability gates. A gate has no gate of its own: a gate that cannot go red is
    # not coverage (#2825). The `prometheus` gate left with Cutover-2: the Watchdog covers a dead
    # Prometheus, and `host_packages` keeps the value as the label of its exporter suppression.
    PushCheck(name="b2_reachable", fn=b2.check_b2_reachable, is_gate=True, critical=True, runbook="longhorn-backup-tiering"),
    PushCheck(name="wan_reachable", fn=wan.check_wan_reachable, is_gate=True, critical=True),
)
# fmt: on
