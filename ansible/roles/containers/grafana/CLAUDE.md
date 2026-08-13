# grafana — promtail host-tailer + the dashboards source tree

See repo-root `CLAUDE.md` for shared conventions. The role name is historical: the
**grafana container moved to the cluster** (E1, 2026-08-12 — the claude-otel role's
grafana serves `grafana.<domain>` now) and the **Docker loki cut executed 2026-08-13**
(D.2 step 4, four days early by operator decision; container, `loki` volume, and
pre-cutover history all discarded, `loki-docker-retiring` datasource deleted). What this
role still owns:

1. **promtail** — the host's log tailer (/var/log + Docker container logs), pushing to
   the cluster loki-homelab as its sole sink. It stays a Docker container until the
   Phase F join (KL2: a DaemonSet cannot see a non-node host's logs), then converts.
   Its 9102 metrics scrape and monitor-bridge's `TARGETS_MIN=4` hold until F.
2. **The dashboards source of truth** — `files/dashboards/**/*.json`. The cluster
   grafana does NOT copy this tree into its own role: `k8s/claude-otel/tasks/dashboards.yml`
   reads it from here (via `playbook_dir`) and bakes per-folder ConfigMaps. Editing a
   dashboard means editing here, then deploying **claude-otel**, not this role.

## At a glance
- **Image:** `grafana/promtail:latest`
- **Host:** daniel-server · no port/hostname (the 9102 LAN publish is the cluster
  Prometheus' scrape, firewalled to daniel-box)
- **Networks:** monitoring
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- promtail needs `DAC_READ_SEARCH` (reads root-owned /var/log and
  /var/lib/docker/containers) and persists its positions cursor in the
  `promtail_positions` named volume — without it every recreate re-reads all sources
  from the start and can trip Loki's ingestion rate limit.
- **E1 leftovers, swept at Phase F/G with the role's endgame:** the tasks still render
  datasources/dashboards/admin_password into `containers/grafana/` for the retired
  Docker grafana (dead writes, harmless), and the "Sync the live Grafana admin password"
  task still execs the nonexistent `grafana` container — a `grafana_admin_password`
  rotation would fail this role's deploy until that task is removed or re-pointed at the
  cluster grafana.
- `templates/provisioning/datasources.yml.j2` is no longer deployed anywhere live, but
  it stays the **uid registry** the `validate-grafana-dashboards` prek guard parses:
  every dashboard's datasource ref must resolve to a uid declared there (`EGdsQqhVk`
  Prometheus / `bf4q19tuivta8e` Loki — the cluster grafana declares the same uids).

## Editing
- Compose: `templates/docker-compose.yml.j2` (single `promtail` service) · Tailing:
  `templates/promtail-config.yml.j2`
- Dashboards: edit `files/dashboards/**/*.json`, then deploy **claude-otel**.
  `scripts/fetch_grafana_dashboards.py` refreshes the two community boards (1860, 14282).
  **`scripts/export_grafana_dashboards.py` is BROKEN since E1** — it `docker exec`s the
  retired `grafana` container; the UI→code round-trip needs re-pointing at the cluster
  grafana pod (same F/G sweep as the leftovers above). Until then, UI edits in the
  cluster grafana do not round-trip to the repo.
- Deploy (promtail only): `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
