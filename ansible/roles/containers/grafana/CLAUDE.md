# grafana — Metrics dashboards + log aggregation

Grafana with a co-deployed Loki/Promtail logging stack. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `grafana/grafana:latest` + `grafana/loki:latest` + `grafana/promtail:latest`
- **Host:** daniel-server · **Port:** 3000 · **URL:** `grafana.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, **prometheus**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Datasources + dashboards are provisioned as code** (not hand-clicked in the UI):
  - `templates/provisioning/datasources.yml.j2` — **Prometheus** (uid `EGdsQqhVk`,
    default) and **Loki** (uid `bf4q19tuivta8e`). Both `editable: true`. **The uids are
    adopted from the original hand-created datasources** so the 9 pre-existing dashboards
    (Crowdsec/Traefik/logs) keep resolving — provisioning updates them in place by uid
    rather than delete/recreate.
  - `templates/provisioning/dashboards.yml.j2` — a file provider with `allowUiUpdates: true`
    pointing at `/var/lib/grafana/dashboards`.
  - `files/dashboards/*.json` — seed dashboards (Node Exporter Full 1860, cAdvisor 14282,
    Traefik 17346). Their datasource references are **pinned to the Prometheus uid
    `EGdsQqhVk`** (the grafana.com `${DS_*}` import placeholders are rewritten at fetch
    time), so they resolve without the import prompt that file-provisioning skips.
- Editing in the UI still works — changes persist in Grafana's DB (`./data`); the JSON files
  are read-only and only **re-seed** a dashboard when their internal `version` is bumped.
- Promtail ships container logs into Loki for the Explore/log views.
- Loki/Promtail config in `templates/loki-config.yml.j2`, `promtail-config.yml.j2`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Logging: `templates/loki-config.yml.j2`, `promtail-config.yml.j2`
- Datasources/dashboards: `templates/provisioning/*.j2`, `files/dashboards/*.json`
- The seed dashboards in `files/dashboards/` are produced by
  `scripts/fetch_grafana_dashboards.py` (fetches the grafana.com boards, pins datasource
  uids, and bakes a working default into each template variable so panels render on first
  load without manual dropdown selection). Re-run it to refresh them.
- To add your own dashboard: drop its JSON in `files/dashboards/` (pin datasource refs to
  uid `EGdsQqhVk` for Prometheus / `bf4q19tuivta8e` for Loki), then redeploy.
- Deploy: `ansible-playbook ansible/deploy.yml --tags "grafana"`
