# prometheus — Metrics collection

Prometheus plus its exporters; the scrape source for Grafana. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `prom/prometheus:latest` + `prom/node-exporter:latest`
  + `ghcr.io/google/cadvisor` (container metrics)
- **Host:** daniel-server · **Port:** 9090 · **URL:** `prometheus.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Bundles **node-exporter** (host metrics) and **cAdvisor** (per-container CPU/mem) — the
  data behind the M1 resource-limit tuning.
- Scrape targets in `templates/prometheus.yml.j2`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Scrape cfg: `templates/prometheus.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "prometheus"`
