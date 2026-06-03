# grafana — Metrics dashboards + log aggregation

Grafana with a co-deployed Loki/Promtail logging stack. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `grafana/grafana:latest` + `grafana/loki` + `grafana/promtail`
- **Host:** daniel-server · **Port:** 3000 · **URL:** `grafana.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, **prometheus**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Two datasources: **Prometheus** (metrics) and **Loki** (logs). Promtail ships container
  logs into Loki.
- Loki/Promtail config in `templates/loki-config.yml.j2`, `promtail-config.yml.j2`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Logging: `templates/loki-config.yml.j2`, `promtail-config.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "grafana"`
