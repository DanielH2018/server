# prometheus — Host exporters (Prometheus itself retired 2026-08-12)

The Prometheus server this role was named for retired in Phase E2 of the k3s migration — the
cluster prometheus (claude-otel stack) now scrapes everything directly. What remains here are
the two daniel-server host exporters it scrapes over the LAN. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `prom/node-exporter:latest` + `ghcr.io/google/cadvisor` (version-pinned,
  Renovate-managed)
- **Host:** daniel-server · no web route (`containers_list` entry is name+networks only)
- **Networks:** monitoring
- **Ports:** node-exporter `{{ server_ip }}:9100`, cadvisor `{{ server_ip }}:9101` —
  LAN-published for the cluster scrape, locked to daniel-box by
  `prometheus-exporters-lan-firewall.sh` (DOCKER-USER; the same unit also guards promtail
  9102 published from its own role)

## Notable
- **node-exporter** (host metrics) and **cAdvisor** (per-container CPU/mem) — the data behind
  the M1 resource-limit tuning; scraped as jobs `node`/`cadvisor` with
  `instance: daniel-server` labels in claude-otel's `prometheus.yaml.j2`.
- The textfile-collector dir `/var/lib/node-exporter-textfile` takes host-cron `.prom` gauges
  (e.g. `kopia_b2_billable_bytes`).

## Editing
- Compose: `templates/docker-compose.yml.j2` · Scrape jobs live in
  `ansible/roles/k8s/claude-otel/templates/prometheus.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "prometheus"`
