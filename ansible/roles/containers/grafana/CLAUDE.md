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
    and `foldersFromFilesStructure: true` pointing at `/var/lib/grafana/dashboards`. Each
    subdirectory becomes a Grafana folder of the same name (e.g. `dashboards/Crowdsec/`).
  - `files/dashboards/**/*.json` — **every** dashboard is provisioned as code, from two
    sources (see *Editing* below):
    - **Community boards** (`node-exporter-full`, `cadvisor`, `traefik`) — upstream is
      grafana.com (1860 / 14282 / 17346).
    - **Custom boards** — upstream is the live Grafana DB: the CrowdSec set
      (`Crowdsec/`), the Loki log views (`system-logs`, `docker-app-logs`),
      `docker-and-system-monitoring`, and `traefik-custom`.
    - All datasource references are **pinned to the provisioned uids** (`EGdsQqhVk`
      Prometheus / `bf4q19tuivta8e` Loki) so they resolve without the import prompt that
      file-provisioning skips. A stale Prometheus uid (`IH0jqv6nz`) that lingered in a
      hand-imported CrowdSec board is remapped to `EGdsQqhVk` at export time.
- Editing in the UI still works — changes persist in Grafana's DB (`./data`); the JSON files
  **re-seed** a dashboard whenever their *content* changes (Grafana ignores the JSON
  `version` field for provisioned boards — the export script pins it to 1 purely so
  drift-check re-exports don't produce noise diffs).
- Promtail ships container logs into Loki for the Explore/log views.
- **Loki has no Docker healthcheck** — the image is a single Go binary (no shell/wget), so
  its Kuma monitor is an **HTTP probe of `http://loki:3100/ready`** instead of the default
  container-running docker monitor. NB `/ready` 503s for ~15s after a restart while the
  ingester warms up — brief PENDING in Kuma after a deploy is normal.
- Loki/Promtail config in `templates/loki-config.yml.j2`, `promtail-config.yml.j2`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Logging: `templates/loki-config.yml.j2`, `promtail-config.yml.j2`
- Datasources/dashboards: `templates/provisioning/*.j2`, `files/dashboards/*.json`
- Two generator scripts keep `files/dashboards/` in sync, owning **disjoint** files:
  - `scripts/fetch_grafana_dashboards.py` — *grafana.com → code*. Fetches the community
    boards, pins datasource uids, and bakes a working default into each template variable so
    panels render on first load without manual dropdown selection.
  - `scripts/export_grafana_dashboards.py` — *live DB → code*. Dumps every `dash-db`
    dashboard **except** the community ones (`SKIP_UIDS`), preserving the live folder
    structure as subdirectories and remapping stale datasource uids. **Run this after
    editing a custom board in the UI** to capture the change back into version control.
- To add your own dashboard: build it in the UI, then run `export_grafana_dashboards.py`
  (it will be captured into the matching folder), **or** drop its JSON in `files/dashboards/`
  manually (pin datasource refs to uid `EGdsQqhVk` for Prometheus / `bf4q19tuivta8e` for
  Loki) and redeploy.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
