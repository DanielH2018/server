# nut-exporter — UPS variables as Prometheus series

`DRuggeri/nut_exporter` as a NUT network client. It dials upsd on the `nut` ClusterIP, reads the
UPS variables, and serves them as `network_ups_tools_*` on `:9199`. One Deployment, one Service,
no volume, no route, no secret.

**Deploy tag:** `--tags "nut-exporter"`.

## Why it exists separately from `nut`

Before this role, UPS state reached Grafana only as `hass_*` series — Prometheus scraped Home
Assistant's `/api/prometheus`, and HA's NUT integration was the only thing talking to upsd. That
routes a power-loss signal through a workload the UPS itself protects. These series do not.

It is **not** a sidecar in the nut pod, deliberately. That pod is `privileged`, holds the UPS's
USB device, is pinned to daniel-server for that reason, uses `Recreate`, and is the pod half of
the emergency-shutdown chain. A sidecar would inherit all of it and would restart the shutdown
chain on every exporter image bump. This pod holds no device, so it can schedule on either node
and its `k8s_autodeploy` stance is `true` where `nut`'s is `false`.

## It holds no credential, and that is upsd's design

upsd permits reading variables anonymously; authentication gates the upsmon roles and instant
commands, not `LIST VAR`. Confirmed on daniel-server 2026-09-10 — `upsc apc-ups@127.0.0.1`
returns every variable with no credentials — and the nut pod's own livenessProbe already relies
on it. So there is no `upsd.users` entry for this workload and nothing to rotate. Do not add one
on the assumption that a scrape must authenticate.

## Two things that bite

**The UPS reading is on `/ups_metrics`, not `/metrics`.** `/metrics` is the exporter's own Go and
process registry and carries no UPS variable at all, so a scrape job pointed there succeeds and
returns nothing useful. The scrape job in `claude-otel/templates/prometheus.yaml.j2` (`job_name:
nut`) sets `metrics_path` accordingly and passes `ups=` as a param — the exporter fails a scrape
outright if it discovers several UPS devices and has not been told which, so naming it means a
second UPS cannot silently blank the job.

**The probes are `tcpSocket` for two separate reasons.** Probing `/ups_metrics` would crash-loop
this pod whenever upsd is unreachable, turning a metrics gap into a second outage — Prometheus
already reports that as `up == 0`. Probing `/metrics` would trip
`ansible/tests/k8s/test_probes_do_not_flood_metrics.py`, which refuses a kubelet httpGet on a
metrics route below a 300s period after node-exporter's probes produced 97% of the namespace's
Loki ingest.

## Which variables

`nut_exporter_k8s_variables` in `defaults/main.yml`, stated in full rather than defaulted:
upstream's default omits `battery.runtime`, which is the one an operator reads during an outage.
A variable this UPS does not report is simply absent from the output, not an error.

`ups.status` is the one that is not a number. It renders as
`network_ups_tools_ups_status{flag="OL"} 1`, with a forced 0 for every flag in the exporter's
`--nut.statuses` default the UPS is not asserting — so `flag="OB"` is a real 0/1 series rather
than one that exists only during an outage.

## NetworkPolicy

The pod carries `netpol-baseline: enforced`, which is what admits Prometheus to it. Its outbound
call to upsd is admitted by the `app: nut-exporter` podSelector in
`netpol-baseline/templates/networkpolicy-nut.yaml.j2` — a podSelector and not an ipBlock, because
pod → ClusterIP → pod traffic keeps the caller's pod IP and never arrives as a node address.
