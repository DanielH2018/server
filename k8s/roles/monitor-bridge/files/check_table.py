"""Every check and gate monitor-bridge runs.

This table is the one place a check is declared. Everything else derives from it:

- `registry.build_checks` turns every row into a `Check` carrying its `gate`, the two gate
  rows first. `check.run_once` skips a row while the gate row it names reads down.
  `EXPORTER_DEPENDENT` stays in `check.py`: it is keyed by scrape job, and one check can sit
  under two jobs.
- `cli.main` counts the table's enabled rows into `homelab_check_rows`, which `BridgeCheckStale`
  compares with the rows that export a verdict.
- `scripts/docs/fragments_lib/fragments_bridge.py` reads it through
  `scripts/renderer/template_filters/py_table.py` for the generated bridge fragments. That
  filter parses this file without running it, which is why every field but `fn` is a literal
  and why this module imports only check bodies.

Adding a check takes one row here, one `check_*` body, and one rule in
`k8s/observability/files/rules/bridge-exporter.yaml`.

Stdlib only, like every module under files/.
"""

from bridge.types import PushCheck
from checks import (
    b2,
    cloudflare_ips,
    domain_registration,
    healthchecks,
    host_edge,
    host_packages,
    host_thermal,
    notify,
    r2,
    service,
    uptime_robot,
    wan,
)

# One row per line rather than ruff's one argument per line, which keeps the table short.
# fmt: off
CHECKS: tuple[PushCheck, ...] = (
    PushCheck(name="n8n", fn=service.check_n8n, gate="startup_grace"),
    # Not startup-graced: its own ARR_FETCH_CONSECUTIVE streak covers the reboot transient longer,
    # and compounding both would only delay a page.
    PushCheck(name="arr_queue", fn=service.check_arr_queue),
    PushCheck(name="bazarr", fn=service.check_bazarr, gate="startup_grace"),
    PushCheck(name="prowlarr_indexers", fn=service.check_prowlarr_indexers, gate="startup_grace"),
    # Under EXPORTER_DEPENDENT["node"] in check.py as well.
    PushCheck(name="host_packages", fn=host_packages.check_host_packages, gate="prometheus", runbook="host-packages"),
    PushCheck(name="etcd_restore_drill", fn=service.check_etcd_restore_drill, runbook="k3s-etcd-restore"),
    PushCheck(name="scrutiny", fn=host_thermal.check_scrutiny, gate="startup_grace", critical=True, runbook="alert-runbooks"),
    PushCheck(name="ha_heartbeat", fn=service.check_ha_heartbeat),
    PushCheck(name="airflow_scheduler", fn=service.check_airflow_scheduler, gate="startup_grace"),
    # The producer is the bridge re-reading the newest result each cycle, not the 6-hourly run; the
    # staleness arm catches a dead scheduler.
    PushCheck(name="speedtest", fn=host_edge.check_speedtest, gate="startup_grace"),
    PushCheck(name="discord", fn=notify.check_discord, gate="wan_reachable", critical=True, runbook="alert-runbooks"),
    # WAN-gated rather than startup-graced: the WAN gate covers the reboot transient.
    PushCheck(name="r2_usage", fn=r2.check_r2_usage, gate="wan_reachable", critical=True, runbook="longhorn-backup-tiering"),
    # Proves ADR-0041's R2 lock over etcd-snapshots/ with a daily canary; holds a verdict a day.
    PushCheck(name="r2_etcd_lock", fn=r2.check_r2_etcd_lock, gate="wan_reachable", runbook="longhorn-backup-tiering"),
    PushCheck(name="cloudflare_ips_drift", fn=cloudflare_ips.check_cloudflare_ips_drift, gate="wan_reachable"),
    # Same cache shape as cloudflare_ips_drift.
    PushCheck(name="healthchecks_drift", fn=healthchecks.check_healthchecks_drift, gate="wan_reachable", runbook="healthchecks-io-deadman"),
    # Down only when RDAP cannot be read; the days-left gauge carries the expiry thresholds.
    PushCheck(name="domain_registration", fn=domain_registration.check_domain_registration, gate="wan_reachable"),
    # Same cache shape as healthchecks_drift. A monitor that is down is Uptime Robot's own page.
    PushCheck(name="uptime_robot_monitors", fn=uptime_robot.check_uptime_robot_monitors, gate="wan_reachable", runbook="uptime-robot-monitors"),
    PushCheck(name="b2_storage", fn=b2.check_b2_storage, gate="b2_reachable", critical=True, runbook="longhorn-backup-tiering"),
    # Reads the bucket's Object Lock (ADR-0041) and holds a verdict a day, either outcome.
    PushCheck(name="b2_object_lock", fn=b2.check_b2_object_lock, gate="b2_reachable", runbook="longhorn-backup-tiering"),
    # A gate has no gate of its own: a gate that cannot go red is not coverage. No `prometheus`
    # gate, as the Watchdog covers it.
    PushCheck(name="b2_reachable", fn=b2.check_b2_reachable, is_gate=True, critical=True, runbook="longhorn-backup-tiering"),
    PushCheck(name="wan_reachable", fn=wan.check_wan_reachable, is_gate=True, critical=True, runbook="alert-runbooks"),
)
# fmt: on
