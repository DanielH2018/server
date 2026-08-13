# scrutiny — SMART collector spoke (since slice-7 Phase D)

Since 2026-08-10 (slice-7-phase-d-scrutiny.md, SC6) this role is only the **collector
spoke**: web UI + InfluxDB live in the cluster (`roles/k8s/scrutiny`), and this container
pushes daniel-server's NVMe SMART data to the cluster web API through the Authelia
`^/api/.*` LAN bypass. The cluster's collector DaemonSet covers daniel-box; at the Phase F
join it covers daniel-server too and this role retires.

## At a glance
- **Image:** `ghcr.io/analogj/scrutiny:master-collector` (rolling branch tag — manual
  update tier, watchtower opted out, Renovate can't track it; update via
  `deploy -t scrutiny -e common_pull=always`)
- **Host:** daniel-server · **Web UI:** none here — `https://scrutiny-k8s.local.<domain>`
  (Authelia) is the UI for BOTH nodes
- **Networks:** monitoring (egress only) · **Device:** `{{ scrutiny_nvme_device }}`
  (host_vars) with SETUID/SETGID/SYS_RAWIO/SYS_ADMIN
- **Monitoring:** monitor-bridge's "SMART Data Freshness" (26 h window, now against the
  cluster API and covering both nodes) + docker-fleet liveness; the `k3s Scrutiny` Kuma
  tile probes the cluster web

## Editing
- Compose: `templates/docker-compose.yml.j2` · Cluster stack: `ansible/roles/k8s/scrutiny/`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "scrutiny"`
