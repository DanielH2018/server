# Slice-7 Phase D — scrutiny to the cluster

Planned 2026-08-10. Parent: `slice-7-drain-and-join.md` (Phase D: "scrutiny: collector →
DaemonSet on both nodes, influxdb+web to the cluster"). Decisions SC1–SC6.

## What the survey established

- Three containers under one `scrutiny` containers_list entry: web (`master-web`, Authelia
  UI + API), influxdb (`influxdb:2.9`, SMART history — 3.1 MB), collector
  (`master-collector`, daily 00:00 cron, caps SETUID/SETGID/SYS_RAWIO/SYS_ADMIN +
  `/dev/nvme0`, pushes to the web API over the internal net).
- Consumers: monitor-bridge (`SCRUTINY_URL` → `/api/summary`, the "SMART Data Freshness" +
  health push checks), probe.py `scrutiny`, the Traefik label route. No homepage widget.
  The three docker-type Kuma tiles are already inert (fleet-wide KD2 retirement).
- **daniel-box's NVMe (CT1000, 1 TB) has never been SMART-monitored** — only
  daniel-server's SHPP41 is. The move closes that gap, not just relocates the service.
- Rolling `master-*` branch tags: manual-update tier, deliberately un-Renovate-able —
  policy must carry over.
- Secrets already in SOPS: `scrutiny_influxdb_admin_password`, `scrutiny_influxdb_token`.

## Decisions

### SC1 — one k8s role, mirroring the three-way split
`roles/k8s/scrutiny`: web Deployment + influxdb Deployment (Recreate, its own PVC) +
collector DaemonSet, in the `homelab` namespace. Same images, same env/entrypoint idioms
(the token-from-file wrapper carries over; k8s Secrets at defaultMode 0444 — the
root-owned-0400 trap). Update policy stays manual: no Renovate tracking on branch tags,
update = redeploy with pull.

### SC2 — history seeds into a backed-up Longhorn PVC
The influxdb2 dir (3.1 MB) seeds into a default-class (backed-up) PVC — SMART history is
trend data, cheap to keep and the whole point of the tool. The web config SQLite (32 KB)
seeds likewise into the web's PVC.

### SC3 — collector: DaemonSet now covers daniel-box; daniel-server keeps a Docker spoke until F
A DaemonSet with the SMART caps + hostPath `/dev` runs on every node — today that is
daniel-box only, which **adds** its never-monitored NVMe. daniel-server is not a node
yet, so its existing Docker collector survives, re-pointed at the cluster web API — the
same forwarder-until-the-join shape as D7. At Phase F the DaemonSet lands on
daniel-server automatically and the Docker spoke (the whole remaining compose) retires.
Try caps + hostPath first; fall back to `privileged: true` only if the device cgroup
blocks the ioctl, and record which one held.

### SC4 — API access via Authelia bypass, the uptime-kuma pattern
One `scrutiny-k8s` route with Authelia for the UI, plus config-secret bypass rules for
`^/api/.*` restricted to LAN networks — the collector POSTs and monitor-bridge GETs are
machine traffic that cannot 302-dance. (The Docker spoke's push crosses the LAN over
TLS; ClientIP can't gate this one because the DaemonSet's own POSTs arrive from the pod
CIDR too — the bypass network list covers both.)

### SC5 — consumer re-points
monitor-bridge `SCRUTINY_URL` → `https://scrutiny-k8s.local.<domain>`; probe.py
`scrutiny` → the same host VIP-pinned (generalize the loki_endpoint pin rather than
adding a second bespoke one); a `scrutiny-k8s.json` static Kuma entity (http, 302-aware,
like the other Authelia-fronted k8s tiles). Docker tiles need no deletion — they were
already superseded by docker-fleet.

### SC6 — retirement order
Deploy cluster stack (dark, scrutiny-k8s name) → seed influxdb2 + config → verify UI +
`/api/summary` shows BOTH nodes' devices with fresh timestamps → re-point monitor-bridge
+ probe.py → slim the Docker compose to collector-only (web + influxdb retire; the
containers_list entry stays for the spoke) → Phase F removes the rest. The
`scrutiny_internal` Docker net and the web's Traefik route go with the slim-down.

## Execution order

1. Build `roles/k8s/scrutiny` (SC1) + inventory entry (`hostname: scrutiny-k8s`) +
   Authelia bypass (SC4) + Pi-hole redeploys. Deploy dark.
2. Seed influxdb2 + web config from the Docker copies (SC2); verify UI up, history
   present, daniel-box device appears after the first DaemonSet collector run (trigger
   one manually rather than waiting for 00:00).
3. Re-point the Docker collector at the cluster API (SC3); verify daniel-server's device
   reports fresh through it.
4. Re-point monitor-bridge + probe.py + Kuma entity (SC5); verify SMART Data Freshness
   green against the cluster.
5. Slim the Docker compose to collector-only (SC6); drop the Traefik route + internal
   net; docs.

## Unverified — resolve during execution

- Whether caps + hostPath suffice for the NVMe admin ioctl under k8s's device cgroup, or
  the DaemonSet needs `privileged: true` (SC3).
- Whether the collector tolerates an HTTPS + bypass API endpoint (it should — plain
  `COLLECTOR_API_ENDPOINT` URL — but the 302-on-miss shape is worth one probe).
- Whether influxdb 2.9's first-run setup skips cleanly on a seeded volume (it keys on
  `influxd.bolt` existing — expected yes, same as the Docker deploy's re-runs).
