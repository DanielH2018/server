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
    default), **Loki** (uid `bf4q19tuivta8e`, pointed at the CLUSTER loki-homelab since
    Phase D.2), and the non-default **loki-docker-retiring** (pre-cutover history; retires
    with the Docker loki container). All `editable: true`. **The Prometheus/Loki uids are
    adopted from the original hand-created datasources** so the 9 pre-existing dashboards
    (Crowdsec/Traefik/logs) keep resolving — provisioning updates them in place by uid
    rather than delete/recreate. (Tempo and the Loki↔Tempo trace cross-links retired at
    D7 with the Docker tempo — the cluster claude-otel Grafana carries them now.)
  - `templates/provisioning/dashboards.yml.j2` — a file provider with `allowUiUpdates: true`
    and `foldersFromFilesStructure: true` pointing at `/var/lib/grafana/dashboards`. Each
    subdirectory becomes a Grafana folder of the same name (e.g. `dashboards/Security/`).
  - `files/dashboards/**/*.json` — **every** dashboard is provisioned as code, sorted into
    functional folders (`AI/`, `Apps/`, `Infrastructure/`, `Logs/`, `Networking/`, `Security/`),
    from two sources (see *Editing* below):
    - **Community boards** — upstream is grafana.com: `Infrastructure/node-exporter-full`
      (1860) and `Infrastructure/cadvisor` (14282).
    - **Custom boards** — upstream is the live Grafana DB: the CrowdSec set + `lapi-metrics`
      (`Security/`); `home-assistant`, `uptime-kuma`, `backups`, `player-stats` (`Apps/`);
      `docker-and-system-monitoring`, `ups`, `alert-history` (`Infrastructure/`); the Loki log
      views `logs` + `loki-internals` (`Logs/`); and `traefik-custom` (`Networking/`).
      (The `AI/claude-code` board retired at D7 with the otel-collector scrape job — the
      live board is the cluster claude-otel Grafana's.) `ups.json` is the visual companion to monitor-bridge's UPS Battery
      Health check (its runtime-trend panel is the slow battery-decay view the alert floor can't
      show); `alert-history.json` reconstructs monitor-bridge DOWN episodes from Loki (the board
      twin of `probe.py alerts`). These are hand-authored seeds — edit-in-UI then
      `export_grafana_dashboards.py` to round-trip like the rest.
    - All datasource references are **pinned to the provisioned uids** (`EGdsQqhVk`
      Prometheus / `bf4q19tuivta8e` Loki) so they resolve without the import prompt that
      file-provisioning skips. A stale Prometheus uid (`IH0jqv6nz`) that lingered in a
      hand-imported CrowdSec board is remapped to `EGdsQqhVk` at export time.
- Editing in the UI still works — changes persist in Grafana's DB (`./data`); the JSON files
  **re-seed** a dashboard whenever their *content* changes (Grafana ignores the JSON
  `version` field for provisioned boards — the export script pins it to 1 purely so
  drift-check re-exports don't produce noise diffs).
- **The admin password is synced on rotation, not just at init.**
  `GF_SECURITY_ADMIN_PASSWORD__FILE` is only consulted when Grafana initialises its DB, so a
  rotated `grafana_admin_password` would otherwise never reach the live admin user (SOPS and the
  actual login diverge silently). A post-deploy task runs `grafana cli admin
  reset-admin-password` — it writes straight to the DB and needs no knowledge of the *current*
  password, unlike `PUT /api/admin/users/1/password`, which authenticates as admin and is
  therefore useless in exactly the case that matters. Gated on the password file changing, with
  a retry loop because the CLI exits non-zero while a fresh Grafana is still migrating.
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
- **Datasource-uid guard:** `scripts/validate_grafana_dashboards.py` (prek hook
  `validate-grafana-dashboards`, + `scripts/test_validate_grafana_dashboards.py`) asserts every
  `files/dashboards/**/*.json` datasource ref resolves to a uid/name declared in
  `datasources.yml.j2` (or a Grafana built-in). A wrong/empty uid → silent "No data"; this
  catches it before deploy. The valid set is parsed from the template, so adding a datasource
  there is enough — no edit to the guard.
- To add your own dashboard: build it in the UI, then run `export_grafana_dashboards.py`
  (it will be captured into the matching folder), **or** drop its JSON in `files/dashboards/`
  manually (pin datasource refs to uid `EGdsQqhVk` for Prometheus / `bf4q19tuivta8e` for
  Loki) and redeploy.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
