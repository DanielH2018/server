# grafana — dashboards-tree source only (nothing deploys from here)

> **Deploy machinery retired 2026-08-14** (Phase F drain): promtail — this role's last
> deployable — moved to the unpinned promtail DaemonSet (roles/k8s/loki-homelab), which
> ships BOTH nodes' authlog/syslog + pod logs to loki-homelab. What deliberately did NOT
> move: the Docker remnant's container-stdout stream (docker_sd via docker-proxy) — the
> residual Docker set's stdout is on-host only (`docker logs`), accepted with the
> residual tier. `files/dashboards/` remains the cluster grafana's dashboards tree,
> read at deploy time via playbook_dir by the claude-otel role — that is this role's
> whole remaining purpose, and why the directory is not archived.

See repo-root `CLAUDE.md` for shared conventions. The role name is historical: the
**grafana container moved to the cluster** (E1, 2026-08-12 — the claude-otel role's
grafana serves `grafana.<domain>` now) and the **Docker loki cut executed 2026-08-13**
(D.2 step 4, four days early by operator decision; container, `loki` volume, and
pre-cutover history all discarded, `loki-docker-retiring` datasource deleted). What this
role still owns:

1. **promtail** — the host's log tailer (/var/log + Docker container logs), pushing to
   the cluster loki-homelab as its sole sink. The F join (2026-08-13) made a DaemonSet
   possible, but it stays a Docker container until the DRAIN step — the cluster promtail
   DS is pinned to daniel-box meanwhile to avoid double-shipping this host's logs.
   Its 9102 metrics scrape and monitor-bridge's `TARGETS_MIN` floor (see check.py for
   the live value) hold until that drain step drops them together.
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
- **E1 leftovers swept 2026-08-13:** the dead datasources/dashboards/admin_password
  renders and the admin-password exec against the retired container are gone from
  tasks/main.yml. A `grafana_admin_password` rotation reaches the live admin by
  redeploying **claude-otel**, whose post-deploy reset task now carries the same
  init-only-env fix this role used to (Grafana reads the Secret only at DB init).
- `templates/provisioning/datasources.yml.j2` is no longer deployed anywhere live, but
  it stays the **uid registry** the `validate-grafana-dashboards` prek guard parses:
  every dashboard's datasource ref must resolve to a uid declared there (`EGdsQqhVk`
  Prometheus / `bf4q19tuivta8e` Loki — the cluster grafana declares the same uids).

## Editing
- Compose: `templates/docker-compose.yml.j2` (single `promtail` service) · Tailing:
  `templates/promtail-config.yml.j2`
- Dashboards: edit `files/dashboards/**/*.json` (or edit in the cluster grafana UI and
  run `scripts/export_grafana_dashboards.py` to round-trip — it execs into the
  observability/grafana pod via `sudo k3s kubectl`, so expect a sudo prompt), then
  deploy **claude-otel**. `scripts/fetch_grafana_dashboards.py` refreshes the two
  community boards (1860, 14282).
- Deploy (promtail only): `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
